#!/usr/bin/env python3
"""
test_saas_flow.py — end-to-end proof that the SaaS onboarding flow works.

Run from the project root:

    python test_saas_flow.py

Uses Flask's test client against a throwaway SQLite database, so it touches
neither your PostgreSQL data nor any real payment gateway.

Asserted behaviour
──────────────────
 1. Sign Up  → /portal → /dealer/subscription?onboarding=1   (NOT the dashboard)
 2. Plan page returns 200 and renders all three plans
 3. /dealer/dashboard typed directly → redirected to plan selection
 4. /dealer/inventory, /studio/... typed directly → same
 5. Activation writes a real DealerSubscription row + returns redirect_url
 6. /portal now → /dealer/dashboard
 7. Logout + Login again → /portal → /dealer/dashboard  (plan survives)
 8. Expiring the subscription → /portal → plan selection again
 9. A buyer ('user' role) is never gated
10. The demo dealer seeded with legacy User.subscription_* fields is not gated
"""

import os
import sys
import tempfile

os.environ.setdefault('SUBSCRIPTION_GATE_ENABLED', 'True')
os.environ.setdefault('ALLOW_FREE_PLAN_ACTIVATION', 'True')

_tmpdb = os.path.join(tempfile.mkdtemp(), 'flowtest.db')
os.environ['DATABASE_URL'] = 'sqlite:///' + _tmpdb

from app import create_app                      # noqa: E402
from models import db, User, DealerSubscription  # noqa: E402
from subscription_gate import _now_ist           # noqa: E402

PASS, FAIL = [], []


def check(label, condition, detail=''):
    (PASS if condition else FAIL).append(label)
    mark = '  ok  ' if condition else ' FAIL '
    print(f'[{mark}] {label}' + (f'\n           → {detail}' if detail and not condition else ''))


def loc(resp):
    return resp.headers.get('Location', '')


def main():
    app = create_app()
    app.config['WTF_CSRF_ENABLED'] = False

    # Dealers and users sign in with an SMS code. Capture the codes instead
    # of sending SMS, and drive sign-up / login the way a person would.
    import utils.sms as _sms
    _codes = []
    _sms.send_otp_sms = lambda phone, code: (_codes.append(code) or (True, None))

    def otp_signup(client, data):
        client.post('/auth/register', data=dict(data, action='send'))
        return client.post('/auth/register', data={'action': 'verify', 'code': _codes[-1]},
                           follow_redirects=False)

    def otp_login(client, phone):
        client.post('/auth/login', data={'action': 'send', 'phone': phone, 'policy_agreed': 'on'})
        return client.post('/auth/login', data={'action': 'verify', 'code': _codes[-1]},
                           follow_redirects=False)

    c = app.test_client()
    email = 'flowtest.dealer@example.com'

    with app.app_context():
        old = User.query.filter_by(email=email).first()
        if old:
            DealerSubscription.query.filter_by(dealer_id=old.id).delete()
            db.session.delete(old)
            db.session.commit()

    print('\n── 1. Sign Up ────────────────────────────────────────────────')
    r = otp_signup(c, {
        'name': 'Flow Test Motors', 'email': email, 'phone': '9876543299',
        'role': 'dealer', 'city': 'Pune',
        'company_name': 'Flow Test Motors',
    })
    check('Sign Up redirects to the SaaS Portal', '/portal' in loc(r), f'got {loc(r)!r}')

    r = c.get('/portal', follow_redirects=False)
    check('Portal sends a new dealer to Select Plan (not the dashboard)',
          '/dealer/subscription' in loc(r) and 'onboarding=1' in loc(r), f'got {loc(r)!r}')

    print('\n── 2. Select Plan page ───────────────────────────────────────')
    r = c.get('/dealer/subscription?onboarding=1')
    body = r.get_data(as_text=True)
    check('Plan page renders (HTTP 200)', r.status_code == 200, f'status {r.status_code}')
    check('Starter plan is displayed', 'Starter' in body)
    check('Growth plan is displayed', 'Growth' in body and '999' in body)
    check('Pro plan is displayed', 'Pro' in body and '1,999' in body)
    # Free plan is one-click now — it must NOT go through the payment modal.
    check('Free plan is selectable during onboarding', 'activateFreePlan()' in body)
    check('Page knows there is no active plan', 'HAS_ACTIVE_PLAN = false' in body)
    check('No localStorage write of subscription state',
          "localStorage.setItem(STORAGE_KEY" not in body)

    print('\n── 3. Direct-URL access is blocked ───────────────────────────')
    for path in ('/dealer/dashboard', '/dealer/inventory', '/dealer/leads',
                 '/dealer/finance', '/studio/'):
        r = c.get(path, follow_redirects=False)
        blocked = r.status_code in (301, 302, 308) and 'subscription' in loc(r)
        check(f'{path} blocked without a plan', blocked,
              f'status {r.status_code}, location {loc(r)!r}')

    r = c.get('/dealer/dashboard', headers={'X-Requested-With': 'XMLHttpRequest'})
    check('AJAX call gets 402 + redirect target instead of HTML',
          r.status_code == 402 and r.get_json().get('error') == 'SUBSCRIPTION_REQUIRED',
          f'status {r.status_code}')

    print('\n── 4. Payment / Activation ───────────────────────────────────')
    r = c.post('/dealer/subscription/activate', json={'plan': 'growth', 'method': 'demo'})
    j = r.get_json() or {}
    check('Activation succeeds', r.status_code == 200 and j.get('success') is True, str(j)[:200])
    check('Server returns the dashboard as the next hop',
          j.get('redirect_url', '').endswith('/dealer/dashboard'), str(j.get('redirect_url')))
    check('Payment recorded as Paid', j.get('payment_status') == 'Paid', str(j.get('payment_status')))
    check('Transaction id issued', bool(j.get('transaction_id')), str(j.get('transaction_id')))

    with app.app_context():
        u = User.query.filter_by(email=email).first()
        sub = DealerSubscription.query.filter_by(dealer_id=u.id, is_active=True).first()
        check('DealerSubscription row exists in the database', sub is not None)
        check('Row stores the chosen plan', sub is not None and sub.plan_name == 'growth')
        check('Legacy User.subscription_* columns kept in sync',
              u.subscription_plan == 'growth' and u.subscription_expiry is not None)
        dealer_id = u.id

    print('\n── 5. Dashboard is now reachable ─────────────────────────────')
    r = c.get('/portal', follow_redirects=False)
    check('Portal now sends the dealer to the dashboard',
          '/dealer/dashboard' in loc(r), f'got {loc(r)!r}')
    r = c.get('/dealer/dashboard', follow_redirects=False)
    check('Dashboard no longer bounces to plan selection',
          'subscription' not in loc(r), f'got {loc(r)!r}')
    r = c.get('/dealer/subscription')
    check('Subscription page still reachable for upgrades',
          r.status_code == 200 and 'HAS_ACTIVE_PLAN = true' in r.get_data(as_text=True))

    print('\n── 6. Logout / Login must not reset the plan ─────────────────')
    c.get('/auth/logout')
    r = c.get('/dealer/dashboard', follow_redirects=False)
    check('Logged-out user cannot reach the dashboard',
          '/auth/login' in loc(r), f'got {loc(r)!r}')

    r = otp_login(c, '9876543299')
    check('Login redirects to the SaaS Portal', '/portal' in loc(r), f'got {loc(r)!r}')
    r = c.get('/portal', follow_redirects=False)
    check('Returning subscriber goes straight to the dashboard',
          '/dealer/dashboard' in loc(r), f'got {loc(r)!r}')

    r = c.get('/portal/status')
    j = r.get_json() or {}
    check('Status endpoint reports the active plan',
          j.get('has_active_plan') is True and j.get('plan') == 'growth', str(j)[:200])

    print('\n── 7. Expiry re-locks the dashboard ──────────────────────────')
    with app.app_context():
        sub = DealerSubscription.query.filter_by(dealer_id=dealer_id, is_active=True).first()
        sub.expires_at = _now_ist().replace(year=_now_ist().year - 1)
        u = User.query.get(dealer_id)
        u.subscription_expiry = sub.expires_at
        db.session.commit()

    r = c.get('/portal', follow_redirects=False)
    check('Expired plan sends the dealer back to Select Plan',
          '/dealer/subscription' in loc(r), f'got {loc(r)!r}')
    r = c.get('/dealer/dashboard', follow_redirects=False)
    check('Expired plan blocks the dashboard', 'subscription' in loc(r), f'got {loc(r)!r}')

    print('\n── 8. Buyers are never gated ─────────────────────────────────')
    c2 = app.test_client()
    buyer = 'flowtest.buyer@example.com'
    with app.app_context():
        b = User.query.filter_by(email=buyer).first()
        if b:
            db.session.delete(b)
            db.session.commit()
    r = otp_signup(c2, {
        'name': 'Flow Buyer', 'email': buyer, 'phone': '9876500098',
        'role': 'user', 'city': 'Pune',
    })
    check('Buyer sign-up also lands on the portal', '/portal' in loc(r), f'got {loc(r)!r}')
    r = c2.get('/portal', follow_redirects=False)
    check('Buyer is sent to the marketplace, never to plan selection',
          '/dealer/subscription' not in loc(r), f'got {loc(r)!r}')

    print('\n── 9. Legacy accounts keep their access ──────────────────────')
    with app.app_context():
        legacy = User.query.filter_by(role='dealer').filter(
            User.email != email).first()
        if legacy and legacy.subscription_expiry and legacy.subscription_expiry > _now_ist():
            from subscription_gate import has_active_plan
            check('Dealer with only legacy User.subscription_* fields is not gated',
                  has_active_plan(legacy), f'{legacy.email} expiry={legacy.subscription_expiry}')
        else:
            print('[  --  ] no legacy dealer in this database — skipped')

    print('\n── 10. Free plan: one click to the dashboard ─────────────────')
    c3 = app.test_client()
    free_email = 'flowtest.free@example.com'
    otp_signup(c3, {
        'name': 'Free Plan Motors', 'email': free_email, 'phone': '9876511199',
        'role': 'dealer', 'city': 'Pune',
        'company_name': 'Free Plan Motors',
    })

    page = c3.get('/dealer/subscription?onboarding=1').get_data(as_text=True)
    check('Plan Comparison section is gone', 'Plan Comparison' not in page)
    check('No comparison table markup remains', '<table' not in page)
    check('No orphaned comparison CSS remains', 'table-wrap' not in page)
    check('Security footer still closes the page', 'security-footer' in page)
    check('Free plan CTA calls activateFreePlan()', 'activateFreePlan()' in page)
    check('Free plan CTA advertises the dashboard',
          'Continue Free — Go to Dashboard' in page)
    check('Free plan does not open the payment modal',
          "openConfirmModal('starter')" not in page)
    check('Paid plans keep the payment modal',
          "openConfirmModal('growth')" in page and "openConfirmModal('pro')" in page)
    check('Paid plan pricing untouched', '999' in page and '1,999' in page)

    r = c3.post('/dealer/subscription/activate', json={'plan': 'starter', 'method': 'free'})
    j = r.get_json() or {}
    check('Free plan activates', r.status_code == 200 and j.get('success') is True, str(j)[:200])
    check('Free plan records no charge', j.get('amount') == 0, str(j.get('amount')))
    check('Free plan needs no payment reference',
          j.get('transaction_id') is None and j.get('payment_method') == 'Free', str(j)[:160])
    check('Free plan hands back the dashboard URL',
          j.get('redirect_url', '').endswith('/dealer/dashboard'), str(j.get('redirect_url')))

    with app.app_context():
        fu = User.query.filter_by(email=free_email).first()
        fsub = DealerSubscription.query.filter_by(dealer_id=fu.id, is_active=True).first()
        check('Free plan saved to the database',
              fsub is not None and fsub.plan_name == 'starter')
        check('Free plan marked as a free activation',
              fsub is not None and fsub.payment_status == 'Free Trial' and fsub.price == 0)
        check('Free plan synced to User.subscription_*',
              fu.subscription_plan == 'starter' and fu.subscription_expiry is not None)
        free_id = fu.id

        # Approve KYC so the dashboard itself can render — the KYC gate is a
        # separate, pre-existing rule that sits after the subscription gate.
        from models import DealerKYC
        kyc = DealerKYC(
            dealer_id=free_id,
            aadhaar_front='x.jpg', aadhaar_back='y.jpg', pan_card='z.jpg',
            aadhaar_front_status='approved', aadhaar_back_status='approved',
            pan_card_status='approved', kyc_status='approved',
        )
        db.session.add(kyc)
        db.session.commit()

    r = c3.get('/portal', follow_redirects=False)
    check('Portal sends the free-plan dealer to the dashboard',
          '/dealer/dashboard' in loc(r), f'got {loc(r)!r}')
    r = c3.get('/dealer/dashboard')
    check('Dashboard renders for a free-plan dealer', r.status_code == 200,
          f'status {r.status_code} location {loc(r)!r}')

    print('\n── 11. Refresh + re-login keep the free plan ─────────────────')
    r = c3.get('/dealer/dashboard')
    check('Refreshing the dashboard stays authenticated', r.status_code == 200)
    j = c3.get('/portal/status').get_json() or {}
    check('Status still reports the free plan',
          j.get('has_active_plan') is True and j.get('plan') == 'starter', str(j)[:200])
    c3.get('/auth/logout')
    otp_login(c3, '9876511199')
    r = c3.get('/portal', follow_redirects=False)
    check('Free plan survives logout and login',
          '/dealer/dashboard' in loc(r), f'got {loc(r)!r}')

    print('\n── 12. Paid plans still work from a free plan ────────────────')
    r = c3.post('/dealer/subscription/activate', json={'plan': 'pro', 'method': 'demo'})
    j = r.get_json() or {}
    check('Upgrading free → Pro works',
          r.status_code == 200 and j.get('success') and j.get('plan') == 'pro', str(j)[:200])
    check('Paid upgrade charges the plan price', j.get('amount') == 1999, str(j.get('amount')))
    check('Paid upgrade issues a transaction id', bool(j.get('transaction_id')))
    with app.app_context():
        rows = DealerSubscription.query.filter_by(dealer_id=free_id, is_active=True).all()
        check('Exactly one active subscription after the upgrade', len(rows) == 1,
              f'{len(rows)} active rows')
        check('Active row is the Pro plan', rows and rows[0].plan_name == 'pro')

    print('\n' + '=' * 62)
    print(f'  {len(PASS)} passed, {len(FAIL)} failed')
    if FAIL:
        for f in FAIL:
            print('   ✗ ' + f)
    print('=' * 62)
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
