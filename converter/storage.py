"""SQLite storage for users, saved mapping templates and imports (status, progress, results).

Overview
--------
This module is the only code that reads or writes the application database (``DATA_DIR/converter.db``) and the
per-import file folders next to it. It owns five tables:

* ``users``     -- login accounts: bcrypt password hash, admin flag, created / last-login timestamps.
* ``templates`` -- saved column mappings. Each is stored with a *fingerprint* of the file's header row so a file
  with the same layout is recognised and mapped automatically next time (see ``Store.find_template``).
* ``imports``   -- one row per import job: who started it, a frozen snapshot of everything needed to run it
  (target schema, mapping, read options, approved value fixes), the live status / progress written by the
  background worker, and the final counts and summary.
* ``user_prefs`` -- small per-user display preferences such as the light/dark ``theme`` (since 0.10.0).
* ``number_log`` -- every number each import converted, with its consent value and file row (since 0.21.0), kept
  after the import's files are deleted, for the Number history page (``converter.numberlog``).

Main entry points
-----------------
``Store`` is the single entry point. The web app (``app.py``, one cached instance per server process), the
background runner (``jobs.py``), the mapping service (``services.py``) and the CLI (``cli.py``) all use it.
``ImportRecord`` and ``Template`` are plain dataclasses handed back to callers, so the rest of the code never
deals with ``sqlite3.Row`` objects or the JSON-encoded columns.

Data flow of an import
----------------------
1. ``create_import`` inserts a ``queued`` row and writes the uploaded bytes to ``DATA_DIR/imports/<id>/source.<ext>``.
2. ``jobs.run_import`` calls ``start_import`` -> ``update_progress`` (many times) -> ``complete_import`` on
   success, or ``finish_import`` with ``failed`` / ``cancelled``. Browsers poll ``get_import`` / ``list_imports``
   to show progress, so every user sees the same state.
3. A user can ``request_cancel``; the worker polls ``is_cancel_requested`` and stops at the next check.
4. On server start ``mark_interrupted`` closes imports a previous process left ``queued`` / ``running``.
5. ``purge_files`` is called periodically to delete the folders of old finished imports (file retention).
6. ``log_numbers`` records the converted numbers just before ``complete_import`` (``numberlog.record``);
   ``purge_number_log`` removes them after NUMBER_RECORD_DAYS (default: kept forever).

Files and retention
-------------------
Each import's files (original upload, converted file, rejected rows, summary, comparison data) live in
DATA_DIR/imports/<id>/ and are deleted after FILE_RETENTION_DAYS; the import record and its counts stay.
The ``files_json`` column maps a file *kind* ("output", "rejects", "summary", "audit", ...) to its file name in
that folder; ``files_purged`` records that the folder has been deleted so the UI can say so.

A file still being mapped on New import is a *draft* (since 0.15.0): DATA_DIR/drafts/<random id>/ holds the upload
and the page's last saved state, so a browser refresh can restore the work (``create_draft``, ``save_draft_state``,
``load_draft``). Drafts are deleted on "Start import", "Start over" or a different upload, and after 24 hours
without changes (``purge_drafts``). They are files only; there is no database table for them.

Concurrency
-----------
SQLite in WAL mode comfortably handles an internal tool with 5–20 concurrent users. Every method opens its own
short-lived connection (see ``Store._conn``) instead of sharing one, because ``sqlite3`` connections must not be
used from several threads at once; this way a single ``Store`` can be shared by all Streamlit sessions and the
worker threads without any Python-side locking. SQLite itself serialises writers; ``timeout=30`` makes a
writer wait for the lock instead of failing immediately.

Schema changes
--------------
There is no migration framework. The schema is applied with ``CREATE TABLE/INDEX IF NOT EXISTS`` every time a
``Store`` is created: that creates missing tables and indexes on a fresh database but never alters an existing
table, so adding a column to a deployed database needs an explicit ``ALTER TABLE`` step.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import bcrypt

from .mapping import Mapping, has_generic_columns, header_fingerprint, header_similarity

# Database schema, applied idempotently by Store.__init__ (comments live here, not inside the SQL string, so the
# executed SQL stays exactly as it is). All timestamps are UTC ISO-8601 strings with seconds precision written by
# _now(); because they share one format, comparing them as text (as purge_files does) orders them correctly.
# Booleans are stored as INTEGER 0/1. *_json columns hold JSON text decoded by the _row_to_* helpers.
#
# users -- one row per login account
#   username       primary key; always stored stripped and lower-case so logins are case-insensitive
#   password_hash  bcrypt hash (salt included); the plain password is never stored
#   is_admin       1 = may manage users and other admin-only settings
#   created_at     when the account was created
#   last_login     updated by verify_user on every successful login (NULL until the first login)
#
# templates -- saved column mappings, reusable for files with the same (or a similar) header row
#   id             surrogate key
#   name           user-facing name, UNIQUE: saving under an existing name updates that template
#   fingerprint    header_fingerprint(headers): hash of the normalised, de-duplicated, sorted header names, so
#                  column order, case, spacing and punctuation don't matter. Indexed (idx_templates_fp).
#   headers_json   the original header list, kept for fuzzy matching (header_similarity) and display
#   mapping_json   the Mapping (pydantic model) as JSON
#   read_json      read options used with the template (sheet, header row, timezone, ...)
#   schema_name    target import format the mapping was made for; templates only match within one format
#   notes          free-text notes from the user
#   created_by/at  author and time of the first save
#   updated_by/at  author and time of the latest save (used as a tie-breaker in find_template)
#   use_count      times the template was applied (mark_template_used); the most-used exact match wins
#
# imports -- one row per import job (a re-run creates a new row with parent_id pointing at the original)
#   id               surrogate key; also the name of the import's folder DATA_DIR/imports/<id>/
#   username         who started the import
#   filename         original upload name (its extension decides the stored source file name)
#   file_size        upload size in bytes
#   status           lifecycle: queued -> running -> completed | failed | cancelled; queued/running imports
#                    found at server start become 'interrupted'. Indexed (idx_imports_status) for active lists.
#   progress         0.0-1.0 completion written by the worker; stage = human-readable current step
#   created_at       when queued; started_at / finished_at when the worker started / stopped (NULL until then)
#   cancel_requested 1 once a user pressed Cancel; the worker polls it and stops
#   schema_label     display name of the target format
#   schema_json      full TargetSchema snapshot, so later edits to the schema file don't change how an old
#                    import is displayed, compared or re-run
#   template_name    template the mapping came from, if any (informational; not a foreign key)
#   read_json        read options snapshot (sheet, header_row, timezone)
#   mapping_json     Mapping snapshot used for this run
#   overrides_json   user-approved value fixes {field_name: {raw_value: fixed_value}} passed to the engine
#   ai_used          1 if Claude helped produce the mapping or fixes
#   parent_id        import this one is a re-run of (NULL for a fresh upload)
#   error            failure / interruption message shown to the user
#   total_rows, imported, rejected, duplicates, blank_rows, blanked
#                    headline counts copied out of the summary so lists can show them cheaply; they are kept
#                    after the files are purged
#   summary_json     the full summary dict (reasons, fill rates, failed values, warnings, ...)
#   files_json       {kind: file name inside the import folder}, set on completion
#   files_purged     1 once purge_files deleted the folder; file_path() then returns None for every kind
#
# user_prefs -- small per-user display preferences, one row per (username, key); added in 0.10.0 as a new table so
#   existing databases need no ALTER TABLE (CREATE TABLE IF NOT EXISTS adds it on the next start)
#   username       the user (lower-cased like users.username; not a foreign key, so it also works for the
#                  AUTH_MODE=none user "local")
#   key            preference name, e.g. "theme"
#   value          preference value as text, e.g. "light" / "dark"
#   updated_at     time of the last change
#
# number_log -- one row per number an import converted (0.21.0; a new table, so existing databases need no ALTER
#   TABLE). It is kept after the import's files are deleted (FILE_RETENTION_DAYS), so the Number history page can
#   say where a number and its consent came from months later; NUMBER_RECORD_DAYS limits how long (default forever).
#   Written by numberlog.record when an import completes. The file name, who imported it and when, and the column
#   the consent came from are read from the import's own row (import_id), which is never deleted.
#   id             surrogate key
#   phone          the number as written to the import (the phone field's output, e.g. 5165550177). Indexed.
#   import_id      the import it came from (imports.id; not a foreign key). Indexed.
#   file_row       the row in the client's file, numbered as Excel shows it
#   phone_value    the phone cell as it was in the file ("(516) 555-0177", "Cell: 555-0177")
#   consent        the consent value written to the import: "true", "false" or "" (no change)
#   consent_value  the consent cell as it was in the file ("Y", "Opted in"); "" when consent wasn't mapped
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    username      TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    last_login    TEXT
);
CREATE TABLE IF NOT EXISTS templates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    fingerprint   TEXT NOT NULL,
    headers_json  TEXT NOT NULL,
    mapping_json  TEXT NOT NULL,
    read_json     TEXT NOT NULL DEFAULT '{}',
    schema_name   TEXT NOT NULL,
    notes         TEXT NOT NULL DEFAULT '',
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_by    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    use_count     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_templates_fp ON templates(fingerprint);
CREATE TABLE IF NOT EXISTS imports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT NOT NULL,
    filename        TEXT NOT NULL,
    file_size       INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,          -- queued | running | completed | failed | cancelled | interrupted
    progress        REAL NOT NULL DEFAULT 0,
    stage           TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    schema_label    TEXT NOT NULL,
    schema_json     TEXT NOT NULL,
    template_name   TEXT,
    read_json       TEXT NOT NULL DEFAULT '{}',
    mapping_json    TEXT NOT NULL,
    overrides_json  TEXT NOT NULL DEFAULT '{}',
    ai_used         INTEGER NOT NULL DEFAULT 0,
    parent_id       INTEGER,
    error           TEXT,
    total_rows      INTEGER,
    imported        INTEGER,
    rejected        INTEGER,
    duplicates      INTEGER,
    blank_rows      INTEGER,
    blanked         INTEGER,
    summary_json    TEXT,
    files_json      TEXT NOT NULL DEFAULT '{}',
    files_purged    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_imports_status ON imports(status);
CREATE TABLE IF NOT EXISTS user_prefs (
    username      TEXT NOT NULL,
    key           TEXT NOT NULL,
    value         TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (username, key)
);
CREATE TABLE IF NOT EXISTS number_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    phone         TEXT NOT NULL,
    import_id     INTEGER NOT NULL,
    file_row      INTEGER,
    phone_value   TEXT NOT NULL DEFAULT '',
    consent       TEXT NOT NULL DEFAULT '',
    consent_value TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_number_log_phone ON number_log(phone);
CREATE INDEX IF NOT EXISTS idx_number_log_import ON number_log(import_id);
"""


# Statuses in which the background worker still owns the import (shown as "in progress", can be cancelled, and
# must not have their files purged) versus statuses that are final and never change again.
ACTIVE_STATUSES = ("queued", "running")
FINAL_STATUSES = ("completed", "failed", "cancelled", "interrupted")


def _now() -> str:
    """Return the current UTC time as an ISO-8601 string with seconds precision (e.g. ``2026-10-10T14:03:07+00:00``).

    Every timestamp column is written through this helper so all stored values share one format; that keeps them
    readable and lets SQL compare them as plain text (see ``Store.purge_files``).
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(value: str | None) -> datetime | None:
    """Parse a stored timestamp back into a timezone-aware ``datetime``.

    Args:
        value: an ISO-8601 string as written by ``_now()``, or ``None`` / ``""`` for "not set yet".

    Returns:
        ``None`` for an empty value; otherwise an aware datetime. Naive values (e.g. written by an older version
        or by hand) are assumed to be UTC, so arithmetic with ``datetime.now(timezone.utc)`` never raises.
    """
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class ImportRecord:
    """One row of the ``imports`` table, with JSON columns decoded and 0/1 flags turned into booleans.

    Built by ``Store._row_to_import``; returned by ``get_import`` / ``list_imports``. It is a read-only snapshot:
    changing attributes does not write anything back -- use the ``Store`` methods for that.

    Notable attributes (see the schema comment above ``SCHEMA_SQL`` for every column):
        read_options: decoded ``read_json`` (sheet, header_row, timezone).
        mapping: decoded ``mapping_json`` as a ``Mapping``.
        overrides: decoded ``overrides_json`` -- approved fixes ``{field_name: {raw_value: fixed_value}}``.
        summary: decoded ``summary_json`` (``{}`` until the import completes).
        files: decoded ``files_json`` -- ``{kind: file name}`` inside the import folder.
        files_purged: True once the retention purge deleted the folder.
    """

    id: int
    username: str
    filename: str
    file_size: int
    status: str
    progress: float
    stage: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    cancel_requested: bool
    schema_label: str
    schema_json: str
    template_name: str | None
    read_options: dict[str, Any]
    mapping: Mapping
    overrides: dict[str, dict[str, str]]
    ai_used: bool
    parent_id: int | None
    error: str | None
    total_rows: int | None
    imported: int | None
    rejected: int | None
    duplicates: int | None
    blank_rows: int | None
    blanked: int | None
    summary: dict[str, Any]
    files: dict[str, str]
    files_purged: bool

    @property
    def active(self) -> bool:
        """True while the import is queued or running (the worker may still change it; it can be cancelled)."""
        return self.status in ACTIVE_STATUSES

    def elapsed(self, now: datetime | None = None) -> timedelta:
        """Queued: time waiting. Running: time so far. Finished: total run time.

        Args:
            now: reference time (defaults to the current UTC time); passed in by tests and by callers that render
                many records and want one consistent "now".

        An import that finished without ever starting (e.g. cancelled or interrupted while queued) has no
        ``started_at``; its elapsed time is then measured from ``created_at`` to ``finished_at``.
        """
        now = now or datetime.now(timezone.utc)
        start = parse_ts(self.started_at)
        end = parse_ts(self.finished_at)
        if self.status == "queued" or start is None:
            return (end or now) - parse_ts(self.created_at)
        return (end or now) - start

    def eta(self, now: datetime | None = None) -> timedelta | None:
        """Estimated time remaining for a running import, or ``None`` when no sensible estimate exists.

        Linear extrapolation: if ``progress`` of the work took ``elapsed`` seconds, the remaining
        ``1 - progress`` will take proportionally long. No estimate is given below 5 % progress because the first
        stage (reading the file) is not representative and would produce wildly wrong numbers.
        """
        if self.status != "running" or self.progress < 0.05:
            return None
        el = self.elapsed(now).total_seconds()
        return timedelta(seconds=el / self.progress * (1 - self.progress))


@dataclass
class Draft:
    """A file being mapped on New import, kept on disk so a browser refresh doesn't lose the work (since 0.15.0).

    Attributes:
        id: random token, also in the page's URL (?draft=<id>); only that browser tab knows it.
        username: who uploaded the file; a draft is only restored for the same user.
        filename: the uploaded file's name.
        data: the uploaded bytes.
        state: what the app saved last (how the file was read, the mapping, time zone, template name...); ``{}``
            until the first save.
    """

    id: str
    username: str
    filename: str
    data: bytes
    state: dict[str, Any]


# Drafts nobody changed for this long are deleted (they hold a client's file, like an import's stored files).
DRAFT_MAX_AGE_HOURS = 24
# Regex: a draft id as made by secrets.token_urlsafe(18) (24 URL-safe characters). Anything else is refused before
# it gets near a path, so an edited URL can't point outside the drafts folder.
_DRAFT_ID_RE = re.compile(r"[A-Za-z0-9_-]{20,64}")


@dataclass
class Template:
    """One row of the ``templates`` table: a saved mapping for a particular header layout and target format.

    Attributes:
        fingerprint: ``header_fingerprint(headers)`` -- identical for files with the same set of column names,
            regardless of column order, case or punctuation.
        headers: the header row the template was saved from (used for fuzzy matching and display).
        mapping: the saved ``Mapping``.
        read_options: read options saved with it (sheet, header_row, timezone).
        schema_name: the target format it applies to; templates are never matched across formats.
        use_count: how often it was applied; breaks ties between several exact matches.
    """

    id: int
    name: str
    fingerprint: str
    headers: list[str]
    mapping: Mapping
    read_options: dict[str, Any]
    schema_name: str
    notes: str
    created_by: str
    created_at: str
    updated_by: str
    updated_at: str
    use_count: int


class Store:
    """Access layer for the SQLite database and the per-import file folders.

    One instance is cheap and stateless apart from ``db_path``: it holds no open connection, so it is safe to
    share across threads (Streamlit sessions and the import worker pool). Methods are grouped into users,
    templates and imports. All SQL uses ``?`` placeholders; user input is never formatted into SQL text.
    """

    def __init__(self, db_path: str | Path):
        """Open (or create) the database at ``db_path`` and make sure all tables and indexes exist.

        Side effects: creates the parent directory (the data directory) if needed, creates the database file on
        first use and runs ``SCHEMA_SQL`` (idempotent -- existing tables are left untouched).
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as con:
            con.executescript(SCHEMA_SQL)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """Yield a fresh connection that commits on success and is always closed.

        Why a new connection per call: ``sqlite3`` connections are bound to the thread that created them, and the
        app calls the store from many threads. Opening a connection to a local SQLite file is cheap, and short
        transactions keep lock times small.

        Settings applied to every connection:
            * ``timeout=30`` -- wait up to 30 s for another writer's lock instead of raising "database is locked".
            * ``row_factory = sqlite3.Row`` -- rows can be read by column name.
            * ``journal_mode=WAL`` -- write-ahead logging: readers (progress polling from many browsers) are not
              blocked by the worker's writes, and vice versa. The mode is stored in the database file, so repeating
              the PRAGMA is a cheap no-op after the first time.
            * ``foreign_keys=ON`` -- enforce foreign keys (the current schema declares none; kept for safety).

        If the ``with`` body raises, ``commit()`` is skipped and closing the connection discards the open
        transaction, i.e. the changes are rolled back.
        """
        con = sqlite3.connect(self.db_path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        try:
            yield con
            con.commit()
        finally:
            con.close()

    # ------------------------------------------------------------------ users
    def user_count(self) -> int:
        """Return the number of user accounts (0 on a fresh install, which lets the app offer first-admin setup)."""
        with self._conn() as con:
            return con.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def add_user(self, username: str, password: str, is_admin: bool = False) -> None:
        """Create a user account with a bcrypt-hashed password.

        Args:
            username: login name; stored stripped and lower-case so logins are case-insensitive.
            password: plain-text password, at least 8 characters; only its bcrypt hash is stored.
            is_admin: grant admin rights.

        Raises:
            ValueError: empty username/password or a password shorter than 8 characters.
            sqlite3.IntegrityError: the username already exists (primary-key violation).

        Writes: inserts one row into ``users``.
        """
        username = username.strip().lower()
        if not username or not password:
            raise ValueError("username and password are required")
        if len(password) < 8:
            raise ValueError("password must be at least 8 characters")
        # gensalt() makes a new random salt per hash; the salt and cost factor are embedded in the hash string.
        pw = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        with self._conn() as con:
            con.execute(
                "INSERT INTO users(username, password_hash, is_admin, created_at) VALUES (?,?,?,?)",
                (username, pw, int(is_admin), _now()),
            )

    def set_password(self, username: str, password: str) -> None:
        """Replace a user's password (same 8-character minimum as ``add_user``).

        Raises:
            ValueError: password too short.
            KeyError: no such user (detected because the UPDATE changed no row).

        Writes: ``users.password_hash`` for that user.
        """
        if len(password) < 8:
            raise ValueError("password must be at least 8 characters")
        pw = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        with self._conn() as con:
            cur = con.execute("UPDATE users SET password_hash=? WHERE username=?", (pw, username.strip().lower()))
            if cur.rowcount == 0:
                raise KeyError(username)

    def set_admin(self, username: str, is_admin: bool) -> None:
        """Grant or revoke admin rights. Silently does nothing for an unknown username. Writes ``users.is_admin``."""
        with self._conn() as con:
            con.execute("UPDATE users SET is_admin=? WHERE username=?", (int(is_admin), username.strip().lower()))

    def delete_user(self, username: str) -> None:
        """Delete a user account (no-op if it doesn't exist).

        The ``users`` row and the user's display preferences (``user_prefs``) are removed; the user's imports and
        templates keep their username as plain text.
        """
        with self._conn() as con:
            con.execute("DELETE FROM users WHERE username=?", (username.strip().lower(),))
            con.execute("DELETE FROM user_prefs WHERE username=?", (username.strip().lower(),))

    def get_user_pref(self, username: str, key: str) -> str | None:
        """Return one stored display preference of a user (e.g. key "theme" -> "dark"), or ``None`` if never set.

        Preferences follow the user across browsers and sessions (Streamlit sessions end on a page refresh).
        """
        with self._conn() as con:
            row = con.execute("SELECT value FROM user_prefs WHERE username=? AND key=?",
                              (username.strip().lower(), key)).fetchone()
        return row["value"] if row else None

    def set_user_pref(self, username: str, key: str, value: str) -> None:
        """Store (insert or replace) one display preference of a user. Writes ``user_prefs``."""
        with self._conn() as con:
            con.execute(
                "INSERT INTO user_prefs(username, key, value, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(username, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (username.strip().lower(), key, value, _now()),
            )

    def verify_user(self, username: str, password: str) -> dict | None:
        """Check a login attempt.

        Returns:
            ``{"username": ..., "is_admin": bool}`` when the user exists and the password matches the stored bcrypt
            hash, otherwise ``None`` (the caller can't tell "unknown user" from "wrong password", on purpose).

        Side effect: on success, records the time in ``users.last_login``.
        """
        username = username.strip().lower()
        with self._conn() as con:
            row = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            if row is None or not bcrypt.checkpw(password.encode(), row["password_hash"].encode()):
                return None
            con.execute("UPDATE users SET last_login=? WHERE username=?", (_now(), username))
            return {"username": row["username"], "is_admin": bool(row["is_admin"])}

    def list_users(self) -> list[dict]:
        """Return all users as dicts (username, is_admin, created_at, last_login), sorted by name.

        Password hashes are deliberately not selected, so the result is safe to display.
        """
        with self._conn() as con:
            rows = con.execute("SELECT username, is_admin, created_at, last_login FROM users ORDER BY username")
            return [dict(r) for r in rows]

    # -------------------------------------------------------------- templates
    def _row_to_template(self, r: sqlite3.Row) -> Template:
        """Convert a ``templates`` row into a ``Template``, decoding the JSON columns (headers, mapping, read)."""
        return Template(
            id=r["id"], name=r["name"], fingerprint=r["fingerprint"], headers=json.loads(r["headers_json"]),
            mapping=Mapping.model_validate_json(r["mapping_json"]), read_options=json.loads(r["read_json"]),
            schema_name=r["schema_name"], notes=r["notes"], created_by=r["created_by"], created_at=r["created_at"],
            updated_by=r["updated_by"], updated_at=r["updated_at"], use_count=r["use_count"],
        )

    def save_template(self, name: str, headers: list[str], mapping: Mapping, read_options: dict, schema_name: str,
                      username: str, notes: str = "") -> Template:
        """Create a template, or overwrite the one that already has this name ("save as" / "update").

        Args:
            name: template name (stripped; must not be empty). Names are unique across all target formats.
            headers: the header row of the file the mapping was made for; its fingerprint is recomputed here, so
                updating a template with a different file also moves it to that file's layout.
            mapping, read_options, schema_name, notes: what to store (see ``Template``).
            username: recorded as ``updated_by`` (and ``created_by`` for a new template).

        Returns:
            The stored template, re-read from the database.

        Raises:
            ValueError: an empty name, or ``headers`` of a file without a header row (all "Column N").

        On update, ``created_by/created_at`` and ``use_count`` are preserved; only the content and the
        ``updated_*`` columns change. Writes: one INSERT or UPDATE on ``templates``.
        """
        name = name.strip()
        if not name:
            raise ValueError("template name is required")
        # Files without a header row don't use templates (see find_template). Callers check first; this is a backstop.
        if has_generic_columns(headers):
            raise ValueError("files without a header row are not saved as templates")
        fp = header_fingerprint(headers)
        now = _now()
        with self._conn() as con:
            # Check-then-write inside one connection/transaction, keyed by the UNIQUE name.
            existing = con.execute("SELECT id FROM templates WHERE name=?", (name,)).fetchone()
            if existing:
                con.execute(
                    "UPDATE templates SET fingerprint=?, headers_json=?, mapping_json=?, read_json=?, schema_name=?, "
                    "notes=?, updated_by=?, updated_at=? WHERE id=?",
                    (fp, json.dumps(headers), mapping.model_dump_json(), json.dumps(read_options), schema_name, notes,
                     username, now, existing["id"]),
                )
                tid = existing["id"]
            else:
                cur = con.execute(
                    "INSERT INTO templates(name, fingerprint, headers_json, mapping_json, read_json, schema_name, "
                    "notes, created_by, created_at, updated_by, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (name, fp, json.dumps(headers), mapping.model_dump_json(), json.dumps(read_options), schema_name,
                     notes, username, now, username, now),
                )
                tid = cur.lastrowid
            row = con.execute("SELECT * FROM templates WHERE id=?", (tid,)).fetchone()
        return self._row_to_template(row)

    def list_templates(self, schema_name: str | None = None) -> list[Template]:
        """Return saved templates sorted by name, optionally only those for one target format (``schema_name``)."""
        with self._conn() as con:
            if schema_name:
                rows = con.execute("SELECT * FROM templates WHERE schema_name=? ORDER BY name", (schema_name,))
            else:
                rows = con.execute("SELECT * FROM templates ORDER BY name")
            return [self._row_to_template(r) for r in rows]

    def get_template(self, name: str) -> Template | None:
        """Return the template with exactly this name, or ``None``."""
        with self._conn() as con:
            r = con.execute("SELECT * FROM templates WHERE name=?", (name,)).fetchone()
            return self._row_to_template(r) if r else None

    def delete_template(self, name: str) -> None:
        """Delete a template by name (no-op if missing). Imports that used it keep their own mapping snapshot."""
        with self._conn() as con:
            con.execute("DELETE FROM templates WHERE name=?", (name,))

    def rename_template(self, old: str, new: str) -> None:
        """Rename a template. ``new`` is stripped; renaming onto an existing name raises ``sqlite3.IntegrityError``
        because of the UNIQUE constraint on ``templates.name``. Imports keep the old name in ``template_name``.
        """
        with self._conn() as con:
            con.execute("UPDATE templates SET name=? WHERE name=?", (new.strip(), old))

    def mark_template_used(self, name: str) -> None:
        """Increment ``use_count`` (done atomically in SQL, so concurrent uses are not lost)."""
        with self._conn() as con:
            con.execute("UPDATE templates SET use_count = use_count + 1 WHERE name=?", (name,))

    def find_template(self, headers: list[str], schema_name: str, min_similarity: float = 0.75
                      ) -> tuple[Template | None, float]:
        """Exact header-set match first, otherwise the most similar template above min_similarity.

        Template matching works on the *set* of normalised header names (lower-case, letters and digits only):

        1. Exact: ``header_fingerprint(headers)`` equals a template's stored fingerprint, i.e. the file has exactly
           the same column names (any order). If several templates match, the most used one wins, then the most
           recently updated. Returned similarity is ``1.0`` -- callers treat that as "apply automatically".
        2. Fuzzy: otherwise the template with the highest ``header_similarity`` (Jaccard index: shared names /
           all names) is returned if it reaches ``min_similarity``. Callers use it as a starting point only.

        Only templates for ``schema_name`` are considered. A file without a header row (columns named "Column 1",
        "Column 2", ... by the reader) never matches: those names say nothing about its layout, so two clients'
        headerless files with the same number of columns would share a template. (Such files aren't saved as
        templates either, see save_template; this also keeps templates saved before 0.12.1 from matching.)

        Returns:
            ``(template, similarity)``; ``(None, best_similarity_seen)`` when nothing qualifies, so the UI can
            still say how close the nearest template was.
        """
        if has_generic_columns(headers):
            return None, 0.0
        fp = header_fingerprint(headers)
        templates = self.list_templates(schema_name)
        exact = [t for t in templates if t.fingerprint == fp]
        if exact:
            # ISO timestamps compare correctly as strings, so updated_at works as a recency tie-breaker.
            return max(exact, key=lambda t: (t.use_count, t.updated_at)), 1.0
        best, best_sim = None, 0.0
        for t in templates:
            sim = header_similarity(headers, t.headers)
            if sim > best_sim:
                best, best_sim = t, sim
        if best is not None and best_sim >= min_similarity:
            return best, best_sim
        return None, best_sim

    # ---------------------------------------------------------------- imports
    @property
    def files_root(self) -> Path:
        """Folder holding one sub-folder per import: ``<directory of the database>/imports`` (i.e. DATA_DIR/imports)."""
        return self.db_path.parent / "imports"

    def import_dir(self, import_id: int) -> Path:
        """Return the folder for one import, creating it if needed.

        ``int(import_id)`` makes sure only a number ever becomes part of the path (no path tricks from strings).
        """
        d = self.files_root / str(int(import_id))
        d.mkdir(parents=True, exist_ok=True)
        return d

    def create_import(self, *, username: str, filename: str, data: bytes, schema_label: str, schema_json: str,
                      mapping: Mapping, read_options: dict[str, Any], overrides: dict | None = None,
                      template_name: str | None = None, ai_used: bool = False, parent_id: int | None = None) -> int:
        """Queue a new import and store its uploaded file. Returns the new import id.

        Everything the worker needs is snapshotted into the row (schema JSON, mapping, read options, approved
        fixes), so the run is reproducible even if the schema file or templates change later. Used both for new
        uploads and for re-runs (``parent_id`` = the import being re-run).

        Side effects:
            * inserts a row into ``imports`` with status ``queued`` and stage "Waiting to start";
            * writes ``data`` to ``DATA_DIR/imports/<id>/source<ext>`` (extension lower-cased from ``filename``).

        The row is inserted first because its auto-increment id names the folder. The file is written after the
        transaction commits. If that write raises, the exception reaches the caller before it submits the id to the
        ``ImportRunner``, so the row stays ``queued`` until ``mark_interrupted`` closes it at the next start.
        """
        with self._conn() as con:
            cur = con.execute(
                "INSERT INTO imports(username, filename, file_size, status, stage, created_at, schema_label, "
                "schema_json, template_name, read_json, mapping_json, overrides_json, ai_used, parent_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (username, filename, len(data), "queued", "Waiting to start", _now(), schema_label, schema_json,
                 template_name, json.dumps(read_options), mapping.model_dump_json(), json.dumps(overrides or {}),
                 int(ai_used), parent_id),
            )
            import_id = cur.lastrowid
        # Stored as "source" + extension (not the user's file name) so names with odd characters can't cause
        # problems on disk; the original name is kept in the filename column for downloads.
        (self.import_dir(import_id) / ("source" + Path(filename).suffix.lower())).write_bytes(data)
        return import_id

    def source_path(self, rec: ImportRecord) -> Path:
        """Path of the stored original upload for ``rec``.

        Computed, not checked: the file may be gone after a purge, so callers test ``.exists()`` (e.g. before
        offering a download or a re-run). Unlike ``import_dir`` this does not create the folder.
        """
        return self.files_root / str(rec.id) / ("source" + Path(rec.filename).suffix.lower())

    def file_path(self, rec: ImportRecord, kind: str) -> Path | None:
        """Path of one result file of a completed import, or ``None`` if it is not available.

        Args:
            rec: the import.
            kind: a key of ``rec.files`` as written by ``jobs.run_import`` -- e.g. "output", "rejects",
                "summary", "audit", "source_table", "source".

        Returns ``None`` when the kind is unknown, the files were purged, or the file is missing on disk.
        """
        name = rec.files.get(kind)
        if not name or rec.files_purged:
            return None
        p = self.files_root / str(rec.id) / name
        return p if p.exists() else None

    def _row_to_import(self, r: sqlite3.Row) -> ImportRecord:
        """Convert an ``imports`` row into an ``ImportRecord``.

        Decodes the JSON columns (treating NULL/empty as ``{}``, which covers rows that never completed) and turns
        the 0/1 integer flags into booleans.
        """
        return ImportRecord(
            id=r["id"], username=r["username"], filename=r["filename"], file_size=r["file_size"], status=r["status"],
            progress=r["progress"], stage=r["stage"], created_at=r["created_at"], started_at=r["started_at"],
            finished_at=r["finished_at"], cancel_requested=bool(r["cancel_requested"]),
            schema_label=r["schema_label"], schema_json=r["schema_json"], template_name=r["template_name"],
            read_options=json.loads(r["read_json"] or "{}"), mapping=Mapping.model_validate_json(r["mapping_json"]),
            overrides=json.loads(r["overrides_json"] or "{}"), ai_used=bool(r["ai_used"]), parent_id=r["parent_id"],
            error=r["error"], total_rows=r["total_rows"], imported=r["imported"], rejected=r["rejected"],
            duplicates=r["duplicates"], blank_rows=r["blank_rows"], blanked=r["blanked"],
            summary=json.loads(r["summary_json"] or "{}"), files=json.loads(r["files_json"] or "{}"),
            files_purged=bool(r["files_purged"]),
        )

    def get_import(self, import_id: int) -> ImportRecord | None:
        """Return one import by id, or ``None``. Polled by the UI to show live progress."""
        with self._conn() as con:
            r = con.execute("SELECT * FROM imports WHERE id=?", (int(import_id),)).fetchone()
            return self._row_to_import(r) if r else None

    def list_imports(self, limit: int = 500, username: str | None = None, statuses: list[str] | None = None,
                     search: str | None = None) -> list[ImportRecord]:
        """Return imports, newest first, optionally filtered.

        Args:
            limit: maximum number of records.
            username: only imports started by this user.
            statuses: only these statuses (e.g. ``ACTIVE_STATUSES``).
            search: matches a substring of the file name or template name, or an exact import id with or without
                a leading ``#`` ("#42"). ``%`` and ``_`` typed by the user act as LIKE wildcards.

        The WHERE clause is assembled from fixed fragments only; every user value goes through a ``?`` placeholder
        (one per status in the IN list), so the dynamic SQL is not open to injection.
        """
        sql, args = "SELECT * FROM imports WHERE 1=1", []
        if username:
            sql += " AND username=?"
            args.append(username)
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args += statuses
        if search:
            sql += " AND (filename LIKE ? OR template_name LIKE ? OR CAST(id AS TEXT)=?)"
            args += [f"%{search}%", f"%{search}%", search.lstrip("#")]
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._conn() as con:
            return [self._row_to_import(r) for r in con.execute(sql, args)]

    def active_imports(self) -> list[ImportRecord]:
        """Return all queued or running imports (newest first), e.g. for the "in progress" list."""
        return self.list_imports(limit=1000, statuses=list(ACTIVE_STATUSES))

    def start_import(self, import_id: int) -> None:
        """Mark an import as running (called by the worker when it picks the import up).

        Writes ``status='running'``, ``started_at``, ``stage='Starting'`` and resets ``progress`` to 0.
        """
        with self._conn() as con:
            con.execute("UPDATE imports SET status='running', started_at=?, stage='Starting', progress=0 "
                        "WHERE id=?", (_now(), import_id))

    def update_progress(self, import_id: int, progress: float, stage: str) -> None:
        """Record progress (clamped to 0..1) and the current stage text (cut to 200 characters).

        Only applies while the import is ``running``: a late progress write from the worker can therefore never
        overwrite a final status or move a finished import's progress bar. The worker throttles these writes (see
        ``jobs.ProgressTracker``) to keep database traffic low.
        """
        with self._conn() as con:
            con.execute("UPDATE imports SET progress=?, stage=? WHERE id=? AND status='running'",
                        (max(0.0, min(1.0, progress)), stage[:200], import_id))

    def complete_import(self, import_id: int, summary: dict[str, Any], files: dict[str, str]) -> None:
        """Mark an import as successfully completed and store its results.

        Args:
            summary: ``ConversionResult.summary_dict()`` plus extras from the worker; must contain the keys
                total_rows, imported, rejected, duplicates, blank_rows and blanked_values.
            files: ``{kind: file name}`` of the files written to the import folder.

        The headline counts are copied into their own columns (so lists don't need to parse JSON and they survive
        as plain numbers); the whole summary is stored as JSON too. Note ``blanked_values`` -> column ``blanked``.
        """
        with self._conn() as con:
            con.execute(
                "UPDATE imports SET status='completed', progress=1, stage='Done', finished_at=?, total_rows=?, "
                "imported=?, rejected=?, duplicates=?, blank_rows=?, blanked=?, summary_json=?, files_json=? "
                "WHERE id=?",
                (_now(), summary["total_rows"], summary["imported"], summary["rejected"], summary["duplicates"],
                 summary["blank_rows"], summary["blanked_values"], json.dumps(summary), json.dumps(files),
                 import_id),
            )

    def finish_import(self, import_id: int, status: str, error: str | None = None, stage: str = "") -> None:
        """Close an import with a final status other than a normal completion (``failed`` / ``cancelled`` ...).

        Args:
            status: one of ``FINAL_STATUSES`` (asserted -- a programming error otherwise).
            error: message shown to the user (e.g. the exception text for ``failed``).
            stage: final stage text; defaults to the capitalised status.

        Successful runs use ``complete_import`` instead, which also stores counts and files.
        """
        assert status in FINAL_STATUSES
        with self._conn() as con:
            con.execute("UPDATE imports SET status=?, error=?, stage=?, finished_at=? WHERE id=?",
                        (status, error, stage or status.capitalize(), _now(), import_id))

    def request_cancel(self, import_id: int) -> None:
        """Ask for an import to be cancelled (sets ``cancel_requested``; only for queued/running imports).

        Cancellation is cooperative: nothing is stopped here. A queued import is closed as cancelled when the
        worker picks it up; a running one stops at the worker's next ``is_cancel_requested`` check.
        """
        with self._conn() as con:
            con.execute("UPDATE imports SET cancel_requested=1 WHERE id=? AND status IN ('queued','running')",
                        (import_id,))

    def is_cancel_requested(self, import_id: int) -> bool:
        """Return True if a user asked to cancel this import (False also for an unknown id)."""
        with self._conn() as con:
            r = con.execute("SELECT cancel_requested FROM imports WHERE id=?", (import_id,)).fetchone()
            return bool(r and r[0])

    def mark_interrupted(self) -> int:
        """Called at startup: imports that were queued/running when the server stopped can't resume.

        The worker threads and their in-memory queue died with the old process, so any import still marked
        queued/running would otherwise show as "in progress" forever. They are closed as ``interrupted`` with an
        explanatory error; the user can re-run them (the source file is still on disk).

        Returns:
            The number of imports changed (logged by ``jobs.ImportRunner``).
        """
        with self._conn() as con:
            cur = con.execute(
                "UPDATE imports SET status='interrupted', finished_at=?, stage='Interrupted', "
                "error='The server restarted while this import was waiting or running. Use Re-run to start it again.' "
                "WHERE status IN ('queued','running')", (_now(),))
            return cur.rowcount

    # ---------------------------------------------------------------- drafts (New import, before "Start import")
    @property
    def drafts_root(self) -> Path:
        """Folder with one sub-folder per draft: DATA_DIR/drafts/<draft id>/ (file, meta.json, state.json)."""
        return self.db_path.parent / "drafts"

    def _draft_dir(self, draft_id: str | None) -> Path | None:
        """The folder of a draft id, or None for anything that isn't a well-formed id (see _DRAFT_ID_RE)."""
        if not draft_id or not _DRAFT_ID_RE.fullmatch(draft_id):
            return None
        return self.drafts_root / draft_id

    def create_draft(self, username: str, filename: str, data: bytes) -> str:
        """Keep an uploaded file as a new draft and return its id (a random, unguessable token).

        Side effects: writes DATA_DIR/drafts/<id>/file and meta.json, and deletes drafts nobody changed for
        DRAFT_MAX_AGE_HOURS (purge_drafts), so abandoned uploads don't pile up.
        """
        self.purge_drafts()
        draft_id = secrets.token_urlsafe(18)
        d = self.drafts_root / draft_id
        d.mkdir(parents=True)
        (d / "file").write_bytes(data)
        (d / "meta.json").write_text(json.dumps({"username": username, "filename": filename,
                                                 "created_at": _now()}), encoding="utf-8")
        return draft_id

    def save_draft_state(self, draft_id: str, state: dict[str, Any]) -> bool:
        """Store the app's state for a draft (replacing the previous one). Returns False if the draft is gone.

        Written to a temporary file and renamed, so a reader never sees half a file. Each save also marks the
        draft as recently used (the purge goes by the time of the last change).
        """
        d = self._draft_dir(draft_id)
        if d is None or not (d / "file").exists():
            return False
        tmp = d / "state.json.tmp"
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(d / "state.json")
        return True

    def load_draft(self, draft_id: str | None, username: str) -> Draft | None:
        """The draft with this id if it exists, belongs to `username` and isn't expired; otherwise None."""
        d = self._draft_dir(draft_id)
        try:
            if d is None or self._draft_expired(d):
                return None
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
            if meta.get("username") != username:
                return None
            state_p = d / "state.json"
            state = json.loads(state_p.read_text(encoding="utf-8")) if state_p.exists() else {}
            return Draft(id=draft_id, username=meta["username"], filename=meta["filename"],
                         data=(d / "file").read_bytes(), state=state)
        except (OSError, ValueError, KeyError):
            return None

    def delete_draft(self, draft_id: str | None) -> None:
        """Delete a draft's folder (after "Start import", "Start over" or a different upload). Unknown ids: no-op."""
        d = self._draft_dir(draft_id)
        if d is not None:
            shutil.rmtree(d, ignore_errors=True)

    @staticmethod
    def _draft_expired(d: Path, max_age_hours: float = DRAFT_MAX_AGE_HOURS) -> bool:
        """True when nothing in the draft folder changed for `max_age_hours` (or the folder is missing)."""
        try:
            last = max(p.stat().st_mtime for p in d.iterdir())
        except (OSError, ValueError):  # missing folder, or an empty one
            return True
        return time.time() - last > max_age_hours * 3600

    def purge_drafts(self, max_age_hours: float = DRAFT_MAX_AGE_HOURS) -> int:
        """Delete drafts nobody changed for `max_age_hours`; returns how many. Called on every new draft and by
        the runner's regular purge. A draft holds a client's file, so it isn't kept longer than needed."""
        if not self.drafts_root.exists():
            return 0
        n = 0
        for d in self.drafts_root.iterdir():
            if d.is_dir() and self._draft_expired(d, max_age_hours):
                shutil.rmtree(d, ignore_errors=True)
                n += 1
        return n

    def purge_files(self, older_than_days: int) -> int:
        """Delete stored files of finished imports older than N days (records and counts are kept).

        File retention: uploads and results can contain customer contact data, so they are only kept for
        ``FILE_RETENTION_DAYS`` (passed in as ``older_than_days``; 0 or less disables purging). The import rows,
        their counts and summary stay for history.

        Selection: imports not already purged, not queued/running (never delete files a worker is using), whose
        ``finished_at`` -- or ``created_at`` if they never finished -- is before the cutoff. The cutoff is built
        in the same ISO format as ``_now()``, so the text comparison in SQL is a correct time comparison.

        Side effects: removes each ``DATA_DIR/imports/<id>/`` folder (errors such as an already missing folder are
        ignored) and sets ``files_purged=1`` on its row.

        Returns:
            The number of imports purged.
        """
        if older_than_days <= 0:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat(timespec="seconds")
        with self._conn() as con:
            rows = con.execute(
                "SELECT id FROM imports WHERE files_purged=0 AND status NOT IN ('queued','running') "
                "AND COALESCE(finished_at, created_at) < ?", (cutoff,)).fetchall()
            for r in rows:
                shutil.rmtree(self.files_root / str(r["id"]), ignore_errors=True)
                con.execute("UPDATE imports SET files_purged=1 WHERE id=?", (r["id"],))
            return len(rows)

    # ------------------------------------------------------------------ number history (number_log)

    def log_numbers(self, import_id: int, rows: list[tuple[str, int | None, str, str, str]]) -> int:
        """Record the numbers an import converted: ``rows`` of (phone, file_row, phone_value, consent,
        consent_value), see the number_log table. Replaces what was recorded for this import before, so recording
        twice (a backfill after a crash) never doubles it. Returns the number of rows written."""
        with self._conn() as con:
            con.execute("DELETE FROM number_log WHERE import_id=?", (import_id,))
            con.executemany("INSERT INTO number_log (phone, import_id, file_row, phone_value, consent, consent_value) "
                            "VALUES (?,?,?,?,?,?)", ((r[0], import_id, *r[1:]) for r in rows))
        return len(rows)

    def number_history(self, phone: str, limit: int = 1000) -> list[dict[str, Any]]:
        """Every recorded row for this number (exactly as written to the import, e.g. "5165550177"), newest import
        first, as dicts with the number_log columns. At most ``limit`` rows."""
        with self._conn() as con:
            rows = con.execute(
                "SELECT n.import_id, n.file_row, n.phone_value, n.consent, n.consent_value FROM number_log n "
                "JOIN imports i ON i.id = n.import_id WHERE n.phone=? "
                "ORDER BY COALESCE(i.finished_at, i.created_at) DESC, n.import_id DESC, n.file_row LIMIT ?",
                (phone, limit)).fetchall()
        return [dict(r) for r in rows]

    def numbers_logged(self, import_id: int) -> int:
        """How many numbers are recorded for an import (0 before 0.21.0, or after NUMBER_RECORD_DAYS)."""
        with self._conn() as con:
            return con.execute("SELECT COUNT(*) FROM number_log WHERE import_id=?", (import_id,)).fetchone()[0]

    def imports_to_log(self, finished_since: str | None = None) -> list[int]:
        """Ids of completed imports that converted numbers, still have their files, and have nothing recorded in
        number_log (imports from before 0.21.0): what numberlog.backfill records. ``finished_since`` (an ISO time
        as written by _now) leaves out older imports, so records removed after NUMBER_RECORD_DAYS don't come back.
        """
        sql = ("SELECT id FROM imports i WHERE status='completed' AND files_purged=0 AND COALESCE(imported, 0) > 0 "
               "AND NOT EXISTS (SELECT 1 FROM number_log n WHERE n.import_id = i.id)")
        args: tuple = ()
        if finished_since:
            sql += " AND finished_at >= ?"
            args = (finished_since,)
        with self._conn() as con:
            return [r["id"] for r in con.execute(sql + " ORDER BY id", args).fetchall()]

    def purge_number_log(self, older_than_days: int) -> int:
        """Delete the recorded numbers of imports that finished more than N days ago (``NUMBER_RECORD_DAYS``; 0 or
        less keeps them forever). The import rows themselves stay, like with purge_files. Returns the rows deleted.
        """
        if older_than_days <= 0:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat(timespec="seconds")
        with self._conn() as con:
            cur = con.execute("DELETE FROM number_log WHERE import_id IN (SELECT id FROM imports "
                              "WHERE COALESCE(finished_at, created_at) < ?)", (cutoff,))
            return cur.rowcount
