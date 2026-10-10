"""Birthdays and anniversaries with no real year always come out as MM/DD/1600.

The import system reads year 1600 as "no year". Birthday and anniversary texts are sent on the month and day, and
any year we invent (the year Excel added when someone typed "9/12", or a 1970/2000 "unknown" default) would show
up as a wrong age or a wrong number of years married. Real years must be kept, and nothing may truncate the value.
"""

import pandas as pd
import pytest

from converter import normalizers as N
from converter.engine import convert
from converter.heuristics import auto_map
from converter.mapping import FieldMapping, Mapping, profile_columns


def bday(real_schema, value, column=None):
    """Normalize one value into the real birthday field and return (output, error).

    `column` stands in for the whole source column (default: just this value), because the placeholder-year
    rule looks at the other values in the column.
    """
    f = real_schema.field("birthday")
    return N.normalize(value, f, {}, N.prepare(f, column or [value], {}))


# Ways exports write "September 12, no year": a year of zeros, X's or y's left by an export template, a label in
# front ("Birthday:", "DOB", "Born"), "n/a" in the year position, the ISO no-year form "--09-12", 1900 used as an
# "unknown" default, and plain month/day with names or numbers.
@pytest.mark.parametrize("raw", ["09/12/0000", "09-12-0000", "09/12/XXXX", "XXXX-09-12", "09/12/yyyy", "YYYY-09-12",
                                 "Birthday: 9/12", "DOB 9/12", "Born Sep 12", "9/12/n/a", "--09-12", "0000-09-12",
                                 "09/12/1900", "Sep 12", "12-Sep", "9/12"])
def test_no_year_written_many_ways(real_schema, raw):
    """Every way of writing a birthday without a real year becomes 09/12/1600, never an error or a guessed year."""
    assert bday(real_schema, raw) == ("09/12/1600", None)


def test_real_years_are_kept(real_schema):
    """A genuine birth year is kept, with or without a label; a column of varied years is not a placeholder."""
    assert bday(real_schema, "DOB 9/12/1985") == ("09/12/1985", None)
    # All different years, so none of them can be mistaken for a placeholder year shared by the column.
    col = ["1985-06-01", "1990-03-14", "1972-07-04", "2001-12-25", "1999-01-02"]
    assert [bday(real_schema, v, col)[0] for v in col] == ["06/01/1985", "03/14/1990", "07/04/1972", "12/25/2001",
                                                          "01/02/1999"]


def test_year_added_by_excel_or_a_system_is_dropped(real_schema):
    """A year shared by most of the column is a placeholder and becomes 1600; a different real year (1985) stays."""
    # "9/12" typed into Excel is stored with the year it was typed; some systems use 1970 or 2000 for "unknown".
    for year in ("2025", "1970", "2000"):
        col = [f"{year}-09-12", f"{year}-03-14", f"{year}-07-04", f"{year}-12-25", f"{year}-01-02", "1985-06-01"]
        out = [bday(real_schema, v, col)[0] for v in col]
        assert out == ["09/12/1600", "03/14/1600", "07/04/1600", "12/25/1600", "01/02/1600", "06/01/1985"], year


def test_recent_or_future_year_is_dropped(real_schema):
    """No customer's birthday is in the future or the last two years, so such years become 1600.

    A year five years back is outside that window and is kept.
    """
    from datetime import date
    assert bday(real_schema, "12/25/2099") == ("12/25/1600", None)
    assert bday(real_schema, f"03/14/{date.today().year}") == ("03/14/1600", None)  # Excel added this year
    assert bday(real_schema, f"03/14/{date.today().year - 5}") == (f"03/14/{date.today().year - 5}", None)


def test_month_and_day_in_separate_columns(real_schema):
    """Separate month/day(/year) columns are auto-mapped as one concat field and combined into a date.

    The month can be a number, a name or an abbreviation; a blank year gives 1600, a filled one is kept, and a
    blank anniversary stays blank rather than becoming a made-up 1600 date.
    """
    # Only the second customer has a birth year; only the first has an anniversary.
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
    """A full 'Birthday' column is copied as-is; a lookalike 'Birth Day' header must not trigger a month/day concat.

    'Birth Day' here holds a whole date, so combining it with another column would garble the value.
    """
    df = pd.DataFrame({"phone": ["5165550100"], "Birthday": ["9/12"], "Birth Day": ["9/12"]})
    m = auto_map(real_schema, profile_columns(df))
    assert m.get("birthday").transform == "copy"


def test_wrong_column_warning_still_fires_for_recent_dates(real_schema):
    """Recent years are quietly turned into 1600, but a birthday column full of recent dates must still be flagged.

    Otherwise a record-creation column mapped by mistake would import everyone's sign-up day as their birthday.
    """
    from converter.checks import run_checks
    # 'Created' holds 2025-2026 record dates: plausible as sign-up dates, impossible as birthdays.
    df = pd.DataFrame({"Phone": [f"516555{n:04d}" for n in range(100, 106)],
                       "Created": ["10/01/2026", "09/15/2026", "08/20/2025", "10/05/2026", "07/04/2026", "01/02/2026"]})
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Phone"], transform="copy"),
                        "birthday": FieldMapping(sources=["Created"], transform="copy")})
    res = convert(df, real_schema, m)
    findings = run_checks(real_schema, m, df, res.audit, profile_columns(df))
    assert any("within the last two years" in f.issue for f in findings)


def test_year_written_without_control_characters():
    """Regression test for birthdays coming out as '10/18/' on a Mac: the year must be put in as plain text.

    StrictMac reproduces the macOS behaviour on any OS, so this fails on Linux CI too if a NUL marker comes back.
    """
    # macOS strftime stops at a NUL byte; the year must never be inserted with one (it produced "10/18/").
    from datetime import datetime

    class StrictMac(datetime):
        """A datetime whose strftime refuses NUL bytes, mimicking macOS where strftime stops at a NUL."""

        def strftime(self, fmt):
            """Fail the test if the format contains a NUL, otherwise format normally."""
            assert "\x00" not in fmt, "strftime would truncate at the NUL on macOS"
            return super().strftime(fmt)

    # 1600 + 400 = 2000; the second case also covers %y, a literal %% and Feb 29 (2000 is a leap year).
    assert N._strftime_year(StrictMac(1600 + 400, 10, 18), "%m/%d/%Y", "1600") == "10/18/1600"
    assert N._strftime_year(StrictMac(2000, 2, 29), "%Y-%m-%d (%y) 100%%", "1600") == "1600-02-29 (00) 100%"
