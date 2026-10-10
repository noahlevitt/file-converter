"""Write the converted file (in the exact target format) and the rejected-rows report.

Overview
--------
Pure functions that turn an ``engine.ConversionResult`` into bytes; the caller decides where to store them
(``jobs.run_import`` writes them into the import folder, from where the web app and the CLI hand them out).

* :func:`write_output` — the converted file, exactly as the target schema's ``output:`` section specifies:

  - **CSV** (``format: csv``): ``delimiter``, ``quote_all`` (quote every field vs. only when needed),
    ``line_terminator`` (e.g. "\\n" or "\\r\\n", written byte-for-byte), ``include_header`` and ``encoding``.
    Characters the encoding can't represent are replaced with "?" rather than failing the whole import.
  - **Excel** (``format: xlsx``): one sheet named ``sheet_name`` (cut to Excel's 31-character limit), optional
    header row, every column formatted as **Text** (number format "@") so values such as ZIP "07030", phone
    "0044…" or "+1…" stay exactly as converted — Excel would otherwise turn them into numbers and drop leading
    zeros / plus signs, including when someone edits and re-saves the file. Column widths fit the content.

* :func:`write_rejects` — rejected and duplicate rows with reasons, as UTF-8 CSV with a BOM (opens correctly in
  Excel).
* :func:`write_summary` — the human-readable ``summary.txt``.
* :func:`output_filename` / :func:`rejects_filename` / :func:`timestamp` — file names, e.g.
  ``guests_converted_20261010-1342.csv`` and ``guests_REJECTED_3rows_20261010-1342.csv``.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from pathlib import Path

import pandas as pd

from .engine import ConversionResult
from .schema import TargetSchema


def _stem(source_filename: str) -> str:
    """Make a safe, short base name from the uploaded file's name (for output/rejects file names).

    Keeps letters, digits, "-" and "_"; every other character (spaces, brackets, dots, …) becomes "_" (accented
    letters such as "é" count as letters and are kept). Truncated to 60 characters; an empty result becomes "file".

    Example: ``"Guest List (Oct).csv"`` -> ``"Guest_List__Oct_"``.
    """
    stem = Path(source_filename).stem
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in stem)[:60] or "file"


def timestamp() -> str:
    """Current server-local time as ``YYYYMMDD-HHMM`` (e.g. "20261010-1342"), used in result file names.

    Computed once per import and passed to the filename helpers so all of an import's files share the same stamp.
    """
    return datetime.now().strftime("%Y%m%d-%H%M")


def output_filename(schema: TargetSchema, source_filename: str, ts: str | None = None) -> str:
    """Name of the converted file: ``<stem>_<filename_suffix>_<timestamp>.<csv|xlsx>``.

    Example: ``output_filename(schema, "POS export.csv", "20261010-1342")`` ->
    ``"POS_export_converted_20261010-1342.csv"`` (``filename_suffix`` defaults to "converted").
    """
    ext = "xlsx" if schema.output.format == "xlsx" else "csv"
    return f"{_stem(source_filename)}_{schema.output.filename_suffix}_{ts or timestamp()}.{ext}"


def rejects_filename(source_filename: str, n: int, ts: str | None = None) -> str:
    """Download name of the rejected-rows report: ``<stem>_REJECTED_<n>rows_<timestamp>.csv``.

    ``n`` is the number of rows in the report (bad rows plus removed duplicates), so the count is visible before
    the file is opened. Example: ``"guests_REJECTED_3rows_20261010-1342.csv"``.
    """
    return f"{_stem(source_filename)}_REJECTED_{n}rows_{ts or timestamp()}.csv"


def write_output(df: pd.DataFrame, schema: TargetSchema) -> bytes:
    """Serialize the converted rows (``ConversionResult.output``) in the schema's output format; return bytes.

    ``df`` columns are the schema headers in order and every value is already a final string, so nothing here
    changes values — only the file format. See the module docstring for the CSV/Excel options.
    """
    spec = schema.output
    if spec.format == "xlsx":
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
            # Excel limits sheet names to 31 characters.
            df.to_excel(writer, sheet_name=spec.sheet_name[:31], index=False, header=spec.include_header)
            ws = writer.sheets[spec.sheet_name[:31]]
            text_fmt = writer.book.add_format({"num_format": "@"})  # keep leading zeros / '+' signs as text
            for i, col in enumerate(df.columns):
                # Width = longest of the header and the first 500 values, + 2 padding, capped at 60 characters.
                width = min(60, max([len(str(col))] + [len(str(v)) for v in df[col].head(500)]) + 2)
                ws.set_column(i, i, width, text_fmt)
        return buf.getvalue()

    # CSV. newline="" stops StringIO from translating line endings, so spec.line_terminator is written exactly.
    buf = io.StringIO(newline="")
    writer = csv.writer(
        buf,
        delimiter=spec.delimiter,
        quoting=csv.QUOTE_ALL if spec.quote_all else csv.QUOTE_MINIMAL,
        lineterminator=spec.line_terminator,
    )
    if spec.include_header:
        writer.writerow(list(df.columns))
    writer.writerows(df.itertuples(index=False, name=None))
    # errors="replace": a character the target encoding can't hold becomes "?" instead of aborting the import.
    return buf.getvalue().encode(spec.encoding, errors="replace")


def write_rejects(result: ConversionResult) -> bytes:
    """Serialize ``result.rejects`` (rejected + duplicate rows with status, file row, reasons and original values).

    Always plain CSV, independent of the target format, because this report is for people to read and fix in
    Excel, not for importing.
    """
    # utf-8-sig so Excel opens it with the right encoding.
    return result.rejects.to_csv(index=False).encode("utf-8-sig")


def write_summary(result: ConversionResult, schema: TargetSchema, source_filename: str, user: str,
                  app_version: str) -> bytes:
    """Build ``summary.txt``: who converted what, the counts and the reasons; return UTF-8 bytes.

    Layout: a header block (generation time, tool version, source file, target schema, user), the one-line
    result message, the row counts, then optional sections, each sorted by frequency:

    * "Why rows were rejected" — reason counts of bad rows;
    * "Repaired from likely typos" — e.g. a phone area code one digit off, imported but worth checking;
    * "Extra numbers skipped" — invalid 2nd/3rd phone numbers that didn't get their own row;
    * "Optional values left blank" — invalid optional values that were blanked.

    When 'One row per phone number' added rows, an indented "of which extra rows …" line is inserted right after
    "Rows converted" and a count of skipped extra numbers is appended.
    """
    lines = [
        "IMPORT CONVERSION SUMMARY",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}   Tool version: {app_version}",
        f"Source file: {source_filename}",
        f"Target schema: {schema.label}",
        f"Converted by: {user}",
        "",
        result.flag_message(),
        "",
        f"Rows in file (excluding empty rows): {result.total_rows:,}",
        f"Rows converted:                      {result.imported:,}",
        f"Bad rows (not imported):             {result.rejected:,}",
        f"Duplicate rows removed:              {result.duplicates:,}",
        f"Empty rows skipped:                  {result.blank_rows:,}",
        f"Invalid optional values blanked:     {result.blanked_values:,}",
    ]
    if result.extra_rows or result.skipped_values:
        # Find the "Rows converted" line by its exact text and put the breakdown directly under it.
        lines.insert(lines.index(f"Rows converted:                      {result.imported:,}") + 1,
                     f"  of which extra rows for 2nd/3rd… phone numbers: {result.extra_rows:,}")
        lines.append(f"Invalid extra phone numbers skipped: {result.skipped_values:,}")
    if result.repaired_values:
        lines.append(f"Phone numbers repaired from typos:   {result.repaired_values:,}  (check these)")
    if result.reason_counts:
        lines += ["", "Why rows were rejected:"]
        lines += [f"  {count:>6,}  {reason}" for reason, count in result.reason_counts.most_common()]
    if result.repaired_counts:
        lines += ["", "Repaired from likely typos (imported — please check):"]
        lines += [f"  {count:>6,}  {reason}" for reason, count in result.repaired_counts.most_common()]
    if result.skipped_counts:
        lines += ["", "Extra numbers skipped (the row was still imported with its other number):"]
        lines += [f"  {count:>6,}  {reason}" for reason, count in result.skipped_counts.most_common()]
    if result.blanked_counts:
        lines += ["", "Optional values left blank:"]
        lines += [f"  {count:>6,}  {reason}" for reason, count in result.blanked_counts.most_common()]
    return ("\n".join(lines) + "\n").encode("utf-8")
