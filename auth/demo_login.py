"""
auth/demo_login.py — TEMPORARY dealer demo login.

Lets dealers sign in with a fixed mobile number (9999999999) and OTP while
the real SMS OTP API is not available yet. It lives apart from the SMS login
in auth/routes.py so it can be deleted in one go: the demo number and OTP only
work in the demo form, and the SMS login is unchanged.

It stays OFF unless all of these hold when the app starts:
  * DEMO_LOGIN_ENABLED=True in .env
  * DEMO_LOGIN_OTP is a 6-digit code (it is never hardcoded here)
  * SMS_PROVIDER is "console" (SMS test mode). Setting up a real SMS provider
    therefore switches the demo login off on its own.
When off, the route is not registered at all: /auth/demo-login is a 404.

The demo signs in to an existing dealer account (DEMO_DEALER_EMAIL, by default
the sample dealer from models.seed_demo_data) with the same sign-in token as
the SMS login. Plan and KYC checks, sessions and logout therefore behave
exactly as they do for a real dealer login.

To remove it: delete this file and templates/auth/_demo_login.html, the
init_app() call in app.py, the include in templates/auth/login.html, and the
DEMO_* lines in .env and .env.example.
"""
from __future__ import annotations

import hmac
import logging
import os
import re
from dataclasses import dataclass
from datetime import timedelta

from flask import Blueprint, Flask, current_app, flash, redirect, request, url_for
from werkzeug.wrappers import Response

from auth.routes import _log_auth_action
from db import user_get_by_email
from models import AdminLog, _now_ist
from utils.auth_tokens import sign_in
from utils.otp import normalise_phone
from utils.sms import is_test_mode, provider

log = logging.getLogger(__name__)

DEMO_MOBILE = '9999999999'  # 10 digits without +91, the form normalise_phone() returns
DEFAULT_DEALER_EMAIL = 'dealer@caryanams.com'  # sample dealer created by seed_demo_data()

# A fixed code never expires or dies after 5 wrong tries the way an SMS code
# does, so wrong guesses are capped across all clients. A per-IP limit alone
# would not hold: utils/ip_utils.py trusts proxy headers, which can be faked.
MAX_FAILED_ATTEMPTS = 10
LOCKOUT_MINUTES = 15
FAILED_ACTION = 'Failed demo login attempt'

demo_login_bp = Blueprint('demo_login', __name__)


@dataclass(frozen=True)
class DemoLoginSettings:
    otp: str
    dealer_email: str


def _load_settings() -> DemoLoginSettings | None:
    """Demo login settings from the environment, or None if it must stay off."""
    if os.environ.get('DEMO_LOGIN_ENABLED', 'False') != 'True':
        return None
    if not is_test_mode():
        log.warning('Dealer demo login NOT enabled: it only runs in SMS test mode '
                    '(SMS_PROVIDER=console), but SMS_PROVIDER is %r.', provider())
        return None
    otp = os.environ.get('DEMO_LOGIN_OTP', '').strip()
    if not re.fullmatch(r'[0-9]{6}', otp):
        log.warning('Dealer demo login NOT enabled: DEMO_LOGIN_OTP must be exactly 6 digits.')
        return None
    dealer_email = os.environ.get('DEMO_DEALER_EMAIL', '').strip() or DEFAULT_DEALER_EMAIL
    return DemoLoginSettings(otp=otp, dealer_email=dealer_email)


def init_app(app: Flask) -> None:
    """Register the demo login route if the environment allows it."""
    settings = _load_settings()
    app.config['DEMO_LOGIN_ENABLED'] = settings is not None
    if settings is None:
        return
    app.extensions['demo_login'] = settings
    app.register_blueprint(demo_login_bp, url_prefix='/auth')
    log.warning('TEMPORARY dealer demo login is ENABLED (signs in to %s). '
                'Set DEMO_LOGIN_ENABLED=False before going live.', settings.dealer_email)


def _credentials_match(phone: str, otp: str, settings: DemoLoginSettings) -> bool:
    # Check both values every time, in constant time, so neither the response
    # nor its timing reveals which one was wrong.
    phone_ok = hmac.compare_digest(phone.encode(), DEMO_MOBILE.encode())
    otp_ok = hmac.compare_digest(otp.encode(), settings.otp.encode())
    return phone_ok and otp_ok


def _recent_failures() -> int:
    since = _now_ist() - timedelta(minutes=LOCKOUT_MINUTES)
    return (AdminLog.query
            .filter(AdminLog.action == FAILED_ACTION, AdminLog.created_at >= since)
            .count())


def _back_to_form(message: str) -> Response:
    flash(message, 'error')
    return redirect(url_for('auth.login', demo=1))


@demo_login_bp.route('/demo-login', methods=['GET', 'POST'])
def login() -> Response:
    if request.method == 'GET':
        return redirect(url_for('auth.login', demo=1))

    settings: DemoLoginSettings = current_app.extensions['demo_login']
    phone = normalise_phone(request.form.get('phone'))
    otp = re.sub(r'[^0-9]', '', request.form.get('otp') or '')

    if phone is None:
        return _back_to_form('Please enter a valid 10-digit mobile number.')
    if len(otp) != 6:
        return _back_to_form('Enter the 6-digit demo OTP.')
    if _recent_failures() >= MAX_FAILED_ATTEMPTS:
        return _back_to_form('Too many failed demo login attempts. '
                             f'Please try again in {LOCKOUT_MINUTES} minutes.')
    if not _credentials_match(phone, otp, settings):
        _log_auth_action(FAILED_ACTION, 'Auth', status='Failed',
                         description='Wrong mobile number or OTP on the dealer demo login')
        return _back_to_form('Incorrect mobile number or OTP.')

    dealer = user_get_by_email(settings.dealer_email)
    if dealer is None or dealer.role != 'dealer':
        log.error('Dealer demo login: no dealer account has the email in DEMO_DEALER_EMAIL (%s).',
                  settings.dealer_email)
        return _back_to_form('The demo dealer account is not available. Please contact the admin.')
    if not dealer.is_active:
        return _back_to_form('Your account has been suspended. Please contact the admin.')

    sign_in('dealer', dealer.id)
    _log_auth_action('Dealer logged in via demo login', 'Auth', user=dealer,
                     description='Dealer logged in with the temporary demo login (no SMS OTP)')
    flash('Login successful!', 'success')
    # Same landing point as the SMS login: the portal picks dashboard or plan selection.
    return redirect(url_for('portal.portal', **{'as': 'dealer'}))
