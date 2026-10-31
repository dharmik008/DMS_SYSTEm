"""
test_dashboard_studio_lock.py — the dashboard's Caryanams Studio card follows the plan.

Starter has no Studio (subscription_features.PLAN_FEATURES), so its dashboard
card is locked like the sidebar link; Growth and Pro keep the normal link.

Run from the project root:

    pytest test_dashboard_studio_lock.py
"""
from html.parser import HTMLParser

import pytest

import utils.sms
from app import create_app
from models import DealerKYC, User
from subscription_gate import activate_plan_for_dealer

SAMPLE_DEALER_EMAIL = 'dealer@caryanams.com'  # created by models.seed_demo_data()
SAMPLE_DEALER_PHONE = '9876543210'


class _AttrsById(HTMLParser):
    """Collects the attributes of the element with the given id."""

    def __init__(self, element_id):
        super().__init__()
        self.element_id = element_id
        self.attrs = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('id') == self.element_id:
            self.attrs = attrs


def studio_card(client) -> dict:
    response = client.get('/dealer/dashboard')
    assert response.status_code == 200
    parser = _AttrsById('studioCta')
    parser.feed(response.get_data(as_text=True))
    assert parser.attrs is not None, 'Studio card missing from the dashboard'
    return parser.attrs


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{tmp_path / "studio_lock.db"}')
    monkeypatch.setenv('SMS_PROVIDER', 'console')
    monkeypatch.delenv('DEMO_LOGIN_ENABLED', raising=False)
    app = create_app()
    app.config['TESTING'] = True
    return app


@pytest.fixture
def dealer_on_plan(app, monkeypatch):
    """Returns sign_in(plan): puts the sample dealer on `plan` and signs it in via SMS OTP."""
    sent_codes = []

    def capture_sms(phone, code):
        sent_codes.append(code)
        return True, None

    monkeypatch.setattr(utils.sms, 'send_otp_sms', capture_sms)

    def sign_in(plan):
        with app.app_context():
            dealer = User.query.filter_by(email=SAMPLE_DEALER_EMAIL).one()
            # Approve KYC so the dashboard itself renders (it has its own KYC gate).
            kyc = DealerKYC.query.filter_by(dealer_id=dealer.id).one()
            kyc.aadhaar_front_status = kyc.aadhaar_back_status = kyc.pan_card_status = 'approved'
            activate_plan_for_dealer(dealer.id, plan)  # commits the KYC change too

        client = app.test_client()
        client.post('/auth/login',
                    data={'action': 'send', 'phone': SAMPLE_DEALER_PHONE, 'policy_agreed': 'on'})
        client.post('/auth/login', data={'action': 'verify', 'code': sent_codes[-1]})
        return client

    return sign_in


def test_starter_sees_the_studio_card_locked(dealer_on_plan):
    card = studio_card(dealer_on_plan('starter'))

    assert card['href'] == '/dealer/subscription'
    assert card['onclick'] == "return handleLockedFeature(event, 'Caryanams Studio')"


@pytest.mark.parametrize('plan', ['growth', 'pro'])
def test_paid_plans_keep_the_studio_card_open(dealer_on_plan, plan):
    card = studio_card(dealer_on_plan(plan))

    assert card['href'] == '/studio/'
    assert 'onclick' not in card


def test_starter_cannot_open_studio_by_url(dealer_on_plan):
    response = dealer_on_plan('starter').get('/studio/')

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/dealer/subscription')
