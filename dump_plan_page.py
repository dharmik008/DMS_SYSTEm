#!/usr/bin/env python3
"""
dump_plan_page.py — render the Subscription Plans page to static HTML.

Produces two files for test_plan_page_dom.js to execute:

    no_plan.html    a dealer mid-onboarding, no subscription row
    with_plan.html  a dealer on an active Growth plan

Uses a throwaway SQLite database, so it never touches production data.

    python dump_plan_page.py && node test_plan_page_dom.js
"""

import os
import tempfile

os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'dump.db')

from app import create_app                    # noqa: E402
from models import db, User                   # noqa: E402

DEALER = {
    'name': 'DOM Test Motors', 'email': 'domtest.dealer@example.com',
    'password': 'secret123', 'confirm_password': 'secret123',
    'phone': '9876500123', 'role': 'dealer', 'city': 'Pune',
    'company_name': 'DOM Test Motors',
}


def main():
    app = create_app()
    c = app.test_client()

    c.post('/auth/register', data=DEALER)

    with open('no_plan.html', 'w', encoding='utf-8') as fh:
        fh.write(c.get('/dealer/subscription?onboarding=1').get_data(as_text=True))
    print('wrote no_plan.html')

    r = c.post('/dealer/subscription/activate', json={'plan': 'growth', 'method': 'demo'})
    assert (r.get_json() or {}).get('success'), r.get_json()

    with open('with_plan.html', 'w', encoding='utf-8') as fh:
        fh.write(c.get('/dealer/subscription').get_data(as_text=True))
    print('wrote with_plan.html')

    with app.app_context():
        u = User.query.filter_by(email=DEALER['email']).first()
        print(f'dealer #{u.id} plan={u.subscription_plan} expiry={u.subscription_expiry}')


if __name__ == '__main__':
    main()
