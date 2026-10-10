@echo off
REM Run the converter on this computer without Docker (Windows). Needs Python 3.11+ from python.org.
REM
REM Usage: double-click this file (or run it from a Command Prompt), then open http://localhost:8501.
REM Stop it by closing the window or pressing Ctrl+C.
REM First run: creates a virtual environment in .venv\ and installs requirements.txt into it, and copies
REM .env.example to .env. Data (users, templates, imports) is stored in .\data next to this file.

REM Work from the project folder, wherever the script is started from.
cd /d "%~dp0"
REM One-time setup of an isolated Python environment for this project.
if not exist .venv (
  py -3 -m venv .venv
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\pip install -r requirements.txt
)
REM Settings file: created from the template on first run.
if not exist .env copy .env.example .env
.venv\Scripts\streamlit run app.py --server.port 8501
