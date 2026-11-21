#!/usr/bin/env python3
"""
apply_saas_flow_patch.py — Caryanams DMS v28 → v29 SaaS onboarding flow
═══════════════════════════════════════════════════════════════════════
Rewires authentication + subscription routing to match the reference flow:

    Landing → Sign Up / Login → Authentication → SaaS Portal
        ├── no active plan → Select Plan → Payment/Activation → Dashboard
        └── active plan    → Dashboard
        → Features / Subscription / Account

Run it from the project root (the folder containing app.py):

    python apply_saas_flow_patch.py            # apply
    python apply_saas_flow_patch.py --check    # report only, change nothing
    python apply_saas_flow_patch.py --revert   # restore .bak_saasflow backups

Properties
──────────
• Idempotent — every edit is marker-guarded, so re-running is a no-op.
• Safe — each target file is copied to <file>.bak_saasflow before its first edit.
• Loud — an anchor that no longer matches is reported instead of silently skipped.

Prerequisites (ship these two files alongside this script):
    subscription_gate.py
    portal/__init__.py, portal/routes.py
"""

from __future__ import annotations

import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
BACKUP_SUFFIX = '.bak_saasflow'

CHECK_ONLY = '--check' in sys.argv
REVERT = '--revert' in sys.argv

_results: list[tuple[str, str, str]] = []          # (status, file, note)


# ─────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────

def _path(rel: str) -> str:
    return os.path.join(ROOT, rel)


def _read(rel: str) -> str | None:
    p = _path(rel)
    if not os.path.isfile(p):
        _results.append(('MISSING', rel, 'file not found — skipped'))
        return None
    with open(p, 'r', encoding='utf-8') as fh:
        return fh.read()


def _write(rel: str, text: str) -> None:
    p = _path(rel)
    bak = p + BACKUP_SUFFIX
    if not os.path.exists(bak):
        shutil.copy2(p, bak)
    with open(p, 'w', encoding='utf-8') as fh:
        fh.write(text)


def patch(rel: str, edits: list[dict]) -> None:
    """
    edits = [{'marker': str, 'old': str, 'new': str, 'mode': 'replace'|'after'|'before'}]

    marker : text whose presence means this edit is already applied
    mode   : replace old with new / insert new after old / insert new before old
    """
    src = _read(rel)
    if src is None:
        return

    out = src
    applied, skipped, failed = [], [], []

    for e in edits:
        name = e.get('name', e['marker'][:40])
        if e['marker'] in out:
            skipped.append(name)
            continue

        old = e['old']
        count = out.count(old)
        if count != 1:
            failed.append(f'{name} (anchor matched {count}x, expected 1)')
            continue

        mode = e.get('mode', 'replace')
        if mode == 'replace':
            out = out.replace(old, e['new'], 1)
        elif mode == 'after':
            out = out.replace(old, old + e['new'], 1)
        elif mode == 'before':
            out = out.replace(old, e['new'] + old, 1)
        applied.append(name)

    note_bits = []
    if applied:
        note_bits.append(f'{len(applied)} applied')
    if skipped:
        note_bits.append(f'{len(skipped)} already present')
    if failed:
        note_bits.append('FAILED: ' + '; '.join(failed))
    note = ', '.join(note_bits) or 'nothing to do'

    status = 'ERROR' if failed else ('OK' if applied else 'SKIP')
    _results.append((status, rel, note))

    if out != src and not CHECK_ONLY:
        _write(rel, out)


def patch_regex(rel: str, marker: str, pattern: str, replacement, name: str) -> None:
    """For blocks too long / whitespace-sensitive to match literally."""
    src = _read(rel)
    if src is None:
        return
    if marker in src:
        _results.append(('SKIP', rel, f'{name} already present'))
        return
    out, n = re.subn(pattern, replacement, src, count=1, flags=re.S)
    if n != 1:
        _results.append(('ERROR', rel, f'{name}: pattern matched {n}x, expected 1'))
        return
    _results.append(('OK', rel, f'{name} applied'))
    if not CHECK_ONLY:
        _write(rel, out)


def revert_all() -> None:
    n = 0
    for dirpath, _dirs, files in os.walk(ROOT):
        for f in files:
            if f.endswith(BACKUP_SUFFIX):
                bak = os.path.join(dirpath, f)
                orig = bak[: -len(BACKUP_SUFFIX)]
                shutil.copy2(bak, orig)
                os.remove(bak)
                n += 1
                print(f'  restored {os.path.relpath(orig, ROOT)}')
    print(f'\nReverted {n} file(s).')


# ═════════════════════════════════════════════════════════════════════════
# 1. models.py — the inverted NULL-expiry check
# ═════════════════════════════════════════════════════════════════════════

MODELS_EDITS = [{
    'name': 'User.has_active_subscription',
    'marker': 'v29 FIX: a NULL expiry',
    'old': """    def has_active_subscription(self):
        if not self.subscription_expiry:
            return True
        return self.subscription_expiry > _now_ist() and self.subscription_status == 'active'""",
    'new': """    def has_active_subscription(self):
        \"\"\"
        v29 FIX: a NULL expiry used to return True. A freshly registered
        dealer has exactly that (plan='starter', status='active',
        expiry=None), so every new signup looked fully subscribed and the
        plan-selection step was skipped.

        The authoritative answer now lives in subscription_gate.py, which
        reads the DealerSubscription table first and falls back to these
        legacy columns. This method delegates so old call sites stay valid.
        \"\"\"
        try:
            from subscription_gate import has_active_plan
            return has_active_plan(self)
        except Exception:
            if not self.subscription_expiry:
                return False
            return (self.subscription_expiry > _now_ist()
                    and (self.subscription_status or '').strip().lower() == 'active')""",
}]


# ═════════════════════════════════════════════════════════════════════════
# 2. app.py — register the portal, install the gate, expose it to templates
# ═════════════════════════════════════════════════════════════════════════

APP_EDITS = [
    {
        'name': 'subscription config flags',
        'marker': 'SUBSCRIPTION_GATE_ENABLED',
        'old': """    app.config['ALLOW_FREE_PLAN_ACTIVATION'] = os.environ.get('ALLOW_FREE_PLAN_ACTIVATION', 'True') == 'True'""",
        'new': """
    # ── v29 SaaS onboarding flow ───────────────────────────────────────────
    # Master switch for the plan gate. Leave True in production; set to
    # 'False' only to temporarily open the DMS without a subscription.
    app.config['SUBSCRIPTION_GATE_ENABLED'] = os.environ.get('SUBSCRIPTION_GATE_ENABLED', 'True') == 'True'
    # Validity granted by one activation, in days.
    app.config['SUBSCRIPTION_DEFAULT_DAYS'] = int(os.environ.get('SUBSCRIPTION_DEFAULT_DAYS', '30'))
    # Sign Up must land on the portal like Login does, so both paths behave
    # identically. Set to 'False' to force a manual login after registering.
    app.config['AUTO_LOGIN_AFTER_SIGNUP'] = os.environ.get('AUTO_LOGIN_AFTER_SIGNUP', 'True') == 'True'""",
        'mode': 'after',
    },
    {
        'name': 'import portal_bp',
        'marker': 'from portal.routes',
        'old': """    from policies.routes   import policies_bp         # ← Privacy & Refund Policy pages""",
        'new': """
    from portal.routes     import portal_bp           # ← v29 SaaS Portal (plan gate)""",
        'mode': 'after',
    },
    {
        'name': 'register portal_bp',
        'marker': "app.register_blueprint(portal_bp",
        'old': """    app.register_blueprint(policies_bp,    url_prefix='')""",
        'new': """
    app.register_blueprint(portal_bp,      url_prefix='')     # /portal — decides dashboard vs plan selection""",
        'mode': 'after',
    },
    {
        'name': 'install gate + template helpers',
        'marker': 'install_subscription_guard(app)',
        'old': """    @app.context_processor
    def inject_user():
        return dict(current_user=g.user)  # None when not logged in""",
        'new': """    @app.context_processor
    def inject_user():
        return dict(current_user=g.user)  # None when not logged in

    # ── v29: subscription firewall ─────────────────────────────────────────
    # Registered AFTER load_user so g.user is populated when it runs.
    # Blocks direct-URL access to /dealer/* and /studio/* for any dealer
    # without an active plan, so no route can be reached by typing its URL.
    from subscription_gate import install_subscription_guard
    install_subscription_guard(app)

    # ── v29: plan helpers for every template ───────────────────────────────
    #   {% if has_active_plan() %} … {% endif %}
    #   {{ subscription_state().plan }}
    #   {{ portal_url() }}   ← always-correct "go to my app" link
    @app.context_processor
    def inject_subscription_state():
        from subscription_gate import (
            has_active_plan, subscription_state, portal_target,
        )
        return dict(
            has_active_plan=has_active_plan,
            subscription_state=subscription_state,
            portal_url=portal_target,
        )""",
    },
]


# ═════════════════════════════════════════════════════════════════════════
# 3. auth/routes.py — Login AND Sign Up both end at the portal
# ═════════════════════════════════════════════════════════════════════════

AUTH_EDITS = [
    {
        'name': 'login → portal',
        'marker': "# v29: every authenticated path funnels through the SaaS Portal",
        'old': """            if user.role == 'dealer':
                return redirect(url_for('dealer.dashboard'))
            else:
                return redirect(url_for('user.home'))""",
        'new': """            # v29: every authenticated path funnels through the SaaS Portal,
            # which reads the database and decides dashboard vs plan selection.
            # Previously this jumped straight to dealer.dashboard, which is why
            # plan selection never appeared for new dealers.
            return redirect(url_for('portal.portal'))""",
    },
    {
        'name': 'signup → auto-login → portal',
        'marker': "AUTO_LOGIN_AFTER_SIGNUP",
        'old': """        flash('Registration successful! Please login.', 'success')
        return redirect(url_for('auth.login'))""",
        'new': """        # ── v29: Sign Up behaves exactly like Login ────────────────────────
        # Reference flow: Sign Up → Create Account → SaaS Portal.
        # Authenticate the new account here and hand it to /portal, which sends
        # a dealer to plan selection (no plan yet) or to the dashboard.
        from flask import current_app as _capp
        if _capp.config.get('AUTO_LOGIN_AFTER_SIGNUP', True) and user_id:
            session['user_id'] = user_id
            session['user_role'] = role
            flash('Account created successfully!', 'success')
            return redirect(url_for('portal.portal'))

        flash('Registration successful! Please login.', 'success')
        return redirect(url_for('auth.login'))""",
    },
    {
        'name': 'dealer-link login → portal',
        'marker': "# v29: dealer-link login also goes through the portal",
        'old': """            if user.role == 'dealer':
                return redirect(url_for('dealer.dashboard'))
            return redirect(url_for('minisite.home', website_name=website_name))""",
        'new': """            # v29: dealer-link login also goes through the portal so the
            # plan check cannot be side-stepped via a dealer's shared link.
            if user.role == 'dealer':
                return redirect(url_for('portal.portal'))
            return redirect(url_for('minisite.home', website_name=website_name))""",
    },
]


# ═════════════════════════════════════════════════════════════════════════
# 4. dealer/routes.py — gate the dashboard, fix activation
# ═════════════════════════════════════════════════════════════════════════

DEALER_EDITS = [
    {
        'name': 'import subscription_gate',
        'marker': 'from subscription_gate import',
        'old': """from subscription_features import feature_required""",
        'new': """
from subscription_gate import (
    subscription_required, subscription_state, activate_plan_for_dealer,
    get_active_subscription, get_latest_subscription,
)""",
        'mode': 'after',
    },
    {
        'name': 'gate the dashboard',
        'marker': """@dealer_bp.route('/dashboard')
@dealer_required
@subscription_required""",
        'old': """@dealer_bp.route('/dashboard')
@dealer_required
@kyc_required
def dashboard():""",
        'new': """@dealer_bp.route('/dashboard')
@dealer_required
@subscription_required   # v29: no plan → /dealer/subscription?onboarding=1
@kyc_required
def dashboard():""",
    },
]

# The subscription page + activation endpoint are replaced wholesale — the old
# bodies contain the two showstoppers (g.user.name on a dict, and starter being
# rejected), so a surgical edit would be less clear than a clean rewrite.

SUBSCRIPTION_ROUTE_NEW = '''@dealer_bp.route('/subscription')
@dealer_required
def subscription():
    """
    Select Plan page — step 4 of the reference flow.

    Reached automatically by the portal when a dealer has no active plan
    (`?onboarding=1`), and manually from the sidebar for upgrades/renewals.
    All state is read from the database on every request, so it is correct
    on a new device, in a new browser and after any logout.
    """
    dealer_id = get_dealer_id()

    state       = subscription_state()
    current_sub = state['subscription'] or get_active_subscription(dealer_id)
    last_sub    = get_latest_subscription(dealer_id)

    # Onboarding mode = "this dealer cannot leave until a plan is active".
    onboarding = (request.args.get('onboarding') == '1') or (not state['has_plan'])

    return render_template(
        'dealer/subscription.html',
        plans=PLANS,
        dealer=g.user,
        current_sub=current_sub,
        last_sub=last_sub,
        sub_state=state,
        onboarding=onboarding,
        has_active_plan=state['has_plan'],
        current_plan_key=(state['plan'] or ''),
        razorpay_enabled=current_app.config.get('RAZORPAY_ENABLED', False),
        allow_free_plan=current_app.config.get('ALLOW_FREE_PLAN_ACTIVATION', True),
    )


@dealer_bp.route('/subscription/status')
@dealer_required
def subscription_status():
    """JSON snapshot of the caller's plan — used by the plan page and for debugging."""
    state = subscription_state()
    expires = state['expires_at']
    return jsonify({
        'success': True,
        'has_active_plan': state['has_plan'],
        'plan': state['plan'],
        'payment_status': state['status'],
        'source': state['source'],
        'expires_at': expires.isoformat() if expires else None,
        'days_left': state['days_left'],
        'dashboard_url': url_for('dealer.dashboard'),
    })


# ── Plan Activation (AJAX) — demo payment or free activation ──────────────
# Flipping RAZORPAY_ENABLED=True later only requires creating the order here
# and calling activate_plan_for_dealer() from the signature-verify route:
# the activation writer, the gate and the redirect all stay exactly as they are.
@dealer_bp.route('/subscription/activate', methods=['POST'])
@dealer_required
def activate_subscription():
    data     = request.get_json(silent=True) or {}
    plan_key = (data.get('plan') or '').strip().lower()
    method   = (data.get('method') or 'demo').strip().lower()      # 'demo' | 'free'

    plan = next((p for p in PLANS if p['key'] == plan_key), None)
    if not plan:
        return jsonify({'success': False, 'error': 'Invalid plan selected.'}), 400

    dealer_id = get_dealer_id()

    # v29 FIX: g.user is a dict (see db.user_get_by_id), so the old
    # `g.user.name` raised AttributeError → 500 → the browser fell back to a
    # localStorage-only "activation" that never reached the database. That is
    # why a plan vanished after logging out and back in.
    dealer_name = (g.user.get('name', '') if isinstance(g.user, dict)
                   else getattr(g.user, 'name', '')) or ''

    if current_app.config.get('RAZORPAY_ENABLED', False):
        # ── Live Razorpay: create an order and let the browser check out.
        # Verify the signature in a separate route, then call
        # activate_plan_for_dealer(...) with payment_status='Paid'.
        return jsonify({
            'success': False,
            'error': 'Online payments are not enabled yet. Please contact support.',
        }), 400

    # A zero-price plan is free by definition — it must always be activatable,
    # otherwise a new dealer has no way to finish onboarding. v28 rejected
    # 'starter' outright, which left new accounts with no subscription row at all.
    if int(plan['price_inr']) == 0:
        method = 'free'

    if method == 'free':
        if int(plan['price_inr']) > 0 and not current_app.config.get('ALLOW_FREE_PLAN_ACTIVATION', True):
            return jsonify({
                'success': False,
                'error': 'Free activation of paid plans is currently disabled by admin.',
            }), 403
        amount, payment_method = 0, 'Free'
        payment_status, transaction_id = 'Free Trial', None
        success_msg = f'{plan["name"]} plan activated. Welcome to Caryanams DMS!'
    else:
        amount, payment_method = int(plan['price_inr']), 'Demo'
        # v29: written as 'Paid' (was 'Pending'), so "did this dealer pay?"
        # has an unambiguous answer. Legacy 'Pending' demo rows still work.
        payment_status = 'Paid'
        transaction_id = _generate_demo_transaction_id()
        success_msg = 'Payment successful. Your subscription is now active.'

    sub = activate_plan_for_dealer(
        dealer_id, plan['key'],
        price=amount,
        payment_method=payment_method,
        payment_status=payment_status,
        transaction_id=transaction_id,
        days=int(current_app.config.get('SUBSCRIPTION_DEFAULT_DAYS', 30)),
        dealer_name=dealer_name,
        notes=f'{dealer_name} activated {plan["name"]} plan via {payment_method} flow.',
    )

    log_dealer_action(
        f'Activated subscription plan: {plan["name"]} via {payment_method}'
        + (f' (Txn: {transaction_id})' if transaction_id else ''),
        'Subscription'
    )

    return jsonify({
        'success':        True,
        'plan':           plan['key'],
        'plan_name':      plan['name'],
        'payment_method': payment_method,
        'payment_status': payment_status,
        'amount':         amount,
        'transaction_id': transaction_id,
        'expires_at':     sub.expires_at.isoformat() if sub.expires_at else None,
        # The dashboard is unlocked the moment the row above is committed.
        'redirect_url':   url_for('dealer.dashboard'),
        'message':        success_msg,
    })

# ========== MY ACCOUNT =========='''


# ═════════════════════════════════════════════════════════════════════════
# 5. templates/dealer/subscription.html — server truth, no browser bypass
# ═════════════════════════════════════════════════════════════════════════

TEMPLATE_EDITS = [
    {
        'name': 'header button (no plan → Sign Out, not a dead dashboard link)',
        'marker': 'v29-header-btn',
        'old': """  <a href="{{ url_for('dealer.dashboard') }}" id="backToDmsBtn" class="back-btn">
    <i class="fas fa-arrow-left"></i> Back to DMS
  </a>""",
        'new': """  {# v29-header-btn: during onboarding the dashboard is locked, so offer Sign Out instead of a link that bounces straight back here. #}
  <a href="{{ url_for('dealer.dashboard') if has_active_plan else url_for('auth.logout') }}" id="backToDmsBtn" class="back-btn">
    {% if has_active_plan %}<i class="fas fa-arrow-left"></i> Back to DMS
    {% else %}<i class="fas fa-sign-out-alt"></i> Sign Out{% endif %}
  </a>""",
    },
    {
        'name': 'onboarding notice',
        'marker': 'v29-onboarding-notice',
        'old': """  <!-- Current Plan Banner -->""",
        'new': """  {# v29-onboarding-notice #}
  {% if not has_active_plan %}
  <div class="card mb-24" style="border:2px solid #0f3d7a;background:#eef4ff">
    <div class="card-body" style="padding:18px 22px;display:flex;gap:12px;align-items:flex-start">
      <i class="fas fa-hand-point-down" style="color:#0f3d7a;font-size:18px;margin-top:2px"></i>
      <div>
        <strong style="color:#072654;font-size:14px;display:block;margin-bottom:6px">
          {% if sub_state and sub_state.ever_subscribed %}Your subscription has ended{% else %}One last step — choose your plan{% endif %}
        </strong>
        <p style="color:#1e3a8a;font-size:13px;margin:0">
          Your Caryanams DMS dashboard unlocks the moment a plan is activated.
          {% if sub_state and sub_state.ever_subscribed %}Renew below to pick up exactly where you left off.{% endif %}
        </p>
      </div>
    </div>
  </div>
  {% endif %}

  <!-- Current Plan Banner -->""",
    },
    {
        'name': 'banner reflects real state',
        'marker': 'v29-banner',
        'old': """      <div class="current-plan-name" id="bannerPlanName">Starter</div>
      <div class="current-plan-expiry" id="bannerExpiry">Free plan — no expiry</div>""",
        'new': """      {# v29-banner: rendered from the database, not from localStorage #}
      <div class="current-plan-name" id="bannerPlanName">{% if has_active_plan %}{{ (current_plan_key or 'starter') | title }}{% else %}No active plan{% endif %}</div>
      <div class="current-plan-expiry" id="bannerExpiry">{% if has_active_plan %}{% if sub_state and sub_state.expires_at %}Valid until {{ sub_state.expires_at.strftime('%d %b %Y') }}{% else %}Active{% endif %}{% else %}Choose a plan below to unlock your dashboard{% endif %}</div>""",
    },
    {
        'name': 'status badge reflects real state',
        'marker': 'v29-status-badge',
        'old': """      <span class="badge-success" id="bannerStatus"><i class="fas fa-check-circle"></i> Active</span>""",
        'new': """      {# v29-status-badge #}
      <span class="{{ 'badge-success' if has_active_plan else 'badge-pending' }}" id="bannerStatus">{% if has_active_plan %}<i class="fas fa-check-circle"></i> Active{% else %}<i class="fas fa-clock"></i> Not Active{% endif %}</span>""",
    },
    {
        'name': 'starter is selectable when nothing is active',
        'marker': 'v29-starter-btn',
        'old': """        <button class="btn btn-success btn-full" id="btnStarter" disabled>
          <i class="fas fa-check"></i> Current Plan
        </button>""",
        'new': """        {# v29-starter-btn: the free plan is a real choice, not a dead button #}
        {% if has_active_plan and current_plan_key == 'starter' %}
        <button class="btn btn-success btn-full" id="btnStarter" disabled>
          <i class="fas fa-check"></i> Current Plan
        </button>
        {% else %}
        <button class="btn btn-primary btn-full" id="btnStarter" onclick="openConfirmModal('starter')">
          <i class="fas fa-play"></i> Start Free — Starter
        </button>
        {% endif %}""",
    },
    {
        'name': 'JS config from server',
        'marker': 'HAS_ACTIVE_PLAN',
        'old': """  var USE_BACKEND   = true;
  var ACTIVATE_URL  = "{{ url_for('dealer.activate_subscription') }}";
  var BACK_TO_DMS_URL = "{{ url_for('dealer.dashboard') }}";""",
        'new': """  var USE_BACKEND   = true;
  var ACTIVATE_URL  = "{{ url_for('dealer.activate_subscription') }}";
  var DASHBOARD_URL = "{{ url_for('dealer.dashboard') }}";

  /* v29: injected fresh from the database on every page load — the browser is
     no longer a source of truth about who has paid. */
  var HAS_ACTIVE_PLAN = {{ 'true' if has_active_plan else 'false' }};
  var IS_ONBOARDING   = {{ 'true' if onboarding else 'false' }};
  var BACK_TO_DMS_URL = HAS_ACTIVE_PLAN ? DASHBOARD_URL : "{{ url_for('auth.logout') }}";""",
    },
    {
        'name': 'drop localStorage persistence',
        'marker': 'v29: subscription state is NEVER persisted in the browser',
        'old': """  var STORAGE_KEY = 'caryanams_demo_subscription';
  if (SERVER_SUB) {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(SERVER_SUB)); } catch (e) {}
  }""",
        'new': """  /* v29: subscription state is NEVER persisted in the browser. The old
     localStorage cache leaked one dealer's plan to the next person on a shared
     machine, and let a plan look "active" with no database row behind it.
     Any key left over from v28 is purged here. */
  try { localStorage.removeItem('caryanams_demo_subscription'); } catch (e) {}
  var _memState = SERVER_SUB;""",
    },
    {
        'name': 'in-memory state accessors',
        'marker': 'function loadState() { return _memState; }',
        'old': """  function loadState() {
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return null;
      return JSON.parse(raw);
    } catch (e) {
      return null; // corrupted/old data — never let this crash the page
    }
  }

  function saveState(state) {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); }
    catch (e) { /* storage unavailable — ignore, demo still works in-memory */ }
  }""",
        'new': """  function loadState() { return _memState; }
  function saveState(state) { _memState = state; }""",
    },
    {
        'name': 'success → mark active + redirect to dashboard',
        'marker': 'v29-activated-redirect',
        'old': """      if (success) {
        closeConfirmModal();
        renderBanner();""",
        'new': """      if (success) {
        /* v29-activated-redirect */
        closeConfirmModal();
        HAS_ACTIVE_PLAN = true;
        renderBanner();""",
    },
    {
        'name': 'popup continues into the dashboard',
        'marker': 'redirect: _redirectTo',
        'old': """        showSuccessPopup(
          '🎉 Plan Activated',
          'Subscription Active',
          buildSpRow('Plan Name', (info && info.planName) || plan.name) +
          buildSpRow('Amount', amountDisplay) +
          buildSpRow('Validity', '30 Days') +
          buildSpRow('Activation Date', activationDate)
        );""",
        'new': """        /* Payment/Activation → User Dashboard: the final hop of the flow. */
        var _redirectTo = (info && info.redirectUrl) || DASHBOARD_URL;
        showSuccessPopup(
          '🎉 Plan Activated',
          'Subscription Active',
          buildSpRow('Plan Name', (info && info.planName) || plan.name) +
          buildSpRow('Amount', amountDisplay) +
          buildSpRow('Validity', '30 Days') +
          buildSpRow('Activation Date', activationDate),
          { redirect: _redirectTo }
        );
        /* Safety net in case the popup is dismissed without the button. */
        setTimeout(function() {
          if (window.location.href.indexOf(_redirectTo) === -1) {
            window.location.href = _redirectTo;
          }
        }, 9000);""",
    },
    {
        'name': 'pass redirect_url through from the server',
        'marker': 'redirectUrl: res.data.redirect_url',
        'old': """          finish(true, res.data.message || 'Subscription activated.', {
            planName: res.data.plan_name,
            amount: res.data.amount
          });""",
        'new': """          finish(true, res.data.message || 'Subscription activated.', {
            planName: res.data.plan_name,
            amount: res.data.amount,
            redirectUrl: res.data.redirect_url
          });""",
    },
    {
        'name': 'no client-side activation fallback',
        'marker': 'v29: no client-side fallback',
        'old': """      .catch(function() {
        // Backend unreachable (e.g. Razorpay keys/server not ready yet) —
        // gracefully fall back to the local demo flow instead of failing.
        doLocalActivation();
      });
    } else {
      doLocalActivation();
    }""",
        'new': """      .catch(function() {
        /* v29: no client-side fallback. A plan exists in the database or it
           does not exist at all — otherwise the dashboard gate would be asked
           to honour an activation the server never recorded. */
        finish(false, 'Could not reach the server. Please check your connection and try again.');
      });
    } else {
      finish(false, 'Activation is unavailable right now. Please try again shortly.');
    }""",
    },
]

# doLocalActivation wrote a fake subscription with no server round-trip.
DO_LOCAL_PATTERN = r"    var doLocalActivation = function\(\) \{.*?\n    \};\n"
DO_LOCAL_REPLACEMENT = (
    "    /* v29: the local (offline) activation path has been removed. It wrote a\n"
    "       browser-only subscription that the server knew nothing about, which\n"
    "       both bypassed payment and disappeared on the next login. */\n"
)


# ═════════════════════════════════════════════════════════════════════════
# 6. templates/user/base.html — landing-page links honour the flow
# ═════════════════════════════════════════════════════════════════════════

USER_BASE_EDITS = [
    {
        'name': 'nav DMS button → portal',
        'marker': """{{ url_for('portal.portal') }}" class="btn-nav-dealer\"""",
        'old': """      <a href="{{ url_for('dealer.dashboard') }}" class="btn-nav-dealer"><i class="fas fa-tachometer-alt"></i> DMS Dashboard</a>""",
        'new': """      {# v29: route through the portal so the plan check always runs #}
      <a href="{{ url_for('portal.portal') }}" class="btn-nav-dealer"><i class="fas fa-tachometer-alt"></i> DMS Dashboard</a>""",
    },
    {
        'name': 'footer Dashboard link → portal',
        'marker': """<li><a href="{{ url_for('portal.portal') }}">Dashboard</a></li>""",
        'old': """        <li><a href="{{ url_for('dealer.dashboard') }}">Dashboard</a></li>""",
        'new': """        <li><a href="{{ url_for('portal.portal') }}">Dashboard</a></li>""",
    },
]


# ═════════════════════════════════════════════════════════════════════════
# main
# ═════════════════════════════════════════════════════════════════════════

def main() -> int:
    print('Caryanams v28 → v29 — SaaS onboarding flow patch')
    print('=' * 66)

    if REVERT:
        revert_all()
        return 0

    for rel in ('subscription_gate.py', 'portal/routes.py'):
        if not os.path.isfile(_path(rel)):
            print(f'\n  ERROR: {rel} is missing. Copy it into the project first.\n')
            return 2

    patch('models.py', MODELS_EDITS)
    patch('app.py', APP_EDITS)
    patch('auth/routes.py', AUTH_EDITS)
    patch('dealer/routes.py', DEALER_EDITS)

    # dealer/routes.py — replace the subscription + activation block wholesale
    patch_regex(
        'dealer/routes.py',
        marker="@dealer_bp.route('/subscription/status')",
        pattern=r"@dealer_bp\.route\('/subscription'\)\n@dealer_required\ndef subscription\(\):.*?# ========== MY ACCOUNT ==========",
        replacement=lambda _m: SUBSCRIPTION_ROUTE_NEW,   # callable → no escape headaches
        name='subscription + activate_subscription rewrite',
    )

    patch('templates/dealer/subscription.html', TEMPLATE_EDITS)
    patch_regex(
        'templates/dealer/subscription.html',
        marker='the local (offline) activation path has been removed',
        pattern=DO_LOCAL_PATTERN,
        replacement=lambda _m: DO_LOCAL_REPLACEMENT,
        name='remove doLocalActivation',
    )

    patch('templates/user/base.html', USER_BASE_EDITS)

    print()
    width = max(len(r[1]) for r in _results) if _results else 20
    for status, rel, note in _results:
        print(f'  [{status:<7}] {rel:<{width}}  {note}')

    errors = [r for r in _results if r[0] == 'ERROR']
    print()
    if CHECK_ONLY:
        print('--check: no files were modified.')
    elif errors:
        print(f'{len(errors)} edit group(s) failed — apply those by hand '
              f'(see SAAS_FLOW_CHANGES.md) and re-run.')
    else:
        print('Done. Backups saved as *.bak_saasflow — `--revert` restores them.')
        print('\nNext:  python app.py    then open  /portal')
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
