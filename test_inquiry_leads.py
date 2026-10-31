"""
test_inquiry_leads.py — customer inquiries are saved as CRM leads.

Covers the marketplace car page, the dealer website (car page and contact
form), duplicate protection, copying old inquiries, the dealer and admin
screens, and dealer data isolation.

Run from the project root:

    pytest test_inquiry_leads.py
"""
from datetime import datetime

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import OperationalError

import utils.inquiry_leads as inquiry_service
import utils.sms
from app import create_app
from db import user_create
from models import DealerKYC, Inquiry, Lead, User, Vehicle, db
from subscription_gate import activate_plan_for_dealer
from utils.inquiry_leads import create_lead_from_inquiry, migrate_legacy_inquiries

SAMPLE_DEALER_EMAIL = 'dealer@caryanams.com'  # created by models.seed_demo_data()
SAMPLE_DEALER_PHONE = '9876543210'
WEBSITE = '/caryanams/rajeshmotors/rajesh'    # the sample dealer's website
CUSTOMER = {'name': 'Asha Verma', 'phone': '+91 98111 22233',
            'email': 'Asha@Example.com', 'message': 'Is the price negotiable?'}


def _approve_kyc(dealer_id):
    kyc = DealerKYC.query.filter_by(dealer_id=dealer_id).first() or DealerKYC(dealer_id=dealer_id)
    for doc in ('aadhaar_front', 'aadhaar_back', 'pan_card'):  # the KYC gate needs the files too
        setattr(kyc, doc, getattr(kyc, doc) or f'{doc}.png')
        setattr(kyc, f'{doc}_status', 'approved')
    db.session.add(kyc)
    db.session.commit()


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{tmp_path / "inquiry_leads.db"}')
    monkeypatch.setenv('SMS_PROVIDER', 'console')
    monkeypatch.delenv('DEMO_LOGIN_ENABLED', raising=False)
    app = create_app()
    app.config['TESTING'] = True
    with app.app_context():
        dealer = User.query.filter_by(email=SAMPLE_DEALER_EMAIL).one()
        dealer.website_name = 'rajesh'
        _approve_kyc(dealer.id)
        activate_plan_for_dealer(dealer.id, 'pro')  # the dealer website is a Pro feature
    return app


@pytest.fixture
def dealer_id(app):
    with app.app_context():
        return User.query.filter_by(email=SAMPLE_DEALER_EMAIL).one().id


@pytest.fixture
def car_id(app, dealer_id):
    with app.app_context():
        return Vehicle.query.filter_by(dealer_id=dealer_id, status='available').first().id


@pytest.fixture
def other_dealer(app, car_id):
    """A second dealer (KYC approved, Pro) owning one car: (dealer_id, car_id, phone)."""
    phone = '9811100001'
    with app.app_context():
        user_create({'name': 'Other Motors', 'email': 'other@example.com', 'phone': phone,
                     'role': 'dealer', 'password': 'Secret#123'})
        other = User.query.filter_by(email='other@example.com').one()
        _approve_kyc(other.id)
        activate_plan_for_dealer(other.id, 'pro')
        car = Vehicle.query.filter(Vehicle.dealer_id != other.id, Vehicle.id != car_id).first()
        car.dealer_id = other.id
        db.session.commit()
        return other.id, car.id, phone


@pytest.fixture
def sign_in(app, monkeypatch):
    """sign_in(phone) returns a test client signed in to that dealer via SMS OTP."""
    sent_codes = []

    def capture_sms(phone, code):
        sent_codes.append(code)
        return True, None

    monkeypatch.setattr(utils.sms, 'send_otp_sms', capture_sms)

    def sign_in_as(phone):
        client = app.test_client()
        client.post('/auth/login', data={'action': 'send', 'phone': phone, 'policy_agreed': 'on'})
        client.post('/auth/login', data={'action': 'verify', 'code': sent_codes[-1]})
        return client

    return sign_in_as


def inquiry_leads(app) -> list[dict]:
    with app.app_context():
        return [lead.to_dict() for lead in
                Lead.query.filter(Lead.inquiry_type.isnot(None)).order_by(Lead.id)]


def send_marketplace_inquiry(client, car_id, **overrides):
    data = {**CUSTOMER, 'inquiry_type': 'test_drive', **overrides}
    return client.post(f'/car/{car_id}/inquire', data=data, follow_redirects=True)


# ── Marketplace ─────────────────────────────────────────────────────────────
def test_marketplace_inquiry_becomes_a_lead(app, dealer_id, car_id):
    page = send_marketplace_inquiry(app.test_client(), car_id).get_data(as_text=True)

    assert 'Inquiry sent successfully!' in page
    [lead] = inquiry_leads(app)
    assert (lead['dealer_id'], lead['vehicle_id']) == (dealer_id, car_id)
    assert lead['customer_name'] == 'Asha Verma'
    assert lead['customer_phone'] == '9811122233'
    assert lead['customer_email'] == 'asha@example.com'
    assert (lead['source'], lead['stage'], lead['inquiry_type']) == ('marketplace', 'new', 'test_drive')
    assert lead['notes'] == lead['inquiry_message'] == 'Is the price negotiable?'
    with app.app_context():
        assert Inquiry.query.count() == 0  # no separate inquiry record any more


def test_inquiry_lead_appears_in_crm_leads(app, car_id, sign_in):
    send_marketplace_inquiry(app.test_client(), car_id)

    page = sign_in(SAMPLE_DEALER_PHONE).get('/dealer/leads').get_data(as_text=True)

    assert 'Asha Verma' in page and '9811122233' in page
    assert 'Test Drive ·' in page  # inquiry type and received date under Source


def test_resending_an_inquiry_does_not_duplicate_the_lead(app, car_id):
    client = app.test_client()
    send_marketplace_inquiry(client, car_id)
    send_marketplace_inquiry(client, car_id)
    send_marketplace_inquiry(client, car_id, message='  is the PRICE   negotiable? ')
    assert len(inquiry_leads(app)) == 1

    send_marketplace_inquiry(client, car_id, message='Can I see it on Sunday?')
    assert len(inquiry_leads(app)) == 2


@pytest.mark.parametrize('overrides, message', [
    ({'name': ''}, 'Name and phone are required'),
    ({'phone': '12345'}, 'Please enter a valid 10-digit mobile number.'),
    ({'email': 'not-an-email'}, 'Please enter a valid email address.'),
])
def test_invalid_marketplace_inquiry_is_rejected(app, car_id, overrides, message):
    page = send_marketplace_inquiry(app.test_client(), car_id, **overrides).get_data(as_text=True)

    assert message in page
    assert inquiry_leads(app) == []


def test_a_failed_save_is_reported_not_claimed_as_sent(app, car_id, monkeypatch):
    def database_down(data):
        raise OperationalError('INSERT INTO leads', {}, Exception('database unavailable'))

    monkeypatch.setattr(inquiry_service, 'lead_create', database_down)
    client = app.test_client()

    page = send_marketplace_inquiry(client, car_id).get_data(as_text=True)
    assert 'Please try again in a moment.' in page
    assert 'Inquiry sent successfully!' not in page

    response = client.post(f'{WEBSITE}/inquiry', data={**CUSTOMER, 'vehicle_id': car_id})
    assert response.status_code == 503
    assert response.get_json()['success'] is False


# ── Dealer website ──────────────────────────────────────────────────────────
def test_website_car_page_renders(app, car_id):
    assert app.test_client().get(f'{WEBSITE}/car/{car_id}').status_code == 200


def test_website_car_inquiry_becomes_a_lead(app, dealer_id, car_id):
    response = app.test_client().post(f'{WEBSITE}/inquiry', data={**CUSTOMER, 'vehicle_id': car_id})

    assert response.get_json() == {'success': True, 'message': 'Inquiry submitted!'}
    [lead] = inquiry_leads(app)
    assert (lead['dealer_id'], lead['vehicle_id']) == (dealer_id, car_id)
    assert (lead['source'], lead['inquiry_type']) == ('website', 'vehicle')


def test_website_contact_form_becomes_a_lead(app, dealer_id):
    response = app.test_client().post(f'{WEBSITE}/contact', data=CUSTOMER, follow_redirects=True)

    assert 'Message sent!' in response.get_data(as_text=True)
    [lead] = inquiry_leads(app)
    assert (lead['dealer_id'], lead['vehicle_id']) == (dealer_id, None)
    assert (lead['source'], lead['inquiry_type']) == ('website', 'contact')


def test_blank_website_inquiries_are_rejected(app, car_id):
    client = app.test_client()

    response = client.post(f'{WEBSITE}/inquiry', data={'vehicle_id': car_id, 'name': '', 'phone': ''})
    assert response.status_code == 400
    assert response.get_json()['error'] == 'Please enter your name.'

    page = client.post(f'{WEBSITE}/contact', data={'name': '', 'phone': ''},
                       follow_redirects=True).get_data(as_text=True)
    assert 'Please enter your name.' in page
    assert inquiry_leads(app) == []


# ── Dealer isolation ────────────────────────────────────────────────────────
def test_website_inquiry_cannot_attach_another_dealers_car(app, dealer_id, other_dealer):
    _, other_car, _ = other_dealer

    app.test_client().post(f'{WEBSITE}/inquiry', data={**CUSTOMER, 'vehicle_id': other_car})

    [lead] = inquiry_leads(app)
    assert (lead['dealer_id'], lead['vehicle_id']) == (dealer_id, None)


def test_dealers_only_see_their_own_inquiry_leads(app, car_id, other_dealer, sign_in):
    send_marketplace_inquiry(app.test_client(), car_id)
    [lead] = inquiry_leads(app)
    other = sign_in(other_dealer[2])

    leads_page = other.get('/dealer/leads')
    assert leads_page.status_code == 200
    assert 'Asha Verma' not in leads_page.get_data(as_text=True)
    response = other.get(f"/dealer/leads/edit/{lead['id']}")
    assert response.headers['Location'].endswith('/dealer/leads')  # "Lead not found"


# ── Dealer screens ──────────────────────────────────────────────────────────
def test_inquiries_menu_is_gone_and_old_urls_redirect(sign_in):
    client = sign_in(SAMPLE_DEALER_PHONE)

    page = client.get('/dealer/dashboard').get_data(as_text=True)
    assert '<span>Inquiries</span>' not in page
    assert '/dealer/inquiries' not in page and 'Recent Inquiries' not in page

    for response in (client.get('/dealer/inquiries'), client.post('/dealer/inquiries/respond/1')):
        assert response.status_code == 302
        assert response.headers['Location'].endswith('/dealer/leads')


def test_lead_page_shows_the_inquiry_and_editing_keeps_it(app, car_id, sign_in):
    send_marketplace_inquiry(app.test_client(), car_id)
    [lead] = inquiry_leads(app)
    client = sign_in(SAMPLE_DEALER_PHONE)

    page = client.get(f"/dealer/leads/edit/{lead['id']}").get_data(as_text=True)
    assert 'Customer Inquiry' in page and 'Is the price negotiable?' in page
    assert '<option value="marketplace" selected>' in page

    client.post(f"/dealer/leads/edit/{lead['id']}", data={
        'customer_name': lead['customer_name'], 'customer_phone': lead['customer_phone'],
        'customer_email': lead['customer_email'], 'vehicle_id': car_id,
        'source': 'marketplace', 'stage': 'interested', 'notes': 'Called back, visiting Saturday.'})

    [updated] = inquiry_leads(app)
    assert (updated['stage'], updated['source']) == ('interested', 'marketplace')
    assert updated['notes'] == 'Called back, visiting Saturday.'
    assert updated['inquiry_message'] == 'Is the price negotiable?'  # the original stays


# ── Old inquiries and the database guard ────────────────────────────────────
def test_old_inquiries_are_copied_into_leads_once(app, dealer_id, car_id):
    received = datetime(2026, 9, 10, 14, 30)
    with app.app_context():
        db.session.add_all([
            Inquiry(dealer_id=dealer_id, vehicle_id=car_id, name='dsf', phone='+91 98222 33344',
                    email='d@example.com', message=' is interested in booking a test drive.',
                    inquiry_type='test_drive', status='pending', created_at=received),
            Inquiry(dealer_id=dealer_id, vehicle_id=car_id, name='Ravi', phone='9833344455',
                    inquiry_type='general', status='responded'),
            Inquiry(dealer_id=dealer_id, vehicle_id=None, name='Meera', phone='9844455566',
                    message='Do you buy used cars?', inquiry_type='contact', status='pending'),
        ])
        db.session.commit()

        assert migrate_legacy_inquiries() == 3
        assert migrate_legacy_inquiries() == 0  # safe to run again
        assert Inquiry.query.count() == 3       # the archive is untouched

    test_drive, responded, contact = inquiry_leads(app)
    assert test_drive['customer_phone'] == '9822233344'
    assert test_drive['created_at'] == received
    assert (test_drive['source'], test_drive['stage']) == ('marketplace', 'new')
    assert test_drive['inquiry_message'] == 'is interested in booking a test drive.'
    assert responded['stage'] == 'connected'
    assert (contact['source'], contact['vehicle_id'], contact['dealer_id']) == ('website', None, dealer_id)


def test_the_database_allows_one_lead_per_inquiry(app, dealer_id, car_id, monkeypatch):
    real_lead_create = inquiry_service.lead_create

    def racing_lead_create(data):
        real_lead_create(dict(data))  # a parallel request saves the same inquiry first
        return real_lead_create(data)

    monkeypatch.setattr(inquiry_service, 'lead_create', racing_lead_create)
    with app.app_context():
        indexes = {index['name']: index for index in inspect(db.engine).get_indexes('leads')}
        assert indexes['ix_leads_inquiry_key']['unique']

        lead, created = create_lead_from_inquiry(
            dealer_id=dealer_id, vehicle_id=car_id, source='marketplace',
            inquiry_type='general', name='Asha Verma', phone='9811122233')

        assert created is False
        assert Lead.query.filter(Lead.inquiry_type.isnot(None)).count() == 1


# ── Admin ───────────────────────────────────────────────────────────────────
def test_admin_sees_inquiry_leads_and_the_old_page_redirects(app, car_id):
    send_marketplace_inquiry(app.test_client(), car_id)
    [lead] = inquiry_leads(app)
    client = app.test_client()
    with client.session_transaction() as session:
        session['admin_logged_in'] = True
        session['admin_username'] = 'admin'

    response = client.get('/admin/inquiries')
    assert response.headers['Location'].endswith('/admin/leads')
    page = client.get('/admin/leads').get_data(as_text=True)
    assert '/admin/inquiries' not in page  # sidebar link removed
    assert 'Asha Verma' in page
    # Saving the admin edit form must not switch the source to the first option.
    edit_page = client.get(f"/admin/leads/{lead['id']}/edit").get_data(as_text=True)
    assert '<option value="marketplace" selected>' in edit_page
