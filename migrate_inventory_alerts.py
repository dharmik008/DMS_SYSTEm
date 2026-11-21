"""
Migration: Inventory & Dead-Stock Popup Alerts (v29.3)

Creates the inventory_alert_state table that holds the per-dealer "armed"
flags. Without it, an alert would re-appear on every page load.

Run ONCE from your project root:
    python migrate_inventory_alerts.py

Safe to re-run — every statement is IF NOT EXISTS. db.create_all() in app.py
also creates this table from the model, so on a fresh database you can skip
this script entirely.
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

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS inventory_alert_state (
                id            SERIAL PRIMARY KEY,
                dealer_id     INTEGER     NOT NULL REFERENCES users(id),
                threshold_id  VARCHAR(40) NOT NULL,
                armed         BOOLEAN     NOT NULL DEFAULT TRUE,
                last_fired_at TIMESTAMP,
                created_at    TIMESTAMP,
                updated_at    TIMESTAMP,
                CONSTRAINT uq_inv_alert_dealer_threshold
                    UNIQUE (dealer_id, threshold_id)
            )
        """))
        print("OK  inventory_alert_state")

        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_inventory_alert_state_dealer_id
                ON inventory_alert_state (dealer_id)
        """))
        print("OK  ix_inventory_alert_state_dealer_id")

        try:
            conn.commit()
        except AttributeError:
            pass  # SQLAlchemy 1.x autocommits DDL on connect()

    print("\nDone. Inventory popup alerts are live on the dealer inventory page.")
    print("Existing dealers start fully armed, so the first visit will surface")
    print("any condition already true — low stock, or vehicles already aged.")
    print("To start a dealer silent instead, run:")
    print("    from utils.inventory_alerts import evaluate_dealer_alerts")
    print("    evaluate_dealer_alerts(dealer_id, silent=True)")
