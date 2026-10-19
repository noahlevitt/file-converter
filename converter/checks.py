"""Mistake checks for a mapping or a finished import.

Rule checks run without Claude and are always available. When Claude is configured, `build_review_payload`
prepares a compact sample (original vs converted rows, per-field stats, rule findings) for Claude's review.

Overview
--------
"Check for mistakes" looks for signs that the mapping is wrong (a column sent to the wrong field, day/month
swapped, a useful column left out, ...) rather than for individual bad values, which the conversion engine
already reports row by row. It works on the same two tables as the side-by-side view (see ``compare.py``):
``source`` (original values, aligned row-for-row with the audit by the caller) and ``audit`` (per-row status and
per-field ``out.`` / ``raw.`` / ``st.`` columns), plus optional ``ColumnProfile`` statistics of the file. They may
be a preview sample (before importing) or a whole finished import.

Main entry points
-----------------
* ``run_checks`` -- all rule-based checks; returns a sorted list of ``Finding`` (errors first).
* ``column_warnings`` -- the "column content doesn't fit its field" check on its own; the mapping screen also
  uses it to flag columns while the user edits the mapping.
* ``build_review_payload`` -- the JSON-ready dict sent to Claude for an optional second opinion (see
  ``ai.ClaudeAssistant.review``), with phone digits masked by default (``mask_phone``).
* ``Finding.as_row`` -- one display row for the findings table.

Data flow
---------
``services.check_for_mistakes`` aligns ``source`` to ``audit`` -> ``run_checks`` -> (if Claude is requested and
configured) ``build_review_payload`` -> Claude -> Claude's findings are appended as ``Finding(source="claude")``
-> the app shows all findings with ``Finding.as_row``. Nothing in this module writes files or the database.

Severity levels: ``error`` = the data will be wrong, ``warning`` = probably wrong, ``info`` = worth a look.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta

import pandas as pd

from .engine import CELL_BLANKED, CELL_FIXED, CELL_FORMAT, CELL_REJECT, CELL_REPAIRED
from . import normalizers as N
from .heuristics import TYPED, header_score, is_consent_column, is_phone_header
from .mapping import ColumnProfile, Mapping
from .schema import TargetSchema

# Sort key for findings: errors first, then warnings, then info.
SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}
# Signs that a date/time value carries its own time zone: a "Z" (UTC) right after a digit, a numeric offset such
# as +05:30 / -0500, or a zone abbreviation / name. Used only to tell the user that such times were converted to
# the client time zone. Note: the offset pattern also matches a trailing "-YYYY" year in dash-separated dates
# (e.g. "01-15-2024"), so the count can include such values.
_ZONE_HINT_RE = re.compile(r"(?:\dz\b|[+-]\d{2}:?\d{2}\b|\b(?:utc|gmt|est|edt|cst|cdt|mst|mdt|pst|pdt|eastern|"
                           r"central|mountain|pacific)\b)", re.I)


@dataclass
class Finding:
    """One possible mistake found by the rule checks or by Claude.

    Attributes:
        severity: "error" (data will be wrong), "warning" (probably wrong) or "info" (worth a look).
        field: target field *name* the finding is about, or "" for file-level findings.
        column: source column(s) involved (comma-separated when several), or "".
        issue: what looks wrong, in plain language.
        suggestion: what the user could do about it (may be "").
        source: who found it -- "rules" (this module) or "claude" (added by ``services.check_for_mistakes``).

    ``dataclasses.asdict`` of a Finding is what ``build_review_payload`` sends to Claude as ``rule_findings``.
    """

    severity: str           # error | warning | info
    field: str              # target field name or ""
    column: str             # source column or ""
    issue: str
    suggestion: str = ""
    source: str = "rules"   # rules | claude

    def as_row(self, schema: TargetSchema) -> dict:
        """Return the finding as one row of the findings table shown in the app.

        The severity becomes a coloured circle icon (red / orange / blue), the field name is translated to its
        display header (blank if the field isn't in the schema, e.g. a name Claude made up), and the source becomes
        "Rules" or "Claude".
        """
        icon = {"error": "🔴", "warning": "🟠", "info": "🔵"}.get(self.severity, "")
        header = schema.field(self.field).header if self.field in schema.field_names else ""
        return {"": icon, "Field": header, "Column": self.column, "Issue": self.issue,
                "Suggestion": self.suggestion, "Found by": "Claude" if self.source == "claude" else "Rules"}


def _sources(mapping: Mapping, name: str) -> str:
    """Return the source columns of target field ``name`` as one comma-separated string (for messages)."""
    return ", ".join(mapping.get(name).sources)


def _out_dates(values: pd.Series, fmt: str) -> list[datetime]:
    """Parse converted date values back into datetimes using the field's output format.

    Args:
        values: converted (``out.``) values -- already normalised to ``fmt`` by the engine.
        fmt: the field's ``strptime`` format.

    Empty and unparseable values are skipped silently, so the result can be shorter than the input. Used to
    judge the *converted* dates (future dates, very old dates, all identical).
    """
    out = []
    for v in values:
        if not v:
            continue
        try:
            out.append(datetime.strptime(v, fmt))
        except ValueError:
            continue
    return out


# Per content kind (heuristics.TYPED maps field types to these kinds): the "does this value look like one?" test
# from the normalizers, and the plural noun used in messages.
_LOOKS_LIKE = {"phone": N.looks_like_phone, "email": N.looks_like_email, "date": N.looks_like_date,
               "zip": N.looks_like_zip, "state": N.looks_like_state}
_KIND_WORDS = {"phone": "phone numbers", "email": "email addresses", "date": "dates", "zip": "ZIP codes",
               "state": "states"}


def column_warnings(schema: TargetSchema, mapping: Mapping,
                    profiles: list[ColumnProfile] | None) -> dict[str, tuple[str, str]]:
    """Columns whose content doesn't fit the field they feed — {column: (field name, message)}.

    Catches a column like "Occupation" going to the phone field because someone typed a phone number into it:
    the column name doesn't say "phone" and most of its values aren't phone numbers.

    How it decides, per source column of each typed field (phone, email, date/datetime, ZIP, state):
        * Only plain pass-through mappings (copy, first-non-empty, one-row-per-value) are checked; transforms
          like split/combine/name part legitimately read columns that hold other content.
        * A column is fine if its name says it belongs there (``header_score`` >= 70, or a phone-like name for a
          phone field) -- the user/column name is trusted even when the values are messy.
        * Otherwise it is fine if enough of its distinct sampled values look like that kind: at least 60 % for
          phone and email (those are easy to recognise), at least 30 % for dates, ZIPs and states (formats vary
          more, so the test is less certain).
        * Anything else is flagged, with the counts and an example value that doesn't fit. Each column is
          reported once (for the first field it feeds).

    Args:
        schema, mapping: the target schema and the mapping to check.
        profiles: column profiles of the file (``mapping.profile_columns``); ``None`` -> no warnings.

    Returns:
        ``{column: (field_name, message)}``. Pure function.
    """
    by_name = {p.name: p for p in profiles or []}
    out: dict[str, tuple[str, str]] = {}
    for f in schema.mappable_fields:
        kind = TYPED.get(f.type)
        fm = mapping.get(f.name)
        if not kind or fm.transform not in ("copy", "coalesce", "each_row"):
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
            # Profiles measure shares over at most 300 distinct values, so convert the share back to a count of
            # that same sample for the message.
            n_unique = min(p.unique, 300)
            n_like = round(share * n_unique)
            example = next((v for v in p.samples if not looks(v)), "")
            # Only claim "typed into the wrong column" when at least one value does look right (that's the
            # stray value); with none, the column is simply the wrong one.
            msg = (f"'{col}' goes to {f.header}, but its name doesn't say so and only {n_like} of its {n_unique} "
                   f"distinct values {'looks' if n_like == 1 else 'look'} like {_KIND_WORDS[kind]}"
                   + (f" (e.g. '{example}')" if example else "") + ". "
                   + ("Someone probably typed a value into the wrong column." if n_like else ""))
            out[col] = (f.name, msg.strip())
    return out


def _raw_dates(raw: pd.Series) -> list[datetime]:
    """Dates with a real year, as written in the source (before placeholder years are dropped).

    Birthday/anniversary fields output recent or future years as "month/day only" (placeholder year), so the
    converted values can't show whether the file's years were plausible; this re-parses the original text instead.
    Looks at the first 2000 non-empty values; unparseable values, month/day-only values (``NO_YEAR``) and years up
    to 1900 (typical "unknown" fillers) are skipped.
    """
    from .dateparse import NO_YEAR, DateError, parse

    out = []
    for v in raw[raw != ""].head(2000):
        try:
            dt, _ = parse(v, allow_no_year=True)
        except (DateError, ValueError, OverflowError, TypeError):
            continue
        if dt.year != NO_YEAR and dt.year > 1900:
            out.append(dt)
    return out


def run_checks(schema: TargetSchema, mapping: Mapping, source: pd.DataFrame, audit: pd.DataFrame,
               profiles: list[ColumnProfile] | None = None, timezone_label: str = "",
               today: date | None = None) -> list[Finding]:
    """Rule-based checks. `source`/`audit` may be a sample (pre-import preview) or the whole import.

    Args:
        schema: target schema.
        mapping: the mapping that produced ``audit``.
        source: original rows aligned with ``audit`` (currently not read by the rules; part of the shared
            signature with ``build_review_payload``).
        audit: per-row / per-cell conversion record (``engine.ConversionResult.audit``).
        profiles: column profiles of the whole file; enables the column-content and unused-column checks.
        timezone_label: name of the client time zone, used in the "times were converted" message.
        today: reference date (tests pass a fixed date); defaults to today.

    Returns:
        Findings sorted by severity, then field, then column. No side effects.

    Checks, in order (each explained where it is implemented below):
        per mapped field -- many values unreadable; nothing came through; for dates: future / very old values,
        birthday columns that look like sign-up dates, one value repeated everywhere, mixed day/month order,
        times with their own time zone; then file-level -- column content doesn't fit its field, one column
        feeding several fields, many bad rows (and 7-digit phones), many duplicates, useful columns not imported.
    """
    today = today or date.today()
    findings: list[Finding] = []
    # Empty rows say nothing about the mapping; all rates below are relative to the non-empty ("live") rows.
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
        # Check: many values can't be read. Of the cells that had an input value, how many failed validation
        # (left blank or rejected the row)? >= 30 % (with at least 5 values) suggests a wrong format option;
        # >= 80 % means the column almost certainly holds something else -> error.
        # Excel error codes (#REF!, #VALUE!…) mean a formula in the client's file was broken when it was exported,
        # not that this is the wrong column: they get their own finding and are left out of the share below.
        if raw is not None:
            sheet_err = raw.map(lambda x: bool(x) and N.sheet_error(x) is not None).astype(bool)
            if n_err := int(sheet_err.sum()):
                findings.append(Finding(
                    "warning", f.name, src,
                    f"{n_err:,} value(s) from '{src}' are Excel errors (e.g. '{raw[sheet_err].iloc[0]}'): a formula "
                    "in the client's file was broken when it was exported, so those cells hold no data.",
                    "If this column matters, ask the client for a fresh export."))
            raw = raw[~sheet_err]
            st = st[~sheet_err]
        attempted = int((raw != "").sum()) if raw is not None else 0
        failed = int(((raw != "") & st.isin([CELL_BLANKED, CELL_REJECT])).sum()) if raw is not None else 0
        if attempted >= 5 and failed / attempted >= 0.3:
            example = raw[st.isin([CELL_BLANKED, CELL_REJECT]) & (raw != "")].iloc[0] if failed else ""
            severe = failed / attempted >= 0.8
            findings.append(Finding(
                "error" if severe else "warning", f.name, src,
                f"{failed / attempted:.0%} of values from '{src}' can't be read as {f.type} (e.g. '{example}').",
                "This is probably the wrong column." if severe else "Check the column or its format option."))
        # Check: the field is mapped and its sources had values, but not a single converted value came out.
        if attempted and out is not None and (out != "").sum() == 0:
            findings.append(Finding("warning", f.name, src, f"'{src}' is mapped but no value came through.",
                                    "Check that this is the right column."))

        # Date checks need the output format to read the converted values back.
        if f.type in ("date", "datetime") and out is not None and f.format:
            dts = _out_dates(out, f.format)
            if not dts:
                continue
            # Leave out month/day-only values (output with the placeholder year) when judging years.
            real = [d for d in dts if f.no_year_value is None or d.year != f.no_year_value]
            if f.type == "datetime":
                # Check (date+time fields, e.g. last activity): values in the future. One day of slack absorbs
                # time-zone shifts. Usually means day/month were read the wrong way round, or a wrong column.
                future = [d for d in real if d.date() > today + timedelta(days=1)]
                if future:
                    findings.append(Finding(
                        "warning", f.name, src,
                        f"{len(future):,} value(s) are in the future (latest {max(future):%m/%d/%Y}). "
                        "A 'last activity' time can't be in the future.",
                        "Check day/month order (Option: DMY) or whether this is the right column."))
                # Check: a sizeable share (>= 20 %) before 2000 -- likely two-digit years read in the wrong century.
                old = [d for d in real if d.year < 2000]
                if old and len(old) >= 0.2 * len(real):
                    findings.append(Finding("info", f.name, src, f"{len(old):,} value(s) are before the year 2000.",
                                            "Check two-digit years and that this is the right column."))
            elif f.no_year_value is not None:  # birthday / anniversary
                # Judge the years as written in the file: recent or future years are output as month/day only.
                written = _raw_dates(raw) if raw is not None else []
                # Check: a birth/anniversary year in the future can't be right (the year was dropped anyway).
                future = [d for d in written if d.date() > today]
                if future:
                    findings.append(Finding("info", f.name, src,
                                            f"{len(future):,} value(s) have a year in the future (e.g. "
                                            f"{future[0]:%m/%d/%Y}); the year was dropped ({f.no_year_value}).",
                                            "Check the column and the day/month order."))
                # Check: at least half of the dates fall in the last two years. Real birthdays don't cluster
                # there; sign-up / visit dates do (or Excel added the current year to "month/day" values).
                recent = [d for d in written if today - timedelta(days=730) <= d.date() <= today]
                if len(written) >= 5 and len(recent) >= 0.5 * len(written):
                    findings.append(Finding(
                        "warning", f.name, src,
                        f"{len(recent) / len(written):.0%} of these dates are within the last two years — that looks "
                        "like sign-up or visit dates, not birthdays/anniversaries (or a year Excel added to dates "
                        f"typed without one; those are output with year {f.no_year_value}).",
                        "Confirm the column."))
            # Check: 10+ values that are all the same date -- typically an export default, not real data.
            if len(dts) >= 10 and len(set(dts)) == 1:
                findings.append(Finding("info", f.name, src, f"Every row has the same value ({dts[0]:%m/%d/%Y}).",
                                        "Might be an export default rather than real data."))
            # mixed day/month evidence in the raw values
            # A value like 25/03/2024 can only be day-first (first number > 12), 03/25/2024 only month-first.
            # Seeing both kinds in one column means the ambiguous ones (03/04) can't all be read correctly.
            # Booleans add as 0/1, so "dmy += a > 12 >= b" counts the day-first evidence.
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
                # Info (date+time fields only): times that carried their own zone were shifted to the client zone,
                # so they may look different from the file -- tell the user why.
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
    # (Derived uses such as name parts or address parts legitimately read one column for several fields, so only
    # pass-through transforms are counted.)
    copies = Counter(s for f in schema.mappable_fields for s in mapping.get(f.name).sources
                     if mapping.get(f.name).transform in ("copy", "coalesce", "each_row", "area_code"))
    for col, n in copies.items():
        if n > 1:
            fields = [f.header for f in schema.mappable_fields if col in mapping.get(f.name).sources]
            findings.append(Finding("warning", "", col, f"'{col}' is used for {n} fields: {', '.join(fields)}.",
                                    "Usually each column belongs to one field."))

    # Rejected-row patterns.
    # When at least 10 % of rows (and at least 3) are bad, report the most common reason. A row's _reason lists
    # its problems separated by "; ", each possibly followed by " ('<value>')"; cutting at " ('" groups the same
    # problem across different values.
    rejected = audit[audit["_status"] == "rejected"]
    if n_live and len(rejected) / n_live >= 0.1 and len(rejected) >= 3:
        reasons = Counter(r.split(" ('")[0] for rs in rejected["_reason"] for r in rs.split("; "))
        top, cnt = reasons.most_common(1)[0]
        findings.append(Finding("warning", "", "", f"{len(rejected) / n_live:.0%} of rows are bad rows; most common: "
                                f"{top} ({cnt:,}).", ""))
        # 7-digit phone numbers usually mean the area code is in a separate column.
        if any("missing area code" in r for r in reasons):
            findings.append(Finding("warning", "phone_number" if "phone_number" in schema.field_names else "", "",
                                    "Some phone numbers have 7 digits (no area code).",
                                    "If the area code is in its own column, set How to 'Phone + area code column' "
                                    "for the phone, with that column and the phone column(s)."))
    # Many exact duplicates (>= 30 % of at least 10 rows) often means the same export was pasted in twice.
    dup = (audit["_status"] == "duplicate").sum()
    if n_live >= 10 and dup / n_live >= 0.3:
        findings.append(Finding("info", "", "", f"{dup / n_live:.0%} of rows are exact duplicates.",
                                "The file may contain the same export twice."))

    # Columns that look useful but aren't imported.
    # Consent columns are mentioned only to explain that leaving them out is intended. Date-like columns are
    # suggested when a date field is still unmapped; phone-like columns can be added to the phone field.
    unmapped_dates = [f.header for f in schema.auto_fields
                      if f.type in ("date", "datetime") and mapping.get(f.name).transform == "none"]
    for p in profiles or []:
        if p.name in used or p.non_empty == 0:
            continue
        if is_consent_column(p.name):
            findings.append(Finding("info", "", p.name, f"'{p.name}' is not imported.",
                                    "Consent is only imported when you map it yourself: if this column holds the "
                                    "customers' consent, send it to consent under Columns in your file with what "
                                    "it records: opt-ins only (a no is left blank, e.g. an email opt-in column), "
                                    "opt-ins and opt-outs (a no opts the customer out of texts) or opt-outs only "
                                    "(e.g. Unsubscribed: a yes opts the customer out, a no is left blank). "
                                    "Otherwise leaving it out is intended."))
            continue
        if p.shares.get("date", 0) >= 0.6 and unmapped_dates:
            findings.append(Finding("info", "", p.name, f"'{p.name}' contains dates but isn't imported.",
                                    "Does it belong in one of: " + ", ".join(unmapped_dates) + "?"))
        elif p.shares.get("phone", 0) >= 0.6:
            findings.append(Finding("info", "", p.name, f"'{p.name}' contains phone numbers but isn't used.",
                                    "Add it to the phone field (Goes to → phone field) to give those numbers their "
                                    "own rows."))

    findings.sort(key=lambda x: (SEVERITY_ORDER.get(x.severity, 9), x.field, x.column))
    return findings


# ---------------------------------------------------------------------------
# Sample for Claude's review
# ---------------------------------------------------------------------------

def mask_phone(value: str) -> str:
    """Keep the shape and area code, hide the rest: (516) 555-0100 → (516) ###-##00.

    Privacy measure for the Claude review: enough stays visible to judge the format, the area code and whether a
    number was converted correctly, but not enough to identify or call the person.

    Rules (positions are digits only; punctuation and spaces are always kept):
        * fewer than 7 digits -> returned unchanged (not a phone number);
        * up to 11 digits -> keep the first 3 and the last 2;
        * exactly 11 digits starting with "1" (US country code, with or without "+") -> keep the first 4
          ("1" + area code) and the last 2;
        * more than 11 digits (international / with extension) -> keep only the first 3.
    """
    digits = [i for i, ch in enumerate(value) if ch.isdigit()]
    if len(digits) < 7:
        return value
    keep = set(digits[:3] + digits[-2:]) if len(digits) <= 11 else set(digits[:3])
    if len(digits) == 11 and value.strip().lstrip("+").startswith("1"):
        keep = set(digits[:4] + digits[-2:])
    return "".join(ch if (not ch.isdigit() or i in keep) else "#" for i, ch in enumerate(value))


def pick_sample_rows(audit: pd.DataFrame, limit: int = 30) -> list[int]:
    """Rows worth showing a reviewer: up to a third problems, a third reformatted, the rest ordinary rows.

    Groups, filled in this order without picking a row twice:
        1. problems -- bad rows, or rows with a cell left blank as invalid or repaired from a typo (``limit // 3``);
        2. changed -- rows with a reformatted or Claude-fixed cell (``limit // 3``);
        3. any non-empty rows, in file order, until ``limit`` is reached.
    Empty rows are never picked. Duplicate rows are not prioritised (they would only repeat other rows).

    Returns:
        Up to ``limit`` audit index labels, sorted (file order).
    """
    live = audit[audit["_status"] != "empty"]
    st_cols = [c for c in audit.columns if c.startswith("st.")]
    problem = live[(live["_status"] == "rejected")
                   | live[st_cols].isin([CELL_BLANKED, CELL_REPAIRED]).any(axis=1)].index
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
    """Build the compact, JSON-serialisable summary of a conversion that Claude reviews for mistakes.

    Claude never gets the whole file: only aggregate statistics plus up to ``limit`` sample rows chosen by
    ``pick_sample_rows``. ``ai.ClaudeAssistant.review`` sends this dict together with the column profiles and the
    current mapping.

    Args:
        schema, mapping: target schema and the mapping under review.
        source: original rows aligned with ``audit``.
        audit: per-row / per-cell conversion record (sample or whole import).
        findings: the rule findings from ``run_checks`` (so Claude doesn't repeat them).
        mask_phones: mask phone digits with ``mask_phone`` (setting ``AI_MASK_PHONES``, on by default).
        limit: maximum number of sample rows.
        stats_scope: text telling Claude what the statistics cover (e.g. a preview sample or the whole import).

    Returns a dict with:
        scope, rows_checked (non-empty rows), row_status (count per row status),
        mapping (field header -> mapping summary), unused_columns (names only),
        cell_status_counts_per_field (per mapped field: count per cell status code, plus "empty"),
        rule_findings (``asdict`` of each finding), sample_rows and phones_masked (so the prompt can say that
        "#" in phone numbers is masking, not data).

    Each sample row: file_row, status, ``original`` (non-empty values of the mapped columns plus up to 6 unused
    columns for context), ``converted`` (non-empty converted values by field header) and ``problems`` (per-field
    reasons by field header).

    What is masked: when ``mask_phones`` is on, the original values of source columns that feed a phone-type
    field and the converted values of phone-type fields. Nothing else is masked -- names, emails, addresses and
    dates are sent as they are, as are numbers in columns that don't feed a phone field (e.g. an unused phone
    column included for context) and the problem reason texts.
    """
    # Which values count as phone numbers for masking: converted phone fields, and the source columns feeding them.
    phone_fields = {f.name for f in schema.fields if f.type == "phone"}
    phone_cols = {s for n in phone_fields for s in mapping.get(n).sources}
    # Columns shown in sample rows: every mapped column, plus the first 6 unused ones (enough for Claude to spot a
    # useful column that was left out, while keeping the payload small).
    mapped_cols = [c for c in source.columns if c in mapping.used_sources()]
    other_cols = [c for c in source.columns if c not in mapping.used_sources()][:6]

    def m(col_or_field: str, value: str, is_phone: bool) -> str:
        """Mask ``value`` if phone masking is on and it is a non-empty phone value; otherwise return it unchanged.

        ``col_or_field`` (the column or field the value belongs to) is not used for the decision; it only makes
        the call sites read clearly.
        """
        return mask_phone(value) if (mask_phones and is_phone and value) else value

    rows = []
    for idx in pick_sample_rows(audit, limit):
        a = audit.loc[idx]
        issues = json.loads(a["_issues"]) if a["_issues"] else {}
        rows.append({
            "file_row": int(a["_row"]),
            "status": a["_status"],
            # Empty cells are left out to keep the payload small.
            "original": {c: m(c, str(source.at[idx, c]), c in phone_cols) for c in mapped_cols + other_cols
                         if str(source.at[idx, c])},
            "converted": {f.header: m(f.name, a.get(f"out.{f.name}", ""), f.name in phone_fields)
                          for f in schema.mappable_fields if a.get(f"out.{f.name}", "")},
            "problems": {schema.field(k).header: v for k, v in issues.items() if k in schema.field_names},
        })
    # Per mapped field: how many cells got each status (the "" status = empty cell is reported as "empty").
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
