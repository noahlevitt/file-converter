"""File ingest: CSV/TSV/TXT and Excel (xlsx, xlsm, xls, ods).

Everything is read as text so nothing is silently coerced (ZIP leading zeros, phone numbers
stored as numbers, Excel date formatting). Header rows are auto-detected because exports often
have title or blank rows above the real header.

Overview
--------
Responsibilities
    * Text files: detect the encoding (:func:`_decode`) and the delimiter (:func:`_sniff_delimiter`), then
      parse with the ``csv`` module (:func:`_read_text_grid`).
    * Spreadsheets: list the sheets (:func:`list_sheets`) and read the chosen one as text
      (:func:`_read_excel_grid`, calamine engine).
    * Find the header row in the raw grid (:func:`detect_header`: titles above it skipped, files without one
      recognised, unsure cases flagged), or use the row the reviewer picked.
    * Build a clean, text-only DataFrame with unique column names (:func:`build_frame`).
    * Report what was detected and anything suspicious (:class:`ReadResult`: encoding, delimiter, sheet,
      header row, warnings, a raw preview for choosing the header row by hand).

Main entry point
    :func:`read_file` - used by the web app, the CLI and the background import jobs. :func:`detect_header`,
    :func:`detect_header_row` and :func:`build_frame` are also usable on their own.

Data flow
    upload bytes -> raw grid (list of rows of strings) -> cell cleaning -> header-row detection ->
    :func:`build_frame` -> ``ReadResult.df`` -> ``mapping.profile_columns`` / ``engine.convert``.

Example::

    rr = read_file("guests.csv", data)       # data = the uploaded bytes
    rr.df.columns   # ["Guest Name", "Phone", ...]
    rr.header_row   # 3 -> the header is on file row 4; rows 1-3 were a title and blank lines
    rr.encoding, rr.delimiter   # "cp1252", ","
"""

from __future__ import annotations

import csv
import io
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from charset_normalizer import from_bytes

from .normalizers import clean_text, looks_like_date, looks_like_email, looks_like_phone
from .sheetsize import SheetTooBig, check_sheet_size

# Delimited text files (the delimiter is sniffed, except .tsv/.tab which are always tab-separated).
TEXT_EXTENSIONS = {".csv", ".tsv", ".txt", ".tab"}
# Spreadsheet formats, all read with the calamine engine (one engine for old and new Excel and OpenDocument).
EXCEL_EXTENSIONS = {".xlsx", ".xlsm", ".xls", ".ods", ".xlsb"}
# Sorted list for error messages and the upload widget.
SUPPORTED_EXTENSIONS = sorted(TEXT_EXTENSIONS | EXCEL_EXTENSIONS)

# Only the first 30 raw rows are searched for the header: title/blank rows above a header are a handful at most,
# and scanning the whole file would be slow and could pick a text-heavy data row further down.
HEADER_SCAN_ROWS = 30

# Cells that look like DATA rather than a header label (see _looks_like_data):
#   numbers, amounts and percentages: "42", "1,234.50", "$ 12.00", "(12)", "15%". Bare-digit phones such as
#   "2015550111" count as numbers; formatted ones like "(201) 555-0111" don't match and count as text.
_NUMERIC_RE = re.compile(r"^[\s$€£+\-()]*[\d.,\s]+%?$")
#   values starting like a date: "10/01/2026", "2026-10-01", "1.2.26"
_DATEISH_RE = re.compile(r"^\d{1,4}[/\-.]\d{1,2}[/\-.]\d{1,4}")
#   email addresses: "ana@example.com"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ReadError(Exception):
    """Raised when a file cannot be read.

    The message is meant for the reviewer and is shown as-is in the web app / CLI, e.g. "The file is empty." or
    "Unsupported file type '.pdf'. Supported: .csv, .ods, ...".
    """


@dataclass
class ReadResult:
    """Everything :func:`read_file` found out about an uploaded file.

    ``df`` is the data: one column per source column (unique, cleaned header names), one row per data row, every
    cell a string ("" for empty). The other attributes describe how the file was read, so the UI can show it and
    let the reviewer correct the sheet or header row.
    """

    # The data, all text (dtype str): what profiling, mapping and conversion work on.
    df: pd.DataFrame
    # Spreadsheets: all sheet names in file order (empty for text files).
    sheet_names: list[str] = field(default_factory=list)
    # Spreadsheets: the sheet that was read (None for text files).
    sheet: str | None = None
    header_row: int | None = 0  # 0-based index in the raw grid; None = file has no header row
    # What auto-detection found, kept even when the caller chose another header row (or "no header").
    detected_header_row: int | None = 0
    # False when detection had to fall back to a guess (see detect_header); a warning then asks the reviewer to check.
    header_sure: bool = True
    # Text files: the encoding used to decode the bytes ("utf-8", "utf-8-sig", "cp1252", ...).
    encoding: str | None = None
    # Text files: the column separator ("," ";" "\t" "|").
    delimiter: str | None = None
    raw_preview: list[list[str]] = field(default_factory=list)  # first rows of the raw grid
    # (up to 15 cleaned raw rows, including any rows above the header, for picking the header row by hand)
    # Messages for the reviewer: skipped title rows, no header row, rows with extra values, no data rows.
    warnings: list[str] = field(default_factory=list)

    def excel_row_number(self, data_index: int) -> int:
        """1-based row number in the original file for a 0-based data row index.

        Lets error and reject reports point at the row the user sees in Excel / a text editor. Data starts right
        after the header row (interior blank rows are kept by :func:`build_frame`, so the count stays true).

        Examples: header on the first row (``header_row=0``): data index 0 -> row 2. Header on row 4
        (``header_row=3``): data index 0 -> row 5. No header (``header_row=None``): data index 0 -> row 1.
        """
        offset = 1 if self.header_row is None else self.header_row + 2
        return data_index + offset


def _decode(data: bytes) -> tuple[str, str]:
    """Decode the bytes of a text file; returns (text, encoding name).

    Order of attempts:

    1. A byte-order mark decides: UTF-8 BOM -> "utf-8-sig" (the BOM is removed), UTF-16 BOM -> "utf-16".
    2. Strict UTF-8 - the most common encoding, and it practically never decodes non-UTF-8 text by accident.
    3. Strict Windows-1252 - Excel's "Save as CSV" on US Windows ("José" stored as one byte per letter).
    4. charset_normalizer's best guess on the first 2 MB (enough to decide, and fast on huge files), decoded
       with errors replaced; Windows-1252 if it has no guess.

    Example: ``_decode("José".encode("cp1252")) -> ("José", "cp1252")``.
    """
    # Byte-order marks: unambiguous, so they win over any guessing.
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
    # Rare: Windows-1252 leaves only 5 byte values undefined, so we only get here for unusual data.
    best = from_bytes(data[:2_000_000]).best()
    enc = best.encoding if best else "cp1252"
    return data.decode(enc, errors="replace"), enc


def _sniff_delimiter(text: str, ext: str) -> str:
    """Guess the column separator of a text file: comma, semicolon, tab or pipe.

    * ``.tsv`` / ``.tab`` files are always tab-separated.
    * Otherwise ``csv.Sniffer`` looks at the first 64 KB (handles quoting, e.g. ``"Smith, John";2015550111``).
    * If the sniffer can't decide, the candidate that occurs most often in the first 20 non-blank lines wins
      (European exports often use ";" because "," is their decimal mark).
    * A single-column file with none of them falls back to ",".

    Examples: ``"Name;Phone\\nAna;201..."`` -> ``";"``; any text in a ``.tsv`` file -> ``"\\t"``.
    """
    if ext in {".tsv", ".tab"}:
        return "\t"
    sample = text[:65536]
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        # Fallback: count each candidate in the first 20 non-blank lines.
        lines = [ln for ln in sample.splitlines()[:20] if ln.strip()]
        counts = {d: sum(ln.count(d) for ln in lines) for d in [",", "\t", ";", "|"]}
        return max(counts, key=counts.get) if any(counts.values()) else ","


def _read_text_grid(data: bytes, ext: str) -> tuple[list[list[str]], str, str]:
    """Parse a delimited text file into a raw grid; returns (rows of cell strings, encoding, delimiter).

    Rows may have different lengths (ragged files); :func:`build_frame` pads them later. ``newline=""`` lets the
    csv module keep line breaks that are inside quoted cells (e.g. a multi-line address) in one cell.
    """
    text, encoding = _decode(data)
    delimiter = _sniff_delimiter(text, ext)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    grid = [[c if c is not None else "" for c in row] for row in reader]
    return grid, encoding, delimiter


def list_sheets(data: bytes, ext: str) -> list[str]:
    """Names of all sheets in a spreadsheet, in file order (e.g. ["Summary", "Members"]).

    ``ext`` is accepted for symmetry with the text reader; calamine detects the format itself. Raises
    :class:`ReadError` when the file can't be opened (corrupt, password-protected, not really a spreadsheet).
    """
    try:
        xl = pd.ExcelFile(io.BytesIO(data), engine="calamine")
        return [str(s) for s in xl.sheet_names]
    except Exception as exc:  # pragma: no cover - library error surface
        raise ReadError(f"Could not open spreadsheet: {exc}") from exc


def _read_excel_grid(data: bytes, sheet: str | None) -> list[list[str]]:
    """Read one sheet (``None`` = the first) as a raw grid of strings, without interpreting any row as header.

    ``header=None`` because the header row is detected afterwards like for text files; ``dtype=str`` plus
    ``keep_default_na=False`` / ``na_filter=False`` keep every cell as text, so "NA" or "null" stay literal and
    empty cells become "" instead of NaN. Raises :class:`ReadError` when the sheet can't be read.
    """
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
    # Plain lists of strings, same shape as the text reader's grid.
    return [["" if v is None else str(v) for v in row] for row in df.values.tolist()]


def _clean_cell(value: str) -> str:
    """Normalise one raw cell: no non-breaking spaces, no stray byte-order marks, no surrounding whitespace.

    Non-breaking spaces (U+00A0) become normal spaces and byte-order marks (U+FEFF) are removed, then the value
    is stripped. Exports from web tools and Excel often contain non-breaking spaces, and a BOM can survive inside
    a file that was put together from several exports; both would otherwise make "Phone" and "Phone " different
    headers or hide an empty cell. Example: ``"  Ana\\u00a0Lee "`` -> ``"Ana Lee"``.
    """
    value = value.replace(" ", " ").replace("﻿", "")
    return value.strip()


def _looks_like_data(cell: str) -> bool:
    """True when a cell looks like a data value (number / amount / phone, date, email) rather than a header label.

    Examples: "2015550111", "$12.50", "10/01/2026", "ana@example.com" -> True; "Phone", "Last Visit" -> False.
    Formatted phones such as "(201) 555-0111" don't match any pattern and count as text. Only used by the fallback
    of :func:`detect_header` (files where no column holds phones, emails or dates); the main rule uses
    :func:`_cell_kind`.
    """
    return bool(_NUMERIC_RE.match(cell) or _DATEISH_RE.match(cell) or _EMAIL_RE.match(cell))


# Header detection compares each row with the rest of its columns, learned from this many rows at the top of the
# file (enough to know what a column holds; few enough to stay fast on huge files).
KIND_SAMPLE_ROWS = 100
# Kinds of value that never appear as a column label: a header row doesn't hold phone numbers, emails or dates.
_STRONG_KINDS = {"phone", "email", "date"}
# Kinds a data column can have, as opposed to free text.
_TYPED_KINDS = _STRONG_KINDS | {"number", "flag"}
# Kinds no header label ever is: a row holding one of them is never a header candidate, whatever its column.
_NEVER_IN_HEADER = {"phone", "email"}
# Yes/no style values ("flag" kind).
_FLAG_WORDS = {"y", "n", "yes", "no", "true", "false", "t", "f", "x"}


def _cell_kind(cell: str) -> str | None:
    """What kind of value a cell holds, for header detection. None for an empty cell.

    "email" (ana@x.com), "phone" (a valid number in any format, or 7-15 digits outweighing the letters: typos and
    7-digit numbers are still phone-shaped, a date range's 16 digits are not), "date" (any date the date parser
    reads, including "03/15"),
    "number" (amounts, counts, ZIPs, years), "flag" (Y/N, true/false) or "text" (everything else - names, labels).
        "516-555-0100", "(516) 555-0100", "555-1212" -> "phone";   "10/01/2026 3:15 PM", "Sep 12" -> "date"
        "Phone", "Last Visit", "Visits 2024", "Ana Lee" -> "text";  "$12.50", "2024" -> "number";  "Y" -> "flag"
    """
    if not clean_text(cell):  # empty, or a stand-in for empty such as "N/A", "none", "-"
        return None
    if looks_like_email(cell):
        return "email"
    if looks_like_phone(cell):
        return "phone"
    if looks_like_date(cell):
        return "date"
    digits = sum(ch.isdigit() for ch in cell)
    letters = sum(ch.isalpha() for ch in cell)
    if 7 <= digits <= 15 and digits >= 2 * letters:
        return "phone"
    if digits and digits >= letters:
        return "number"
    if cell.lower() in _FLAG_WORDS:
        return "flag"
    return "text"


def _usual_kind(below: Counter, rows_below: int) -> str | None:
    """The kind most of a column's cells BELOW a row hold, or None when no kind has 60% or there is too little to go on.

    ``below`` counts the kinds of the column's non-empty cells under the row (in the sample); ``rows_below`` is the
    number of non-empty rows under it. At least two cells are needed (one when only one row follows, so a file
    with a header and a single customer still works): a lone value in a column that only a report line uses
    ("Kravit" next to "Store") says nothing about that column.
    """
    total = sum(below.values())
    if total < min(2, rows_below) or not total:
        return None
    kind, n = max(below.items(), key=lambda kv: kv[1])
    return kind if n / total >= 0.6 else None


def _mostly_typed(below: Counter, rows_below: int) -> bool:
    """Whether at least half of a column's cells below a row are typed values (phone, email, date, number, flag).

    Used for *contrast*: a label over such a column. Half (not the 60% of _usual_kind) so that a "Phone" label still
    counts over a short column where one customer typed "no phone". Same minimum support as _usual_kind, and the
    column must be filled in at least half the rows below.
    """
    total = sum(below.values())
    # The column must also be filled in at least half the rows below: a column that only a few shifted rows reach
    # ("Apt 2" pushed a phone one column right) says nothing about the label above it.
    if not total or total < min(2, rows_below) or total < 0.5 * rows_below:
        return False
    return sum(n for k, n in below.items() if k in _TYPED_KINDS) / total >= 0.5


def detect_header(grid: list[list[str]]) -> tuple[int | None, bool]:
    """Find the header row: (0-based index or None when the file has no header row, sure?).

    Exports often start with titles, report dates, store names or blank rows; some have no header at all. Header
    labels and customer values differ in a way cell shapes alone can't show ("516-555-0100" and "Name" are both
    "text" to a naive check), so each row is compared with the rest of its columns (cells already cleaned):

    1. Every cell gets a kind (:func:`_cell_kind`), and for each row every column gets the usual kind of the cells
       below that row (within the first KIND_SAMPLE_ROWS rows; :func:`_usual_kind`).
    2. A row's *contrast* = its text cells sitting over a typed column (a label such as "Phone" over phone numbers);
       its *consistency* = its phone/email/date cells matching the column below (a customer's phone over more
       phone numbers).
    3. Rows are read from the top (the first HEADER_SCAN_ROWS). The first customer row is the first row holding a
       valid phone number or email (in any column), or the first row as full as the file's usual row with more
       consistency than contrast. The header is the label row above it with
       the most contrast (ties: the lower one): at least one label over a typed column, more contrast than
       consistency, mostly text (60%+), mostly distinct, and no phone number or email anywhere in it. No such row
       above the first customer row means the file has no header row (None).
    So a title, a "Generated 10/12/2026" line or a store name above the header is skipped, and a row holding a
    phone number or email (in any column) or a date that fits its column is never used as the header. Returns
    sure=True.

    When no customer row is found in the scanned rows (e.g. no column holds phones, emails or dates, or the file
    is tiny), the best label row found is used if there is one (sure=True); otherwise the older shape rule decides
    and the answer is marked unsure (sure=False) so the reviewer is asked to check: the first row filled in at least
    half the columns that is 70%+ text and has no phone/email/date is the header, a first row holding one is data
    (None), and if nothing decides, row 0 is assumed.

    Examples::

        [["Customer export"], [], ["Name", "Phone"], ["Ana", "516-555-0100"], ["Ben", "917-555-1234"]] -> (2, True)
        [["Ana Lee", "(516) 555-0100"], ["Ben Cruz", "(917) 555-1234"]]                               -> (None, True)
        [["Store", "Kravit"], ["Name", "Phone", "Visit"], ["Ana", "516-555-0100", "10/01/2026"]]       -> (1, True)
    """
    rows = grid[:HEADER_SCAN_ROWS]
    if not rows:
        return 0, False
    sample = grid[:KIND_SAMPLE_ROWS]
    # Width = the usual number of filled cells per row (the upper median over the non-empty sample rows, so a wide
    # report line above a one-column list doesn't count); a "substantial" row fills at least half of it (and 2+
    # cells), so a one-cell title line never counts as the header or as the first customer row.
    filled_counts = sorted(n for n in (sum(1 for c in r if c) for r in sample) if n)
    if not filled_counts:
        return 0, False
    width = filled_counts[len(filled_counts) // 2]
    min_cells = max(2, -(-width // 2)) if width > 1 else 1
    kinds = [[_cell_kind(c) for c in r] for r in sample]
    # below[i][j] = kinds of column j's cells under row i; nonempty_below[i] = non-empty rows under row i. Built from
    # the bottom of the sample up (only for the rows that are scanned).
    ncols = max(len(r) for r in sample)
    below: list[list[Counter]] = [[] for _ in range(len(rows))]
    nonempty_below = [0] * len(rows)
    running = [Counter() for _ in range(ncols)]
    nonempty = 0
    for i in range(len(sample) - 1, -1, -1):
        if i < len(rows):
            below[i] = [Counter(c) for c in running]
            nonempty_below[i] = nonempty
        for j, k in enumerate(kinds[i]):
            if k:
                running[j][k] += 1
        nonempty += any(kinds[i])

    # Rows holding a valid phone number or email anywhere (in any column, so also when an unquoted comma has shifted
    # the row): customers or other data, never the header, and the header is never below them.
    contact = [any(c and (looks_like_phone(c) or looks_like_email(c)) for c in r) for r in rows]
    # The older width rule, for the shape-based fallback: half the widest row (and 2+ cells).
    widest = max((sum(1 for c in r if c) for r in rows), default=0)
    wide_min_cells = max(2, -(-widest // 2)) if widest > 1 else 1

    # The best label row so far, as (contrast - consistency, familiar column words, row index): more contrast wins,
    # then more words common in column names, then the lower row.
    best: tuple[int, int, int] | None = None

    def header_above(first_data: int) -> tuple[int | None, bool]:
        """The header for a file whose first customer row is ``first_data``: the best label row above it; failing
        that, the row above it that looks most like labels by shape (most familiar column words, then the lowest;
        unsure); failing that, no header row."""
        if best is not None:
            return best[2], True
        shaped = [i for i in range(first_data) if _labels_by_shape(rows[i], kinds[i], wide_min_cells)]
        if not shaped:
            return None, True
        return max(shaped, key=lambda i: (_header_words(rows[i]), i)), False

    for i in range(len(rows)):
        kr = kinds[i]
        filled = [k for k in kr if k]
        if not filled:
            continue
        if contact[i]:
            return header_above(i)
        if len(filled) < min_cells:
            continue
        contrast = consistency = 0
        for j, k in enumerate(kr):
            if not k:
                continue
            if k == "text" and _mostly_typed(below[i][j], nonempty_below[i]):
                contrast += 1
            elif k in _STRONG_KINDS and k == _usual_kind(below[i][j], nonempty_below[i]):
                consistency += 1
        if consistency > contrast and len(filled) >= width:
            # The first data row (dates matching their column, in a row as full as the file's usual row - a short
            # report line such as "Generated, 10/12/2026" over a birthday column doesn't count): header above it.
            return header_above(i)
        cells = [c for c in rows[i] if c]
        textlike = sum(1 for k in filled if k == "text") / len(filled)
        unique = len({c.lower() for c in cells}) / len(cells)
        # Column labels never hold anything phone- or email-shaped (not even a typo'd number such as "516-555-01").
        if contrast > consistency and textlike >= 0.6 and unique >= 0.5 and not _NEVER_IN_HEADER & set(filled):
            score = (contrast - consistency, _header_words(rows[i]), i)
            if best is None or score >= best:
                best = score
    if best is not None:
        return best[2], True
    # Fallback: no phone, email or matching date in the scanned rows (e.g. a file of names only). The older shape
    # rule decides, unsure: the first full row that looks like labels is the header; a first full row that looks
    # like data (a date, or under 50% text) means no header row; if nothing decides, row 0.
    for i, row in enumerate(rows):
        cells = [c for c in row if c]
        if len(cells) < wide_min_cells:
            continue
        if any(k in _STRONG_KINDS for k in kinds[i] if k):
            return None, False
        textlike = sum(1 for c in cells if not _looks_like_data(c) and len(c) <= 80) / len(cells)
        unique = len({c.lower() for c in cells}) / len(cells)
        has_following_data = any(any(c for c in r) for r in rows[i + 1 : i + 4]) or i == len(rows) - 1
        if textlike >= 0.7 and unique >= 0.5 and has_following_data:
            return i, False
        if textlike < 0.5:
            return None, False
    return 0, False


def _labels_by_shape(row: list[str], kinds: list[str | None], min_cells: int) -> bool:
    """Whether a row looks like column labels by its cells alone (the older rule, used when columns can't be compared).

    Filled in at least ``min_cells`` cells, no phone number, email or date, at least 70% short text labels (not
    numbers, amounts or dates; 80 characters at most) and at least half of them distinct. ``kinds`` are the row's
    cell kinds (:func:`_cell_kind`).
        ["Name", "Phone"] → True;  ["Smith", "John", "5165550100"] → False;  ["Guest export"] with min_cells 2 → False
    """
    cells = [c for c in row if clean_text(c)]  # "N/A", "none" and the like don't count as labels
    if len(cells) < min_cells or any(k in _STRONG_KINDS for k in kinds if k):
        return False
    textlike = sum(1 for c in cells if not _looks_like_data(c) and len(c) <= 80) / len(cells)
    return textlike >= 0.7 and len({c.lower() for c in cells}) / len(cells) >= 0.5


# Words common in the column names of customer exports (any system). Only used to choose between rows that both look
# like labels when the columns can't be compared (see detect_header): the header has more of them than a report
# line ("Store, Kravit Bayside") or a customer row without a phone ("271 Main St, no phone").
_HEADER_WORDS = {
    "name", "first", "last", "full", "phone", "mobile", "cell", "tel", "telephone", "email", "mail", "address",
    "street", "city", "state", "zip", "postal", "country", "date", "visit", "visits", "birthday", "dob", "birth",
    "anniversary", "created", "updated", "signup", "joined", "total", "spend", "orders", "order", "amount",
    "points", "opt", "sms", "marketing", "consent", "notes", "tags", "id", "customer", "guest", "member", "gender",
    "company", "type",
}


def _header_words(row: list[str]) -> int:
    """How many cells of a row contain a word common in column names (see _HEADER_WORDS).
        ["First Name", "Mobile #", "City"] → 3;  ["Store", "Kravit Bayside"] → 0;  ["271 Main St", "no phone"] → 1
    """
    return sum(1 for c in row if any(w in _HEADER_WORDS for w in re.findall(r"[a-z]+", c.lower())))


def detect_header_row(grid: list[list[str]]) -> int | None:
    """The header row from :func:`detect_header` without the sure/unsure flag (0-based index, None = no header).

    Examples::

        [["Customer export"], [], ["Name", "Phone", "Email"], ["Ana", "2015550111", "a@x.com"]] -> 2
        [["Ana Lee", "2015550111", "a@x.com"], ["Ben Ode", "2015550112", "b@x.com"]]           -> None
    """
    return detect_header(grid)[0]


def _unique_headers(raw: list[str]) -> list[str]:
    """Make header names usable as DataFrame columns and mapping sources: non-blank and unique.

    Whitespace runs are collapsed; a blank header becomes "Column <position>" (1-based); a repeated header
    (case-insensitive) gets " (2)", " (3)", ... appended, keeping its own spelling.

    Example: ``["Name", "name", " ", "Phone  Number", "Name"]`` ->
    ``["Name", "name (2)", "Column 3", "Phone Number", "Name (3)"]``.
    """
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
    """Turn the raw grid into the text-only DataFrame, using row ``header_row`` (0-based) as the header.

    * Cells are cleaned (:func:`_clean_cell`) and short rows are padded, so ragged files still give a rectangle;
      values beyond the header's width end up in extra "Column N" columns instead of being lost.
    * ``header_row=None``: columns are named "Column 1", "Column 2", ... and every row is data. Otherwise rows
      above the header (titles, blank lines) are discarded and the rows after it are the data.
    * Columns with neither a header nor any data (e.g. trailing delimiters) are dropped; names are made unique.
    * Trailing empty rows are dropped, interior blank rows kept (so file row numbers stay correct).

    Example: ``build_frame([["Name", "Phone", ""], ["Ana", "2015550111", ""]], 0)`` -> columns
    ["Name", "Phone"], one row.
    """
    grid = [[_clean_cell(c) for c in row] for row in grid]
    # Pad every row to the widest row.
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

    Inputs: ``filename`` (only its extension is used, to pick the reader), the raw ``data`` bytes, and for
    spreadsheets the ``sheet`` name (unknown or None -> the first sheet). The reviewer can re-read the file with
    another sheet or header row; ``ReadResult.detected_header_row`` keeps the automatic choice.

    Steps: check the extension and that the file isn't empty -> raw grid (text: encoding + delimiter detection;
    spreadsheet: sheet choice) -> clean cells -> detect or apply the header row -> :func:`build_frame` ->
    warnings.

    Warnings (``ReadResult.warnings``): rows with more values than header columns (text files only - usually an
    unquoted comma such as ``Smith, John``), header found below the first row, no header row, detection unsure,
    a header row holding a phone number / email / date (likely a customer row, e.g. picked by hand), no data rows.

    Raises :class:`ReadError` for an unsupported extension, an empty file, a file or sheet without any values, a
    header row past the end of the file, a spreadsheet that can't be opened, or a sheet too big for this computer's
    memory (:func:`sheetsize.check_sheet_size`).

    Example: ``read_file("export.csv", b"Report\\n\\nName,Phone\\nAna,2015550111\\n")`` -> header_row 2, columns
    ["Name", "Phone"], warning "Header found on row 3; 2 row(s) above it were skipped.".
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
        # The requested sheet if it exists, else the first one.
        chosen_sheet = sheet if sheet in sheets else (sheets[0] if sheets else None)
        # Before the spreadsheet library reserves memory for the sheet: a stray value far away from the table can
        # make it ask for hundreds of GB and abort the whole app (see sheetsize.py). Refused with a clear message
        # only when the sheet needs more memory than this computer has.
        try:
            check_sheet_size(data, ext, chosen_sheet)
        except SheetTooBig as exc:
            raise ReadError(str(exc)) from None
        grid = _read_excel_grid(data, chosen_sheet)

    # Clean once up front: the empty-file check, header detection, the ragged-row check and the raw preview see
    # the same values as build_frame (a cell holding only a non-breaking space counts as empty).
    cleaned = [[_clean_cell(c) for c in row] for row in grid]
    if not any(any(r) for r in cleaned):
        raise ReadError("No data found in the file" + (f" (sheet '{chosen_sheet}')" if chosen_sheet else "") + ".")

    # Always detect (reported as detected_header_row); use it unless the caller chose a row (int) or "no header"
    # (None).
    detected, sure = detect_header(cleaned)
    hr = detected if header_row == "auto" else header_row
    if isinstance(hr, int) and hr >= len(cleaned):
        raise ReadError(f"Header row {hr + 1} is past the end of the file ({len(cleaned)} rows).")

    df = build_frame(cleaned, hr)
    warnings = []
    if isinstance(hr, int) and ext in TEXT_EXTENSIONS:
        # Ragged rows: data rows with values past the number of named header columns. In a CSV that almost always
        # means a value with an unquoted delimiter, which shifts every later value of the row one column right.
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
        warnings.append("No header row detected — columns are named Column 1, Column 2, … Choose where each one "
                        "goes under Columns in your file.")
    elif hr > 0:
        warnings.append(f"Header found on row {hr + 1}; {hr} row(s) above it were skipped.")
    if header_row == "auto" and not sure:
        warnings.append("Couldn't tell for sure whether the file has a header row. Check the first rows under File "
                        "options: if row " + ("1" if hr is None else str(hr + 1)) + " is not what you expect, set "
                        "'Header is on row' and click Re-read file.")
    if isinstance(hr, int):
        # Safety net, also for a row picked by hand: a header row holding a phone number, email or date is usually
        # a customer row, and its values would be lost as column names.
        data_cells = [c for c in cleaned[hr] if _cell_kind(c) in _STRONG_KINDS]
        if data_cells:
            warnings.append(f"Row {hr + 1} is used as the header but holds customer-looking values "
                            f"('{data_cells[0][:40]}'). If it is a customer, set 'Header is on row' to the real "
                            "header row (0 = no header row) and click Re-read file.")
    if df.empty:
        warnings.append("The file has a header but no data rows.")

    # encoding / delimiter are only set for text files (result_kwargs).
    return ReadResult(
        df=df,
        sheet_names=sheets,
        sheet=chosen_sheet,
        header_row=hr,
        detected_header_row=detected,
        header_sure=sure,
        raw_preview=cleaned[:15],
        warnings=warnings,
        **result_kwargs,
    )
