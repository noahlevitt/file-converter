"""Field normalizers in isolation, using hand-built FieldSpecs instead of a schema file.

These are the building blocks every schema uses: phone parsing and output formats, date order, ZIP, state, email,
boolean/enum/number and string rules, and null tokens. A phone must be a real US number or the row is rejected
(a wrong number texts a stranger); a bad optional value such as a ZIP or email is reported rather than imported.
"""

import pytest

from converter import normalizers as N
from converter.schema import FieldSpec


def norm(field, value, values=None, params=None):
    """Normalize one value for `field` and return (output, error).

    `values` stands in for the whole source column (default: just this value), because rules such as day-first
    detection look at the column; `params` are the mapping's transform options.
    """
    params = params or {}
    ctx = N.prepare(field, values if values is not None else [value], params)
    return N.normalize(value, field, params, ctx)


# A required phone written in E.164 (+15165550100), like the example schema's phone field.
PHONE = FieldSpec(name="phone", type="phone", format="E164", required=True)


@pytest.mark.parametrize("raw", [
    "5165550100", "516-555-0100", "(516) 555-0100", "+1 516 555 0100", "1-516-555-0100", "516.555.0100",
    # Excel float and scientific forms that still hold all 10 digits, an extension, and a second number
    "5165550100.0", "5.165550100E+09", "(516) 555-0100 ext 12", "516-555-0100 / 516-555-1212",
])
def test_phone_valid_variants(raw):
    """Common US spellings all give the same E.164 number."""
    assert norm(PHONE, raw) == ("+15165550100", None)


@pytest.mark.parametrize("raw,reason", [
    ("555-1234", "missing area code"),
    # Excel kept only 4 significant digits; the rest of the number is gone and can't be recovered.
    ("5.163E+09", "scientific notation"),
    ("555-555-5555", "not a real US number"),
    ("+44 20 7946 0958", "not a US number"),
    ("call me", "no phone number"),
])
def test_phone_invalid(raw, reason):
    """Phones that can't be read safely are rejected with a readable reason instead of a guessed number."""
    out, err = norm(PHONE, raw)
    assert out == "" and reason in err


@pytest.mark.parametrize("fmt,expected", [
    ("digits10", "5165550100"), ("digits11", "15165550100"), ("national", "(516) 555-0100"),
    ("dashed", "516-555-0100"),
])
def test_phone_output_formats(fmt, expected):
    """Each supported phone output format writes the same number in its shape, to match any import system."""
    f = FieldSpec(name="p", type="phone", format=fmt)
    assert norm(f, "516 555 0100") == (expected, None)


# A US date field (MM/DD/YYYY).
DATE = FieldSpec(name="d", type="date", format="%m/%d/%Y")


def test_date_month_first_default():
    """Ambiguous dates are read month-first (US) when nothing in the column says otherwise."""
    assert norm(DATE, "03/05/2026", ["03/05/2026", "12/01/2026"]) == ("03/05/2026", None)


def test_date_day_first_inferred_from_column():
    """A column with clearly day-first values (15/03, 28/02) turns the ambiguous 01/04 into April 1."""
    values = ["15/03/2026", "28/02/2026", "01/04/2026"]
    assert norm(DATE, "01/04/2026", values) == ("04/01/2026", None)


def test_date_explicit_order_param():
    """A date order chosen in the mapping overrides inference, even when the column gives no evidence."""
    assert norm(DATE, "01/04/2026", ["01/04/2026"], {"date_order": "DMY"}) == ("04/01/2026", None)


# ISO with and without a time, month names, an ordinal day, UTC ISO, and 46086 (Excel's serial number for
# 03/05/2026).
@pytest.mark.parametrize("raw", ["2026-03-05", "2026-03-05 00:00:00", "March 5, 2026", "Mar 5th 2026",
                                 "5 Mar 2026", "2026-03-05T14:22:01Z", "46086"])
def test_date_formats(raw):
    """Common date spellings, including an Excel serial number, all give the same date."""
    assert norm(DATE, raw, [raw]) == ("03/05/2026", None)


@pytest.mark.parametrize("raw", ["13/45/1999", "March 2026", "hello", "1234"])
def test_date_invalid(raw):
    """Impossible dates, a month without a day, text and a bare small number are errors, not dates."""
    out, err = norm(DATE, raw, [raw])
    assert out == "" and err


def test_date_no_year_output_accepts_month_day():
    """When the output format has no year, a month/day value is complete on its own."""
    f = FieldSpec(name="b", type="date", format="%m/%d")
    assert norm(f, "3/14", ["3/14"]) == ("03/14", None)


def test_zip():
    """ZIPs: a leading zero lost in Excel comes back, ZIP+4 and '.0' are cut to 5 digits, zip5_4 adds the dash.

    Non-numeric ZIPs are errors.
    """
    f = FieldSpec(name="z", type="zip")
    assert norm(f, "7030") == ("07030", None)
    assert norm(f, "07030-1234") == ("07030", None)
    assert norm(f, "11801.0") == ("11801", None)
    assert norm(FieldSpec(name="z", type="zip", format="zip5_4"), "070301234") == ("07030-1234", None)
    assert norm(f, "ABCDE")[1]


def test_state():
    """State names and abbreviations in any case, with or without dots, become the 2-letter code; unknowns fail."""
    f = FieldSpec(name="s", type="state")
    for raw in ["NY", "ny", "New York", "N.Y.", "new york"]:
        assert norm(f, raw) == ("NY", None)
    assert norm(f, "Atlantis")[1]


def test_email():
    """Emails are lowercased and lose a 'mailto:' prefix; an address without a full domain is an error."""
    f = FieldSpec(name="e", type="email")
    assert norm(f, "JOHN@GMAIL.COM") == ("john@gmail.com", None)
    assert norm(f, "mailto:a@b.com") == ("a@b.com", None)
    assert norm(f, "a@b")[1]


def test_boolean_enum_numbers():
    """Booleans, enums and numbers are normalized to the schema's own values.

    Yes/opt-out words map to the schema's true/false values, enums match case-insensitively and reject unknown
    values, and numbers lose thousands separators and currency symbols.
    """
    b = FieldSpec(name="b", type="boolean", true_value="Y", false_value="N")
    assert norm(b, "yes") == ("Y", None) and norm(b, "Unsubscribed") == ("N", None)
    e = FieldSpec(name="e", type="enum", allowed_values=["Gold", "Silver"])
    assert norm(e, "gold") == ("Gold", None) and norm(e, "bronze")[1]
    assert norm(FieldSpec(name="i", type="integer"), "1,200") == ("1200", None)
    assert norm(FieldSpec(name="d", type="decimal"), "$1,200.50") == ("1200.5", None)


def test_string_case_and_length():
    """Name casing handles 'Mc' names and truncates when allowed; without truncate, a too-long value is an error."""
    f = FieldSpec(name="n", type="string", case="name", max_length=5, truncate=True)
    assert norm(f, "MCDONALD") == ("McDon", None)
    g = FieldSpec(name="n", type="string", max_length=3)
    assert norm(g, "abcd")[1]


def test_null_tokens_are_empty():
    """'N/A' and whitespace mean no value (empty, no error), so the required check fires rather than a format error."""
    assert norm(PHONE, "N/A") == ("", None)
    assert norm(PHONE, "  ") == ("", None)
