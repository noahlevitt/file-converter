"""SQLite storage for users, saved mapping templates and imports (status, progress, results).

Each import's files (original upload, converted file, rejected rows, summary, comparison data) live in
DATA_DIR/imports/<id>/ and are deleted after FILE_RETENTION_DAYS; the import record and its counts stay.
SQLite in WAL mode comfortably handles an internal tool with 5–20 concurrent users.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import bcrypt

from .mapping import Mapping, header_fingerprint, header_similarity

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
"""


ACTIVE_STATUSES = ("queued", "running")
FINAL_STATUSES = ("completed", "failed", "cancelled", "interrupted")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class ImportRecord:
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
        return self.status in ACTIVE_STATUSES

    def elapsed(self, now: datetime | None = None) -> timedelta:
        """Queued: time waiting. Running: time so far. Finished: total run time."""
        now = now or datetime.now(timezone.utc)
        start = parse_ts(self.started_at)
        end = parse_ts(self.finished_at)
        if self.status == "queued" or start is None:
            return (end or now) - parse_ts(self.created_at)
        return (end or now) - start

    def eta(self, now: datetime | None = None) -> timedelta | None:
        if self.status != "running" or self.progress < 0.05:
            return None
        el = self.elapsed(now).total_seconds()
        return timedelta(seconds=el / self.progress * (1 - self.progress))


@dataclass
class Template:
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
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as con:
            con.executescript(SCHEMA_SQL)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
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
        with self._conn() as con:
            return con.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def add_user(self, username: str, password: str, is_admin: bool = False) -> None:
        username = username.strip().lower()
        if not username or not password:
            raise ValueError("username and password are required")
        if len(password) < 8:
            raise ValueError("password must be at least 8 characters")
        pw = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        with self._conn() as con:
            con.execute(
                "INSERT INTO users(username, password_hash, is_admin, created_at) VALUES (?,?,?,?)",
                (username, pw, int(is_admin), _now()),
            )

    def set_password(self, username: str, password: str) -> None:
        if len(password) < 8:
            raise ValueError("password must be at least 8 characters")
        pw = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        with self._conn() as con:
            cur = con.execute("UPDATE users SET password_hash=? WHERE username=?", (pw, username.strip().lower()))
            if cur.rowcount == 0:
                raise KeyError(username)

    def set_admin(self, username: str, is_admin: bool) -> None:
        with self._conn() as con:
            con.execute("UPDATE users SET is_admin=? WHERE username=?", (int(is_admin), username.strip().lower()))

    def delete_user(self, username: str) -> None:
        with self._conn() as con:
            con.execute("DELETE FROM users WHERE username=?", (username.strip().lower(),))

    def verify_user(self, username: str, password: str) -> dict | None:
        username = username.strip().lower()
        with self._conn() as con:
            row = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            if row is None or not bcrypt.checkpw(password.encode(), row["password_hash"].encode()):
                return None
            con.execute("UPDATE users SET last_login=? WHERE username=?", (_now(), username))
            return {"username": row["username"], "is_admin": bool(row["is_admin"])}

    def list_users(self) -> list[dict]:
        with self._conn() as con:
            rows = con.execute("SELECT username, is_admin, created_at, last_login FROM users ORDER BY username")
            return [dict(r) for r in rows]

    # -------------------------------------------------------------- templates
    def _row_to_template(self, r: sqlite3.Row) -> Template:
        return Template(
            id=r["id"], name=r["name"], fingerprint=r["fingerprint"], headers=json.loads(r["headers_json"]),
            mapping=Mapping.model_validate_json(r["mapping_json"]), read_options=json.loads(r["read_json"]),
            schema_name=r["schema_name"], notes=r["notes"], created_by=r["created_by"], created_at=r["created_at"],
            updated_by=r["updated_by"], updated_at=r["updated_at"], use_count=r["use_count"],
        )

    def save_template(self, name: str, headers: list[str], mapping: Mapping, read_options: dict, schema_name: str,
                      username: str, notes: str = "") -> Template:
        name = name.strip()
        if not name:
            raise ValueError("template name is required")
        fp = header_fingerprint(headers)
        now = _now()
        with self._conn() as con:
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
        with self._conn() as con:
            if schema_name:
                rows = con.execute("SELECT * FROM templates WHERE schema_name=? ORDER BY name", (schema_name,))
            else:
                rows = con.execute("SELECT * FROM templates ORDER BY name")
            return [self._row_to_template(r) for r in rows]

    def get_template(self, name: str) -> Template | None:
        with self._conn() as con:
            r = con.execute("SELECT * FROM templates WHERE name=?", (name,)).fetchone()
            return self._row_to_template(r) if r else None

    def delete_template(self, name: str) -> None:
        with self._conn() as con:
            con.execute("DELETE FROM templates WHERE name=?", (name,))

    def rename_template(self, old: str, new: str) -> None:
        with self._conn() as con:
            con.execute("UPDATE templates SET name=? WHERE name=?", (new.strip(), old))

    def mark_template_used(self, name: str) -> None:
        with self._conn() as con:
            con.execute("UPDATE templates SET use_count = use_count + 1 WHERE name=?", (name,))

    def find_template(self, headers: list[str], schema_name: str, min_similarity: float = 0.75
                      ) -> tuple[Template | None, float]:
        """Exact header-set match first, otherwise the most similar template above min_similarity."""
        fp = header_fingerprint(headers)
        templates = self.list_templates(schema_name)
        exact = [t for t in templates if t.fingerprint == fp]
        if exact:
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
        return self.db_path.parent / "imports"

    def import_dir(self, import_id: int) -> Path:
        d = self.files_root / str(int(import_id))
        d.mkdir(parents=True, exist_ok=True)
        return d

    def create_import(self, *, username: str, filename: str, data: bytes, schema_label: str, schema_json: str,
                      mapping: Mapping, read_options: dict[str, Any], overrides: dict | None = None,
                      template_name: str | None = None, ai_used: bool = False, parent_id: int | None = None) -> int:
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
        (self.import_dir(import_id) / ("source" + Path(filename).suffix.lower())).write_bytes(data)
        return import_id

    def source_path(self, rec: ImportRecord) -> Path:
        return self.files_root / str(rec.id) / ("source" + Path(rec.filename).suffix.lower())

    def file_path(self, rec: ImportRecord, kind: str) -> Path | None:
        name = rec.files.get(kind)
        if not name or rec.files_purged:
            return None
        p = self.files_root / str(rec.id) / name
        return p if p.exists() else None

    def _row_to_import(self, r: sqlite3.Row) -> ImportRecord:
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
        with self._conn() as con:
            r = con.execute("SELECT * FROM imports WHERE id=?", (int(import_id),)).fetchone()
            return self._row_to_import(r) if r else None

    def list_imports(self, limit: int = 500, username: str | None = None, statuses: list[str] | None = None,
                     search: str | None = None) -> list[ImportRecord]:
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
        return self.list_imports(limit=1000, statuses=list(ACTIVE_STATUSES))

    def start_import(self, import_id: int) -> None:
        with self._conn() as con:
            con.execute("UPDATE imports SET status='running', started_at=?, stage='Starting', progress=0 "
                        "WHERE id=?", (_now(), import_id))

    def update_progress(self, import_id: int, progress: float, stage: str) -> None:
        with self._conn() as con:
            con.execute("UPDATE imports SET progress=?, stage=? WHERE id=? AND status='running'",
                        (max(0.0, min(1.0, progress)), stage[:200], import_id))

    def complete_import(self, import_id: int, summary: dict[str, Any], files: dict[str, str]) -> None:
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
        assert status in FINAL_STATUSES
        with self._conn() as con:
            con.execute("UPDATE imports SET status=?, error=?, stage=?, finished_at=? WHERE id=?",
                        (status, error, stage or status.capitalize(), _now(), import_id))

    def request_cancel(self, import_id: int) -> None:
        with self._conn() as con:
            con.execute("UPDATE imports SET cancel_requested=1 WHERE id=? AND status IN ('queued','running')",
                        (import_id,))

    def is_cancel_requested(self, import_id: int) -> bool:
        with self._conn() as con:
            r = con.execute("SELECT cancel_requested FROM imports WHERE id=?", (import_id,)).fetchone()
            return bool(r and r[0])

    def mark_interrupted(self) -> int:
        """Called at startup: imports that were queued/running when the server stopped can't resume."""
        with self._conn() as con:
            cur = con.execute(
                "UPDATE imports SET status='interrupted', finished_at=?, stage='Interrupted', "
                "error='The server restarted while this import was waiting or running. Use Re-run to start it again.' "
                "WHERE status IN ('queued','running')", (_now(),))
            return cur.rowcount

    def purge_files(self, older_than_days: int) -> int:
        """Delete stored files of finished imports older than N days (records and counts are kept)."""
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
