#!/usr/bin/env python3
"""
run.py — one-click setup + start for Caryanams DMS.

Double-click start.bat (Windows), start.command (Mac) or start.sh (Linux),
or run:  python run.py

What it does, every time, in order:
  1. Finds a Python the project's packages support (3.9 – 3.12).
  2. Creates a private environment in .venv (first run only).
  3. Installs requirements.txt (first run, and again only when it changes).
  4. Creates .env from .env.example if there is no .env yet.
  5. Checks the database. If PostgreSQL is set but unreachable, it asks
     whether to use a local SQLite file instead, so the app still starts.
  6. Picks a free port (5000 if available), opens the browser, runs the app.

Options:
  --sqlite       Use the local SQLite file for this run, skip PostgreSQL.
  --port N       Use port N instead of the first free one from 5000.
  --no-browser   Don't open the browser automatically.
  --setup-only   Install everything, then exit without starting the app.
  --reinstall    Reinstall all packages even if nothing changed.
  --reload       Restart automatically when you edit code (developer mode).

This file uses only the Python standard library until the environment is
ready, so it works on a brand-new machine.
"""
import hashlib
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(ROOT, '.venv')
REQS = os.path.join(ROOT, 'requirements.txt')
ENV_FILE = os.path.join(ROOT, '.env')
ENV_EXAMPLE = os.path.join(ROOT, '.env.example')
STAMP = os.path.join(VENV, '.requirements.sha256')

# numpy 1.26, onnxruntime 1.18 and psycopg2-binary 2.9.9 (pinned in
# requirements.txt) only ship ready-made installs for these versions.
SUPPORTED = [(3, 12), (3, 11), (3, 10), (3, 9)]
IS_WIN = os.name == 'nt'
ARGS = sys.argv[1:]


# ── Small output helpers ────────────────────────────────────────────────────
def _supports_color():
    if os.environ.get('NO_COLOR') or not sys.stdout.isatty():
        return False
    if IS_WIN:
        os.system('')  # turns on ANSI colors in Windows 10+ consoles
    return True


_C = _supports_color()


def _c(code, s):
    return f'\033[{code}m{s}\033[0m' if _C else s


def step(msg):
    print(_c('1;36', '▸ ') + msg, flush=True)


def ok(msg):
    print(_c('1;32', '✔ ') + msg, flush=True)


def warn(msg):
    print(_c('1;33', '! ') + msg, flush=True)


def fail(msg, hint=None):
    print()
    print(_c('1;31', '✖ ' + msg))
    if hint:
        print('  ' + hint.replace('\n', '\n  '))
    print()
    _pause_if_double_clicked()
    sys.exit(1)


def _pause_if_double_clicked():
    # Keep the window open long enough to read an error when launched by a
    # double-click. The launchers set CARYANAMS_LAUNCHER=1.
    if os.environ.get('CARYANAMS_LAUNCHER') and sys.stdin.isatty():
        try:
            input('Press Enter to close this window...')
        except EOFError:
            pass


def flag(name):
    return name in ARGS


def option(name, default=None):
    if name in ARGS:
        i = ARGS.index(name)
        if i + 1 < len(ARGS):
            return ARGS[i + 1]
    return default


# ── 1. Python version ───────────────────────────────────────────────────────
def venv_python():
    return os.path.join(VENV, 'Scripts', 'python.exe') if IS_WIN \
        else os.path.join(VENV, 'bin', 'python')


def running_in_venv():
    return os.path.normcase(os.path.abspath(sys.prefix)) == \
        os.path.normcase(os.path.abspath(VENV))


def _version_of(cmd):
    try:
        out = subprocess.run(cmd + ['-c', 'import sys;print(sys.version_info[0],sys.version_info[1])'],
                             capture_output=True, text=True, timeout=15)
        if out.returncode == 0:
            a, b = out.stdout.split()
            return int(a), int(b)
    except Exception:
        pass
    return None


def find_supported_python():
    """Return a command list for a supported Python, or None."""
    if tuple(sys.version_info[:2]) in SUPPORTED:
        return [sys.executable]
    candidates = []
    if IS_WIN and shutil.which('py'):
        candidates += [['py', f'-{a}.{b}'] for a, b in SUPPORTED]
    for a, b in SUPPORTED:
        exe = shutil.which(f'python{a}.{b}')
        if exe:
            candidates.append([exe])
    for cmd in candidates:
        if _version_of(cmd) in SUPPORTED:
            return cmd
    return None


# ── 2 + 3. Environment and packages ─────────────────────────────────────────
def ensure_venv():
    py = venv_python()
    if os.path.exists(py):
        v = _version_of([py])
        if v in SUPPORTED:
            return py
        warn(f'Existing .venv uses an unsupported Python {v}; rebuilding it.')
        shutil.rmtree(VENV, ignore_errors=True)

    base = find_supported_python()
    if not base:
        have = '.'.join(map(str, sys.version_info[:3]))
        fail(f'This project needs Python 3.9, 3.10, 3.11 or 3.12 (you have {have}).',
             'Install Python 3.12 from https://www.python.org/downloads/release/python-3128/\n'
             'On Windows, tick "Add python.exe to PATH" in the installer.\n'
             'Then run the start file again; your current Python can stay installed.')
    step(f'Creating a private Python environment in .venv (one time, using {" ".join(base)})...')
    r = subprocess.run(base + ['-m', 'venv', VENV])
    if r.returncode != 0 or not os.path.exists(py):
        hint = 'On Ubuntu/Debian run:  sudo apt install python3-venv' if not IS_WIN else None
        fail('Could not create the .venv environment.', hint)
    ok('Environment created.')
    return py


def _req_hash():
    with open(REQS, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def ensure_packages(py):
    want = _req_hash()
    have = open(STAMP).read().strip() if os.path.exists(STAMP) else ''
    if want == have and not flag('--reinstall'):
        ok('Packages are up to date.')
        return
    step('Installing packages. The first time takes a few minutes (AI image libraries are large)...')
    subprocess.run([py, '-m', 'pip', 'install', '--upgrade', 'pip', '--quiet', '--disable-pip-version-check'])
    r = subprocess.run([py, '-m', 'pip', 'install', '-r', REQS, '--disable-pip-version-check'])
    if r.returncode != 0:
        fail('Package installation failed (see the messages above).',
             'Check your internet connection and run the start file again.\n'
             'It resumes where it stopped; finished packages are not downloaded twice.')
    with open(STAMP, 'w') as f:
        f.write(want)
    ok('Packages installed.')


# ── 4. .env ─────────────────────────────────────────────────────────────────
def ensure_env_file():
    if os.path.exists(ENV_FILE):
        return
    if os.path.exists(ENV_EXAMPLE):
        shutil.copyfile(ENV_EXAMPLE, ENV_FILE)
        ok('Created .env from .env.example (uses a local SQLite database, no setup needed).')
    else:
        warn('No .env file found; using built-in defaults (local SQLite database).')


def load_env():
    from dotenv import load_dotenv  # installed from requirements.txt
    load_dotenv(ENV_FILE, override=False)  # real environment variables win


# ── 5. Database ─────────────────────────────────────────────────────────────
def _mask(url):
    # postgresql://user:secret@host/db  ->  postgresql://user:****@host/db
    if '://' in url and '@' in url:
        scheme, rest = url.split('://', 1)
        creds, host = rest.rsplit('@', 1)
        if ':' in creds:
            creds = creds.split(':', 1)[0] + ':****'
        return f'{scheme}://{creds}@{host}'
    return url


def _comment_out_database_url():
    with open(ENV_FILE, encoding='utf-8') as f:
        lines = f.readlines()
    with open(ENV_FILE, 'w', encoding='utf-8') as f:
        for line in lines:
            if line.strip().startswith('DATABASE_URL='):
                f.write('# Turned off by run.py (option 3). Remove the "# " to use PostgreSQL again:\n')
                f.write('# ' + line)
            else:
                f.write(line)


def use_sqlite(reason):
    os.environ.pop('DATABASE_URL', None)  # app.py then falls back to SQLite
    os.environ['CARYANAMS_FORCE_SQLITE'] = '1'  # remembered across --reload restarts
    ok(f'Using local SQLite database file instance/Caryanams.db ({reason}).')


def check_database():
    url = os.environ.get('DATABASE_URL', '')
    if flag('--sqlite'):
        use_sqlite('--sqlite option')
        return
    if not url:
        use_sqlite('no PostgreSQL configured')
        return
    if url.startswith('sqlite'):
        ok('Using SQLite from .env.')
        return

    step(f'Checking PostgreSQL at {_mask(url)} ...')
    error = None
    try:
        import psycopg2
        conn = psycopg2.connect(url.replace('postgres://', 'postgresql://', 1), connect_timeout=5)
        conn.close()
        ok('PostgreSQL is reachable.')
        return
    except Exception as e:  # noqa: BLE001 — show any connection problem plainly
        error = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__

    warn('Cannot connect to PostgreSQL: ' + error)
    print('  Common causes: PostgreSQL is not running, the password in .env is wrong,')
    print('  or the database "caryanams_db" has not been created yet.')
    if not sys.stdin.isatty():
        fail('Stopping because PostgreSQL is unreachable.',
             'Fix DATABASE_URL in .env, or run:  python run.py --sqlite')
    print()
    print('  What would you like to do?')
    print('   1) Use a local SQLite file just for this run   [press Enter]')
    print('   2) Stop, so I can start/fix PostgreSQL')
    print('   3) Always use SQLite (turns off DATABASE_URL in .env)')
    print('  Note: SQLite is a separate database; it will not contain your PostgreSQL data.')
    try:
        choice = input('  Choice [1/2/3]: ').strip() or '1'
    except EOFError:
        choice = '2'
    if choice == '1':
        use_sqlite('for this run')
    elif choice == '3':
        _comment_out_database_url()
        use_sqlite('saved in .env')
    else:
        fail('Stopped. Start PostgreSQL or fix DATABASE_URL in .env, then run the start file again.')


# ── 6. Start ────────────────────────────────────────────────────────────────
def _port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(('127.0.0.1', port)) != 0


def choose_port():
    wanted = option('--port')
    if wanted:
        if not wanted.isdigit():
            fail(f'--port needs a number, got "{wanted}".')
        if not _port_free(int(wanted)):
            fail(f'Port {wanted} is already in use by another program.',
                 'Close it, or run without --port to pick a free one automatically.')
        return int(wanted)
    for port in range(5000, 5021):
        if _port_free(port):
            if port != 5000:
                warn(f'Port 5000 is busy (on Mac this is often AirPlay Receiver); using {port}.')
            return port
    fail('No free port between 5000 and 5020.', 'Run with --port 8080 (or any free number).')


def open_browser_when_ready(url, port):
    def wait_and_open():
        for _ in range(240):  # up to 2 minutes (first start creates tables)
            if not _port_free(port):
                webbrowser.open(url)
                return
            time.sleep(0.5)
    threading.Thread(target=wait_and_open, daemon=True).start()


def start_app():
    port = choose_port()
    base = f'http://localhost:{port}'
    # Mini-site links use APP_URL; keep them right when the port changes.
    app_url = os.environ.get('APP_URL', '')
    if not app_url or app_url.rstrip('/') in ('http://localhost:5000', 'http://127.0.0.1:5000'):
        os.environ['APP_URL'] = base

    step('Starting Caryanams (first start also prepares the database tables)...')
    sys.path.insert(0, ROOT)
    os.chdir(ROOT)
    from app import create_app
    app = create_app()

    line = '─' * 58
    print()
    print(_c('1;32', line))
    print(_c('1;32', '  Caryanams DMS is running'))
    print(_c('1;32', line))
    print(f'  Website       {base}/')
    print(f'  Admin panel   {base}/admin/login')
    print('  Admin login   username: admin    password: admin123')
    sms = (os.environ.get('SMS_PROVIDER') or 'console').strip().lower()
    if sms == 'console':
        print('  Dealer/user   sign in with mobile + code. TEST MODE: codes appear in this window')
    else:
        print(f'  Dealer/user   sign in with mobile + code, sent by SMS via {sms}')
    print('  Stop          press Ctrl+C in this window (or close it)')
    print(_c('1;32', line))
    print()

    if not flag('--no-browser'):
        open_browser_when_ready(f'{base}/', port)

    os.environ['CARYANAMS_PORT'] = str(port)  # reused by --reload restarts
    try:
        # load_dotenv=False: .env was already applied above, and Flask must not
        # re-add DATABASE_URL after the user chose SQLite.
        app.run(host='127.0.0.1', port=port, debug=True,
                use_reloader=flag('--reload'), load_dotenv=False)
    except KeyboardInterrupt:
        pass
    print('\nCaryanams stopped. Run the start file again any time.')


def main():
    if not os.environ.get('CARYANAMS_IN_VENV'):
        print(_c('1', '\nCaryanams DMS — setup & start\n'))
    if not os.path.exists(REQS):
        fail('requirements.txt is missing. Run this from the project folder.')

    if not running_in_venv():
        py = ensure_venv()
        ensure_packages(py)
        # Continue inside the environment so every import comes from .venv.
        env = dict(os.environ, CARYANAMS_IN_VENV='1')
        r = subprocess.run([py, os.path.abspath(__file__)] + ARGS, env=env, cwd=ROOT)
        sys.exit(r.returncode)

    if os.environ.get('WERKZEUG_RUN_MAIN'):
        # Restart triggered by --reload after a code edit: reuse earlier choices
        # silently instead of asking again.
        load_env()
        if os.environ.get('CARYANAMS_FORCE_SQLITE'):
            os.environ.pop('DATABASE_URL', None)
        sys.path.insert(0, ROOT)
        from app import create_app
        create_app().run(host='127.0.0.1', port=int(os.environ['CARYANAMS_PORT']),
                         debug=True, use_reloader=True, load_dotenv=False)
        return

    if flag('--setup-only'):
        ok('Setup complete. Run the start file again to launch the app.')
        return
    ensure_env_file()
    load_env()
    check_database()
    try:
        start_app()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        fail(f'The app could not start: {e}',
             'The details are above. If it mentions the database, try:  python run.py --sqlite')


if __name__ == '__main__':
    main()
