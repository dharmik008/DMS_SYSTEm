"""
utils/vehicle_documents.py — documents uploaded with the Add / Edit Vehicle form.

The Registration & Documents card posts two repeatable lists with the form:

  vdoc-<n>-…  vehicle documents (RC book, insurance, PUC…), linked to the vehicle
  cdoc-<n>-…  customer / owner documents (Aadhaar, PAN…), linked to the customer
              record rather than to the vehicle, so they stay with that person

Each entry has a type, name, number, issue and expiry dates, notes and a file.
On Edit Vehicle the documents already saved are listed too, and Edit / Remove
stage a change that is saved with the form:

  sdoc-<id>-…       new details and/or a replacement file
  sdoc-<id>-remove  delete the document and its file

Everything is validated before anything is written. Files go to the private
PRIVATE_DOCUMENTS_FOLDER (never under /static) and are served only through the
owner-checked route in dealer/routes.py.
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime

from flask import current_app
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from werkzeug.datastructures import FileStorage

from db import documents_get_by_dealer
from extensions import db
from models import Customer, Document, Vehicle
from utils.otp import normalise_phone

log = logging.getLogger(__name__)

VEHICLE_DOC_TYPES = ('RC Book', 'Insurance', 'PUC Certificate', 'Fitness Certificate',
                     'Permit', 'Tax Receipt', 'NOC', 'Other')
CUSTOMER_DOC_TYPES = ('Aadhaar Card', 'PAN Card', 'Other Identity Proof', 'Address Proof',
                      'Authorization Letter', 'Other')

# The first bytes of each allowed format, so a renamed file of another kind is refused.
FILE_SIGNATURES = {
    'pdf': (b'%PDF-',),
    'png': (b'\x89PNG\r\n\x1a\n',),
    'jpg': (b'\xff\xd8\xff',),
    'jpeg': (b'\xff\xd8\xff',),
}
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
MAX_DOCUMENTS_PER_SAVE = 20
MAX_NAME_LENGTH = 150    # Document.doc_name
MAX_NUMBER_LENGTH = 100  # Document.doc_number
MAX_NOTES_LENGTH = 1000
PRIVATE_STORAGE = 'private'

_FIELD = re.compile(r'^(vdoc|cdoc)-(\d{1,4})-(type|name|number|issue|expiry|notes|file)$')
_TEXT_FIELDS = ('type', 'name', 'number', 'issue', 'expiry', 'notes')
_SECTIONS = {
    'vdoc': ('vehicle', 'Vehicle document', VEHICLE_DOC_TYPES),
    'cdoc': ('customer', 'Customer document', CUSTOMER_DOC_TYPES),
}


class DocumentSaveError(Exception):
    """The documents could not be stored; nothing from the batch was kept."""


@dataclass
class DocumentEntry:
    party: str  # 'vehicle' or 'customer'
    doc_type: str
    name: str
    number: str
    issue_date: date | None
    expiry_date: date | None
    notes: str
    file: FileStorage
    extension: str


# ── Validation ──────────────────────────────────────────────────────────────
def validate_document_submission(form, files) -> tuple[list[DocumentEntry], list[str]]:
    """
    All document entries on the vehicle form, checked. Returns (entries, errors);
    if there are errors nothing should be saved. Rows left empty are ignored.
    """
    entries, errors = _parse_entries(form, files)
    if any(entry.party == 'customer' for entry in entries) and not _has_customer(form):
        errors.append("Customer documents need a customer: add the customer's name or "
                      "phone in Customer Details.")
    return entries, errors


def _parse_entries(form, files) -> tuple[list[DocumentEntry], list[str]]:
    rows: dict[tuple[str, int], dict] = {}
    for key in form:
        match = _FIELD.match(key)
        if match and match.group(3) != 'file':
            rows.setdefault((match.group(1), int(match.group(2))), {})[match.group(3)] = \
                (form.get(key) or '').strip()
    for key in files:
        match = _FIELD.match(key)
        if match and match.group(3) == 'file':
            rows.setdefault((match.group(1), int(match.group(2))), {})['file'] = files.get(key)

    entries, errors = [], []
    counts = {'vdoc': 0, 'cdoc': 0}
    for prefix, index in sorted(rows, key=lambda row: (row[0] != 'vdoc', row[1])):
        fields = rows[(prefix, index)]
        upload = fields.get('file')
        has_file = upload is not None and bool(upload.filename)
        if not has_file and not any(fields.get(name) for name in _TEXT_FIELDS):
            continue
        counts[prefix] += 1
        party, label, allowed_types = _SECTIONS[prefix]
        problems, entry = _check_entry(fields, upload if has_file else None, party, allowed_types)
        errors.extend(f'{label} {counts[prefix]}: {problem}' for problem in problems)
        if entry is not None:
            entries.append(entry)

    if sum(counts.values()) > MAX_DOCUMENTS_PER_SAVE:
        errors.append(f'You can attach up to {MAX_DOCUMENTS_PER_SAVE} documents at a time.')
    return entries, errors


def _check_entry(fields, upload, party, allowed_types) -> tuple[list[str], DocumentEntry | None]:
    problems = []
    doc_type = fields.get('type', '')
    if doc_type not in allowed_types:
        problems.append('choose a document type.')

    extension = None
    if upload is None:
        problems.append('choose a file to upload.')
    else:
        extension, problem = _check_file(upload)
        if problem:
            problems.append(problem)

    detail_problems, issue_date, expiry_date = _check_details(fields)
    problems.extend(detail_problems)

    if problems:
        return problems, None
    return [], DocumentEntry(party=party, doc_type=doc_type, name=fields.get('name', ''),
                             number=fields.get('number', ''), issue_date=issue_date,
                             expiry_date=expiry_date, notes=fields.get('notes', ''),
                             file=upload, extension=extension)


def _check_details(fields) -> tuple[list[str], date | None, date | None]:
    """Dates and text lengths of one document: (problems, issue_date, expiry_date)."""
    problems = []
    issue_date = expiry_date = None
    try:
        issue_date = _parse_date(fields.get('issue', ''))
        expiry_date = _parse_date(fields.get('expiry', ''))
    except ValueError:
        problems.append('a date is not valid.')
    if issue_date and expiry_date and expiry_date < issue_date:
        problems.append('the expiry date is before the issue date.')

    if len(fields.get('name', '')) > MAX_NAME_LENGTH:
        problems.append(f'the document name is longer than {MAX_NAME_LENGTH} characters.')
    if len(fields.get('number', '')) > MAX_NUMBER_LENGTH:
        problems.append(f'the document number is longer than {MAX_NUMBER_LENGTH} characters.')
    if len(fields.get('notes', '')) > MAX_NOTES_LENGTH:
        problems.append(f'the notes are longer than {MAX_NOTES_LENGTH} characters.')
    return problems, issue_date, expiry_date


def _check_file(upload: FileStorage) -> tuple[str | None, str | None]:
    """(extension, None) for an acceptable file, otherwise (None, reason)."""
    extension = upload.filename.rsplit('.', 1)[-1].lower() if '.' in upload.filename else ''
    if extension not in FILE_SIGNATURES:
        return None, 'only PDF, JPG and PNG files can be uploaded.'
    stream = upload.stream
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(0)
    if size == 0:
        return None, 'the file is empty.'
    if size > MAX_DOCUMENT_BYTES:
        return None, f'the file is larger than {MAX_DOCUMENT_BYTES // (1024 * 1024)} MB.'
    head = stream.read(16)
    stream.seek(0)
    if not head.startswith(FILE_SIGNATURES[extension]):
        return None, f'the file is not a valid {extension.upper()} file.'
    return extension, None


def _parse_date(value: str) -> date | None:
    return datetime.strptime(value, '%Y-%m-%d').date() if value else None


def _has_customer(form) -> bool:
    return bool((form.get('seller_name') or '').strip()) or normalise_phone(form.get('seller_phone')) is not None


# ── Customer records ────────────────────────────────────────────────────────
def link_customer(dealer_id: int, form, current_id: int | None = None) -> Customer | None:
    """
    The customer record for the form's Customer Details, found or created.

    The same person is recognised by phone. Without a phone, the vehicle's
    current customer is kept (when editing) or a new record is made; names
    alone are never matched. Existing details are only filled in, never
    overwritten. Returns None when neither a name nor a valid phone is given.
    """
    name = (form.get('seller_name') or '').strip()[:120] or None
    phone = normalise_phone(form.get('seller_phone'))
    if name is None and phone is None:
        return None
    details = {
        'name': name,
        'email': (form.get('seller_email') or '').strip().lower()[:120] or None,
        'city': (form.get('seller_city') or '').strip()[:80] or None,
        'address': (form.get('seller_address') or '').strip()[:255] or None,
    }

    current = (Customer.query.filter_by(id=current_id, dealer_id=dealer_id).first()
               if current_id else None)
    if current is not None and current.phone == phone:
        customer = current
    elif phone is not None:
        customer = Customer.query.filter_by(dealer_id=dealer_id, phone=phone).first()
    else:
        customer = None

    if customer is not None:
        for field, value in details.items():
            if value and not getattr(customer, field):
                setattr(customer, field, value)
        db.session.commit()
        return customer

    customer = Customer(dealer_id=dealer_id, phone=phone, **details)
    db.session.add(customer)
    try:
        db.session.commit()
    except IntegrityError:
        # Another request created this phone's record a moment ago.
        db.session.rollback()
        customer = Customer.query.filter_by(dealer_id=dealer_id, phone=phone).first()
        if customer is None:
            raise
    return customer


def set_vehicle_customer(vehicle_id: int, customer: Customer | None) -> None:
    db.session.query(Vehicle).filter_by(id=vehicle_id).update(
        {'customer_id': customer.id if customer is not None else None})
    db.session.commit()


# ── Storage ─────────────────────────────────────────────────────────────────
def document_folder(storage: str | None) -> str:
    """Where a document's file lives: the private folder, or the older public upload folder."""
    key = 'PRIVATE_DOCUMENTS_FOLDER' if storage == PRIVATE_STORAGE else 'UPLOAD_FOLDER'
    return current_app.config[key]


def save_documents(*, dealer_id: int, vehicle_id: int | None, customer: Customer | None,
                   entries: list[DocumentEntry], owner_name: str = '') -> int:
    """
    Store the files privately and record them, all or nothing.

    Returns how many were saved. On any failure the database is rolled back,
    files already written are removed, and DocumentSaveError is raised.
    """
    if not entries:
        return 0
    if customer is None and any(entry.party == 'customer' for entry in entries):
        raise ValueError('customer documents need a customer record')

    folder = current_app.config['PRIVATE_DOCUMENTS_FOLDER']
    written = []
    try:
        for entry in entries:
            stored_name = f'{uuid.uuid4().hex}.{entry.extension}'
            entry.file.save(os.path.join(folder, stored_name))
            written.append(stored_name)
            is_customer_doc = entry.party == 'customer'
            db.session.add(Document(
                dealer_id=dealer_id,
                vehicle_id=None if is_customer_doc else vehicle_id,
                customer_id=customer.id if is_customer_doc else None,
                customer_name=(customer.display_name if is_customer_doc else owner_name) or None,
                doc_type=entry.doc_type,
                doc_name=entry.name or None,
                doc_number=entry.number or None,
                issue_date=entry.issue_date,
                expiry_date=entry.expiry_date,
                notes=entry.notes or None,
                filename=stored_name,
                original_name=_original_name(entry.file),
                storage=PRIVATE_STORAGE,
            ))
        db.session.commit()
    except (OSError, SQLAlchemyError) as exc:
        db.session.rollback()
        for stored_name in written:
            remove_file(os.path.join(folder, stored_name))
        log.exception('Documents for dealer %s (vehicle %s) could not be saved', dealer_id, vehicle_id)
        raise DocumentSaveError('The documents could not be saved.') from exc
    return len(entries)


def _original_name(upload) -> str:
    return re.split(r'[\\/]', upload.filename)[-1][:255]


def remove_file(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        log.warning('Could not remove document file %s', path, exc_info=True)


def saved_documents(dealer_id: int, vehicle: dict) -> dict:
    """Documents already stored for a vehicle and its customer, for the edit form."""
    documents = documents_get_by_dealer(dealer_id)  # hides files an admin removed
    customer_id = vehicle.get('customer_id')
    return {
        'vehicle_documents': [doc for doc in documents
                              if doc['vehicle_id'] == vehicle['id'] and not doc['customer_id']],
        'customer_documents': [doc for doc in documents
                               if customer_id and doc['customer_id'] == customer_id],
    }


# ── Saved documents: edit, replace, remove (Edit Vehicle) ───────────────────
_SAVED_FIELD = re.compile(r'^sdoc-(\d{1,10})-(type|name|number|issue|expiry|notes|file|remove)$')


@dataclass
class SavedDocumentChange:
    """A change to an already saved document, staged on the Edit Vehicle form."""
    document_id: int
    remove: bool = False
    details: dict | None = None      # Document column -> new value
    file: FileStorage | None = None  # replacement file
    extension: str | None = None


@dataclass
class SavedChangeCounts:
    updated: int = 0
    replaced: int = 0
    removed: int = 0

    @property
    def total(self) -> int:
        return self.updated + self.replaced + self.removed

    def summary(self) -> str:
        parts = [(self.updated, 'document(s) updated'), (self.replaced, 'file(s) replaced'),
                 (self.removed, 'document(s) removed')]
        return ', '.join(f'{count} {what}' for count, what in parts if count)


def validate_saved_document_changes(form, files, saved: dict) -> tuple[list[SavedDocumentChange], list[str]]:
    """
    Edits and removals of the documents listed on the Edit Vehicle form.

    saved is saved_documents() for that vehicle. Only those documents can be
    changed: an id for anything else is refused, so a crafted form cannot reach
    another vehicle's or another dealer's files. Returns (changes, errors).
    """
    editable = {doc['id']: ('vehicle', doc) for doc in saved.get('vehicle_documents', [])}
    editable.update({doc['id']: ('customer', doc) for doc in saved.get('customer_documents', [])})

    posted: dict[int, dict] = {}
    for key in form:
        match = _SAVED_FIELD.match(key)
        if match and match.group(2) != 'file':
            posted.setdefault(int(match.group(1)), {})[match.group(2)] = (form.get(key) or '').strip()
    for key in files:
        match = _SAVED_FIELD.match(key)
        upload = files.get(key)
        if match and match.group(2) == 'file' and upload is not None and upload.filename:
            posted.setdefault(int(match.group(1)), {})['file'] = upload

    changes, errors = [], []
    for doc_id in sorted(posted):
        fields = posted[doc_id]
        if doc_id not in editable:
            errors.append('A saved document could not be found on this vehicle. '
                          'Reload the page and try again.')
            continue
        party, doc = editable[doc_id]
        if fields.get('remove') == '1':
            changes.append(SavedDocumentChange(document_id=doc_id, remove=True))
            continue
        problems, change = _check_saved_change(doc_id, doc, party, fields)
        label = doc['doc_name'] or doc['original_name'] or doc['doc_type'] or f'#{doc_id}'
        errors.extend(f'Saved document "{label}": {problem}' for problem in problems)
        if change is not None:
            changes.append(change)
    return changes, errors


def _check_saved_change(doc_id, doc, party, fields) -> tuple[list[str], SavedDocumentChange | None]:
    problems, details = [], None
    if any(name in fields for name in _TEXT_FIELDS):
        allowed_types = VEHICLE_DOC_TYPES if party == 'vehicle' else CUSTOMER_DOC_TYPES
        doc_type = fields.get('type', '')
        # Types from the old Documents page ('RC (Registration Certificate)', …)
        # are not in today's lists, but a document may keep the one it has.
        if doc_type not in allowed_types and doc_type != (doc['doc_type'] or ''):
            problems.append('choose a document type.')
        detail_problems, issue_date, expiry_date = _check_details(fields)
        problems.extend(detail_problems)
        details = {'doc_type': doc_type or None, 'doc_name': fields.get('name') or None,
                   'doc_number': fields.get('number') or None, 'issue_date': issue_date,
                   'expiry_date': expiry_date, 'notes': fields.get('notes') or None}

    upload, extension = fields.get('file'), None
    if upload is not None:
        extension, problem = _check_file(upload)
        if problem:
            problems.append(problem)

    if problems:
        return problems, None
    if details is None and upload is None:
        return [], None
    return [], SavedDocumentChange(document_id=doc_id, details=details, file=upload, extension=extension)


def apply_saved_document_changes(dealer_id: int, changes: list[SavedDocumentChange]) -> SavedChangeCounts:
    """
    Apply checked edits, file replacements and removals, all or nothing.

    Replacement files are written to private storage before the commit; the
    files they replace, and those of removed documents, are deleted only after
    it, so a failure never leaves a record pointing at a missing file. On
    failure everything is rolled back and DocumentSaveError is raised.
    """
    counts = SavedChangeCounts()
    if not changes:
        return counts

    folder = current_app.config['PRIVATE_DOCUMENTS_FOLDER']
    written, obsolete = [], []
    try:
        for change in changes:
            record = Document.query.filter_by(id=change.document_id, dealer_id=dealer_id).first()
            if record is None:
                continue  # removed in the meantime
            old_path = os.path.join(document_folder(record.storage), record.filename)
            if change.remove:
                db.session.delete(record)
                obsolete.append(old_path)
                counts.removed += 1
                continue
            if change.details and any(getattr(record, column) != value
                                      for column, value in change.details.items()):
                for column, value in change.details.items():
                    setattr(record, column, value)
                counts.updated += 1
            if change.file is not None:
                stored_name = f'{uuid.uuid4().hex}.{change.extension}'
                change.file.save(os.path.join(folder, stored_name))
                written.append(stored_name)
                record.filename = stored_name
                record.original_name = _original_name(change.file)
                record.storage = PRIVATE_STORAGE
                obsolete.append(old_path)
                counts.replaced += 1
        db.session.commit()
    except (OSError, SQLAlchemyError) as exc:
        db.session.rollback()
        for stored_name in written:
            remove_file(os.path.join(folder, stored_name))
        log.exception('Document changes for dealer %s could not be saved', dealer_id)
        raise DocumentSaveError('The document changes could not be saved.') from exc

    for path in obsolete:
        remove_file(path)
    return counts


# ── Documents no vehicle page shows (Inventory → Other Documents) ───────────
def unlinked_documents(dealer_id: int) -> list[dict]:
    """
    The dealer's documents that no vehicle page lists: uploaded without a
    vehicle on the old Documents page, left behind when their vehicle was
    deleted, or belonging to a customer who no longer has a vehicle here.
    """
    vehicle_ids, customer_ids = set(), set()
    for vehicle_id, customer_id in (db.session.query(Vehicle.id, Vehicle.customer_id)
                                    .filter(Vehicle.dealer_id == dealer_id)):
        vehicle_ids.add(vehicle_id)
        if customer_id is not None:
            customer_ids.add(customer_id)
    return [doc for doc in documents_get_by_dealer(dealer_id)
            if (doc['customer_id'] is None and doc['vehicle_id'] not in vehicle_ids)
            or (doc['customer_id'] is not None and doc['customer_id'] not in customer_ids)]


def vehicle_choices(dealer_id: int) -> list[dict]:
    """Every vehicle of the dealer, newest first, for "Attach to vehicle"."""
    rows = (db.session.query(Vehicle.id, Vehicle.year, Vehicle.make, Vehicle.model,
                             Vehicle.registration_number)
            .filter(Vehicle.dealer_id == dealer_id)
            .order_by(Vehicle.created_at.desc())
            .all())
    choices = []
    for row in rows:
        label = f'{row.year} {row.make} {row.model}'
        if row.registration_number:
            label = f'{row.registration_number} — {label}'
        choices.append({'id': row.id, 'label': label})
    return choices


def attach_document(dealer_id: int, document_id: int, vehicle_id: int) -> bool:
    """Link an unlinked vehicle document to one of the dealer's vehicles; False if either isn't theirs."""
    doc = next((d for d in unlinked_documents(dealer_id) if d['id'] == document_id), None)
    if doc is None or doc['customer_id'] is not None:
        return False
    if Vehicle.query.filter_by(id=vehicle_id, dealer_id=dealer_id).first() is None:
        return False
    Document.query.filter_by(id=document_id, dealer_id=dealer_id).update({'vehicle_id': vehicle_id})
    db.session.commit()
    return True


def remove_unlinked_document(dealer_id: int, document_id: int) -> bool:
    """Delete one of the dealer's unlinked documents and its file; False if there is no such document."""
    if all(doc['id'] != document_id for doc in unlinked_documents(dealer_id)):
        return False
    record = Document.query.filter_by(id=document_id, dealer_id=dealer_id).one()
    path = os.path.join(document_folder(record.storage), record.filename)
    db.session.delete(record)
    db.session.commit()
    remove_file(path)
    return True
