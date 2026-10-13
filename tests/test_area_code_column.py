"""Phone + area code column (0.21.0): a file with the area code in a column of its own.

Before: a file with "Area Code" (516) and "Phone" (555-0177) had no usable phone column. The 7-digit numbers didn't
look like phone numbers, so the phone field stayed unmapped, and mapping "Phone" by hand rejected every row as
"missing area code (only 7 digits)". Now the rules combine the two ('Phone + area code column'): each 7-digit
number gets its row's area code; numbers that already have one are kept as they are.
"""

import random

import pandas as pd
import pytest

from converter.compare import build_compare_html, column_plan
from converter.editor import apply_column_edit, apply_row_edit, option_text, parse_option
from converter.engine import CELL_REPAIRED, convert, source_rows
from converter.mapping import FieldMapping, Mapping, area_code_source, is_area_code_header, phone_rule, profile_columns
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
                                        ("123", ""), ("11530", ""), ("Nassau", ""), ("", "")])
def test_area_code_cell(value, code):
    assert area_code_cell(value) == code


def test_split_area_code():
    assert split_area_code(["516", "555-0177"]) == ("516", [1])
    assert split_area_code(["555-0177", "516"]) == ("516", [0])
    assert split_area_code(["", "555-0177"]) == ("", [0, 1])
    assert split_area_code(["000", "555-0177"], 0) == ("", [1])
    assert split_area_code(["516-555-0177", ""], 0) == ("", [0, 1])
    assert split_area_code(["555-0177", "516", "555-0100"], 1) == ("516", [0, 2])


def test_area_code_source():
    rows = [{"Phone": "555-0177", "Area Code": "516", "X": "631"}]
    assert area_code_source(["Phone", "Area Code"], rows) == 1
    assert area_code_source(["Phone", "X"], rows) == 1  # no header names it: the column holding area codes
    assert area_code_source(["Phone", "Area Code", "NPA"], [{"Phone": "555-0177", "Area Code": "x", "NPA": "516"}]) == 2


def test_with_area_code_leaves_other_values_alone():
    assert with_area_code("555-0177", "516") == "(516) 555-0177"
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
