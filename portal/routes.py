"""
portal/routes.py — Caryanams SaaS Portal (the decision point)
═════════════════════════════════════════════════════════════
This blueprint is the "SaaS Portal" box in the reference flow diagram.
Nothing renders a dashboard here and nothing renders plans here — this is
purely the switchboard that every authenticated entry point funnels into:

    Landing → Sign Up / Login → Authentication → **/portal** → …
        ├─ dealer, no active plan → /dealer/subscription?onboarding=1
        │                            → Payment / Activation → /dealer/dashboard
        └─ dealer, active plan    → /dealer/dashboard

Login and Sign Up both end at `/portal`, so the two paths can never drift
apart again. Because the plan check happens server-side on every visit,
the answer is always current — expiry, admin revocation and renewal are
all picked up on the next page load.

Routes
──────
    GET  /portal              decide + redirect (302)
    GET  /portal/select-plan  explicit shortcut to plan selection
    GET  /portal/status       JSON snapshot (debugging / frontend polling)
"""

from flask import Blueprint, g, jsonify, redirect, session, url_for

from subscription_gate import (
    portal_target, subscription_state, user_id_of, user_role_of,
)

portal_bp = Blueprint('portal', __name__)


@portal_bp.route('/portal')
def portal():
    """
    SaaS Portal entry point — checks the subscription, then redirects.

    Kept as a redirect rather than an interstitial page so the existing
    design and branding are untouched and the user never sees a flash of
    a screen they don't need.
    """
    if not g.user:
        return redirect(url_for('auth.login'))
    return redirect(portal_target())


@portal_bp.route('/portal/select-plan')
def select_plan():
    """
    Direct link to plan selection. A dealer who already has a plan is sent
    to the normal (non-onboarding) subscription page so they can still
    upgrade or renew — that path must never be blocked.
    """
    if not g.user:
        return redirect(url_for('auth.login'))

    state = subscription_state()
    if not state['gated']:
        return redirect(url_for('user.home'))
    if state['has_plan']:
        return redirect(url_for('dealer.subscription'))
    return redirect(url_for('dealer.subscription', onboarding=1))


@portal_bp.route('/portal/status')
def status():
    """JSON view of the gate's decision — handy when debugging redirects."""
    state = subscription_state()
    if not state['user_id']:
        return jsonify({'authenticated': False, 'next': url_for('auth.login')})

    expires = state['expires_at']
    return jsonify({
        'authenticated': True,
        'user_id': user_id_of(),
        'role': user_role_of(),
        'requires_plan': state['gated'],
        'has_active_plan': state['has_plan'],
        'plan': state['plan'],
        'payment_status': state['status'],
        'source': state['source'],
        'expires_at': expires.isoformat() if expires else None,
        'days_left': state['days_left'],
        'ever_subscribed': state['ever_subscribed'],
        'next': portal_target(),
    })
