#!/usr/bin/env bash
# Caryanams DMS - double-click in Finder to set up and start (Mac).
cd "$(dirname "$0")" || exit 1
export CARYANAMS_LAUNCHER=1
for v in 3.12 3.11 3.10 3.9; do
  if command -v "python$v" >/dev/null 2>&1; then exec "python$v" run.py "$@"; fi
done
if command -v python3 >/dev/null 2>&1; then exec python3 run.py "$@"; fi
echo
echo "Python 3 is not installed."
echo "Mac:    install Python 3.12 from https://www.python.org/downloads/release/python-3128/"
echo "Ubuntu: sudo apt install python3 python3-venv"
read -r -p "Press Enter to close..." _
exit 1
