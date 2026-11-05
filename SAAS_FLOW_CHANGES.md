# Caryanams DMS — SaaS Onboarding Flow Fix (v28 → v29)

Target flow, now enforced end to end:

```
caryanams.in
   └─ Landing Page  (/)
        └─ Sign Up / Login  (/auth/register · /auth/login)
             └─ Authentication
                  └─ SaaS Portal  (/portal)   ← decision point
                       ├─ no active plan → Select Plan  (/dealer/subscription?onboarding=1)
                       │                     └─ Payment / Activation
                       │                          └─ User Dashboard  (/dealer/dashboard)
                       └─ active plan    → User Dashboard  (/dealer/dashboard)
                                                 └─ Features · Subscription · Account
```

---

## 1. Why every user landed on the dashboard

Four separate defects stacked up. Fixing only the redirect would not have been enough.

### 1.1 The inverted expiry check — the root cause

`models.py`, `User.has_active_subscription()`:

```python
if not self.subscription_expiry:
    return True          # ← a brand-new dealer has expiry = NULL
```

A new account is created with `subscription_plan='starter'`,
`subscription_status='active'`, `subscription_expiry=None`. That combination
made **every new signup look like a paying subscriber**, so nothing anywhere
in the app had a reason to show plan selection.

**Fixed:** NULL expiry now means *no plan*. The authoritative check moved into
`subscription_gate.py`, which reads the `dealer_subscriptions` table first and
only falls back to the legacy `User.subscription_*` columns.

### 1.2 Login jumped straight to the dashboard

`auth/routes.py` did `redirect(url_for('dealer.dashboard'))` on success, with
no subscription check anywhere in between.

**Fixed:** login, signup and dealer-link login all redirect to
`/portal`, which is the only place that decides where a user goes.

### 1.3 The dashboard had no gate at all

`/dealer/dashboard` carried `@dealer_required @kyc_required` — nothing about
subscriptions. Typing the URL always worked, as did every other
`/dealer/*` and `/studio/*` route.

**Fixed:** `install_subscription_guard(app)` registers one `before_request`
hook that covers the entire `dealer` and `background` blueprints, so no route
can be reached by typing its URL. `@subscription_required` is also applied
explicitly to the dashboard for readability.

### 1.4 Activation never reached the database

`dealer/routes.py`, `activate_subscription()`:

```python
dealer_name = g.user.name if g.user else ''
```

`g.user` is a **dict** (see `db.user_get_by_id`), so `.name` raised
`AttributeError` → the global 500 handler returned JSON → and the page's
`.catch()` silently fell back to `doLocalActivation()`, which wrote the
"subscription" to **localStorage only**.

Consequences, all reported as separate bugs:

* the plan vanished after logout/login (nothing was ever persisted),
* the plan appeared active on one browser and not another,
* a dealer could fake a plan by editing localStorage,
* `plan_key == 'starter'` was rejected outright, so the free plan could never
  be activated and a new dealer had no way to finish onboarding at all.

**Fixed:** the dict access, the starter rejection, and the client-side
fallback are all gone. Activation goes through one writer,
`activate_plan_for_dealer()`, which commits a `DealerSubscription` row, a
`DealerPayment` audit row, and the legacy `User.subscription_*` sync in a
single transaction.

---

## 2. Files

### New

| File | Purpose |
|---|---|
| `subscription_gate.py` | Single source of truth: `subscription_state()`, `has_active_plan()`, `portal_target()`, `@subscription_required`, `install_subscription_guard()`, `activate_plan_for_dealer()` |
| `portal/__init__.py`, `portal/routes.py` | The SaaS Portal — `/portal`, `/portal/select-plan`, `/portal/status` |
| `apply_saas_flow_patch.py` | Re-runnable patcher for the edits below (`--check`, `--revert`) |
| `test_saas_flow.py` | 34-assertion end-to-end test of the whole flow |

### Modified

| File | Change |
|---|---|
| `models.py` | `has_active_subscription()` no longer treats NULL expiry as active; delegates to the gate |
| `app.py` | Registers `portal_bp`; installs the gate after `load_user`; adds `has_active_plan` / `subscription_state` / `portal_url` to every template; adds three config flags |
| `auth/routes.py` | Login → `/portal`; Sign Up authenticates the new account then → `/portal`; dealer-link login → `/portal` |
| `dealer/routes.py` | Dashboard gated; `subscription()` passes real state + onboarding mode; `activate_subscription()` rewritten; new `/dealer/subscription/status` |
| `templates/dealer/subscription.html` | Banner and buttons render from the database; Starter is selectable; localStorage persistence and the offline activation fallback removed; success redirects to the dashboard |
| `templates/user/base.html` | "DMS Dashboard" links point at `/portal` instead of the dashboard |

Backups are written as `<file>.bak_saasflow` on first patch;
`python apply_saas_flow_patch.py --revert` restores them.

---

## 3. New configuration

Add to `.env` (all optional — the defaults are what you want):

```ini
SUBSCRIPTION_GATE_ENABLED=True    # master switch for the plan gate
SUBSCRIPTION_DEFAULT_DAYS=30      # validity granted per activation
AUTO_LOGIN_AFTER_SIGNUP=True      # Sign Up → Portal, matching Login
ALLOW_FREE_PLAN_ACTIVATION=True   # existing flag: free activation of PAID plans
RAZORPAY_ENABLED=False            # existing flag
```

`ALLOW_FREE_PLAN_ACTIVATION=False` now blocks free activation of *paid* plans
only. The zero-price Starter plan stays activatable regardless — otherwise a
dealer who does not want to pay yet could never get past the gate.

---

## 4. How to verify

```bash
python test_saas_flow.py     # 34 passed, 0 failed
```

Then by hand:

1. Register a new dealer → you land on **Select Plan**, not the dashboard.
2. In another tab type `/dealer/dashboard` → bounced back to Select Plan.
3. Activate Growth → success popup → **dashboard**.
4. Log out, log back in → straight to the dashboard, no plan page.
5. `GET /portal/status` → JSON showing `has_active_plan: true`, plan, expiry.
6. In the database, set `dealer_subscriptions.expires_at` to a past date →
   next page load sends that dealer back to Select Plan.

---

## 5. Two things worth knowing

**KYC still runs after the plan.** Your existing `@kyc_required` decorator is
untouched, so a newly activated dealer goes
`plan → payment → /dealer/dashboard → /dealer/kyc` until the three documents
are approved. That is the pre-existing product rule, and it is *after* the
subscription step, so the reference flow is intact. To put KYC first instead,
swap the two decorators on `dealer.dashboard` and add `'dealer.kyc_upload'`
plus `'dealer.kyc_submit'` — already in `PLAN_EXEMPT_ENDPOINTS` — to whatever
order you prefer.

**Turning on real payments is now a small, contained change.** In
`activate_subscription()`, the `RAZORPAY_ENABLED` branch is where you create
the order; in your signature-verify route, call:

```python
activate_plan_for_dealer(
    dealer_id, plan_key, price=amount_inr,
    payment_method='Razorpay', payment_status='Paid',
    transaction_id=razorpay_payment_id, days=30,
)
```

The gate, the portal, the redirect and the templates need no further edits —
they only ever ask "is there an active row?", and that writer is what creates
one.

---

## 6. v29.1 — Subscription Plans page update

### 6.1 Plan Comparison removed

`templates/dealer/subscription.html`:

* the comparison card is gone — heading, `.table-wrap`, the whole `<table>`
  and every feature row (~176 lines);
* the `/* ── Comparison Table ── */` CSS block is gone too. Its rules
  (`.table-wrap`, `table`, `thead th`, `tbody td`, `.check-icon`, `.dash`)
  were verified to have no other user on the page, and the bare `table`
  element selector would otherwise have leaked styling into any future table;
* no layout gap: the card carried `mb-24`, so its bottom margin left with it.
  The plans grid now sits directly above the security footer.

Everything else on the page — the plan cards, prices, feature lists, the
demo-mode notice, the confirmation modal, the success popup, Sign Out and the
Caryanams header — is untouched.

### 6.2 Free plan goes straight to the Dashboard

The Starter (₹0) CTA reads **"Continue Free — Go to Dashboard"** and calls a
new `activateFreePlan()` instead of `openConfirmModal('starter')`. It skips
the confirmation modal and every payment field.

Order of operations matters, and it is deliberate:

1. `POST /dealer/subscription/activate {"plan":"starter","method":"free"}`
2. the server commits the `DealerSubscription` row
   (`payment_status='Free Trial'`, `price=0`, no transaction id) and syncs
   `User.subscription_*`
3. only on a confirmed `success: true` does the browser navigate to
   `response.redirect_url` (`/dealer/dashboard`)

So the plan gate always finds an active plan when it runs, and the state
survives a refresh, a logout and a different device because it lives in the
database rather than on the page. A failed request restores the button and
shows a toast — it never navigates on a plan that was not saved.

The paid plans are unchanged: modal, demo payment, transaction id,
"Use Free For Now" trial button, all exactly as before.

### 6.3 Bug fixed: `renderBanner()` overwrote the server-rendered state

This was a real defect in the previous patch. The v28 `renderBanner()` runs on
page load and rewrites the banner and all three plan buttons from
`state ? state.plan : 'starter'`. With no subscription row, `state` is null,
so it printed **"Starter · Free plan — no expiry · Active"** and re-disabled
the Starter button as "Current Plan" — restoring the exact behaviour the patch
set out to remove.

It was invisible to the Flask test client, which does not execute JavaScript,
and visible immediately in a browser. `renderBanner()` is now driven by
`HAS_ACTIVE_PLAN`, injected from the database on every page load.

To stop that class of bug recurring, `test_plan_page_dom.js` executes the
page's real JavaScript under jsdom and asserts the DOM the dealer actually
sees — 23 assertions across both states (no plan / active Growth plan).

### 6.4 New files

| File | Purpose |
|---|---|
| `apply_plan_page_update.py` | Idempotent patcher for §6.1–6.3 (`--check`, `--revert`) |
| `dump_plan_page.py` | Renders both page states to `no_plan.html` / `with_plan.html` |
| `test_plan_page_dom.js` | Executes the page JS in jsdom and asserts the real DOM |

### 6.5 Verification

```bash
python test_saas_flow.py                       # 60 passed, 0 failed
python dump_plan_page.py && node test_plan_page_dom.js   # 23 passed, 0 failed
```

The DOM test needs `npm install jsdom` once. It is a development aid — the
application itself still has zero new dependencies.

Covering your validation list, in order: Plan Comparison absent (§6.1, asserted
server-side and in the DOM); free plan saves before redirecting (§6.2); the
redirect lands on the existing `/dealer/dashboard` route; the dashboard renders
and a refresh keeps both the session and the plan; and the paid plans still
activate, charge and issue transaction ids, including an upgrade from free.

One thing to expect: the dashboard still enforces your pre-existing
`@kyc_required` rule, so a dealer whose three KYC documents are not yet
approved goes `free plan → /dealer/dashboard → /dealer/kyc`. That gate sits
*after* the subscription gate and is unrelated to it — the test suite approves
KYC first in order to assert that the dashboard itself returns HTTP 200 for a
free-plan dealer.
