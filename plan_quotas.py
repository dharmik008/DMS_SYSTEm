"""
plan_quotas.py
--------------
Numeric limits per subscription plan, and the guard that enforces them.

subscription_features.py already answers "can this plan use Finance at all?"
This file answers the other question: "this plan can use Inventory, but how
MANY cars?" A Starter dealer adding an 11th car is not using a forbidden
feature — they are exceeding a count.

The numbers below are exactly what templates/dealer/subscription.html
advertises. If you change one, change it there too, or dealers will be sold
something the code refuses to give them.

WHAT COUNTS
-----------
Vehicles count CURRENT STOCK, not everything ever added. A sold car has left
the lot and no longer occupies a slot, which matches how the inventory page,
the marketplace and the low-stock alerts already treat it. Otherwise a Starter
dealer who sold ten cars could never add an eleventh, which is not what
"Inventory Management — 10 Cars" means to anyone reading it.

Agents count every agent on the account, active or not.
"""

from functools import wraps

from flask import flash, redirect, url_for, request, jsonify

from extensions import db
from models import Vehicle, Agent

UNLIMITED = None          # no cap

# ── Advertised limits, per templates/dealer/subscription.html ──────────────
PLAN_QUOTAS = {
    'starter': {'vehicles': 10,  'agents': 1,  'studio_monthly': 5},
    'growth':  {'vehicles': 100, 'agents': 3,  'studio_monthly': 100},
    'pro':     {'vehicles': UNLIMITED, 'agents': 10, 'studio_monthly': 500},
}

PLAN_ORDER = ['starter', 'growth', 'pro']
PLAN_DISPLAY = {'starter': 'Starter', 'growth': 'Growth', 'pro': 'Pro'}

RESOURCE_LABELS = {
    'vehicles': ('car', 'cars'),
    'agents': ('staff member / agent', 'staff / agents'),
    'studio_monthly': ('Studio render this month', 'Studio renders this month'),
}

# Statuses that still occupy an inventory slot. A sold car does not.
IN_STOCK_STATUSES = ('available', 'reserved', 'trade-in')


def normalise_plan(plan):
    plan = (plan or 'starter').strip().lower()
    return plan if plan in PLAN_QUOTAS else 'starter'


def limit_for(plan, resource):
    """The cap for this plan, or None for unlimited."""
    return PLAN_QUOTAS[normalise_plan(plan)].get(resource, UNLIMITED)


def usage_for(dealer_id, resource):
    """How much of the allowance is already spent."""
    if resource == 'vehicles':
        return Vehicle.query.filter(
            Vehicle.dealer_id == dealer_id,
            Vehicle.status.in_(IN_STOCK_STATUSES)).count()
    if resource == 'agents':
        return Agent.query.filter_by(dealer_id=dealer_id).count()
    if resource == 'studio_monthly':
        return _studio_usage_this_month(dealer_id)
    return 0


def _studio_usage_this_month(dealer_id):
    """Studio renders since the 1st. Returns 0 if the table is not present."""
    try:
        from models import StudioImage
        from datetime import datetime
        start = datetime.now().replace(day=1, hour=0, minute=0,
                                       second=0, microsecond=0)
        return StudioImage.query.filter(
            StudioImage.dealer_id == dealer_id,
            StudioImage.created_at >= start).count()
    except Exception:
        return 0


def next_plan_with_room(plan, resource, needed):
    """
    The cheapest plan that would fit `needed`, or None if even Pro will not.
    Used to say "upgrade to Growth", not just "upgrade".
    """
    try:
        start = PLAN_ORDER.index(normalise_plan(plan)) + 1
    except ValueError:
        start = 0
    for candidate in PLAN_ORDER[start:]:
        cap = limit_for(candidate, resource)
        if cap is UNLIMITED or cap >= needed:
            return candidate
    return None


def check_quota(dealer_id, plan, resource, adding=1):
    """
    Returns a dict describing the allowance:

        allowed    True when `adding` more would still fit
        used       current count
        limit      cap, or None for unlimited
        remaining  slots left, or None for unlimited
        upgrade_to next plan key that would fit, or None
        message    ready-to-show explanation when blocked
    """
    plan = normalise_plan(plan)
    limit = limit_for(plan, resource)
    used = usage_for(dealer_id, resource)

    if limit is UNLIMITED:
        return {'allowed': True, 'used': used, 'limit': None,
                'remaining': None, 'upgrade_to': None, 'message': None,
                'plan': plan, 'resource': resource}

    allowed = (used + adding) <= limit
    singular, plural = RESOURCE_LABELS.get(resource, (resource, resource))
    upgrade = None if allowed else next_plan_with_room(plan, resource, used + adding)

    message = None
    if not allowed:
        own = singular if limit == 1 else plural
        if upgrade:
            cap = limit_for(upgrade, resource)
            room = 'unlimited' if cap is UNLIMITED else '{}'.format(cap)
            theirs = singular if cap == 1 else plural
            message = ('Your {} plan covers {} {}, and you already have {}. '
                       'Upgrade to {} for {} {} to add more.').format(
                PLAN_DISPLAY[plan], limit, own, used,
                PLAN_DISPLAY[upgrade], room, theirs)
        else:
            message = 'You have reached the maximum of {} {} on {}.'.format(
                limit, own, PLAN_DISPLAY[plan])

    return {'allowed': allowed, 'used': used, 'limit': limit,
            'remaining': max(0, limit - used), 'upgrade_to': upgrade,
            'message': message, 'plan': plan, 'resource': resource}


def _wants_json():
    return (request.is_json
            or 'application/json' in (request.headers.get('Accept') or '')
            or '/api/' in request.path)


def quota_required(resource, adding=1):
    """
    Block a create route once the plan's allowance is spent.

    Stacks under the existing decorators:

        @dealer_bp.route('/inventory/add', methods=['GET', 'POST'])
        @dealer_required
        @kyc_required
        @quota_required('vehicles')
        def add_vehicle(): ...

    The check runs on POST only, so a dealer at their limit can still open the
    form and see why they are blocked rather than being bounced with no
    explanation. GET requests pass through untouched.
    """
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if request.method == 'GET':
                return view(*args, **kwargs)

            from dealer.routes import get_dealer_id
            from subscription_gate import current_plan_key

            dealer_id = get_dealer_id()
            if not dealer_id:
                return view(*args, **kwargs)

            try:
                plan = current_plan_key()
            except Exception:
                plan = 'starter'

            result = check_quota(dealer_id, plan, resource, adding=adding)
            if result['allowed']:
                return view(*args, **kwargs)

            if _wants_json():
                return jsonify({
                    'error': 'plan_limit_reached',
                    'message': result['message'],
                    'used': result['used'],
                    'limit': result['limit'],
                    'upgrade_to': result['upgrade_to'],
                    'redirect': url_for('dealer.subscription'),
                }), 403

            flash(result['message'], 'error')
            return redirect(url_for('dealer.subscription'))
        return wrapper
    return decorator


def quota_summary(dealer_id, plan):
    """Every allowance at once, for showing usage in the UI."""
    return {r: check_quota(dealer_id, plan, r, adding=0)
            for r in ('vehicles', 'agents', 'studio_monthly')}
