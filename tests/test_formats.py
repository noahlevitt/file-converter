"""Exhaustive date and phone format coverage against the real target format.

Target outputs: phone_number → 10 digits · activity columns → MM/DD/YYYY HH:MM AM/PM ·
birthday/anniversary → MM/DD/YYYY with 1600 for "no year".
"""

from datetime import date

import pytest

from converter import dateparse as DP
from converter import normalizers as N

TODAY = date(2026, 10, 9)


def norm(schema, field, value, column=None, tz=None):
    f = schema.field(field)
    ctx = N.prepare(f, column or [value], {})
    if tz:
        ctx["tz"] = tz
    return N.normalize(value, f, {}, ctx)


# ---------------------------------------------------------------------------
# Dates → activity columns (MM/DD/YYYY HH:MM AM/PM)
# ---------------------------------------------------------------------------

DATE_ONLY = "10/09/2026 12:00 AM"
DATETIME = "10/09/2026 03:45 PM"

ACTIVITY_CASES = {
    # numeric, month first (US)
    "10/09/2026": DATE_ONLY, "10/9/2026": DATE_ONLY, "10-09-2026": DATE_ONLY, "10-9-2026": DATE_ONLY,
    "10.09.2026": DATE_ONLY, "10 09 2026": DATE_ONLY, "10/09/26": DATE_ONLY, "10/9/26": DATE_ONLY,
    "10-9-26": DATE_ONLY, "10.9.26": DATE_ONLY,
    # year first
    "2026-10-09": DATE_ONLY, "2026/10/09": DATE_ONLY, "2026.10.09": DATE_ONLY, "2026-10-9": DATE_ONLY,
    "2026 10 09": DATE_ONLY,
    # compact digits
    "20261009": DATE_ONLY, "10092026": DATE_ONLY, "100926": DATE_ONLY, "261009": DATE_ONLY,
    # month names
    "October 9, 2026": DATE_ONLY, "October 9 2026": DATE_ONLY, "October 9th, 2026": DATE_ONLY,
    "October 9th 2026": DATE_ONLY, "Oct 9, 2026": DATE_ONLY, "Oct 9 2026": DATE_ONLY, "Oct. 9, 2026": DATE_ONLY,
    "Oct 09 2026": DATE_ONLY, "OCTOBER 9 2026": DATE_ONLY, "october 9 2026": DATE_ONLY,
    "9 October 2026": DATE_ONLY, "9, October, 2026": DATE_ONLY, "9 Oct 2026": DATE_ONLY,
    "9th October 2026": DATE_ONLY, "9th of October 2026": DATE_ONLY, "the 9th of October, 2026": DATE_ONLY,
    "09-Oct-2026": DATE_ONLY, "09-Oct-26": DATE_ONLY, "9-Oct-26": DATE_ONLY, "Oct-09-2026": DATE_ONLY,
    "Oct-9-26": DATE_ONLY, "2026-Oct-09": DATE_ONLY, "2026 October 9": DATE_ONLY, "Oct 9 '26": DATE_ONLY,
    "Oct. 9th, '26": DATE_ONLY, "9/Oct/2026": DATE_ONLY, "Oct/09/2026": DATE_ONLY,
    # weekdays
    "Friday, October 9, 2026": DATE_ONLY, "Fri, Oct 9, 2026": DATE_ONLY, "Fri Oct 9 2026": DATE_ONLY,
    "Fri. Oct. 9 2026": DATE_ONLY, "FRIDAY 10/09/2026": DATE_ONLY,
    # Spanish
    "9 de octubre de 2026": DATE_ONLY, "9 octubre 2026": DATE_ONLY, "viernes, 9 de octubre de 2026": DATE_ONLY,
    # times: 12-hour
    "10/09/2026 3:45 PM": DATETIME, "10/09/2026 3:45 pm": DATETIME, "10/09/2026 3:45PM": DATETIME,
    "10/09/2026 3:45pm": DATETIME, "10/09/2026 3:45 p.m.": DATETIME, "10/09/2026 3:45 P.M.": DATETIME,
    "10/09/2026 3:45p": DATETIME, "10/09/2026 03:45 PM": DATETIME, "10/09/2026 3:45:12 PM": DATETIME,
    "10/09/2026 3.45pm": DATETIME, "10/9/26 3:45 PM": DATETIME,
    # times: 24-hour, seconds, fractions
    "10/09/2026 15:45": DATETIME, "10/09/2026 15:45:12": DATETIME, "10/09/2026 15:45:12.123": DATETIME,
    "2026-10-09 15:45": DATETIME, "2026-10-09 15:45:00": DATETIME, "2026-10-09 15:45:00.123456": DATETIME,
    "10/09/2026 1545": DATETIME, "10/09/2026 15h45": DATETIME,
    # time words and positions
    "October 9, 2026 at 3:45 PM": DATETIME, "Oct 9, 2026, 3:45 PM": DATETIME, "10/09/2026 @ 3:45 PM": DATETIME,
    "3:45 PM 10/09/2026": DATETIME, "3:45 PM on October 9, 2026": DATETIME,
    "Friday, October 9, 2026 3:45 PM": DATETIME,
    "10/09/2026 3 PM": "10/09/2026 03:00 PM", "10/09/2026 3pm": "10/09/2026 03:00 PM",
    "10/09/2026 noon": "10/09/2026 12:00 PM", "10/09/2026 12 noon": "10/09/2026 12:00 PM",
    "10/09/2026 midnight": "10/09/2026 12:00 AM", "10/09/2026 12:00 AM": "10/09/2026 12:00 AM",
    "10/09/2026 12:30 AM": "10/09/2026 12:30 AM", "10/09/2026 12:30 PM": "10/09/2026 12:30 PM",
    "10/09/2026 00:30": "10/09/2026 12:30 AM", "10/09/2026 23:59": "10/09/2026 11:59 PM",
    # ISO and machine formats (no zone → as written)
    "2026-10-09T15:45": DATETIME, "2026-10-09T15:45:00": DATETIME, "2026-10-09T15:45:00.000": DATETIME,
    "202610091545": DATETIME, "20261009154500": DATETIME,
    # spreadsheet artifacts
    "46304": DATE_ONLY, "46304.65625": DATETIME, "46304,65625": DATETIME, '="10/09/2026"': DATE_ONLY, "'10/09/2026": DATE_ONLY,
    " 10/09/2026 ": DATE_ONLY, "10/09/2026 ": DATE_ONLY,
    # placeholders → blank, not an error
    "0000-00-00": "", "0000-00-00 00:00:00": "", "00/00/0000": "", "Never": "", "N/A": "", "NULL": "", "-": "",
}


@pytest.mark.parametrize("raw,expected", list(ACTIVITY_CASES.items()))
def test_activity_formats(real_schema, raw, expected):
    assert norm(real_schema, "transactions_last", raw) == (expected, None)


ZONED_CASES = {  # converted to the import's time zone (Eastern here)
    "2026-10-09T19:45:00Z": DATETIME,
    "2026-10-09T19:45:00.000Z": DATETIME,
    "2026-10-09T15:45:00-04:00": DATETIME,
    "2026-10-09T23:45:00+04:00": DATETIME,
    "2026-10-09 19:45:00 UTC": DATETIME,
    "2026-10-09 19:45 GMT": DATETIME,
    "Fri, 09 Oct 2026 19:45:00 +0000": DATETIME,
    "Fri, 09 Oct 2026 19:45:00 GMT": DATETIME,
    "Fri Oct 09 2026 15:45:00 GMT-0400 (Eastern Daylight Time)": DATETIME,
    "10/09/2026 3:45 PM EST": DATETIME,  # EST/EDT read as Eastern local time
    "10/09/2026 3:45 PM EDT": DATETIME,
    "10/09/2026 3:45 PM ET": DATETIME,
    "10/09/2026 2:45 PM CST": DATETIME,
    "10/09/2026 12:45 PM PT": DATETIME,
    "10/09/2026 12:45 PM PDT": DATETIME,
    "2026-10-09 19:45:00+00": DATETIME,
    "2026-10-09T19:45:00+0000": DATETIME,
    "20261009T194500Z": DATETIME,
    "10/09/2026 3:45 PM (EST)": DATETIME,
    "10/09/2026 1:45 PM (MDT)": DATETIME,
    "October 9, 2026 3:45 PM Eastern": DATETIME,
    "October 9, 2026 12:45 PM Pacific Time": DATETIME,
    "10/09/2026 2:45 PM Central Daylight Time": DATETIME,
    "10/09/2026 12:45 PM (Pacific Standard Time)": DATETIME,
    "Thu Oct 9 15:45:00 EDT 2026": DATETIME,
    "10/09/2026 3:45 PM GMT-4": DATETIME,
    "1791575100": DATETIME,        # Unix seconds
    "1791575100000": DATETIME,     # Unix milliseconds
}


@pytest.mark.parametrize("raw,expected", list(ZONED_CASES.items()))
def test_zoned_times_convert_to_import_zone(real_schema, raw, expected):
    assert norm(real_schema, "transactions_last", raw, tz="America/New_York") == (expected, None)


def test_zone_follows_import_setting(real_schema):
    assert norm(real_schema, "calls_last", "2026-10-09T19:45:00Z", tz="America/Denver")[0] == "10/09/2026 01:45 PM"
    assert norm(real_schema, "calls_last", "10/09/2026 3:45 PM", tz="America/Denver")[0] == DATETIME  # no zone


@pytest.mark.parametrize("raw", [
    "October 2026", "10/2026", "2026-10", "2026", "October", "today", "yesterday", "Q4 2026", "asap",
    "13/45/2026", "02/30/2026", "10/09/2026 25:00", "10/09/2026 3:75 PM", "10/09", "Oct 9", "hello world",
])
def test_activity_invalid(real_schema, raw):
    out, err = norm(real_schema, "transactions_last", raw)
    assert out == "" and err


def test_day_first_only_with_clear_evidence(real_schema):
    # A US column with one odd value stays month-first; that value is still read the only way it fits.
    us = ["10/09/2026", "11/03/2026", "13/10/2026"]
    assert norm(real_schema, "calls_last", "11/03/2026", us)[0] == "11/03/2026 12:00 AM"
    assert norm(real_schema, "calls_last", "13/10/2026", us)[0] == "10/13/2026 12:00 AM"
    # A clearly day-first column flips the ambiguous values.
    eu = ["15/03/2026", "28/02/2026", "01/04/2026"]
    assert norm(real_schema, "calls_last", "01/04/2026", eu)[0] == "04/01/2026 12:00 AM"


def test_two_digit_years():
    assert DP.parse("08/09/26", today=TODAY)[0].year == 2026
    assert DP.parse("08/09/85", today=TODAY)[0].year == 1985
    assert DP.parse("08/09/45", today=TODAY)[0].year == 1945   # 2045 is far in the future
    assert DP.parse("12/25/26", today=TODAY)[0].year == 2026   # within a year → this century
    assert DP.parse("01/01/00", today=TODAY)[0].year == 2000


# ---------------------------------------------------------------------------
# Dates → birthday / anniversary (MM/DD/YYYY, 1600 = no year)
# ---------------------------------------------------------------------------

BIRTHDAY_CASES = {
    "09/12/1985": "09/12/1985", "9/12/1985": "09/12/1985", "9/12/85": "09/12/1985", "1985-09-12": "09/12/1985",
    "19850912": "09/12/1985", "09121985": "09/12/1985", "Sep 12, 1985": "09/12/1985", "12 Sep 1985": "09/12/1985",
    "September 12th, 1985": "09/12/1985", "12-Sep-85": "09/12/1985", "1985-09-12 00:00:00": "09/12/1985",
    "Sep 12, 1985 12:00 AM": "09/12/1985", "31064": "01/17/1985",
    # no year → 1600
    "09/12": "09/12/1600", "9/12": "09/12/1600", "9-12": "09/12/1600", "09.12": "09/12/1600",
    "Sep 12": "09/12/1600", "Sept 12": "09/12/1600", "September 12": "09/12/1600", "September 12th": "09/12/1600",
    "12 Sep": "09/12/1600", "12th September": "09/12/1600", "12-Sep": "09/12/1600", "Sep-12": "09/12/1600",
    "12th of September": "09/12/1600", "0912": "09/12/1600", "12/31": "12/31/1600", "31/12": "12/31/1600",
    "Dec 31st": "12/31/1600", "02/29": "02/29/1600", "Feb 29": "02/29/1600",
    "12 de septiembre": "09/12/1600",
    # placeholder years that mean "unknown year" → 1600
    "09/12/1600": "09/12/1600", "01/15/1900": "01/15/1600", "0000-09-12": "09/12/1600",
    # placeholders → blank
    "0000-00-00": "", "00/00/0000": "", "N/A": "",
}


@pytest.mark.parametrize("raw,expected", list(BIRTHDAY_CASES.items()))
def test_birthday_formats(real_schema, raw, expected):
    assert norm(real_schema, "birthday", raw) == (expected, None)
    assert norm(real_schema, "anniversary", raw) == (expected, None)


@pytest.mark.parametrize("raw", ["1985", "September", "02/30", "13/13", "Feb 30", "1985-02-30", "sometime in May"])
def test_birthday_invalid(real_schema, raw):
    out, err = norm(real_schema, "birthday", raw)
    assert out == "" and err


# ---------------------------------------------------------------------------
# Phones → 10 digits, US only, +1 assumed
# ---------------------------------------------------------------------------

PHONE_OK = [
    "5165550100", "516-555-0100", "(516) 555-0100", "(516)555-0100", "516.555.0100", "516 555 0100",
    "516/555/0100", "516 555 01 00", "516-5550100", "516555-0100",
    "+15165550100", "+1 516 555 0100", "+1 (516) 555-0100", "+1-516-555-0100", "+1.516.555.0100",
    "1-516-555-0100", "1 (516) 555-0100", "1.516.555.0100", "15165550100", "1 516 555 0100", "(+1) 516-555-0100",
    "+5165550100",                      # "+" with the 1 left out
    "001 516 555 0100", "0015165550100", "011 1 516 555 0100", "+011 1 516-555-0100",
    "516-555-0100 ext. 12", "516-555-0100 ext 12", "516-555-0100 x12", "516-555-0100 x 12", "516-555-0100 #12",
    "(516) 555-0100 ext 1234", "516-555-0100;ext=12", "516-555-0100 extension 9",
    "tel:+15165550100", "sms:5165550100", "Phone: 516-555-0100", "Cell: (516) 555-0100",
    "516-555-0100 (cell)", "516-555-0100 mobile", "Mobile 516.555.0100",
    "H: 516-555-1212 C: 516-555-0100", "Home 516-555-1212 / Cell 516-555-0100",
    "Fax: 516-555-1212, Main: 516-555-0100", "Work: 516-555-1212 Mobile: 516-555-0100",
    "516-555-0100 / 516-555-1212", "516-555-0100, 516-555-1212", "5165550100;5165551212", "5165550100 or 5165551212",
    "5165550100.0", "5.165550100E+09", "5.165550100e9", '="5165550100"', "'5165550100",
    "５１６－５５５－０１００", "516–555–0100", "516 555 0100", " 516-555-0100 ",
]


@pytest.mark.parametrize("raw", PHONE_OK)
def test_phone_formats(real_schema, raw):
    assert norm(real_schema, "phone_number", raw) == ("5165550100", None)


def test_vanity_and_toll_free(real_schema):
    assert norm(real_schema, "phone_number", "1-800-FLOWERS") == ("8003569377", None)
    assert norm(real_schema, "phone_number", "516-CALL-NOW") == ("5162255669", None)
    assert norm(real_schema, "phone_number", "(800) 356-9377") == ("8003569377", None)


def test_us_territories_allowed(real_schema):
    assert norm(real_schema, "phone_number", "(787) 555-0100") == ("7875550100", None)  # Puerto Rico


@pytest.mark.parametrize("raw,reason", [
    ("555-0100", "missing area code"),
    ("555-1234", "missing area code"),
    ("51655501", "too few digits"),
    ("(516) 555-010", "too few digits"),
    ("516555010066", "too many digits"),
    ("555-555-5555", "not a real US number"),
    ("123-456-7890", "not a real US number"),
    ("(000) 000-0000", "not a real US number"),
    ("1111111111", "not a real US number"),
    ("+44 20 7946 0958", "not a US number"),
    ("+1 416 555 0123", "not a US number"),   # Canada
    ("416-555-0123", "not a US number"),      # Canada without +1
    ("+52 55 1234 5678", "not a US number"),
    ("5.163E+09", "scientific notation"),
    ("call me", "no phone number"),
])
def test_phone_rejected(real_schema, raw, reason):
    out, err = norm(real_schema, "phone_number", raw)
    assert out == "" and reason in err


@pytest.mark.parametrize("raw", ["", "N/A", "none", "null", "-", "   "])
def test_phone_empty_values(real_schema, raw):
    assert norm(real_schema, "phone_number", raw) == ("", None)  # engine then rejects the row as missing
