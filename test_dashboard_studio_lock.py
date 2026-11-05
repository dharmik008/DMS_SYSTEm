"""
test_dashboard_studio_lock.py — the dashboard's Caryanams Studio card follows the plan.

Starter has no Studio (subscription_features.PLAN_FEATURES), and a Growth or Pro
plan activated with "Use Free For Now" keeps it locked until the plan is paid
for (subscription_features.PAID_ONLY_FEATURES). Locked means the dashboard card
behaves like the locked sidebar link and /studio/ itself is refused.

Run from the project root:

    pytest test_dashboard_studio_lock.py
"""
from html.parser import HTMLParser

import pytest

import utils.sms
from app import create_app
from models import DealerKYC, User, db

SAMPLE_DEALER_EMAIL = 'dealer@caryanams.com'  # created by models.seed_demo_data()
SAMPLE_DEALER_PHONE = '9876543210'
LOCKED_CLICK = "return handleLockedFeature(event, 'Caryanams Studio')"


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


def choose_plan(client, plan, method):
    """Pick a plan the way the plan page does: method 'demo' (Demo Payment) or 'free'."""
    response = client.post('/dealer/subscription/activate', json={'plan': plan, 'method': method})
    assert response.get_json()['success'], response.get_json()


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
    """Returns sign_in(plan, method): signs the sample dealer in via SMS OTP, then picks `plan`."""
    sent_codes = []

    def capture_sms(phone, code):
        sent_codes.append(code)
        return True, None

    monkeypatch.setattr(utils.sms, 'send_otp_sms', capture_sms)

    def sign_in(plan, method='demo'):
        with app.app_context():
            dealer = User.query.filter_by(email=SAMPLE_DEALER_EMAIL).one()
            # Approve KYC so the dashboard itself renders (it has its own KYC gate).
            kyc = DealerKYC.query.filter_by(dealer_id=dealer.id).one()
            kyc.aadhaar_front_status = kyc.aadhaar_back_status = kyc.pan_card_status = 'approved'
            db.session.commit()

        client = app.test_client()
        client.post('/auth/login',
                    data={'action': 'send', 'phone': SAMPLE_DEALER_PHONE, 'policy_agreed': 'on'})
        client.post('/auth/login', data={'action': 'verify', 'code': sent_codes[-1]})
        # The activation route turns the ₹0 Starter plan into a free activation
        # whatever method is sent, just as the plan page's "Continue Free" does.
        choose_plan(client, plan, method)
        return client

    return sign_in


def test_starter_sees_the_studio_card_locked(dealer_on_plan):
    card = studio_card(dealer_on_plan('starter'))

    assert card['href'] == '/dealer/subscription'
    assert card['onclick'] == LOCKED_CLICK


@pytest.mark.parametrize('plan', ['growth', 'pro'])
def test_paid_plans_keep_the_studio_card_open(dealer_on_plan, plan):
    card = studio_card(dealer_on_plan(plan))

    assert card['href'] == '/studio/'
    assert 'onclick' not in card


def test_starter_cannot_open_studio_by_url(dealer_on_plan):
    response = dealer_on_plan('starter').get('/studio/')

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/dealer/subscription')


@pytest.mark.parametrize('plan', ['growth', 'pro'])
def test_free_activation_keeps_the_studio_card_locked(dealer_on_plan, plan):
    card = studio_card(dealer_on_plan(plan, method='free'))  # "Use Free For Now"

    assert card['href'] == '/dealer/subscription'
    assert card['onclick'] == LOCKED_CLICK


@pytest.mark.parametrize('plan', ['growth', 'pro'])
def test_free_activation_cannot_open_studio_by_url(dealer_on_plan, plan):
    client = dealer_on_plan(plan, method='free')

    page = client.get('/studio/')
    api = client.get('/studio/api/gallery')

    assert page.status_code == 302
    assert page.headers['Location'].endswith('/dealer/subscription')
    assert api.status_code == 403
    assert api.get_json()['error'] == 'FEATURE_NOT_AVAILABLE'


def test_paying_for_the_plan_unlocks_studio_after_a_free_activation(dealer_on_plan):
    client = dealer_on_plan('growth', method='free')

    choose_plan(client, 'growth', 'demo')

    assert studio_card(client)['href'] == '/studio/'
    assert client.get('/studio/').status_code == 200
