"""
dealer_sync.py
--------------
Two-way sync between Caryanams dealers (users where role='dealer') and an
external system.

HOW IT DECIDES WHAT TO DO
-------------------------
`users` has no updated_at, so "who changed it last" cannot be answered from
timestamps. Instead every synced dealer stores a SHADOW: the field values as
they stood at the end of the last sync. Each run compares three versions:

    shadow   what both sides agreed on last time
    local    what Caryanams holds now
    remote   what the other system holds now

    local == shadow, remote != shadow   -> pull  (only they changed it)
    local != shadow, remote == shadow   -> push  (only we changed it)
    local == remote                     -> nothing to do
    both moved, to different values     -> CONFLICT

That last case is the whole reason two-way sync is hard. There is no correct
automatic answer, so by default nothing is overwritten: the conflict is
recorded and a human decides. Set a policy of 'local' or 'remote' only if you
genuinely accept silent data loss on the losing side.

DELETES
-------
A dealer missing from the remote feed is NEVER deleted here. Sixteen tables
hang off dealer_id — vehicles, deals, leads, documents, payments — and
deleting the row would orphan or destroy all of it. Missing dealers are
deactivated instead, which is reversible.

WRITING AN ADAPTER
------------------
Subclass DealerSource for whatever you are connecting to — a database, an
API, a CSV export. Two methods, no framework involved:

    class MySource(DealerSource):
        name = 'legacy_dms'

        def fetch_all(self):
            return [{'external_id': '1001', 'name': 'Sharma Motors',
                     'phone': '9876543210', 'city': 'Ahmedabad'}, ...]

        def push(self, external_id, changes):
            # changes is {field: new_value}; return True on success
            ...
"""

import json
from datetime import datetime, timedelta, timezone as _tz

from extensions import db
from models import User

_IST = _tz(timedelta(hours=5, minutes=30))


def _now_ist():
    return datetime.now(_IST).replace(tzinfo=None)


# ── What is safe to sync ────────────────────────────────────────────────────
# Deliberately excluded and why:
#   email          unique key here; a remote change can collide with a real
#                  account. Used for matching instead, never overwritten.
#   password_hash  credentials do not belong in a data feed
#   role           changing it would turn a dealer into an admin
#   display_id     permanent internal identifier, must never move
#   subscription_* billing is owned by Caryanams alone
#   is_active      driven by the deactivation rule below, not field sync
SYNCED_FIELDS = [
    'name',
    'phone',
    'company_name',
    'city',
    'address',
    'whatsapp_number',
    'website_name',
    'google_maps_url',
    'years_in_business',
    'business_hours',
]

CONFLICT_POLICIES = ('flag', 'local', 'remote')


class DealerSource:
    """Adapter interface. Subclass this for your system."""

    name = 'external'

    def fetch_all(self):
        raise NotImplementedError('fetch_all() must return a list of dicts')

    def push(self, external_id, changes):
        """Apply {field: value} remotely. Return True on success."""
        raise NotImplementedError('push() must be implemented for two-way sync')


class SyncReport:
    def __init__(self):
        self.created = []
        self.pulled = []        # (external_id, field, old, new)
        self.pushed = []        # (external_id, field, old, new)
        self.conflicts = []     # (external_id, field, local, remote)
        self.deactivated = []
        self.skipped = []       # (external_id, reason)
        self.push_failures = []

    def summary(self):
        return {
            'created': len(self.created),
            'pulled': len(self.pulled),
            'pushed': len(self.pushed),
            'conflicts': len(self.conflicts),
            'deactivated': len(self.deactivated),
            'skipped': len(self.skipped),
            'push_failures': len(self.push_failures),
        }


def _norm(value):
    """Compare values the way a person would: '' and None are the same."""
    if value is None:
        return ''
    if isinstance(value, str):
        return value.strip()
    return value


def _load_shadow(user):
    try:
        return json.loads(user.sync_shadow) if user.sync_shadow else {}
    except (ValueError, TypeError):
        return {}


def _save_shadow(user, values):
    user.sync_shadow = json.dumps(values, default=str)
    user.synced_at = _now_ist()


def _match_existing(source_name, external_id, email):
    """
    Find the local dealer for a remote record.

    Matches on the link columns first. Falls back to email only for a dealer
    that has never been linked, which is how existing accounts get adopted
    into the sync on the first run instead of being duplicated.
    """
    user = User.query.filter_by(
        external_source=source_name, external_id=str(external_id)).first()
    if user:
        return user, 'link'

    if email:
        user = User.query.filter(
            db.func.lower(User.email) == str(email).strip().lower()).first()
        if user and not user.external_id:
            return user, 'email'
        if user:
            return None, 'email_taken'

    return None, 'new'


def sync_dealers(source, policy='flag', dry_run=False,
                 deactivate_missing=True, create_missing=True,
                 password_factory=None):
    """
    Run one sync pass. Returns a SyncReport.

    policy   'flag'   record conflicts, change nothing   (default, safe)
             'local'  Caryanams wins, push over remote
             'remote' the other system wins, overwrite local
    dry_run  work out everything, write nothing, anywhere
    """
    if policy not in CONFLICT_POLICIES:
        raise ValueError('policy must be one of {}'.format(CONFLICT_POLICIES))

    report = SyncReport()
    remote_rows = source.fetch_all() or []
    seen_ids = set()

    for row in remote_rows:
        external_id = row.get('external_id')
        if external_id in (None, ''):
            report.skipped.append((None, 'row has no external_id'))
            continue
        external_id = str(external_id)
        seen_ids.add(external_id)

        user, how = _match_existing(source.name, external_id, row.get('email'))

        # ── a dealer we have never seen ────────────────────────────────────
        if user is None:
            if how == 'email_taken':
                report.skipped.append(
                    (external_id, 'email belongs to a dealer linked elsewhere'))
                continue
            if not create_missing:
                report.skipped.append((external_id, 'create_missing is off'))
                continue
            if not row.get('email'):
                report.skipped.append((external_id, 'no email; cannot create'))
                continue

            if not dry_run:
                user = User(
                    name=row.get('name') or 'Unnamed dealer',
                    email=str(row['email']).strip().lower(),
                    role='dealer',
                    external_source=source.name,
                    external_id=external_id,
                )
                # password_hash is NOT NULL. A synced dealer has no Caryanams
                # password, so set an unusable one and force a reset.
                user.set_password(password_factory() if password_factory
                                  else _unusable_password())
                user.force_password_change = True
                for field in SYNCED_FIELDS:
                    if field in row:
                        setattr(user, field, row[field])
                db.session.add(user)
                db.session.flush()
                _save_shadow(user, {f: _norm(getattr(user, f, None))
                                    for f in SYNCED_FIELDS})
            report.created.append(external_id)
            continue

        # ── adopt an existing account into the sync ────────────────────────
        if how == 'email':
            if not dry_run:
                user.external_source = source.name
                user.external_id = external_id
                # no shadow yet: treat current local values as the baseline so
                # the first run does not report every field as a conflict
                _save_shadow(user, {f: _norm(getattr(user, f, None))
                                    for f in SYNCED_FIELDS})

        shadow = _load_shadow(user)
        to_push = {}

        for field in SYNCED_FIELDS:
            if field not in row:
                continue                     # remote does not carry this field

            local = _norm(getattr(user, field, None))
            remote = _norm(row.get(field))
            base = _norm(shadow.get(field))

            if local == remote:
                continue                     # already agree

            local_moved = local != base
            remote_moved = remote != base

            if remote_moved and not local_moved:
                if not dry_run:
                    setattr(user, field, row.get(field))
                report.pulled.append((external_id, field, local, remote))

            elif local_moved and not remote_moved:
                to_push[field] = getattr(user, field, None)
                report.pushed.append((external_id, field, remote, local))

            else:
                # both sides moved to different values
                report.conflicts.append((external_id, field, local, remote))
                if policy == 'remote' and not dry_run:
                    setattr(user, field, row.get(field))
                elif policy == 'local':
                    to_push[field] = getattr(user, field, None)
                elif not dry_run:
                    _record_conflict(user, field, local, remote)

        if to_push and not dry_run:
            try:
                ok = source.push(external_id, to_push)
            except Exception:
                ok = False
            if not ok:
                report.push_failures.append((external_id, sorted(to_push)))

        if not dry_run:
            # shadow records the agreed state; conflicted fields keep their old
            # baseline so the disagreement is still visible on the next run
            new_shadow = dict(shadow)
            conflicted = {f for eid, f, _, _ in report.conflicts if eid == external_id}
            for field in SYNCED_FIELDS:
                if field in row and field not in conflicted:
                    new_shadow[field] = _norm(getattr(user, field, None))
            _save_shadow(user, new_shadow)

    # ── dealers the feed no longer mentions ────────────────────────────────
    if deactivate_missing:
        gone = User.query.filter(
            User.external_source == source.name,
            User.external_id.isnot(None),
            ~User.external_id.in_(seen_ids) if seen_ids else True,
            User.is_active.is_(True),
        ).all()
        for user in gone:
            # never delete: 16 tables hang off this row
            if not dry_run:
                user.is_active = False
            report.deactivated.append(user.external_id)

    if not dry_run:
        db.session.commit()

    return report


def _unusable_password():
    import secrets
    return 'sync-' + secrets.token_urlsafe(32)


def _record_conflict(user, field, local, remote):
    from models import DealerSyncConflict
    existing = DealerSyncConflict.query.filter_by(
        dealer_id=user.id, field=field, resolved=False).first()
    if existing:
        existing.local_value = str(local)
        existing.remote_value = str(remote)
        existing.detected_at = _now_ist()
        return
    db.session.add(DealerSyncConflict(
        dealer_id=user.id, field=field,
        local_value=str(local), remote_value=str(remote),
    ))


def resolve_conflict(conflict_id, winner):
    """
    Settle one flagged conflict. `winner` is 'local' or 'remote'.
    Applies the chosen value and updates the shadow so it stops reappearing.
    """
    from models import DealerSyncConflict
    c = DealerSyncConflict.query.get(conflict_id)
    if not c or c.resolved:
        return False

    user = User.query.get(c.dealer_id)
    if not user:
        return False

    if winner == 'remote':
        setattr(user, c.field, c.remote_value)
        agreed = c.remote_value
    else:
        agreed = _norm(getattr(user, c.field, None))

    shadow = _load_shadow(user)
    shadow[c.field] = _norm(agreed)
    _save_shadow(user, shadow)

    c.resolved = True
    c.resolved_at = _now_ist()
    db.session.commit()
    return True


# ── A working adapter, so this is testable on day one ───────────────────────
class CsvDealerSource(DealerSource):
    """
    Reads dealers from a CSV whose first column is the external id.

    Two-way needs push() to write back, which a flat file cannot do safely
    under concurrent access — so this one reports push failures rather than
    pretending. Use it to trial the merge logic, then swap in a real adapter.
    """

    name = 'csv'

    def __init__(self, path, name=None):
        self.path = path
        if name:
            self.name = name

    def fetch_all(self):
        import csv
        with open(self.path, newline='', encoding='utf-8') as fh:
            return [dict(r) for r in csv.DictReader(fh)]

    def push(self, external_id, changes):
        return False
