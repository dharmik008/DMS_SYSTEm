"""
inventory_alerts.py
-------------------
Inventory threshold + dead-stock popup alert engine (dealer-scoped).

An alert fires on a CROSSING, not on a state. Every (dealer, threshold) pair
holds an `armed` flag in the inventory_alert_state table:

    condition true  + armed    -> fire, then disarm
    condition true  + disarmed -> silence (it already fired)
    condition false            -> re-arm silently (no recovery alert)

That is what makes a page refresh silent and what lets a threshold fire a
second time after inventory recovers and falls again.

Usage
-----
    from utils.inventory_alerts import evaluate_dealer_alerts, inventory_snapshot

    alerts = evaluate_dealer_alerts(dealer_id)   # consumes the crossings
    snap   = inventory_snapshot(dealer_id)       # read-only counts, fires nothing
"""

from datetime import datetime, timedelta, timezone as _tz

from extensions import db
from models import Vehicle, InventoryAlertState

_IST = _tz(timedelta(hours=5, minutes=30))


def _now_ist():
    return datetime.now(_IST).replace(tzinfo=None)


# ── Configuration ────────────────────────────────────────────────────────────
# Which vehicle statuses count as "on the lot". Sold cars are not inventory.
IN_STOCK_STATUSES = ('available', 'reserved', 'trade-in')

DAYS_PER_MONTH = 30

# Inventory thresholds, least to most severe. `sev` breaks ties when a single
# sale crosses several at once — only the most severe one is shown.
INVENTORY_THRESHOLDS = [
    {'id': 'inv_at_50',    'sev': 0, 'boundary': 50, 'tier': 'green',
     'glyph': '🟢', 'title': 'INVENTORY THRESHOLD REACHED'},
    {'id': 'inv_below_50', 'sev': 1, 'boundary': 50, 'tier': 'blue',
     'glyph': '🔵', 'title': 'LOW INVENTORY ALERT'},
    {'id': 'inv_below_30', 'sev': 2, 'boundary': 30, 'tier': 'yellow',
     'glyph': '🟡', 'title': 'INVENTORY WARNING'},
    {'id': 'inv_below_25', 'sev': 3, 'boundary': 25, 'tier': 'orange',
     'glyph': '🟠', 'title': 'CRITICAL INVENTORY'},
    {'id': 'inv_below_20', 'sev': 4, 'boundary': 20, 'tier': 'red',
     'glyph': '🔴', 'title': 'VERY CRITICAL INVENTORY'},
    {'id': 'inv_below_10', 'sev': 5, 'boundary': 10, 'tier': 'maroon',
     'glyph': '🔴', 'title': 'EXTREMELY LOW INVENTORY'},
]

AGING_MILESTONES = [
    {'id': 'age_2mo',  'sev': 0, 'months': 2,  'tier': 'blue',
     'glyph': '🔵', 'title': 'DEAD STOCK ALERT'},
    {'id': 'age_4mo',  'sev': 1, 'months': 4,  'tier': 'yellow',
     'glyph': '🟡', 'title': '4-MONTH AGING ALERT'},
    {'id': 'age_6mo',  'sev': 2, 'months': 6,  'tier': 'orange',
     'glyph': '🟠', 'title': '6-MONTH AGING ALERT'},
    {'id': 'age_8mo',  'sev': 3, 'months': 8,  'tier': 'red',
     'glyph': '🔴', 'title': '8-MONTH AGING ALERT'},
    {'id': 'age_12mo', 'sev': 4, 'months': 12, 'tier': 'maroon',
     'glyph': '🔴', 'title': '12-MONTH AGING ALERT'},
]


def _threshold_hit(rule, count):
    """`inv_at_50` fires at exactly 50 and below; the rest fire strictly below."""
    if rule['id'] == 'inv_at_50':
        return count <= rule['boundary']
    return count < rule['boundary']


# ── Snapshot ─────────────────────────────────────────────────────────────────
def inventory_snapshot(dealer_id):
    """Current stock and aging counts for one dealer. Read-only: fires nothing."""
    rows = (db.session.query(Vehicle.created_at)
            .filter(Vehicle.dealer_id == dealer_id,
                    Vehicle.status.in_(IN_STOCK_STATUSES))
            .all())

    now = _now_ist()
    ages = []
    for (created_at,) in rows:
        if created_at is None:
            ages.append(0)
        else:
            ages.append((now - created_at).days)

    total = len(ages)
    aging = {}
    for m in AGING_MILESTONES:
        limit = m['months'] * DAYS_PER_MONTH
        aging[m['months']] = sum(1 for d in ages if d > limit)

    return {
        'in_stock': total,
        'aging': aging,                      # {2: n, 4: n, 6: n, 8: n, 12: n}
        'dead_stock': aging.get(2, 0),
    }


# ── Alert bodies ─────────────────────────────────────────────────────────────
def _pct(part, whole):
    return '{:.1f}%'.format((part / whole * 100) if whole else 0.0)


def _inventory_alert(rule, count):
    below = max(0, rule['boundary'] - count)
    if rule['id'] == 'inv_at_50':
        sub = 'Your inventory has reached the <strong>{}-vehicle threshold</strong>.'.format(
            rule['boundary'])
    else:
        sub = 'Your inventory has fallen below the <strong>{}-vehicle threshold</strong>.'.format(
            rule['boundary'])

    return {
        'kind': 'inventory',
        'threshold_id': rule['id'],
        'tier': rule['tier'],
        'glyph': rule['glyph'],
        'title': rule['title'],
        'lead': 'Your current inventory is <strong>{} vehicle{}</strong>.'.format(
            count, '' if count == 1 else 's'),
        'sub': sub,
        'rows': [
            ['Current Inventory', str(count), True],
            ['Alert Threshold', str(rule['boundary']), False],
            ['Vehicles Below Threshold', str(below), False],
        ],
        'action_label': 'View Inventory',
        'aging_filter': 0,
    }


def _aging_alert(rule, count, total):
    months = rule['months']
    if months == 2:
        count_label, pct_label = 'Dead Stock', 'Dead Stock Percentage'
    else:
        count_label = 'Aged {} Months+'.format(months)
        pct_label = 'Share of Inventory'

    return {
        'kind': 'aging',
        'threshold_id': rule['id'],
        'tier': rule['tier'],
        'glyph': rule['glyph'],
        'title': rule['title'],
        'lead': '<strong>{} vehicle{}</strong> {} been in inventory for more than '
                '<strong>{} months</strong>.'.format(
                    count, '' if count == 1 else 's',
                    'has' if count == 1 else 'have', months),
        'sub': '',
        'rows': [
            ['Total Inventory', str(total), False],
            [count_label, str(count), True],
            [pct_label, _pct(count, total), False],
        ],
        'action_label': 'View Vehicles',
        'aging_filter': months,
    }


# ── State helpers ────────────────────────────────────────────────────────────
def _state_map(dealer_id):
    rows = InventoryAlertState.query.filter_by(dealer_id=dealer_id).all()
    return {r.threshold_id: r for r in rows}


def _set_armed(dealer_id, states, threshold_id, armed, fired=False):
    row = states.get(threshold_id)
    if row is None:
        row = InventoryAlertState(dealer_id=dealer_id, threshold_id=threshold_id)
        db.session.add(row)
        states[threshold_id] = row
    row.armed = armed
    if fired:
        row.last_fired_at = _now_ist()
    return row


# ── The engine ───────────────────────────────────────────────────────────────
def evaluate_dealer_alerts(dealer_id, silent=False):
    """
    Evaluate every threshold for one dealer and return the popups to show.

    Returns at most two alerts: the most severe inventory crossing and the most
    severe aging crossing. Crossing several thresholds in one move disarms them
    all but shows only the worst — no duplicate popups.

    `silent=True` syncs the armed flags to current reality without returning
    anything. Use it when seeding a dealer so pre-existing conditions do not
    all fire at once on first load.
    """
    snap = inventory_snapshot(dealer_id)
    count = snap['in_stock']
    states = _state_map(dealer_id)

    fired_inventory = []
    for rule in INVENTORY_THRESHOLDS:
        row = states.get(rule['id'])
        armed = True if row is None else row.armed
        if _threshold_hit(rule, count):
            if armed:
                _set_armed(dealer_id, states, rule['id'], False, fired=True)
                fired_inventory.append(rule)
        else:
            # Re-arms on the way up, so the same threshold can fire again later.
            if row is None or row.armed is False:
                _set_armed(dealer_id, states, rule['id'], True)

    fired_aging = []
    for rule in AGING_MILESTONES:
        aged = snap['aging'].get(rule['months'], 0)
        row = states.get(rule['id'])
        armed = True if row is None else row.armed
        if aged > 0:
            if armed:
                _set_armed(dealer_id, states, rule['id'], False, fired=True)
                fired_aging.append((rule, aged))
        else:
            if row is None or row.armed is False:
                _set_armed(dealer_id, states, rule['id'], True)

    db.session.commit()

    if silent:
        return []

    alerts = []
    if fired_inventory:
        worst = max(fired_inventory, key=lambda r: r['sev'])
        alerts.append(_inventory_alert(worst, count))
    if fired_aging:
        worst_rule, worst_count = max(fired_aging, key=lambda x: x[0]['sev'])
        alerts.append(_aging_alert(worst_rule, worst_count, count))

    return alerts


def reset_dealer_alerts(dealer_id):
    """Re-arm everything for a dealer. Useful in testing."""
    InventoryAlertState.query.filter_by(dealer_id=dealer_id).delete()
    db.session.commit()


# ── Standing condition, as opposed to a crossing ────────────────────────────
# ALERT_MODE decides what the inventory page shows:
#
#   'always'    show where stock stands every time the page opens. A dealer
#               sitting at 8 vehicles is reminded on every visit, not just on
#               the day they crossed 10.
#   'crossing'  the original behaviour — fire once when a threshold is crossed,
#               then stay quiet until stock recovers and falls again.
#
# 'always' deliberately reverses requirement 12 of the original spec. The
# armed flags still work exactly as before underneath, so switching back to
# 'crossing' restores the old behaviour with no other change.
ALERT_MODE = 'always'


def current_alerts(dealer_id):
    """
    The conditions that are true right now, regardless of what has already
    been shown. Read-only: touches no armed flag and commits nothing, so
    calling it on every page load costs one query and changes no state.

    Returns at most two: the most severe inventory threshold currently
    breached, and the most severe aging milestone currently exceeded.
    """
    snap = inventory_snapshot(dealer_id)
    count = snap['in_stock']
    alerts = []

    breached = [r for r in INVENTORY_THRESHOLDS if _threshold_hit(r, count)]
    if breached:
        worst = max(breached, key=lambda r: r['sev'])
        alert = _inventory_alert(worst, count)
        alert['standing'] = True
        alerts.append(alert)

    aged = [(b, snap['aging'].get(b['months'], 0)) for b in AGING_MILESTONES]
    aged = [(b, c) for b, c in aged if c > 0]
    if aged:
        worst_b, worst_c = max(aged, key=lambda x: x[0]['sev'])
        alert = _aging_alert(worst_b, worst_c, count)
        alert['standing'] = True
        alerts.append(alert)

    return alerts


def alerts_for_page(dealer_id, mode=None):
    """
    What the inventory page should display on this load.

    A fresh crossing always wins, because "you just dropped below 20" is more
    urgent than "you are below 20". Only when nothing new has crossed does the
    standing condition fill in — so the dealer sees something whenever stock
    is low, without the same crossing being announced twice.
    """
    mode = mode or ALERT_MODE

    # runs either way: the armed flags must stay in step with reality
    fired = evaluate_dealer_alerts(dealer_id)

    if mode != 'always':
        return fired

    if fired:
        return fired

    return current_alerts(dealer_id)
