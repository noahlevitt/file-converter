import io

import openpyxl
import pytest

from converter.reader import ReadError, detect_header_row, read_file
from tests.conftest import read_sample


def test_title_rows_and_cp1252(samples):
    rr = read_sample(samples, "pos_guest_export.csv")
    assert rr.header_row == 3
    assert rr.encoding == "cp1252"
    assert rr.df.columns[0] == "Guest Name"
    assert rr.df.iloc[0]["Guest Name"] == "José Martínez"
    assert rr.excel_row_number(0) == 5


def test_semicolon_delimiter(samples):
    rr = read_sample(samples, "eu_style_semicolon.csv")
    assert rr.delimiter == ";"
    assert list(rr.df.columns)[:3] == ["Name", "Surname", "Cell"]


def test_no_header(samples):
    rr = read_sample(samples, "no_header.tsv")
    assert rr.header_row is None
    assert list(rr.df.columns) == ["Column 1", "Column 2", "Column 3"]
    assert len(rr.df) == 3
    assert rr.excel_row_number(0) == 1


def test_excel_sheet_selection_and_text_values(samples):
    rr = read_sample(samples, "loyalty_members.xlsx", sheet="Members")
    assert rr.sheet_names == ["Summary", "Members"]
    assert rr.df.iloc[0]["PHONE NUMBER"] == "5165550100"
    assert rr.df.iloc[1]["ZIP CODE"] == "7030"  # restored to 07030 by the zip normalizer later


def test_manual_header_override(samples):
    rr = read_sample(samples, "pos_guest_export.csv", header_row=None)
    assert rr.header_row is None and rr.df.columns[0] == "Column 1"


def test_ragged_row_warning():
    data = b"Name,Phone\nSmith, John,5165550100\nAmy,5165550131\n"
    rr = read_file("x.csv", data)
    assert any("more values than there are header columns" in w for w in rr.warnings)


def test_duplicate_and_blank_headers():
    rr = read_file("x.csv", b"Phone,Phone,,Email\n1,2,3,a@b.com\n")
    assert list(rr.df.columns) == ["Phone", "Phone (2)", "Column 3", "Email"]


def test_errors():
    with pytest.raises(ReadError):
        read_file("x.pdf", b"abc")
    with pytest.raises(ReadError):
        read_file("x.csv", b"")
    with pytest.raises(ReadError):
        read_file("x.csv", b",,,\n,,,\n")


def test_detect_header_row_skips_title():
    grid = [["Report"], [], ["First", "Last", "Phone"], ["A", "B", "5165550100"]]
    assert detect_header_row(grid) == 2


def test_xlsx_with_blank_top_rows():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append([])
    ws.append(["Customer List"])
    ws.append(["Phone", "Email"])
    ws.append([5165550100, "a@b.com"])
    buf = io.BytesIO()
    wb.save(buf)
    rr = read_file("x.xlsx", buf.getvalue())
    assert list(rr.df.columns) == ["Phone", "Email"]
    assert rr.df.iloc[0]["Phone"] == "5165550100"
