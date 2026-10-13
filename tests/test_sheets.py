"""Which sheet of a workbook is read when nobody chose one: the first visible sheet with data (0.17.0).

Before: the first sheet was always read. An empty "Sheet1" left in front of the customer list stopped the file with
"No data found", and a hidden first sheet (a lookup table) was read instead of the customers, silently. Now empty,
hidden and chart sheets are passed over, a warning names them, the sheet choice marks hidden sheets, and a sheet
chosen by the reviewer (or saved with an import) is read as before.
"""

import io

import openpyxl
import pytest

from converter.reader import ReadError, read_file


def workbook(*sheets) -> bytes:
    """An .xlsx with sheets given as (name, rows, state); state is "visible", "hidden" or "veryHidden"."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows, state in sheets:
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(r)
        ws.sheet_state = state
    if all(s[2] != "visible" for s in sheets):
        wb.active = 0
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


CUSTOMERS = [["Phone", "Last Visit"], ["5165550100", "10/01/2026"], ["9175551234", "10/02/2026"]]
LOOKUP = [["Code", "Meaning"], ["A", "Active"]]


def test_empty_first_sheet_is_passed_over():
    rr = read_file("x.xlsx", workbook(("Sheet1", [], "visible"), ("Customers", CUSTOMERS, "visible")))
    assert rr.sheet == "Customers" and rr.df["Phone"].tolist() == ["5165550100", "9175551234"]
    assert any(w.startswith("Read sheet 'Customers' because 'Sheet1' is empty.") for w in rr.warnings)


def test_hidden_first_sheet_is_passed_over():
    data = workbook(("Lookup", LOOKUP, "hidden"), ("Sheet1", [], "visible"), ("Customers", CUSTOMERS, "visible"),
                    ("Old", CUSTOMERS, "veryHidden"))
    rr = read_file("x.xlsx", data)
    assert rr.sheet == "Customers" and list(rr.df.columns) == ["Phone", "Last Visit"]
    assert rr.hidden_sheets == ["Lookup", "Old"]
    assert any("because 'Lookup' is hidden and 'Sheet1' is empty" in w for w in rr.warnings)


def test_first_sheet_with_data_is_read_without_a_warning():
    rr = read_file("x.xlsx", workbook(("Summary", LOOKUP, "visible"), ("Customers", CUSTOMERS, "visible")))
    assert rr.sheet == "Summary" and not any("Read sheet" in w for w in rr.warnings)


def test_a_chosen_sheet_is_read_even_if_hidden_or_empty():
    data = workbook(("Lookup", LOOKUP, "hidden"), ("Sheet1", [], "visible"), ("Customers", CUSTOMERS, "visible"))
    assert list(read_file("x.xlsx", data, sheet="Lookup").df.columns) == ["Code", "Meaning"]
    with pytest.raises(ReadError, match=r"No data found in the file \(sheet 'Sheet1'\)"):
        read_file("x.xlsx", data, sheet="Sheet1")
    # A sheet name the file doesn't have (e.g. from a template made on another workbook): chosen automatically.
    assert read_file("x.xlsx", data, sheet="Guests").sheet == "Customers"


def test_data_only_on_a_hidden_sheet_is_still_read():
    rr = read_file("x.xlsx", workbook(("Sheet1", [], "visible"), ("Data", CUSTOMERS, "hidden")))
    assert rr.sheet == "Data" and len(rr.df) == 2
    assert any("Read sheet 'Data' (a hidden sheet) because 'Sheet1' is empty." in w for w in rr.warnings)


def test_no_data_anywhere():
    with pytest.raises(ReadError, match="No data found in any sheet of the file"):
        read_file("x.xlsx", workbook(("Sheet1", [], "visible"), ("Sheet2", [], "visible")))
    with pytest.raises(ReadError, match=r"No data found in the file \(sheet 'Sheet1'\)"):
        read_file("x.xlsx", workbook(("Sheet1", [], "visible")))
