# Advanced Implementation Prompt — Caryanams SaaS Onboarding Flow

Paste everything between the rules into Claude Code, Cursor, or any coding
agent with the repository open. It is written to be executed, not
interpreted: every path, symbol and acceptance test below was verified
against the real `caryanams_v28` codebase.

---

## ROLE

You are a senior Flask engineer doing a surgical refactor on a live
multi-tenant SaaS product (`caryanams_v28`, Flask + SQLAlchemy +
PostgreSQL, blueprints: `auth`, `dealer`, `user`, `admin`, `minisite`,
`background`, `policies`). Production dealers depend on this app. You change
routing, authentication state and subscription checking — and nothing else.
No redesign, no renaming, no dependency additions, no reformatting of files
you touch.

## OBJECTIVE

Make the authenticated navigation flow match this exactly:

```
Landing Page (/)
  → Sign Up / Login  (/auth/register · /auth/login)
      → Authentication
          → SaaS Portal  (/portal)          ← single decision point
              ├─ dealer WITHOUT an active plan
              │     → Select Plan (/dealer/subscription?onboarding=1)
              │         → Payment / Activation
              │             → User Dashboard (/dealer/dashboard)
              └─ dealer WITH an active plan
                    → User Dashboard (/dealer/dashboard)
                        → Features · Subscription · Account
```

Today both Sign Up and Login jump straight to `/dealer/dashboard` and the
plan page is never shown. Fix the cause, not the symptom.

## ROOT CAUSES — CONFIRM EACH, THEN FIX EACH

Do not start writing code until you have read these four sites and
confirmed the diagnosis in your own words.

1. **`models.py` → `User.has_active_subscription()`**

   ```python
   if not self.subscription_expiry:
       return True
   ```

   New accounts are created with `subscription_plan='starter'`,
   `subscription_status='active'`, `subscription_expiry=None`, so this
   returns `True` for every fresh signup. This is the root cause: the app
   genuinely believes new dealers are subscribed. Invert it — a NULL expiry
   means **no plan** — and move the authoritative answer into one module.

2. **`auth/routes.py` → `login()`** ends with
   `redirect(url_for('dealer.dashboard'))`, with no subscription check
   between authentication and the dashboard. `register()` does not log the
   user in at all, so Sign Up and Login behave differently.
   `dealer_login()` has the same direct jump.

3. **`dealer/routes.py` → `dashboard()`** carries only
   `@dealer_required @kyc_required`. There is no subscription gate on it or
   on any other `/dealer/*` or `/studio/*` route, so typing a URL always
   works.

4. **`dealer/routes.py` → `activate_subscription()`** contains
   `dealer_name = g.user.name if g.user else ''`, but `g.user` is a **dict**
   (built by `db.user_get_by_id`). `.name` raises `AttributeError` → the
   global 500 handler returns JSON → and in
   `templates/dealer/subscription.html` the `fetch().catch()` falls back to
   `doLocalActivation()`, which writes the subscription to **localStorage
   only**. That single bug explains three reported symptoms: the plan
   disappears after logout/login, it differs per browser, and it can be
   faked from devtools. In the same function, `plan_key == 'starter'` is
   rejected as invalid, so the free plan can never be activated and a new
   dealer has no path out of onboarding.

## WHAT TO BUILD

### A. `subscription_gate.py` (new, project root) — one source of truth

Nothing else in the codebase may answer "is this dealer subscribed?".

* `get_active_subscription(dealer_id)` — newest `DealerSubscription` row
  that currently grants access, else `None`. A row grants access when
  `is_active` is true, `expires_at` is null or in the future, and
  `payment_status` is one of `paid / active / free trial / free / success /
  captured / completed`. **Compatibility requirement:** v28 wrote demo
  activations with `payment_status='Pending'` even though they did grant
  access — keep honouring rows whose `payment_method` is `Demo` or `Free`
  so no existing dealer loses their plan on deploy.
* `_legacy_user_plan(dealer_id)` — fallback to `User.subscription_*` for
  accounts provisioned before `dealer_subscriptions` existed, including the
  dealer created by `models.seed_demo_data()` (plan `growth`, expiry +365d,
  **no** subscription row) and any plan an admin granted by editing the user
  row. Requires `status == 'active'` **and** a non-null future expiry.
* `subscription_state(user=None)` → dict with `has_plan`, `plan`, `status`,
  `source`, `expires_at`, `days_left`, `subscription`, `gated`,
  `ever_subscribed`. Read fresh from the database on every call — never
  cached in `session`, `g`, or the browser, because a cached "yes" is how
  logout/login resets and fakes happen.
* `has_active_plan(user=None)` → bool.
* `portal_target(user=None)` → the URL this user belongs on:
  not logged in → login; `admin` → admin dashboard; `user` (buyer) →
  marketplace home; dealer with a plan → dashboard; dealer without →
  `url_for('dealer.subscription', onboarding=1)`.
* `@subscription_required` — per-route gate.
* `install_subscription_guard(app)` — one `app.before_request` that blocks
  every gated blueprint at once. Gate by **blueprint name**
  (`{'dealer', 'background'}`), not by URL prefix: `/dealer/<website_name>`
  strings appear in `auth/routes.py` return-URL checks and a prefix rule
  invites collisions with public minisite pages. Exempt, at minimum:
  `static`, the portal endpoints, `dealer.subscription`,
  `dealer.activate_subscription`, `dealer.subscription_status`,
  `dealer.my_account`, `dealer.kyc_upload`, `dealer.kyc_submit`,
  `dealer.mark_notifications_read`, and the `auth.*` endpoints — otherwise a
  dealer with no plan can never reach the page that sells them one.
* `activate_plan_for_dealer(dealer_id, plan, *, price, payment_method, payment_status, transaction_id, days, notes, dealer_name)`
  — the only writer. Retires previously active rows, inserts
  `DealerSubscription` + `DealerPayment`, syncs the legacy
  `User.subscription_*` columns (`subscription_features.py` still reads
  them), commits once, returns the row.

`g.user` is a dict in most call sites and an ORM object in a few, so every
accessor in this module must read both shapes.

### B. `portal/` (new blueprint) — the SaaS Portal

`GET /portal` → `redirect(portal_target())`. A 302, not a rendered page, so
the existing design and branding are untouched and nobody sees a screen they
do not need. Also add `GET /portal/select-plan` (explicit shortcut; a dealer
who already has a plan goes to the normal subscription page so upgrades are
never blocked) and `GET /portal/status` (JSON snapshot — invaluable when
debugging a redirect loop). Register in `app.py` with `url_prefix=''`.

### C. Route rewiring

* `auth.login` success → `redirect(url_for('portal.portal'))` for **all**
  roles. Preserve the existing `minisite_return_url` behaviour ahead of it.
* `auth.register` success → set `session['user_id']` and
  `session['user_role']`, then `redirect(url_for('portal.portal'))`, behind
  a new `AUTO_LOGIN_AFTER_SIGNUP` config flag (default True). Keep the
  existing rule that minisite registrations must log in manually.
* `auth.dealer_login` success (dealer) → `portal.portal`.
* `dealer.dashboard` → add `@subscription_required` between
  `@dealer_required` and `@kyc_required`.
* `app.py` → register `portal_bp`; call `install_subscription_guard(app)`
  **after** the `load_user` `before_request` (Flask runs app-level hooks in
  registration order, so an earlier call would see `g.user` unset); add a
  context processor exposing `has_active_plan`, `subscription_state` and
  `portal_url` to every template; add `SUBSCRIPTION_GATE_ENABLED`,
  `SUBSCRIPTION_DEFAULT_DAYS`, `AUTO_LOGIN_AFTER_SIGNUP`.
* `templates/user/base.html` → the two "DMS Dashboard" / "Dashboard" links
  point at `portal.portal`, so even the landing page honours the gate.

### D. Plan selection and activation

`dealer.subscription` — keep the existing `PLANS` list in
`dealer/routes.py` as the single plan definition (Starter ₹0 / Growth ₹999 /
Pro ₹1999, with the feature lists already there; do not invent plans or
prices). Pass `has_active_plan`, `current_plan_key`, `sub_state`,
`current_sub`, `last_sub` and `onboarding` to the template, where
`onboarding = request.args.get('onboarding') == '1' or not state['has_plan']`.

`dealer.activate_subscription` — rewrite the body:

* read `dealer_name` dict-safely;
* force `method='free'` for any zero-price plan and **accept `starter`** —
  free is a legitimate choice and currently the only way a non-paying dealer
  can complete onboarding;
* keep honouring `ALLOW_FREE_PLAN_ACTIVATION` for free activation of *paid*
  plans;
* write demo payments as `payment_status='Paid'` (not `'Pending'`) so
  "has this dealer paid?" has one unambiguous answer;
* delegate the writes to `activate_plan_for_dealer(...)`;
* return `redirect_url = url_for('dealer.dashboard')` in the JSON.

Add `GET /dealer/subscription/status` returning the same snapshot as
`/portal/status`, scoped to the caller.

Leave the `RAZORPAY_ENABLED` branch as the documented seam: creating the
order there and calling `activate_plan_for_dealer(..., payment_method='Razorpay', payment_status='Paid')`
from the signature-verify route must be the *only* work needed to go live.

### E. `templates/dealer/subscription.html`

Keep the existing design, CSS, copy and plan cards. Change only what is
factually wrong:

* delete the `localStorage.setItem(...)` seeding of `SERVER_SUB` and make
  `loadState`/`saveState` in-memory; purge the old
  `caryanams_demo_subscription` key on load (it leaks one dealer's plan to
  the next person on a shared machine);
* delete `doLocalActivation()` and its `.catch()` fallback — on a network
  failure show an error, never a fake activation;
* inject `HAS_ACTIVE_PLAN` and `IS_ONBOARDING` from the server and drive the
  banner, the status badge and the plan buttons from them, so a dealer with
  no plan sees "No active plan / Not Active" rather than "Starter / Active";
* make Starter selectable whenever nothing is active (it is currently a
  permanently disabled "Current Plan" button);
* during onboarding, replace "Back to DMS" with "Sign Out" — the dashboard
  is locked, so that link would bounce straight back;
* on success, pass `{ redirect: response.redirect_url }` to the existing
  `showSuccessPopup(...)` (it already supports a `redirect` option — see
  `templates/_success_popup.html`) and add a timeout fallback so a dismissed
  popup still lands on the dashboard.

## CONSTRAINTS

* Do not change the visual design, branding, CSS, plan names, prices or
  feature lists.
* Do not touch `subscription_features.py` (`PLAN_FEATURES`,
  `@feature_required`) — plan *tiering* already works; this task is about
  plan *existence*. The two gates compose: no plan → portal gate;
  wrong tier → existing feature gate.
* Do not break the public marketplace (`user`), the dealer minisites
  (`minisite`, `/caryanams/<dealer>/<site>`), or the admin panel. Buyers
  (`role='user'`) and admins must never be asked for a plan.
* Do not regress existing dealers: anyone with a real
  `DealerSubscription` row, a legacy `User.subscription_*` grant, or a v28
  `Pending`+`Demo` row keeps working on first deploy.
* Never cache subscription state in the session or the browser.
* Every edit to an existing file must be marker-guarded and idempotent, and
  back the file up before the first write.

## DELIVERABLES

1. `subscription_gate.py`, `portal/__init__.py`, `portal/routes.py`.
2. Edits to `models.py`, `app.py`, `auth/routes.py`, `dealer/routes.py`,
   `templates/dealer/subscription.html`, `templates/user/base.html`.
3. `apply_saas_flow_patch.py` — re-runnable patcher with `--check` and
   `--revert`, which fails loudly if an anchor no longer matches instead of
   silently skipping.
4. `test_saas_flow.py` — the acceptance tests below, runnable against a
   throwaway SQLite database (`DATABASE_URL=sqlite:///…`) so it never
   touches production data or a payment gateway.
5. `SAAS_FLOW_CHANGES.md` — root-cause notes, file-by-file diff summary, new
   config flags, manual test script, and the exact steps to enable Razorpay.

## ACCEPTANCE TESTS — all must pass

1. `POST /auth/register` (role `dealer`) → 302 `/portal`.
2. `GET /portal` for that dealer → 302 `/dealer/subscription?onboarding=1`.
3. `GET /dealer/subscription?onboarding=1` → 200 and the HTML contains
   Starter, Growth ₹999, Pro ₹1,999.
4. `GET /dealer/dashboard`, `/dealer/inventory`, `/dealer/leads`,
   `/dealer/finance`, `/studio/` while unsubscribed → each redirects to the
   subscription page.
5. The same request with `X-Requested-With: XMLHttpRequest` → HTTP 402 and
   `{"error": "SUBSCRIPTION_REQUIRED", "redirect": …}` — never an HTML page
   into a `fetch()`.
6. `POST /dealer/subscription/activate {"plan":"growth","method":"demo"}` →
   `success: true`, `payment_status: "Paid"`, a transaction id, and
   `redirect_url` ending `/dealer/dashboard`.
7. After activation a `DealerSubscription` row exists with
   `plan_name='growth'`, `is_active=true`, a future `expires_at`, and
   `User.subscription_plan` / `subscription_expiry` are in sync.
8. `GET /portal` → 302 `/dealer/dashboard`; `GET /dealer/dashboard` no
   longer redirects to the subscription page.
9. `GET /dealer/subscription` still returns 200 for an active subscriber
   (upgrades must never be blocked).
10. Logout → `GET /dealer/dashboard` redirects to `/auth/login`; log in
    again → 302 `/portal` → 302 `/dealer/dashboard`. The plan survives.
11. Set `expires_at` to a past date → the next `GET /portal` returns to plan
    selection and the dashboard is blocked again.
12. `POST /auth/register` (role `user`) → `/portal` → marketplace home,
    never plan selection.
13. `POST …/activate {"plan":"starter"}` → succeeds as
    `payment_status='Free Trial'`, `amount=0`, and unlocks the dashboard.
14. A dealer holding only legacy `User.subscription_*` fields (the seeded
    demo dealer) is not gated.
15. The rendered plan page contains no `localStorage.setItem` of
    subscription state and no `doLocalActivation`.

## PROCESS

Work in this order and report after each step: **inspect and confirm the
four root causes → write `subscription_gate.py` → write the portal →
rewire the routes → patch the template → write the tests → run them.**
Show the test output. If any acceptance test fails, fix the code — never
weaken the test. Finish with a summary of which of the four defects each
change addresses, and flag anything you found that is out of scope
(for example: `dealer/routes.py` calls
`url_for('minisite.home', website_name=…)` while that endpoint also
requires `dealer_name` — a pre-existing bug on the dealer-link login path,
untouched here).
