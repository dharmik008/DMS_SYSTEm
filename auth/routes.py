from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from db import user_get_by_email, user_create
from models import db, User

auth_bp = Blueprint('auth', __name__)


def _log_auth_action(action, module, status='Success', user=None, description=None):
    """
    Shared activity logger for the auth blueprint. Captures real client IP
    (proxy/Cloudflare aware) and device/browser metadata. Best-effort and
    silent on failure so it never blocks login/logout/registration.
    """
    try:
        from models import AdminLog
        from utils.request_meta import get_request_meta
        ip, browser, os_name, device = get_request_meta(request)
        role_label = 'Dealer' if (user and getattr(user, 'role', None) == 'dealer') else 'Admin'
        log = AdminLog(
            user_id=user.id if user else None,
            admin_user=(user.name if user and user.name else (user.email if user else 'unknown')),
            user_role=role_label,
            action=action,
            module=module,
            description=description or action,
            ip_address=ip,
            device=device,
            browser=browser,
            timezone='Asia/Kolkata (IST)',
            status=status,
        )
        db.session.add(log)
        db.session.commit()
    except Exception:
        pass


@auth_bp.before_request
def track_visitor():
    """Record public auth page visits (login, register) in visitor_logs."""
    from utils.visitor_tracker import log_visit
    log_visit(request)


@auth_bp.route('/logout')
def logout():
    """
    Sign out ONE account: the dealer or the user (?as=dealer|user). Anything
    else signed in on this browser, including the admin, stays signed in.
    Without ?as, the page the person came from decides (a dealer page means
    the dealer), and failing that whoever this page shows.
    """
    from flask import g
    from urllib.parse import urlparse
    from utils.auth_tokens import ROLES, token_uid, sign_out
    return_url = request.args.get('returnUrl', '').strip()
    role = request.args.get('as')
    if role not in ROLES:
        ref = urlparse(request.referrer or '').path
        if ref.startswith(('/dealer/', '/studio/')) and token_uid('dealer'):
            role = 'dealer'
        elif g.user:
            role = g.user.get('role')
    uid = token_uid(role) if role in ROLES else None
    if uid:
        try:
            user = User.query.get(uid)
            if user:
                role_label = 'Dealer' if user.role == 'dealer' else 'Admin'
                _log_auth_action(f'{role_label} logged out', 'Auth', user=user)
        except Exception:
            pass
        sign_out(role)
    for key in ('otp_flow', 'minisite_return_url') + (('phone_change',) if role == 'dealer' else ()):
        session.pop(key, None)
    flash('Logged out successfully', 'success')
    if return_url and (return_url.startswith('/dealer/') or return_url.startswith('/caryanams/')):
        return redirect(return_url)
    return redirect(url_for('auth.login'))

@auth_bp.route('/role-select')
def role_select():
    return render_template('auth/role_select.html')


# ═══════════════════════════════════════════════════════════════════════════
#  OTP LOGIN & SIGN-UP (dealers and users)
#
#  Dealers and users sign in with their mobile number and a 6-digit SMS code;
#  there is no password. Super Admin / Sub-Admin logins (admin blueprint) are
#  unchanged.
#
#  Every page here runs the same small state machine, kept in
#  session['otp_flow']:
#     phone  ──send──▶  code  ──verify──▶  (choose account, if the number is
#                                           shared by several) ──▶ signed in
#  A plain GET always starts over at the first step.
# ═══════════════════════════════════════════════════════════════════════════
import secrets
import time

from utils.otp import (normalise_phone, mask_phone, find_accounts_by_phone,
                       phone_in_use, issue_otp, verify_otp, seconds_until_resend)
from utils.sms import is_test_mode

_FLOW = 'otp_flow'
_CHOOSE_WINDOW = 600  # seconds a verified code may be used to pick an account
_SAFE_RETURN = ('/dealer/', '/caryanams/')


def _flow(kind, ctx):
    f = session.get(_FLOW)
    return f if f and f.get('kind') == kind and f.get('ctx') == ctx else None


def _clear_flow():
    session.pop(_FLOW, None)


def _otp_view(template, step, flow=None, **kw):
    """Render an auth page at the given step with the shared OTP variables."""
    phone = (flow or {}).get('phone')
    kw.setdefault('otp_accounts', [])
    return render_template(
        template,
        otp_step=step,
        otp_phone_masked=mask_phone(phone) if phone else '',
        otp_wait=seconds_until_resend(phone, flow['purpose']) if phone and step == 'code' else 0,
        otp_test_mode=is_test_mode(),
        **kw,
    )


def _send(flow):
    """Send (or resend) the code for this flow and flash the outcome."""
    ok, msg, wait = issue_otp(flow['phone'], flow['purpose'])
    if ok:
        flash(msg, 'success')
    else:
        flash(msg, 'error')
    # A cooldown means a valid code is already on its way, so the code step
    # still makes sense. A hard failure (SMS down, hourly limit) does not.
    return ok or wait > 0


def _stash_return_url(value):
    value = (value or '').strip()
    if value.startswith(_SAFE_RETURN):
        session['minisite_return_url'] = value
    else:
        session.pop('minisite_return_url', None)


def _account_choices(users):
    return [{'id': u.id,
             'name': u.name,
             'role': 'Dealer' if u.role == 'dealer' else 'User',
             'detail': (u.company_name if u.role == 'dealer' and u.company_name else
                        (u.email[:2] + '•••' + u.email[u.email.find('@'):] if u.email and '@' in u.email else ''))}
            for u in users]


def _otp_login(ctx, template, template_kw, finish):
    """
    Shared login handler. `finish(user)` returns the response after a
    successful sign-in (it differs between the main and dealer-link pages).
    """
    if request.method == 'GET':
        _clear_flow()
        return _otp_view(template, 'phone', **template_kw)

    action = request.form.get('action', 'send')
    flow = _flow('login', ctx)

    if action == 'send':
        if template_kw.get('require_policy') and not request.form.get('policy_agreed'):
            flash('Please accept the Privacy Policy and Refund Policy to continue.', 'error')
            return _otp_view(template, 'phone', **template_kw)
        phone = normalise_phone(request.form.get('phone'))
        if not phone:
            flash('Please enter a valid 10-digit mobile number.', 'error')
            return _otp_view(template, 'phone', **template_kw)
        if not find_accounts_by_phone(phone):
            _log_auth_action('Failed login attempt', 'Auth', status='Failed',
                             description=f'OTP login requested for unregistered number ******{phone[-4:]} ({ctx})')
            flash('No account uses this mobile number. Check the number, or create an account.', 'error')
            return _otp_view(template, 'phone', **template_kw)
        flow = {'kind': 'login', 'ctx': ctx, 'purpose': 'login', 'phone': phone}
        session[_FLOW] = flow
        return _otp_view(template, 'code' if _send(flow) else 'phone', flow, **template_kw)

    if not flow:  # session expired or page opened in another tab
        flash('Your session timed out. Please enter your mobile number again.', 'error')
        return _otp_view(template, 'phone', **template_kw)

    if action == 'resend':
        _send(flow)
        return _otp_view(template, 'code', flow, **template_kw)

    if action == 'verify':
        ok, msg = verify_otp(flow['phone'], 'login', request.form.get('code'))
        if not ok:
            _log_auth_action('Failed login attempt', 'Auth', status='Failed',
                             description=f'Wrong/expired OTP for ******{flow["phone"][-4:]} ({ctx})')
            flash(msg, 'error')
            return _otp_view(template, 'code', flow, **template_kw)
        users = find_accounts_by_phone(flow['phone'])
        if len(users) == 1:
            _clear_flow()
            return finish(users[0])
        if not users:
            _clear_flow()
            flash('No account uses this mobile number any more. Please contact support.', 'error')
            return _otp_view(template, 'phone', **template_kw)
        flow.update(verified_ids=[u.id for u in users], verified_at=int(time.time()))
        session[_FLOW] = flow
        return _otp_view(template, 'choose', flow, otp_accounts=_account_choices(users), **template_kw)

    if action == 'choose':
        ids = flow.get('verified_ids') or []
        fresh = int(time.time()) - int(flow.get('verified_at') or 0) < _CHOOSE_WINDOW
        try:
            chosen = int(request.form.get('user_id', ''))
        except ValueError:
            chosen = None
        if not fresh or chosen not in ids:
            _clear_flow()
            flash('Please sign in again.', 'error')
            return _otp_view(template, 'phone', **template_kw)
        user = User.query.get(chosen)
        # Re-check: the phone on the account must still be this number.
        if not user or normalise_phone(user.phone) != flow['phone']:
            _clear_flow()
            flash('Please sign in again.', 'error')
            return _otp_view(template, 'phone', **template_kw)
        _clear_flow()
        return finish(user)

    return _otp_view(template, 'phone', **template_kw)


def _sign_in(user, how):
    """
    Give this browser the sign-in token for the account's type (dealer or
    user). Other accounts signed in on this browser are left alone.
    Returns False if the account may not sign in.
    """
    from utils.auth_tokens import sign_in
    if user.role == 'dealer' and not user.is_active:
        flash('Your account has been suspended. Please contact the admin.', 'error')
        return False
    if user.role not in ('dealer', 'user'):
        flash('This account cannot sign in here.', 'error')
        return False
    sign_in(user.role, user.id)
    role_label = 'Dealer' if user.role == 'dealer' else 'Admin'
    _log_auth_action(f'{role_label} logged in{how}', 'Auth', user=user,
                     description=f'{role_label} logged in{how} with SMS OTP')
    flash('Login successful!', 'success')
    return True


# ── Main login ────────────────────────────────────────────────────────────────
@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        _stash_return_url(request.args.get('returnUrl'))
    elif request.form.get('action', 'send') == 'send' and request.form.get('return_url'):
        _stash_return_url(request.form.get('return_url'))

    def finish(user):
        if not _sign_in(user, ''):
            return _otp_view('auth/login.html', 'phone', require_policy=True)
        # If the user came from a dealer minisite, send them back there
        minisite_url = session.pop('minisite_return_url', None)
        if minisite_url and minisite_url.startswith(_SAFE_RETURN):
            return redirect(minisite_url)
        # v29: every authenticated path funnels through the SaaS Portal,
        # which decides dashboard vs plan selection. ?as= tells it which of
        # the browser's signed-in accounts this is.
        return redirect(url_for('portal.portal', **{'as': user.role}))

    return _otp_login('main', 'auth/login.html', {'require_policy': True}, finish)


# ── Dealer-link login ─────────────────────────────────────────────────────────
def _dealer_for_link(website_name):
    dealer = User.query.filter(User.role == 'dealer', User.website_name.ilike(website_name)).first()
    if not dealer:
        dealer = User.query.filter(User.role == 'dealer', User.name.ilike(website_name)).first()
    return dealer


@auth_bp.route('/dealer-login/<website_name>', methods=['GET', 'POST'])
def dealer_login(website_name):
    """
    Dealer-link login page: someone who opened a dealer's shared link signs in
    here with their mobile number. The dealer's branding is shown on the page.
    """
    dealer = _dealer_for_link(website_name)
    if not dealer:
        flash('Invalid dealer link.', 'error')
        return redirect(url_for('auth.login'))
    kw = {'dealer': dealer, 'website_name': website_name}

    def finish(user):
        if not _sign_in(user, f' via dealer link ({website_name})'):
            return _otp_view('auth/dealer_login.html', 'phone', **kw)
        # v29: dealers go through the portal so the plan check cannot be
        # side-stepped via a dealer's shared link; users go to the minisite.
        if user.role == 'dealer':
            return redirect(url_for('portal.portal', **{'as': 'dealer'}))
        # (The original code omitted dealer_name, which the minisite URL
        # requires, so this redirect used to fail with an error page.)
        return redirect(url_for('minisite.home', dealer_name=dealer.name, website_name=website_name))

    return _otp_login(f'link:{website_name.lower()}', 'auth/dealer_login.html', kw, finish)


# ── Sign-up (shared) ──────────────────────────────────────────────────────────
def _otp_register(ctx, template, template_kw, collect, create):
    """
    Shared sign-up handler.
      collect() -> (data dict | None). Reads and validates the form; flashes
                   and returns None on a problem. data must include 'phone'.
      create(data) -> response. Creates the account after the code is verified.
    """
    if request.method == 'GET':
        _clear_flow()
        return _otp_view(template, 'phone', **template_kw)

    action = request.form.get('action', 'send')
    flow = _flow('register', ctx)

    if action == 'send':
        data = collect()
        if not data:
            return _otp_view(template, 'phone', form=request.form, **template_kw)
        flow = {'kind': 'register', 'ctx': ctx, 'purpose': 'register', 'phone': data['phone'], 'data': data}
        session[_FLOW] = flow
        if _send(flow):
            return _otp_view(template, 'code', flow, **template_kw)
        return _otp_view(template, 'phone', form=request.form, **template_kw)

    if not flow:
        flash('Your session timed out. Please fill in the form again.', 'error')
        return _otp_view(template, 'phone', **template_kw)

    if action == 'resend':
        _send(flow)
        return _otp_view(template, 'code', flow, **template_kw)

    if action == 'verify':
        ok, msg = verify_otp(flow['phone'], 'register', request.form.get('code'))
        if not ok:
            flash(msg, 'error')
            return _otp_view(template, 'code', flow, **template_kw)
        data = dict(flow['data'])
        _clear_flow()
        # Someone may have taken the email or number while the SMS was in flight.
        if user_get_by_email(data['email']):
            flash('This email is already registered. Please sign in.', 'error')
            return _otp_view(template, 'phone', **template_kw)
        if phone_in_use(data['phone']):
            flash('This mobile number is already registered. Please sign in.', 'error')
            return _otp_view(template, 'phone', **template_kw)
        return create(data)

    return _otp_view(template, 'phone', **template_kw)


def _collect_common():
    name = (request.form.get('name') or '').strip()
    email = (request.form.get('email') or '').strip().lower()
    phone = normalise_phone(request.form.get('phone'))
    if not name or not email:
        flash('Name and email are required.', 'error')
        return None
    if '@' not in email or '.' not in email.split('@')[-1]:
        flash('Please enter a valid email address.', 'error')
        return None
    if not phone:
        flash('Please enter a valid 10-digit mobile number.', 'error')
        return None
    if user_get_by_email(email):
        flash('This email is already registered. Please sign in.', 'error')
        return None
    if phone_in_use(phone):
        flash('This mobile number is already registered. Please sign in.', 'error')
        return None
    return {'name': name, 'email': email, 'phone': phone}


def _new_account(data):
    """Create the user. Login is by SMS code, so the stored password is random and unused."""
    return user_create(dict(data, phone='+91' + data['phone'], password=secrets.token_urlsafe(32)))


# ── Main sign-up ──────────────────────────────────────────────────────────────
@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'GET':
        _stash_return_url(request.args.get('returnUrl'))
    elif request.form.get('action', 'send') == 'send':
        _stash_return_url(request.form.get('return_url'))

    def collect():
        data = _collect_common()
        if not data:
            return None
        role = request.form.get('role')
        if role not in ('dealer', 'user'):
            flash('Please choose Dealer or User.', 'error')
            return None
        data.update(role=role,
                    city=(request.form.get('city') or '').strip(),
                    company_name=(request.form.get('company_name') or '').strip() if role == 'dealer' else '')
        return data

    def create(data):
        user_id = _new_account(data)
        # user_create returns None when the database rejected the address as a
        # duplicate — someone claimed it between the check and the insert.
        if not user_id:
            flash('This email is already registered. Please sign in.', 'error')
            return redirect(url_for('auth.register'))
        new_user = User.query.get(user_id)
        try:
            role_label = 'Dealer' if data['role'] == 'dealer' else 'Admin'
            _log_auth_action(
                f'New {role_label.lower()} registered: {data["name"]}', 'Auth', user=new_user,
                description=f'New user registered — name="{data["name"]}", email="{data["email"]}", '
                            f'role="{data["role"]}", mobile verified by SMS')
        except Exception:
            pass

        # The mobile number was just verified by SMS, so the new account is
        # signed straight in; there is no second trip through the login page.
        from utils.auth_tokens import sign_in
        sign_in(data['role'], user_id)
        flash('Account created successfully!', 'success')
        minisite_url = session.pop('minisite_return_url', None)
        if minisite_url and minisite_url.startswith(_SAFE_RETURN):
            return redirect(minisite_url)   # back to the dealer website they came from
        # v29: Sign Up → SaaS Portal (plan selection for a new dealer,
        # marketplace for a buyer).
        return redirect(url_for('portal.portal', **{'as': data['role']}))

    return _otp_register('main', 'auth/register.html', {}, collect, create)


# ── Dealer-link sign-up ───────────────────────────────────────────────────────
# A dealer shares /auth/dealer-register/<website_name>; whoever signs up there
# is recorded as referred by that dealer, then signs in with their mobile.
@auth_bp.route('/dealer-register/<website_name>', methods=['GET', 'POST'])
def dealer_register(website_name):
    dealer = _dealer_for_link(website_name)
    if not dealer:
        flash('Invalid dealer link. Please contact the dealer for a valid link.', 'error')
        return redirect(url_for('auth.login'))
    kw = {'dealer': dealer, 'website_name': website_name}

    def collect():
        data = _collect_common()
        if data:
            data.update(role='user', city='', company_name='')
        return data

    def create(data):
        if not _new_account(data):
            flash('This email is already registered. Please sign in.', 'error')
            return _otp_view('auth/dealer_register.html', 'phone', **kw)
        new_user = user_get_by_email(data['email'])
        from utils.auth_tokens import sign_in
        sign_in('user', new_user.id)
        _log_auth_action(f'New user registered via dealer link ({website_name})', 'Auth', user=new_user)
        flash(f'Welcome! Your account was created via {dealer.website_name or dealer.name}.', 'success')
        return redirect(url_for('minisite.home', dealer_name=dealer.name, website_name=website_name))

    return _otp_register(f'link:{website_name.lower()}', 'auth/dealer_register.html', kw, collect, create)


# ── Old password-reset URLs ───────────────────────────────────────────────────
# Passwords are no longer used for dealer/user login. Old bookmarks and links
# land on the login page instead of a dead end.
@auth_bp.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    flash('Passwords are no longer needed. Sign in with your mobile number and the code we text you.', 'info')
    return redirect(url_for('auth.login'))


@auth_bp.route('/api/forgot-password/verify', methods=['POST'])
def forgot_password_verify():
    from flask import jsonify
    return jsonify({'success': False, 'error': 'Password reset has been replaced by SMS login.'}), 410


@auth_bp.route('/api/forgot-password/reset', methods=['POST'])
def forgot_password_reset():
    from flask import jsonify
    return jsonify({'success': False, 'error': 'Password reset has been replaced by SMS login.'}), 410
