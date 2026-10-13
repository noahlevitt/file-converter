"""Only US numbers are expected, so a number that fails is treated as a likely typo and repaired when exactly one
US number fits. Every repair is marked so a person can check it.

A wrong guess texts a stranger, so each repair was approved one by one: a foreign country code in front of a US
number is dropped, one extra digit is removed, an area code that doesn't exist anywhere is read as the file's own
area code it is one keystroke from (156 → 516), and a 7-digit number gets the area code of 80%+ of the file. The
last two were removed in 0.9.0 and restored in 0.20.0; a real area code (514, Quebec) is never changed. When two
numbers fit equally well, the one that appears elsewhere in the file wins; otherwise the row is rejected with a
message that says why. Repairs are counted and shown in the flag message, audit, comparison view, field summary and
summary file.
"""

import pandas as pd

from converter import normalizers as N
from converter.compare import build_compare_html, column_plan, field_change_summary, filter_rows, status_counts
from converter.engine import CELL_REPAIRED, convert
from converter.mapping import FieldMapping, Mapping
from converter.output import write_summary

# A Long Island / NYC column (516, 631, 917): the numbers and area codes the file itself provides as evidence.
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


def test_real_area_codes_are_never_changed(real_schema):
    """A real area code is never "corrected", even one keystroke from the file's own (514 → 516 is NOT done): it can
    be someone's real number elsewhere. The number stays a bad row (or imports as written, if it is a US number)."""
    out, err, note = fix(real_schema, "5149689696")  # 514 (Montreal) one digit off 516, which the file uses
    assert (out, note) == ("", None) and err == "not a US number — Canada (area code 514, Quebec)"
    out, err, note = fix(real_schema, "(613) 555-0199")  # Ottawa: 631 with two digits swapped, but real
    assert (out, note) == ("", None) and "Canada (area code 613" in err
    out, _, note = fix(real_schema, "(561) 555-0199")  # a real US area code (Florida) is kept as written
    assert out == "5615550199" and note is None


def test_area_code_that_doesnt_exist_is_read_as_the_files_own(real_schema):
    """156 exists nowhere; with two digits swapped it is 516, which the file uses (restored in 0.20.0)."""
    out, _, note = fix(real_schema, "(156) 555-0199")
    assert out == "5165550199" and note == "area code 156 doesn't exist; read as 516, which this file uses"
    out, _, note = fix(real_schema, "(517) 555-0199")  # one digit off 516, but 517 is real (Michigan): kept
    assert out == "5175550199" and note is None


def test_area_code_that_doesnt_exist_and_matches_nothing(real_schema):
    """433 exists nowhere and isn't one keystroke from the file's area codes: rejected, with that reason."""
    out, err, _ = fix(real_schema, "+1 (433) 309-8420")
    assert out == "" and err == ("not a real US number (area code 433 isn't in use) — not a likely typo of this "
                                 "file's area codes (516, 631)")


def test_area_code_that_could_be_two_of_the_files_is_not_guessed(real_schema):
    """211 isn't an area code; it is one digit off both 212 and 213 in this file: both are listed, none is used."""
    column = ["2125550100", "2125550111", "2135550122", "2135550133"]
    out, err, _ = fix(real_schema, "(211) 555-0199", column)
    assert out == "" and "could be (212) 555-0199 or (213) 555-0199" in err
    out, _, note = fix(real_schema, "(211) 555-0199", column + ["213-555-0199"])  # one of them is in the file
    assert out == "2135550199" and "appears elsewhere in this file" in note


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


def test_missing_area_code_is_filled_from_the_files_main_area_code(real_schema):
    """A 7-digit number gets the area code of 80%+ of the file's numbers (restored in 0.20.0); with a mix of area
    codes (516 is 62% of LONG_ISLAND) nothing is guessed."""
    all_516 = [f"516555{n:04d}" for n in range(100, 120)]
    out, _, note = fix(real_schema, "555-0177", all_516)
    assert out == "5165550177" and note == "no area code; used 516, the area code of most numbers in this file"
    out, err, note = fix(real_schema, "555-0177", LONG_ISLAND)
    assert (out, note) == ("", None) and err == "missing area code (only 7 digits)"
    out, err, _ = fix(real_schema, "555-0177", all_516[:4])  # too few numbers to tell (fewer than 5)
    assert out == "" and err == "missing area code (only 7 digits)"


def test_filled_area_codes_are_marked_and_counted(real_schema):
    """Imported, marked orange like every repair; also for a second number in a cell."""
    df = pd.DataFrame({"Phone": [f"516555{n:04d}" for n in range(100, 110)] + ["555-0177", "516-555-0150 / 555-1212"]})
    res = convert(df, real_schema, Mapping(fields={"phone_number": FieldMapping(sources=["Phone"], transform="copy")}))
    assert res.output["phone_number"].tolist()[10:] == ["5165550177", "5165550150", "5165551212"]
    assert res.repaired_values == 2 and res.audit.loc[10, "st.phone_number"] == CELL_REPAIRED
    assert "no area code; used 516" in res.audit.loc[10, "_issues"]


def test_repairs_are_marked_and_counted(real_schema):
    """A repair is visible everywhere a person reviews the import, with the original value shown.

    It is counted, named in the flag message and summary file, marked in the audit, listed under 'problems' in the
    comparison and counted in the field summary.
    """
    # Row 8 (an extra digit: 212-728-1399 typed with its last digit twice) is repaired; row 9 (unused 433) is rejected.
    df = pd.DataFrame({"Phone": LONG_ISLAND + ["21272813999", "+1 (433) 309-8420"]})
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Phone"], transform="copy")})
    res = convert(df, real_schema, m)
    assert (res.imported, res.rejected, res.repaired_values) == (9, 1, 1)
    assert "1 phone number was repaired from likely typos" in res.flag_message()
    assert res.audit.loc[8, "st.phone_number"] == CELL_REPAIRED and "one digit too many" in res.audit.loc[8, "_issues"]
    assert "Repaired from likely typos" in write_summary(res, real_schema, "f.csv", "noah", "x").decode()

    plan = column_plan(real_schema, m, list(df.columns))
    assert filter_rows(res.audit, df, real_schema, "problems") == [8, 9]
    assert status_counts(res.audit, real_schema)["problems"] == 2
    h = build_compare_html(df, res.audit, [8], real_schema, plan)
    assert "c-rpr" in h and "repaired from a likely typo" in h and "21272813999" in h
    summary = field_change_summary(res.audit, real_schema, m)
    assert summary.loc[summary["Field"] == "phone_number", "Repaired typo"].iloc[0] == 1


def test_repairs_with_several_phone_columns(real_schema):
    """Repairs also apply to a second phone column; the repaired number becomes its own extra row."""
    df = pd.DataFrame({"phone": LONG_ISLAND, "phone2": [""] * 7 + ["+44 516 555 0199"]})
    m = Mapping(fields={"phone_number": FieldMapping(sources=["phone", "phone2"], transform="each_row")})
    res = convert(df, real_schema, m)
    assert res.extra_rows == 1 and res.repaired_values == 1
    assert res.output["phone_number"].tolist()[-1] == "5165550199"


def test_odd_phone_values_never_crash_date_detection():
    """Regression: date sniffing on odd phone-like values must return False, never raise and stop an upload."""
    # "1 51694619661" once reached datetime() as day 51,694,619,661 and stopped the upload.
    for v in ["+1 +51694619661", "1 99999999999999999999", "12/99999999999"]:
        assert N.looks_like_date(v) is False
