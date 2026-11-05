"""
Migration: remove the dealer GST number (GSTIN) field.

Drops users.gst_number. The application no longer reads or writes it.

DESTRUCTIVE — this deletes every stored GSTIN. Back up first:
    pg_dump -t users your_database > users_backup.sql

Run ONCE from your project root:
    python migrate_remove_gst_number.py

Note: this touches the GST *number* only. GST tax on deals and invoices
(deals.gst_amount, the finance GST calculator, CGST/SGST) is untouched.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app
from extensions import db
from sqlalchemy import text

app = create_app()

with app.app_context():
    with db.engine.connect() as conn:

        # Report what is about to be lost, so this is not a silent deletion.
        try:
            existing = conn.execute(text("""
                SELECT COUNT(*) FROM information_schema.columns
                 WHERE table_name = 'users' AND column_name = 'gst_number'
            """)).scalar()
        except Exception:
            existing = None

        if existing == 0:
            print("SKIP  users.gst_number is already gone — nothing to do.")
        else:
            try:
                filled = conn.execute(text(
                    "SELECT COUNT(*) FROM users "
                    "WHERE gst_number IS NOT NULL AND gst_number <> ''"
                )).scalar()
                print("INFO  {} user row(s) currently hold a GST number.".format(filled))
            except Exception:
                pass

            conn.execute(text("ALTER TABLE users DROP COLUMN IF EXISTS gst_number"))
            print("OK    dropped users.gst_number")

        try:
            conn.commit()
        except AttributeError:
            pass  # SQLAlchemy 1.x autocommits DDL on connect()

    print("\nDone. Restart the app.")
