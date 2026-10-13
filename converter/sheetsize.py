"""Size check for spreadsheets, before they are read (since 0.15.2).

Why
---
The spreadsheet reader (calamine) first reserves memory for the whole rectangle from the first to the last cell
that holds a value, 32 bytes per cell, empty or not. One stray value in the last cell of an .xlsx sheet
(XFD1048576) makes that 17 billion cells: it asks for 512 GB and the whole app process is aborted - every user is
disconnected and running imports are interrupted. An 822-byte .ods that says "repeat this row 1,000,000 times"
does the same. Python can't catch that abort, so the size has to be known *before* the reader starts.

What
----
:func:`check_sheet_size` estimates the memory a sheet needs from its file structure alone (where its values are,
not the values) and raises :class:`SheetTooBig` only when that is more than the computer has, i.e. for a file
that would crash or exhaust the machine anyway. Every file that can be read today is still read; nothing is
changed or left out. Estimate (measured on this app's reader): about 60 bytes per cell of the rectangle plus
about 150 bytes per cell that holds a value; the budget is 80% of the machine's memory (the container's limit
when it has one).

Formats: .xlsx/.xlsm (positions are read from the sheet XML) and .ods (repeated rows and cells counted). .xls
needs no check (the format can't exceed 65,536 x 256 cells); .xlsb isn't checked (rare, binary). Any surprise
while checking (unusual file layout, missing parts) skips the check, so the check itself never blocks a file.

Speed: one extra pass over the sheet's XML, done by fast regular expressions: about 0.015 s for 5,000 rows and
0.5 s for 150,000 rows x 8 columns (reading such a file takes about 3 s). A file that passed isn't scanned again
(re-read, browser refresh, the background import). Formatting (styled cells, formatted rows reaching the bottom
of the sheet) only triggers the slower exact pass, which ignores cells without a value just like the reader does.
"""

from __future__ import annotations

import html
import io
import os
import posixpath
import re
import sys
import zipfile
import xml.etree.ElementTree as ET  # small files only (workbook.xml and its relations)
import zlib

# Memory estimate, measured with this app's reader (read_file): a sheet whose values span 200,000 rows x 26
# columns but hold only ~2,000 values needed ~60 bytes per cell of the rectangle; a dense sheet (150,000 x 8, every
# cell a value) ~210 bytes per cell, i.e. ~150 more for each cell holding a value.
AREA_BYTES = 60
VALUE_BYTES = 150
# Share of the machine's memory a single sheet may use: the app, other users and the running imports need the rest.
MEMORY_SHARE = 0.8


# Sheets that passed the check, keyed by (file size, CRC-32 of the bytes, sheet, budget): the same file is read
# again on "Re-read file", after a browser refresh and by the background import, and needn't be scanned again.
# CRC-32 runs at about 1 GB/s, so the key costs a few milliseconds even for a large file.
_PASSED: dict[tuple, bool] = {}
_PASSED_MAX = 64


class SheetTooBig(Exception):
    """A sheet needs more memory than this computer has; the message says where its values reach and what to do."""


def memory_total() -> int | None:
    """Physical memory of this computer in bytes, or the container's memory limit when that is lower.

    None when it can't be found (then a conservative 8 GB is assumed by :func:`memory_budget`).
    """
    total = None
    try:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")  # Linux and macOS
    except (ValueError, OSError, AttributeError):
        if sys.platform == "win32":
            total = _windows_memory()
    # Docker / cgroup limits (v2, then v1). "max" or a huge number means no limit.
    for p in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            v = open(p).read().strip()
        except OSError:
            continue
        if v.isdigit() and int(v) < (total or 1 << 62):
            total = int(v)
    return total


def _windows_memory() -> int | None:
    """Total physical memory on Windows (GlobalMemoryStatusEx), or None."""
    try:
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(MemoryStatus)
        return int(status.ullTotalPhys) if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)) else None
    except Exception:  # noqa: BLE001 - any failure just means "unknown"
        return None


def memory_budget() -> int:
    """Bytes one sheet may need: MEMORY_SHARE of :func:`memory_total` (8 GB assumed when unknown)."""
    return int((memory_total() or 8 << 30) * MEMORY_SHARE)


def col_letters(n: int) -> str:
    """1-based column number -> Excel letters: 1 -> "A", 27 -> "AA", 16384 -> "XFD"."""
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _col_number(letters: bytes) -> int:
    """Excel column letters -> 1-based number: b"A" -> 1, b"XFD" -> 16384."""
    n = 0
    for ch in letters:
        n = n * 26 + (ch - 64)
    return n


class _Extent:
    """Where a sheet's values are: first/last row and column (1-based), the cells that reach furthest, how many."""

    def __init__(self, min_row, max_row, min_col, max_col, values, far_row_cell=None, far_col_cell=None):
        self.min_row, self.max_row, self.min_col, self.max_col = min_row, max_row, min_col, max_col
        self.values = values
        self.far_row_cell, self.far_col_cell = far_row_cell, far_col_cell

    @property
    def area(self) -> int:
        return (self.max_row - self.min_row + 1) * (self.max_col - self.min_col + 1)

    @property
    def need(self) -> int:
        """Estimated bytes to read the sheet (see AREA_BYTES / VALUE_BYTES)."""
        return self.area * AREA_BYTES + self.values * VALUE_BYTES


def check_sheet_size(data: bytes, ext: str, sheet: str | None, budget: int | None = None) -> None:
    """Raise :class:`SheetTooBig` when reading `sheet` of this spreadsheet would need more memory than `budget`.

    `ext` is the lower-case extension (".xlsx"…); `sheet` the sheet name that will be read (None = the first);
    `budget` defaults to :func:`memory_budget`. Returns quietly for formats that aren't checked and whenever the
    file's layout isn't what the check expects (the reader then reports any real problem itself).
    """
    budget = memory_budget() if budget is None else budget
    if ext not in (".xlsx", ".xlsm", ".ods"):
        return
    key = (len(data), zlib.crc32(data), sheet, budget)
    if key in _PASSED:
        return
    try:
        extent = _xlsx_extent(data, sheet, budget) if ext in (".xlsx", ".xlsm") else _ods_extent(data, sheet)
    except Exception:  # noqa: BLE001 - any surprise (corrupt zip, unusual layout) skips the check by design
        return  # the reader then reads the file, or reports what's wrong with it, as before
    if extent is None or extent.need <= budget:
        if len(_PASSED) >= _PASSED_MAX:
            _PASSED.clear()
        _PASSED[key] = True
        return
    raise SheetTooBig(_message(extent, budget))


def _message(e: _Extent, budget: int) -> str:
    """The reviewer-facing explanation: where the values reach, the memory needed and what to do."""
    gb = lambda b: f"{b / (1 << 30):,.0f} GB" if b >= 1 << 30 else f"{b / (1 << 20):,.0f} MB"  # noqa: E731
    total = budget / MEMORY_SHARE
    if e.far_row_cell and e.far_col_cell and e.far_row_cell != e.far_col_cell:
        where = (f"its values reach row {e.max_row:,} (cell {e.far_row_cell}) and column {col_letters(e.max_col)} "
                 f"(cell {e.far_col_cell})")
    else:
        where = f"its values reach cell {e.far_row_cell or col_letters(e.max_col) + str(e.max_row)}"
    rows, cols = e.max_row - e.min_row + 1, e.max_col - e.min_col + 1
    return (f"This sheet is too big to read on this computer: {where}, a block of {rows:,} rows x {cols:,} columns "
            f"that would need about {gb(e.need)} of memory (this computer has {gb(total)}). This is usually a stray "
            "value far away from the table, or a formula copied down to the bottom of the sheet. In Excel, Ctrl+End "
            "jumps to the last used cell: delete what's there (or copy just the table into a new file) and upload "
            "the file again.")


# ---------------------------------------------------------------------------------------------------------- xlsx

# Fast pass. Excel and most writers put the position first: <c r="B2" ...>, <row r="2" ...>.
_CELL_COL_FAST = re.compile(rb'<c r="([A-Z]{1,3})')
_ROW_FAST = re.compile(rb'<row r="([0-9]{1,7})"')
# Exact pass: any attribute order and an optional namespace prefix (<x:c s="1" r="B2">), and only cells that hold
# a value (<v>) or text (<is>), after an optional formula (<f>). Formatting-only cells - <c r="B2" s="1"/> from
# Excel, <c r="B2" s="1" t="n"></c> from other writers - are skipped, as the reader skips them.
_VALUE_CELL = re.compile(
    rb'<(?:\w+:)?c\s(?:[^>]*?\s)?r="([A-Z]{1,3})([0-9]{1,7})"[^>]*?(?<!/)>'   # opening tag, not self-closing
    rb'\s*(?:<(?:\w+:)?f\b[^>]*?(?:/>|>[^<]*</(?:\w+:)?f>)\s*)?'              # optional formula
    rb'<(?:\w+:)?(?:v|is)[\s>]')                                               # a value or inline text
_ANY_CELL_TAG = re.compile(rb'<(?:[A-Za-z0-9_]+:)?c[\s>/]')


def _xlsx_sheet_path(z: zipfile.ZipFile, sheet: str | None) -> str | None:
    """Path inside the zip of the named worksheet's XML (None = first sheet), following workbook.xml.rels."""
    wb_path = "xl/workbook.xml"
    try:  # the package's main part, normally xl/workbook.xml
        for rel in ET.fromstring(z.read("_rels/.rels")):
            if rel.get("Type", "").endswith("/officeDocument"):
                wb_path = rel.get("Target", wb_path).lstrip("/")
    except KeyError:
        pass
    rid = None
    for el in ET.fromstring(z.read(wb_path)).iter():
        if el.tag.endswith("}sheet") and (sheet is None or el.get("name") == sheet):
            rid = next((v for k, v in el.attrib.items() if k.endswith("}id")), None)
            break
    if rid is None:
        return None
    base = posixpath.dirname(wb_path)
    rels_path = posixpath.join(base, "_rels", posixpath.basename(wb_path) + ".rels")
    for rel in ET.fromstring(z.read(rels_path)):
        if rel.get("Id") == rid:
            target = rel.get("Target", "")
            return target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(base, target))
    return None


def _xlsx_extent(data: bytes, sheet: str | None, budget: int) -> _Extent | None:
    """Where the sheet's values are. Fast pass first; the exact pass only when the fast one says "too big"."""
    z = zipfile.ZipFile(io.BytesIO(data))
    path = _xlsx_sheet_path(z, sheet)
    if path is None or path not in z.namelist():
        return None
    xml = z.read(path)
    # Fast pass (upper bound): every cell tag, formatted ones included, from row 1 / column A. Valid only when every
    # cell tag starts with r="…" (otherwise some cells would be missed).
    cols = _CELL_COL_FAST.findall(xml)
    n_tags = xml.count(b"<c ") + xml.count(b"<c>")
    if cols and len(cols) == n_tags and b":c " not in xml:
        rows = _ROW_FAST.findall(xml)
        max_col = max(_col_number(c) for c in set(cols))
        max_row = max(map(int, rows)) if rows else 0
        upper = _Extent(1, max(max_row, 1), 1, max_col, len(cols))
        if upper.need <= budget:
            return upper
    # Exact pass: only cells that hold something, at their real positions.
    found = _VALUE_CELL.findall(xml)
    if not found:
        # Cells without an r="…" position are placed one after another: no far-away jump is possible.
        return None if _ANY_CELL_TAG.search(xml) else _Extent(1, 1, 1, 1, 0)
    rows = [int(r) for _, r in found]
    col_nums = {c: _col_number(c) for c in set(c for c, _ in found)}
    i_row = max(range(len(found)), key=rows.__getitem__)
    far_col = max(col_nums, key=col_nums.get)
    far_col_row = next(r for c, r in found if c == far_col)
    return _Extent(min(rows), rows[i_row], min(col_nums.values()), col_nums[far_col], len(found),
                   far_row_cell=f"{found[i_row][0].decode()}{rows[i_row]}",
                   far_col_cell=f"{far_col.decode()}{int(far_col_row)}")


# ----------------------------------------------------------------------------------------------------------- ods

# content.xml uses the conventional prefixes "table:" and "office:" (LibreOffice, Excel, Google Sheets all do).
_ODS_TABLE = re.compile(rb'<table:table\s([^>]*?)/?>')        # a sheet (not <table:table-row>, which has "-")
_ODS_NAME = re.compile(rb'table:name="([^"]*)"')
# A row's start tag (<table:table-row ...>, not <table:table-row-group>).
_ODS_ROW_START = re.compile(rb'<table:table-row(?=[\s/>])')
_ODS_CELL = re.compile(rb'<table:(?:covered-)?table-cell\b([^>]*?)(/?)>')
_ODS_REP = re.compile(rb'number-(?:rows|columns)-repeated="([0-9]+)"')


def _ods_extent(data: bytes, sheet: str | None) -> _Extent | None:
    """Where the named sheet's values are in content.xml, counting repeated rows and cells as the reader does.

    Empty cells and rows don't count (LibreOffice writes the unused rest of a sheet as e.g. 1,048,000 repeated
    empty rows). A cell counts when it has a value type or any content.
    """
    xml = zipfile.ZipFile(io.BytesIO(data)).read("content.xml")
    start = None
    for m in _ODS_TABLE.finditer(xml):
        name = _ODS_NAME.search(m.group(1))
        if sheet is None or (name and html.unescape(name.group(1).decode("utf-8", "replace")) == sheet):
            start = m.end()
            break
    if start is None:
        return None
    end = xml.find(b"</table:table>", start)
    end = len(xml) if end < 0 else end
    # Row start positions; each row is then handled by position, without copying the (often 100+ MB) XML.
    starts = [m.start() for m in _ODS_ROW_START.finditer(xml, start, end)] + [end]
    row = 0
    min_row = max_row = min_col = max_col = None
    values = 0
    far_row_cell = far_col_cell = None
    for r0, r1 in zip(starts, starts[1:]):
        gt = xml.find(b">", r0, r1)
        head = xml[r0:gt]
        rep_m = _ODS_REP.search(head) if b"repeated" in head else None
        rep_rows = int(rep_m.group(1)) if rep_m else 1
        span = None if head.endswith(b"/") else _ods_row_values(xml, gt + 1, r1)
        if span:
            first_col, last_col, row_values = span
            min_col = first_col if min_col is None else min(min_col, first_col)
            if max_col is None or last_col > max_col:
                max_col, far_col_cell = last_col, (row + 1, last_col)
            first, last = row + 1, row + rep_rows
            min_row = first if min_row is None else min(min_row, first)
            if max_row is None or last >= max_row:
                max_row, far_row_cell = last, (last, last_col)
            values += row_values * rep_rows
        row += rep_rows
    if max_row is None:
        return None
    return _Extent(min_row, max_row, min_col, max_col, values,
                   far_row_cell=f"{col_letters(far_row_cell[1])}{far_row_cell[0]}",
                   far_col_cell=f"{col_letters(far_col_cell[1])}{far_col_cell[0]}")


def _ods_row_values(xml: bytes, start: int, end: int) -> tuple[int, int, int] | None:
    """(first value column, last value column, number of values) of the row content xml[start:end], or None.

    A cell holds a value when it has a value type or any content (an empty cell is a self-closing tag). Quick path,
    with byte searches only: no cell before the last value is repeated (LibreOffice repeats only the empty rest of a
    row), so the last value's column is the number of cell tags up to it; first column and count are then taken as
    1 and that column (upper bounds, which only make the estimate safer). Otherwise the row's cells are walked.
    """
    last_v = max(xml.rfind(b"office:value-type", start, end), xml.rfind(b"</table:table-cell>", start, end))
    if last_v < 0:
        return None
    if xml.find(b"repeated", start, last_v) < 0:
        last_col = xml.count(b"<table:table-cell", start, last_v)
        if xml.find(b"covered-table-cell", start, last_v) >= 0:
            last_col += xml.count(b"<table:covered-table-cell", start, last_v)
        last_col = max(last_col, 1)
        return 1, last_col, last_col
    col = 0
    first_col = last_col = None
    n = 0
    for cm in _ODS_CELL.finditer(xml, start, end):
        attrs, self_closing = cm.group(1), cm.group(2)
        c_rep = _ODS_REP.search(attrs) if b"repeated" in attrs else None
        rep_cols = int(c_rep.group(1)) if c_rep else 1
        if b"value-type" in attrs or not self_closing:
            first_col = col + 1 if first_col is None else first_col
            last_col = col + rep_cols
            n += rep_cols
        col += rep_cols
    return (first_col, last_col, n) if n else None
