"""Runtime settings, read from environment variables (see .env.example).

Overview
--------
All configuration comes from environment variables. For running without Docker, a ``.env`` file in the project
root is loaded once when this module is imported (real environment variables always win). Code calls
:func:`get_settings` to get a :class:`Settings` snapshot.

Settings (environment variable → default → effect):

=====================  ==============================  ==========================================================
ANTHROPIC_API_KEY      "" (empty)                      Claude API key. Empty = all Claude features are off; the
                                                       rest of the app works the same.
AI_ENABLED             true                            Master switch for Claude, even when a key is set.
AI_AUTO_MAP            false                           true = Claude also refines the column mapping on every
                                                       upload (otherwise only on button clicks).
AI_MASK_PHONES         true                            Partly mask phone digits in rows sent for Claude's
                                                       "Check for mistakes".
MAPPING_MODEL          claude-sonnet-5-5               Model for mapping, notes and mistake reviews.
REPAIR_MODEL           claude-haiku-5-5                Model for "Suggest fixes" (value repairs).
AI_SAMPLE_VALUES       15                              Sample values per column sent to Claude / kept in column
                                                       profiles.
SCHEMA_PATH            <project>/schemas/              Target import format (YAML).
                       target_schema.yaml
DATA_DIR               <project>/data                  SQLite database (converter.db) and import files
                                                       (imports/<id>/).
AUTH_MODE              password                        "password" = per-user logins; "none" = no login (single
                                                       person on their own computer).
ADMIN_USERNAME         ""                              First admin, created on start when there are no users.
ADMIN_PASSWORD         ""                              Password for that first admin (8+ characters).
IMPORT_WORKERS         2 (minimum 1)                   Imports that run at the same time; more wait queued.
FILE_RETENTION_DAYS    30                              Days to keep each import's files; 0 = keep forever.
APP_TIMEZONE           America/New_York                Time zone for times shown in the app and the default
                                                       client time zone for conversions.
=====================  ==============================  ==========================================================

``APP_TITLE`` and ``DEFAULT_PAGE`` are read directly by ``app.py`` and are not part of :class:`Settings`.

Boolean variables accept 1/true/yes/on (any case) as true; any other non-empty value is false; empty or unset
means the default. Integer variables raise ``ValueError`` when :func:`get_settings` is called if they are not
whole numbers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Repository root (the folder containing converter/, schemas/, app.py); default location of .env, schemas/, data/.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path = PROJECT_ROOT / ".env") -> None:
    """Minimal .env loader for running without Docker. Real environment variables win.

    Reads ``KEY=value`` lines into ``os.environ`` using ``setdefault``, so a variable already set in the
    environment (e.g. by Docker Compose or the shell) is never overwritten. Blank lines, ``#`` comment lines and
    lines without "=" are skipped; whitespace around key and value and surrounding quote characters are
    removed (``KEY="a b"`` -> ``a b``). Deliberately minimal: no ``export`` prefix, no variable expansion and no
    inline comments (``KEY=x # note`` keeps ``x # note`` as the value). A missing file is ignored.

    Side effect: modifies ``os.environ`` of the current process. Called once at import time (below).
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)  # split on the first "=" only, so values may contain "="
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# Load .env as soon as the package is imported, before any Settings object reads the environment.
_load_dotenv()


def _bool(value: str | None, default: bool) -> bool:
    """Parse an environment-variable flag.

    ``None`` or blank -> ``default``; "1", "true", "yes", "on" (case-insensitive, surrounding spaces ignored) ->
    ``True``; anything else ("false", "0", "off", "no", typos) -> ``False``.
    """
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of the configuration (see the table in the module docstring).

    Each field uses a ``default_factory`` that reads its environment variable when the object is *created*, not
    when the module is imported, so a fresh ``Settings()`` reflects the current environment (e.g. variables set
    by a script or test before calling :func:`get_settings`). Frozen so no code can change settings at runtime.
    """

    # ANTHROPIC_API_KEY (default ""): Claude API key; empty disables every Claude feature (see ai_available).
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    # AI_ENABLED (default true): master switch for Claude, e.g. to turn it off without removing the key.
    ai_enabled: bool = field(default_factory=lambda: _bool(os.getenv("AI_ENABLED"), True))
    # Claude is only called when a user clicks "Apply notes", "Check for mistakes" or "Suggest fixes".
    # Set AI_AUTO_MAP=true to also have Claude refine the column mapping automatically on every upload.
    ai_auto_map: bool = field(default_factory=lambda: _bool(os.getenv("AI_AUTO_MAP"), False))
    # AI_MASK_PHONES (default true): partly mask phone digits (with #) in the sample rows sent to Claude for
    # "Check for mistakes". The per-column sample values in the column profiles (AI_SAMPLE_VALUES) are sent
    # unmasked with every Claude call.
    ai_mask_phones: bool = field(default_factory=lambda: _bool(os.getenv("AI_MASK_PHONES"), True))
    # MAPPING_MODEL (default claude-sonnet-5-5): model for proposing mappings, applying notes and reviewing.
    mapping_model: str = field(default_factory=lambda: os.getenv("MAPPING_MODEL", "claude-sonnet-5-5"))
    # REPAIR_MODEL (default claude-haiku-5-5): faster/cheaper model for "Suggest fixes" on values that failed.
    repair_model: str = field(default_factory=lambda: os.getenv("REPAIR_MODEL", "claude-haiku-5-5"))
    # AI_SAMPLE_VALUES (default 15): sample values per column in column profiles and sent to Claude. Only headers
    # and these samples are sent for mapping, never the whole file.
    ai_sample_values: int = field(default_factory=lambda: int(os.getenv("AI_SAMPLE_VALUES", "15")))
    # SCHEMA_PATH (default <project>/schemas/target_schema.yaml): the target import format.
    schema_path: Path = field(
        default_factory=lambda: Path(os.getenv("SCHEMA_PATH", str(PROJECT_ROOT / "schemas" / "target_schema.yaml")))
    )
    # DATA_DIR (default <project>/data): holds converter.db (users, templates, imports) and imports/<id>/ folders.
    # docker-compose.yml mounts ./data here so data survives container rebuilds.
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", str(PROJECT_ROOT / "data"))))
    # AUTH_MODE (default "password"; trimmed, lower-cased): "none" skips the login entirely and everyone acts as
    # admin user "local" — only for one person running the app on their own computer.
    auth_mode: str = field(default_factory=lambda: os.getenv("AUTH_MODE", "password").strip().lower())
    # ADMIN_USERNAME / ADMIN_PASSWORD (default ""): first admin account, created automatically on start when the
    # user table is empty (auth.ensure_bootstrap_admin). Remove the password from .env after the first sign-in.
    admin_username: str = field(default_factory=lambda: os.getenv("ADMIN_USERNAME", ""))
    admin_password: str = field(default_factory=lambda: os.getenv("ADMIN_PASSWORD", ""))
    # IMPORT_WORKERS (default 2, at least 1): size of the background thread pool, i.e. imports that run at the
    # same time; the rest wait with status "queued".
    import_workers: int = field(default_factory=lambda: max(1, int(os.getenv("IMPORT_WORKERS", "2"))))
    # FILE_RETENTION_DAYS (default 30): files of finished imports older than this are deleted (records and counts
    # are kept); 0 or negative = keep forever.
    file_retention_days: int = field(default_factory=lambda: int(os.getenv("FILE_RETENTION_DAYS", "30")))
    # APP_TIMEZONE (default America/New_York): IANA zone for displaying times and the default client time zone
    # for conversions (times that carry their own zone are converted to it).
    timezone: str = field(default_factory=lambda: os.getenv("APP_TIMEZONE", "America/New_York"))

    @property
    def ai_available(self) -> bool:
        """True when Claude can be used: ``AI_ENABLED`` is on and ``ANTHROPIC_API_KEY`` is set."""
        return self.ai_enabled and bool(self.anthropic_api_key)

    @property
    def db_path(self) -> Path:
        """Path of the SQLite database: ``DATA_DIR/converter.db`` (created on first use by ``Store``)."""
        return self.data_dir / "converter.db"


def get_settings() -> Settings:
    """Return a new :class:`Settings` built from the current environment.

    Not cached: the object is cheap to build, and re-reading the environment keeps tests and scripts simple.
    May raise ``ValueError`` if an integer variable (AI_SAMPLE_VALUES, IMPORT_WORKERS, FILE_RETENTION_DAYS) is
    not a whole number.
    """
    return Settings()
