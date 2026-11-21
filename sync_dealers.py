"""
Run a two-way dealer sync against an external system.

    python sync_dealers.py --dry-run                 # plan only, writes nothing
    python sync_dealers.py                           # apply, flag conflicts
    python sync_dealers.py --policy remote           # other system wins
    python sync_dealers.py --conflicts               # list what needs a decision

Point ADAPTER below at your own DealerSource subclass. Everything else —
matching, three-way merge, conflict detection, deactivation — is handled by
utils/dealer_sync.py and does not change per source.

Schedule it with cron or Task Scheduler once you trust the dry-run output:

    */30 * * * *  cd /path/to/app && python sync_dealers.py >> sync.log 2>&1
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app
from extensions import db
from utils.dealer_sync import sync_dealers, CsvDealerSource


# ── Point this at your system ───────────────────────────────────────────────
# Replace with your own DealerSource subclass. The CSV one is here so you can
# trial the merge logic before writing an adapter.
def build_source():
    return CsvDealerSource('dealers_export.csv', name='legacy_dms')


def main():
    args = sys.argv[1:]
    dry_run = '--dry-run' in args or '-n' in args
    show_conflicts = '--conflicts' in args

    policy = 'flag'
    if '--policy' in args:
        try:
            policy = args[args.index('--policy') + 1]
        except IndexError:
            print('--policy needs a value: flag | local | remote')
            return 2

    app = create_app()
    with app.app_context():

        if show_conflicts:
            from models import DealerSyncConflict, User
            open_rows = DealerSyncConflict.query.filter_by(resolved=False).all()
            if not open_rows:
                print('No open conflicts.')
                return 0
            print('{} open conflict(s):\n'.format(len(open_rows)))
            for c in open_rows:
                who = User.query.get(c.dealer_id)
                print('  #{}  {} — {}'.format(c.id, who.name if who else c.dealer_id, c.field))
                print('       local : {}'.format(c.local_value))
                print('       remote: {}'.format(c.remote_value))
            print('\nResolve with:')
            print('  from utils.dealer_sync import resolve_conflict')
            print("  resolve_conflict(<id>, 'local')   # or 'remote'")
            return 0

        source = build_source()
        print('Source : {}'.format(source.name))
        print('Policy : {}{}'.format(policy, '   (DRY RUN)' if dry_run else ''))
        print()

        try:
            report = sync_dealers(source, policy=policy, dry_run=dry_run)
        except FileNotFoundError as exc:
            print('Cannot read the source: {}'.format(exc))
            return 1
        except Exception as exc:
            db.session.rollback()
            print('Sync failed: {}'.format(exc))
            return 1

        s = report.summary()
        for key in ('created', 'pulled', 'pushed', 'conflicts',
                    'deactivated', 'skipped', 'push_failures'):
            print('  {:<14} {}'.format(key, s[key]))

        if report.conflicts:
            print('\nConflicts (nothing was overwritten):')
            for eid, field, local, remote in report.conflicts:
                print('  {} · {}'.format(eid, field))
                print('     local : {}'.format(local))
                print('     remote: {}'.format(remote))
            print('\nRun with --conflicts to list them with ids.')

        if report.skipped:
            print('\nSkipped:')
            for eid, why in report.skipped:
                print('  {}: {}'.format(eid, why))

        if report.push_failures:
            print('\nCould not push to the other system:')
            for eid, fields in report.push_failures:
                print('  {}: {}'.format(eid, ', '.join(fields)))
            print('These will be retried on the next run.')

        if dry_run:
            print('\nDry run — nothing was written on either side.')

    return 0


if __name__ == '__main__':
    sys.exit(main())
