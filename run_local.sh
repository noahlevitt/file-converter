#!/usr/bin/env bash
# Run the converter on this computer without Docker (macOS / Linux). Needs Python 3.11+.
#
# Usage:  ./run_local.sh            then open http://localhost:8501   (stop with Ctrl+C)
#         PORT=8600 ./run_local.sh  to use another port
# First run: creates a virtual environment in .venv/ and installs requirements.txt into it (a minute or two),
# and copies .env.example to .env. Later runs start straight away. After `git pull`, if requirements.txt
# changed, refresh the packages with:  .venv/bin/pip install -r requirements.txt
# Data (users, templates, imports) is stored in ./data next to this script.

# Stop on the first error, on unset variables and on failures inside pipes.
set -euo pipefail
# Work from the project folder, wherever the script is started from.
cd "$(dirname "$0")"
# One-time setup of an isolated Python environment for this project.
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements.txt
fi
# Settings file: created from the template on first run (edit it to set the admin password, Claude key, …).
[ -f .env ] || cp .env.example .env
# Replace this shell with the web app so Ctrl+C stops it cleanly.
exec .venv/bin/streamlit run app.py --server.port "${PORT:-8501}"
