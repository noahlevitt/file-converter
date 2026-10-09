#!/usr/bin/env bash
# Run the converter on this computer without Docker (macOS / Linux). Needs Python 3.11+.
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements.txt
fi
[ -f .env ] || cp .env.example .env
exec .venv/bin/streamlit run app.py --server.port "${PORT:-8501}"
