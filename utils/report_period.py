"""
report_period.py
----------------
Resolves the Day / Week / Month / Year buttons on the Reports page into a
concrete date window, and filters already-fetched rows against it.

Windows are calendar-aligned, not rolling:

    day    today, from 00:00
    week   this week, from Monday 00:00
    month  this month, from the 1st
    year   this year, from 1 January
    all    everything (the default, so the page behaves as it always has)

"This week" therefore means Monday to now, not the last seven days. A dealer
comparing Monday's figure to Sunday's expects the week to reset, not to slide.
"""

from datetime import datetime, timedelta, timezone as _tz

_IST = _tz(timedelta(hours=5, minutes=30))

PERIODS = [
    ('day',   'Today'),
    ('week',  'This Week'),
    ('month', 'This Month'),
    ('year',  'This Year'),
    ('all',   'All Time'),
]

VALID = {key for key, _ in PERIODS}
DEFAULT = 'all'


def _now_ist():
    return datetime.now(_IST).replace(tzinfo=None)


def resolve(period):
    """
    Turn a period key into (key, start, end, label).

    `start` is None for 'all'. `end` is always now, so a period never includes
    the future. An unknown or missing key falls back to the default rather
    than erroring — this comes straight off a query string.
    """
    key = (period or '').strip().lower()
    if key not in VALID:
        key = DEFAULT

    now = _now_ist()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if key == 'day':
        start = midnight
    elif key == 'week':
        start = midnight - timedelta(days=midnight.weekday())     # Monday
    elif key == 'month':
        start = midnight.replace(day=1)
    elif key == 'year':
        start = midnight.replace(month=1, day=1)
    else:
        start = None

    label = dict(PERIODS)[key]
    return key, start, now, label


def within(value, start, end):
    """True when a datetime falls inside the window. None never counts."""
    if start is None:
        return True
    if value is None:
        return False
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return False
    return start <= value <= end


def filter_rows(rows, start, end, field='created_at'):
    """Filter a list of dicts by a datetime field."""
    if start is None:
        return list(rows)
    return [r for r in rows if within(r.get(field), start, end)]


def describe(key, start, end):
    """Short human range for the page header, e.g. '01 Sep – 25 Sep 2026'."""
    if start is None:
        return 'All time'
    if key == 'day':
        return start.strftime('%d %b %Y')
    return '{} – {}'.format(start.strftime('%d %b'), end.strftime('%d %b %Y'))
