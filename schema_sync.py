"""
schema_sync.py
--------------
Closes the gap that causes errors like:

    psycopg2.errors.UndefinedColumn: column vehicles.purchase_price does not exist

db.create_all() creates missing TABLES but never adds missing COLUMNS to a
table that already exists. So the moment a column is added to a model, every
database created before that day is broken until somebody remembers to run
the matching migrate_*.py script. Somebody always forgets.

This module compares every model column against the live database and adds
whatever is missing, automatically, at startup.

It is deliberately ADDITIVE ONLY:
  • adds columns that exist in a model but not in the database
  • never drops a column
  • never changes a column's type, length or constraints
  • never deletes or rewrites a row

so it cannot destroy data. Anything it cannot handle safely is reported and
skipped rather than forced.

Usage
-----
Automatic (already wired into app.py, right after db.create_all()):

    from schema_sync import sync_columns
    sync_columns(db)

Manual, including a read-only drift report:

    python migrate_schema_sync.py --check
    python migrate_schema_sync.py

Set SCHEMA_AUTO_SYNC=0 in the environment to turn the startup pass off and
manage migrations by hand instead.
"""

import os
import logging

import sqlalchemy as sa

log = logging.getLogger('schema_sync')

# Tables this pass must not touch, if you ever need an escape hatch.
SKIP_TABLES = set()


def _quote(dialect, name):
    return dialect.identifier_preparer.quote(name)


def _literal_default(column, dialect):
    """
    A DEFAULT clause for the new column, or None when there isn't a safe one.

    Only plain scalar defaults are used. Callables such as `default=_now_ist`
    are Python-side and cannot be expressed in DDL, so those columns are added
    without a default and simply start as NULL.
    """
    if column.server_default is not None:
        arg = getattr(column.server_default, 'arg', None)
        if arg is not None:
            return str(getattr(arg, 'text', arg))

    default = column.default
    if default is None or not getattr(default, 'is_scalar', False):
        return None

    value = default.arg
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return None


def plan(db):
    """
    Work out what is missing. Returns a list of dicts, one per absent column:
        {'table', 'column', 'ddl', 'backfill'}
    Read-only — issues no DDL. Safe to call anywhere.
    """
    engine = db.engine
    dialect = engine.dialect
    inspector = sa.inspect(engine)

    try:
        present_tables = set(inspector.get_table_names())
    except Exception as exc:
        log.warning('schema_sync: cannot list tables (%s)', exc)
        return []

    actions = []

    for table in db.metadata.sorted_tables:
        if table.name in SKIP_TABLES or table.name not in present_tables:
            # Unknown table: db.create_all() is responsible for creating it.
            continue

        try:
            present_cols = {c['name'] for c in inspector.get_columns(table.name)}
        except Exception as exc:
            log.warning('schema_sync: cannot inspect %s (%s)', table.name, exc)
            continue

        for column in table.columns:
            if column.name in present_cols:
                continue

            try:
                type_sql = column.type.compile(dialect=dialect)
            except Exception as exc:
                log.warning('schema_sync: skipping %s.%s — cannot render type (%s)',
                            table.name, column.name, exc)
                continue

            tq = _quote(dialect, table.name)
            cq = _quote(dialect, column.name)
            default_sql = _literal_default(column, dialect)

            # Always added as NULLable, even when the model says nullable=False.
            # Adding a NOT NULL column to a table that already holds rows fails
            # outright; letting it be nullable keeps the app running, and the
            # model still enforces the constraint on new writes.
            ddl = 'ALTER TABLE {} ADD COLUMN {} {}'.format(tq, cq, type_sql)
            if default_sql is not None:
                ddl += ' DEFAULT ' + default_sql

            backfill = None
            if default_sql is not None:
                backfill = 'UPDATE {} SET {} = {} WHERE {} IS NULL'.format(
                    tq, cq, default_sql, cq)

            actions.append({
                'table': table.name,
                'column': column.name,
                'ddl': ddl,
                'backfill': backfill,
                'not_null_in_model': not column.nullable,
            })

    return actions


def sync_columns(db, dry_run=False, verbose=True):
    """
    Apply the plan. Returns (applied, failed) as lists of 'table.column'.

    Each column is applied in its own transaction, so one failure never rolls
    back the others and never stops the app from booting.
    """
    actions = plan(db)
    if not actions:
        if verbose:
            log.info('schema_sync: database matches the models, nothing to add')
        return [], []

    if dry_run:
        for a in actions:
            log.info('schema_sync [check]: missing %s.%s', a['table'], a['column'])
        return [f"{a['table']}.{a['column']}" for a in actions], []

    applied, failed = [], []

    for a in actions:
        label = '{}.{}'.format(a['table'], a['column'])
        try:
            with db.engine.begin() as conn:
                conn.execute(sa.text(a['ddl']))
                if a['backfill']:
                    conn.execute(sa.text(a['backfill']))
            applied.append(label)
            if verbose:
                log.warning('schema_sync: added missing column %s', label)
        except Exception as exc:
            failed.append(label)
            log.error('schema_sync: FAILED to add %s — %s', label, exc)

    if failed and verbose:
        log.error('schema_sync: %d column(s) could not be added: %s',
                  len(failed), ', '.join(failed))

    return applied, failed


def auto_sync(db):
    """
    The startup entry point. Honours SCHEMA_AUTO_SYNC=0 and swallows every
    error, because a schema check must never be the reason the app won't boot.
    """
    if os.environ.get('SCHEMA_AUTO_SYNC', '1').strip().lower() in ('0', 'false', 'no', 'off'):
        log.info('schema_sync: disabled by SCHEMA_AUTO_SYNC')
        return [], []
    try:
        return sync_columns(db)
    except Exception as exc:
        log.error('schema_sync: pass aborted — %s', exc)
        return [], []
