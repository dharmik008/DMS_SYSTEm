"""
test_invoice_branding.py — the Deals & Sales invoice shows the signed-in
dealer's own company details, never Caryanams branding or another dealer's,
and no CGST/SGST breakdown.

Run from the project root:

    pytest test_invoice_branding.py
"""
import pytest

import utils.sms
from app import create_app
from db import user_create
from models import Deal, DealerKYC, User, Vehicle, db
from subscription_gate import activate_plan_for_dealer

SAMPLE_DEALER_EMAIL = 'dealer@caryanams.com'  # created by models.seed_demo_data()
SAMPLE_DEALER_PHONE = '9876543210'
OTHER_DEALER_EMAIL = 'abc@example.com'
OTHER_DEALER_PHONE = '9811100001'


def _approve_kyc(dealer_id):
    kyc = DealerKYC.query.filter_by(dealer_id=dealer_id).first() or DealerKYC(dealer_id=dealer_id)
    for doc in ('aadhaar_front', 'aadhaar_back', 'pan_card'):
        setattr(kyc, doc, getattr(kyc, doc) or f'{doc}.png')
        setattr(kyc, f'{doc}_status', 'approved')
    db.session.add(kyc)
    db.session.commit()


def _dealer_id(email):
    return User.query.filter_by(email=email).one().id


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{tmp_path / "invoice.db"}')
    monkeypatch.setenv('SMS_PROVIDER', 'console')
    monkeypatch.setenv('PRIVATE_DOCUMENTS_FOLDER', str(tmp_path / 'private_documents'))
    monkeypatch.delenv('DEMO_LOGIN_ENABLED', raising=False)
    app = create_app()
    app.config['TESTING'] = True
    # Keep test logos out of the real static/images/uploads folder.
    app.config['UPLOAD_FOLDER'] = str(tmp_path / 'uploads')
    (tmp_path / 'uploads').mkdir()
    with app.app_context():
        _approve_kyc(_dealer_id(SAMPLE_DEALER_EMAIL))
        user_create({'name': 'Anil Shah', 'email': OTHER_DEALER_EMAIL, 'phone': OTHER_DEALER_PHONE,
                     'role': 'dealer', 'company_name': 'ABC Motors Pvt Ltd', 'city': 'Pune',
                     'password': 'Secret#123'})
        other_id = _dealer_id(OTHER_DEALER_EMAIL)
        _approve_kyc(other_id)
        activate_plan_for_dealer(other_id, 'growth')
    return app


@pytest.fixture
def sign_in(app, monkeypatch):
    """sign_in(phone) returns a test client signed in to that dealer via SMS OTP."""
    sent_codes = []

    def capture_sms(phone, code):
        sent_codes.append(code)
        return True, None

    monkeypatch.setattr(utils.sms, 'send_otp_sms', capture_sms)

    def sign_in_as(phone=SAMPLE_DEALER_PHONE):
        client = app.test_client()
        client.post('/auth/login', data={'action': 'send', 'phone': phone, 'policy_agreed': 'on'})
        client.post('/auth/login', data={'action': 'verify', 'code': sent_codes[-1]})
        return client

    return sign_in_as


def update_dealer(app, email, **fields):
    with app.app_context():
        dealer = User.query.filter_by(email=email).one()
        for name, value in fields.items():
            setattr(dealer, name, value)
        db.session.commit()


def create_deal(app, client, email) -> int:
    with app.app_context():
        dealer_id = _dealer_id(email)
        vehicle = Vehicle(dealer_id=dealer_id, make='Honda', model='City', year=2021, price=1020000,
                          purchase_price=900000, status='available')
        db.session.add(vehicle)
        db.session.commit()
        vehicle_id = vehicle.id
    client.post('/dealer/deals/add', data={'vehicle_id': vehicle_id, 'customer_name': 'Vikram Singh',
                                           'customer_phone': '9000000005', 'final_price': '1020000',
                                           'status': 'delivered'})
    with app.app_context():
        return Deal.query.filter_by(vehicle_id=vehicle_id).one().id


def invoice_html(client, deal_id) -> str:
    """The invoice card itself, without the DMS sidebar and top bar around it."""
    response = client.get(f'/dealer/deals/invoice/{deal_id}')
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    return page[page.index('class="invoice-wrapper"'):]


def page_title(client, deal_id) -> str:
    page = client.get(f'/dealer/deals/invoice/{deal_id}').get_data(as_text=True)
    return page[page.index('<title>') + len('<title>'):page.index('</title>')]


def test_invoice_shows_the_dealers_registered_company(app, sign_in):
    client = sign_in()
    deal_id = create_deal(app, client, SAMPLE_DEALER_EMAIL)

    invoice = invoice_html(client, deal_id)

    assert 'Rajesh Motors Pvt Ltd' in invoice
    assert 'Automobile Sales Invoice' in invoice
    assert 'Mumbai' in invoice
    assert SAMPLE_DEALER_EMAIL in invoice
    assert 'Caryanams' not in invoice
    assert 'images/logo.png' not in invoice
    assert 'Rajesh Motors Pvt Ltd' in page_title(client, deal_id)
    assert 'Caryanams' not in page_title(client, deal_id)


def test_invoice_keeps_number_date_and_status(app, sign_in):
    client = sign_in()
    deal_id = create_deal(app, client, SAMPLE_DEALER_EMAIL)

    invoice = invoice_html(client, deal_id)

    assert f'#INV-{deal_id:04d}' in invoice
    assert 'Date:' in invoice
    assert 'delivered' in invoice


def test_invoice_without_logo_shows_company_initial(app, sign_in):
    client = sign_in()
    deal_id = create_deal(app, client, SAMPLE_DEALER_EMAIL)

    invoice = invoice_html(client, deal_id)

    assert '<img' not in invoice.split('Automobile Sales Invoice')[0]
    assert '>R</div>' in invoice


def test_invoice_shows_uploaded_logo(app, sign_in, tmp_path):
    (tmp_path / 'uploads' / 'wlogo_test.png').write_bytes(b'png')
    update_dealer(app, SAMPLE_DEALER_EMAIL, website_logo='wlogo_test.png')
    client = sign_in()
    deal_id = create_deal(app, client, SAMPLE_DEALER_EMAIL)

    invoice = invoice_html(client, deal_id)

    assert 'src="/static/images/uploads/wlogo_test.png"' in invoice
    assert 'alt="Rajesh Motors Pvt Ltd"' in invoice


def test_invoice_falls_back_to_initial_when_logo_file_is_missing(app, sign_in):
    update_dealer(app, SAMPLE_DEALER_EMAIL, website_logo='wlogo_deleted.png')
    client = sign_in()
    deal_id = create_deal(app, client, SAMPLE_DEALER_EMAIL)

    invoice = invoice_html(client, deal_id)

    assert 'wlogo_deleted.png' not in invoice
    assert '>R</div>' in invoice


def test_invoice_uses_dealer_name_when_company_name_is_blank(app, sign_in):
    update_dealer(app, SAMPLE_DEALER_EMAIL, company_name=None)
    client = sign_in()
    deal_id = create_deal(app, client, SAMPLE_DEALER_EMAIL)

    assert 'Rajesh Motors' in invoice_html(client, deal_id)


def test_invoice_shows_address_and_skips_city_already_in_it(app, sign_in):
    update_dealer(app, SAMPLE_DEALER_EMAIL, address='12 Link Road, Andheri West, Mumbai')
    client = sign_in()
    deal_id = create_deal(app, client, SAMPLE_DEALER_EMAIL)

    invoice = invoice_html(client, deal_id)

    assert '12 Link Road, Andheri West, Mumbai' in invoice
    assert invoice.count('Mumbai') == 1


def test_each_dealer_sees_only_their_own_company(app, sign_in):
    first = sign_in()
    first_deal = create_deal(app, first, SAMPLE_DEALER_EMAIL)
    other = sign_in(OTHER_DEALER_PHONE)
    other_deal = create_deal(app, other, OTHER_DEALER_EMAIL)

    other_invoice = invoice_html(other, other_deal)
    assert 'ABC Motors Pvt Ltd' in other_invoice
    assert 'Rajesh Motors' not in other_invoice

    response = other.get(f'/dealer/deals/invoice/{first_deal}')
    assert response.status_code == 302
    assert 'Rajesh Motors' not in response.get_data(as_text=True)


def test_invoice_has_no_gst_rows_and_totals_the_sale_price(app, sign_in):
    client = sign_in()
    deal_id = create_deal(app, client, SAMPLE_DEALER_EMAIL)

    invoice = invoice_html(client, deal_id)
    total_row = invoice[invoice.index('TOTAL AMOUNT'):invoice.index('</tfoot>')]

    assert 'CGST' not in invoice
    assert 'SGST' not in invoice
    assert '₹1,020,000.00' in total_row
