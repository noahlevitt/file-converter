"""Conversion engine: apply a mapping to every row, normalize, validate, dedupe, and report.

Deterministic: the same file + mapping always produces the same output. Claude is never called here.

Responsibilities
----------------
This module is the only place where source data is turned into import data. It takes the uploaded file (already
read into a DataFrame of text columns by ``reader.py``), the target schema (``schema.py``, loaded from YAML) and a
reviewed mapping (``mapping.py``), and produces:

* the import-ready rows (columns = schema headers, in schema order),
* a rejects report (bad rows and removed duplicates, with reasons and the original values),
* summary counts (imported / rejected / duplicates / empty rows / blanked, skipped and repaired values), and
* a per-cell audit table that powers the side-by-side comparison, the rule checks and Claude's mistake check.

The engine is schema-driven and knows nothing about specific business rules. Rules such as "US phone numbers only",
"birthday/anniversary without a year is written with year 1600" or "activity timestamps are converted to the
import's time zone" live in the schema YAML and in ``normalizers.py``. The one rule the engine enforces itself is
``fixed_blank``: such a field is always written blank, whatever the file or the mapping says. (Consent and
last_contacted are ``manual_only`` instead: ``Mapping.normalized`` keeps them only when a person mapped them.)

It does no file I/O and has no side effects apart from calling the optional ``progress`` callback.

Main entry points
-----------------
* :func:`convert` - run a full conversion and return a :class:`ConversionResult`. Used by the background import
  runner (``jobs.py``), the CLI, and the "Check for mistakes" preview on the first 1,000 rows (``app.py``).
* :func:`preview_values` - convert the first 200 rows and return a few sample output values per field for the
  mapping review screen.
* :func:`source_rows` - line source rows up with audit rows when one file row became several output rows.
* :func:`count_multi_number_cells` - how many phone cells hold several numbers (for the mapping page).
* ``CELL_*`` constants - per-cell status codes, shared with ``compare.py`` (colors, labels, filters) and
  ``checks.py`` (rule checks, choosing sample rows for Claude).

Data flow inside :func:`convert`
--------------------------------
1. Normalize the mapping against the file (unknown fields/columns dropped, every schema field present) and turn
   every cell into text. A row whose cells are all empty is an "empty" row: it is skipped, never rejected.
2. Expand the rows for the phone field (:func:`_expand_rows`; the field comes from ``Mapping.row_field``: the one
   using ``each_row`` / 'One row per phone number', else a phone field copied from one column or using 'Phone +
   area code column', where each 7-digit number first gets its row's area code from its own area code column,
   ``mapping.area_code_pairs``): a file row with several *different* valid phone numbers - in separate columns or
   several in one cell - becomes one working row per number, every other field copied. From here on the engine
   works on "working rows"; ``src`` maps each working row back to its file row.
3. Field by field (column-at-a-time, so each normalizer can first look at the whole column via ``N.prepare`` -
   e.g. to detect day-first dates or the file's common area codes): run the transform to get the raw value, then
   normalize/validate it, and record the output value, the raw value and a cell status. Identical raw values are
   normalized once (per-field cache).
4. Classify each working row: rejected (any issue with ``rejected=True``) or accepted; count blanked, skipped and
   repaired values.
5. Remove duplicates among the accepted rows on ``schema.dedupe.keys`` (the real schema uses ``["*"]``, i.e. every
   mappable field, so a row is only a duplicate when *every* column matches).
6. Build the output, the rejects report, the audit table and the summary counts.

Kinds of problem values (see :class:`RowIssue`)
-----------------------------------------------
* **rejected** - a required value is missing, or a value is invalid in a field with ``on_invalid: reject``
  (the default for required fields). The whole row is NOT imported and appears in the rejects report.
* **blanked** - an invalid value in a field with ``on_invalid: blank`` (the default for optional fields). The value
  is left empty but the row still imports.
* **skipped** - an invalid *extra* phone number (e.g. in a 3rd phone column, or the second number in a cell), or
  a number labelled fax, on a file row that has at least one valid number. It doesn't get its own row; it is only
  noted. The row itself still imports.
* **repaired** - a value the normalizer fixed from a likely typo (e.g. a phone digit typed twice). It imports
  with the repaired value but is flagged so a person can check it (Side by side -> Problems only).

Audit table layout (``ConversionResult.audit``)
-----------------------------------------------
One row per *working* row, in file order, including empty rows (so its length can exceed the file's when phone
rows were expanded). Columns:

=================  ============================================================================================
``_row``           file row number as the user sees it (Excel/CSV line number), repeated on expanded rows
``_src``           0-based position of the row in the source DataFrame (see :func:`source_rows`)
``_extra``         True for the 2nd, 3rd... working row made from one file row (one row per phone number)
``_status``        "imported", "rejected", "duplicate" or "empty" (see ``ROW_STATUSES``)
``_reason``        why the row was rejected / which row it duplicates; empty otherwise
``_issues``        JSON ``{field_name: reason}`` of every issue on the row (rejected, blanked, skipped, repaired),
                   or "" when there are none. One entry per field: a later issue on the same field wins.
``out.<field>``    the converted value written to the import (also kept for rejected/duplicate rows)
``raw.<field>``    the value before normalizing: the transform's output after ``clean_text`` (not always the
                   literal file cell, e.g. for a name part, combined columns, or one number of a phone cell
                   holding several)
``st.<field>``     cell status, one of the ``CELL_*`` codes, or "" (empty row, empty optional value, fixed blank)
=================  ============================================================================================

When no file row has several numbers there is exactly one audit row per source row and ``_src`` is ``0..n-1``.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd

from . import normalizers as N
from .mapping import Mapping, area_code_pairs, opt_out_problem
from .schema import TargetSchema
from .transforms import apply_transform


# Progress callback signature. The engine calls it at the start of each field, every `chunk` rows inside a field,
# and once more before deduplication. jobs.ProgressTracker throttles database writes and raises ImportCancelled
# when a user clicks Cancel; the engine does not catch that exception, so the conversion stops immediately and no
# partial result is returned.
ProgressFn = Callable[[float, str], None]  # (fraction 0–1, stage text); may raise to cancel

# Per-cell status codes stored in the audit table (used by the side-by-side comparison).
# They are short because they are stored once per cell (rows x fields) in audit.parquet. compare.py maps them to
# labels/colors and filters ("Problems only" = blanked or repaired); checks.py uses them to count failures per field
# and to pick interesting sample rows for Claude's review. A cell with no status ("") is an empty row, an empty
# optional value with no default, or a fixed-blank field.
CELL_SAME = "same"      # copied unchanged
CELL_FORMAT = "fmt"     # reformatted (phone, date, case, ZIP…)
CELL_DERIVED = "drv"    # built from part of / several columns (name part, address part, combine, split, translate)
CELL_DEFAULT = "def"    # filled from a default or fixed value
CELL_FIXED = "fix"      # user-approved AI fix
CELL_REPAIRED = "rpr"   # repaired from a likely typo (e.g. a phone digit typed twice) — worth a look
CELL_BLANKED = "blk"    # invalid optional value, left blank
CELL_OPTOUT = "out"     # a false value (opt-out) left blank because consent was mapped as "opt-ins only" (0.22.0)
CELL_REJECT = "rej"     # invalid or missing required value → row rejected
# Possible values of the audit's _status column. "empty" rows (every cell blank) are skipped, not rejected.
ROW_STATUSES = ("imported", "rejected", "duplicate", "empty")


@dataclass
class RowIssue:
    """One problem found with one value on one working row.

    Issues are collected per working row while each field is converted, then used to decide whether the row is
    rejected, to build the summary counts (rejected / blanked / skipped / repaired), the rejects report's Reason
    column and the audit's ``_issues`` JSON. The flags are mutually exclusive in practice:

    * ``rejected=True``  - the row is not imported (missing required value, or invalid value with on_invalid=reject).
    * ``rejected=False`` and no other flag - the value was *blanked*: left empty, the row still imports.
    * ``skipped=True``   - an invalid (or fax) extra phone number that didn't get a row of its own (row still
      imports).
    * ``repaired=True``  - the value was repaired from a likely typo and imported; ``reason`` starts with
      "repaired:" and says how.
    """
    field: str  # schema field name (FieldSpec.name, not the output header)
    value: str  # the cleaned raw value that caused the issue ("" for a missing required value)
    reason: str  # human-readable reason from the normalizer or the engine, e.g. "too few digits (5)"
    rejected: bool  # True = caused the row to be rejected; False = value was blanked
    skipped: bool = False  # an extra value (e.g. a 3rd phone column) that was invalid or fax and got no row
    repaired: bool = False  # value was repaired from a likely typo; reason says how


@dataclass
class ConversionResult:
    """Everything one conversion produced: output rows, rejects report, counts and the per-cell audit table.

    Built only by :func:`convert`. ``jobs.py`` writes ``output`` (via ``output.write_output``), ``rejects``,
    a text summary and ``audit`` (as parquet) to the import's folder and stores ``summary_dict()`` on the import
    record. Counts are in *working* rows (after 'One row per phone number' expansion) except ``total_rows`` and
    ``blank_rows``, which count file rows. So ``imported`` can be larger than ``total_rows``; ``extra_rows`` says
    by how much the expansion added.

    The ``*_counts`` Counters are keyed by ``"<Header>: <reason>"`` with per-value details removed
    (see :func:`_reason_key`), so the summary groups e.g. all unknown area codes into one line.
    """
    output: pd.DataFrame  # rows that import, columns = schema headers
    rejects: pd.DataFrame  # rejected + duplicate rows with reasons and original values
    total_rows: int  # data rows in the file, excluding blank rows
    imported: int  # working rows written to the output (after rejects and duplicates are removed)
    rejected: int  # bad rows (failed validation)
    duplicates: int  # accepted rows removed because an earlier (or later, with keep=last) row has the same keys
    blank_rows: int  # file rows where every cell was empty; skipped, not counted as rejected
    blanked_values: int  # optional values that were invalid and left blank
    reason_counts: Counter = field(default_factory=Counter)  # why rows were rejected: "Header: reason" -> count
    blanked_counts: Counter = field(default_factory=Counter)  # why values were blanked: "Header: reason" -> count
    # Share of imported rows with a non-empty value, per output header (0.0 when nothing imported).
    fill_rates: dict[str, float] = field(default_factory=dict)
    # Feeds "Suggest fixes" (Claude repair of unconvertible values) and the preview warnings. Includes values that
    # were blanked as well as values that rejected the row, but not missing required values (nothing to repair).
    failed_values: dict[str, list[str]] = field(default_factory=dict)  # field -> unique raw values that failed
    # One row per converted row, in file order: _row (file row number), _src (position in the source DataFrame),
    # _extra (True for the 2nd, 3rd… row made from one file row by 'One row per phone number'), _status, _reason,
    # _issues (JSON), and per field: out.<name> (converted value), raw.<name> (value before normalizing),
    # st.<name> (cell status). Without 'One row per phone number' there is exactly one audit row per source row.
    audit: pd.DataFrame = field(default_factory=pd.DataFrame)
    extra_rows: int = 0  # rows added because a file row had several different values (e.g. phone numbers)
    skipped_values: int = 0  # invalid (or fax) extra values that didn't get a row of their own
    skipped_counts: Counter = field(default_factory=Counter)  # why extra values were skipped: "Header: reason"
    repaired_values: int = 0  # values repaired from likely typos (each marked in the audit)
    repaired_counts: Counter = field(default_factory=Counter)  # how values were repaired: "Header: repaired: ..."
    # False values (opt-outs) left blank because consent was mapped as "opt-ins only" (0.22.0). Counted on every
    # non-empty row, like blanked_values (also rows then rejected for another reason or removed as duplicates).
    opt_outs_left_blank: int = 0

    @property
    def has_bad_rows(self) -> bool:
        """True when at least one row was rejected (duplicates and empty rows don't count as bad rows)."""
        return self.rejected > 0

    def flag_message(self) -> str:
        """One plain-English paragraph summarising the result, shown in the UI and written to the summary file.

        Only non-zero counts are mentioned, with singular/plural wording. When phone rows were expanded the first
        sentence says "N rows ready to import from M file rows" instead of "N of M rows", because the output can
        then have more rows than the file.
        """
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
            parts.append(f"{self.skipped_values:,} extra phone number{'s' if self.skipped_values != 1 else ''} "
                         "skipped (invalid, or labelled fax).")
        if self.repaired_values:
            parts.append(f"{self.repaired_values:,} phone number{'s were' if self.repaired_values != 1 else ' was'} "
                         "repaired from likely typos — check them under Side by side → Problems only.")
        if self.opt_outs_left_blank:
            n = self.opt_outs_left_blank
            parts.append(f"{n:,} consent value{'s' if n != 1 else ''} of false left blank (consent: opt-ins only), "
                         "so this import opts nobody out.")
        return " ".join(parts)

    def summary_dict(self) -> dict[str, Any]:
        """JSON-serialisable summary stored on the import record (``storage.py``) and shown on the results page.

        Counters become plain dicts ordered from most to least common. ``jobs.run_import`` adds ``fill_rates``,
        ``failed_values`` and reader warnings to this dict before saving it.
        """
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
            "opt_outs_left_blank": self.opt_outs_left_blank,
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

    df: the file as read by ``reader.read_file`` - one column per source header; values are treated as text
        (NaN becomes ""). Not modified.
    schema: the target import format. Its field order is the output column order.
    mapping: the reviewed mapping (target field -> transform + source columns + params). It is normalized against
             ``df``'s columns first, so stale templates, skipped columns and missing sources are handled here;
             the caller's object is not modified.
    row_numbers: original file row number for each df row (for the rejects report).
                 Defaults to ``index + 2`` (row 1 is the header row, as in a spreadsheet).
    overrides: optional {field_name: {raw_value: final_output_value}} — user-approved repairs. A raw value
               listed here skips normalization and outputs the approved value (already validated).
               (They come from "Suggest fixes": Claude's suggestions, re-validated by ``services.validate_repairs``
               and approved by a person, then the import is re-run with them. Claude itself is never called here.)
    progress: optional callback(fraction, stage) called regularly; raising from it aborts the conversion.
              The exception propagates unchanged (this is how Cancel works in ``jobs.py``).
    timezone: IANA zone for this import (e.g. America/New_York). Times in the file that carry a zone, and Unix
              timestamps, are converted to it; times without a zone are kept as written.

    Returns a :class:`ConversionResult`. See the module docstring for the step-by-step data flow and the audit
    table layout. Deterministic: no randomness and no network calls, so identical inputs give identical output,
    rejects and audit. (The one outside input is today's date, which the date normalizer uses to decide whether a
    birthday's year is really a "no year" placeholder, and the date parser uses to choose 19xx or 20xx for
    two-digit years.)
    """
    mapping = mapping.normalized(schema, list(df.columns))
    overrides = overrides or {}
    # Work on plain dicts of strings: much faster than DataFrame row access in the per-row loops below.
    src_records = df.fillna("").astype(str).to_dict("records")
    row_numbers = row_numbers or [i + 2 for i in range(len(src_records))]

    all_cols = list(df.columns)
    # A row is "empty" when every cell of the file (not just the mapped columns) is blank after cleaning.
    # Empty rows get status "empty": no output, no reject, counted separately as blank_rows.
    src_blank = [not any(N.clean_text(r.get(c, "")) for c in all_cols) for r in src_records]

    # 'One row per phone number': a file row with several different numbers (in Phone, Phone 2, Phone 3… or several
    # in one cell) becomes one row per number; every other field is copied onto each. `src` maps each working row to
    # its file row. The field is the one using each_row, else a phone field copied from one column (Mapping.row_field).
    # Mapping.normalized only allows each_row on phone fields and Mapping.problems flags a second one. If two fields
    # used it anyway, only the first expands rows; apply_transform treats each_row on the other like coalesce.
    each_field = mapping.row_field(schema)
    src = list(range(len(src_records)))
    each_raw: list[str] = []  # raw value of the each_row field per working row (only used when each_field is set)
    early_issues: dict[int, list[RowIssue]] = {}  # "extra value skipped" notes found while expanding, per work row
    each_ctx: dict = {}  # normalizer context built from ALL the each_row source columns
    fax_rows: set[int] = set()  # working rows whose only number is labelled fax: rejected (N.FAX_REASON)
    if each_field is not None:
        src, each_raw, early_issues, each_ctx, fax_rows = _expand_rows(src_records, src_blank, each_field, mapping,
                                                                       overrides, timezone)
    # Re-index everything per working row. Without expansion `src` is 0..n-1 and these are plain copies.
    records = [src_records[k] for k in src]
    blank = [src_blank[k] for k in src]
    row_numbers = [row_numbers[k] for k in src]
    n = len(records)
    # Expanded rows of one file row are contiguous, so "same file row as the previous working row" marks the
    # 2nd, 3rd… row made from a file row (audit column _extra).
    extra = [j > 0 and src[j] == src[j - 1] for j in range(n)]

    # Per-field columns, each a list with one entry per working row: converted value, raw value, cell status.
    out_cols: dict[str, list[str]] = {}
    raw_cols: dict[str, list[str]] = {}
    status_cols: dict[str, list[str]] = {}
    issues: list[list[RowIssue]] = [list(early_issues.get(i, [])) for i in range(n)]
    failed_values: dict[str, set[str]] = {}
    chunk = 2000  # report progress every `chunk` rows inside a field (and give Cancel a chance to fire)
    # Relative cost per field, so progress and time-left estimates move evenly.
    # Unmapped and fixed-blank fields cost almost nothing (0.1); phone parsing/repair is the slowest.
    cost = {"phone": 4.0, "email": 3.0, "date": 2.0, "datetime": 2.0}
    weights = [0.1 if f.fixed_blank or mapping.get(f.name).transform == "none" else cost.get(f.type, 1.0)
               for f in schema.fields]
    total_w = sum(weights) or 1.0
    done_w = 0.0

    # Column-at-a-time: each field is fully converted before the next one, so the normalizer context (ctx) can be
    # built from the whole column first.
    for fi, f in enumerate(schema.fields):
        if f.fixed_blank:  # always blank, whatever the file or mapping says
            out_cols[f.name] = [""] * n
            raw_cols[f.name] = [""] * n
            status_cols[f.name] = [""] * n
            done_w += weights[fi]
            continue
        fm = mapping.get(f.name)
        params = fm.params
        if progress:
            progress(done_w / total_w, f"Converting {f.header}")
        # Step 1: the transform pulls the raw value out of the source column(s). For the each_row field the value
        # was already chosen per working row by _expand_rows.
        if f is each_field:
            raw = ["" if blank[i] else each_raw[i] for i in range(n)]
        else:
            raw = [
                "" if blank[i] else apply_transform(fm.transform, records[i], fm.sources, params)
                for i in range(n)
            ]
        fixes = overrides.get(f.name, {})
        cleaned = [N.clean_text(v) for v in raw]
        # Step 2: per-column context for the normalizer, built from this field's values on non-empty rows (may
        # include empty strings; the prepare functions skip them), e.g. the
        # date order (day-first or month-first), Excel serial dates, a placeholder year, or the area codes the
        # file's valid numbers use (evidence for repairing typos). Normalizers may also write into it: phone
        # repairs are recorded in ctx["repairs"] as {raw value: how it was repaired}.
        # The 'one row per number' field reuses the context built from ALL its columns (area codes in the file).
        ctx = each_ctx if f is each_field else N.prepare(f, [v for i, v in enumerate(cleaned) if not blank[i]], params)
        if timezone:
            ctx["tz"] = timezone
        # copy/coalesce/each_row/area_code pass the file value through, so "unchanged" vs "reformatted" can be told
        # apart by comparing output with input. Other transforms build a new value (name part, combine, split…).
        passthrough = fm.transform in ("copy", "coalesce", "each_row", "area_code")

        # Cache of raw value -> (output, error) for this field. Contact files repeat values a lot (same date, city,
        # state, area code…) and phone/date parsing is the expensive part, so each distinct value is normalized
        # once. It also guarantees identical inputs get identical outputs within a column.
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
            # Empty value: use the schema default if there is one, otherwise a required field rejects the row and
            # an optional one simply stays blank (no issue recorded). on_invalid doesn't apply to missing values.
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
            # Approved overrides win over normalization (they were already validated when they were approved).
            if v not in cache:
                cache[v] = (fixes[v], None) if v in fixes else N.normalize(v, f, params, ctx)
            out, err = cache[v]
            if f is each_field and i in fax_rows:
                out, err = "", N.FAX_REASON  # the row's only number is a fax line
            # Consent mapped as "opt-ins only": a false value (an opt-out) is left blank, so it opts nobody out.
            opt_out = (not err and f.type == "boolean" and params.get("opt_outs") == "skip" and out != ""
                       and out == f.false_value)
            # Cell status, in order of precedence: invalid (rejected or blanked, per the field's on_invalid) >
            # opt-out left blank > approved fix > repaired typo > fixed value > derived > unchanged/reformatted.
            if err:
                rejected = f.on_invalid == "reject"
                issues[i].append(RowIssue(f.name, v, err, rejected))
                if err != N.FAX_REASON:  # nothing to fix: Suggest fixes must not turn a fax into a phone
                    failed_values.setdefault(f.name, set()).add(v)
                out = ""
                statuses.append(CELL_REJECT if rejected else CELL_BLANKED)
            elif opt_out:
                out = ""
                statuses.append(CELL_OPTOUT)
            elif v in fixes:
                statuses.append(CELL_FIXED)
            elif v in ctx.get("repairs", {}):
                # The normalizer accepted the value only after repairing it; flag it on every row that has it
                # (ctx["repairs"] survives the cache, so later rows with the same value are flagged too).
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
    # Every working row is either accepted (may still have blanked/skipped/repaired values) or rejected (has at
    # least one issue with rejected=True). Blanked/skipped/repaired values are counted on all non-empty rows, even
    # rows that end up rejected for another reason.
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
            # Summary key: output header + reason without per-value details, so similar problems group together.
            key = f"{schema.field(x.field).header}: {_reason_key(x.reason)}"
            if x.repaired:
                repaired_counts[key] += 1
            elif x.skipped:
                skipped_counts[key] += 1
            elif not x.rejected:
                blanked_counts[key] += 1
                blanked_total += 1
        if row_rejects:
            # A row counts once per rejecting issue in reason_counts (a row can fail on several fields); the
            # rejects report's Reason lists all of them.
            for x in row_rejects:
                reason_counts[f"{schema.field(x.field).header}: {_reason_key(x.reason)}"] += 1
            rejected_rows.append((i, "; ".join(_fmt_issue(schema, x) for x in row_rejects)))
        else:
            accepted.append(i)

    # Dedupe accepted rows on the configured keys.
    # Rules:
    # * Only accepted rows take part; rejected rows are never "duplicates".
    # * Rows match when the *converted* values of every key field are equal, ignoring case. So 516-555-0100 and
    #   (516) 555-0100 count as the same phone (same output).
    # * The real schema uses dedupe keys ["*"] (every mappable field), so a row is a duplicate only when every
    #   column matches: the same phone with a different signup date is NOT a duplicate and both rows import.
    # * A row whose key fields are all empty is always kept (nothing to compare on).
    # * keep="first" keeps the earliest row in the file; keep="last" walks the rows backwards to keep the latest.
    duplicate_rows: list[tuple[int, str]] = []
    keys = schema.dedupe.keys
    if keys:
        seen: dict[tuple, int] = {}  # key tuple -> working row that was kept for it
        order = accepted if schema.dedupe.keep == "first" else list(reversed(accepted))
        kept: list[int] = []
        for i in order:
            key = tuple(out_cols[k][i].lower() for k in keys)
            if all(not k for k in key):
                kept.append(i)
                continue
            if key in seen:
                first = seen[key]
                # Pick the clearest explanation for the rejects report. The first case is a safety net: expanded
                # rows from one file row already differ in phone number, so it only triggers when the dedupe keys
                # don't include that field.
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
        accepted = sorted(kept)  # back to file order (keep="last" collected them in reverse)

    # Output: accepted rows only, in file order, columns = schema headers in schema order.
    output = pd.DataFrame(
        {f.header: [out_cols[f.name][i] for i in accepted] for f in schema.fields},
        columns=[f.header for f in schema.fields],
    )

    # Rejects report: bad rows and removed duplicates together, in file order, with File Row / Status / Reason
    # followed by the row's ORIGINAL cell values (as in the file, not converted), so the user sees what was there.
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
    # Show which value of the file row each entry is about, when file rows can hold several numbers.
    if each_field is not None and not rejects.empty and (
            mapping.get(each_field.name).transform in ("each_row", "area_code") or any(extra)):
        # An expanded file row can appear several times in the report (e.g. one number rejected, another a
        # duplicate); this extra column (after Reason) says which phone value each entry is about. If the file
        # already has a column with that name (e.g. a file in the import format itself), the extra one is renamed.
        name = each_field.header if each_field.header not in rejects.columns else f"{each_field.header} (this row)"
        rejects.insert(3, name, [each_raw[i] for i, _, _ in reject_entries])

    # Audit table: one row per working row (see the module docstring for the column layout).
    row_status = ["empty" if blank[i] else "imported" for i in range(n)]
    row_reason = [""] * n
    for i, r in rejected_rows:
        row_status[i], row_reason[i] = "rejected", r
    for i, r in duplicate_rows:
        row_status[i], row_reason[i] = "duplicate", r
    # _issues keeps one reason per field (dict): if a field has several issues on one row (e.g. two skipped extra
    # numbers), the last one is stored. Every issue is still counted in the summary counts, and every rejecting
    # issue is listed in _reason and the rejects report.
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

    # total_rows counts non-empty FILE rows (not working rows), so the summary reads "N rows ready to import from
    # M file rows" when phone rows were expanded.
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
        opt_outs_left_blank=sum(status_cols[f.name].count(CELL_OPTOUT) for f in schema.fields),
    )


def _expand_rows(records: list[dict[str, str]], blank: list[bool], f, mapping: Mapping,
                 overrides: dict[str, dict[str, str]], timezone: str | None
                 ) -> tuple[list[int], list[str], dict[int, list[RowIssue]], dict, set[int]]:
    """Work out the rows for 'One row per phone number' on field f.

    Returns (source index per working row, the raw value each working row uses, early issues per working row,
    the normalizer context, the working rows whose only number is labelled fax) - details under "Returns" below.
    A file row gets one row per different valid value (different after formatting, so 516-555-0100 and
    (516) 555-0100 count once). For a phone field, a cell holding several numbers is first split into one value
    per number (``normalizers.split_phones``), so "516-555-0100 / 917-555-1234" in one cell works exactly like two
    phone columns. Invalid extra values are skipped and noted on the row. A number labelled fax is never imported (a
    fax line can't receive texts): skipped the same way when the row has another valid number, and when it is the
    row's only number the row is rejected with N.FAX_REASON (before 0.17.0 it was used). A file row with no valid
    value keeps one row with its first value (or blank), which is then rejected the normal way.

    Inputs:
        records: every source row as {column: text}, in file order.
        blank: per source row, True when every cell is empty (such a row keeps exactly one working row).
        f: the FieldSpec that expands rows (``Mapping.row_field``: the phone field using ``each_row``, or copied from
           one column); its source columns come from ``mapping``.
        overrides: user-approved repairs, applied the same way as in :func:`convert`.
        timezone: stored in the normalizer context as ``ctx["tz"]``, like in :func:`convert` (only the date/time
                  normalizers read it).

    Returns a 5-tuple (``src`` and ``raw`` are lists indexed by *working* row; ``early`` and ``ctx`` are dicts):
        src: the source row index of each working row. Rows from one file row are contiguous and in file order,
             which is what :func:`convert` relies on to compute ``_extra``.
        raw: the raw value of field f for each working row: the first spelling of each different number, in
             column order (Phone before Phone 2 ...) and, within a cell, in the order written. For a number that
             came from a cell holding several, it is that number's piece of the cell ("C: 516-555-0100").
        early: {working row: [RowIssue]} - the "extra value skipped" notes. They are attached to the first working
               row of the file row, so they are reported once, not once per number.
        ctx: the normalizer context built from the values of ALL the source columns together, e.g. the area
             codes the file uses (evidence for repairing typos). :func:`convert` reuses it, together with any
             ``ctx["repairs"]`` recorded here, when it converts field f.
        fax_only: the working rows whose only valid number is labelled fax; :func:`convert` rejects them with
                  ``N.FAX_REASON`` (their raw value is that number, so the rejects report shows it).

    Why validate here: deciding how many rows a file row becomes needs to know which values are valid and which
    are the same number written differently. The values are normalized again in :func:`convert` (same context
    and overrides, so the same result); that keeps the main loop identical for every field.
    """
    fm = mapping.get(f.name)
    fixes = overrides.get(f.name, {})
    # Per file row: the non-empty cleaned values of the source columns, each cell split into one value per number
    # (phone fields only), as (value, labelled fax) pairs. Empty list for blank rows.
    per_row: list[list[tuple[str, bool]]] = []
    # 'Phone + area code column': which sources hold area codes, and which area code column each phone column uses
    # (decided once for the file; mapping.area_code_pairs).
    areas, area_of = area_code_pairs(fm.sources, records) if fm.transform == "area_code" else ([], {})
    for k, r in enumerate(records):
        vals: list[tuple[str, bool]] = []
        if not blank[k]:
            cells = [N.clean_text(r.get(c, "")) for c in fm.sources]
            # Each 7-digit number (also one of several in a cell) gets the area code of its row in its area code
            # column (N.with_area_code). An area code cell is no phone number, unless a whole one (7+ digits) was
            # typed into it.
            codes = {a: N.area_code_cell(cells[a]) for a in areas}
            for i, v in enumerate(cells):
                if not v or (i in codes and (codes[i] or sum(c.isdigit() for c in v) < 7)):
                    continue
                area = codes.get(area_of[i], "") if area_of.get(i) is not None else ""
                pieces = N.split_phones(v, f, fm.params) if f.type == "phone" else [(v, False)]
                vals += [(N.with_area_code(p, area), fax) for p, fax in pieces] if area else pieces
        per_row.append(vals)
    # Evidence for phone repairs (the file's area codes and known numbers) comes from the split values, so numbers
    # from cells holding several count too.
    ctx = N.prepare(f, [v for vals in per_row for v, _ in vals], fm.params)
    if timezone:
        ctx["tz"] = timezone
    cache: dict[str, tuple[str, str | None]] = {}  # raw value -> (output, error), each value normalized once

    def check(v: str) -> tuple[str, str | None]:
        """Normalize one raw value (approved override first), caching the (output, error) result."""
        if v not in cache:
            cache[v] = (fixes[v], None) if v in fixes else N.normalize(v, f, fm.params, ctx)
        return cache[v]

    src: list[int] = []
    raw: list[str] = []
    early: dict[int, list[RowIssue]] = {}
    fax_only: set[int] = set()
    for k, vals in enumerate(per_row):
        good: dict[str, str] = {}  # formatted output → first raw value that produced it
        fax: dict[str, str] = {}  # the same, for valid numbers labelled fax
        bad: list[tuple[str, str]] = []  # (raw value, why it was skipped)
        for v, is_fax in vals:
            out, err = check(v)
            if err:
                bad.append((v, err))
            elif is_fax:
                fax.setdefault(out.lower(), v)
            elif out.lower() not in good:
                good[out.lower()] = v
        # Fax numbers are never imported: skipped when the row has another valid number (unless it is the same
        # number); a row with nothing else is rejected (in convert, via fax_only).
        if good:
            bad += [(v, N.FAX_REASON) for out, v in fax.items() if out not in good]
        elif fax:
            fax_only.add(len(src))
            src.append(k)
            raw.append(next(iter(fax.values())))
            continue
        # No valid number (or no number at all, or a blank row): keep a single working row with the first value so
        # that convert() rejects it with the normal reason ("too few digits", "required value is missing"…).
        if not good:
            src.append(k)
            raw.append(vals[0][0] if vals else "")
            continue
        first = len(src)
        for v in good.values():
            src.append(k)
            raw.append(v)
        # Invalid values (and fax numbers) next to at least one valid number are skipped, not rejected: the row
        # imports with its valid number(s) and the skipped ones are counted as "skipped" in the summary.
        if bad:
            early[first] = [RowIssue(f.name, v, f"extra value skipped — {why}", rejected=False, skipped=True)
                            for v, why in bad]
    return src, raw, early, ctx, fax_only


def source_rows(source: pd.DataFrame, audit: pd.DataFrame) -> pd.DataFrame:
    """The source rows lined up with the audit rows (one per converted row).

    Needed when 'One row per phone number' made several rows from one file row. Safe to call more than once.

    source: the file as read (one row per file row), e.g. the import's saved ``source.parquet``.
    audit: ``ConversionResult.audit`` for that file.

    Returns a DataFrame where row ``i`` is the file row that audit row ``i`` came from (file rows are repeated for
    expanded phone rows), with a fresh 0..n-1 index so ``source.at[i, col]`` and ``audit.loc[i]`` refer to the same
    working row. Used by the side-by-side comparison, the rule checks and the sample sent to Claude.

    Returned unchanged when the audit has no ``_src`` column (an audit saved before that column existed) or when
    the lengths already match. Expansion only ever adds rows, so equal lengths mean "already lined up" - which is
    also why a second call is a no-op.
    """
    if "_src" not in audit.columns or len(source) == len(audit):
        return source
    return source.iloc[audit["_src"].astype(int).to_numpy()].reset_index(drop=True)


def _reason_key(reason: str) -> str:
    """Reason text for the summary counts: without the per-value details (numbers, 'could be …' options).

    Drops any " — could be …" suggestion appended by the phone repair logic, and replaces runs of 3+ digits
    (area codes, exchanges, long numbers) with "…", so e.g. "not a real US number (area code 999 isn't in use)"
    and the same reason for area code 998 are counted as one line. Short numbers such as "too few digits (5)"
    are kept because they are useful and few.
    """
    reason = reason.split(" — could be ")[0]
    return re.sub(r"\d{3,}", "…", reason)


def _fmt_issue(schema: TargetSchema, x: RowIssue) -> str:
    """Format one issue for the rejects report's Reason column: "Header: reason ('value')".

    The value is truncated to 40 characters to keep the column readable; it is omitted for a missing value.
    """
    header = schema.field(x.field).header
    return f"{header}: {x.reason}" + (f" ('{x.value[:40]}')" if x.value else "")


def count_multi_number_cells(df: pd.DataFrame, field, columns: list[str], params: dict | None = None) -> int:
    """How many cells of these phone columns hold several numbers (each number later gets its own row).

    For the mapping page's "one row per number" note. Only cells with 14 or more digits can hold two numbers, so
    the split (``normalizers.split_phones``) only runs on those; a 100,000-row column takes well under a second.
    Columns missing from ``df`` are ignored. Example: a "Phone" column with "516-555-0100 / 917-555-1234" in two
    rows and single numbers elsewhere → 2.
    """
    n = 0
    for c in columns:
        if c not in df.columns:
            continue
        col = df[c].fillna("").astype(str)
        candidates = col[col.str.count(r"\d") >= 14]
        n += sum(1 for v in candidates if len(N.split_phones(N.clean_text(v), field, params or {})) > 1)
    return n


def preview_values(df: pd.DataFrame, schema: TargetSchema, mapping: Mapping, limit: int = 3,
                   timezone: str | None = None) -> dict[str, str]:
    """Return a short sample of converted output per field for the mapping review screen.

    Runs a full :func:`convert` on the first 200 rows (fast enough to redo after every mapping edit, and enough
    rows for the normalizers' column context to be meaningful), then for each schema field returns a display
    string keyed by field name:

    * fixed-blank fields: "(always blank)";
    * unmapped fields with no default: "" (nothing to show);
    * otherwise up to ``limit`` non-empty output values from the imported rows, joined with " | ", followed by a
      warning with the number of distinct unconvertible values in those rows and one example (truncated).

    No overrides are applied here, so the preview shows what the mapping alone would produce.
    """
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
        if opt_out_problem(f, fm):
            # Consent before anyone chose what its "no" means: its false values would neither surely be written
            # nor surely be left blank, so no sample is shown.
            out[f.name] = "choose below what a no means"
            continue
        # Samples come from the output (accepted rows only), so a field shows nothing when every row was rejected.
        vals = [v for v in res.output[f.header].tolist() if v][:limit] if not res.output.empty else []
        bad = res.failed_values.get(f.name, [])
        text = " | ".join(vals)
        if bad:
            text += f"   ⚠ {len(bad)} unconvertible in first {len(head)} rows (e.g. '{bad[0][:30]}')"
        out[f.name] = text
    return out
