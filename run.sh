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
# Updating the database under a dashboard that's still running would break it, so stop first.
if .venv/bin/python -c "import socket, sys; sys.exit(socket.socket().connect_ex(('127.0.0.1', int(sys.argv[1]))) != 0)" "$PORT"; then
  echo "Something is already running on port $PORT, probably Job Finder. Stop it first (Ctrl+C in its window), then run ./run.sh again." >&2
  exit 1
fi
.venv/bin/python manage.py migrate --noinput -v0

(sleep 2 && open "http://127.0.0.1:$PORT" >/dev/null 2>&1 || true) &
exec .venv/bin/python manage.py runserver "127.0.0.1:$PORT" --noreload
