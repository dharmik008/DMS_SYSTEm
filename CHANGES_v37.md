# v37 — SMS OTP login: security fixes and test coverage

The SMS OTP system (login, sign-up, dealer mobile-number change) was already
built in (`utils/otp.py`, `utils/sms.py`, `auth/routes.py`,
`templates/auth/_otp_steps.html`). Nothing was replaced; these are fixes.

## Fixes
- **Secret key was public.** `app.py` hardcoded the key, and `.env` had the
  same value, so `SECRET_KEY` was never read. That key signs the `cy_dealer` /
  `cy_user` sign-in cookies, so anyone with the code could forge a cookie and
  sign in as any account without an OTP. The app now reads `SECRET_KEY`, and
  if it is blank or a value that has shipped in this project, it creates a
  random key in `instance/secret_key` on first start.
- **Parallel wrong guesses.** `verify_otp` read the attempt count, compared,
  then wrote it back, so parallel requests each got a guess: 40 at once
  produced 24–32 evaluated guesses against one code (limit 5). The attempt is
  now counted in one conditional `UPDATE` before comparing, and a correct code
  is used up the same way, so it can't succeed twice.
- **Parallel send requests.** Requests arriving together all passed the
  resend and hourly checks and each sent an SMS. After inserting its row, a
  request now re-checks against rows inserted alongside it; the earliest wins.
- **Per-IP limit could be dodged** by sending a different `X-Forwarded-For`
  each time. The limit now uses the connection address when it is public, and
  `TRUSTED_PROXY_COUNT` enables Werkzeug's `ProxyFix` behind a reverse proxy.
- The SMS text said "expires in 5 minutes" independently of `CODE_TTL`.
- The login pages rejected `091…` numbers in the browser that the server accepts.
- **MSG91 reported success before the SMS was sent.** The request now passes
  `realTimeResponse=1`, so a failed delivery shows "We couldn't send the SMS"
  and logs MSG91's reason instead of "We sent a code". It also passes
  `otp_expiry` (matching `CODE_TTL`) and ignores stray spaces around the
  MSG91 key and template ID in `.env`.

## Files
Changed: `app.py`, `utils/otp.py`, `utils/sms.py`, `templates/auth/login.html`,
`templates/auth/dealer_login.html`, `.env`, `.env.example`.
New: `test_otp.py`. No new dependencies, no database changes.

## Configuration
| Variable | Value |
|---|---|
| `SMS_PROVIDER` | `console` (test: code printed in the app window), `fast2sms`, `msg91` or `twilio` |
| `FAST2SMS_API_KEY` | Fast2SMS → Dev API |
| `MSG91_AUTH_KEY`, `MSG91_TEMPLATE_ID` | MSG91 authkey + DLT-approved OTP template using `##OTP##` |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM` | Twilio console |
| `SECRET_KEY` | blank = auto-generated per install; set one value for every server if you run several |
| `TRUSTED_PROXY_COUNT` | proxies in front of the app (Nginx = 1, Cloudflare + Nginx = 2), else 0 |

Setting a real `SMS_PROVIDER` switches the temporary demo login off by itself.
After this update everyone is signed out once, because the signing key changed.

## Testing
- `pytest`: 164 passed (32 new in `test_otp.py`: correct, wrong, expired and
  reused codes; resend cooldown and old-code retirement; per-number and per-IP
  limits incl. forged headers; parallel sends; sign-up; provider errors; the MSG91
  request and its error handling; secret key).
  `test_saas_flow.py`: 60 of 60.
- Chromium against the running app in console mode: login (validation, wrong
  code, resend countdown, old code rejected after resend, sign-in) and sign-up
  at phone size. No JavaScript errors.
- Not tested: delivery through a real SMS provider (needs your credentials).
