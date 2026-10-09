"""File ingest: CSV/TSV/TXT and Excel (xlsx, xlsm, xls, ods).

Everything is read as text so nothing is silently coerced (ZIP leading zeros, phone numbers
stored as numbers, Excel date formatting). Header rows are auto-detected because exports often
have title or blank rows above the real header.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from charset_normalizer import from_bytes

TEXT_EXTENSIONS = {".csv", ".tsv", ".txt", ".tab"}
EXCEL_EXTENSIONS = {".xlsx", ".xlsm", ".xls", ".ods", ".xlsb"}
SUPPORTED_EXTENSIONS = sorted(TEXT_EXTENSIONS | EXCEL_EXTENSIONS)

HEADER_SCAN_ROWS = 30

_NUMERIC_RE = re.compile(r"^[\s$€£+\-()]*[\d.,\s]+%?$")
_DATEISH_RE = re.compile(r"^\d{1,4}[/\-.]\d{1,2}[/\-.]\d{1,4}")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ReadError(Exception):
    """Raised when a file cannot be read."""


@dataclass
class ReadResult:
    df: pd.DataFrame
    sheet_names: list[str] = field(default_factory=list)
    sheet: str | None = None
    header_row: int | None = 0  # 0-based index in the raw grid; None = file has no header row
    detected_header_row: int | None = 0
    encoding: str | None = None
    delimiter: str | None = None
    raw_preview: list[list[str]] = field(default_factory=list)  # first rows of the raw grid
    warnings: list[str] = field(default_factory=list)

    def excel_row_number(self, data_index: int) -> int:
        """1-based row number in the original file for a 0-based data row index."""
        offset = 1 if self.header_row is None else self.header_row + 2
        return data_index + offset


def _decode(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace"), "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace"), "utf-16"
    # utf-8 first, then Windows-1252 (what Excel "Save as CSV" produces on US Windows machines).
    for enc in ("utf-8", "cp1252"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            pass
    best = from_bytes(data[:2_000_000]).best()
    enc = best.encoding if best else "cp1252"
    return data.decode(enc, errors="replace"), enc


def _sniff_delimiter(text: str, ext: str) -> str:
    if ext in {".tsv", ".tab"}:
        return "\t"
    sample = text[:65536]
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        lines = [ln for ln in sample.splitlines()[:20] if ln.strip()]
        counts = {d: sum(ln.count(d) for ln in lines) for d in [",", "\t", ";", "|"]}
        return max(counts, key=counts.get) if any(counts.values()) else ","


def _read_text_grid(data: bytes, ext: str) -> tuple[list[list[str]], str, str]:
    text, encoding = _decode(data)
    delimiter = _sniff_delimiter(text, ext)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    grid = [[c if c is not None else "" for c in row] for row in reader]
    return grid, encoding, delimiter


def list_sheets(data: bytes, ext: str) -> list[str]:
    try:
        xl = pd.ExcelFile(io.BytesIO(data), engine="calamine")
        return [str(s) for s in xl.sheet_names]
    except Exception as exc:  # pragma: no cover - library error surface
        raise ReadError(f"Could not open spreadsheet: {exc}") from exc


def _read_excel_grid(data: bytes, sheet: str | None) -> list[list[str]]:
    try:
        df = pd.read_excel(
            io.BytesIO(data),
            sheet_name=sheet if sheet is not None else 0,
            header=None,
            dtype=str,
            engine="calamine",
            keep_default_na=False,
            na_filter=False,
        )
    except Exception as exc:
        raise ReadError(f"Could not read spreadsheet: {exc}") from exc
    return [["" if v is None else str(v) for v in row] for row in df.values.tolist()]


def _clean_cell(value: str) -> str:
    value = value.replace(" ", " ").replace("﻿", "")
    return value.strip()


def _looks_like_data(cell: str) -> bool:
    return bool(_NUMERIC_RE.match(cell) or _DATEISH_RE.match(cell) or _EMAIL_RE.match(cell))


def detect_header_row(grid: list[list[str]]) -> int | None:
    """Return the 0-based index of the header row, or None if the file has no header row."""
    rows = grid[:HEADER_SCAN_ROWS]
    if not rows:
        return 0
    width = max((sum(1 for c in r if c) for r in rows), default=0)
    if width == 0:
        return 0
    min_cells = max(2, -(-width // 2)) if width > 1 else 1  # at least half the columns (and 2+)
    for i, row in enumerate(rows):
        cells = [c for c in row if c]
        if len(cells) < min_cells:
            continue
        textlike = sum(1 for c in cells if not _looks_like_data(c) and len(c) <= 80) / len(cells)
        unique = len({c.lower() for c in cells}) / len(cells)
        has_following_data = any(any(c for c in r) for r in rows[i + 1 : i + 4]) or i == len(rows) - 1
        if textlike >= 0.7 and unique >= 0.5 and has_following_data:
            return i
        if textlike < 0.5:
            # First substantial row already looks like data → no header row.
            return None
    return 0


def _unique_headers(raw: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for i, h in enumerate(raw):
        name = re.sub(r"\s+", " ", h).strip() or f"Column {i + 1}"
        key = name.lower()
        if key in seen:
            seen[key] += 1
            name = f"{name} ({seen[key]})"
        else:
            seen[key] = 1
        out.append(name)
    return out


def build_frame(grid: list[list[str]], header_row: int | None) -> pd.DataFrame:
    grid = [[_clean_cell(c) for c in row] for row in grid]
    width = max((len(r) for r in grid), default=0)
    grid = [r + [""] * (width - len(r)) for r in grid]

    if header_row is None:
        headers = [f"Column {i + 1}" for i in range(width)]
        body = grid
    else:
        headers = grid[header_row] if header_row < len(grid) else [""] * width
        body = grid[header_row + 1 :]

    # Drop columns that have no header and no data.
    keep = [i for i in range(width) if headers[i] or any(r[i] for r in body)]
    headers = _unique_headers([headers[i] for i in keep])
    body = [[r[i] for i in keep] for r in body]

    # Drop trailing fully-empty rows (keep interior blank rows so row numbers stay true).
    while body and not any(body[-1]):
        body.pop()

    return pd.DataFrame(body, columns=headers, dtype=str)


def read_file(
    filename: str,
    data: bytes,
    sheet: str | None = None,
    header_row: int | None | str = "auto",
) -> ReadResult:
    """Read an uploaded file into a text-only DataFrame.

    header_row: "auto" to detect, an int (0-based raw row), or None for "no header row".
    """
    ext = Path(filename).suffix.lower()
    if ext not in TEXT_EXTENSIONS | EXCEL_EXTENSIONS:
        raise ReadError(f"Unsupported file type '{ext}'. Supported: {', '.join(SUPPORTED_EXTENSIONS)}")
    if not data:
        raise ReadError("The file is empty.")

    result_kwargs: dict = {}
    if ext in TEXT_EXTENSIONS:
        grid, encoding, delimiter = _read_text_grid(data, ext)
        result_kwargs.update(encoding=encoding, delimiter=delimiter)
        sheets: list[str] = []
        chosen_sheet = None
    else:
        sheets = list_sheets(data, ext)
        chosen_sheet = sheet if sheet in sheets else (sheets[0] if sheets else None)
        grid = _read_excel_grid(data, chosen_sheet)

    cleaned = [[_clean_cell(c) for c in row] for row in grid]
    if not any(any(r) for r in cleaned):
        raise ReadError("No data found in the file" + (f" (sheet '{chosen_sheet}')" if chosen_sheet else "") + ".")

    detected = detect_header_row(cleaned)
    hr = detected if header_row == "auto" else header_row
    if isinstance(hr, int) and hr >= len(cleaned):
        raise ReadError(f"Header row {hr + 1} is past the end of the file ({len(cleaned)} rows).")

    df = build_frame(cleaned, hr)
    warnings = []
    if isinstance(hr, int) and ext in TEXT_EXTENSIONS:
        header_width = len([c for c in cleaned[hr] if c]) if hr < len(cleaned) else 0
        ragged = sum(
            1 for r in cleaned[hr + 1 :]
            if any(r) and len(r) > header_width and any(c for c in r[header_width:])
        )
        if ragged:
            warnings.append(
                f"{ragged} row(s) have more values than there are header columns — usually an unquoted comma "
                "inside a value (e.g. Smith, John). Check those rows in the rejects file."
            )
    if hr is None:
        warnings.append("No header row detected — columns are named Column 1, Column 2, …")
    elif hr > 0:
        warnings.append(f"Header found on row {hr + 1}; {hr} row(s) above it were skipped.")
    if df.empty:
        warnings.append("The file has a header but no data rows.")

    return ReadResult(
        df=df,
        sheet_names=sheets,
        sheet=chosen_sheet,
        header_row=hr,
        detected_header_row=detected,
        raw_preview=cleaned[:15],
        warnings=warnings,
        **result_kwargs,
    )
