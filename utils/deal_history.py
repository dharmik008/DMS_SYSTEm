"""
utils/deal_history.py — the Car Status history behind the Purchase → Sale Trail.

Each deal keeps a numbered, append-only list of its status events:

    1  Negotiation  Deal created with Negotiation status
    2  Finalized    Deal status changed from Negotiation to Finalized
    3  Delivered    Deal status changed from Finalized to Delivered

db.deal_create() and db.deal_update() add the entry in the same transaction as
the change it describes, so a deal's status and its history cannot disagree.
Saving without changing the status adds nothing. (deal_id, seq) is unique, so
two saves racing to record the same step cannot both succeed: the loser is
rolled back and gets DealStatusConflict instead of writing a duplicate.

Deals saved before the history existed get one 'baseline' entry at startup
with the status they had then. Their earlier changes were never recorded, and
the entry says so rather than inventing them.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from extensions import db
from models import Deal, DealStatusHistory
from utils.statuses import DEAL, resolve_status, status_label

log = logging.getLogger(__name__)

MAX_NOTE_LENGTH = 500


class DealStatusConflict(Exception):
    """Another save changed the deal's status at the same moment; nothing was written."""


def record_status_event(deal: Deal, *, event: str, from_status: str | None = None,
                        changed_by: Mapping | None = None,
                        note: str | None = None) -> DealStatusHistory:
    """Add the deal's next history entry, for its current status, to the session. The caller commits."""
    entry = DealStatusHistory(
        deal_id=deal.id,
        vehicle_id=deal.vehicle_id,
        seq=_next_seq(deal.id),
        event=event,
        from_status=from_status,
        to_status=resolve_status(DEAL, deal.status).value,
        changed_by_id=changed_by.get('id') if changed_by else None,
        changed_by_name=changed_by.get('name') if changed_by else None,
        note=(note or '').strip() or None,
    )
    db.session.add(entry)
    return entry


def _next_seq(deal_id: int) -> int:
    last = (db.session.query(func.max(DealStatusHistory.seq))
            .filter(DealStatusHistory.deal_id == deal_id).scalar())
    return (last or 0) + 1


def describe(entry: DealStatusHistory) -> str:
    """The Description column of the trail."""
    stage = status_label(DEAL, entry.to_status)
    if entry.event == DealStatusHistory.CREATED:
        return f'Deal created with {stage} status'
    if entry.event == DealStatusHistory.BASELINE:
        return (f'Deal was already at {stage} status when status history began; '
                'earlier changes were not recorded')
    return f'Deal status changed from {status_label(DEAL, entry.from_status)} to {stage}'


def history_by_deal(dealer_id: int) -> dict[int, list[dict]]:
    """Every history entry of the dealer's deals, grouped by deal id, oldest first."""
    rows = (DealStatusHistory.query
            .join(Deal, Deal.id == DealStatusHistory.deal_id)
            .filter(Deal.dealer_id == dealer_id)
            .order_by(DealStatusHistory.deal_id, DealStatusHistory.seq)
            .all())
    history: dict[int, list[dict]] = {}
    for row in rows:
        history.setdefault(row.deal_id, []).append({
            'seq': row.seq,
            'event': row.event,
            'from_status': row.from_status,
            'to_status': row.to_status,
            'description': describe(row),
            'changed_at': row.changed_at,
            'changed_by': row.changed_by_name,
            'note': row.note,
        })
    return history


def start_tracking_existing_deals() -> int:
    """Startup hook: give every deal that has no history its baseline entry. Returns how many."""
    try:
        untracked = Deal.query.filter(~Deal.status_history.any()).order_by(Deal.id).all()
        for deal in untracked:
            db.session.add(DealStatusHistory(
                deal_id=deal.id, vehicle_id=deal.vehicle_id, seq=1,
                event=DealStatusHistory.BASELINE,
                to_status=resolve_status(DEAL, deal.status).value,
            ))
        db.session.commit()
    except IntegrityError:
        # Another worker starting at the same moment added them first.
        db.session.rollback()
        return 0
    except SQLAlchemyError:
        db.session.rollback()
        log.exception('deal_history: could not start the history of existing deals; '
                      'it is retried at the next start')
        return 0
    if untracked:
        log.warning('deal_history: started the status history of %d existing deal(s)', len(untracked))
    return len(untracked)
