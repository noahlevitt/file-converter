"""Exhaustive date and phone format coverage against the real target format.

Target outputs: phone_number → 10 digits · activity columns → MM/DD/YYYY HH:MM AM/PM ·
birthday/anniversary → MM/DD/YYYY with 1600 for "no year".

Files come from many POS systems, CRMs and hand-edited spreadsheets, each with its own way of writing a date or a
phone. Activity timestamps drive who gets which campaign (e.g. "visited in the last 30 days"), so a misread date
targets the wrong people; a misread phone number texts a stranger. Anything that can't be read safely must be an
error with a reason, never a guess. Every activity date case below is Friday 9 October 2026 and every accepted
phone spelling in PHONE_OK is (516) 555-0100, so a single expected value per group shows that all spellings agree (birthday,
vanity, territory and rejected cases use their own values).
"""

from datetime import date

import pytest

from converter import dateparse as DP
from converter import normalizers as N

# Fixed "today" for the two-digit-year tests, so their meaning doesn't change as the calendar moves on.
TODAY = date(2026, 10, 9)


def norm(schema, field, value, column=None, tz=None):
    """Normalize one value into a real-schema field and return (output, error).

    `column` stands in for the rest of the source column (used for day-first detection and placeholder years);
    `tz` is the import's time zone, as chosen on the import screen.
    """
    f = schema.field(field)
    ctx = N.prepare(f, column or [value], {})
    if tz:
        ctx["tz"] = tz
    return N.normalize(value, f, {}, ctx)


# ---------------------------------------------------------------------------
# Dates → activity columns (MM/DD/YYYY HH:MM AM/PM)
# ---------------------------------------------------------------------------

# Date-only inputs become midnight; most inputs with a time are 3:45 PM (other times are listed explicitly).
DATE_ONLY = "10/09/2026 12:00 AM"
DATETIME = "10/09/2026 03:45 PM"

# Spellings of 9 Oct 2026 (and 3:45 PM) seen in real exports, grouped by style.
ACTIVITY_CASES = {
    # numeric, month first (US)
    "10/09/2026": DATE_ONLY, "10/9/2026": DATE_ONLY, "10-09-2026": DATE_ONLY, "10-9-2026": DATE_ONLY,
    "10.09.2026": DATE_ONLY, "10 09 2026": DATE_ONLY, "10/09/26": DATE_ONLY, "10/9/26": DATE_ONLY,
    "10-9-26": DATE_ONLY, "10.9.26": DATE_ONLY,
    # year first
    "2026-10-09": DATE_ONLY, "2026/10/09": DATE_ONLY, "2026.10.09": DATE_ONLY, "2026-10-9": DATE_ONLY,
    "2026 10 09": DATE_ONLY,
    # compact digits (8 or 6 digits with no separators; the six-digit forms are read MMDDYY and YYMMDD)
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
    # spreadsheet artifacts: 46304 is Excel's serial day number for 10/09/2026 and .65625 of a day is 3:45 PM
    # (European exports write it with a decimal comma); ="..." and a leading apostrophe are Excel's ways of forcing
    # a cell to text; stray spaces come from copy-paste.
    "46304": DATE_ONLY, "46304.65625": DATETIME, "46304,65625": DATETIME, '="10/09/2026"': DATE_ONLY, "'10/09/2026": DATE_ONLY,
    " 10/09/2026 ": DATE_ONLY, "10/09/2026 ": DATE_ONLY,
    # placeholders → blank, not an error (systems write these for "never happened"; the row still imports)
    "0000-00-00": "", "0000-00-00 00:00:00": "", "00/00/0000": "", "Never": "", "N/A": "", "NULL": "", "-": "",
}


@pytest.mark.parametrize("raw,expected", list(ACTIVITY_CASES.items()))
def test_activity_formats(real_schema, raw, expected):
    """Every spelling of the same moment becomes the exact MM/DD/YYYY HH:MM AM/PM text the import requires."""
    assert norm(real_schema, "transactions_last", raw) == (expected, None)


# The same instant, 3:45 PM Eastern daylight time (19:45 UTC), written with a UTC marker, a numeric offset, a zone
# name or a zone abbreviation. Times in other US zones are earlier on the clock but the same moment.
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
    # 1791575100 seconds after 1970-01-01 UTC is 2026-10-09 19:45 UTC.
    "1791575100": DATETIME,        # Unix seconds
    "1791575100000": DATETIME,     # Unix milliseconds
}


@pytest.mark.parametrize("raw,expected", list(ZONED_CASES.items()))
def test_zoned_times_convert_to_import_zone(real_schema, raw, expected):
    """Timestamps that carry a zone are converted to the import's time zone.

    A cloud POS that stores UTC would otherwise put an evening visit on the next day.
    """
    assert norm(real_schema, "transactions_last", raw, tz="America/New_York") == (expected, None)


def test_zone_follows_import_setting(real_schema):
    """The target zone is the import's own setting (Denver here); a timestamp without a zone is kept as written."""
    assert norm(real_schema, "calls_last", "2026-10-09T19:45:00Z", tz="America/Denver")[0] == "10/09/2026 01:45 PM"
    assert norm(real_schema, "calls_last", "10/09/2026 3:45 PM", tz="America/Denver")[0] == DATETIME  # no zone


@pytest.mark.parametrize("raw", [
    # missing the day (or more), and relative or vague words that depend on when the file was made
    "October 2026", "10/2026", "2026-10", "2026", "October", "today", "yesterday", "Q4 2026", "asap",
    # impossible dates and times, month/day with no year (an activity needs one), and plain text
    "13/45/2026", "02/30/2026", "10/09/2026 25:00", "10/09/2026 3:75 PM", "10/09", "Oct 9", "hello world",
])
def test_activity_invalid(real_schema, raw):
    """Values that aren't a complete, real date are errors with a blank output rather than a guessed date."""
    out, err = norm(real_schema, "transactions_last", raw)
    assert out == "" and err


def test_day_first_only_with_clear_evidence(real_schema):
    """Columns are read month-first (US) unless the column as a whole is clearly day-first.

    One impossible-as-US value doesn't flip a whole US column; it is just read the only way it fits.
    """
    # A US column with one odd value stays month-first; that value is still read the only way it fits.
    us = ["10/09/2026", "11/03/2026", "13/10/2026"]
    assert norm(real_schema, "calls_last", "11/03/2026", us)[0] == "11/03/2026 12:00 AM"
    assert norm(real_schema, "calls_last", "13/10/2026", us)[0] == "10/13/2026 12:00 AM"
    # A clearly day-first column flips the ambiguous values.
    eu = ["15/03/2026", "28/02/2026", "01/04/2026"]
    assert norm(real_schema, "calls_last", "01/04/2026", eu)[0] == "04/01/2026 12:00 AM"


def test_two_digit_years():
    """A two-digit year takes the century that keeps the date from being far in the future (relative to TODAY)."""
    assert DP.parse("08/09/26", today=TODAY)[0].year == 2026
    assert DP.parse("08/09/85", today=TODAY)[0].year == 1985
    assert DP.parse("08/09/45", today=TODAY)[0].year == 1945   # 2045 is far in the future
    assert DP.parse("12/25/26", today=TODAY)[0].year == 2026   # within a year → this century
    assert DP.parse("01/01/00", today=TODAY)[0].year == 2000


# ---------------------------------------------------------------------------
# Dates → birthday / anniversary (MM/DD/YYYY, 1600 = no year)
# ---------------------------------------------------------------------------

BIRTHDAY_CASES = {
    # full dates keep their year; 31064 is Excel's serial day number for 01/17/1985
    "09/12/1985": "09/12/1985", "9/12/1985": "09/12/1985", "9/12/85": "09/12/1985", "1985-09-12": "09/12/1985",
    "19850912": "09/12/1985", "09121985": "09/12/1985", "Sep 12, 1985": "09/12/1985", "12 Sep 1985": "09/12/1985",
    "September 12th, 1985": "09/12/1985", "12-Sep-85": "09/12/1985", "1985-09-12 00:00:00": "09/12/1985",
    "Sep 12, 1985 12:00 AM": "09/12/1985", "31064": "01/17/1985",
    # no year → 1600 ("31/12" can only be day-first; Feb 29 must survive without a year to check it against)
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
    """Birthday and anniversary follow the same rules: real years kept, missing or placeholder years become 1600."""
    assert norm(real_schema, "birthday", raw) == (expected, None)
    assert norm(real_schema, "anniversary", raw) == (expected, None)


@pytest.mark.parametrize("raw", ["1985", "September", "02/30", "13/13", "Feb 30", "1985-02-30", "sometime in May"])
def test_birthday_invalid(real_schema, raw):
    """A year alone, a month alone, impossible days (Feb 30, 13/13) and vague text are errors, not guesses."""
    out, err = norm(real_schema, "birthday", raw)
    assert out == "" and err


# ---------------------------------------------------------------------------
# Phones → 10 digits, US only, +1 assumed
# ---------------------------------------------------------------------------

# Spellings of (516) 555-0100 seen in real exports, grouped by style.
PHONE_OK = [
    # separators
    "5165550100", "516-555-0100", "(516) 555-0100", "(516)555-0100", "516.555.0100", "516 555 0100",
    "516/555/0100", "516 555 01 00", "516-5550100", "516555-0100",
    # US country code written with "+1", "1" or "(+1)"
    "+15165550100", "+1 516 555 0100", "+1 (516) 555-0100", "+1-516-555-0100", "+1.516.555.0100",
    "1-516-555-0100", "1 (516) 555-0100", "1.516.555.0100", "15165550100", "1 516 555 0100", "(+1) 516-555-0100",
    "+5165550100",                      # "+" with the 1 left out
    "+1 +15165550100", "+1 +5165550100", "+1 +51 65 550100",  # +1 put in front of a number that had one already
    # international dialing prefixes (00 and 011) in front of the US country code
    "001 516 555 0100", "0015165550100", "011 1 516 555 0100", "+011 1 516-555-0100",
    # extensions are dropped (a text goes to the line, not the extension)
    "516-555-0100 ext. 12", "516-555-0100 ext 12", "516-555-0100 x12", "516-555-0100 x 12", "516-555-0100 #12",
    "(516) 555-0100 ext 1234", "516-555-0100;ext=12", "516-555-0100 extension 9",
    # link prefixes and labels around the number
    "tel:+15165550100", "sms:5165550100", "Phone: 516-555-0100", "Cell: (516) 555-0100",
    "516-555-0100 (cell)", "516-555-0100 mobile", "Mobile 516.555.0100",
    # several numbers in one cell: a labelled cell/mobile/main number wins over home, work and fax;
    # an unlabelled list takes the first number
    "H: 516-555-1212 C: 516-555-0100", "Home 516-555-1212 / Cell 516-555-0100",
    "Fax: 516-555-1212, Main: 516-555-0100", "Work: 516-555-1212 Mobile: 516-555-0100",
    "516-555-0100 / 516-555-1212", "516-555-0100, 516-555-1212", "5165550100;5165551212", "5165550100 or 5165551212",
    # spreadsheet artifacts: a float ".0", scientific notation that still holds all 10 digits (safe to expand),
    # and Excel's force-as-text forms
    "5165550100.0", "5.165550100E+09", "5.165550100e9", '="5165550100"', "'5165550100",
    # unusual characters: full-width digits and dashes (Asian keyboard input), en dashes, non-breaking spaces,
    # and spaces around the value
    "５１６－５５５－０１００", "516–555–0100", "516 555 0100", " 516-555-0100 ",
]


@pytest.mark.parametrize("raw", PHONE_OK)
def test_phone_formats(real_schema, raw):
    """Every spelling gives the same 10 digits, so the right person is texted and isn't imported twice."""
    assert norm(real_schema, "phone_number", raw) == ("5165550100", None)


def test_vanity_and_toll_free(real_schema):
    """Letters are turned into keypad digits (FLOWERS → 3569377) and toll-free numbers are accepted."""
    assert norm(real_schema, "phone_number", "1-800-FLOWERS") == ("8003569377", None)
    assert norm(real_schema, "phone_number", "516-CALL-NOW") == ("5162255669", None)
    assert norm(real_schema, "phone_number", "(800) 356-9377") == ("8003569377", None)


def test_plus_one_in_front_of_plus_number(real_schema):
    """A US number mangled into a Peru-looking '+51' form and then given '+1' again is recovered, not rejected."""
    # A Long Island number stored as "+5163171059", shown Peru-style as "+51 63 171059", then given "+1 " again.
    assert norm(real_schema, "phone_number", "+1 +51 63 171059") == ("5163171059", None)


def test_us_territories_allowed(real_schema):
    """US territories on the +1 plan, such as Puerto Rico (787), count as US numbers."""
    assert norm(real_schema, "phone_number", "(787) 555-0100") == ("7875550100", None)  # Puerto Rico


@pytest.mark.parametrize("raw,reason", [
    # wrong length (a lone 7-digit number has no other numbers in its column to borrow an area code from)
    ("555-0100", "missing area code"),
    ("555-1234", "missing area code"),
    ("51655501", "too few digits"),
    ("(516) 555-010", "too few digits"),
    ("516555010066", "too many digits"),
    # fake or placeholder numbers people type into forms
    ("555-555-5555", "not a real US number"),
    ("123-456-7890", "not a real US number"),
    ("(000) 000-0000", "not a real US number"),
    ("1111111111", "not a real US number"),
    # foreign numbers; Canada shares +1 and looks exactly like a US number, so it is caught by area code. A lone
    # 514 number has no local 516 numbers around it to suggest a typo (that repair is in test_phone_repair.py).
    ("+44 20 7946 0958", "not a US number"),
    ("+1 416 555 0123", "not a US number"),   # Canada
    ("416-555-0123", "not a US number"),      # Canada without +1
    ("+52 55 1234 5678", "not a US number"),
    ("5149689696", "not a US number — Canada (area code 514, Quebec)"),
    ("+51 1 4567890", "not a US number — Peru (+51, Lima"),           # real Lima number, not a US one missing "1"
    # a foreign number that a system prefixed with +1
    ("+1 +44 20 7946 0958", "the file put +1 in front of its own country code"),
    ("+1 +51694619661", "has two country codes (+1 and +51)"),
    # right length but not a working US number
    ("+1 (433) 309-8420", "area code 433 isn't in use"),
    ("(516) 155-0100", "isn't a working exchange in area code 516"),
    # Excel shows a 10-digit number in a General cell as 5.163E+09; saved like that only 4 digits are left and
    # the real number can't be recovered.
    ("5.163E+09", "scientific notation"),
    ("call me", "no phone number"),
])
def test_phone_rejected(real_schema, raw, reason):
    """Values that aren't a real, textable US number are rejected with a reason a person can act on.

    Importing them would text someone abroad, a fake number, or the wrong person.
    """
    out, err = norm(real_schema, "phone_number", raw)
    assert out == "" and reason in err


@pytest.mark.parametrize("raw", ["", "N/A", "none", "null", "-", "   "])
def test_phone_empty_values(real_schema, raw):
    """Blank and placeholder phones give an empty value without a format error; the row is then 'missing'."""
    assert norm(real_schema, "phone_number", raw) == ("", None)  # engine then rejects the row as missing
