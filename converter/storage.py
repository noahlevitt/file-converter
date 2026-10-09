"""SQLite storage for users, saved mapping templates and the job history.

No customer data is stored: templates hold column names and mapping rules; jobs hold counts only.
SQLite in WAL mode comfortably handles an internal tool with 5–20 concurrent users.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
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
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL,
    filename      TEXT NOT NULL,
    template_name TEXT,
    schema_label  TEXT NOT NULL,
    total_rows    INTEGER NOT NULL,
    imported      INTEGER NOT NULL,
    rejected      INTEGER NOT NULL,
    duplicates    INTEGER NOT NULL,
    blank_rows    INTEGER NOT NULL,
    blanked       INTEGER NOT NULL,
    ai_used       INTEGER NOT NULL DEFAULT 0,
    summary_json  TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


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

    # ------------------------------------------------------------------- jobs
    def log_job(self, username: str, filename: str, template_name: str | None, schema_label: str,
                summary: dict[str, Any], ai_used: bool) -> int:
        with self._conn() as con:
            cur = con.execute(
                "INSERT INTO jobs(username, filename, template_name, schema_label, total_rows, imported, rejected, "
                "duplicates, blank_rows, blanked, ai_used, summary_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (username, filename, template_name, schema_label, summary["total_rows"], summary["imported"],
                 summary["rejected"], summary["duplicates"], summary["blank_rows"], summary["blanked_values"],
                 int(ai_used), json.dumps(summary), _now()),
            )
            return cur.lastrowid

    def list_jobs(self, limit: int = 200) -> list[dict]:
        with self._conn() as con:
            rows = con.execute(
                "SELECT id, created_at, username, filename, template_name, total_rows, imported, rejected, duplicates, "
                "blank_rows, blanked, ai_used FROM jobs ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return [dict(r) for r in rows]
