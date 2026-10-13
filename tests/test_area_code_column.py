"""Phone + area code column (0.21.0): a file with the area code in a column of its own.

Before: a file with "Area Code" (516) and "Phone" (555-0177) had no usable phone column. The 7-digit numbers didn't
look like phone numbers, so the phone field stayed unmapped, and mapping "Phone" by hand rejected every row as
"missing area code (only 7 digits)". Now the rules combine the two ('Phone + area code column'): each 7-digit
number gets its row's area code; numbers that already have one are kept as they are.

0.21.1 (found by testing 0.21.0): whether the area code column was used depended on the first values of the file
(full numbers first, then 7-digit ones, got the file's main area code instead of their own); one odd value in the
area code column ("000") turned the feature off and every row became a bad row; a second area code column ("Cell
Area Code") was ignored; letters typed for digits ("555-O177") and a spreadsheet ".0" ("5550177.0") didn't get the
row's area code; "Store Area Code" and "Area #" were taken for the customer's area code.

0.21.2: the country code 1 in an area code cell ("1-516", "+1 516") or in front of a 7-digit number ("+1 555-0177")
is read as such.
"""

import random

import pandas as pd
import pytest

from converter.compare import build_compare_html, column_plan
from converter.editor import apply_column_edit, apply_row_edit, option_text, parse_option
from converter.engine import CELL_REPAIRED, convert, source_rows
from converter.mapping import (FieldMapping, Mapping, area_code_columns, area_code_pairs, is_area_code_header,
                               phone_rule, profile_columns)
from converter.normalizers import area_code_cell, split_area_code, with_area_code
from converter.services import initial_mapping
from converter.storage import Store
from converter.transforms import apply_transform


def suggest(schema, df, store=None):
    return initial_mapping(schema, df, profile_columns(df), store, None, use_ai=False)


def area_mapping(*sources, **params):
    return Mapping(fields={"phone_number": FieldMapping(sources=list(sources), transform="area_code", params=params)})


def local_numbers(n, seed=1):
    rnd = random.Random(seed)
    return [f"{rnd.randint(2, 9)}{rnd.randint(10, 99)}-{rnd.randint(1000, 9999)}" for _ in range(n)]


# --- detection ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("header,area", [
    ("Area Code", True), ("AreaCode", True), ("area_code", True), ("Phone Area Code", True), ("AREA CD", True),
    ("NPA", True), ("Area", True), ("areacode1", True),
    ("Delivery Area", False), ("Areas Served", False), ("Zip Code", False), ("Area Manager", False),
    ("Phone", False), ("Sales Area Name", False),
    ("Home Area Code", True), ("Cell Area Code", True), ("Area Code (2)", True), ("Business Area Code", True),
    ("Store Area Code", False), ("Delivery Area Code", False), ("Area #", False), ("Area No", False),
])
def test_is_area_code_header(header, area):
    assert is_area_code_header(header) is area


def test_area_code_and_seven_digit_phone_are_combined(real_schema):
    df = pd.DataFrame({"Name": ["Ann", "Bo", "Cy"], "Area Code": ["516", "631", "917"],
                       "Phone": ["555-0177", "555-0188", "555-1234"]})
    m = suggest(real_schema, df).mapping
    fm = m.get("phone_number")
    assert (fm.sources, fm.transform, fm.origin) == (["Area Code", "Phone"], "area_code", "rule")
    assert "Area Code" in fm.reason
    assert convert(df, real_schema, m).output["phone_number"].tolist() == ["5165550177", "6315550188", "9175551234"]


@pytest.mark.parametrize("area", [["(516)"] * 3, ["516.0"] * 3, ["516", "631", "(917)"]])
def test_area_code_cells_written_differently(real_schema, area):
    df = pd.DataFrame({"Area Code": area, "Phone": ["5550177", "555 0188", "555.1234"]})
    out = convert(df, real_schema, suggest(real_schema, df).mapping).output["phone_number"].tolist()
    assert [v[3:] for v in out] == ["5550177", "5550188", "5551234"] and out[0].startswith("516")


def test_full_numbers_in_the_column_are_kept(real_schema):
    """A row that already has its area code keeps it, even when the Area Code column says something else."""
    df = pd.DataFrame({"Area Code": ["516", "516", "516"], "Phone": ["555-0177", "212-555-0188", "555-1234"]})
    m = suggest(real_schema, df).mapping
    assert m.get("phone_number").transform == "area_code"
    assert convert(df, real_schema, m).output["phone_number"].tolist() == ["5165550177", "2125550188", "5165551234"]


def test_full_numbers_and_an_area_code_column_stay_a_copy(real_schema):
    """Every number already has its area code: the Area Code column isn't needed."""
    df = pd.DataFrame({"Phone": ["516-555-0177", "631-555-0188", "917-555-1234"], "Area Code": ["516", "631", "917"]})
    fm = suggest(real_schema, df).mapping.get("phone_number")
    assert (fm.sources, fm.transform) == (["Phone"], "copy")


@pytest.mark.parametrize("header", ["Delivery Area", "Zone", "Store"])
def test_columns_that_arent_area_codes(real_schema, header):
    df = pd.DataFrame({header: ["516", "631", "917"], "Phone": ["555-0177", "555-0188", "555-1234"]})
    assert suggest(real_schema, df).mapping.get("phone_number").transform != "area_code"


def test_area_code_column_with_values_that_arent_area_codes(real_schema):
    """Named like one, but its values aren't area codes in use (store numbers): not combined."""
    df = pd.DataFrame({"Area": ["101", "102", "103"], "Phone": ["555-0177", "555-0188", "555-1234"]})
    assert suggest(real_schema, df).mapping.get("phone_number").transform != "area_code"


def test_several_phone_columns(real_schema):
    """Cell and Home both get the row's area code; one row per different number."""
    df = pd.DataFrame({"Name": ["Ann", "Bo"], "Area Code": ["516", "631"], "Cell": ["555-0177", "555-0188"],
                       "Home Phone": ["555-0100", ""]})
    m = suggest(real_schema, df).mapping
    fm = m.get("phone_number")
    assert fm.transform == "area_code" and fm.sources[0] == "Area Code"
    assert set(fm.sources[1:]) == {"Cell", "Home Phone"}
    out = convert(df, real_schema, m).output["phone_number"].tolist()
    assert sorted(out) == ["5165550100", "5165550177", "6315550188"]


def test_several_numbers_in_one_cell_and_labels(real_schema):
    df = pd.DataFrame({"Area Code": ["516", "631"], "Phone": ["555-0177 / 555-0100", "Cell: 555-0188"]})
    res = convert(df, real_schema, area_mapping("Area Code", "Phone"))
    assert res.output["phone_number"].tolist() == ["5165550177", "5165550100", "6315550188"]


def test_extension_is_kept_out_of_the_count(real_schema):
    assert with_area_code("555-0177 ext 12", "516") == "(516) 555-0177 ext 12"
    df = pd.DataFrame({"Area Code": ["516"], "Phone": ["555-0177 x12"]})
    assert convert(df, real_schema, area_mapping("Area Code", "Phone")).output["phone_number"].tolist() == [
        "5165550177"]


# --- rows without an area code ------------------------------------------------------------------------------------

def test_empty_area_code_without_a_main_area_code_is_a_bad_row(real_schema):
    df = pd.DataFrame({"Name": ["Ann", "Bo", "Cy"], "AreaCode": ["516", "", "631"],
                       "Phone": ["555-0177", "555-0188", "555-1234"]})
    res = convert(df, real_schema, area_mapping("AreaCode", "Phone"))
    assert res.output["phone_number"].tolist() == ["5165550177", "6315551234"]
    bo = res.rejects.loc[res.rejects["Name"] == "Bo"].iloc[0]
    assert "missing area code" in bo["Reason"]


def test_empty_area_code_gets_the_files_main_area_code(real_schema):
    """0.20.0's repair still applies: most numbers in the file use 516, so the row without one gets 516 (noted)."""
    phones = local_numbers(9)
    df = pd.DataFrame({"Area Code": ["516"] * 8 + [""], "Phone": phones})
    res = convert(df, real_schema, area_mapping("Area Code", "Phone"))
    assert res.imported == 9
    assert res.output["phone_number"].iloc[8] == "516" + phones[8].replace("-", "")
    assert res.audit.iloc[8]["st.phone_number"] == CELL_REPAIRED


def test_odd_value_in_the_area_code_column_is_not_a_number(real_schema):
    """"000" in the Area Code column: the row has no area code, and "000" isn't reported as a number of its own."""
    df = pd.DataFrame({"Name": ["Ann", "Bo"], "Area Code": ["516", "000"], "Phone": ["555-0177", "555-0188"]})
    res = convert(df, real_schema, area_mapping("Area Code", "Phone"))
    assert res.output["phone_number"].tolist() == ["5165550177"]
    assert len(res.rejects) == 1 and "000" not in res.rejects.iloc[0]["Reason"]


def test_whole_number_typed_into_the_area_code_column_is_kept(real_schema):
    df = pd.DataFrame({"Area Code": ["516", "631-555-0199"], "Phone": ["555-0177", ""]})
    res = convert(df, real_schema, area_mapping("Area Code", "Phone"))
    assert res.output["phone_number"].tolist() == ["5165550177", "6315550199"]


# --- helpers --------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value,code", [("516", "516"), ("(516)", "516"), (" 516 ", "516"), ("516.0", "516"),
                                        ("123", ""), ("11530", ""), ("Nassau", ""), ("", ""),
                                        ("1-516", "516"), ("+1 516", "516"), ("+1 (516)", "516"), ("1516", "516"),
                                        ("(1) 516", "516"), ("1516.0", "516"), ("151", ""), ("+44 20", "")])
def test_area_code_cell(value, code):
    assert area_code_cell(value) == code


def test_split_area_code():
    assert split_area_code(["516", "555-0177"]) == ("516", [1])
    assert split_area_code(["555-0177", "516"]) == ("516", [0])
    assert split_area_code(["", "555-0177"]) == ("", [0, 1])
    assert split_area_code(["000", "555-0177"], 0) == ("", [1])
    assert split_area_code(["516-555-0177", ""], 0) == ("", [0, 1])
    assert split_area_code(["555-0177", "516", "555-0100"], 1) == ("516", [0, 2])


def test_area_code_columns_and_pairs():
    rows = [{"Phone": "555-0177", "Area Code": "516", "X": "631"}]
    assert area_code_columns(["Phone", "Area Code"], rows) == [1]
    assert area_code_columns(["Phone", "X"], rows) == [1]  # no header names it: the column holding area codes
    assert area_code_columns(["Phone", "Mobile"], [{"Phone": "555-0177", "Mobile": "555-0100"}]) == []
    assert area_code_pairs(["Phone", "Area Code", "Mobile"], rows) == ([1], {0: 1, 2: 1})
    assert area_code_pairs(["Home Area Code", "Home Phone", "Cell Area Code", "Mobile", "Work Phone"], []) == (
        [0, 2], {1: 0, 3: 2, 4: None})  # Mobile ← Cell; Work Phone has none of its own and never borrows one
    assert area_code_pairs(["Phone (2)", "Area Code", "Area Code (2)", "Phone"], []) == ([1, 2], {0: 2, 3: 1})
    assert area_code_pairs(["Cell Area Code", "Home Phone"], []) == ([0], {1: None})
    assert area_code_pairs(["Home Area Code", "Phone"], []) == ([0], {1: 0})  # a plain Phone uses the only one


def test_with_area_code_leaves_other_values_alone():
    assert with_area_code("555-0177", "516") == "(516) 555-0177"
    assert with_area_code("555-O177", "516") == "(516) 555-0177"  # letters typed for digits
    assert with_area_code("5550177.0", "516") == "(516) 5550177"  # a spreadsheet's ".0"
    assert with_area_code("555 - 0177", "516") == "(516) 555 - 0177"
    assert with_area_code("516-555-0177", "631") == "516-555-0177"
    assert with_area_code("555-0177", "") == "555-0177"
    assert with_area_code("555-0177", "abc") == "555-0177"
    assert with_area_code("", "516") == ""


def test_apply_transform():
    row = {"Phone": "555-0177", "Area Code": "516"}
    assert apply_transform("area_code", row, ["Phone", "Area Code"], {}) == "(516) 555-0177"


# --- order, templates, editor --------------------------------------------------------------------------------------

def test_column_order_doesnt_matter(real_schema):
    df = pd.DataFrame({"Area Code": ["516", "631"], "Phone": ["555-0177", "555-0188"]})
    a = convert(df, real_schema, area_mapping("Area Code", "Phone")).output["phone_number"].tolist()
    b = convert(df, real_schema, area_mapping("Phone", "Area Code")).output["phone_number"].tolist()
    assert a == b == ["5165550177", "6315550188"]


@pytest.mark.parametrize("columns,expected", [
    (["Area Code", "Phone"], (["Area Code", "Phone"], "area_code")),
    (["Phone"], (["Phone"], "copy")),          # a template used on a file without the area code column
    (["Area Code"], ([], "none")),              # ... or without the numbers: nothing to give the area code to
])
def test_normalized(real_schema, columns, expected):
    fm = area_mapping("Area Code", "Phone").normalized(real_schema, columns).get("phone_number")
    assert (fm.sources, fm.transform) == expected


def test_only_the_phone_field_uses_it(real_schema):
    assert phone_rule("phone", "area_code") == "area_code"
    assert phone_rule("date", "area_code") == "concat"
    m = Mapping(fields={"phone_number": FieldMapping(sources=["P"], transform="copy")})
    m2, err = apply_row_edit(real_schema, m, "birthday", {"How": "Phone + area code column", "Source": ["A", "B"]},
                             "How", "Source", "Option")
    assert m2.get("birthday").transform == "concat" and "only for the phone field" in err


def test_option_is_the_country(real_schema):
    f = real_schema.field("phone_number")
    assert parse_option("area_code", f, "ca") == ({"region": "CA"}, None)
    assert option_text(FieldMapping(sources=["A", "P"], transform="area_code", params={"region": "CA"}), f) == "CA"


def test_editor_adds_and_removes_the_area_code_column(real_schema):
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Phone"], transform="copy")})
    m, msgs = apply_column_edit(real_schema, m, "Area Code", ["phone_number"])
    fm = m.get("phone_number")
    assert (fm.sources, fm.transform) == (["Phone", "Area Code"], "area_code")
    assert "area code" in msgs[0]
    # Another phone column joins the combination.
    m, _ = apply_column_edit(real_schema, m, "Cell", ["phone_number"])
    assert m.get("phone_number").transform == "area_code"
    # Without the numbers' columns the area code column alone isn't kept.
    m2, _ = apply_column_edit(real_schema, m, "Phone", [])
    m2, _ = apply_column_edit(real_schema, m2, "Cell", [])
    assert m2.get("phone_number").transform == "none"
    # Without the area code column it's a plain copy again.
    m3, _ = apply_column_edit(real_schema, m, "Cell", [])
    m3, _ = apply_column_edit(real_schema, m3, "Area Code", [])
    assert (m3.get("phone_number").sources, m3.get("phone_number").transform) == (["Phone"], "copy")


def test_template_is_reused(real_schema, tmp_path):
    store = Store(tmp_path / "t.db")
    df = pd.DataFrame({"Name": ["Ann", "Bo"], "Area Code": ["516", "631"], "Phone": ["555-0177", "555-0188"]})
    m = suggest(real_schema, df, store).mapping
    store.save_template("Area", list(df.columns), m, {}, real_schema.name, "x")
    sug = suggest(real_schema, df, store)
    assert sug.source == "template" and sug.mapping.get("phone_number").transform == "area_code"


def test_similar_template_gains_a_new_area_code_column(real_schema, tmp_path):
    """A template made when the client's export had whole numbers; the new export splits off the area code."""
    store = Store(tmp_path / "t.db")
    old = Mapping(fields={"phone_number": FieldMapping(sources=["Phone"], transform="copy", origin="user")})
    store.save_template("Client", ["Name", "Email", "Visits", "Notes", "Phone"], old, {}, real_schema.name, "x")
    df = pd.DataFrame({"Name": ["Ann", "Bo"], "Email": ["a@x.com", "b@x.com"], "Visits": ["1", "2"],
                       "Notes": ["", ""], "Phone": ["555-0177", "555-0188"], "Area Code": ["516", "631"]})
    sug = suggest(real_schema, df, store)
    fm = sug.mapping.get("phone_number")
    assert (fm.sources, fm.transform, fm.origin) == (["Phone", "Area Code"], "area_code", "template")
    assert any("Area code column 'Area Code' is new" in n for n in sug.notes)
    assert convert(df, real_schema, sug.mapping).output["phone_number"].tolist() == ["5165550177", "6315550188"]


# --- reports ----------------------------------------------------------------------------------------------------------

def test_side_by_side_marks_the_phone_cell(real_schema):
    df = pd.DataFrame({"Name": ["Ann", "Bo"], "Area Code": ["516", "631"], "Phone": ["555-0177", "555-0188"]})
    m = area_mapping("Area Code", "Phone").normalized(real_schema, list(df.columns))
    res = convert(df, real_schema, m)
    src = source_rows(df, res.audit)
    h = build_compare_html(src, res.audit, [0, 1], real_schema, column_plan(real_schema, m, list(df.columns)))
    assert h.count('class="o each"') == 2 and 'title="Phone: 555-0177 — this row&#x27;s number"' in h


# --- 0.21.1: found by testing 0.21.0 ------------------------------------------------------------------------------

def phones(schema, df):
    m = suggest(schema, df).mapping
    return m.get("phone_number"), convert(df, schema, m)


@pytest.mark.parametrize("n_full,n_local", [(21, 9), (100, 900), (10, 90), (2000, 3)])
def test_full_numbers_first_dont_hide_the_7_digit_ones(real_schema, n_full, n_local):
    """Detection reads the whole column, not its first values: the 7-digit rows get THEIR row's area code (631),
    not the file's main one (516)."""
    df = pd.DataFrame({"Area Code": ["516"] * n_full + ["631"] * n_local,
                       "Phone": [f"516-{200 + i // 10000}-{i % 10000:04d}" for i in range(n_full)]
                       + [f"555-{i:04d}" for i in range(n_local)]})
    fm, res = phones(real_schema, df)
    assert fm.transform == "area_code" and res.imported == n_full + n_local
    assert res.output["phone_number"].tolist()[-n_local:] == [f"631555{i:04d}" for i in range(n_local)]
    assert res.repaired_values == 0


@pytest.mark.parametrize("junk", ["000", "156", "0", "N/A"])
def test_one_odd_value_in_the_area_code_column(real_schema, junk):
    """Counted by rows: 9 of 10 cells are area codes, so the column is used; the odd row is treated like a row
    without an area code (here: the file's main area code, marked as a repair)."""
    df = pd.DataFrame({"Area Code": ["631"] * 9 + [junk], "Phone": [f"555-01{i:02d}" for i in range(10)]})
    fm, res = phones(real_schema, df)
    assert fm.transform == "area_code" and res.imported == 10
    assert res.output["phone_number"].tolist()[:9] == [f"63155501{i:02d}" for i in range(9)]
    assert res.audit.iloc[9]["st.phone_number"] == CELL_REPAIRED


def test_an_area_code_that_doesnt_exist_in_the_column(real_schema):
    """999 is shaped like an area code but isn't one: the row is a bad row saying so (nothing is guessed)."""
    df = pd.DataFrame({"Area Code": ["631"] * 9 + ["999"], "Phone": [f"555-01{i:02d}" for i in range(10)]})
    fm, res = phones(real_schema, df)
    assert fm.transform == "area_code" and res.imported == 9
    assert "999" in res.rejects.iloc[0]["Reason"]


def test_mostly_odd_values_are_not_an_area_code_column(real_schema):
    df = pd.DataFrame({"Area Code": ["631"] * 7 + ["123"] * 3, "Phone": [f"555-01{i:02d}" for i in range(10)]})
    assert suggest(real_schema, df).mapping.get("phone_number").transform != "area_code"


@pytest.mark.parametrize("columns", [
    ["Home Area Code", "Home Phone", "Cell Area Code", "Cell Phone"],
    ["Area Code", "Phone", "Area Code (2)", "Phone (2)"],
    ["Cell Phone", "Home Phone", "Home Area Code", "Cell Area Code"],
])
def test_each_phone_column_gets_its_own_area_code_column(real_schema, columns):
    home_area, home, cell_area, cell = sorted(columns, key=lambda c: (("Cell" in c or "2" in c), "Area" not in c))
    df = pd.DataFrame({home_area: ["516", "516"], home: ["555-7000", "555-7002"], cell_area: ["718", "917"],
                       cell: ["555-7001", "555-7003"]})[columns]
    fm, res = phones(real_schema, df)
    assert fm.transform == "area_code" and set(fm.sources) == set(columns)
    assert sorted(res.output["phone_number"]) == ["5165557000", "5165557002", "7185557001", "9175557003"]


def test_letters_and_spreadsheet_zero_get_the_row_area_code(real_schema):
    df = pd.DataFrame({"Area Code": ["631", "917", "212", "212", "212", "212"],
                       "Phone": ["555-O177", "5550001.0", "555-0002", "555-0003", "555-0004", "555-0005"]})
    fm, res = phones(real_schema, df)
    assert res.output["phone_number"].tolist()[:2] == ["6315550177", "9175550001"]
    assert res.repaired_values == 0


def test_spreadsheet_zero_also_gets_the_files_main_area_code(real_schema):
    """No area code column: "5550177.0" is a 7-digit number like "555-0177" (the 0.20.0 repair applies)."""
    df = pd.DataFrame({"Phone": [f"516-555-01{i:02d}" for i in range(9)] + ["5550177.0"]})
    res = convert(df, real_schema, Mapping(fields={"phone_number": FieldMapping(sources=["Phone"], transform="copy")}))
    assert res.output["phone_number"].iloc[9] == "5165550177"


@pytest.mark.parametrize("header", ["Store Area Code", "Delivery Area Code", "Area #"])
def test_area_codes_of_something_else_are_not_used(real_schema, header):
    df = pd.DataFrame({header: ["516", "631", "917"], "Phone": ["555-0177", "555-0188", "555-1234"]})
    assert suggest(real_schema, df).mapping.get("phone_number").transform != "area_code"


def test_removing_the_area_code_column_leaves_one_row_per_number(real_schema):
    m = area_mapping("Area Code", "Phone", "Mobile")
    m, _ = apply_column_edit(real_schema, m, "Area Code", [])
    assert (m.get("phone_number").sources, m.get("phone_number").transform) == (["Phone", "Mobile"], "each_row")


def test_a_phone_column_never_borrows_another_lines_area_code(real_schema):
    """Cell Area Code has zeros for "none" (left out of the 80% check): each cell number gets ITS row's cell area
    code, and the rows with 000 have no area code (here: reported as skipped), never the home one."""
    df = pd.DataFrame({"Home Area Code": "516 631 917 212 718 347 516 631 917 212".split(),
                       "Home Phone": [f"555-01{i:02d}" for i in range(10)],
                       "Cell Area Code": "000 000 000 908 914 845 203 860 609 732".split(),
                       "Cell Phone": [f"555-02{i:02d}" for i in range(10)]})
    fm, res = phones(real_schema, df)
    assert fm.sources == ["Home Area Code", "Home Phone", "Cell Area Code", "Cell Phone"]
    out = set(res.output["phone_number"])
    assert {"9085550203", "7325550209"} <= out and not {"2125550203", "5165550200"} & out
    assert res.skipped_values == 3
    # Without the cell area code column, the cell numbers get no area code from the home one.
    m, _ = apply_column_edit(real_schema, suggest(real_schema, df).mapping, "Cell Area Code", [])
    out = set(convert(df, real_schema, m).output["phone_number"])
    assert not any(v[3:8] == "55502" for v in out)  # skipped (no main area code in this file), not given 516…


def test_choosing_the_how_first_waits_for_the_second_column(real_schema):
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Phone"], transform="copy")})
    m, _ = apply_row_edit(real_schema, m, "phone_number", {"How": "Phone + area code column"}, "How", "Source",
                          "Option")
    assert m.get("phone_number").transform == "area_code"  # not silently turned back into a copy
    m, err = apply_row_edit(real_schema, m, "phone_number", {"Source": ["Phone", "Area Code"]}, "How", "Source",
                            "Option")
    assert (m.get("phone_number").sources, m.get("phone_number").transform, err) == (
        ["Phone", "Area Code"], "area_code", None)
    # How and a single column chosen in one edit, or How first and then one column: the row waits for the second.
    empty = Mapping()
    m, _ = apply_row_edit(real_schema, empty, "phone_number", {"How": "Phone + area code column", "Source": ["P"]},
                          "How", "Source", "Option")
    assert m.get("phone_number").transform == "area_code"
    m, _ = apply_row_edit(real_schema, empty, "phone_number", {"How": "Phone + area code column"}, "How", "Source",
                          "Option")
    m, _ = apply_row_edit(real_schema, m, "phone_number", {"Source": ["P"]}, "How", "Source", "Option")
    assert m.get("phone_number").transform == "area_code"
    # Removing a column from two still leaves a copy.
    m, _ = apply_row_edit(real_schema, area_mapping("Area Code", "Phone"), "phone_number", {"Source": ["Phone"]},
                          "How", "Source", "Option")
    assert m.get("phone_number").transform == "copy"


def test_adding_a_second_area_code_column_says_so(real_schema):
    m, msgs = apply_column_edit(real_schema, area_mapping("Area Code", "Phone", "Cell"), "Cell Area Code",
                                ["phone_number"])
    assert m.get("phone_number").transform == "area_code" and "area code" in msgs[0]


def test_no_area_code_column_among_the_sources(real_schema):
    """A template's 'Phone + area code column' used on a file without its area code column: numbers read as they
    are, nothing taken for an area code."""
    df = pd.DataFrame({"Phone": ["516-555-0177", "631-555-0188"], "Mobile": ["917-555-1234", ""]})
    res = convert(df, real_schema, area_mapping("Phone", "Mobile"))
    assert sorted(res.output["phone_number"]) == ["5165550177", "6315550188", "9175551234"]


# --- 0.21.2: the country code 1 -------------------------------------------------------------------------------------

def test_country_code_in_the_area_code_and_phone_columns(real_schema):
    """"1-516" in the area code column is area code 516; "+1 555-0177" in the phone column is a 7-digit number."""
    df = pd.DataFrame({"Area Code": ["1-516", "+1 (631)", "1917", "212"],
                       "Phone": ["555-0177", "+1 555-0188", "1-555-0199", "Cell: +1 555-0100 x12"]})
    fm, res = phones(real_schema, df)
    assert fm.transform == "area_code"
    assert res.output["phone_number"].tolist() == ["5165550177", "6315550188", "9175550199", "2125550100"]
    assert res.repaired_values == 0


@pytest.mark.parametrize("value,local", [("+1 555-0177", "555-0177"), ("+15550177", "5550177"),
                                         ("1-555-0177", "555-0177"), ("1 555 0177", "555 0177"),
                                         ("15550177", None), ("+1 516-555-0177", None), ("+44 555-0177", None)])
def test_country_code_in_front_of_7_digits(value, local):
    from converter.normalizers import _local_form
    assert _local_form(value) == local


def test_country_code_and_7_digits_get_the_files_main_area_code(real_schema):
    """No area code column: "+1 555-0177" is filled from the file's main area code like "555-0177" (0.20.0 rule);
    "15550188" (8 digits run together) is not guessed."""
    df = pd.DataFrame({"Phone": [f"516-555-01{i:02d}" for i in range(8)] + ["+1 555-0177", "15550188"]})
    res = convert(df, real_schema, Mapping(fields={"phone_number": FieldMapping(sources=["Phone"], transform="copy")}))
    assert res.output["phone_number"].tolist()[8:] == ["5165550177"]
    assert res.audit.iloc[8]["st.phone_number"] == CELL_REPAIRED
    assert "too few digits" in res.rejects.iloc[0]["Reason"]
