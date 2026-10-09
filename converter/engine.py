"""Conversion engine: apply a mapping to every row, normalize, validate, dedupe, and report.

Deterministic: the same file + mapping always produces the same output. Claude is never called here.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd

from . import normalizers as N
from .mapping import Mapping
from .schema import TargetSchema
from .transforms import apply_transform


ProgressFn = Callable[[float, str], None]  # (fraction 0–1, stage text); may raise to cancel

# Per-cell status codes stored in the audit table (used by the side-by-side comparison).
CELL_SAME = "same"      # copied unchanged
CELL_FORMAT = "fmt"     # reformatted (phone, date, case, ZIP…)
CELL_DERIVED = "drv"    # built from part of / several columns (name part, address part, combine, split, translate)
CELL_DEFAULT = "def"    # filled from a default or fixed value
CELL_FIXED = "fix"      # user-approved AI fix
CELL_BLANKED = "blk"    # invalid optional value, left blank
CELL_REJECT = "rej"     # invalid or missing required value → row rejected
ROW_STATUSES = ("imported", "rejected", "duplicate", "empty")


@dataclass
class RowIssue:
    field: str
    value: str
    reason: str
    rejected: bool  # True = caused the row to be rejected; False = value was blanked


@dataclass
class ConversionResult:
    output: pd.DataFrame  # rows that import, columns = schema headers
    rejects: pd.DataFrame  # rejected + duplicate rows with reasons and original values
    total_rows: int  # data rows in the file, excluding blank rows
    imported: int
    rejected: int  # bad rows (failed validation)
    duplicates: int
    blank_rows: int
    blanked_values: int  # optional values that were invalid and left blank
    reason_counts: Counter = field(default_factory=Counter)
    blanked_counts: Counter = field(default_factory=Counter)
    fill_rates: dict[str, float] = field(default_factory=dict)
    failed_values: dict[str, list[str]] = field(default_factory=dict)  # field -> unique raw values that failed
    # One row per source row (same order as the source DataFrame): _row, _status, _reason, _issues (JSON),
    # and per field: out.<name> (converted value), raw.<name> (value before normalizing), st.<name> (cell status).
    audit: pd.DataFrame = field(default_factory=pd.DataFrame)

    @property
    def has_bad_rows(self) -> bool:
        return self.rejected > 0

    def flag_message(self) -> str:
        parts = [f"{self.imported:,} of {self.total_rows:,} rows converted and ready to import."]
        if self.rejected:
            parts.append(f"{self.rejected:,} bad row{'s' if self.rejected != 1 else ''} were NOT imported.")
        if self.duplicates:
            parts.append(f"{self.duplicates:,} duplicate row{'s' if self.duplicates != 1 else ''} removed.")
        if self.blank_rows:
            parts.append(f"{self.blank_rows:,} empty row{'s' if self.blank_rows != 1 else ''} skipped.")
        if self.blanked_values:
            parts.append(f"{self.blanked_values:,} invalid optional value{'s' if self.blanked_values != 1 else ''} "
                         "left blank.")
        return " ".join(parts)

    def summary_dict(self) -> dict[str, Any]:
        return {
            "total_rows": self.total_rows,
            "imported": self.imported,
            "rejected": self.rejected,
            "duplicates": self.duplicates,
            "blank_rows": self.blank_rows,
            "blanked_values": self.blanked_values,
            "reasons": dict(self.reason_counts.most_common()),
            "blanked": dict(self.blanked_counts.most_common()),
            "message": self.flag_message(),
        }


def convert(
    df: pd.DataFrame,
    schema: TargetSchema,
    mapping: Mapping,
    row_numbers: list[int] | None = None,
    overrides: dict[str, dict[str, str]] | None = None,
    progress: ProgressFn | None = None,
) -> ConversionResult:
    """Convert a source DataFrame (text columns) into the target schema.

    row_numbers: original file row number for each df row (for the rejects report).
    overrides: optional {field_name: {raw_value: final_output_value}} — user-approved repairs. A raw value
               listed here skips normalization and outputs the approved value (already validated).
    progress: optional callback(fraction, stage) called regularly; raising from it aborts the conversion.
    """
    mapping = mapping.normalized(schema, list(df.columns))
    overrides = overrides or {}
    records = df.fillna("").astype(str).to_dict("records")
    n = len(records)
    row_numbers = row_numbers or [i + 2 for i in range(n)]

    all_cols = list(df.columns)
    blank = [not any(N.clean_text(r.get(c, "")) for c in all_cols) for r in records]

    out_cols: dict[str, list[str]] = {}
    raw_cols: dict[str, list[str]] = {}
    status_cols: dict[str, list[str]] = {}
    issues: list[list[RowIssue]] = [[] for _ in range(n)]
    failed_values: dict[str, set[str]] = {}
    chunk = 2000
    # Relative cost per field, so progress and time-left estimates move evenly.
    cost = {"phone": 4.0, "email": 3.0, "date": 2.0, "datetime": 2.0}
    weights = [0.1 if f.fixed_blank or mapping.get(f.name).transform == "none" else cost.get(f.type, 1.0)
               for f in schema.fields]
    total_w = sum(weights) or 1.0
    done_w = 0.0

    for fi, f in enumerate(schema.fields):
        if f.fixed_blank:  # e.g. consent: always blank, whatever the file or mapping says
            out_cols[f.name] = [""] * n
            raw_cols[f.name] = [""] * n
            status_cols[f.name] = [""] * n
            done_w += weights[fi]
            continue
        fm = mapping.get(f.name)
        params = fm.params
        if progress:
            progress(done_w / total_w, f"Converting {f.header}")
        raw = [
            "" if blank[i] else apply_transform(fm.transform, records[i], fm.sources, params)
            for i in range(n)
        ]
        fixes = overrides.get(f.name, {})
        cleaned = [N.clean_text(v) for v in raw]
        ctx = N.prepare(f, [v for i, v in enumerate(cleaned) if not blank[i]], params)
        passthrough = fm.transform in ("copy", "coalesce")

        cache: dict[str, tuple[str, str | None]] = {}
        values: list[str] = []
        statuses: list[str] = []
        for i, v in enumerate(cleaned):
            if progress and i and i % chunk == 0:
                progress((done_w + weights[fi] * i / n) / total_w, f"Converting {f.header} — row {i:,} of {n:,}")
            if blank[i]:
                values.append("")
                statuses.append("")
                continue
            if not v:
                if f.default not in (None, ""):
                    values.append(str(f.default))
                    statuses.append(CELL_DEFAULT)
                    continue
                if f.required:
                    issues[i].append(RowIssue(f.name, "", "required value is missing", True))
                    statuses.append(CELL_REJECT)
                else:
                    statuses.append("")
                values.append("")
                continue
            if v not in cache:
                cache[v] = (fixes[v], None) if v in fixes else N.normalize(v, f, params, ctx)
            out, err = cache[v]
            if err:
                rejected = f.on_invalid == "reject"
                issues[i].append(RowIssue(f.name, v, err, rejected))
                failed_values.setdefault(f.name, set()).add(v)
                out = ""
                statuses.append(CELL_REJECT if rejected else CELL_BLANKED)
            elif v in fixes:
                statuses.append(CELL_FIXED)
            elif fm.transform == "constant":
                statuses.append(CELL_DEFAULT)
            elif not passthrough:
                statuses.append(CELL_DERIVED)
            else:
                statuses.append(CELL_SAME if out == v else CELL_FORMAT)
            values.append(out)
        out_cols[f.name] = values
        raw_cols[f.name] = cleaned
        status_cols[f.name] = statuses
        done_w += weights[fi]

    if progress:
        progress(1.0, "Checking for duplicates")

    # Classify rows.
    reason_counts: Counter = Counter()
    blanked_counts: Counter = Counter()
    accepted: list[int] = []
    rejected_rows: list[tuple[int, str]] = []
    blanked_total = 0
    for i in range(n):
        if blank[i]:
            continue
        row_rejects = [x for x in issues[i] if x.rejected]
        for x in issues[i]:
            if not x.rejected:
                blanked_counts[f"{schema.field(x.field).header}: {x.reason}"] += 1
                blanked_total += 1
        if row_rejects:
            for x in row_rejects:
                reason_counts[f"{schema.field(x.field).header}: {x.reason}"] += 1
            rejected_rows.append((i, "; ".join(_fmt_issue(schema, x) for x in row_rejects)))
        else:
            accepted.append(i)

    # Dedupe accepted rows on the configured keys.
    duplicate_rows: list[tuple[int, str]] = []
    keys = schema.dedupe.keys
    if keys:
        seen: dict[tuple, int] = {}
        order = accepted if schema.dedupe.keep == "first" else list(reversed(accepted))
        kept: list[int] = []
        for i in order:
            key = tuple(out_cols[k][i].lower() for k in keys)
            if all(not k for k in key):
                kept.append(i)
                continue
            if key in seen:
                first = seen[key]
                if set(keys) == {f.name for f in schema.mappable_fields}:
                    why = f"exact duplicate of file row {row_numbers[first]} (every column matches)"
                else:
                    why = (f"duplicate of file row {row_numbers[first]} "
                           f"(same {', '.join(schema.field(k).header for k in keys)})")
                duplicate_rows.append((i, why))
            else:
                seen[key] = i
                kept.append(i)
        accepted = sorted(kept)

    output = pd.DataFrame(
        {f.header: [out_cols[f.name][i] for i in accepted] for f in schema.fields},
        columns=[f.header for f in schema.fields],
    )

    reject_entries = sorted(
        [(i, "Rejected", r) for i, r in rejected_rows] + [(i, "Duplicate", r) for i, r in duplicate_rows]
    )
    rejects = pd.DataFrame(
        [
            {"File Row": row_numbers[i], "Status": status, "Reason": reason, **records[i]}
            for i, status, reason in reject_entries
        ],
        columns=["File Row", "Status", "Reason", *df.columns],
    )

    row_status = ["empty" if blank[i] else "imported" for i in range(n)]
    row_reason = [""] * n
    for i, r in rejected_rows:
        row_status[i], row_reason[i] = "rejected", r
    for i, r in duplicate_rows:
        row_status[i], row_reason[i] = "duplicate", r
    audit_data: dict[str, list] = {
        "_row": row_numbers,
        "_status": row_status,
        "_reason": row_reason,
        "_issues": [json.dumps({x.field: x.reason for x in issues[i]}) if issues[i] else "" for i in range(n)],
    }
    for f in schema.fields:
        audit_data[f"out.{f.name}"] = out_cols[f.name]
        audit_data[f"raw.{f.name}"] = raw_cols[f.name]
        audit_data[f"st.{f.name}"] = status_cols[f.name]
    audit = pd.DataFrame(audit_data)

    total = n - sum(blank)
    fill_rates = {
        f.header: (sum(1 for i in accepted if out_cols[f.name][i]) / len(accepted)) if accepted else 0.0
        for f in schema.fields
    }
    return ConversionResult(
        output=output,
        rejects=rejects,
        total_rows=total,
        imported=len(accepted),
        rejected=len(rejected_rows),
        duplicates=len(duplicate_rows),
        blank_rows=sum(blank),
        blanked_values=blanked_total,
        reason_counts=reason_counts,
        blanked_counts=blanked_counts,
        fill_rates=fill_rates,
        failed_values={k: sorted(v) for k, v in failed_values.items()},
        audit=audit,
    )


def _fmt_issue(schema: TargetSchema, x: RowIssue) -> str:
    header = schema.field(x.field).header
    return f"{header}: {x.reason}" + (f" ('{x.value[:40]}')" if x.value else "")


def preview_values(df: pd.DataFrame, schema: TargetSchema, mapping: Mapping, limit: int = 3) -> dict[str, str]:
    """Return a short sample of converted output per field for the mapping review screen."""
    head = df.head(200)
    res = convert(head, schema, mapping)
    out = {}
    for f in schema.fields:
        fm = mapping.get(f.name)
        if f.fixed_blank:
            out[f.name] = "(always blank)"
            continue
        if fm.transform == "none" and not f.default:
            out[f.name] = ""
            continue
        vals = [v for v in res.output[f.header].tolist() if v][:limit] if not res.output.empty else []
        bad = res.failed_values.get(f.name, [])
        text = " | ".join(vals)
        if bad:
            text += f"   ⚠ {len(bad)} unconvertible in first {len(head)} rows (e.g. '{bad[0][:30]}')"
        out[f.name] = text
    return out
