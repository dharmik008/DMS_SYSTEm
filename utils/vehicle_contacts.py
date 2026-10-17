"""
vehicle_contacts.py
-------------------
Who has already asked about this vehicle.

Powers the suggestion list in Customer Info on the New Deal page: pick a
vehicle and the people who raised a lead or an inquiry against it appear,
so the dealer taps a name instead of retyping it.

Reads only. Nothing here writes, and it never invents a contact — an empty
list simply means nobody has asked about that car yet.
"""

from extensions import db
from models import Lead, Inquiry, Vehicle

MAX_CONTACTS = 12

STAGE_LABELS = {
    'new': 'New',
    'contacted': 'Contacted',
    'qualified': 'Qualified',
    'negotiation': 'Negotiation',
    'won': 'Won',
    'lost': 'Lost',
}

STATUS_LABELS = {
    'pending': 'Pending',
    'responded': 'Responded',
    'closed': 'Closed',
}


def _phone_key(phone):
    """Last 10 digits, so '+919876543210' and '9876543210' are one person."""
    digits = ''.join(ch for ch in str(phone or '') if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def _fmt_date(value):
    try:
        return value.strftime('%d %b %Y')
    except Exception:
        return ''


def _clip(text, limit=90):
    text = (text or '').strip().replace('\n', ' ')
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def vehicle_contacts(dealer_id, vehicle_id):
    """
    Leads and inquiries raised against one vehicle, newest first.

    Both queries are filtered by dealer_id as well as vehicle_id, so a dealer
    can never pull another rooftop's contacts by passing a vehicle id that is
    not theirs. The route checks vehicle ownership too — belt and braces.

    Someone who appears as both a lead and an inquiry is returned once, with
    their most recent record's details and both sources noted.
    """
    merged = {}

    leads = (Lead.query
             .filter(Lead.dealer_id == dealer_id, Lead.vehicle_id == vehicle_id)
             .order_by(Lead.created_at.desc())
             .limit(MAX_CONTACTS * 2)
             .all())

    for lead in leads:
        key = _phone_key(lead.customer_phone) or 'l{}'.format(lead.id)
        entry = merged.get(key)
        if entry is None:
            merged[key] = {
                'name': lead.customer_name or '',
                'phone': lead.customer_phone or '',
                'email': lead.customer_email or '',
                'city': lead.customer_city or '',
                'sources': ['Lead'],
                'detail': STAGE_LABELS.get(
                    (lead.stage or '').lower(), (lead.stage or '').title()),
                'when': _fmt_date(lead.created_at),
                'note': _clip(lead.notes),
                '_sort': lead.created_at,
            }
        elif 'Lead' not in entry['sources']:
            entry['sources'].append('Lead')

    inquiries = (Inquiry.query
                 .filter(Inquiry.dealer_id == dealer_id,
                         Inquiry.vehicle_id == vehicle_id)
                 .order_by(Inquiry.created_at.desc())
                 .limit(MAX_CONTACTS * 2)
                 .all())

    for inq in inquiries:
        key = _phone_key(inq.phone) or 'i{}'.format(inq.id)
        entry = merged.get(key)
        if entry is None:
            merged[key] = {
                'name': inq.name or '',
                'phone': inq.phone or '',
                'email': inq.email or '',
                'city': '',
                'sources': ['Inquiry'],
                'detail': STATUS_LABELS.get(
                    (inq.status or '').lower(), (inq.status or '').title()),
                'when': _fmt_date(inq.created_at),
                'note': _clip(inq.message),
                '_sort': inq.created_at,
            }
        else:
            if 'Inquiry' not in entry['sources']:
                entry['sources'].append('Inquiry')
            # fill any gap the lead record left
            if not entry['email']:
                entry['email'] = inq.email or ''
            if not entry['note']:
                entry['note'] = _clip(inq.message)

    out = sorted(merged.values(),
                 key=lambda e: e['_sort'] or 0,
                 reverse=True)[:MAX_CONTACTS]

    for entry in out:
        entry.pop('_sort', None)
        entry['source_label'] = ' + '.join(entry['sources'])
        if entry['detail']:
            entry['source_label'] += ' · ' + entry['detail']

    return out


def dealer_owns_vehicle(dealer_id, vehicle_id):
    """True when this vehicle belongs to this dealer."""
    return db.session.query(
        Vehicle.query.filter(Vehicle.id == vehicle_id,
                             Vehicle.dealer_id == dealer_id).exists()
    ).scalar()
