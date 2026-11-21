"""
test_vehicle_documents.py — documents uploaded with the Add / Edit Vehicle form.

Covers vehicle and customer documents saved with a vehicle, their links to the
right vehicle and customer records, validation, private storage and access
control, failure recovery, editing and removing saved documents on Edit
Vehicle, their display on Vehicle Details, and documents without a vehicle
(Inventory → Other Documents) now that the separate Documents page is gone.

Run from the project root:

    pytest test_vehicle_documents.py
"""
import dataclasses
import io
import os
from datetime import date, timedelta

import pytest
from PIL import Image
from werkzeug.datastructures import FileStorage

import utils.sms
from app import create_app
from db import user_create
from models import Customer, DealerKYC, Document, User, Vehicle, db
from subscription_gate import activate_plan_for_dealer
from utils.vehicle_documents import DocumentEntry, DocumentSaveError, save_documents

SAMPLE_DEALER_EMAIL = 'dealer@caryanams.com'  # created by models.seed_demo_data()
SAMPLE_DEALER_PHONE = '9876543210'
PHOTO_SLOTS = ('img_front', 'img_rear', 'img_right_side', 'img_left_side',
               'img_engine', 'img_boot', 'img_interior')
PDF = b'%PDF-1.4\n% test document\n%%EOF\n'


def png() -> bytes:
    buffer = io.BytesIO()
    Image.new('RGB', (8, 8), 'white').save(buffer, 'PNG')
    return buffer.getvalue()


def upload(content: bytes, name: str):
    return io.BytesIO(content), name


def vehicle_form(**fields) -> dict:
    """A complete Add Vehicle submission; keyword arguments add or override fields."""
    data = {
        'make': 'Hyundai', 'model': 'Creta', 'year': '2021', 'price': '950000',
        'status': 'available', 'fuel_type': 'Petrol', 'transmission': 'Manual',
        'registration_number': 'GJ 06 AB 1234', 'insurance_valid_till': '2027-05-01',
        'seller_name': 'Ramesh Patel', 'seller_phone': '+91 98765 11122',
        'seller_email': 'ramesh@example.com', 'seller_city': 'Ahmedabad',
    }
    for slot in PHOTO_SLOTS:
        data[slot] = upload(png(), f'{slot}.png')
    data.update(fields)
    return data


def rc_book(n=1, **overrides) -> dict:
    fields = {'type': 'RC Book', 'name': 'Original RC', 'number': 'GJ06AB1234',
              'issue': '2021-03-01', 'expiry': '2036-02-28', 'notes': 'Smart card',
              'file': upload(PDF, 'rc.pdf')}
    fields.update(overrides)
    return {f'vdoc-{n}-{key}': value for key, value in fields.items()}


def aadhaar(n=1, **overrides) -> dict:
    fields = {'type': 'Aadhaar Card', 'number': 'XXXX-XXXX-1234', 'file': upload(png(), 'aadhaar.png')}
    fields.update(overrides)
    return {f'cdoc-{n}-{key}': value for key, value in fields.items()}


def _approve_kyc(dealer_id):
    kyc = DealerKYC.query.filter_by(dealer_id=dealer_id).first() or DealerKYC(dealer_id=dealer_id)
    for doc in ('aadhaar_front', 'aadhaar_back', 'pan_card'):
        setattr(kyc, doc, getattr(kyc, doc) or f'{doc}.png')
        setattr(kyc, f'{doc}_status', 'approved')
    db.session.add(kyc)
    db.session.commit()


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{tmp_path / "vehicle_documents.db"}')
    monkeypatch.setenv('SMS_PROVIDER', 'console')
    monkeypatch.setenv('PRIVATE_DOCUMENTS_FOLDER', str(tmp_path / 'private_documents'))
    monkeypatch.delenv('DEMO_LOGIN_ENABLED', raising=False)
    app = create_app()
    app.config['TESTING'] = True
    # Vehicle photos and Documents-page uploads go to UPLOAD_FOLDER; keep them out of the project.
    app.config['UPLOAD_FOLDER'] = str(tmp_path / 'uploads')
    os.makedirs(app.config['UPLOAD_FOLDER'])
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


def add_vehicle(client, **fields):
    return client.post('/dealer/inventory/add', data=vehicle_form(**fields),
                       content_type='multipart/form-data')


def saved_vehicle(app, registration='GJ 06 AB 1234'):
    with app.app_context():
        vehicle = Vehicle.query.filter_by(registration_number=registration).order_by(Vehicle.id.desc()).first()
        return vehicle and {'id': vehicle.id, 'customer_id': vehicle.customer_id}


def documents(app, **filters) -> list[Document]:
    with app.app_context():
        rows = Document.query.filter_by(**filters).order_by(Document.id).all()
        db.session.expunge_all()
        return rows


# ── Saving with a new vehicle ───────────────────────────────────────────────
def test_vehicle_and_customer_documents_are_saved_and_linked(app, dealer):
    response = add_vehicle(dealer, **rc_book(1), **rc_book(2, type='Insurance', name='Policy 2026',
                                                            file=upload(png(), 'policy.png')),
                           **aadhaar(1), **aadhaar(2, type='PAN Card', number='ABCDE1234F',
                                                    file=upload(PDF, 'pan.pdf')))

    assert response.headers['Location'].endswith('/dealer/inventory')
    vehicle = saved_vehicle(app)
    with app.app_context():
        customer = db.session.get(Customer, vehicle['customer_id'])
        assert (customer.name, customer.phone, customer.city) == ('Ramesh Patel', '9876511122', 'Ahmedabad')

    rc, policy = documents(app, vehicle_id=vehicle['id'])
    assert (rc.doc_type, rc.doc_name, rc.doc_number, rc.notes) == ('RC Book', 'Original RC', 'GJ06AB1234', 'Smart card')
    assert (rc.issue_date, rc.expiry_date) == (date(2021, 3, 1), date(2036, 2, 28))
    assert (policy.doc_type, policy.original_name) == ('Insurance', 'policy.png')
    assert {rc.customer_id, policy.customer_id} == {None}

    id_proof, pan = documents(app, customer_id=vehicle['customer_id'])
    assert (id_proof.doc_type, pan.doc_type) == ('Aadhaar Card', 'PAN Card')
    assert {id_proof.vehicle_id, pan.vehicle_id} == {None}  # they belong to the person


def test_files_are_stored_privately_not_under_static(app, dealer):
    add_vehicle(dealer, **rc_book(1))
    [doc] = documents(app)

    private_folder = app.config['PRIVATE_DOCUMENTS_FOLDER']
    assert doc.storage == 'private'
    assert os.path.isfile(os.path.join(private_folder, doc.filename))
    assert not os.path.abspath(private_folder).startswith(os.path.abspath(app.static_folder))


def test_the_same_customer_is_reused_across_vehicles(app, dealer):
    add_vehicle(dealer, **aadhaar(1))
    add_vehicle(dealer, registration_number='GJ 06 CD 5678', seller_phone='9876511122',
                **aadhaar(1, type='Address Proof', file=upload(PDF, 'bill.pdf')))

    first, second = saved_vehicle(app), saved_vehicle(app, 'GJ 06 CD 5678')
    assert first['customer_id'] == second['customer_id']
    with app.app_context():
        assert Customer.query.count() == 1
    assert len(documents(app, customer_id=first['customer_id'])) == 2


def test_vehicle_without_documents_still_saves(app, dealer):
    response = add_vehicle(dealer, **{'vdoc-1-type': '', 'vdoc-1-name': ''})  # an untouched entry

    assert response.headers['Location'].endswith('/dealer/inventory')
    assert saved_vehicle(app) is not None
    assert documents(app) == []


# ── Validation: nothing is saved when an entry is wrong ─────────────────────
@pytest.mark.parametrize('fields, message', [
    (rc_book(1, file=upload(b'MZ\x90\x00', 'rc.exe')), 'only PDF, JPG and PNG files can be uploaded.'),
    (rc_book(1, file=upload(b'<html>not a pdf</html>', 'rc.pdf')), 'the file is not a valid PDF file.'),
    (rc_book(1, file=upload(b'', 'rc.pdf')), 'the file is empty.'),
    ({'vdoc-1-type': 'RC Book', 'vdoc-1-number': 'GJ06'}, 'choose a file to upload.'),
    (rc_book(1, type='Passport'), 'choose a document type.'),
    (rc_book(1, issue='2030-01-01', expiry='2029-01-01'), 'the expiry date is before the issue date.'),
    ({**aadhaar(1), 'seller_name': '', 'seller_phone': ''}, 'Customer documents need a customer'),
])
def test_invalid_documents_are_rejected_before_anything_is_saved(app, dealer, fields, message):
    page = add_vehicle(dealer, **fields)

    assert page.status_code == 200  # the form is shown again with the error
    assert message in page.get_data(as_text=True).replace('&#39;', "'")
    assert saved_vehicle(app) is None
    assert documents(app) == []


def test_oversized_file_is_rejected(app, dealer, monkeypatch):
    monkeypatch.setattr('utils.vehicle_documents.MAX_DOCUMENT_BYTES', len(PDF) - 1)

    page = add_vehicle(dealer, **rc_book(1)).get_data(as_text=True)

    assert 'the file is larger than' in page
    assert saved_vehicle(app) is None


# ── Failure recovery ────────────────────────────────────────────────────────
def test_storage_failure_keeps_one_vehicle_and_offers_a_retry(app, dealer, tmp_path):
    app.config['PRIVATE_DOCUMENTS_FOLDER'] = str(tmp_path / 'missing' / 'folder')  # writes fail

    response = add_vehicle(dealer, **rc_book(1), **aadhaar(1))

    vehicle = saved_vehicle(app)
    assert response.headers['Location'].endswith(f"/dealer/inventory/edit/{vehicle['id']}")
    assert documents(app) == []  # nothing half-saved
    with app.app_context():
        assert Vehicle.query.filter_by(registration_number='GJ 06 AB 1234').count() == 1

    # Retry from the edit page: documents attach to the same vehicle, no duplicate is created.
    app.config['PRIVATE_DOCUMENTS_FOLDER'] = str(tmp_path / 'private_documents')
    edit_form = vehicle_form(**rc_book(1))
    dealer.post(f"/dealer/inventory/edit/{vehicle['id']}", data=edit_form,
                content_type='multipart/form-data')
    assert [doc.vehicle_id for doc in documents(app)] == [vehicle['id']]


def test_a_failed_save_leaves_no_files_or_records(app):
    class BrokenUpload:
        filename = 'broken.pdf'

        def save(self, path):
            raise OSError('disk full')

    written_first = DocumentEntry(party='vehicle', doc_type='RC Book', name='', number='',
                                  issue_date=None, expiry_date=None, notes='',
                                  file=FileStorage(io.BytesIO(PDF), 'rc.pdf'), extension='pdf')
    fails_second = dataclasses.replace(written_first, file=BrokenUpload())
    with app.app_context():
        dealer_id = User.query.filter_by(email=SAMPLE_DEALER_EMAIL).one().id
        with pytest.raises(DocumentSaveError):
            save_documents(dealer_id=dealer_id, vehicle_id=None, customer=None,
                           entries=[written_first, fails_second])
        assert Document.query.count() == 0
    assert os.listdir(app.config['PRIVATE_DOCUMENTS_FOLDER']) == []  # the first file was removed


# ── Edit Vehicle ────────────────────────────────────────────────────────────
def test_edit_vehicle_lists_saved_documents_and_adds_more(app, dealer):
    add_vehicle(dealer, seller_phone='', **rc_book(1), **aadhaar(1))  # a customer with no phone
    vehicle = saved_vehicle(app)

    page = dealer.get(f"/dealer/inventory/edit/{vehicle['id']}").get_data(as_text=True)
    assert 'Original RC' in page and 'Aadhaar Card' in page

    dealer.post(f"/dealer/inventory/edit/{vehicle['id']}",
                data=vehicle_form(seller_phone='', **rc_book(1, type='PUC Certificate', name='PUC 2026')),
                content_type='multipart/form-data')
    assert [doc.doc_type for doc in documents(app, vehicle_id=vehicle['id'])] == ['RC Book', 'PUC Certificate']
    assert saved_vehicle(app)['customer_id'] == vehicle['customer_id']  # same customer kept
    with app.app_context():
        assert Customer.query.count() == 1


# ── Access control ──────────────────────────────────────────────────────────
def test_only_the_owning_dealer_can_open_a_document(app, dealer, sign_in):
    add_vehicle(dealer, **aadhaar(1))
    [doc] = documents(app)

    response = dealer.get(f'/dealer/documents/{doc.id}/file')
    assert response.status_code == 200 and response.mimetype == 'image/png'
    assert response.headers['Cache-Control'] == 'private, no-store'
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    download = dealer.get(f'/dealer/documents/{doc.id}/file?download=1')
    assert download.headers['Content-Disposition'].startswith('attachment')

    assert app.test_client().get(f'/dealer/documents/{doc.id}/file').status_code == 302  # signed out

    with app.app_context():
        user_create({'name': 'Other Motors', 'email': 'other@example.com', 'phone': '9811100001',
                     'role': 'dealer', 'password': 'Secret#123'})
        other_id = User.query.filter_by(email='other@example.com').one().id
        _approve_kyc(other_id)
        activate_plan_for_dealer(other_id, 'growth')
    assert sign_in('9811100001').get(f'/dealer/documents/{doc.id}/file').status_code == 404


def test_documents_survive_signing_out_and_back_in(app, dealer, sign_in, monkeypatch):
    add_vehicle(dealer, **rc_book(1))
    [doc] = documents(app)
    dealer.get('/auth/logout?as=dealer')
    assert dealer.get(f'/dealer/documents/{doc.id}/file').status_code == 302

    monkeypatch.setattr('utils.otp.RESEND_AFTER', timedelta(0))  # sign straight back in
    assert sign_in().get(f'/dealer/documents/{doc.id}/file').data == PDF


# ── Vehicle Details ─────────────────────────────────────────────────────────
def test_vehicle_details_shows_its_documents(app, dealer):
    add_vehicle(dealer, **rc_book(1, expiry='2020-01-01', issue='2015-01-01'), **aadhaar(1))
    vehicle = saved_vehicle(app)
    [rc] = documents(app, vehicle_id=vehicle['id'])
    [id_proof] = documents(app, customer_id=vehicle['customer_id'])

    page = dealer.get(f"/dealer/inventory/{vehicle['id']}").get_data(as_text=True)

    assert 'Original RC' in page and 'No. GJ06AB1234' in page and 'Smart card' in page
    assert 'Expired' in page
    assert 'Customer / Owner Documents' in page and 'Aadhaar Card' in page
    for doc in (rc, id_proof):  # preview and download go through the checked route
        assert f'/dealer/documents/{doc.id}/file"' in page
        assert f'/dealer/documents/{doc.id}/file?download=1' in page
    assert 'Condition Details' not in page


# ── Edit Vehicle: saved documents ───────────────────────────────────────────
def test_editing_a_saved_document_updates_it_and_replaces_its_file(app, dealer):
    add_vehicle(dealer, **rc_book(1))
    vehicle = saved_vehicle(app)
    [old] = documents(app)
    old_path = os.path.join(app.config['PRIVATE_DOCUMENTS_FOLDER'], old.filename)
    edits = {'type': 'Insurance', 'name': 'Policy 2027', 'number': 'POL-9', 'issue': '2026-01-01',
             'expiry': '2027-01-01', 'notes': '', 'file': upload(png(), 'policy.png')}

    dealer.post(f"/dealer/inventory/edit/{vehicle['id']}", content_type='multipart/form-data',
                data=vehicle_form(**{f'sdoc-{old.id}-{key}': value for key, value in edits.items()}))

    [doc] = documents(app)
    assert (doc.id, doc.vehicle_id) == (old.id, vehicle['id'])
    assert (doc.doc_type, doc.doc_name, doc.doc_number, doc.notes) == ('Insurance', 'Policy 2027', 'POL-9', None)
    assert (doc.issue_date, doc.expiry_date) == (date(2026, 1, 1), date(2027, 1, 1))
    assert (doc.original_name, doc.storage) == ('policy.png', 'private')
    assert not os.path.exists(old_path)
    assert dealer.get(f'/dealer/documents/{doc.id}/file').mimetype == 'image/png'
    page = dealer.get(f"/dealer/inventory/{vehicle['id']}").get_data(as_text=True)
    assert 'Policy 2027' in page and 'Original RC' not in page


def test_removing_a_saved_document_deletes_it_and_its_file(app, dealer):
    add_vehicle(dealer, **rc_book(1), **rc_book(2, type='Insurance', name='Policy'))
    vehicle = saved_vehicle(app)
    rc, policy = documents(app)
    path = os.path.join(app.config['PRIVATE_DOCUMENTS_FOLDER'], rc.filename)

    dealer.post(f"/dealer/inventory/edit/{vehicle['id']}", content_type='multipart/form-data',
                data=vehicle_form(**{f'sdoc-{rc.id}-remove': '1'}))

    assert [doc.id for doc in documents(app)] == [policy.id]
    assert not os.path.exists(path)
    assert 'Original RC' not in dealer.get(f"/dealer/inventory/{vehicle['id']}").get_data(as_text=True)


def test_saved_document_edits_are_checked_before_anything_is_saved(app, dealer):
    add_vehicle(dealer, **rc_book(1))
    vehicle = saved_vehicle(app)
    [doc] = documents(app)
    edits = {'type': 'RC Book', 'issue': '2030-01-01', 'expiry': '2029-01-01',
             'file': upload(b'not a pdf', 'rc.pdf')}

    page = dealer.post(f"/dealer/inventory/edit/{vehicle['id']}", content_type='multipart/form-data',
                       data=vehicle_form(make='Kia', **{f'sdoc-{doc.id}-{key}': value
                                                        for key, value in edits.items()}))

    text = page.get_data(as_text=True)
    assert page.status_code == 200
    assert 'the expiry date is before the issue date.' in text
    assert 'the file is not a valid PDF file.' in text
    [unchanged] = documents(app)
    assert (unchanged.issue_date, unchanged.filename) == (doc.issue_date, doc.filename)
    with app.app_context():
        assert db.session.get(Vehicle, vehicle['id']).make == 'Hyundai'  # nothing was saved


def test_another_vehicles_document_cannot_be_changed_from_this_form(app, dealer):
    add_vehicle(dealer, **rc_book(1))
    add_vehicle(dealer, registration_number='GJ 06 CD 5678')
    second = saved_vehicle(app, 'GJ 06 CD 5678')
    [doc] = documents(app)

    page = dealer.post(f"/dealer/inventory/edit/{second['id']}", content_type='multipart/form-data',
                       data=vehicle_form(registration_number='GJ 06 CD 5678',
                                         **{f'sdoc-{doc.id}-remove': '1'}))

    assert 'could not be found on this vehicle' in page.get_data(as_text=True)
    assert [d.id for d in documents(app)] == [doc.id]


def test_deleting_a_vehicle_keeps_its_documents(app, dealer):
    add_vehicle(dealer, **rc_book(1))
    vehicle = saved_vehicle(app)

    dealer.post(f"/dealer/inventory/delete/{vehicle['id']}")

    assert saved_vehicle(app) is None
    [doc] = documents(app)
    assert doc.vehicle_id is None and doc.doc_type == 'RC Book'
    inventory = dealer.get('/dealer/inventory').get_data(as_text=True)
    assert 'Other Documents' in inventory and f'/dealer/documents/{doc.id}/file' in inventory


# ── The Documents page is gone; documents without a vehicle stay reachable ──
def _legacy_document(app, filename='legacy-noc.pdf') -> int:
    """A document as the old Documents page saved it: public upload folder, no vehicle."""
    with open(os.path.join(app.config['UPLOAD_FOLDER'], filename), 'wb') as legacy_file:
        legacy_file.write(PDF)
    with app.app_context():
        dealer_id = User.query.filter_by(email=SAMPLE_DEALER_EMAIL).one().id
        doc = Document(dealer_id=dealer_id, doc_type='NOC', customer_name='Walk-in',
                       filename=filename, original_name='noc.pdf')
        db.session.add(doc)
        db.session.commit()
        return doc.id


def test_documents_page_and_its_link_are_gone(app, dealer):
    assert dealer.get('/dealer/documents').status_code == 404
    assert dealer.post('/dealer/documents/upload').status_code == 404
    page = dealer.get('/dealer/inventory').get_data(as_text=True)
    assert 'href="/dealer/documents"' not in page and '<span>Documents</span>' not in page


def test_documents_without_a_vehicle_are_listed_and_can_be_attached(app, dealer):
    doc_id = _legacy_document(app)
    add_vehicle(dealer)
    vehicle = saved_vehicle(app)

    inventory = dealer.get('/dealer/inventory').get_data(as_text=True)
    assert 'Other Documents' in inventory and 'Walk-in' in inventory
    assert dealer.get(f'/dealer/documents/{doc_id}/file').data == PDF

    response = dealer.post(f'/dealer/inventory/documents/{doc_id}/attach', data={'vehicle_id': vehicle['id']})

    assert response.headers['Location'].endswith(f"/dealer/inventory/{vehicle['id']}#documents")
    assert [doc.vehicle_id for doc in documents(app)] == [vehicle['id']]
    assert 'NOC' in dealer.get(f"/dealer/inventory/{vehicle['id']}").get_data(as_text=True)
    assert 'Other Documents' not in dealer.get('/dealer/inventory').get_data(as_text=True)


def test_a_document_without_a_vehicle_can_be_deleted_from_inventory(app, dealer):
    doc_id = _legacy_document(app)
    add_vehicle(dealer, **rc_book(1))
    [_, rc] = documents(app)

    dealer.post(f'/dealer/inventory/documents/{doc_id}/delete')
    dealer.post(f'/dealer/inventory/documents/{rc.id}/delete')  # linked: managed on Edit Vehicle only

    assert [doc.id for doc in documents(app)] == [rc.id]
    assert not os.path.exists(os.path.join(app.config['UPLOAD_FOLDER'], 'legacy-noc.pdf'))
