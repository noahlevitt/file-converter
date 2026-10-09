@echo off
REM Run the converter on this computer without Docker (Windows). Needs Python 3.11+ from python.org.
cd /d "%~dp0"
if not exist .venv (
  py -3 -m venv .venv
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\pip install -r requirements.txt
)
if not exist .env copy .env.example .env
.venv\Scripts\streamlit run app.py --server.port 8501
