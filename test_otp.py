"""
test_otp.py — SMS one-time codes for dealer/user login and sign-up.

Run from the project root:

    pytest test_otp.py

Each test builds the app on a fresh SQLite file with SMS captured in memory,
so no SMS is sent and your real database is never touched.
"""
from datetime import timedelta

import pytest
import requests

import utils.sms
from app import _load_secret_key, create_app
from models import LoginOTP, User, db
from utils import otp

DEALER_PHONE = '9876543210'   # sample dealer from models.seed_demo_data()
NEW_PHONE = '9811122233'
PORTAL_AS_DEALER = '/portal?as=dealer'


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{tmp_path / "otp.db"}')
    monkeypatch.setenv('SMS_PROVIDER', 'console')
    monkeypatch.setenv('DEMO_LOGIN_ENABLED', 'False')
    app = create_app()
    app.config['TESTING'] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def sms(monkeypatch):
    """Captured SMS as (phone, code) pairs, newest last."""
    sent = []

    def capture(phone, code):
        sent.append((phone, code))
        return True, None

    monkeypatch.setattr(utils.sms, 'send_otp_sms', capture)
    return sent


def request_login_code(client, phone=DEALER_PHONE):
    return client.post('/auth/login', data={'action': 'send', 'phone': phone, 'policy_agreed': 'on'})


def submit_login_code(client, code):
    return client.post('/auth/login', data={'action': 'verify', 'code': code})


def resend_login_code(client):
    return client.post('/auth/login', data={'action': 'resend'})


def dealer_signed_in(client) -> bool:
    return client.get('/portal/status?as=dealer').get_json()['authenticated']


def wrong(code: str) -> str:
    return f'{(int(code) + 1) % 10 ** 6:06d}'


def shift_otp_rows(app, **changes: timedelta) -> None:
    """Move created_at / expires_at of every stored code by the given amounts."""
    with app.app_context():
        for row in LoginOTP.query.all():
            for column, delta in changes.items():
                setattr(row, column, getattr(row, column) + delta)
        db.session.commit()


def allow_resend(app) -> None:
    shift_otp_rows(app, created_at=-otp.RESEND_AFTER - timedelta(seconds=1))


# ── Login ───────────────────────────────────────────────────────────────────
def test_correct_code_signs_the_dealer_in(client, sms):
    page = request_login_code(client).get_data(as_text=True)

    assert sms[-1][0] == DEALER_PHONE
    assert len(sms[-1][1]) == 6 and sms[-1][1].isdigit()
    assert 'id="otpCode"' in page
    assert '+91 ••••• •3210' in page

    response = submit_login_code(client, sms[-1][1])

    assert response.headers['Location'].endswith(PORTAL_AS_DEALER)
    assert dealer_signed_in(client)


def test_code_is_stored_hashed(app, client, sms):
    request_login_code(client)

    with app.app_context():
        row = LoginOTP.query.one()
    assert sms[-1][1] not in row.code_hash


def test_wrong_code_is_rejected_and_shows_tries_left(client, sms):
    request_login_code(client)

    page = submit_login_code(client, wrong(sms[-1][1])).get_data(as_text=True)

    assert f'That code is not correct. {otp.MAX_ATTEMPTS - 1} tries left.' in page
    assert not dealer_signed_in(client)


def test_too_many_wrong_codes_kill_the_code(client, sms):
    request_login_code(client)
    code = sms[-1][1]
    for _ in range(otp.MAX_ATTEMPTS):
        submit_login_code(client, wrong(code))

    page = submit_login_code(client, code).get_data(as_text=True)

    assert 'Too many wrong tries.' in page
    assert not dealer_signed_in(client)


def test_last_allowed_try_can_still_be_correct(client, sms):
    request_login_code(client)
    code = sms[-1][1]
    for _ in range(otp.MAX_ATTEMPTS - 1):
        submit_login_code(client, wrong(code))

    response = submit_login_code(client, code)

    assert response.headers['Location'].endswith(PORTAL_AS_DEALER)


def test_expired_code_is_rejected(app, client, sms):
    request_login_code(client)
    shift_otp_rows(app, expires_at=-otp.CODE_TTL)

    page = submit_login_code(client, sms[-1][1]).get_data(as_text=True)

    assert 'This code has expired.' in page
    assert not dealer_signed_in(client)


def test_code_works_only_once(app, client, sms):
    request_login_code(client)
    code = sms[-1][1]
    submit_login_code(client, code)

    other = app.test_client()
    request_login_code(other)   # inside the resend window: no new code is sent
    page = submit_login_code(other, code).get_data(as_text=True)

    assert len(sms) == 1
    assert 'This code has expired.' in page
    assert not dealer_signed_in(other)


def test_unregistered_number_gets_no_code(client, sms):
    page = request_login_code(client, NEW_PHONE).get_data(as_text=True)

    assert 'No account uses this mobile number.' in page
    assert sms == []


@pytest.mark.parametrize('phone', ['+91 98765 43210', '09876543210', '091-9876543210'])
def test_number_is_accepted_in_common_formats(client, sms, phone):
    request_login_code(client, phone)

    assert sms[-1][0] == DEALER_PHONE


def test_sms_failure_shows_an_error_and_stays_on_the_number_step(client, monkeypatch):
    monkeypatch.setattr(utils.sms, 'send_otp_sms', lambda phone, code: (False, 'provider down'))

    page = request_login_code(client).get_data(as_text=True)

    assert 'send the SMS right now.' in page
    assert 'id="mainLoginForm"' in page
    assert 'provider down' not in page


# ── Resend ──────────────────────────────────────────────────────────────────
def test_resend_is_refused_during_the_cooldown(client, sms):
    request_login_code(client)

    page = resend_login_code(client).get_data(as_text=True)

    assert len(sms) == 1
    assert 'before requesting another code' in page
    assert 'id="otpResend"' in page and 'disabled' in page


def test_resend_after_cooldown_sends_a_new_code_and_retires_the_old_one(app, client, sms):
    request_login_code(client)
    old_code = sms[-1][1]
    allow_resend(app)

    resend_login_code(client)
    new_code = sms[-1][1]
    assert len(sms) == 2

    if old_code != new_code:   # 1-in-a-million chance they match
        assert 'not correct' in submit_login_code(client, old_code).get_data(as_text=True)
    assert submit_login_code(client, new_code).headers['Location'].endswith(PORTAL_AS_DEALER)


def test_hourly_limit_per_number(app, client, sms):
    for _ in range(otp.MAX_PER_PHONE_PER_HOUR):
        request_login_code(client)
        allow_resend(app)

    page = request_login_code(client).get_data(as_text=True)

    assert len(sms) == otp.MAX_PER_PHONE_PER_HOUR
    assert 'Too many codes requested.' in page


def test_hourly_limit_per_ip_ignores_forged_forwarding_headers(app, sms):
    """A client can't dodge the per-IP limit by sending a new X-Forwarded-For each time."""
    results = []
    for n in range(otp.MAX_PER_IP_PER_HOUR + 1):
        with app.test_request_context(environ_base={'REMOTE_ADDR': '81.2.69.142'},
                                      headers={'X-Forwarded-For': f'5.6.7.{n}'}):
            results.append(otp.issue_otp(f'98{n:08d}', 'register'))

    assert all(ok for ok, _, _ in results[:-1])
    assert results[-1] == (False, 'Too many codes requested. Please try again in an hour.', 0)


def test_parallel_send_requests_produce_only_one_code(app):
    """Two requests that both passed the pre-insert checks: only the earlier one may send."""
    with app.app_context():
        now = otp._now()
        rows = [LoginOTP(phone=DEALER_PHONE, purpose='login', code_hash='x', ip_address='203.0.113.7',
                         created_at=now, expires_at=now + otp.CODE_TTL) for _ in range(2)]
        db.session.add_all(rows)
        db.session.commit()
        hour_ago = now - timedelta(hours=1)

        assert otp._refusal_from_parallel_requests(rows[0], hour_ago) is None
        ok, _, wait = otp._refusal_from_parallel_requests(rows[1], hour_ago)
        assert not ok and wait > 0


# ── Sign-up ─────────────────────────────────────────────────────────────────
def register(client, **fields):
    data = {'action': 'send', 'name': 'Asha Verma', 'email': 'asha@example.com',
            'phone': NEW_PHONE, 'role': 'user', 'city': 'Pune', **fields}
    return client.post('/auth/register', data=data)


def test_sign_up_creates_the_account_after_the_code_is_verified(app, client, sms):
    register(client)
    with app.app_context():
        assert User.query.filter_by(email='asha@example.com').first() is None

    response = client.post('/auth/register', data={'action': 'verify', 'code': sms[-1][1]})

    assert '/portal' in response.headers['Location']
    with app.app_context():
        user = User.query.filter_by(email='asha@example.com').one()
    assert otp.normalise_phone(user.phone) == NEW_PHONE


def test_sign_up_with_wrong_code_creates_nothing(app, client, sms):
    register(client)

    page = client.post('/auth/register', data={'action': 'verify', 'code': wrong(sms[-1][1])}).get_data(as_text=True)

    assert 'That code is not correct.' in page
    with app.app_context():
        assert User.query.filter_by(email='asha@example.com').first() is None


def test_sign_up_with_a_registered_number_sends_no_code(client, sms):
    register(client, phone=DEALER_PHONE, email='someone@example.com')

    assert sms == []   # the number already has an account, so no code is sent


# ── SMS providers ───────────────────────────────────────────────────────────
@pytest.mark.parametrize('provider', ['fast2sms', 'msg91', 'twilio'])
def test_provider_without_credentials_reports_an_error(monkeypatch, provider):
    monkeypatch.setenv('SMS_PROVIDER', provider)
    for name in ('FAST2SMS_API_KEY', 'MSG91_AUTH_KEY', 'MSG91_TEMPLATE_ID',
                 'TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_FROM'):
        monkeypatch.delenv(name, raising=False)

    ok, error = utils.sms.send_otp_sms(DEALER_PHONE, '123456')

    assert not ok
    assert 'must' in error or 'not set' in error


def test_provider_network_error_is_reported_not_raised(monkeypatch):
    monkeypatch.setenv('SMS_PROVIDER', 'fast2sms')
    monkeypatch.setenv('FAST2SMS_API_KEY', 'test-key')

    def unreachable(*args, **kwargs):
        raise requests.ConnectionError('no route to host')

    monkeypatch.setattr(utils.sms.requests, 'post', unreachable)

    ok, error = utils.sms.send_otp_sms(DEALER_PHONE, '123456')

    assert not ok and 'unreachable' in error


def test_unknown_provider_is_reported(monkeypatch):
    monkeypatch.setenv('SMS_PROVIDER', 'carrier-pigeon')

    ok, error = utils.sms.send_otp_sms(DEALER_PHONE, '123456')

    assert not ok and 'Unknown SMS_PROVIDER' in error


def test_sms_text_states_the_real_expiry():
    minutes = int(otp.CODE_TTL.total_seconds() // 60)

    assert f'expires in {minutes} minutes' in utils.sms._message('123456')


# ── Secret key ──────────────────────────────────────────────────────────────
def test_secret_key_from_environment_is_used(tmp_path, monkeypatch):
    monkeypatch.setenv('SECRET_KEY', 'a-private-key-for-this-test')

    assert _load_secret_key(str(tmp_path)) == 'a-private-key-for-this-test'


@pytest.mark.parametrize('value', ['', 'Caryanams-secret-2025-xK9mP', 'change-me-to-a-long-random-string'])
def test_published_or_missing_secret_key_is_replaced_by_a_stored_random_key(tmp_path, monkeypatch, value):
    monkeypatch.setenv('SECRET_KEY', value)

    first = _load_secret_key(str(tmp_path))

    assert first != value and len(first) >= 40
    assert _load_secret_key(str(tmp_path)) == first   # same key after a restart


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self.ok = status_code < 400
        self.headers = {'content-type': 'application/json'}
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


@pytest.fixture
def msg91(monkeypatch):
    """Configures MSG91 and records each request; set .reply to change MSG91's answer."""
    monkeypatch.setenv('SMS_PROVIDER', 'msg91')
    monkeypatch.setenv('MSG91_AUTH_KEY', ' test-authkey ')
    monkeypatch.setenv('MSG91_TEMPLATE_ID', 'test-template')

    class Recorder:
        calls = []
        reply = FakeResponse(200, {'type': 'success', 'request_id': 'abc'})

    def post(url, **kwargs):
        Recorder.calls.append((url, kwargs))
        return Recorder.reply

    monkeypatch.setattr(utils.sms.requests, 'post', post)
    return Recorder


def test_msg91_request_carries_our_code_and_asks_for_the_real_send_result(msg91):
    ok, error = utils.sms.send_otp_sms(DEALER_PHONE, '482913')

    url, kwargs = msg91.calls[-1]
    assert (ok, error) == (True, None)
    assert url == 'https://control.msg91.com/api/v5/otp'
    assert kwargs['headers']['authkey'] == 'test-authkey'
    assert kwargs['params'] == {'template_id': 'test-template', 'mobile': '91' + DEALER_PHONE,
                                'otp': '482913', 'otp_expiry': 5, 'realTimeResponse': 1}


def test_msg91_error_is_reported_with_its_message(msg91):
    msg91.reply = FakeResponse(400, {'type': 'error', 'message': 'Invalid template'})

    ok, error = utils.sms.send_otp_sms(DEALER_PHONE, '482913')

    assert not ok
    assert error == 'MSG91 400: Invalid template'
