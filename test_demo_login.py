"""
test_demo_login.py — tests for the temporary dealer demo login (auth/demo_login.py).

Run from the project root:

    pytest test_demo_login.py

Every test builds the app on a fresh SQLite file, so the sample dealer from
models.seed_demo_data() (dealer@caryanams.com) always exists and your real
database is never touched.
"""
from datetime import timedelta

import pytest

import utils.sms
from app import create_app
from auth.demo_login import LOCKOUT_MINUTES, MAX_FAILED_ATTEMPTS
from models import AdminLog, DealerKYC, User, db

DEMO_MOBILE = '9999999999'
DEMO_OTP = '011011'
DEMO_DEALER_EMAIL = 'dealer@caryanams.com'
PORTAL_AS_DEALER = '/portal?as=dealer'


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    """Build the app with the demo login on. Keyword args override env vars; None unsets one."""
    def build(**overrides):
        env = {
            'DATABASE_URL': f'sqlite:///{tmp_path / "demo_login.db"}',
            'SMS_PROVIDER': 'console',
            'DEMO_LOGIN_ENABLED': 'True',
            'DEMO_LOGIN_OTP': DEMO_OTP,
            'DEMO_DEALER_EMAIL': DEMO_DEALER_EMAIL,
            **overrides,
        }
        for name, value in env.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        app = create_app()
        app.config['TESTING'] = True
        return app
    return build


@pytest.fixture
def app(make_app):
    return make_app()


@pytest.fixture
def client(app):
    return app.test_client()


def demo_login(client, phone=DEMO_MOBILE, otp=DEMO_OTP, **kwargs):
    return client.post('/auth/demo-login', data={'phone': phone, 'otp': otp}, **kwargs)


def dealer_signed_in(client) -> bool:
    return client.get('/portal/status?as=dealer').get_json()['authenticated']


def test_correct_credentials_open_the_dealer_dashboard(app, client):
    # The sample dealer's KYC is still pending, which the existing KYC gate
    # answers with the KYC page. Approve it so the dashboard itself renders.
    with app.app_context():
        dealer = User.query.filter_by(email=DEMO_DEALER_EMAIL).one()
        kyc = DealerKYC.query.filter_by(dealer_id=dealer.id).one()
        kyc.aadhaar_front_status = kyc.aadhaar_back_status = kyc.pan_card_status = 'approved'
        db.session.commit()

    response = demo_login(client, follow_redirects=True)

    assert response.status_code == 200
    assert response.request.path == '/dealer/dashboard'


@pytest.mark.parametrize('phone', ['9999999999', '+91 99999 99999', '09999999999', '091-9999999999'])
def test_mobile_number_is_accepted_in_common_formats(client, phone):
    response = demo_login(client, phone=phone)

    assert response.headers['Location'].endswith(PORTAL_AS_DEALER)
    assert dealer_signed_in(client)


@pytest.mark.parametrize('phone, otp', [
    (DEMO_MOBILE, '123456'),
    (DEMO_MOBILE, '011010'),
    ('9876543210', DEMO_OTP),  # the sample dealer's real number is not the demo number
])
def test_wrong_mobile_number_or_otp_is_rejected(client, phone, otp):
    response = demo_login(client, phone, otp, follow_redirects=True)

    assert response.request.path == '/auth/login'
    assert 'Incorrect mobile number or OTP.' in response.get_data(as_text=True)
    assert not dealer_signed_in(client)


@pytest.mark.parametrize('phone, otp, message', [
    ('', DEMO_OTP, 'Please enter a valid 10-digit mobile number.'),
    ('99999', DEMO_OTP, 'Please enter a valid 10-digit mobile number.'),
    ('1999999999', DEMO_OTP, 'Please enter a valid 10-digit mobile number.'),
    (DEMO_MOBILE, '', 'Enter the 6-digit demo OTP.'),
    (DEMO_MOBILE, '0110', 'Enter the 6-digit demo OTP.'),
])
def test_invalid_form_input_shows_a_validation_message(client, phone, otp, message):
    response = demo_login(client, phone, otp, follow_redirects=True)

    assert message in response.get_data(as_text=True)
    assert not dealer_signed_in(client)


def test_too_many_wrong_otps_lock_the_demo_login(client):
    for _ in range(MAX_FAILED_ATTEMPTS):
        demo_login(client, otp='999999')

    response = demo_login(client, follow_redirects=True)  # correct OTP, but locked

    assert 'Too many failed demo login attempts.' in response.get_data(as_text=True)
    assert not dealer_signed_in(client)


def test_lockout_lifts_after_the_lockout_window(app, client):
    for _ in range(MAX_FAILED_ATTEMPTS):
        demo_login(client, otp='999999')
    with app.app_context():
        for row in AdminLog.query.filter_by(status='Failed'):
            row.created_at -= timedelta(minutes=LOCKOUT_MINUTES + 1)
        db.session.commit()

    assert demo_login(client).headers['Location'].endswith(PORTAL_AS_DEALER)


def test_incomplete_forms_do_not_count_toward_the_lockout(client):
    for _ in range(MAX_FAILED_ATTEMPTS + 1):
        demo_login(client, otp='')

    assert demo_login(client).headers['Location'].endswith(PORTAL_AS_DEALER)


def test_logout_ends_the_demo_session(client):
    demo_login(client)
    assert dealer_signed_in(client)

    client.get('/auth/logout?as=dealer')

    assert not dealer_signed_in(client)
    assert client.get('/dealer/dashboard').headers['Location'].endswith('/auth/login')


def test_demo_logins_are_labelled_in_the_activity_log(app, client):
    demo_login(client, otp='999999')
    demo_login(client)

    with app.app_context():
        entries = [(row.action, row.status) for row in AdminLog.query.order_by(AdminLog.id)]
    assert entries[-2:] == [('Failed demo login attempt', 'Failed'),
                            ('Dealer logged in via demo login', 'Success')]


def test_missing_demo_dealer_account_is_reported(make_app):
    client = make_app(DEMO_DEALER_EMAIL='nobody@example.com').test_client()

    response = demo_login(client, follow_redirects=True)

    assert 'The demo dealer account is not available.' in response.get_data(as_text=True)
    assert not dealer_signed_in(client)


def test_suspended_demo_dealer_cannot_sign_in(app, client):
    with app.app_context():
        User.query.filter_by(email=DEMO_DEALER_EMAIL).one().is_active = False
        db.session.commit()

    response = demo_login(client)

    # Turned away at the login step, not merely signed out later by the dashboard.
    assert response.headers['Location'].endswith('/auth/login?demo=1')
    page = client.get(response.headers['Location']).get_data(as_text=True)
    assert 'Your account has been suspended.' in page
    assert not dealer_signed_in(client)


@pytest.mark.parametrize('overrides', [
    {'DEMO_LOGIN_ENABLED': None},
    {'DEMO_LOGIN_ENABLED': 'False'},
    {'SMS_PROVIDER': 'msg91'},
    {'DEMO_LOGIN_OTP': None},
    {'DEMO_LOGIN_OTP': '01101'},
], ids=['flag unset', 'flag off', 'real SMS provider', 'no OTP', 'OTP not 6 digits'])
def test_demo_login_is_off_unless_fully_configured(make_app, overrides):
    client = make_app(**overrides).test_client()

    assert demo_login(client).status_code == 404
    assert 'demoLoginForm' not in client.get('/auth/login').get_data(as_text=True)


def test_login_page_keeps_sms_login_and_offers_demo_login(client):
    page = client.get('/auth/login').get_data(as_text=True)

    assert 'id="mainLoginForm"' in page
    assert 'id="demoLoginForm"' in page
    assert DEMO_MOBILE not in page
    assert DEMO_OTP not in page


def test_opening_the_demo_login_url_shows_the_login_page(client):
    response = client.get('/auth/demo-login', follow_redirects=True)

    assert response.request.path == '/auth/login'
    assert 'id="demoLoginForm"' in response.get_data(as_text=True)


def test_sms_login_still_works_with_demo_login_enabled(client, monkeypatch):
    sent_codes = []

    def capture_sms(phone, code):
        sent_codes.append(code)
        return True, None

    monkeypatch.setattr(utils.sms, 'send_otp_sms', capture_sms)

    client.post('/auth/login', data={'action': 'send', 'phone': '9876543210', 'policy_agreed': 'on'})
    response = client.post('/auth/login', data={'action': 'verify', 'code': sent_codes[-1]})

    assert response.headers['Location'].endswith(PORTAL_AS_DEALER)
    assert dealer_signed_in(client)


def test_sms_login_does_not_accept_the_demo_number(client):
    response = client.post('/auth/login',
                           data={'action': 'send', 'phone': DEMO_MOBILE, 'policy_agreed': 'on'})

    assert 'No account uses this mobile number.' in response.get_data(as_text=True)
    assert not dealer_signed_in(client)
