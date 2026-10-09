import pytest

from converter import normalizers as N
from converter.schema import FieldSpec


def norm(field, value, values=None, params=None):
    params = params or {}
    ctx = N.prepare(field, values if values is not None else [value], params)
    return N.normalize(value, field, params, ctx)


PHONE = FieldSpec(name="phone", type="phone", format="E164", required=True)


@pytest.mark.parametrize("raw", [
    "5165550100", "516-555-0100", "(516) 555-0100", "+1 516 555 0100", "1-516-555-0100", "516.555.0100",
    "5165550100.0", "5.165550100E+09", "(516) 555-0100 ext 12", "516-555-0100 / 516-555-1212",
])
def test_phone_valid_variants(raw):
    assert norm(PHONE, raw) == ("+15165550100", None)


@pytest.mark.parametrize("raw,reason", [
    ("555-1234", "too few digits"),
    ("5.163E+09", "scientific notation"),
    ("555-555-5555", "invalid phone"),
    ("+44 20 7946 0958", "not an allowed country"),
    ("call me", "no phone number"),
])
def test_phone_invalid(raw, reason):
    out, err = norm(PHONE, raw)
    assert out == "" and reason in err


@pytest.mark.parametrize("fmt,expected", [
    ("digits10", "5165550100"), ("digits11", "15165550100"), ("national", "(516) 555-0100"),
    ("dashed", "516-555-0100"),
])
def test_phone_output_formats(fmt, expected):
    f = FieldSpec(name="p", type="phone", format=fmt)
    assert norm(f, "516 555 0100") == (expected, None)


DATE = FieldSpec(name="d", type="date", format="%m/%d/%Y")


def test_date_month_first_default():
    assert norm(DATE, "03/05/2026", ["03/05/2026", "12/01/2026"]) == ("03/05/2026", None)


def test_date_day_first_inferred_from_column():
    values = ["15/03/2026", "01/04/2026"]
    assert norm(DATE, "01/04/2026", values) == ("04/01/2026", None)


def test_date_explicit_order_param():
    assert norm(DATE, "01/04/2026", ["01/04/2026"], {"date_order": "DMY"}) == ("04/01/2026", None)


@pytest.mark.parametrize("raw", ["2026-03-05", "2026-03-05 00:00:00", "March 5, 2026", "Mar 5th 2026",
                                 "5 Mar 2026", "2026-03-05T14:22:01Z", "46086"])
def test_date_formats(raw):
    assert norm(DATE, raw, [raw]) == ("03/05/2026", None)


@pytest.mark.parametrize("raw", ["13/45/1999", "March 2026", "hello", "1234"])
def test_date_invalid(raw):
    out, err = norm(DATE, raw, [raw])
    assert out == "" and err


def test_date_no_year_output_accepts_month_day():
    f = FieldSpec(name="b", type="date", format="%m/%d")
    assert norm(f, "3/14", ["3/14"]) == ("03/14", None)


def test_zip():
    f = FieldSpec(name="z", type="zip")
    assert norm(f, "7030") == ("07030", None)
    assert norm(f, "07030-1234") == ("07030", None)
    assert norm(f, "11801.0") == ("11801", None)
    assert norm(FieldSpec(name="z", type="zip", format="zip5_4"), "070301234") == ("07030-1234", None)
    assert norm(f, "ABCDE")[1]


def test_state():
    f = FieldSpec(name="s", type="state")
    for raw in ["NY", "ny", "New York", "N.Y.", "new york"]:
        assert norm(f, raw) == ("NY", None)
    assert norm(f, "Atlantis")[1]


def test_email():
    f = FieldSpec(name="e", type="email")
    assert norm(f, "JOHN@GMAIL.COM") == ("john@gmail.com", None)
    assert norm(f, "mailto:a@b.com") == ("a@b.com", None)
    assert norm(f, "a@b")[1]


def test_boolean_enum_numbers():
    b = FieldSpec(name="b", type="boolean", true_value="Y", false_value="N")
    assert norm(b, "yes") == ("Y", None) and norm(b, "Unsubscribed") == ("N", None)
    e = FieldSpec(name="e", type="enum", allowed_values=["Gold", "Silver"])
    assert norm(e, "gold") == ("Gold", None) and norm(e, "bronze")[1]
    assert norm(FieldSpec(name="i", type="integer"), "1,200") == ("1200", None)
    assert norm(FieldSpec(name="d", type="decimal"), "$1,200.50") == ("1200.5", None)


def test_string_case_and_length():
    f = FieldSpec(name="n", type="string", case="name", max_length=5, truncate=True)
    assert norm(f, "MCDONALD") == ("McDon", None)
    g = FieldSpec(name="n", type="string", max_length=3)
    assert norm(g, "abcd")[1]


def test_null_tokens_are_empty():
    assert norm(PHONE, "N/A") == ("", None)
    assert norm(PHONE, "  ") == ("", None)
