"""
utils/statuses.py — the options behind every status dropdown in the dealer app.

One list per status field, shared by the forms, the inline dropdowns, the
filters and the badges, so the pages cannot drift apart:

    vehicle   Vehicle.status          Inventory, Add / Edit Vehicle, Vehicle Details
    deal      Deal.status             "Car Status" on Deals & Sales, New / Edit Deal
    document  Deal.document_status    "Document Status" on Deals & Sales

The columns stay plain strings and no stored row is rewritten. Spellings that
clearly mean a listed status ('Delivered', 'trade in', 'finalised') select it.
Anything else saved before these lists existed is kept: status_options() adds
it as a choice and clean_status() accepts it unchanged, so opening and saving
an older record never replaces its status behind the dealer's back.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

VEHICLE = 'vehicle'
DEAL = 'deal'
DOCUMENT = 'document'


@dataclass(frozen=True)
class StatusOption:
    value: str
    label: str
    tone: str  # badge / dropdown colour: success, warning, info, navy, danger or neutral


STATUSES: dict[str, tuple[StatusOption, ...]] = {
    VEHICLE: (
        StatusOption('available', 'Available', 'success'),
        StatusOption('reserved', 'Reserved', 'warning'),
        StatusOption('trade-in', 'Trade-In', 'info'),
        StatusOption('sold', 'Sold', 'danger'),
    ),
    DEAL: (
        StatusOption('negotiation', 'Negotiation', 'warning'),
        StatusOption('booked', 'Booked', 'info'),
        StatusOption('finalized', 'Finalized', 'navy'),
        StatusOption('delivered', 'Delivered', 'success'),
        StatusOption('cancelled', 'Cancelled', 'danger'),
    ),
    DOCUMENT: (
        StatusOption('pending', 'Pending', 'warning'),
        StatusOption('delivered', 'Delivered', 'success'),
    ),
}

# What an empty status means, and what a new record starts with.
DEFAULT_STATUS = {VEHICLE: 'available', DEAL: 'negotiation', DOCUMENT: 'pending'}

_FIELD_NAMES = {VEHICLE: 'vehicle status', DEAL: 'car status', DOCUMENT: 'document status'}

# Other spellings of a listed status. Case, spaces, hyphens and underscores are
# ignored before this lookup, so 'Trade In' already matches 'trade-in'.
_ALIASES = {
    DEAL: {'final': 'finalized', 'finalised': 'finalized', 'canceled': 'cancelled'},
}


class InvalidStatus(ValueError):
    """A submitted status that is not one of the options for its field."""


def _text(value) -> str:
    return str(value).strip() if value is not None else ''


def _key(value: str) -> str:
    return re.sub(r'[\s_-]+', '', value).casefold()


def status_values(kind: str) -> list[str]:
    return [option.value for option in STATUSES[kind]]


def canonical_status(kind: str, value) -> str | None:
    """The listed status a stored or submitted value stands for, or None."""
    key = _key(_text(value))
    if not key:
        return None
    for option in STATUSES[kind]:
        if key in (_key(option.value), _key(option.label)):
            return option.value
    return _ALIASES.get(kind, {}).get(key)


def resolve_status(kind: str, value) -> StatusOption:
    """
    The option a stored status is shown as. An empty value is the field's
    default; a value that matches nothing comes back as its own neutral option.
    """
    text = _text(value) or DEFAULT_STATUS[kind]
    canonical = canonical_status(kind, text)
    for option in STATUSES[kind]:
        if option.value == canonical:
            return option
    return StatusOption(text, text, 'neutral')


def status_options(kind: str, current=None) -> list[StatusOption]:
    """Dropdown options for a record; a stored status that is not listed stays selectable."""
    options = list(STATUSES[kind])
    resolved = resolve_status(kind, current)
    if resolved not in options:
        options.insert(0, resolved)
    return options


def selected_status(kind: str, current) -> str:
    return resolve_status(kind, current).value


def status_label(kind: str, value) -> str:
    return resolve_status(kind, value).label


def status_tone(kind: str, value) -> str:
    return resolve_status(kind, value).tone


def same_status(kind: str, first, second) -> bool:
    return resolve_status(kind, first).value == resolve_status(kind, second).value


def clean_status(kind: str, submitted, current=None) -> str:
    """
    The status to save for a submitted form value.

    A listed status, in any of its spellings, comes back in canonical form.
    The record's current value is accepted as it is, so an unlisted older
    status survives a save, and an empty value keeps the current status (the
    default for a new record). Anything else raises InvalidStatus.
    """
    text = _text(submitted)
    if not text:
        return resolve_status(kind, current).value
    canonical = canonical_status(kind, text)
    if canonical is not None:
        return canonical
    if text == _text(current):
        return text
    raise InvalidStatus(f'"{text[:40]}" is not a valid {_FIELD_NAMES[kind]}.')
