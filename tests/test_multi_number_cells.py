"""Several phone numbers in one cell ("516-555-0100 / 917-555-1234") → one row per different number.

Each number is a separate text recipient, so a second number written into the same cell must get its own row, just
like a number in a second phone column. The split must never invent or cut digits: a cell is only split where the
numbers are clearly separate, a single number is never split, labels stay with their own number, and a number
labelled fax is never imported (a fax line can't receive texts): skipped next to another number, and a bad row when
it is the only one (0.17.0; it used to be imported then).
"""

import pandas as pd
import pytest

from converter.compare import build_compare_html, column_plan
from converter.engine import convert, count_multi_number_cells, source_rows
from converter.mapping import FieldMapping, Mapping
from converter.normalizers import split_phones

from .test_formats import PHONE_OK


def phone_field(real_schema):
    """The real schema's phone_number field (10-digit US numbers)."""
    return real_schema.field("phone_number")


@pytest.mark.parametrize("cell,pieces", [
    # separators between the numbers
    ("516-555-0100 / 917-555-1234", ["516-555-0100", "917-555-1234"]),
    ("5165550100, 9175551234", ["5165550100", "9175551234"]),
    ("5165550100;9175551234", ["5165550100", "9175551234"]),
    ("(516) 555-0100 or (917) 555-1234", ["(516) 555-0100", "(917) 555-1234"]),
    ("516-555-0100 and 917-555-1234", ["516-555-0100", "917-555-1234"]),
    ("516-555-0100 | 917-555-1234", ["516-555-0100", "917-555-1234"]),
    ("516.555.0100 - 917.555.1234", ["516.555.0100", "917.555.1234"]),
    # no separator at all
    ("5165550100 9175551234", ["5165550100", "9175551234"]),
    ("(516)555-0100(917)555-1234", ["(516)555-0100", "(917)555-1234"]),
    ("+1 516 555 0100 +1 917 555 1234", ["+1 516 555 0100", "+1 917 555 1234"]),
    ("15165550100 19175551234", ["15165550100", "19175551234"]),
    # spaced digit groups: split only where the groups make whole numbers
    ("516 555 0100 917 555 1234", ["516 555 0100", "917 555 1234"]),
    ("1 516 555 0100 1 917 555 1234", ["1 516 555 0100", "1 917 555 1234"]),
    # labels before the numbers stay with the number after them
    ("H: 516-555-1212 C: 516-555-0100", ["H: 516-555-1212", "C: 516-555-0100"]),
    ("Home 516-555-1212 / Cell 516-555-0100", ["Home 516-555-1212", "Cell 516-555-0100"]),
    # labels after the numbers stay with the number before them
    ("516-555-0100 (cell) 516-555-0199 (home)", ["516-555-0100 (cell)", "516-555-0199 (home)"]),
    ("516-555-0100 cell, 516-555-0199 home", ["516-555-0100 cell", "516-555-0199 home"]),
    # an extension stays with its own number
    ("516-555-0100 x12 917-555-1234", ["516-555-0100 x12", "917-555-1234"]),
    ("516-555-0100 x12, 917-555-1234 ext 3", ["516-555-0100 x12", "917-555-1234 ext 3"]),
    # a second number without an area code is its own piece (completed only when the file has a main area code)
    ("516-555-0100 & 555-1212", ["516-555-0100", "555-1212"]),
    # invalid numbers between separators are pieces too, so they are reported instead of disappearing
    ("51655501444 / 917-555-1234", ["51655501444", "917-555-1234"]),
    ("516-555-0100 / 51655501444 / 917-555-1234", ["516-555-0100", "51655501444", "917-555-1234"]),
    # a mix: the separator alone would give 2 pieces, the labels show 3 numbers
    ("Cell 516-555-0100 Home 516-555-1212, Work 631-555-0188",
     ["Cell 516-555-0100", "Home 516-555-1212", "Work 631-555-0188"]),
    # 0.16.0: cells that used to lose a number (or both) - the matcher missed one or joined two wrongly
    ("516 555 0100 x12 917-555-1234", ["516 555 0100 x12", "917-555-1234"]),
    ("1. 516-555-0100 2. 917-555-1234", ["1. 516-555-0100", "2. 917-555-1234"]),
    ("+1 631 555 0199 1-718-555-0142 1-201-555-0111", ["+1 631 555 0199", "1-718-555-0142", "1-201-555-0111"]),
    ("516.555.0100 917.555.1234", ["516.555.0100", "917.555.1234"]),
    ("516.555.0100 917.555.1234 fax", ["516.555.0100", "917.555.1234 fax"]),
])
def test_split(real_schema, cell, pieces):
    """Each number of the cell becomes its own piece, with its own label or extension."""
    assert [p for p, _ in split_phones(cell, phone_field(real_schema), {})] == pieces


# The spellings in test_formats.PHONE_OK that really are several numbers in one cell.
MULTI_IN_PHONE_OK = {
    "H: 516-555-1212 C: 516-555-0100", "Home 516-555-1212 / Cell 516-555-0100",
    "Fax: 516-555-1212, Main: 516-555-0100", "Work: 516-555-1212 Mobile: 516-555-0100",
    "516-555-0100 / 516-555-1212", "516-555-0100, 516-555-1212", "5165550100;5165551212", "5165550100 or 5165551212",
}


@pytest.mark.parametrize("cell", [v for v in PHONE_OK if v not in MULTI_IN_PHONE_OK] + [
    # long single values: extensions, doubled country codes, shorthand and run-together digits are never split
    "516-555-0100 ext 123456", "(516) 555-0100 extension 98765", "+1 +15165550100", "0015165550100",
    "516-555-0100/0101", "51655501005165551212", "516 555 0100 x 12", "1-800-FLOWERS 516-555-0100",
])
def test_single_number_is_never_split(real_schema, cell):
    """Every spelling of one number (and cells that can't be split safely) stays one piece, unchanged."""
    assert split_phones(cell, phone_field(real_schema), {}) == [(cell, False)]


@pytest.mark.parametrize("cell,fax", [
    ("Fax: 516-555-1212, Main: 516-555-0100", [True, False]),
    ("516-555-0100 (cell) 516-555-0199 (fax)", [False, True]),
    ("516-555-0199 fax, 516-555-0100", [True, False]),
    ("F: 516-555-0199 C: 516-555-0100", [True, False]),
])
def test_fax_label_goes_with_its_number(real_schema, cell, fax):
    """The fax label is attached to the right number, whether labels come before or after the numbers."""
    assert [f for _, f in split_phones(cell, phone_field(real_schema), {})] == fax


def one_column_file():
    """One phone column, each customer a different case (see test_one_row_per_number_in_a_cell)."""
    return pd.DataFrame({
        "Name": ["Ana", "Ben", "Cy", "Di", "Ed", "Flo", "Gus", "Hal"],
        "Phone": ["516-555-0100 / 917-555-1234",       # two numbers → 2 rows
                  "H: 516-555-1212 C: 516-555-0111",   # labelled → 2 rows
                  "516-555-0122 (cell), 516-555-0199 fax",  # fax skipped → 1 row
                  "Fax: 516-555-0177",                 # fax is the only number → bad row
                  "516-555-0150 / 555-1212",           # second number has no area code → skipped (no main area
                                                       # code in this file to complete it with)
                  "631-555-0160 and 631-555-0160",     # same number twice → 1 row
                  "555-1212 / 555-1213",               # no usable number → bad row
                  "631-555-0170"],                     # one number → 1 row, as always
        "Last Visit": [f"10/{d:02d}/2026" for d in range(1, 9)],
    })


def one_column_mapping():
    """Phone copied from one column (no 'One row per phone number' needed) and the visit date."""
    return Mapping(fields={"phone_number": FieldMapping(sources=["Phone"], transform="copy"),
                           "transactions_last": FieldMapping(sources=["Last Visit"], transform="copy")})


def test_one_row_per_number_in_a_cell(real_schema):
    """Each different valid number in a cell becomes a row with the customer's other data copied.

    Output, counts, flag message, audit and rejects must all agree.
    """
    res = convert(one_column_file(), real_schema, one_column_mapping())
    assert res.output["phone_number"].tolist() == [
        "5165550100", "9175551234",  # Ana
        "5165551212", "5165550111",  # Ben
        "5165550122",                # Cy (fax skipped)
        "5165550150",                # Ed (Di: her only number is a fax, bad row)
        "6315550160",                # Flo
        "6315550170",                # Hal
    ]
    visits = res.output["transactions_last"].str[:10].tolist()
    assert visits == ["10/01/2026"] * 2 + ["10/02/2026"] * 2 + ["10/03/2026", "10/05/2026", "10/06/2026",
                                                                "10/08/2026"]
    assert (res.total_rows, res.imported, res.rejected, res.extra_rows, res.skipped_values) == (8, 8, 2, 2, 2)
    assert set(res.skipped_counts) == {
        "phone_number: extra value skipped — labelled as a fax number (a fax can't receive texts)",
        "phone_number: extra value skipped — missing area code (only 7 digits)"}
    assert "8 rows ready to import from 8 file rows (2 extra rows" in res.flag_message()
    di = res.rejects.loc[res.rejects["Name"] == "Di"].iloc[0]
    assert "labelled as a fax number" in di["Reason"] and di["phone_number"] == "Fax: 516-555-0177"
    assert "phone_number" not in res.failed_values or "Fax: 516-555-0177" not in res.failed_values["phone_number"]
    assert "2 extra phone numbers skipped" in res.flag_message()
    # Gus: rejected for his first number's reason; the report says which number it was.
    gus = res.rejects.loc[res.rejects["Name"] == "Gus"].iloc[0]
    assert "missing area code" in gus["Reason"] and gus["phone_number"] == "555-1212"
    # The audit keeps each number's own piece of the cell as the value before conversion.
    a = res.audit
    assert a["_row"].tolist()[:4] == [2, 2, 3, 3] and a["_extra"].tolist()[:4] == [False, True, False, True]
    assert a["raw.phone_number"].tolist()[2:4] == ["H: 516-555-1212", "C: 516-555-0111"]


def test_numbers_on_separate_lines_of_a_cell(real_schema):
    """Each number of a multi-line cell gets its row (0.16.0: these lost one number, or both)."""
    df = pd.DataFrame({"Phone": ["516 555 0100 x12\n917-555-1234", "516.555.0101\n917.555.1235",
                                 "+1 631 555 0199\n1-718-555-0142\n1-201-555-0111"]})
    res = convert(df, real_schema, Mapping(fields={"phone_number": FieldMapping(sources=["Phone"], transform="copy")}))
    assert res.output["phone_number"].tolist() == ["5165550100", "9175551234", "5165550101", "9175551235",
                                                   "6315550199", "7185550142", "2015550111"]
    assert res.rejected == 0


def test_cells_and_columns_together(real_schema):
    """Several phone columns AND several numbers in a cell: every different number once, in column order."""
    df = pd.DataFrame({"Phone": ["516-555-0100 / 917-555-1234"], "Phone 2": ["(917) 555-1234"],
                       "Phone 3": ["631-555-0188"]})
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Phone", "Phone 2", "Phone 3"], transform="each_row")})
    res = convert(df, real_schema, m)
    assert res.output["phone_number"].tolist() == ["5165550100", "9175551234", "6315550188"]
    assert (res.extra_rows, res.duplicates) == (2, 0)


def test_typo_repair_and_approved_fixes_apply_per_number(real_schema):
    """Each number of a cell is converted on its own: typo repair and approved fixes work on that number only."""
    # 212-728-1399 typed with its last digit twice; an 8-digit second number fixed by an approved suggestion.
    df = pd.DataFrame({"Phone": ["21272813999 / 917-555-1234", "516-555-0100 / 516-555-01"]})
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Phone"], transform="copy")})
    res = convert(df, real_schema, m, overrides={"phone_number": {"516-555-01": "5165550101"}})
    assert res.output["phone_number"].tolist() == ["2127281399", "9175551234", "5165550100", "5165550101"]
    assert res.repaired_values == 1 and res.audit["st.phone_number"].tolist() == ["rpr", "fmt", "fmt", "fix"]


def test_rejects_report_when_file_already_has_the_column(real_schema):
    """A file in the import format itself (a phone_number column) doesn't clash with the report's number column."""
    df = pd.DataFrame({"phone_number": ["516-555-0100 / 917-555-1234", "555-12"],
                       "phone2": ["", ""]})
    for m in (Mapping(fields={"phone_number": FieldMapping(sources=["phone_number"], transform="copy")}),
              Mapping(fields={"phone_number": FieldMapping(sources=["phone_number", "phone2"],
                                                           transform="each_row")})):
        res = convert(df, real_schema, m)
        assert res.rejected == 1
        assert list(res.rejects.columns[3:5]) == ["phone_number (this row)", "phone_number"]


def test_single_numbers_keep_the_plain_report(real_schema):
    """With one phone column and no cell holding several numbers, the rejects report has no extra column."""
    df = pd.DataFrame({"Phone": ["516-555-0100", "555-12"]})
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Phone"], transform="copy")})
    res = convert(df, real_schema, m)
    assert list(res.rejects.columns) == ["File Row", "Status", "Reason", "Phone"]


def test_side_by_side_marks_split_rows_only(real_schema):
    """Side by side: rows made from a cell with several numbers are labelled 'N +' and the cell is underlined.

    Customers with a single number look exactly as before: no underline.
    """
    df = one_column_file()
    m = one_column_mapping()
    res = convert(df, real_schema, m)
    src = source_rows(df, res.audit)
    plan = column_plan(real_schema, m, list(df.columns))
    assert plan.each_field == "phone_number"
    html = build_compare_html(src, res.audit, list(range(len(res.audit))), real_schema, plan)
    # Ana's and Ben's two rows each underline their own number inside the cell (4 rows). Cy's cell also held two
    # numbers, but only file rows that got several rows are marked.
    assert html.count('class="o eachp"') == 4 and html.count("2 +</span>") == 1 and "underlined" in html
    assert "<u>516-555-0100</u> / 917-555-1234" in html and "516-555-0100 / <u>917-555-1234</u>" in html
    assert "<u>H: 516-555-1212</u> C: 516-555-0111" in html
    single = pd.DataFrame({"Phone": ["516-555-0100", "917-555-1234"]})
    res2 = convert(single, real_schema, Mapping(fields={"phone_number": FieldMapping(sources=["Phone"],
                                                                                      transform="copy")}))
    plan2 = column_plan(real_schema, Mapping(fields={"phone_number": FieldMapping(sources=["Phone"],
                                                                                   transform="copy")}), ["Phone"])
    html2 = build_compare_html(single, res2.audit, [0, 1], real_schema, plan2)
    assert "each" not in html2.split("<tbody>")[1] and "underlined" not in html2


def test_count_for_mapping_page(real_schema):
    """The mapping page counts the cells that hold several numbers (fax-only and single cells don't count)."""
    df = one_column_file()
    # Ana, Ben, Cy, Ed, Flo and Gus hold two numbers each (valid or not); Di and Hal hold one.
    assert count_multi_number_cells(df, phone_field(real_schema), ["Phone", "Missing column"]) == 6
