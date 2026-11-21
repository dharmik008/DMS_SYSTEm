# v35 — Demo OTP 111111, Studio locked on free plans

## Demo login
The dealer demo login OTP is now **111111** (`DEMO_LOGIN_OTP` in `.env`). The
mobile number is still 9999999999. Restart the app after changing `.env`.

## Caryanams Studio needs a paid plan
- Starter was already locked. A Growth or Pro plan activated with
  **Use Free For Now** now keeps Studio locked as well: the dashboard card and the
  sidebar link open the upgrade popup, and `/studio/` (pages and API) is refused.
- Paying for the plan (**Activate (Demo Payment)**) unlocks it.
- A plan set on the dealer record without a subscription record (the seeded demo
  dealer, plans changed in the admin panel) counts as paid, as before.

## Plan page
A plan kept only on the dealer record showed as **Starter**, with the Starter
button disabled as "Current Plan". The page now shows the real plan (the demo
dealer sees Growth) and Starter can be picked.

## Files
`.env`, `subscription_features.py`, `templates/dealer/subscription.html`,
`test_dashboard_studio_lock.py`, `test_demo_login.py`

## Testing
- `pytest`: whole suite passes (5 new Studio tests: free activation locks the card,
  the page and the API; paying unlocks it). `test_saas_flow.py`: 60 of 60.
- Plan page in jsdom (`test_plan_page_dom.js` plus the demo dealer's case) and a
  click-through in Chromium: demo login with 111111, Starter, Use Free For Now,
  Demo Payment.

## Note
`dump_plan_page.py` still signs up with a password, which SMS sign-up no longer
accepts, so it cannot produce the pages `test_plan_page_dom.js` reads. This was
already the case before v35.
