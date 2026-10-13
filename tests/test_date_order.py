"""Day/month order and date + time edge cases (0.19.0).

Before:
  - Excel date cells read as "2026-03-15 00:00:00"; joined with a Time column that became
    "2026-03-15 00:00:00 15:15:00", which no date reader understands, so every visit date was lost (0.18.0 left the
    time out instead).
  - An input format such as "%d/%m/%Y" only worked for values that matched it exactly: "03/10/2026 3:15 PM" fell
    back to month-first and became March 10.
  - The YMD option was ignored for a 2-digit year first: "26/10/09" became October 26, 2009.
  - A day-first column written without separators (15032026, 28022026, 01042026) read 01042026 as January 4.
"""

import datetime as dt
import io

import openpyxl
import pytest

from converter import normalizers as N
from converter.engine import convert
from converter.heuristics import auto_map
from converter.mapping import profile_columns
from converter.reader import read_file


def column(real_schema, values, field="transactions_last", **params):
    """Convert a whole column (its context decides the order) and return the outputs."""
    f = real_schema.field(field)
    ctx = N.prepare(f, values, params)
    ctx["tz"] = "America/New_York"
    return [N.normalize(v, f, params, ctx)[0] for v in values]


def workbook(header, rows) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.mark.parametrize("header", [["Phone", "Date", "Time"], ["Phone", "Last Visit Date", "Last Visit Time"]])
def test_excel_date_cells_with_a_time_column(real_schema, header):
    data = workbook(header, [["5165550100", dt.datetime(2026, 3, 15), dt.time(15, 15)],
                             ["9175551234", dt.datetime(2026, 3, 1), dt.time(9, 30)],
                             ["2125550142", dt.datetime(2026, 3, 2), dt.time(0, 0)]])
    rr = read_file("f.xlsx", data)
    assert rr.df[header[1]].iloc[0] == "2026-03-15 00:00:00"  # how the spreadsheet library reads a date cell
    m = auto_map(real_schema, profile_columns(rr.df))
    assert m.get("transactions_last").sources == header[1:]
    assert convert(rr.df, real_schema, m).output["transactions_last"].tolist() == [
        "03/15/2026 03:15 PM", "03/01/2026 09:30 AM", "03/02/2026 12:00 AM"]


def test_real_times_in_the_date_column_still_win(real_schema):
    """A date column whose values have their own times (not just midnight) isn't combined with a time column."""
    rr = read_file("f.xlsx", workbook(["Phone", "Date", "Time"],
                                      [["5165550100", dt.datetime(2026, 3, 15, 14, 0), dt.time(15, 15)],
                                       ["9175551234", dt.datetime(2026, 3, 1, 8, 0), dt.time(9, 30)]]))
    assert auto_map(real_schema, profile_columns(rr.df)).get("transactions_last").sources == ["Date"]


@pytest.mark.parametrize("fmt,values,expected", [
    ("%d/%m/%Y", ["03/10/2026 3:15 PM", "03/10/2026", "4/11/2026"],
     ["10/03/2026 03:15 PM", "10/03/2026 12:00 AM", "11/04/2026 12:00 AM"]),
    ("%Y-%m-%d", ["2026-10-09 15:15"], ["10/09/2026 03:15 PM"]),
    ("%y-%m-%d", ["26-10-09 15:15"], ["10/09/2026 03:15 PM"]),
    ("%m/%d/%Y", ["03/10/2026 3:15 PM"], ["03/10/2026 03:15 PM"]),
])
def test_input_format_order_also_for_values_with_a_time(real_schema, fmt, values, expected):
    assert column(real_schema, values, input_format=fmt) == expected


@pytest.mark.parametrize("fmt,order", [("%d/%m/%Y", "DMY"), ("%d.%m.%y %H:%M", "DMY"), ("%Y-%m-%d", "YMD"),
                                       ("%m/%d/%Y", "MDY"), ("%d %B %Y", "DMY"), ("%B %d, %Y", "MDY"),
                                       ("%H:%M", None)])
def test_order_of_an_input_format(fmt, order):
    assert N._format_order(fmt) == order


def test_year_month_day_option(real_schema):
    values = ["26/10/09", "26.10.09", "261009", "2026/10/09", "26-10-09 3:15 PM", "101009"]
    assert column(real_schema, values, date_order="YMD") == [
        "10/09/2026 12:00 AM"] * 4 + ["10/09/2026 03:15 PM", "10/09/2010 12:00 AM"]
    assert column(real_schema, ["85/09/12", "850912"], field="birthday", date_order="YMD") == ["09/12/1985"] * 2


def test_day_first_without_separators(real_schema):
    """8-digit values that can't be YYYYMMDD vote for the order like 15/03/2026 does."""
    assert column(real_schema, ["15032026", "28022026", "01042026"]) == [
        "03/15/2026 12:00 AM", "02/28/2026 12:00 AM", "04/01/2026 12:00 AM"]
    assert column(real_schema, ["150390", "280290", "010490"]) == [
        "03/15/1990 12:00 AM", "02/28/1990 12:00 AM", "04/01/1990 12:00 AM"]
    assert column(real_schema, ["1503", "2802", "0104"], field="birthday") == ["03/15/1600", "02/28/1600",
                                                                               "04/01/1600"]


def test_values_that_dont_tell_the_order_dont_vote(real_schema):
    """YYYYMMDD and a US column stay as they were; 6 digits that could be YYMMDD don't decide the order."""
    assert column(real_schema, ["20261009", "20261015", "10092026"]) == ["10/09/2026 12:00 AM",
                                                                          "10/15/2026 12:00 AM",
                                                                          "10/09/2026 12:00 AM"]
    assert column(real_schema, ["03152026", "12252026", "01042026"])[2] == "01/04/2026 12:00 AM"
    assert N._first_two_parts("150326", False) is None and N._first_two_parts("1985", True) is None
