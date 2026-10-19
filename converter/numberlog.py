"""Number history (0.21.0): a lasting record of the numbers each import converted, and where their consent came from.

Why: an import's files (the client's upload, the converted file, the side-by-side data) are deleted after
FILE_RETENTION_DAYS, but "where did this customer's opt-in come from?" can be asked months later (a complaint, a
carrier audit). So when an import completes, :func:`record` keeps one small row per converted number in the
database (table ``number_log``, see storage.py): the number, the import, the row in the client's file, the phone
cell as written, and the consent value written to the import next to the cell it came from. The file name, who
imported it and when, and which column the consent came from are read from the import's record (never deleted).
The Number history page looks a number up with :func:`lookup`.

Kept for NUMBER_RECORD_DAYS (default 0 = forever; ``Store.purge_number_log``). Imports completed before 0.21.0
whose files still exist are recorded once by :func:`backfill` when the server starts. Only completed imports are
listed: one that ends cancelled or failed after its numbers were recorded has them removed again
(``jobs._forget_numbers``, 0.21.1).

Example: import #12 of "guests.xlsx" mapped consent from "SMS Opt In" (Y=true; N=false); row 17 had
"(516) 555-0177" and "Y". ``lookup(store, "516-555-0177", schema)`` then gives one entry: number 5165550177,
import #12, guests.xlsx, row 17, phone in file "(516) 555-0177", consent "true", consent in file "Y", consent from
"Translate values: SMS Opt In (Y=true; N=false)".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pandas as pd

from . import normalizers as N
from .mapping import OPT_OUT_CHOICES, Mapping, opt_out_setting
from .schema import FieldSpec, TargetSchema
from .storage import ImportRecord, Store

log = logging.getLogger(__name__)


def phone_field(schema: TargetSchema) -> FieldSpec | None:
    """The field holding the number (the first phone field), or None when the format has none."""
    return next((f for f in schema.fields if f.type == "phone"), None)


def consent_field(schema: TargetSchema) -> FieldSpec | None:
    """The consent field: the one named "consent", otherwise the first whose name or header says consent."""
    return (next((f for f in schema.fields if f.name == "consent"), None)
            or next((f for f in schema.fields if "consent" in f"{f.name} {f.header}".lower()), None))


def consent_source(schema: TargetSchema, mapping: Mapping) -> str:
    """Where an import's consent values came from, in words, or "" when consent wasn't mapped (always blank).

    Examples: "SMS Opt In" (copied), "Translate values: SMS Opt In (Y=true; N=false)", "fixed value 'true'",
    "Email Opt In — opt-ins only" (false values were left blank, 0.22.0).
    """
    f = consent_field(schema)
    if f is None or f.fixed_blank:
        return ""
    fm = mapping.get(f.name)
    if fm.transform == "none" or (f.manual_only and fm.origin != "user"):
        return ""  # not mapped, or mapped by something other than a person, which a manual-only field ignores
    choice = opt_out_setting(fm.params)
    text = fm.summary().removesuffix(f" — {OPT_OUT_CHOICES[choice]}") if choice else fm.summary()
    if fm.transform == "map_values" and fm.params.get("value_map"):
        text += " (" + "; ".join(f"{m.get('from', '')}={m.get('to', '')}" for m in fm.params["value_map"]) + ")"
    return f"{text} — {OPT_OUT_CHOICES[choice]}" if choice else text


def _consent_cells(audit: pd.DataFrame, schema: TargetSchema, source: pd.DataFrame | None,
                   mapping: Mapping | None) -> list[str] | None:
    """The consent cell(s) of each audit row as written in the client's file ("Y", "Opted in"), or None when they
    can't be read (no file or mapping given). Several consent columns are joined with " / "; "" when consent wasn't
    taken from the file (not mapped, a fixed value, or mapped by something other than a person)."""
    f = consent_field(schema)
    if source is None or mapping is None or "_src" not in audit.columns:
        return None
    fm = mapping.get(f.name) if f else None
    if not consent_source(schema, mapping) or fm is None or fm.transform == "constant":
        return [""] * len(audit)
    cols = [c for c in fm.sources if c in source.columns]
    if not cols:
        return [""] * len(audit)
    cells = [" / ".join(v for v in map(N.clean_text, row) if v) for row in source[cols].itertuples(index=False)]
    return [cells[int(k)] if 0 <= int(k) < len(cells) else "" for k in audit["_src"].tolist()]


def numbers_from_audit(audit: pd.DataFrame, schema: TargetSchema, source: pd.DataFrame | None = None,
                       mapping: Mapping | None = None) -> list[tuple[str, int | None, str, str, str]]:
    """The rows to record for one import: (phone, file_row, phone_value, consent, consent_value) for every row
    that was imported (bad rows, duplicates and empty rows weren't).

    From the import's audit table (engine.ConversionResult.audit), the file as read (``source``, one row per file
    row) and the mapping it ran with. phone_value is the number as the file wrote it (one number's part of a cell
    holding several; with 'Phone + area code column', the area code in front). consent_value is the consent cell
    from the file; without ``source`` and ``mapping`` it is the value before normalizing (after a translation such
    as Y=true, so "true").
    """
    pf, cf = phone_field(schema), consent_field(schema)
    if pf is None or audit.empty or f"out.{pf.name}" not in audit.columns:
        return []
    done = audit[audit["_status"] == "imported"]

    def col(name: str) -> list[str]:
        return done[name].fillna("").astype(str).tolist() if name in done.columns else [""] * len(done)

    rows = [int(r) if str(r).lstrip("-").isdigit() else None for r in done["_row"].tolist()]
    consent = col(f"out.{cf.name}") if cf else [""] * len(done)
    cells = _consent_cells(done, schema, source, mapping) if cf else None
    consent_raw = cells if cells is not None else (col(f"raw.{cf.name}") if cf else [""] * len(done))
    return [t for t in zip(col(f"out.{pf.name}"), rows, col(f"raw.{pf.name}"), consent, consent_raw) if t[0]]


def record(store: Store, import_id: int, audit: pd.DataFrame, schema: TargetSchema,
           source: pd.DataFrame | None = None, mapping: Mapping | None = None) -> int:
    """Keep the numbers an import converted (called by jobs.run_import just before the import completes, with the
    file as read and the import's mapping). Returns how many were recorded; recording an import again replaces
    what it had."""
    return store.log_numbers(import_id, numbers_from_audit(audit, schema, source, mapping))


def backfill(store: Store, record_days: int = 0) -> int:
    """Record the numbers of completed imports that have none recorded (from before 0.21.0), from their stored
    audit and source files; imports whose files were deleted can't be. With NUMBER_RECORD_DAYS (``record_days``)
    set, imports older than that are left out. Returns how many imports were recorded; never raises (each failure
    is logged).
    """
    since = None
    if record_days > 0:
        since = (datetime.now(timezone.utc) - timedelta(days=record_days)).isoformat(timespec="seconds")
    done = 0
    for import_id in store.imports_to_log(since):
        try:
            rec = store.get_import(import_id)
            path = store.file_path(rec, "audit") if rec else None
            if path is None:
                continue
            schema = TargetSchema.model_validate_json(rec.schema_json)
            src = store.file_path(rec, "source_table")
            record(store, import_id, pd.read_parquet(path), schema, pd.read_parquet(src) if src else None,
                   rec.mapping)
            done += 1
        except Exception:  # noqa: BLE001 - one unreadable old import must not stop the others
            log.exception("Recording the numbers of import %s failed", import_id)
    return done


@dataclass
class Lookup:
    """Result of :func:`lookup`: the number as the import format writes it, why the text isn't a number (or ""),
    and one entry per time the number was imported (newest first)."""

    number: str = ""
    error: str = ""
    entries: list[dict] = field(default_factory=list)


def lookup(store: Store, text: str, schema: TargetSchema) -> Lookup:
    """Look a number up in the record. ``text`` is read like a phone cell ("(516) 555-0177", "+1 516.555.0177").

    Each entry: import_id, imported_at (UTC ISO time), filename, sheet, username, file_row, phone_value, consent,
    consent_value, consent_from (see :func:`consent_source`; "" = consent wasn't mapped).
    """
    pf = phone_field(schema)
    value = N.clean_text(text)
    if pf is None or not value:
        return Lookup(error="type a phone number" if pf else "the import format has no phone field")
    if len(N.split_phones(value, pf, {})) > 1:
        return Lookup(error="that is more than one number; look them up one at a time")
    number, err = N.normalize(value, pf, {}, N.prepare(pf, [value], {}))
    if err or not number:
        return Lookup(error=err or "not a phone number")
    imports: dict[int, ImportRecord | None] = {}
    entries = []
    for r in store.number_history(number):
        iid = r["import_id"]
        if iid not in imports:
            imports[iid] = store.get_import(iid)
        rec = imports[iid]
        if rec is None:
            continue
        try:
            source = consent_source(TargetSchema.model_validate_json(rec.schema_json), rec.mapping)
        except Exception:  # noqa: BLE001 - an unreadable snapshot only loses this column
            source = ""
        entries.append({
            "import_id": iid, "imported_at": rec.finished_at or rec.created_at, "filename": rec.filename,
            "sheet": str(rec.read_options.get("sheet") or ""), "username": rec.username, "file_row": r["file_row"],
            "phone_value": r["phone_value"], "consent": r["consent"], "consent_value": r["consent_value"],
            "consent_from": source,
        })
    return Lookup(number=number, entries=entries)
