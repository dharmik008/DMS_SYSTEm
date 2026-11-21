"""
Migration: sync database columns to the models.

Adds any column that exists in a model but is missing from the live database.
This is what fixes errors like:

    column vehicles.purchase_price does not exist

Additive only — it never drops a column, never changes a type, and never
edits a row.

Usage
-----
    python migrate_schema_sync.py --check    # report drift, change nothing
    python migrate_schema_sync.py            # apply

app.py already runs this automatically at startup, so you normally never need
to call it by hand. It is here for CI checks, for production deploys where
you would rather apply DDL deliberately, and for anyone running with
SCHEMA_AUTO_SYNC=0.
"""
import sys
import os
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.INFO, format='%(message)s')

from app import create_app
from extensions import db
from schema_sync import plan, sync_columns

check_only = '--check' in sys.argv or '-n' in sys.argv

app = create_app()

with app.app_context():
    pending = plan(db)

    if not pending:
        print('OK    database matches the models — no columns missing.')
        sys.exit(0)

    print('Found {} missing column(s):\n'.format(len(pending)))
    for a in pending:
        flag = '  (model says NOT NULL — added as nullable)' if a['not_null_in_model'] else ''
        print('  {}.{}{}'.format(a['table'], a['column'], flag))

    if check_only:
        print('\n--check: nothing was changed. Re-run without --check to apply.')
        sys.exit(1)

    print('\nApplying...\n')
    applied, failed = sync_columns(db, verbose=False)

    for label in applied:
        print('  OK      {}'.format(label))
    for label in failed:
        print('  FAILED  {}'.format(label))

    print('\n{} added, {} failed.'.format(len(applied), len(failed)))
    if failed:
        print('Check the log above — those columns need a manual ALTER TABLE.')
        sys.exit(1)

    print('Done. Restart the app.')
