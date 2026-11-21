"""
vehicle_contacts.py
-------------------
Who has already asked about this vehicle.

Powers the suggestion list in Customer Info on the New Deal page: pick a
vehicle and the people who raised a lead against it (customer inquiries
included) appear,
so the dealer taps a name instead of retyping it.

Reads only. Nothing here writes, and it never invents a contact — an empty
list simply means nobody has asked about that car yet.
"""

from extensions import db
from models import Lead, Vehicle

MAX_CONTACTS = 12

STAGE_LABELS = {
    'new': 'New',
    'contacted': 'Contacted',
    'qualified': 'Qualified',
    'negotiation': 'Negotiation',
    'won': 'Won',
    'lost': 'Lost',
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
    Leads raised against one vehicle, newest first. Customer inquiries are
    saved as leads, so they are included.

    The query is filtered by dealer_id as well as vehicle_id, so a dealer can
    never pull another rooftop's contacts by passing a vehicle id that is not
    theirs. The route checks vehicle ownership too — belt and braces.

    Someone with several leads for this car is returned once, with the
    details of their most recent one.
    """
    leads = (Lead.query
             .filter(Lead.dealer_id == dealer_id, Lead.vehicle_id == vehicle_id)
             .order_by(Lead.created_at.desc())
             .limit(MAX_CONTACTS * 2)
             .all())

    merged = {}
    for lead in leads:
        key = _phone_key(lead.customer_phone) or 'l{}'.format(lead.id)
        if key in merged:
            continue  # newest first, so the entry already kept is the most recent
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
        }

    out = list(merged.values())[:MAX_CONTACTS]
    for entry in out:
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
