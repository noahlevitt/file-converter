"""Write the converted file (in the exact target format) and the rejected-rows report."""

from __future__ import annotations

import csv
import io
from datetime import datetime
from pathlib import Path

import pandas as pd

from .engine import ConversionResult
from .schema import TargetSchema


def _stem(source_filename: str) -> str:
    stem = Path(source_filename).stem
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in stem)[:60] or "file"


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M")


def output_filename(schema: TargetSchema, source_filename: str, ts: str | None = None) -> str:
    ext = "xlsx" if schema.output.format == "xlsx" else "csv"
    return f"{_stem(source_filename)}_{schema.output.filename_suffix}_{ts or timestamp()}.{ext}"


def rejects_filename(source_filename: str, n: int, ts: str | None = None) -> str:
    return f"{_stem(source_filename)}_REJECTED_{n}rows_{ts or timestamp()}.csv"


def write_output(df: pd.DataFrame, schema: TargetSchema) -> bytes:
    spec = schema.output
    if spec.format == "xlsx":
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
            df.to_excel(writer, sheet_name=spec.sheet_name[:31], index=False, header=spec.include_header)
            ws = writer.sheets[spec.sheet_name[:31]]
            text_fmt = writer.book.add_format({"num_format": "@"})  # keep leading zeros / '+' signs as text
            for i, col in enumerate(df.columns):
                width = min(60, max([len(str(col))] + [len(str(v)) for v in df[col].head(500)]) + 2)
                ws.set_column(i, i, width, text_fmt)
        return buf.getvalue()

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
    return buf.getvalue().encode(spec.encoding, errors="replace")


def write_rejects(result: ConversionResult) -> bytes:
    # utf-8-sig so Excel opens it with the right encoding.
    return result.rejects.to_csv(index=False).encode("utf-8-sig")


def write_summary(result: ConversionResult, schema: TargetSchema, source_filename: str, user: str,
                  app_version: str) -> bytes:
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
        lines.insert(lines.index(f"Rows converted:                      {result.imported:,}") + 1,
                     f"  of which extra rows for 2nd/3rd… phone numbers: {result.extra_rows:,}")
        lines.append(f"Invalid extra phone numbers skipped: {result.skipped_values:,}")
    if result.reason_counts:
        lines += ["", "Why rows were rejected:"]
        lines += [f"  {count:>6,}  {reason}" for reason, count in result.reason_counts.most_common()]
    if result.skipped_counts:
        lines += ["", "Extra numbers skipped (the row was still imported with its other number):"]
        lines += [f"  {count:>6,}  {reason}" for reason, count in result.skipped_counts.most_common()]
    if result.blanked_counts:
        lines += ["", "Optional values left blank:"]
        lines += [f"  {count:>6,}  {reason}" for reason, count in result.blanked_counts.most_common()]
    return ("\n".join(lines) + "\n").encode("utf-8")
