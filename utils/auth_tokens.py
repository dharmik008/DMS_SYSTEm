"""
utils/auth_tokens.py — one sign-in token per account type.

Before: dealers and users shared one slot (session['user_id']), so signing in
as a buyer signed the dealer out, and logging out wiped the admin login too.

Now each account type has its own signed, HttpOnly cookie:

    cy_dealer   the dealer signed in on this browser
    cy_user     the buyer (user) signed in on this browser
    (admin)     unchanged — Super Admin / Sub-Admin keep their own session keys

So one browser can have an admin, a dealer and a user signed in at once, each
in its own tab, and signing out of one leaves the others alone.

Which token a page uses depends on where the page is:

    /dealer/…, /studio/…      dealer token only
    /admin/…                  neither (admin pages use the admin login)
    /portal                   ?as=dealer|user if given, else dealer, then user
    everything else           user token, falling back to the dealer token
                              (so a dealer browsing the marketplace or their
                              own website still appears signed in, as before)

Tokens are signed with the app secret (tamper-proof), expire after 30 days,
and are checked against the database on every request, so a deleted or
suspended account is signed out immediately.
"""
from flask import current_app, g, request, session
from itsdangerous import BadSignature, URLSafeTimedSerializer

ROLES = ('dealer', 'user')
COOKIE = {'dealer': 'cy_dealer', 'user': 'cy_user'}
MAX_AGE = 30 * 24 * 3600  # seconds a token stays valid
_SALT = 'caryanams-signin-v1'


def _serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt=_SALT)


def _ops():
    if not hasattr(g, '_auth_cookie_ops'):
        g._auth_cookie_ops = {}
    return g._auth_cookie_ops


# ── Reading ─────────────────────────────────────────────────────────────────
def token_uid(role):
    """User id held by this role's token (including changes made this request)."""
    ops = _ops()
    if role in ops:                      # signed in/out earlier in this request
        return ops[role]
    raw = request.cookies.get(COOKIE[role])
    if not raw:
        return None
    try:
        data = _serializer().loads(raw, max_age=MAX_AGE)
    except BadSignature:                 # also covers expired tokens
        return None
    if not isinstance(data, dict) or data.get('r') != role:
        return None
    return data.get('u')


def roles_for_request():
    path = request.path or '/'
    if path.startswith(('/dealer/', '/studio/')) or path in ('/dealer', '/studio'):
        return ('dealer',)
    if path.startswith('/admin'):
        return ()
    if path.startswith('/portal'):
        wanted = request.args.get('as')
        if wanted in ROLES:
            return (wanted,)
        return ('dealer', 'user')
    return ('user', 'dealer')


def current_uid():
    """The signed-in dealer/user id that applies to this page, or None."""
    for role in roles_for_request():
        uid = token_uid(role)
        if uid:
            return uid
    return None


def signed_in_roles():
    return [r for r in ROLES if token_uid(r)]


# ── Writing ─────────────────────────────────────────────────────────────────
def sign_in(role, uid):
    """Give this browser a token for `role`. Takes effect on this response."""
    if role not in ROLES:
        raise ValueError(f'unknown role {role!r}')
    _ops()[role] = uid


def sign_out(role):
    if role in ROLES:
        _ops()[role] = None


def _apply(response):
    ops = getattr(g, '_auth_cookie_ops', None)
    if not ops:
        return response
    secure = request.is_secure
    for role, uid in ops.items():
        if uid:
            response.set_cookie(COOKIE[role], _serializer().dumps({'r': role, 'u': uid}),
                                httponly=True, samesite='Lax', secure=secure, path='/')
        else:
            response.delete_cookie(COOKIE[role], path='/', samesite='Lax', secure=secure)
    return response


def init_app(app):
    """Load g.user from the right token before each request; write token changes after."""
    from db import user_get_by_id

    @app.before_request
    def _load_user_from_tokens():
        # One-time upgrade: a browser still signed in the old way keeps its login.
        legacy = session.pop('user_id', None)
        session.pop('user_role', None)
        if legacy:
            row = user_get_by_id(legacy)
            if row and row.get('role') in ROLES:
                sign_in(row['role'], legacy)

        g.user = None
        for role in roles_for_request():
            uid = token_uid(role)
            if not uid:
                continue
            row = user_get_by_id(uid)
            if row and row.get('role') == role:
                g.user = row
                break
            sign_out(role)  # account deleted, or role changed: drop the stale token

    app.after_request(_apply)
