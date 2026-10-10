"""Only US numbers are expected, so a number that fails is treated as a likely typo and repaired when exactly one
US number fits. Every repair is marked so a person can check it.

A wrong guess texts a stranger, so a repair is only made from evidence in the same file: the area codes the file
uses and numbers that appear elsewhere in it. When two numbers fit equally well, or nothing local fits, the row is
rejected with a message that says why. Real US area codes are never "corrected". Repairs are counted and shown in
the flag message, audit, comparison view, field summary and summary file.
"""

import pandas as pd

from converter import normalizers as N
from converter.compare import build_compare_html, column_plan, field_change_summary, filter_rows, status_counts
from converter.engine import CELL_REPAIRED, convert
from converter.mapping import FieldMapping, Mapping
from converter.output import write_summary

# A Long Island / NYC column (516, 631, 917): the local area codes that typos are measured against.
LONG_ISLAND = ["5165550100", "5165550111", "5165550122", "(516) 555-0133", "516-555-0144", "6315550155",
               "6315550166", "9175550177"]


def fix(real_schema, value, column=LONG_ISLAND):
    """Normalize `value` as if it were one more cell in `column`; return (output, error, repair note).

    The note is None when the value was read as-is without a repair.
    """
    f = real_schema.field("phone_number")
    ctx = N.prepare(f, column + [value], {})
    out, err = N.normalize(value, f, {}, ctx)
    return out, err, ctx.get("repairs", {}).get(value)


def test_area_code_typo_of_a_local_area_code(real_schema):
    """An area code one keystroke from a local one is fixed (514 → 516, swapped 156 → 516); a real one (561) isn't."""
    out, err, note = fix(real_schema, "5149689696")  # 514 (Montreal) one digit off 516, which the file uses
    assert out == "5169689696" and err is None and "514 looks like a typo of 516" in note
    out, _, note = fix(real_schema, "(561) 555-0199")  # 561 is a real US area code (Florida): left alone
    assert out == "5615550199" and note is None
    out, _, note = fix(real_schema, "(156) 555-0199")  # swapped digits
    assert out == "5165550199" and "156 looks like a typo of 516" in note


def test_area_code_not_near_any_local_one_is_rejected(real_schema):
    """An unused area code that isn't close to a local one is rejected; the message lists the file's area codes."""
    out, err, _ = fix(real_schema, "+1 (433) 309-8420")
    assert out == "" and "area code 433 isn't in use" in err and "516, 631" in err


def test_foreign_code_in_front_of_us_number(real_schema):
    """A country code put in front of a local number is removed; a real foreign number isn't forced into US shape."""
    out, _, note = fix(real_schema, "+44 516 555 0188")
    assert out == "5165550188" and "removed country code +44" in note
    out, err, _ = fix(real_schema, "+44 20 7946 0958")  # a real London number, 207 isn't local: not guessed
    assert out == "" and "United Kingdom" in err


def test_extra_plus_one_is_noted(real_schema):
    """The '+1 +51 ...' mangling (a local number given a second country code) is repaired and noted as an extra +1."""
    out, _, note = fix(real_schema, "+1 +51 63 171059")
    assert out == "5163171059" and "extra +1" in note


def test_one_digit_too_many(real_schema):
    """An 11-digit typo is fixed only when one candidate is clearly right; with two, both are listed, not guessed."""
    out, _, note = fix(real_schema, "51655501444")  # last digit typed twice → 516-555-0144 (also in the file)
    assert out == "5165550144" and "one digit too many" in note
    out, err, _ = fix(real_schema, "+1 +51694619661")  # two possible numbers, neither in the file: not guessed
    assert out == "" and "could be (516) 946-1961 or (516) 946-1966" in err


def test_tie_broken_by_a_number_elsewhere_in_the_file(real_schema):
    """Of two candidate numbers, the one that already appears in the file wins, and the note says so."""
    out, _, note = fix(real_schema, "+1 +51694619661", LONG_ISLAND + ["516-946-1966"])
    assert out == "5169461966" and "appears elsewhere in this file" in note


def test_missing_area_code_only_with_one_dominant_area_code(real_schema):
    """A 7-digit number gets an area code only when one area code dominates the file; a mixed file is rejected."""
    # Ten 516 numbers and a single 631 number.
    dominant = [f"516555{n:04d}" for n in range(100, 110)] + ["6315550155"]
    out, _, note = fix(real_schema, "555-0177", dominant)
    assert out == "5165550177" and "no area code" in note
    out, err, _ = fix(real_schema, "555-0177")  # mixed 516 / 631 / 917: no single area code to assume
    assert out == "" and "missing area code" in err


def test_repairs_are_marked_and_counted(real_schema):
    """A repair is visible everywhere a person reviews the import, with the original value shown.

    It is counted, named in the flag message and summary file, marked in the audit, listed under 'problems' in the
    comparison and counted in the field summary.
    """
    # Row 8 (514 → 516) is repaired; row 9 (unused 433) is rejected.
    df = pd.DataFrame({"Phone": LONG_ISLAND + ["5149689696", "+1 (433) 309-8420"]})
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Phone"], transform="copy")})
    res = convert(df, real_schema, m)
    assert (res.imported, res.rejected, res.repaired_values) == (9, 1, 1)
    assert "1 phone number was repaired from likely typos" in res.flag_message()
    assert res.audit.loc[8, "st.phone_number"] == CELL_REPAIRED and "typo of 516" in res.audit.loc[8, "_issues"]
    assert "Repaired from likely typos" in write_summary(res, real_schema, "f.csv", "noah", "x").decode()

    plan = column_plan(real_schema, m, list(df.columns))
    assert filter_rows(res.audit, df, real_schema, "problems") == [8, 9]
    assert status_counts(res.audit, real_schema)["problems"] == 2
    h = build_compare_html(df, res.audit, [8], real_schema, plan)
    assert "c-rpr" in h and "repaired from a likely typo" in h and "5149689696" in h
    summary = field_change_summary(res.audit, real_schema, m)
    assert summary.loc[summary["Field"] == "phone_number", "Repaired typo"].iloc[0] == 1


def test_repairs_with_several_phone_columns(real_schema):
    """Repairs also apply to a second phone column; the repaired number becomes its own extra row."""
    df = pd.DataFrame({"phone": LONG_ISLAND, "phone2": [""] * 7 + ["5149689696"]})
    m = Mapping(fields={"phone_number": FieldMapping(sources=["phone", "phone2"], transform="each_row")})
    res = convert(df, real_schema, m)
    assert res.extra_rows == 1 and res.repaired_values == 1
    assert res.output["phone_number"].tolist()[-1] == "5169689696"


def test_odd_phone_values_never_crash_date_detection():
    """Regression: date sniffing on odd phone-like values must return False, never raise and stop an upload."""
    # "1 51694619661" once reached datetime() as day 51,694,619,661 and stopped the upload.
    for v in ["+1 +51694619661", "1 99999999999999999999", "12/99999999999"]:
        assert N.looks_like_date(v) is False
