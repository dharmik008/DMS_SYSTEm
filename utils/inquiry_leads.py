"""
utils/inquiry_leads.py — customer inquiries become CRM leads.

Every inquiry a customer sends (a marketplace car page, a dealer website car
page or a dealer website contact form) is saved as a Lead for the dealer it is
addressed to. It shows up in CRM / Leads straight away and follows the normal
lead workflow; no separate inquiry record is kept.

Duplicates: each inquiry lead has an `inquiry_key` that is unique in the
database. The same inquiry sent again on the same day (double click, retry,
resent form) produces the same key, so the existing lead is returned instead
of a second one being created, even when two requests race each other.

History: rows in the old `inquiries` table are copied into leads once, at
startup (prepare_database). The table is left in place as an archive.
"""
from __future__ import annotations

import hashlib
import logging
import re

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from db import lead_create
from extensions import db
from models import Inquiry, Lead, Vehicle, _now_ist
from utils.otp import normalise_phone

log = logging.getLogger(__name__)

SOURCE_MARKETPLACE = 'marketplace'
SOURCE_WEBSITE = 'website'

INQUIRY_TYPE_LABELS = {
    'general': 'General Inquiry',
    'test_drive': 'Test Drive',
    'price': 'Price Inquiry',
    'vehicle': 'Vehicle Inquiry',
    'contact': 'Contact Form',
}

LEGACY_KEY_PREFIX = 'legacy-inquiry:'
MAX_NAME_LENGTH = 100   # Lead.customer_name
MAX_EMAIL_LENGTH = 120  # Lead.customer_email
SAVE_FAILED_MESSAGE = "We couldn't send your inquiry right now. Please try again in a moment."
_EMAIL_PATTERN = re.compile(r'[^@\s]+@[^@\s]+\.[^@\s]+')


class InquiryError(Exception):
    """The inquiry was not saved. The message is safe to show the customer."""
    status_code = 400


class InquiryNotSaved(InquiryError):
    """The database could not store the inquiry; the customer should retry."""
    status_code = 503


def inquiry_type_label(inquiry_type: str | None) -> str:
    """Display name for an inquiry type, e.g. 'test_drive' -> 'Test Drive'."""
    if not inquiry_type:
        return ''
    return INQUIRY_TYPE_LABELS.get(inquiry_type, inquiry_type.replace('_', ' ').title())


def create_lead_from_inquiry(*, dealer_id: int | None, source: str, inquiry_type: str,
                             name: str | None, phone: str | None, email: str | None = None,
                             message: str | None = None,
                             vehicle_id: int | None = None) -> tuple[Lead, bool]:
    """
    Save a customer inquiry as a new lead for `dealer_id`.

    Returns (lead, created); `created` is False when this inquiry had already
    been saved and the existing lead is returned. Raises InquiryError, with a
    message for the customer, when the inquiry can't be accepted or stored.
    """
    name = (name or '').strip()
    phone10 = normalise_phone(phone)
    email = (email or '').strip().lower()
    message = (message or '').strip()

    if not name:
        raise InquiryError('Please enter your name.')
    if len(name) > MAX_NAME_LENGTH:
        raise InquiryError('Please enter a shorter name.')
    if phone10 is None:
        raise InquiryError('Please enter a valid 10-digit mobile number.')
    if email and (len(email) > MAX_EMAIL_LENGTH or not _EMAIL_PATTERN.fullmatch(email)):
        raise InquiryError('Please enter a valid email address.')
    if inquiry_type not in INQUIRY_TYPE_LABELS:
        inquiry_type = 'general'

    try:
        vehicle_id = _dealer_vehicle_id(dealer_id, vehicle_id)
        key = _inquiry_key(dealer_id, vehicle_id, inquiry_type, phone10, email, message)
        lead = Lead.query.filter_by(inquiry_key=key).first()
        if lead is not None:
            return lead, False
        try:
            lead_id = lead_create({
                'dealer_id': dealer_id,
                'vehicle_id': vehicle_id,
                'customer_name': name,
                'customer_phone': phone10,
                'customer_email': email,
                'source': source,
                'stage': 'new',
                'notes': message,
                'inquiry_type': inquiry_type,
                'inquiry_message': message or None,
                'inquiry_key': key,
            })
        except IntegrityError:
            # A parallel request saved the same inquiry between the check and the insert.
            db.session.rollback()
            lead = Lead.query.filter_by(inquiry_key=key).first()
            if lead is None:
                raise
            return lead, False
        return db.session.get(Lead, lead_id), True
    except SQLAlchemyError as exc:
        db.session.rollback()
        log.exception('Inquiry for dealer %s could not be saved as a lead', dealer_id)
        raise InquiryNotSaved(SAVE_FAILED_MESSAGE) from exc


def _dealer_vehicle_id(dealer_id: int | None, vehicle_id: int | None) -> int | None:
    """
    vehicle_id if it is one of this dealer's cars, otherwise None.

    The dealer website sends vehicle_id from the browser, so a car belonging to
    another dealer (or one deleted since the page loaded) is not linked.
    """
    if vehicle_id is None:
        return None
    if Vehicle.query.filter_by(id=vehicle_id, dealer_id=dealer_id).first() is None:
        log.warning('Inquiry for dealer %s named vehicle %s, which is not theirs; '
                    'saved without the vehicle', dealer_id, vehicle_id)
        return None
    return vehicle_id


def _inquiry_key(dealer_id, vehicle_id, inquiry_type, phone10, email, message) -> str:
    """The same inquiry on the same day gives the same key; message spacing and case don't count."""
    parts = (dealer_id, vehicle_id, inquiry_type, phone10, email,
             ' '.join(message.lower().split()), _now_ist().date().isoformat())
    raw = '|'.join('' if part is None else str(part) for part in parts)
    return hashlib.sha256(raw.encode()).hexdigest()


# ── Old inquiries ───────────────────────────────────────────────────────────
def prepare_database() -> None:
    """Startup hook: add the duplicate guard to older databases, then copy old inquiries."""
    try:
        _ensure_inquiry_key_index()
        copied = migrate_legacy_inquiries()
    except SQLAlchemyError:
        db.session.rollback()
        log.exception('inquiry_leads: startup step failed; it runs again at the next start')
        return
    if copied:
        log.warning('inquiry_leads: copied %d old inquiries into CRM / Leads', copied)


def _ensure_inquiry_key_index() -> None:
    # db.create_all() builds this index on new databases. On older ones
    # schema_sync adds the column but never indexes, so it is added here.
    with db.engine.begin() as conn:
        conn.execute(text(
            'CREATE UNIQUE INDEX IF NOT EXISTS ix_leads_inquiry_key ON leads (inquiry_key)'))


def migrate_legacy_inquiries() -> int:
    """
    Copy inquiries saved before this change into leads; returns how many.

    Safe to run repeatedly: each copied inquiry is keyed by its id, so it is
    never copied twice. The inquiries table itself is not modified.
    """
    done = {key for (key,) in db.session.query(Lead.inquiry_key)
            .filter(Lead.inquiry_key.like(LEGACY_KEY_PREFIX + '%'))}
    pending = [inquiry for inquiry in Inquiry.query.order_by(Inquiry.id).all()
               if f'{LEGACY_KEY_PREFIX}{inquiry.id}' not in done]
    # Read everything before writing: each copy commits on its own, so one bad
    # row can't block the rest and a failed row is retried at the next start.
    rows = [(inquiry.id, _legacy_lead_data(inquiry)) for inquiry in pending]

    copied = 0
    for inquiry_id, data in rows:
        try:
            lead_create(data)
        except SQLAlchemyError:
            db.session.rollback()
            if Lead.query.filter_by(inquiry_key=data['inquiry_key']).first() is None:
                log.exception('Old inquiry #%s could not be copied into leads; '
                              'it will be retried at the next start', inquiry_id)
            continue  # otherwise another worker copied it at the same moment
        copied += 1
    return copied


def _legacy_lead_data(inquiry: Inquiry) -> dict:
    vehicle = db.session.get(Vehicle, inquiry.vehicle_id) if inquiry.vehicle_id else None
    # The old Inquiries page showed an inquiry to the dealer who owns the car.
    if vehicle is not None and vehicle.dealer_id is not None:
        dealer_id = vehicle.dealer_id
    else:
        dealer_id = inquiry.dealer_id
    # Marketplace inquiries always name a car and are 'general' or 'test_drive';
    # the dealer website sends 'vehicle', 'contact', or 'general' without a car.
    from_website = inquiry.inquiry_type in ('vehicle', 'contact') or not inquiry.vehicle_id
    message = (inquiry.message or '').strip()
    return {
        'dealer_id': dealer_id,
        'vehicle_id': vehicle.id if vehicle is not None else None,
        'customer_name': (inquiry.name or '').strip() or 'Unknown',
        'customer_phone': normalise_phone(inquiry.phone) or (inquiry.phone or '').strip(),
        'customer_email': (inquiry.email or '').strip(),
        'source': SOURCE_WEBSITE if from_website else SOURCE_MARKETPLACE,
        # "Responded" means the dealer already reached the customer; the nearest
        # stage in the current lead workflow is Connected.
        'stage': 'connected' if inquiry.status == 'responded' else 'new',
        'notes': message,
        'inquiry_type': inquiry.inquiry_type or 'general',
        'inquiry_message': message or None,
        'inquiry_key': f'{LEGACY_KEY_PREFIX}{inquiry.id}',
        'created_at': inquiry.created_at,
    }
