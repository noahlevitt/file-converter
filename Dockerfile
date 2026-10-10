# ---------------------------------------------------------------------------
# Container image for the file converter (Streamlit web app + conversion engine).
#
# Build and run with docker compose (see docker-compose.yml and README → "Running it"):
#   docker compose up -d --build
# The image holds the code only. Everything that must survive an update lives outside it:
#   /app/data     users, templates, import history (SQLite) and each import's files   → mounted volume
#   /app/schemas  the target import format (target_schema.yaml)                       → mounted read-only
#   .env          settings and secrets (Claude API key, admin account)                → passed as env_file
# ---------------------------------------------------------------------------

# Slim Debian image with Python 3.12 (small download, security updates from the official Python images).
FROM python:3.12-slim

# PYTHONDONTWRITEBYTECODE: no .pyc files in the image. PYTHONUNBUFFERED: logs appear immediately in
# `docker compose logs`. PIP_NO_CACHE_DIR: keeps the image small. DATA_DIR / SCHEMA_PATH: where the app looks
# for its database/import files and the target schema (both mounted from the host by docker-compose.yml).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/app/data \
    SCHEMA_PATH=/app/schemas/target_schema.yaml

WORKDIR /app

# Install dependencies before copying the code, so Docker's layer cache skips this slow step when only the
# code changed.
COPY requirements.txt .
RUN pip install -r requirements.txt

# The rest of the project (.dockerignore keeps .env, data/, sample_data/ and .git out of the image).
COPY . .
# Run as an unprivileged user (uid 1000). On a Linux host the mounted data/ folder must be writable by uid 1000:
#   mkdir -p data && sudo chown 1000 data
RUN useradd --create-home --uid 1000 appuser && mkdir -p /app/data && chown -R appuser /app/data
USER appuser

# Streamlit's port. Publish it only to a reverse proxy with HTTPS (README → "HTTPS / access"), not the internet.
EXPOSE 8501
# Docker marks the container unhealthy if Streamlit stops answering its health endpoint (shown in `docker ps`).
# `restart: unless-stopped` in docker-compose.yml restarts the container when it exits; a container that is
# unhealthy but still running is NOT restarted automatically.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health')" || exit 1

# 0.0.0.0 so the app is reachable from outside the container (through the published port).
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
