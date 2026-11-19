"""
utils/otp.py — one-time SMS codes for dealer and user login.

Rules (change the constants below if needed):
  * 6-digit code, valid for 5 minutes, usable once.
  * 5 wrong guesses kill the code; the person must request a new one.
  * "Resend" is allowed after 30 seconds.
  * At most 5 codes per mobile number per hour, and 20 per IP address per
    hour, so nobody can run up your SMS bill or spam a number.
  * Codes are stored only as a keyed hash (HMAC-SHA256 with the app secret).
"""
import hashlib
import hmac
import ipaddress
import secrets
from datetime import timedelta

from flask import current_app, request

CODE_TTL = timedelta(minutes=5)
MAX_ATTEMPTS = 5
RESEND_AFTER = timedelta(seconds=30)
MAX_PER_PHONE_PER_HOUR = 5
MAX_PER_IP_PER_HOUR = 20
LOGIN_ROLES = ('dealer', 'user')

_TOO_MANY = 'Too many codes requested. Please try again in an hour.'
_EXPIRED = 'This code has expired. Tap "Resend code" to get a new one.'
_TOO_MANY_TRIES = 'Too many wrong tries. Tap "Resend code" to get a new one.'


# ── Phone numbers ───────────────────────────────────────────────────────────
def normalise_phone(raw):
    """
    Return a 10-digit Indian mobile number, or None if it isn't one.
    Accepts 9876543210, +91 98765 43210, 091-9876543210, 09876543210 ...
    """
    digits = ''.join(c for c in str(raw or '') if c.isdigit())
    if len(digits) == 12 and digits.startswith('91'):
        digits = digits[2:]
    elif len(digits) == 13 and digits.startswith('091'):
        digits = digits[3:]
    elif len(digits) == 11 and digits.startswith('0'):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in '6789':
        return digits
    return None


def mask_phone(phone10):
    return f'+91 ••••• •{phone10[-4:]}' if phone10 else ''


def find_accounts_by_phone(phone10, roles=LOGIN_ROLES):
    """All dealer/user accounts whose stored phone is this number, oldest first."""
    from sqlalchemy import func
    from models import User
    if not phone10:
        return []
    # Older rows may store '98765 43210', '+91-9876543210', '(0)9876543210' ...
    # Strip separators in SQL (works on PostgreSQL and SQLite) to narrow the
    # search, then confirm each match exactly in Python.
    bare = User.phone
    for ch in (' ', '-', '+', '(', ')', '.'):
        bare = func.replace(bare, ch, '')
    rows = (User.query
            .filter(User.role.in_(roles), bare.like('%' + phone10))
            .order_by(User.id)
            .all())
    return [u for u in rows if normalise_phone(u.phone) == phone10]


def phone_in_use(phone10, exclude_user_id=None):
    return any(u.id != exclude_user_id for u in find_accounts_by_phone(phone10))


# ── Codes ───────────────────────────────────────────────────────────────────
def _hash(phone10, purpose, code):
    key = (current_app.secret_key or 'caryanams').encode()
    return hmac.new(key, f'{phone10}:{purpose}:{code}'.encode(), hashlib.sha256).hexdigest()


def _client_ip():
    """
    Address used for the per-IP send limit.

    X-Forwarded-For and similar headers are whatever the client sends unless a
    proxy overwrites them, so a public remote_addr (no proxy, or proxies
    configured with TRUSTED_PROXY_COUNT in app.py) is used as is. Headers are
    only consulted when the connection comes from a private address, i.e. an
    unconfigured local proxy, where remote_addr would lump everyone together.
    """
    from utils.ip_utils import get_real_ip
    remote = request.remote_addr or ''
    try:
        if ipaddress.ip_address(remote).is_global:
            return remote[:64]
    except ValueError:
        pass
    return get_real_ip(request)[:64]


def _now():
    from models import _now_ist
    return _now_ist()


def issue_otp(phone10, purpose):
    """
    Create and send a code.
    Returns (ok, message, wait_seconds). message is safe to show the person.
    """
    from extensions import db
    from models import LoginOTP
    from utils.sms import send_otp_sms

    now = _now()
    hour_ago = now - timedelta(hours=1)
    ip = _client_ip()

    # Housekeeping: drop rows older than a day.
    LoginOTP.query.filter(LoginOTP.created_at < now - timedelta(days=1)).delete(synchronize_session=False)

    last = (LoginOTP.query.filter_by(phone=phone10, purpose=purpose)
            .order_by(LoginOTP.created_at.desc()).first())
    if last and now - last.created_at < RESEND_AFTER:
        wait = int((RESEND_AFTER - (now - last.created_at)).total_seconds()) + 1
        db.session.commit()
        return False, f'Please wait {wait} seconds before requesting another code.', wait

    sent_to_phone = LoginOTP.query.filter(LoginOTP.phone == phone10, LoginOTP.created_at >= hour_ago).count()
    sent_from_ip = LoginOTP.query.filter(LoginOTP.ip_address == ip, LoginOTP.created_at >= hour_ago).count() if ip else 0
    if sent_to_phone >= MAX_PER_PHONE_PER_HOUR or sent_from_ip >= MAX_PER_IP_PER_HOUR:
        db.session.commit()
        return False, _TOO_MANY, 0

    code = f'{secrets.randbelow(10 ** 6):06d}'
    row = LoginOTP(phone=phone10, purpose=purpose, code_hash=_hash(phone10, purpose, code),
                   ip_address=ip, created_at=now, expires_at=now + CODE_TTL)
    db.session.add(row)
    db.session.commit()

    refusal = _refusal_from_parallel_requests(row, hour_ago)
    if refusal:
        row.consumed = True
        db.session.commit()
        return refusal

    # Only the newest code for this number and purpose stays valid. Done after
    # the parallel-request check so a request that lost it can't invalidate
    # the code that is actually being sent.
    LoginOTP.query.filter(LoginOTP.phone == phone10, LoginOTP.purpose == purpose,
                          LoginOTP.id < row.id, LoginOTP.consumed.is_(False)) \
        .update({'consumed': True}, synchronize_session=False)
    db.session.commit()

    ok, _err = send_otp_sms(phone10, code)
    if not ok:
        row.consumed = True  # still counts toward the hourly limit
        db.session.commit()
        return False, "We couldn't send the SMS right now. Please try again in a minute.", 0
    return True, f'We sent a 6-digit code to {mask_phone(phone10)}.', int(RESEND_AFTER.total_seconds())


def _refusal_from_parallel_requests(row, hour_ago):
    """
    Re-apply the limits counting rows that other requests inserted at the same
    moment, which the checks before the insert could not see. Without this a
    burst of parallel requests would all pass and each send an SMS. The
    earliest row (lowest id) wins. Returns issue_otp's refusal tuple, or None.
    """
    from models import LoginOTP
    earlier = LoginOTP.query.filter(LoginOTP.id < row.id)
    racing = earlier.filter(LoginOTP.phone == row.phone, LoginOTP.purpose == row.purpose,
                            LoginOTP.created_at > row.created_at - RESEND_AFTER).count()
    if racing:
        wait = int(RESEND_AFTER.total_seconds())
        return False, f'Please wait {wait} seconds before requesting another code.', wait
    to_phone = earlier.filter(LoginOTP.phone == row.phone, LoginOTP.created_at >= hour_ago).count()
    from_ip = (earlier.filter(LoginOTP.ip_address == row.ip_address, LoginOTP.created_at >= hour_ago).count()
               if row.ip_address else 0)
    if to_phone >= MAX_PER_PHONE_PER_HOUR or from_ip >= MAX_PER_IP_PER_HOUR:
        return False, _TOO_MANY, 0
    return None


def verify_otp(phone10, purpose, code):
    """Returns (ok, message). On success the code is used up."""
    from extensions import db
    from models import LoginOTP

    code = ''.join(c for c in str(code or '') if c.isdigit())
    if len(code) != 6:
        return False, 'Enter the 6-digit code from the SMS.'

    row = (LoginOTP.query.filter_by(phone=phone10, purpose=purpose, consumed=False)
           .order_by(LoginOTP.created_at.desc()).first())
    if not row or row.expires_at < _now():
        return False, _EXPIRED
    row_id, code_hash = row.id, row.code_hash

    # Count the attempt before comparing, in a single conditional UPDATE.
    # Reading attempts and writing it back separately would let parallel
    # requests all see "4 tries used" and each get a free guess.
    claimed = (LoginOTP.query
               .filter(LoginOTP.id == row_id, LoginOTP.consumed.is_(False),
                       LoginOTP.attempts < MAX_ATTEMPTS)
               .update({LoginOTP.attempts: LoginOTP.attempts + 1}, synchronize_session=False))
    db.session.commit()
    if not claimed:
        return False, _TOO_MANY_TRIES

    if hmac.compare_digest(code_hash, _hash(phone10, purpose, code)):
        # Conditional again, so two parallel correct submissions can't both succeed.
        used = (LoginOTP.query.filter(LoginOTP.id == row_id, LoginOTP.consumed.is_(False))
                .update({LoginOTP.consumed: True}, synchronize_session=False))
        db.session.commit()
        return (True, None) if used else (False, _EXPIRED)

    left = MAX_ATTEMPTS - db.session.get(LoginOTP, row_id).attempts
    if left <= 0:
        return False, _TOO_MANY_TRIES
    return False, f'That code is not correct. {left} {"try" if left == 1 else "tries"} left.'


def seconds_until_resend(phone10, purpose):
    from models import LoginOTP
    last = (LoginOTP.query.filter_by(phone=phone10, purpose=purpose)
            .order_by(LoginOTP.created_at.desc()).first())
    if not last:
        return 0
    left = (RESEND_AFTER - (_now() - last.created_at)).total_seconds()
    return max(0, int(left) + 1) if left > 0 else 0
