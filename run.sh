#!/usr/bin/env bash
# Sets up on first run, then starts the dashboard at http://127.0.0.1:8765
# To use another port: PORT=9000 ./run.sh
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8765}"

[ -d .venv ] || python3 -m venv .venv
if [ ! -f .venv/.installed ] || [ requirements.txt -nt .venv/.installed ]; then
  .venv/bin/pip install -q --disable-pip-version-check -r requirements.txt
  touch .venv/.installed
fi
[ -f .env ] || cp .env.example .env
chmod 600 .env
.venv/bin/python manage.py migrate --noinput -v0

(sleep 2 && open "http://127.0.0.1:$PORT" >/dev/null 2>&1 || true) &
exec .venv/bin/python manage.py runserver "127.0.0.1:$PORT" --noreload
