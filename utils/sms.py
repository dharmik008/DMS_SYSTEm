"""
utils/sms.py — send login codes by SMS.

Pick a provider with SMS_PROVIDER in .env:

  console   (default) Prints the code in the server window. For testing only;
            nobody receives an SMS. Use this until an SMS account is set up.
  fast2sms  Fast2SMS OTP route (India).   Needs FAST2SMS_API_KEY.
  msg91     MSG91 OTP API (India).        Needs MSG91_AUTH_KEY, MSG91_TEMPLATE_ID
            (a DLT-approved OTP template; MSG91 fills in the ##OTP## variable).
  twilio    Twilio Messages API (global). Needs TWILIO_ACCOUNT_SID,
            TWILIO_AUTH_TOKEN and TWILIO_FROM (your Twilio number).

send_otp_sms() returns (ok: bool, error: str | None). It never raises, so a
provider outage shows the person a clear message instead of a 500 page.
"""
import logging
import os

import requests

log = logging.getLogger(__name__)
TIMEOUT = 10  # seconds


def provider():
    return (os.environ.get('SMS_PROVIDER') or 'console').strip().lower()


def is_test_mode():
    return provider() == 'console'


def _message(code):
    return f'{code} is your Caryanams login code. It expires in 5 minutes. Do not share it with anyone.'


def _console(phone10, code):
    line = '─' * 46
    print(f'\n{line}\n  [TEST MODE] SMS to +91 {phone10}\n  Login code: {code}\n{line}\n', flush=True)
    return True, None


def _fast2sms(phone10, code):
    key = os.environ.get('FAST2SMS_API_KEY', '')
    if not key:
        return False, 'FAST2SMS_API_KEY is not set'
    r = requests.post(
        'https://www.fast2sms.com/dev/bulkV2',
        headers={'authorization': key},
        data={'route': 'otp', 'variables_values': code, 'numbers': phone10},
        timeout=TIMEOUT,
    )
    body = r.json() if r.headers.get('content-type', '').startswith('application/json') else {}
    if r.ok and body.get('return') is True:
        return True, None
    return False, f'Fast2SMS {r.status_code}: {body.get("message") or r.text[:200]}'


def _msg91(phone10, code):
    key = os.environ.get('MSG91_AUTH_KEY', '')
    template = os.environ.get('MSG91_TEMPLATE_ID', '')
    if not key or not template:
        return False, 'MSG91_AUTH_KEY and MSG91_TEMPLATE_ID must both be set'
    r = requests.post(
        'https://control.msg91.com/api/v5/otp',
        headers={'authkey': key, 'Content-Type': 'application/json'},
        params={'template_id': template, 'mobile': '91' + phone10, 'otp': code},
        json={},
        timeout=TIMEOUT,
    )
    body = r.json() if r.headers.get('content-type', '').startswith('application/json') else {}
    if r.ok and body.get('type') == 'success':
        return True, None
    return False, f'MSG91 {r.status_code}: {body.get("message") or r.text[:200]}'


def _twilio(phone10, code):
    sid = os.environ.get('TWILIO_ACCOUNT_SID', '')
    token = os.environ.get('TWILIO_AUTH_TOKEN', '')
    sender = os.environ.get('TWILIO_FROM', '')
    if not (sid and token and sender):
        return False, 'TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_FROM must be set'
    r = requests.post(
        f'https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json',
        auth=(sid, token),
        data={'To': '+91' + phone10, 'From': sender, 'Body': _message(code)},
        timeout=TIMEOUT,
    )
    if r.status_code in (200, 201):
        return True, None
    try:
        msg = r.json().get('message')
    except ValueError:
        msg = r.text[:200]
    return False, f'Twilio {r.status_code}: {msg}'


_PROVIDERS = {'console': _console, 'fast2sms': _fast2sms, 'msg91': _msg91, 'twilio': _twilio}


def send_otp_sms(phone10, code):
    name = provider()
    fn = _PROVIDERS.get(name)
    if not fn:
        log.error('Unknown SMS_PROVIDER %r; expected one of %s', name, ', '.join(_PROVIDERS))
        return False, f'Unknown SMS_PROVIDER "{name}"'
    try:
        ok, err = fn(phone10, code)
    except requests.RequestException as e:
        ok, err = False, f'{name} unreachable: {e}'
    except Exception as e:  # noqa: BLE001 — never let SMS take the page down
        ok, err = False, f'{name} error: {e}'
    if not ok:
        log.error('SMS send failed (%s) to ******%s: %s', name, phone10[-4:], err)
    return ok, err
