"""
subscription_gate.py — Caryanams DMS (v28 → v29 SaaS onboarding flow)
═════════════════════════════════════════════════════════════════════
Single source of truth for one question:

    "Does this logged-in dealer own an ACTIVE, ACTIVATED plan right now?"

Everything in the app that needs to route, gate or display subscription
state must ask THIS module — never `User.subscription_expiry` directly,
never `localStorage`, never the session.

Target flow (reference diagram)
───────────────────────────────
    Landing Page → Sign Up / Login → Authentication
        → SaaS Portal  (/portal  ← the decision point)
            ├── no active plan  → Select Plan → Payment/Activation → Dashboard
            └── active plan     → Dashboard
        → Features / Subscription / Account

Why the old build sent everyone straight to the dashboard
─────────────────────────────────────────────────────────
1. `User.has_active_subscription()` returned **True** when
   `subscription_expiry IS NULL` — and a freshly registered dealer has
   exactly that (default `subscription_plan='starter'`,
   `subscription_status='active'`, `subscription_expiry=None`). So every
   brand-new account looked fully subscribed.
2. `auth.login` redirected `dealer` straight to `dealer.dashboard`.
3. No route in `/dealer/*` or `/studio/*` ever checked for a plan, so
   typing the dashboard URL always worked.

This module fixes #1 (authoritative check below), and `portal/routes.py`
plus the auth patch fix #2 and #3.

Public API
──────────
    subscription_state(user=None) -> dict      # full, cache-free snapshot
    has_active_plan(user=None)    -> bool
    get_active_subscription(id)   -> DealerSubscription | None
    portal_target(user=None)      -> url string ("where should this user go?")
    subscription_required(view)   -> decorator (explicit, per-route)
    install_subscription_guard(app)            # app-wide direct-URL firewall
    activate_plan_for_dealer(...)              # shared activation writer
"""

from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (
    current_app, flash, g, jsonify, redirect, request, session, url_for
)

_IST = timezone(timedelta(hours=5, minutes=30))


def _now_ist():
    """Naive IST datetime — matches how every timestamp in this project is stored."""
    return datetime.now(_IST).replace(tzinfo=None)


# ─────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────

#: Roles that must own a plan before they may use the SaaS product.
#: Buyers ('user') browse the marketplace for free; 'admin' is never gated.
GATED_ROLES = {'dealer'}

#: Blueprints that live behind the plan gate.
#: 'dealer' = the DMS itself, 'background' = Caryanams Studio (/studio/*).
GATED_BLUEPRINTS = {'dealer', 'background'}

#: Endpoints that must stay reachable WITHOUT a plan — otherwise a new
#: dealer could never reach the page where a plan is bought.
PLAN_EXEMPT_ENDPOINTS = {
    'static',
    # the portal / decision point
    'portal.portal', 'portal.status', 'portal.select_plan',
    # plan selection + activation + status polling
    'dealer.subscription', 'dealer.activate_subscription',
    'dealer.subscription_status', 'dealer.verify_payment',
    # account & KYC must stay usable so onboarding can be completed
    'dealer.my_account', 'dealer.kyc_upload', 'dealer.kyc_submit',
    # harmless polling call fired by the shared dealer layout
    'dealer.mark_notifications_read',
    # auth
    'auth.login', 'auth.logout', 'auth.register', 'auth.role_select',
    'auth.forgot_password', 'auth.forgot_password_verify',
    'auth.forgot_password_reset',
}

#: `DealerSubscription.payment_status` values that mean "access granted".
_ACTIVE_PAYMENT_STATUSES = {
    'paid', 'active', 'free trial', 'free', 'success', 'captured', 'completed',
}

#: v28 wrote demo activations with payment_status='Pending' even though they
#: DID grant access. Honour those legacy rows so no existing dealer loses a
#: plan when this module is installed. New activations write 'Paid'/'Free Trial'.
_LEGACY_ACCESS_METHODS = {'demo', 'free'}

#: Plans a dealer may hold. 'starter' is free but still needs an explicit
#: activation row — that is what makes "did the user choose a plan?" answerable.
KNOWN_PLANS = ('starter', 'growth', 'pro')


# ─────────────────────────────────────────────────────────────────────────
# g.user helpers — g.user is a **dict** (see db.user_get_by_id), but some
# older call sites treat it as an ORM object. These read both safely.
# ─────────────────────────────────────────────────────────────────────────

def _field(user, key, default=None):
    if user is None:
        return default
    if isinstance(user, dict):
        return user.get(key, default)
    return getattr(user, key, default)


def _current_user():
    return getattr(g, 'user', None)


def user_id_of(user=None):
    user = user if user is not None else _current_user()
    return _field(user, 'id')


def user_role_of(user=None):
    user = user if user is not None else _current_user()
    return (_field(user, 'role') or '').strip().lower()


# ─────────────────────────────────────────────────────────────────────────
# Authoritative subscription lookup
# ─────────────────────────────────────────────────────────────────────────

def _row_grants_access(sub, now=None):
    """True if this DealerSubscription row currently unlocks the product."""
    if sub is None or not sub.is_active:
        return False
    now = now or _now_ist()
    if sub.expires_at and sub.expires_at <= now:
        return False
    status = (sub.payment_status or '').strip().lower()
    if status in _ACTIVE_PAYMENT_STATUSES:
        return True
    # legacy demo/free rows written as 'Pending'
    method = (sub.payment_method or '').strip().lower()
    return method in _LEGACY_ACCESS_METHODS


def get_active_subscription(dealer_id):
    """
    Newest DealerSubscription row for this dealer that currently grants
    access, or None. This is the authoritative record.
    """
    if not dealer_id:
        return None
    try:
        from models import DealerSubscription
    except Exception:
        return None

    now = _now_ist()
    rows = (DealerSubscription.query
            .filter(DealerSubscription.dealer_id == dealer_id)
            .order_by(DealerSubscription.activated_at.desc().nullslast(),
                      DealerSubscription.id.desc())
            .limit(10)
            .all())
    for sub in rows:
        if _row_grants_access(sub, now):
            return sub
    return None


def get_latest_subscription(dealer_id):
    """Most recent row regardless of state — for 'expired on …' messaging."""
    if not dealer_id:
        return None
    try:
        from models import DealerSubscription
    except Exception:
        return None
    return (DealerSubscription.query
            .filter(DealerSubscription.dealer_id == dealer_id)
            .order_by(DealerSubscription.activated_at.desc().nullslast(),
                      DealerSubscription.id.desc())
            .first())


def _legacy_user_plan(dealer_id):
    """
    Fallback for accounts provisioned before DealerSubscription existed
    (including the demo dealer created by models.seed_demo_data, and any
    plan an admin granted by editing the User row directly).

    THE FIX: a NULL expiry is **not** an active plan any more. That single
    inverted condition is what let every new signup skip plan selection.
    """
    if not dealer_id:
        return None
    try:
        from models import User
    except Exception:
        return None
    user = User.query.get(dealer_id)
    if not user:
        return None
    if (user.subscription_status or '').strip().lower() != 'active':
        return None
    if not user.subscription_expiry:          # ← was treated as "active forever"
        return None
    if user.subscription_expiry <= _now_ist():
        return None
    return {
        'plan': (user.subscription_plan or 'starter').strip().lower(),
        'expires_at': user.subscription_expiry,
    }


def subscription_state(user=None):
    """
    Cache-free snapshot of the current user's subscription.

    Returns:
        {
          'user_id':      int | None,
          'role':         str,
          'gated':        bool,   # does this role need a plan at all?
          'has_plan':     bool,   # ← the decision bit
          'plan':         str | None,
          'status':       str | None,
          'source':       'subscription' | 'legacy_user_fields' | None,
          'expires_at':   datetime | None,
          'days_left':    int | None,
          'subscription': DealerSubscription | None,
          'ever_subscribed': bool,
        }

    Nothing here is cached in the session or in localStorage — logging out
    and back in can therefore never reset or fake a subscription.
    """
    user = user if user is not None else _current_user()
    uid = user_id_of(user)
    role = user_role_of(user)

    state = {
        'user_id': uid, 'role': role,
        'gated': role in GATED_ROLES,
        'has_plan': False, 'plan': None, 'status': None, 'source': None,
        'expires_at': None, 'days_left': None, 'subscription': None,
        'ever_subscribed': False,
    }

    if not uid:
        return state

    # Non-gated roles (buyer / admin) are always "allowed through".
    if role not in GATED_ROLES:
        state['has_plan'] = True
        return state

    sub = get_active_subscription(uid)
    if sub is not None:
        state.update({
            'has_plan': True,
            'plan': (sub.plan_name or 'starter').strip().lower(),
            'status': sub.payment_status,
            'source': 'subscription',
            'expires_at': sub.expires_at,
            'subscription': sub,
            'ever_subscribed': True,
        })
    else:
        legacy = _legacy_user_plan(uid)
        if legacy:
            state.update({
                'has_plan': True,
                'plan': legacy['plan'],
                'status': 'active',
                'source': 'legacy_user_fields',
                'expires_at': legacy['expires_at'],
                'ever_subscribed': True,
            })
        else:
            state['ever_subscribed'] = get_latest_subscription(uid) is not None

    if state['expires_at']:
        state['days_left'] = max(0, (state['expires_at'] - _now_ist()).days)

    return state


def has_active_plan(user=None):
    """Bool shortcut over subscription_state() — safe in templates."""
    return bool(subscription_state(user)['has_plan'])


def current_plan_key(user=None):
    """Plan key for feature gating; falls back to 'starter'."""
    st = subscription_state(user)
    return st['plan'] or 'starter'


# ─────────────────────────────────────────────────────────────────────────
# Routing decision — used by /portal and by auth after login/signup
# ─────────────────────────────────────────────────────────────────────────

def portal_target(user=None):
    """
    THE routing brain. Given the logged-in user, return the URL they belong on.

        not logged in            → /auth/login
        admin / sub-admin        → /admin/dashboard
        buyer ('user')           → marketplace home
        dealer without a plan    → /dealer/subscription?onboarding=1
        dealer with a plan       → /dealer/dashboard
    """
    user = user if user is not None else _current_user()
    if not user_id_of(user):
        return url_for('auth.login')

    role = user_role_of(user)

    if role in ('admin', 'sub_admin', 'subadmin'):
        try:
            return url_for('admin.dashboard')
        except Exception:
            return url_for('user.home')

    if role not in GATED_ROLES:
        return url_for('user.home')

    if has_active_plan(user):
        return url_for('dealer.dashboard')

    return url_for('dealer.subscription', onboarding=1)


# ─────────────────────────────────────────────────────────────────────────
# Enforcement
# ─────────────────────────────────────────────────────────────────────────

def _wants_json():
    if request.is_json:
        return True
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return True
    if '/api/' in (request.path or ''):
        return True
    best = request.accept_mimetypes.best_match(['application/json', 'text/html'])
    return (best == 'application/json'
            and request.accept_mimetypes[best] >= request.accept_mimetypes['text/html'])


def _no_plan_response(state):
    """Uniform 'you have no plan yet' answer for HTML and for JSON/AJAX."""
    target = url_for('dealer.subscription', onboarding=1)
    message = (
        'Your subscription has expired. Please renew a plan to continue.'
        if state.get('ever_subscribed') else
        'Please choose a plan to activate your Caryanams DMS account.'
    )
    if _wants_json():
        return jsonify({
            'success': False,
            'error': 'SUBSCRIPTION_REQUIRED',
            'message': message,
            'redirect': target,
        }), 402   # Payment Required
    flash(message, 'warning')
    return redirect(target)


def subscription_required(f):
    """
    Per-route plan gate. Stack it under dealer_required:

        @dealer_bp.route('/dashboard')
        @dealer_required
        @subscription_required
        @kyc_required
        def dashboard(): ...
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        state = subscription_state()
        if not state['user_id']:
            return redirect(url_for('auth.login'))
        if not state['gated'] or state['has_plan']:
            return f(*args, **kwargs)
        return _no_plan_response(state)
    return decorated


def install_subscription_guard(app):
    """
    App-wide firewall: blocks direct-URL access to every gated blueprint for
    dealers who have no active plan, without touching 100+ route definitions.

    MUST be called after the `load_user` before_request that sets `g.user`,
    because Flask runs app-level hooks in registration order.
    """

    @app.before_request
    def _enforce_plan_gate():          # noqa: D401  (Flask hook)
        endpoint = request.endpoint
        if not endpoint or endpoint in PLAN_EXEMPT_ENDPOINTS:
            return None
        if request.blueprint not in GATED_BLUEPRINTS:
            return None
        if app.config.get('SUBSCRIPTION_GATE_ENABLED', True) is False:
            return None

        user = _current_user()
        if not user_id_of(user):
            return None                # auth decorators own the "not logged in" case
        if user_role_of(user) not in GATED_ROLES:
            return None

        state = subscription_state(user)
        if state['has_plan']:
            return None
        return _no_plan_response(state)

    return app


# ─────────────────────────────────────────────────────────────────────────
# Activation writer — shared by the demo flow and by real Razorpay verify
# ─────────────────────────────────────────────────────────────────────────

def activate_plan_for_dealer(dealer_id, plan, *, price=0, payment_method='Demo',
                             payment_status='Paid', transaction_id=None,
                             days=30, notes=None, dealer_name=''):
    """
    Persist an activation atomically and return the new DealerSubscription.

    - retires any previously active row for this dealer
    - writes the DealerSubscription + DealerPayment audit rows
    - syncs the legacy User.subscription_* columns (feature gating reads them)
    - commits once

    Because the row is in the database, the plan survives logout, a new
    browser, a different device and a server restart.
    """
    from models import db, DealerSubscription, DealerPayment

    plan_key = (plan or '').strip().lower()
    now = _now_ist()
    expires = now + timedelta(days=int(days)) if days else None

    (DealerSubscription.query
     .filter_by(dealer_id=dealer_id, is_active=True)
     .update({'is_active': False}, synchronize_session=False))

    sub = DealerSubscription(
        dealer_id=dealer_id,
        plan_name=plan_key,
        price=int(price or 0),
        payment_method=payment_method,
        payment_status=payment_status,
        transaction_id=transaction_id,
        activated_at=now,
        expires_at=expires,
        is_active=True,
    )
    db.session.add(sub)
    db.session.flush()                 # need sub.id for the payment row

    db.session.add(DealerPayment(
        dealer_id=dealer_id,
        subscription_id=sub.id,
        amount=int(price or 0),
        payment_method=payment_method,
        payment_status=payment_status,
        transaction_id=transaction_id,
        notes=notes or f'{dealer_name or "Dealer"} activated {plan_key} via {payment_method}.',
    ))

    # keep legacy columns in sync — subscription_features.py reads them
    try:
        from db import user_update_subscription
        user_update_subscription(dealer_id, plan_key, expires)
    except Exception:
        from models import User
        u = User.query.get(dealer_id)
        if u:
            u.subscription_plan = plan_key
            u.subscription_expiry = expires
            u.subscription_status = 'active'

    db.session.commit()
    return sub
