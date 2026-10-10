"""Reading uploaded CSV/TSV/Excel files into a table of text values.

Covers encoding, delimiters, title rows above the header, files without a header, Excel sheets and numeric cells,
ragged rows, duplicate or blank headers, and unreadable files. Every later step depends on this: a misread header
or encoding garbles names or shifts phone numbers into the wrong column, and the file row numbers reported back
must match the rows the user sees when they open the file in Excel.
"""

import io

import openpyxl
import pytest

from converter.reader import ReadError, detect_header_row, read_file
from tests.conftest import read_sample


def test_title_rows_and_cp1252(samples):
    """A POS export with title rows and Windows-1252 accents is read with the right header and intact accents.

    Data row 0 maps back to file row 5, which is what the user sees in Excel.
    """
    rr = read_sample(samples, "pos_guest_export.csv")
    # header_row is 0-based: the header is on line 4 of the file, after two title lines and a blank one.
    assert rr.header_row == 3
    assert rr.encoding == "cp1252"
    assert rr.df.columns[0] == "Guest Name"
    assert rr.df.iloc[0]["Guest Name"] == "José Martínez"
    assert rr.excel_row_number(0) == 5


def test_semicolon_delimiter(samples):
    """Semicolon-separated (European-style) files are detected without the user choosing a delimiter."""
    rr = read_sample(samples, "eu_style_semicolon.csv")
    assert rr.delimiter == ";"
    assert list(rr.df.columns)[:3] == ["Name", "Surname", "Cell"]


def test_no_header(samples):
    """A file whose first row is data gets 'Column N' headers, keeps every row, and numbers rows from 1."""
    rr = read_sample(samples, "no_header.tsv")
    assert rr.header_row is None
    assert list(rr.df.columns) == ["Column 1", "Column 2", "Column 3"]
    assert len(rr.df) == 3
    assert rr.excel_row_number(0) == 1


def test_excel_sheet_selection_and_text_values(samples):
    """The chosen sheet of a multi-sheet workbook is read, and numeric cells come back as plain text digits."""
    rr = read_sample(samples, "loyalty_members.xlsx", sheet="Members")
    assert rr.sheet_names == ["Summary", "Members"]
    assert rr.df.iloc[0]["PHONE NUMBER"] == "5165550100"
    assert rr.df.iloc[1]["ZIP CODE"] == "7030"  # restored to 07030 by the zip normalizer later


def test_manual_header_override(samples):
    """The user can say the file has no header row, overriding detection."""
    rr = read_sample(samples, "pos_guest_export.csv", header_row=None)
    assert rr.header_row is None and rr.df.columns[0] == "Column 1"


def test_ragged_row_warning():
    """A row with more values than headers produces a warning rather than silently shifting the phone column."""
    # The unquoted comma in "Smith, John" splits the name into two values.
    data = b"Name,Phone\nSmith, John,5165550100\nAmy,5165550131\n"
    rr = read_file("x.csv", data)
    assert any("more values than there are header columns" in w for w in rr.warnings)


def test_duplicate_and_blank_headers():
    """Duplicate headers get a numbered suffix and blank ones a 'Column N' name, so each can be mapped separately."""
    rr = read_file("x.csv", b"Phone,Phone,,Email\n1,2,3,a@b.com\n")
    assert list(rr.df.columns) == ["Phone", "Phone (2)", "Column 3", "Email"]


def test_errors():
    """An unsupported file type, an empty file and a file of only empty cells raise ReadError with a message."""
    with pytest.raises(ReadError):
        read_file("x.pdf", b"abc")
    with pytest.raises(ReadError):
        read_file("x.csv", b"")
    with pytest.raises(ReadError):
        read_file("x.csv", b",,,\n,,,\n")


def test_detect_header_row_skips_title():
    """Header detection skips a title and a blank row and picks the row of column names (0-based index 2)."""
    grid = [["Report"], [], ["First", "Last", "Phone"], ["A", "B", "5165550100"]]
    assert detect_header_row(grid) == 2


def test_xlsx_with_blank_top_rows():
    """In Excel, blank and title rows above the header are skipped and a numeric phone cell reads as plain digits."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append([])
    ws.append(["Customer List"])
    ws.append(["Phone", "Email"])
    # A number cell: it must come back as the text "5165550100", not "5165550100.0".
    ws.append([5165550100, "a@b.com"])
    buf = io.BytesIO()
    wb.save(buf)
    rr = read_file("x.xlsx", buf.getvalue())
    assert list(rr.df.columns) == ["Phone", "Email"]
    assert rr.df.iloc[0]["Phone"] == "5165550100"
