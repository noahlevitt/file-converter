"""Mistake checks for a mapping or a finished import.

Rule checks run without Claude and are always available. When Claude is configured, `build_review_payload`
prepares a compact sample (original vs converted rows, per-field stats, rule findings) for Claude's review.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta

import pandas as pd

from .engine import CELL_BLANKED, CELL_FIXED, CELL_FORMAT, CELL_REJECT
from . import normalizers as N
from .heuristics import TYPED, header_score, is_consent_column, is_phone_header
from .mapping import ColumnProfile, Mapping
from .schema import TargetSchema

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}
_ZONE_HINT_RE = re.compile(r"(?:\dz\b|[+-]\d{2}:?\d{2}\b|\b(?:utc|gmt|est|edt|cst|cdt|mst|mdt|pst|pdt|eastern|"
                           r"central|mountain|pacific)\b)", re.I)


@dataclass
class Finding:
    severity: str           # error | warning | info
    field: str              # target field name or ""
    column: str             # source column or ""
    issue: str
    suggestion: str = ""
    source: str = "rules"   # rules | claude

    def as_row(self, schema: TargetSchema) -> dict:
        icon = {"error": "🔴", "warning": "🟠", "info": "🔵"}.get(self.severity, "")
        header = schema.field(self.field).header if self.field in schema.field_names else ""
        return {"": icon, "Field": header, "Column": self.column, "Issue": self.issue,
                "Suggestion": self.suggestion, "Found by": "Claude" if self.source == "claude" else "Rules"}


def _sources(mapping: Mapping, name: str) -> str:
    return ", ".join(mapping.get(name).sources)


def _out_dates(values: pd.Series, fmt: str) -> list[datetime]:
    out = []
    for v in values:
        if not v:
            continue
        try:
            out.append(datetime.strptime(v, fmt))
        except ValueError:
            continue
    return out


_LOOKS_LIKE = {"phone": N.looks_like_phone, "email": N.looks_like_email, "date": N.looks_like_date,
               "zip": N.looks_like_zip, "state": N.looks_like_state}
_KIND_WORDS = {"phone": "phone numbers", "email": "email addresses", "date": "dates", "zip": "ZIP codes",
               "state": "states"}


def column_warnings(schema: TargetSchema, mapping: Mapping,
                    profiles: list[ColumnProfile] | None) -> dict[str, tuple[str, str]]:
    """Columns whose content doesn't fit the field they feed — {column: (field name, message)}.

    Catches a column like "Occupation" going to the phone field because someone typed a phone number into it:
    the column name doesn't say "phone" and most of its values aren't phone numbers.
    """
    by_name = {p.name: p for p in profiles or []}
    out: dict[str, tuple[str, str]] = {}
    for f in schema.mappable_fields:
        kind = TYPED.get(f.type)
        fm = mapping.get(f.name)
        if not kind or fm.transform not in ("copy", "coalesce"):
            continue
        for col in fm.sources:
            p = by_name.get(col)
            if p is None or p.non_empty == 0 or col in out:
                continue
            share = p.shares.get(kind, 0.0)
            named_for_it = header_score(col, f) >= 70 or (kind == "phone" and is_phone_header(col))
            limit = 0.6 if kind in ("phone", "email") else 0.3
            if named_for_it or share >= limit:
                continue
            looks = _LOOKS_LIKE[kind]
            n_unique = min(p.unique, 300)
            n_like = round(share * n_unique)
            example = next((v for v in p.samples if not looks(v)), "")
            msg = (f"'{col}' goes to {f.header}, but its name doesn't say so and only {n_like} of its {n_unique} "
                   f"distinct values {'looks' if n_like == 1 else 'look'} like {_KIND_WORDS[kind]}"
                   + (f" (e.g. '{example}')" if example else "") + ". "
                   + ("Someone probably typed a value into the wrong column." if n_like else ""))
            out[col] = (f.name, msg.strip())
    return out


def run_checks(schema: TargetSchema, mapping: Mapping, source: pd.DataFrame, audit: pd.DataFrame,
               profiles: list[ColumnProfile] | None = None, timezone_label: str = "",
               today: date | None = None) -> list[Finding]:
    """Rule-based checks. `source`/`audit` may be a sample (pre-import preview) or the whole import."""
    today = today or date.today()
    findings: list[Finding] = []
    live = audit[audit["_status"] != "empty"]
    n_live = len(live)
    used = mapping.used_sources()

    for f in schema.mappable_fields:
        fm = mapping.get(f.name)
        st = live.get(f"st.{f.name}")
        out = live.get(f"out.{f.name}")
        raw = live.get(f"raw.{f.name}")
        if fm.transform == "none" or st is None:
            continue
        src = _sources(mapping, f.name)
        attempted = int((raw != "").sum()) if raw is not None else 0
        failed = int(((raw != "") & st.isin([CELL_BLANKED, CELL_REJECT])).sum()) if raw is not None else 0
        if attempted >= 5 and failed / attempted >= 0.3:
            example = raw[st.isin([CELL_BLANKED, CELL_REJECT]) & (raw != "")].iloc[0] if failed else ""
            severe = failed / attempted >= 0.8
            findings.append(Finding(
                "error" if severe else "warning", f.name, src,
                f"{failed / attempted:.0%} of values from '{src}' can't be read as {f.type} (e.g. '{example}').",
                "This is probably the wrong column." if severe else "Check the column or its format option."))
        if attempted and out is not None and (out != "").sum() == 0:
            findings.append(Finding("warning", f.name, src, f"'{src}' is mapped but no value came through.",
                                    "Check that this is the right column."))

        if f.type in ("date", "datetime") and out is not None and f.format:
            dts = _out_dates(out, f.format)
            if not dts:
                continue
            real = [d for d in dts if f.no_year_value is None or d.year != f.no_year_value]
            if f.type == "datetime":
                future = [d for d in real if d.date() > today + timedelta(days=1)]
                if future:
                    findings.append(Finding(
                        "warning", f.name, src,
                        f"{len(future):,} value(s) are in the future (latest {max(future):%m/%d/%Y}). "
                        "A 'last activity' time can't be in the future.",
                        "Check day/month order (Option: DMY) or whether this is the right column."))
                old = [d for d in real if d.year < 2000]
                if old and len(old) >= 0.2 * len(real):
                    findings.append(Finding("info", f.name, src, f"{len(old):,} value(s) are before the year 2000.",
                                            "Check two-digit years and that this is the right column."))
            elif f.no_year_value is not None:  # birthday / anniversary
                future = [d for d in real if d.date() > today]
                if future:
                    findings.append(Finding("warning", f.name, src,
                                            f"{len(future):,} value(s) are in the future (e.g. {future[0]:%m/%d/%Y}).",
                                            "Check the column and the day/month order."))
                recent = [d for d in real if d.date() >= today - timedelta(days=730)]
                if len(real) >= 5 and len(recent) >= 0.5 * len(real):
                    findings.append(Finding(
                        "warning", f.name, src,
                        f"{len(recent) / len(real):.0%} of these dates are within the last two years — that looks "
                        "like sign-up or visit dates, not birthdays/anniversaries.",
                        "Confirm the column."))
            if len(dts) >= 10 and len(set(dts)) == 1:
                findings.append(Finding("info", f.name, src, f"Every row has the same value ({dts[0]:%m/%d/%Y}).",
                                        "Might be an export default rather than real data."))
            # mixed day/month evidence in the raw values
            if raw is not None:
                mdy = dmy = 0
                for v in raw[raw != ""].head(2000):
                    m = re.match(r"^\s*(\d{1,2})[/\-. ](\d{1,2})[/\-. ]\d{2,4}", v)
                    if m:
                        a, b = int(m.group(1)), int(m.group(2))
                        dmy += a > 12 >= b
                        mdy += b > 12 >= a
                if mdy and dmy:
                    findings.append(Finding(
                        "warning", f.name, src,
                        f"'{src}' mixes month-first ({mdy}) and day-first ({dmy}) dates, so dates like 03/04 are "
                        "ambiguous.", "Set the Option to MDY or DMY if you know the file's convention."))
                zoned = int(raw.str.contains(_ZONE_HINT_RE, regex=True).sum())
                if zoned and f.type == "datetime":
                    findings.append(Finding("info", f.name, src,
                                            f"{zoned:,} time(s) included a time zone and were converted to "
                                            f"{timezone_label or 'the client time zone'}."))

    # Columns whose content doesn't match the field they feed.
    for col, (fname, msg) in column_warnings(schema, mapping, profiles).items():
        findings.append(Finding("warning", fname, col, msg,
                                "If it isn't that kind of column, clear its 'Goes to' or tick 'Skip' under "
                                "Columns in your file."))

    # Same column feeding two different fields as a plain copy.
    copies = Counter(s for f in schema.mappable_fields for s in mapping.get(f.name).sources
                     if mapping.get(f.name).transform in ("copy", "coalesce"))
    for col, n in copies.items():
        if n > 1:
            fields = [f.header for f in schema.mappable_fields if col in mapping.get(f.name).sources]
            findings.append(Finding("warning", "", col, f"'{col}' is used for {n} fields: {', '.join(fields)}.",
                                    "Usually each column belongs to one field."))

    # Rejected-row patterns.
    rejected = audit[audit["_status"] == "rejected"]
    if n_live and len(rejected) / n_live >= 0.1 and len(rejected) >= 3:
        reasons = Counter(r.split(" ('")[0] for rs in rejected["_reason"] for r in rs.split("; "))
        top, cnt = reasons.most_common(1)[0]
        findings.append(Finding("warning", "", "", f"{len(rejected) / n_live:.0%} of rows are bad rows; most common: "
                                f"{top} ({cnt:,}).", ""))
        if any("missing area code" in r for r in reasons):
            findings.append(Finding("warning", "phone_number" if "phone_number" in schema.field_names else "", "",
                                    "Some phone numbers have 7 digits (no area code).",
                                    "If the area code is in its own column, use 'Combine columns' for the phone."))
    dup = (audit["_status"] == "duplicate").sum()
    if n_live >= 10 and dup / n_live >= 0.3:
        findings.append(Finding("info", "", "", f"{dup / n_live:.0%} of rows are exact duplicates.",
                                "The file may contain the same export twice."))

    # Columns that look useful but aren't imported.
    unmapped_dates = [f.header for f in schema.mappable_fields
                      if f.type in ("date", "datetime") and mapping.get(f.name).transform == "none"]
    for p in profiles or []:
        if p.name in used or p.non_empty == 0:
            continue
        if is_consent_column(p.name):
            findings.append(Finding("info", "", p.name, f"'{p.name}' is not imported.",
                                    "Consent is always left blank in the import file by design."))
            continue
        if p.shares.get("date", 0) >= 0.6 and unmapped_dates:
            findings.append(Finding("info", "", p.name, f"'{p.name}' contains dates but isn't imported.",
                                    "Does it belong in one of: " + ", ".join(unmapped_dates) + "?"))
        elif p.shares.get("phone", 0) >= 0.6:
            findings.append(Finding("info", "", p.name, f"'{p.name}' contains phone numbers but isn't used.",
                                    "Use 'First non-empty' on the phone field to fall back to it."))

    findings.sort(key=lambda x: (SEVERITY_ORDER.get(x.severity, 9), x.field, x.column))
    return findings


# ---------------------------------------------------------------------------
# Sample for Claude's review
# ---------------------------------------------------------------------------

def mask_phone(value: str) -> str:
    """Keep the shape and area code, hide the rest: (516) 555-0100 → (516) ###-##00."""
    digits = [i for i, ch in enumerate(value) if ch.isdigit()]
    if len(digits) < 7:
        return value
    keep = set(digits[:3] + digits[-2:]) if len(digits) <= 11 else set(digits[:3])
    if len(digits) == 11 and value.strip().lstrip("+").startswith("1"):
        keep = set(digits[:4] + digits[-2:])
    return "".join(ch if (not ch.isdigit() or i in keep) else "#" for i, ch in enumerate(value))


def pick_sample_rows(audit: pd.DataFrame, limit: int = 30) -> list[int]:
    """Rows worth showing a reviewer: up to a third problems, a third reformatted, the rest ordinary rows."""
    live = audit[audit["_status"] != "empty"]
    st_cols = [c for c in audit.columns if c.startswith("st.")]
    problem = live[(live["_status"] == "rejected") | live[st_cols].isin([CELL_BLANKED]).any(axis=1)].index
    changed = live[live[st_cols].isin([CELL_FORMAT, CELL_FIXED]).any(axis=1)].index
    picked: list[int] = []
    for group, quota in ((problem, limit // 3), (changed, limit // 3), (live.index, limit)):
        added = 0
        for idx in group:
            if len(picked) >= limit or added >= quota:
                break
            if idx not in picked:
                picked.append(idx)
                added += 1
    return sorted(picked)


def build_review_payload(schema: TargetSchema, mapping: Mapping, source: pd.DataFrame, audit: pd.DataFrame,
                         findings: list[Finding], mask_phones: bool = True, limit: int = 30,
                         stats_scope: str = "") -> dict:
    phone_fields = {f.name for f in schema.fields if f.type == "phone"}
    phone_cols = {s for n in phone_fields for s in mapping.get(n).sources}
    mapped_cols = [c for c in source.columns if c in mapping.used_sources()]
    other_cols = [c for c in source.columns if c not in mapping.used_sources()][:6]

    def m(col_or_field: str, value: str, is_phone: bool) -> str:
        return mask_phone(value) if (mask_phones and is_phone and value) else value

    rows = []
    for idx in pick_sample_rows(audit, limit):
        a = audit.loc[idx]
        issues = json.loads(a["_issues"]) if a["_issues"] else {}
        rows.append({
            "file_row": int(a["_row"]),
            "status": a["_status"],
            "original": {c: m(c, str(source.at[idx, c]), c in phone_cols) for c in mapped_cols + other_cols
                         if str(source.at[idx, c])},
            "converted": {f.header: m(f.name, a.get(f"out.{f.name}", ""), f.name in phone_fields)
                          for f in schema.mappable_fields if a.get(f"out.{f.name}", "")},
            "problems": {schema.field(k).header: v for k, v in issues.items() if k in schema.field_names},
        })
    live = audit[audit["_status"] != "empty"]
    stats = {}
    for f in schema.mappable_fields:
        st = live.get(f"st.{f.name}")
        if st is None or mapping.get(f.name).transform == "none":
            continue
        vc = st.value_counts()
        stats[f.header] = {k: int(v) for k, v in vc.items() if k}
        stats[f.header]["empty"] = int((st == "").sum())
    return {
        "scope": stats_scope,
        "rows_checked": int(len(live)),
        "row_status": {k: int(v) for k, v in audit["_status"].value_counts().items()},
        "mapping": {f.header: mapping.get(f.name).summary() for f in schema.mappable_fields},
        "unused_columns": [c for c in source.columns if c not in mapping.used_sources()],
        "cell_status_counts_per_field": stats,
        "rule_findings": [asdict(x) for x in findings],
        "sample_rows": rows,
        "phones_masked": mask_phones,
    }
