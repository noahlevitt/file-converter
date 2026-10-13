"""Excel error values in spreadsheet cells (#DIV/0!, #REF!, #VALUE!…), which the spreadsheet library reads as empty.

A cell whose formula broke when the client exported the file holds an error code instead of a value. Text files
(CSV) keep the code as text, and since 0.15.1 every field reports it as *Excel error (broken formula in the
client's file)*. The spreadsheet library (calamine) returns such cells as empty, so in .xlsx, .xls and .ods files
the reason was lost: a phone cell became "required value is missing" and any other field was silently blank.
:func:`error_cells` finds the codes so the reader can put them back into the empty cells (0.19.0).

How each format stores them, and how they are found:
  - .xlsx / .xlsm: ``<c r="B2" t="e"><f>1/0</f><v>#DIV/0!</v></c>`` in the sheet's XML (cell type "e").
  - .ods: ``<table:table-cell calcext:value-type="error" …><text:p>#DIV/0!</text:p>`` in content.xml (LibreOffice
    writes the error type; other writers can't be told apart from text and are left alone).
  - .xls: BOOLERR records and formula records whose cached result is an error, read with xlrd. A byte search
    for those records runs first, so files without errors (nearly all) aren't read a second time; when xlrd isn't
    installed, .xls errors stay empty as before.
  - .xlsb: not supported (rare); errors stay empty.
Everything here is best effort: any problem returns {} and the file reads exactly as before.
"""

from __future__ import annotations

import html
import io
import re
import zipfile

from .sheetsize import _col_number, _xlsx_sheet_path

# xlsx: a whole cell element of type "e" (attributes in any order, optional "x:" prefix), and its <v> value.
_XLSX_ERROR_CELL = re.compile(rb'<(?:\w+:)?c\b([^>]*?\bt="e"[^>]*)>(.*?)</(?:\w+:)?c>', re.S)
_XLSX_REF = re.compile(rb'\br="([A-Z]{1,3})([0-9]+)"')
_XLSX_VALUE = re.compile(rb'<(?:\w+:)?v>([^<]*)</')

# ods: a sheet start tag, a row start tag, a cell tag (also covered cells), repeats, the error value type.
_ODS_TABLE = re.compile(rb'<table:table\s([^>]*?)/?>')
_ODS_NAME = re.compile(rb'table:name="([^"]*)"')
_ODS_TOKEN = re.compile(rb'<table:table-row(?=[\s/>])([^>]*?)/?>|<table:(?:covered-)?table-cell\b([^>]*?)(/?)>')
_ODS_REP = re.compile(rb'number-(?:rows|columns)-repeated="([0-9]+)"')
_TAGS = re.compile(rb"<[^>]+>")

# xls (BIFF8) records holding an error: BOOLERR (id 0x0205, length 8, last byte 1 = error) and FORMULA (id 0x0006)
# whose 8-byte cached result starts with 2 (error) and ends with 0xFFFF. Error codes: 0x00 #NULL!, 0x07 #DIV/0!,
# 0x0F #VALUE!, 0x17 #REF!, 0x1D #NAME?, 0x24 #NUM!, 0x2A #N/A, 0x2B #GETTING_DATA.
_XLS_CODES = rb"[\x00\x07\x0f\x17\x1d\x24\x2a\x2b]"
_XLS_ERROR_RECORD = re.compile(rb"\x05\x02\x08\x00.{6}" + _XLS_CODES + rb"\x01|\x06\x00..(?:.{6})\x02." + _XLS_CODES
                               + rb"...\xff\xff", re.S)


def error_cells(data: bytes, ext: str, sheet: str | None) -> dict[tuple[int, int], str]:
    """{(row, column): error code} for the named sheet (None = the first), 0-based from cell A1.

    Example: a sheet whose B2 holds #DIV/0! → {(1, 1): "#DIV/0!"}. Empty when the sheet has no error cells, for
    formats that aren't supported, and on any problem reading the file.
    """
    try:
        if ext in (".xlsx", ".xlsm"):
            return _xlsx_errors(data, sheet)
        if ext == ".ods":
            return _ods_errors(data, sheet)
        if ext == ".xls":
            return _xls_errors(data, sheet)
    except Exception:  # noqa: BLE001 - only restores a reason; never a reason to refuse or slow the file
        return {}
    return {}


def _xlsx_errors(data: bytes, sheet: str | None) -> dict[tuple[int, int], str]:
    z = zipfile.ZipFile(io.BytesIO(data))
    path = _xlsx_sheet_path(z, sheet)
    if path is None or path not in z.namelist():
        return {}
    xml = z.read(path)
    if b't="e"' not in xml:
        return {}
    out = {}
    for m in _XLSX_ERROR_CELL.finditer(xml):
        ref, value = _XLSX_REF.search(m.group(1)), _XLSX_VALUE.search(m.group(2))
        if ref and value and value.group(1).strip():
            out[(int(ref.group(2)) - 1, _col_number(ref.group(1)) - 1)] = html.unescape(value.group(1).decode())
    return out


def _ods_errors(data: bytes, sheet: str | None) -> dict[tuple[int, int], str]:
    xml = zipfile.ZipFile(io.BytesIO(data)).read("content.xml")
    if b'value-type="error"' not in xml:
        return {}
    start = None
    for m in _ODS_TABLE.finditer(xml):
        name = _ODS_NAME.search(m.group(1))
        if sheet is None or (name and html.unescape(name.group(1).decode("utf-8", "replace")) == sheet):
            start = m.end()
            break
    if start is None:
        return {}
    end = xml.find(b"</table:table>", start)
    end = len(xml) if end < 0 else end
    if xml.find(b'value-type="error"', start, end) < 0:
        return {}
    out: dict[tuple[int, int], str] = {}
    row, next_row, col = -1, 0, 0
    for m in _ODS_TOKEN.finditer(xml, start, end):
        if m.group(1) is not None:  # a row: it starts where the previous one (and its repeats) ended
            rep = _ODS_REP.search(m.group(1))
            row, col = next_row, 0
            next_row = row + (int(rep.group(1)) if rep else 1)
            continue
        attrs, closed = m.group(2), m.group(3) == b"/"
        rep = _ODS_REP.search(attrs)
        n = int(rep.group(1)) if rep else 1
        if b'value-type="error"' in attrs and not closed:
            close = xml.find(b"</table:table-cell>", m.end(), end)
            code = html.unescape(_TAGS.sub(b"", xml[m.end():close]).decode("utf-8", "replace")).strip()
            if code:
                for k in range(min(n, 1000)):  # a repeated error cell (capped: never a reason to slow down)
                    out[(row, col + k)] = code
        col += n
    return out


def _xls_errors(data: bytes, sheet: str | None) -> dict[tuple[int, int], str]:
    if not _XLS_ERROR_RECORD.search(data):
        return {}
    import xlrd  # optional (requirements.txt); without it .xls errors stay empty, as before 0.19.0

    book = xlrd.open_workbook(file_contents=data, on_demand=True)
    try:
        sh = book.sheet_by_name(sheet) if sheet is not None else book.sheet_by_index(0)
        out = {}
        for r in range(sh.nrows):
            for c, ctype in enumerate(sh.row_types(r)):
                if ctype == xlrd.XL_CELL_ERROR:
                    out[(r, c)] = xlrd.error_text_from_code.get(sh.cell_value(r, c), "#ERROR!")
        return out
    finally:
        book.release_resources()
