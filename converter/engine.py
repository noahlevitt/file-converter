"""Conversion engine: apply a mapping to every row, normalize, validate, dedupe, and report.

Deterministic: the same file + mapping always produces the same output. Claude is never called here.
"""

from __future__ import annotations

import json
import re
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
CELL_REPAIRED = "rpr"   # repaired from a likely typo (e.g. phone area code one digit off) — worth a look
CELL_BLANKED = "blk"    # invalid optional value, left blank
CELL_REJECT = "rej"     # invalid or missing required value → row rejected
ROW_STATUSES = ("imported", "rejected", "duplicate", "empty")


@dataclass
class RowIssue:
    field: str
    value: str
    reason: str
    rejected: bool  # True = caused the row to be rejected; False = value was blanked
    skipped: bool = False  # an extra value (e.g. a 3rd phone column) that was invalid and didn't get its own row
    repaired: bool = False  # value was repaired from a likely typo; reason says how


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
    # One row per converted row, in file order: _row (file row number), _src (position in the source DataFrame),
    # _extra (True for the 2nd, 3rd… row made from one file row by 'One row per phone number'), _status, _reason,
    # _issues (JSON), and per field: out.<name> (converted value), raw.<name> (value before normalizing),
    # st.<name> (cell status). Without 'One row per phone number' there is exactly one audit row per source row.
    audit: pd.DataFrame = field(default_factory=pd.DataFrame)
    extra_rows: int = 0  # rows added because a file row had several different values (e.g. phone numbers)
    skipped_values: int = 0  # invalid extra values that didn't get a row of their own
    skipped_counts: Counter = field(default_factory=Counter)
    repaired_values: int = 0  # values repaired from likely typos (each marked in the audit)
    repaired_counts: Counter = field(default_factory=Counter)

    @property
    def has_bad_rows(self) -> bool:
        return self.rejected > 0

    def flag_message(self) -> str:
        if self.extra_rows:
            parts = [f"{self.imported:,} rows ready to import from {self.total_rows:,} file rows "
                     f"({self.extra_rows:,} extra row{'s' if self.extra_rows != 1 else ''} for customers with more "
                     "than one phone number)."]
        else:
            parts = [f"{self.imported:,} of {self.total_rows:,} rows converted and ready to import."]
        if self.rejected:
            parts.append(f"{self.rejected:,} bad row{'s were' if self.rejected != 1 else ' was'} NOT imported.")
        if self.duplicates:
            parts.append(f"{self.duplicates:,} duplicate row{'s' if self.duplicates != 1 else ''} removed.")
        if self.blank_rows:
            parts.append(f"{self.blank_rows:,} empty row{'s' if self.blank_rows != 1 else ''} skipped.")
        if self.blanked_values:
            parts.append(f"{self.blanked_values:,} invalid optional value{'s' if self.blanked_values != 1 else ''} "
                         "left blank.")
        if self.skipped_values:
            parts.append(f"{self.skipped_values:,} invalid extra number{'s' if self.skipped_values != 1 else ''} "
                         "in other phone columns skipped.")
        if self.repaired_values:
            parts.append(f"{self.repaired_values:,} phone number{'s were' if self.repaired_values != 1 else ' was'} "
                         "repaired from likely typos — check them under Side by side → Problems only.")
        return " ".join(parts)

    def summary_dict(self) -> dict[str, Any]:
        return {
            "total_rows": self.total_rows,
            "imported": self.imported,
            "rejected": self.rejected,
            "duplicates": self.duplicates,
            "blank_rows": self.blank_rows,
            "blanked_values": self.blanked_values,
            "extra_rows": self.extra_rows,
            "skipped_values": self.skipped_values,
            "reasons": dict(self.reason_counts.most_common()),
            "blanked": dict(self.blanked_counts.most_common()),
            "skipped": dict(self.skipped_counts.most_common()),
            "repaired_values": self.repaired_values,
            "repaired": dict(self.repaired_counts.most_common()),
            "message": self.flag_message(),
        }


def convert(
    df: pd.DataFrame,
    schema: TargetSchema,
    mapping: Mapping,
    row_numbers: list[int] | None = None,
    overrides: dict[str, dict[str, str]] | None = None,
    progress: ProgressFn | None = None,
    timezone: str | None = None,
) -> ConversionResult:
    """Convert a source DataFrame (text columns) into the target schema.

    row_numbers: original file row number for each df row (for the rejects report).
    overrides: optional {field_name: {raw_value: final_output_value}} — user-approved repairs. A raw value
               listed here skips normalization and outputs the approved value (already validated).
    progress: optional callback(fraction, stage) called regularly; raising from it aborts the conversion.
    timezone: IANA zone for this import (e.g. America/New_York). Times in the file that carry a zone, and Unix
              timestamps, are converted to it; times without a zone are kept as written.
    """
    mapping = mapping.normalized(schema, list(df.columns))
    overrides = overrides or {}
    src_records = df.fillna("").astype(str).to_dict("records")
    row_numbers = row_numbers or [i + 2 for i in range(len(src_records))]

    all_cols = list(df.columns)
    src_blank = [not any(N.clean_text(r.get(c, "")) for c in all_cols) for r in src_records]

    # 'One row per phone number': a file row with several different values (Phone, Phone 2, Phone 3…) becomes one
    # row per value; every other field is copied onto each. `src` maps each working row to its file row.
    each_field = next((f for f in schema.mappable_fields if mapping.get(f.name).transform == "each_row"), None)
    src = list(range(len(src_records)))
    each_raw: list[str] = []
    early_issues: dict[int, list[RowIssue]] = {}
    each_ctx: dict = {}
    if each_field is not None:
        src, each_raw, early_issues, each_ctx = _expand_rows(src_records, src_blank, each_field, mapping, overrides,
                                                             timezone)
    records = [src_records[k] for k in src]
    blank = [src_blank[k] for k in src]
    row_numbers = [row_numbers[k] for k in src]
    n = len(records)
    extra = [j > 0 and src[j] == src[j - 1] for j in range(n)]

    out_cols: dict[str, list[str]] = {}
    raw_cols: dict[str, list[str]] = {}
    status_cols: dict[str, list[str]] = {}
    issues: list[list[RowIssue]] = [list(early_issues.get(i, [])) for i in range(n)]
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
        if f is each_field:
            raw = ["" if blank[i] else each_raw[i] for i in range(n)]
        else:
            raw = [
                "" if blank[i] else apply_transform(fm.transform, records[i], fm.sources, params)
                for i in range(n)
            ]
        fixes = overrides.get(f.name, {})
        cleaned = [N.clean_text(v) for v in raw]
        # The 'one row per number' field reuses the context built from ALL its columns (area codes in the file).
        ctx = each_ctx if f is each_field else N.prepare(f, [v for i, v in enumerate(cleaned) if not blank[i]], params)
        if timezone:
            ctx["tz"] = timezone
        passthrough = fm.transform in ("copy", "coalesce", "each_row")

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
            elif v in ctx.get("repairs", {}):
                issues[i].append(RowIssue(f.name, v, f"repaired: {ctx['repairs'][v]}", rejected=False, repaired=True))
                statuses.append(CELL_REPAIRED)
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
    skipped_counts: Counter = Counter()
    repaired_counts: Counter = Counter()
    accepted: list[int] = []
    rejected_rows: list[tuple[int, str]] = []
    blanked_total = 0
    for i in range(n):
        if blank[i]:
            continue
        row_rejects = [x for x in issues[i] if x.rejected]
        for x in issues[i]:
            key = f"{schema.field(x.field).header}: {_reason_key(x.reason)}"
            if x.repaired:
                repaired_counts[key] += 1
            elif x.skipped:
                skipped_counts[key] += 1
            elif not x.rejected:
                blanked_counts[key] += 1
                blanked_total += 1
        if row_rejects:
            for x in row_rejects:
                reason_counts[f"{schema.field(x.field).header}: {_reason_key(x.reason)}"] += 1
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
                if row_numbers[first] == row_numbers[i]:
                    why = f"the same value appears twice in file row {row_numbers[i]}"
                elif set(keys) == {f.name for f in schema.mappable_fields}:
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
    if each_field is not None and not rejects.empty:  # show which value of the file row this was
        rejects.insert(3, each_field.header, [each_raw[i] for i, _, _ in reject_entries])

    row_status = ["empty" if blank[i] else "imported" for i in range(n)]
    row_reason = [""] * n
    for i, r in rejected_rows:
        row_status[i], row_reason[i] = "rejected", r
    for i, r in duplicate_rows:
        row_status[i], row_reason[i] = "duplicate", r
    audit_data: dict[str, list] = {
        "_row": row_numbers,
        "_src": src,
        "_extra": extra,
        "_status": row_status,
        "_reason": row_reason,
        "_issues": [json.dumps({x.field: x.reason for x in issues[i]}) if issues[i] else "" for i in range(n)],
    }
    for f in schema.fields:
        audit_data[f"out.{f.name}"] = out_cols[f.name]
        audit_data[f"raw.{f.name}"] = raw_cols[f.name]
        audit_data[f"st.{f.name}"] = status_cols[f.name]
    audit = pd.DataFrame(audit_data)

    total = len(src_records) - sum(src_blank)
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
        blank_rows=sum(src_blank),
        blanked_values=blanked_total,
        reason_counts=reason_counts,
        blanked_counts=blanked_counts,
        fill_rates=fill_rates,
        failed_values={k: sorted(v) for k, v in failed_values.items()},
        audit=audit,
        extra_rows=sum(extra),
        skipped_values=sum(skipped_counts.values()),
        skipped_counts=skipped_counts,
        repaired_values=sum(repaired_counts.values()),
        repaired_counts=repaired_counts,
    )


def _expand_rows(records: list[dict[str, str]], blank: list[bool], f, mapping: Mapping,
                 overrides: dict[str, dict[str, str]], timezone: str | None
                 ) -> tuple[list[int], list[str], dict[int, list[RowIssue]], dict]:
    """Work out the rows for 'One row per phone number' on field f.

    Returns (source index per working row, the raw value each working row uses, early issues per working row).
    A file row gets one row per different valid value (different after formatting, so 516-555-0100 and
    (516) 555-0100 count once). Invalid extra values are skipped and noted on the row. A file row with no valid
    value keeps one row with its first value (or blank), which is then rejected the normal way.
    """
    fm = mapping.get(f.name)
    fixes = overrides.get(f.name, {})
    per_row = [[] if blank[k] else [v for v in (N.clean_text(r.get(c, "")) for c in fm.sources) if v]
               for k, r in enumerate(records)]
    ctx = N.prepare(f, [v for vals in per_row for v in vals], fm.params)
    if timezone:
        ctx["tz"] = timezone
    cache: dict[str, tuple[str, str | None]] = {}

    def check(v: str) -> tuple[str, str | None]:
        if v not in cache:
            cache[v] = (fixes[v], None) if v in fixes else N.normalize(v, f, fm.params, ctx)
        return cache[v]

    src: list[int] = []
    raw: list[str] = []
    early: dict[int, list[RowIssue]] = {}
    for k, vals in enumerate(per_row):
        good: dict[str, str] = {}  # formatted output → first raw value that produced it
        bad: list[tuple[str, str]] = []
        for v in vals:
            out, err = check(v)
            if err:
                bad.append((v, err))
            elif out.lower() not in good:
                good[out.lower()] = v
        if not good:
            src.append(k)
            raw.append(vals[0] if vals else "")
            continue
        first = len(src)
        for v in good.values():
            src.append(k)
            raw.append(v)
        if bad:
            early[first] = [RowIssue(f.name, v, f"extra value skipped — {err}", rejected=False, skipped=True)
                            for v, err in bad]
    return src, raw, early, ctx


def source_rows(source: pd.DataFrame, audit: pd.DataFrame) -> pd.DataFrame:
    """The source rows lined up with the audit rows (one per converted row).

    Needed when 'One row per phone number' made several rows from one file row. Safe to call more than once.
    """
    if "_src" not in audit.columns or len(source) == len(audit):
        return source
    return source.iloc[audit["_src"].astype(int).to_numpy()].reset_index(drop=True)


def _reason_key(reason: str) -> str:
    """Reason text for the summary counts: without the per-value details (numbers, 'could be …' options)."""
    reason = reason.split(" — could be ")[0]
    return re.sub(r"\d{3,}", "…", reason)


def _fmt_issue(schema: TargetSchema, x: RowIssue) -> str:
    header = schema.field(x.field).header
    return f"{header}: {x.reason}" + (f" ('{x.value[:40]}')" if x.value else "")


def preview_values(df: pd.DataFrame, schema: TargetSchema, mapping: Mapping, limit: int = 3,
                   timezone: str | None = None) -> dict[str, str]:
    """Return a short sample of converted output per field for the mapping review screen."""
    head = df.head(200)
    res = convert(head, schema, mapping, timezone=timezone)
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
