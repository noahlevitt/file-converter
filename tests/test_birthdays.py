"""Birthdays and anniversaries with no real year always come out as MM/DD/1600."""

import pandas as pd
import pytest

from converter import normalizers as N
from converter.engine import convert
from converter.heuristics import auto_map
from converter.mapping import FieldMapping, Mapping, profile_columns


def bday(real_schema, value, column=None):
    f = real_schema.field("birthday")
    return N.normalize(value, f, {}, N.prepare(f, column or [value], {}))


@pytest.mark.parametrize("raw", ["09/12/0000", "09-12-0000", "09/12/XXXX", "XXXX-09-12", "09/12/yyyy", "YYYY-09-12",
                                 "Birthday: 9/12", "DOB 9/12", "Born Sep 12", "9/12/n/a", "--09-12", "0000-09-12",
                                 "09/12/1900", "Sep 12", "12-Sep", "9/12"])
def test_no_year_written_many_ways(real_schema, raw):
    assert bday(real_schema, raw) == ("09/12/1600", None)


def test_real_years_are_kept(real_schema):
    assert bday(real_schema, "DOB 9/12/1985") == ("09/12/1985", None)
    col = ["1985-06-01", "1990-03-14", "1972-07-04", "2001-12-25", "1999-01-02"]
    assert [bday(real_schema, v, col)[0] for v in col] == ["06/01/1985", "03/14/1990", "07/04/1972", "12/25/2001",
                                                          "01/02/1999"]


def test_year_added_by_excel_or_a_system_is_dropped(real_schema):
    # "9/12" typed into Excel is stored with the year it was typed; some systems use 1970 or 2000 for "unknown".
    for year in ("2025", "1970", "2000"):
        col = [f"{year}-09-12", f"{year}-03-14", f"{year}-07-04", f"{year}-12-25", f"{year}-01-02", "1985-06-01"]
        out = [bday(real_schema, v, col)[0] for v in col]
        assert out == ["09/12/1600", "03/14/1600", "07/04/1600", "12/25/1600", "01/02/1600", "06/01/1985"], year


def test_recent_or_future_year_is_dropped(real_schema):
    from datetime import date
    assert bday(real_schema, "12/25/2099") == ("12/25/1600", None)
    assert bday(real_schema, f"03/14/{date.today().year}") == ("03/14/1600", None)  # Excel added this year
    assert bday(real_schema, f"03/14/{date.today().year - 5}") == (f"03/14/{date.today().year - 5}", None)


def test_month_and_day_in_separate_columns(real_schema):
    df = pd.DataFrame({"phone": ["5165550100", "5165550111", "5165550122"],
                       "Birth Month": ["9", "September", "Sep"], "Birth Day": ["12", "3", "25"],
                       "Birth Year": ["", "1985", ""],
                       "anniversaryMonth": ["06", "", ""], "anniversaryDay": ["20", "", ""]})
    m = auto_map(real_schema, profile_columns(df))
    assert m.get("birthday").transform == "concat"
    assert m.get("birthday").sources == ["Birth Month", "Birth Day", "Birth Year"]
    assert m.get("anniversary").sources == ["anniversaryMonth", "anniversaryDay"]
    out = convert(df, real_schema, m).output
    assert out["birthday"].tolist() == ["09/12/1600", "09/03/1985", "09/25/1600"]
    assert out["anniversary"].tolist() == ["06/20/1600", "", ""]


def test_whole_birthday_column_is_not_split(real_schema):
    df = pd.DataFrame({"phone": ["5165550100"], "Birthday": ["9/12"], "Birth Day": ["9/12"]})
    m = auto_map(real_schema, profile_columns(df))
    assert m.get("birthday").transform == "copy"


def test_wrong_column_warning_still_fires_for_recent_dates(real_schema):
    from converter.checks import run_checks
    df = pd.DataFrame({"Phone": [f"516555{n:04d}" for n in range(100, 106)],
                       "Created": ["10/01/2026", "09/15/2026", "08/20/2025", "10/05/2026", "07/04/2026", "01/02/2026"]})
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Phone"], transform="copy"),
                        "birthday": FieldMapping(sources=["Created"], transform="copy")})
    res = convert(df, real_schema, m)
    findings = run_checks(real_schema, m, df, res.audit, profile_columns(df))
    assert any("within the last two years" in f.issue for f in findings)
