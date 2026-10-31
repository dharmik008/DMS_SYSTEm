"""
test_deal_status.py — the Purchase → Sale Trail (Car Status history), Document
Status, and the shared status dropdowns.

Run from the project root:

    pytest test_deal_status.py
"""
import re

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

import utils.sms
from app import create_app
from db import deal_create, deal_update, user_create
from models import Deal, DealerKYC, DealStatusHistory, User, Vehicle, db
from subscription_gate import activate_plan_for_dealer
from utils.deal_history import start_tracking_existing_deals
from utils.statuses import (DEAL, VEHICLE, InvalidStatus, canonical_status, clean_status,
                            status_options)

SAMPLE_DEALER_EMAIL = 'dealer@caryanams.com'  # created by models.seed_demo_data()
SAMPLE_DEALER_PHONE = '9876543210'
OTHER_DEALER_PHONE = '9811100001'


def _approve_kyc(dealer_id):
    kyc = DealerKYC.query.filter_by(dealer_id=dealer_id).first() or DealerKYC(dealer_id=dealer_id)
    for doc in ('aadhaar_front', 'aadhaar_back', 'pan_card'):
        setattr(kyc, doc, getattr(kyc, doc) or f'{doc}.png')
        setattr(kyc, f'{doc}_status', 'approved')
    db.session.add(kyc)
    db.session.commit()


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{tmp_path / "deal_status.db"}')
    monkeypatch.setenv('SMS_PROVIDER', 'console')
    monkeypatch.setenv('PRIVATE_DOCUMENTS_FOLDER', str(tmp_path / 'private_documents'))
    monkeypatch.delenv('DEMO_LOGIN_ENABLED', raising=False)
    app = create_app()
    app.config['TESTING'] = True
    with app.app_context():
        _approve_kyc(User.query.filter_by(email=SAMPLE_DEALER_EMAIL).one().id)
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


@pytest.fixture
def dealer(sign_in):
    return sign_in()


def make_vehicle(app, **fields) -> int:
    with app.app_context():
        data = {'make': 'Hyundai', 'model': 'Creta', 'year': 2021, 'price': 950000,
                'purchase_price': 800000, 'seller_name': 'Ramesh Patel', 'seller_phone': '9876511122',
                'registration_number': 'GJ 06 AB 1234', 'status': 'available'}
        data.update(fields)
        vehicle = Vehicle(dealer_id=User.query.filter_by(email=SAMPLE_DEALER_EMAIL).one().id, **data)
        db.session.add(vehicle)
        db.session.commit()
        return vehicle.id


def deal_form(vehicle_id, **fields) -> dict:
    data = {'vehicle_id': vehicle_id, 'customer_name': 'Vikram Rao', 'customer_phone': '9811122233',
            'final_price': '1000000', 'status': 'negotiation'}
    data.update(fields)
    return data


def create_deal(app, client, vehicle_id, **fields) -> int:
    client.post('/dealer/deals/add', data=deal_form(vehicle_id, **fields))
    with app.app_context():
        return Deal.query.filter_by(vehicle_id=vehicle_id).order_by(Deal.id.desc()).first().id


def history(app, deal_id) -> list[tuple]:
    with app.app_context():
        return [(h.seq, h.event, h.from_status, h.to_status, h.changed_by_name, h.note)
                for h in DealStatusHistory.query.filter_by(deal_id=deal_id).order_by(DealStatusHistory.seq)]


def deal_statuses(app, deal_id) -> tuple:
    with app.app_context():
        deal = db.session.get(Deal, deal_id)
        return deal.status, deal.document_status


def other_dealer(app, sign_in):
    with app.app_context():
        user_create({'name': 'Other Motors', 'email': 'other@example.com', 'phone': OTHER_DEALER_PHONE,
                     'role': 'dealer', 'password': 'Secret#123'})
        other_id = User.query.filter_by(email='other@example.com').one().id
        _approve_kyc(other_id)
        activate_plan_for_dealer(other_id, 'growth')
    return sign_in(OTHER_DEALER_PHONE)


# ── Purchase → Sale Trail ───────────────────────────────────────────────────
def test_a_new_deal_records_its_first_step(app, dealer):
    vehicle_id = make_vehicle(app)

    deal_id = create_deal(app, dealer, vehicle_id, status='negotiation', status_note='First offer')

    assert history(app, deal_id) == [(1, 'created', None, 'negotiation', 'Rajesh Motors', 'First offer')]
    assert 'Deal created with Negotiation status' in dealer.get('/dealer/deals').get_data(as_text=True)


def test_every_car_status_change_adds_a_step_in_order(app, dealer):
    vehicle_id = make_vehicle(app)
    deal_id = create_deal(app, dealer, vehicle_id)

    dealer.post(f'/dealer/deals/edit/{deal_id}',
                data=deal_form(vehicle_id, status='finalized', status_note='Price agreed'))
    dealer.post(f'/dealer/deals/{deal_id}/status', data={'status': 'delivered'})

    steps = history(app, deal_id)
    assert [step[:4] for step in steps] == [
        (1, 'created', None, 'negotiation'),
        (2, 'changed', 'negotiation', 'finalized'),
        (3, 'changed', 'finalized', 'delivered'),
    ]
    assert steps[1][5] == 'Price agreed'
    with app.app_context():  # one timeline, all on the same vehicle
        assert {h.vehicle_id for h in DealStatusHistory.query.filter_by(deal_id=deal_id)} == {vehicle_id}

    page = dealer.get('/dealer/deals').get_data(as_text=True)
    trail = page[page.index(f'id="history-deal-{deal_id}"'):]
    assert (trail.index('Deal created with Negotiation status')
            < trail.index('Deal status changed from Negotiation to Finalized')
            < trail.index('Deal status changed from Finalized to Delivered'))


def test_history_opens_in_a_popup_from_the_deal_row(app, dealer):
    deal_id = create_deal(app, dealer, make_vehicle(app))

    page = dealer.get('/dealer/deals').get_data(as_text=True)

    row = page[page.index(f'/dealer/deals/edit/{deal_id}'):]  # Edit, then History, then Invoice
    assert row.index(f'data-history-open="history-deal-{deal_id}"') < row.index(f'/dealer/deals/invoice/{deal_id}')
    popup = page[page.index(f'id="history-deal-{deal_id}"'):]
    assert 'Purchase &rarr; Sale Trail' in popup and 'Deal created with Negotiation status' in popup
    assert 'class="card mt-24" id="trail"' not in page  # no longer a section at the bottom
    edit_deal = dealer.get(f'/dealer/deals/edit/{deal_id}').get_data(as_text=True)
    assert f'/dealer/deals#history-deal-{deal_id}' in edit_deal


def test_saving_without_a_status_change_adds_nothing(app, dealer):
    vehicle_id = make_vehicle(app)
    deal_id = create_deal(app, dealer, vehicle_id, status='booked')

    dealer.post(f'/dealer/deals/edit/{deal_id}', data=deal_form(vehicle_id, status='booked', final_price='990000'))
    dealer.post(f'/dealer/deals/{deal_id}/status', data={'status': 'booked'})
    dealer.post(f'/dealer/deals/{deal_id}/status', data={'status': 'Booked'})  # same status, other spelling

    assert len(history(app, deal_id)) == 1


def test_the_trail_still_shows_purchase_sale_and_profit(app, dealer):
    vehicle_id = make_vehicle(app)
    create_deal(app, dealer, vehicle_id, final_price='1000000')

    page = dealer.get('/dealer/deals').get_data(as_text=True)

    assert 'Bought From' in page and 'Ramesh Patel' in page and 'Vikram Rao' in page
    assert '800,000' in page and '200,000' in page  # bought for, profit


def test_existing_deals_get_one_starting_step(app, dealer):
    with app.app_context():
        seeded = Deal.query.filter_by(customer_name='Vikram Singh').one()  # saved before any history
        assert [(h.seq, h.event, h.to_status) for h in seeded.status_history] == [(1, 'baseline', 'delivered')]

        legacy = Deal(dealer_id=seeded.dealer_id, vehicle_id=seeded.vehicle_id,
                      customer_name='Old Deal', final_price=1, status='booked')
        db.session.add(legacy)
        db.session.commit()
        assert start_tracking_existing_deals() == 1
        assert start_tracking_existing_deals() == 0  # never twice
        assert [(h.seq, h.event, h.to_status) for h in legacy.status_history] == [(1, 'baseline', 'booked')]

    assert 'already at Delivered status when status history began' in dealer.get('/dealer/deals').get_data(as_text=True)


def test_history_steps_cannot_be_edited_or_recorded_twice(app, dealer):
    vehicle_id = make_vehicle(app)
    deal_id = create_deal(app, dealer, vehicle_id)

    with app.app_context():
        step = DealStatusHistory.query.filter_by(deal_id=deal_id).one()
        step.to_status = 'delivered'
        with pytest.raises(ValueError, match='append-only'):
            db.session.flush()
        db.session.rollback()

        db.session.add(DealStatusHistory(deal_id=deal_id, vehicle_id=vehicle_id, seq=1,
                                         event='changed', to_status='booked'))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    assert history(app, deal_id) == [(1, 'created', None, 'negotiation', 'Rajesh Motors', None)]


def test_a_simultaneous_change_is_refused_not_duplicated(app, dealer, monkeypatch):
    vehicle_id = make_vehicle(app)
    deal_id = create_deal(app, dealer, vehicle_id)
    # Another save recorded the next step a moment earlier, so this one gets a taken number.
    monkeypatch.setattr('utils.deal_history._next_seq', lambda deal_id: 1)

    page = dealer.post(f'/dealer/deals/{deal_id}/status', data={'status': 'booked'}, follow_redirects=True)

    assert 'changed somewhere else at the same moment' in page.get_data(as_text=True)
    assert deal_statuses(app, deal_id) == ('negotiation', 'pending')  # the change was rolled back too
    assert len(history(app, deal_id)) == 1


def test_deleting_a_dealer_removes_their_deal_history(app):
    with app.app_context():
        owner = User(name='Short Lived Motors', email='short@example.com', role='dealer')
        owner.set_password('Secret#123')
        db.session.add(owner)
        db.session.flush()
        vehicle = Vehicle(dealer_id=owner.id, make='Tata', model='Nexon', year=2022, price=900000)
        db.session.add(vehicle)
        db.session.flush()
        deal_id = deal_create({'dealer_id': owner.id, 'vehicle_id': vehicle.id, 'customer_name': 'A',
                               'final_price': 900000, 'status': 'booked'})
        deal_update(deal_id, {'status': 'delivered'})

        # Enforce foreign keys the way PostgreSQL does (SQLite leaves them off by default).
        db.session.execute(text('PRAGMA foreign_keys = ON'))
        assert db.session.execute(text('PRAGMA foreign_keys')).scalar() == 1
        db.session.delete(owner)
        db.session.commit()

        assert db.session.get(Deal, deal_id) is None
        assert DealStatusHistory.query.filter_by(deal_id=deal_id).count() == 0


# ── Document Status ─────────────────────────────────────────────────────────
def test_document_status_starts_pending_and_is_independent_of_car_status(app, dealer):
    vehicle_id = make_vehicle(app)
    deal_id = create_deal(app, dealer, vehicle_id, status='delivered')
    assert deal_statuses(app, deal_id) == ('delivered', 'pending')

    dealer.post(f'/dealer/deals/{deal_id}/document-status', data={'document_status': 'delivered'})
    assert deal_statuses(app, deal_id) == ('delivered', 'delivered')
    assert len(history(app, deal_id)) == 1  # paperwork is not a Car Status step

    dealer.post(f'/dealer/deals/{deal_id}/status', data={'status': 'cancelled'})
    assert deal_statuses(app, deal_id) == ('cancelled', 'delivered')


def test_deals_table_shows_document_status_next_to_car_status(app, dealer):
    create_deal(app, dealer, make_vehicle(app))

    page = dealer.get('/dealer/deals').get_data(as_text=True)

    assert '<th>Car Status</th><th>Document Status</th>' in page
    assert '<th>Status</th>' not in page
    document_select = re.search(r'<select name="document_status".*?</select>', page, re.S).group(0)
    assert re.findall(r'<option value="([^"]+)"', document_select) == ['pending', 'delivered']


@pytest.mark.parametrize('url, field', [('status', 'status'), ('document-status', 'document_status')])
def test_unknown_statuses_are_refused(app, dealer, url, field):
    deal_id = create_deal(app, dealer, make_vehicle(app))

    page = dealer.post(f'/dealer/deals/{deal_id}/{url}', data={field: 'shipped'}, follow_redirects=True)

    assert 'is not a valid' in page.get_data(as_text=True)
    assert deal_statuses(app, deal_id) == ('negotiation', 'pending')
    assert len(history(app, deal_id)) == 1


def test_status_remark_is_limited_in_length(app, dealer):
    vehicle_id = make_vehicle(app)
    deal_id = create_deal(app, dealer, vehicle_id)

    page = dealer.post(f'/dealer/deals/edit/{deal_id}',
                       data=deal_form(vehicle_id, status='booked', status_note='x' * 501))

    assert 'at most 500 characters' in page.get_data(as_text=True)
    assert deal_statuses(app, deal_id)[0] == 'negotiation'


def test_other_dealers_cannot_change_or_reuse_my_records(app, dealer, sign_in):
    vehicle_id = make_vehicle(app)
    deal_id = create_deal(app, dealer, vehicle_id)
    other = other_dealer(app, sign_in)

    other.post(f'/dealer/deals/{deal_id}/status', data={'status': 'cancelled'})
    other.post(f'/dealer/deals/{deal_id}/document-status', data={'document_status': 'delivered'})
    other.post(f'/dealer/inventory/{vehicle_id}/status', data={'status': 'sold'})
    other.post('/dealer/deals/add', data=deal_form(vehicle_id))  # a deal on someone else's car

    assert deal_statuses(app, deal_id) == ('negotiation', 'pending')
    assert len(history(app, deal_id)) == 1
    with app.app_context():
        assert db.session.get(Vehicle, vehicle_id).status == 'available'
        assert Deal.query.filter_by(vehicle_id=vehicle_id).count() == 1


# ── Status dropdowns ────────────────────────────────────────────────────────
def test_vehicle_status_dropdown_saves_listed_values_only(app, dealer):
    vehicle_id = make_vehicle(app)
    detail = f'/dealer/inventory/{vehicle_id}'

    page = dealer.get(detail).get_data(as_text=True)
    assert f'action="{detail}/status"' in page
    assert '<option value="available" selected>Available</option>' in page

    dealer.post(f'{detail}/status', data={'status': 'reserved', 'next': detail})
    dealer.post(f'{detail}/status', data={'status': 'flying'})
    with app.app_context():
        assert db.session.get(Vehicle, vehicle_id).status == 'reserved'

    # 'next' only ever leads back into the dealer app
    response = dealer.post(f'{detail}/status', data={'status': 'reserved', 'next': 'https://evil.example/'})
    assert response.headers['Location'].endswith(detail) and 'evil' not in response.headers['Location']


def test_a_status_saved_before_the_dropdowns_is_kept(app, dealer):
    vehicle_id = make_vehicle(app, status='In Workshop')
    deal_id = create_deal(app, dealer, vehicle_id)
    with app.app_context():
        db.session.get(Deal, deal_id).status = 'On Hold'
        db.session.commit()

    edit_deal = dealer.get(f'/dealer/deals/edit/{deal_id}').get_data(as_text=True)
    assert '<option value="On Hold" selected>On Hold</option>' in edit_deal
    dealer.post(f'/dealer/deals/edit/{deal_id}', data=deal_form(vehicle_id, status='On Hold'))
    assert deal_statuses(app, deal_id)[0] == 'On Hold'
    assert len(history(app, deal_id)) == 1

    edit_vehicle = dealer.get(f'/dealer/inventory/edit/{vehicle_id}').get_data(as_text=True)
    assert '<option value="In Workshop" selected>In Workshop</option>' in edit_vehicle


@pytest.mark.parametrize('kind, stored, expected', [
    (DEAL, 'Delivered', 'delivered'),
    (DEAL, ' finalised ', 'finalized'),
    (DEAL, 'FINAL', 'finalized'),
    (VEHICLE, 'Trade In', 'trade-in'),
    (VEHICLE, 'trade_in', 'trade-in'),
    (DEAL, 'shipped', None),
    (DEAL, '', None),
])
def test_other_spellings_map_to_the_listed_status(kind, stored, expected):
    assert canonical_status(kind, stored) == expected


def test_clean_status_keeps_the_current_value_and_refuses_anything_else():
    assert clean_status(DEAL, 'Delivered') == 'delivered'
    assert clean_status(DEAL, '', current='booked') == 'booked'
    assert clean_status(DEAL, None) == 'negotiation'
    assert clean_status(DEAL, 'On Hold', current='On Hold') == 'On Hold'
    with pytest.raises(InvalidStatus):
        clean_status(DEAL, 'On Hold', current='booked')
    assert [option.value for option in status_options(DEAL, 'On Hold')] == [
        'On Hold', 'negotiation', 'booked', 'finalized', 'delivered', 'cancelled']
