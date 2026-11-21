"""
Migration: Inventory Profit & Cost Tracking (v29.2)
Run ONCE from your project root:
    python migrate_inventory_expenses.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app
from extensions import db
from sqlalchemy import text

app = create_app()

with app.app_context():
    with db.engine.connect() as conn:

        conn.execute(text(
            "ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS purchase_price DOUBLE PRECISION"
        ))
        print("OK  purchase_price")

        conn.execute(text(
            "ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS expected_selling_price DOUBLE PRECISION"
        ))
        print("OK  expected_selling_price")

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS inventory_expenses (
                id           SERIAL PRIMARY KEY,
                vehicle_id   INTEGER NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
                dealer_id    INTEGER NOT NULL REFERENCES users(id),
                expense_type VARCHAR(50)      NOT NULL,
                amount       DOUBLE PRECISION NOT NULL DEFAULT 0,
                expense_date DATE             NOT NULL,
                note         TEXT,
                created_at   TIMESTAMP DEFAULT NOW(),
                updated_at   TIMESTAMP DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_inv_exp_vid ON inventory_expenses(vehicle_id)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_inv_exp_did ON inventory_expenses(dealer_id)"
        ))
        conn.commit()
        print("OK  inventory_expenses table + indexes")

    print("\nDONE. Ab python app.py chalaao.")
