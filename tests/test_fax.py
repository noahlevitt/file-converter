"""Fax numbers are never imported (0.17.0): a fax line can't receive texts.

Before: a "Fax", "Fax Number", "Business Fax" or "Facsimile" column full of numbers was added to the phone field by
its values, so every customer's fax got a row of its own (a file with only a Fax column imported it as the phone).
A number labelled fax inside a phone cell ("Fax: 516-555-0177") was imported when it was the row's only number.
Now the rules never use a fax column, a template or Claude can't bring one back, and a fax-only row is a bad row
with its own reason. A person can still map a fax column by hand (e.g. a "Phone/Fax" column of real phones).
"""

import pandas as pd
import pytest

from converter.editor import apply_column_edit
from converter.engine import convert
from converter.heuristics import is_phone_header
from converter.mapping import FieldMapping, Mapping, is_fax_column, profile_columns
from converter.normalizers import FAX_REASON
from converter.services import initial_mapping, template_mapping
from converter.storage import Store

PHONES = ["5165550100", "5165550101", "5165550102"]
FAXES = ["5165550199", "5165550198", "5165550197"]


def suggest(schema, df, store=None):
    return initial_mapping(schema, df, profile_columns(df), store, None, use_ai=False)


@pytest.mark.parametrize("header,fax", [
    ("Fax", True), ("Fax Number", True), ("Business Fax", True), ("fax_no", True), ("BusinessFax", True),
    ("Facsimile", True), ("Phone/Fax", True), ("FAX", True),
    ("Fairfax Store", False), ("Phone", False), ("Mobile", False),
])
def test_is_fax_column(header, fax):
    assert is_fax_column(header) is fax
    if fax:
        assert not is_phone_header(header)


@pytest.mark.parametrize("header", ["Fax", "Fax Number", "Business Fax", "Facsimile", "Phone/Fax"])
def test_fax_column_is_never_a_phone_column(real_schema, header):
    df = pd.DataFrame({"Name": ["a", "b", "c"], "Phone": PHONES, header: FAXES})
    sug = suggest(real_schema, df)
    assert sug.mapping.get("phone_number").sources == ["Phone"]
    assert convert(df, real_schema, sug.mapping).imported == 3
    assert any(f"Column '{header}' holds fax numbers" in n for n in sug.notes)


def test_file_with_only_a_fax_column_has_no_phone(real_schema):
    df = pd.DataFrame({"Name": ["a", "b", "c"], "Fax": FAXES})
    assert suggest(real_schema, df).mapping.get("phone_number").transform == "none"


def test_a_person_can_map_a_fax_column(real_schema):
    """Mapped by hand ("Goes to" phone_number) it stays, and its numbers are imported."""
    df = pd.DataFrame({"Phone": ["5165550100", ""], "Phone/Fax": ["5165550199", "5165550198"]})
    m = suggest(real_schema, df).mapping
    m, _ = apply_column_edit(real_schema, m, "Phone/Fax", ["phone_number"])
    m = m.normalized(real_schema, list(df.columns))
    assert m.get("phone_number").sources == ["Phone", "Phone/Fax"]
    assert convert(df, real_schema, m).imported == 3


@pytest.mark.parametrize("origin", ["template", "rule", "ai"])
def test_templates_rules_and_claude_never_bring_a_fax_column_back(real_schema, origin):
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Phone", "Fax"], transform="each_row", origin=origin)})
    out = m.normalized(real_schema, ["Phone", "Fax"]).get("phone_number")
    assert (out.sources, out.transform) == (["Phone"], "copy")


def test_old_template_with_a_fax_column(real_schema, tmp_path):
    """A template saved before 0.17.0 that used the Fax column: applied without it, with a note."""
    store = Store(tmp_path / "t.db")
    old = Mapping(fields={"phone_number": FieldMapping(sources=["Phone", "Fax"], transform="each_row", origin="rule")})
    tpl = store.save_template("Old", ["Phone", "Fax"], old, {}, real_schema.name, "x")
    assert template_mapping(real_schema, tpl, ["Phone", "Fax"]).get("phone_number").sources == ["Phone"]
    df = pd.DataFrame({"Phone": PHONES, "Fax": FAXES})
    sug = suggest(real_schema, df, store)
    assert sug.source == "template" and sug.mapping.get("phone_number").sources == ["Phone"]
    assert any("holds fax numbers" in n for n in sug.notes)


def test_number_labelled_fax_in_a_cell_is_never_imported(real_schema):
    """Skipped next to another number (as before); the row's only number → bad row with its own reason."""
    df = pd.DataFrame({"Name": ["Cy", "Di"], "Phone": ["516-555-0122 (cell), 516-555-0199 fax", "Fax: 516-555-0177"]})
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Phone"], transform="copy")})
    res = convert(df, real_schema, m)
    assert res.output["phone_number"].tolist() == ["5165550122"]
    di = res.rejects.loc[res.rejects["Name"] == "Di"].iloc[0]
    assert FAX_REASON in di["Reason"] and di["Phone"] == "Fax: 516-555-0177"
    assert res.reason_counts == {f"phone_number: {FAX_REASON}": 1}
    assert "Fax: 516-555-0177" not in res.failed_values.get("phone_number", [])  # nothing for Suggest fixes
