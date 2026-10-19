"""Consent: opt-ins only, or opt-ins and opt-outs (0.22.0).

Before: a column mapped to consent wrote true for yes and false for no. But a client's "Email Opt In" column says
whether the customer opted into email: a "No" there means they never opted into email, not that they opted out of
texts, and importing it as false would unsubscribe them. Now whoever maps consent chooses, for that file:
  * Opt-ins only: yes → true; no is left blank, which changes nobody's subscription;
  * Opt-ins and opt-outs: yes → true, no → false (as before).
Nothing is assumed: the import can't start until the choice is made.

Found by the independent test of 0.22.0 (fixed before release): a re-run of an import made before 0.22.0 started
without the choice and wrote false; removing one of three consent columns kept the choice; the counts in the
banner and What changed disagreed; the grid's preview showed false before the choice; "Opt-Out", "Un-subscribed",
"1.0" / "0.0" and full-width letters weren't read as yes/no (a real opt-out was dropped); Suggest fixes could turn
"maybe" into false; Claude's answer could carry a choice.
"""

import os

import pandas as pd
import pytest
import streamlit as st
from pydantic import ValidationError
from streamlit.testing.v1 import AppTest

from converter import numberlog
from converter.ai import parse_mapping_entries
from converter.compare import build_compare_html, column_plan, comparison_xlsx, field_change_summary
from converter.editor import apply_column_edit, apply_row_edit, row_problem
from converter.engine import CELL_OPTOUT, convert, preview_values, source_rows
from converter.jobs import run_import
from converter.mapping import FieldMapping, Mapping, opt_out_setting, profile_columns
from converter.output import write_summary
from converter.schema import FieldSpec
from converter.services import initial_mapping
from converter.storage import Store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRANSLATE = {"value_map": [{"from": "Yes", "to": "true"}, {"from": "No", "to": "false"}]}


def file_df():
    return pd.DataFrame({"Phone": ["5165550101", "5165550102", "5165550103", "5165550104"],
                         "Email Opt In": ["Y", "N", "", "maybe"]})


def consent(opt_outs=None, transform="copy", sources=("Email Opt In",), **params):
    if opt_outs:
        params["opt_outs"] = opt_outs
    return Mapping(fields={"phone_number": FieldMapping(sources=["Phone"], transform="copy"),
                           "consent": FieldMapping(sources=list(sources), transform=transform, params=params,
                                                   origin="user")})


# --- the rule --------------------------------------------------------------------------------------------------------

def test_opt_ins_only_leaves_no_blank(real_schema):
    res = convert(file_df(), real_schema, consent("skip"))
    assert res.output["consent"].tolist() == ["true", "", "", ""]
    assert res.audit["st.consent"].tolist()[:2] == ["fmt", CELL_OPTOUT]
    assert res.opt_outs_left_blank == 1
    assert "1 consent value of false left blank (consent: opt-ins only)" in res.flag_message()
    assert res.summary_dict()["opt_outs_left_blank"] == 1


def test_opt_ins_and_opt_outs_writes_false(real_schema):
    res = convert(file_df(), real_schema, consent("import"))
    assert res.output["consent"].tolist() == ["true", "false", "", ""]
    assert res.opt_outs_left_blank == 0 and "opt-ins only" not in res.flag_message()


@pytest.mark.parametrize("values", [["Y", "N"], ["yes", "no"], ["Opted in", "Opted out"], ["1", "0"],
                                    ["Subscribed", "Unsubscribed"], ["TRUE", "FALSE"]])
def test_every_spelling_of_no(real_schema, values):
    df = pd.DataFrame({"Phone": ["5165550101", "5165550102"], "Email Opt In": values})
    assert convert(df, real_schema, consent("skip")).output["consent"].tolist() == ["true", ""]


def test_translated_values_and_a_fixed_value(real_schema):
    df = pd.DataFrame({"Phone": ["5165550101", "5165550102"], "Email Opt In": ["Yes", "No"]})
    assert convert(df, real_schema, consent("skip", "map_values", **TRANSLATE)).output["consent"].tolist() == [
        "true", ""]
    assert convert(df, real_schema, consent("import", "map_values", **TRANSLATE)).output["consent"].tolist() == [
        "true", "false"]
    fixed = consent("skip", "constant", sources=(), value="false")
    assert convert(df, real_schema, fixed).output["consent"].tolist() == ["", ""]


def test_invalid_values_are_still_reported(real_schema):
    """"maybe" isn't a yes/no value: left blank as before, and reported as invalid (not as an opt-out)."""
    res = convert(file_df(), real_schema, consent("skip"))
    assert res.audit["st.consent"].tolist()[3] == "blk" and res.blanked_values == 1


# --- the choice is required -----------------------------------------------------------------------------------------

def test_consent_needs_the_choice(real_schema):
    cols = list(file_df().columns)
    m = consent().normalized(real_schema, cols)
    assert "choose Opt-ins only or Opt-ins and opt-outs" in m.problems(real_schema)["consent"]
    assert "choose Opt-ins only" in row_problem(real_schema, m, "consent")
    for choice in ("skip", "import"):
        assert consent(choice).normalized(real_schema, cols).problems(real_schema) == {}
    # Not mapped: nothing to choose.
    unmapped = Mapping(fields={"phone_number": FieldMapping(sources=["Phone"], transform="copy")})
    assert unmapped.normalized(real_schema, cols).problems(real_schema) == {}


def test_schema_attribute(real_schema):
    assert real_schema.field("consent").opt_out_choice
    with pytest.raises(ValidationError):
        FieldSpec(name="visits", type="integer", opt_out_choice=True)


def test_normalized_keeps_the_choice_only_where_it_means_something(real_schema):
    cols = list(file_df().columns) + ["Last Visit"]
    m = consent("skip", "map_values", **TRANSLATE)
    m.fields["transactions_last"] = FieldMapping(sources=["Last Visit"], transform="copy",
                                                 params={"opt_outs": "skip", "date_order": "DMY"})
    out = m.normalized(real_schema, cols)
    assert out.get("consent").params["opt_outs"] == "skip"
    assert out.get("transactions_last").params == {"date_order": "DMY"}  # not a boolean field
    assert "opt_outs" not in consent("sometimes").normalized(real_schema, cols).get("consent").params


# --- editing --------------------------------------------------------------------------------------------------------

def edit(schema, m, **changes):
    labels = {"how": "How", "src": "Source", "opt": "Option"}
    return apply_row_edit(schema, m, "consent", {labels[k]: v for k, v in changes.items()}, "How", "Source",
                          "Option")[0]


def test_the_choice_survives_how_and_option_edits(real_schema):
    m = edit(real_schema, consent("skip"), how="Translate values")
    assert m.get("consent").params == {"opt_outs": "skip"}
    m = edit(real_schema, m, opt="Yes=true; No=false")
    assert m.get("consent").params == {"opt_outs": "skip", **TRANSLATE}


def test_another_column_asks_again(real_schema):
    """The choice is about what the column means: picking another column for consent clears it."""
    m = edit(real_schema, consent("skip"), src=["Phone"])
    assert "opt_outs" not in m.get("consent").params
    m2, _ = apply_column_edit(real_schema, consent("import"), "SMS Opt In", ["consent"])
    assert "opt_outs" not in m2.get("consent").params


def test_summary_names_the_choice(real_schema):
    assert consent("skip").get("consent").summary() == "Email Opt In — opt-ins only"
    assert consent("import").get("consent").summary() == "Email Opt In — opt-ins and opt-outs"
    assert consent().get("consent").summary() == "Email Opt In"


# --- reports ----------------------------------------------------------------------------------------------------------

def test_side_by_side_what_changed_workbook_and_summary(real_schema):
    df = file_df()
    m = consent("skip").normalized(real_schema, list(df.columns))
    res = convert(df, real_schema, m)
    plan = column_plan(real_schema, m, list(df.columns))
    h = build_compare_html(source_rows(df, res.audit), res.audit, [0, 1], real_schema, plan)
    assert 'class="n c-out"' in h and "(left blank: opt-ins only)" in h and "opt-out left blank" in h
    assert "← Email Opt In — opt-ins only" in h
    row = field_change_summary(res.audit, real_schema, m).set_index("Field").loc["consent"]
    assert row["Opt-outs left blank"] == 1
    assert comparison_xlsx(source_rows(df, res.audit), res.audit, real_schema, plan, "t")[:2] == b"PK"
    assert b"Consent false left blank:            1" in write_summary(res, real_schema, "f.csv", "noah", "0.22.0")


def test_number_history_records_the_blank_and_the_choice(tmp_path, real_schema):
    st_ = Store(tmp_path / "c.db")
    data = b"Phone,Email Opt In\n516-555-0101,Y\n516-555-0102,N\n"
    iid = st_.create_import(username="noah", filename="email.csv", data=data, schema_label=real_schema.label,
                            schema_json=real_schema.model_dump_json(), mapping=consent("skip"),
                            read_options={"header_row": "auto"})
    rec = run_import(st_, iid)
    assert rec.summary["opt_outs_left_blank"] == 1
    [e] = numberlog.lookup(st_, "5165550102", real_schema).entries
    assert (e["consent"], e["consent_value"], e["consent_from"]) == ("", "N", "Email Opt In — opt-ins only")


def test_rules_and_templates_still_never_map_consent(real_schema):
    df = file_df()
    m = initial_mapping(real_schema, df, profile_columns(df), None, None, use_ai=False).mapping
    assert m.get("consent").transform == "none"


# --- the mapping page -----------------------------------------------------------------------------------------------

@pytest.fixture()
def app_data(tmp_path):
    old = {k: os.environ.get(k) for k in ("DATA_DIR", "AUTH_MODE", "ANTHROPIC_API_KEY")}
    os.environ.update(DATA_DIR=str(tmp_path), ANTHROPIC_API_KEY="")
    os.environ.pop("AUTH_MODE", None)
    st.cache_resource.clear()
    yield Store(tmp_path / "converter.db")
    st.cache_resource.clear()
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_mapping_page_asks_and_blocks_until_answered(app_data):
    did = app_data.create_draft("local", "optin.csv", b"Name,Phone,Email Opt In\nAnn,516-555-0101,Y\n"
                                                      b"Bo,516-555-0102,N\n")
    at = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=60)
    at.query_params["draft"] = did
    at.run()
    assert not at.exception and not [r for r in at.radio if "consent" in r.label]
    m = at.session_state["mapping"]
    m.fields["consent"] = FieldMapping(sources=["Email Opt In"], transform="copy", origin="user")
    at.session_state["mapping"] = m
    at.run()
    [radio] = [r for r in at.radio if "consent" in r.label]
    assert radio.value is None and "Email Opt In" in radio.label
    assert any("choose Opt-ins only" in w.value for w in at.warning)
    assert [b for b in at.button if "Start import" in b.label][0].disabled
    radio.set_value("skip").run()
    assert not at.exception
    assert at.session_state["mapping"].get("consent").params == {"opt_outs": "skip"}
    assert not [b for b in at.button if "Start import" in b.label][0].disabled


# --- found by the independent test of 0.22.0 ----------------------------------------------------------------------

@pytest.mark.parametrize("value,out", [("Opt-Out", "false"), ("opted-out", "false"), ("Un-subscribed", "false"),
                                       ("0.0", "false"), ("Ｎ", "false"), ("Opt_In", "true"), ("1.0", "true"),
                                       ("maybe", "")])
def test_more_spellings_of_yes_and_no(real_schema, value, out):
    df = pd.DataFrame({"Phone": ["5165550101"], "Email Opt In": [value]})
    assert convert(df, real_schema, consent("import")).output["consent"].tolist() == [out]


def test_removing_one_of_several_consent_columns_asks_again(real_schema):
    m = consent("import", "coalesce", sources=("A", "B", "C"))
    m2, _ = apply_column_edit(real_schema, m, "A", [])
    assert m2.get("consent").sources == ["B", "C"] and "opt_outs" not in m2.get("consent").params
    m3, _ = apply_column_edit(real_schema, m, "A", skip=True)
    assert "opt_outs" not in m3.get("consent").params
    # A consent column that isn't in the file (or is skipped): the choice was about all of them.
    out = consent("skip", "coalesce", sources=("A", "B")).normalized(real_schema, ["Phone", "B"])
    assert out.get("consent").sources == ["B"] and "opt_outs" not in out.get("consent").params
    assert "choose" in out.problems(real_schema)["consent"]


def test_leave_blank_then_fixed_value_asks_again(real_schema):
    m = edit(real_schema, consent("skip"), how="Leave blank")
    m = edit(real_schema, m, how="Fixed value", opt="false")
    assert "opt_outs" not in m.get("consent").params and row_problem(real_schema, m, "consent")


def test_counts_agree(real_schema):
    """The banner, the summary and What changed count the same cells: every non-empty row, like the other counts."""
    df = pd.DataFrame({"Phone": ["5165550101", "bad", "5165550103", "5165550103"],
                       "Email Opt In": ["Y", "N", "N", "N"]})
    m = consent("skip").normalized(real_schema, list(df.columns))
    res = convert(df, real_schema, m)
    row = field_change_summary(res.audit, real_schema, m).set_index("Field").loc["consent"]
    assert res.opt_outs_left_blank == row["Opt-outs left blank"] == 3


def test_preview_waits_for_the_choice(real_schema):
    df = file_df()
    assert preview_values(df, real_schema, consent())["consent"] == "choose below what a no means"
    assert preview_values(df, real_schema, consent("import"))["consent"].startswith("true | false")


def test_odd_stored_values_mean_no_choice(real_schema):
    for bad in (["skip"], {"a": 1}, True, "SKIP"):
        m = consent().model_copy(deep=True)
        m.fields["consent"].params["opt_outs"] = bad
        assert opt_out_setting(m.fields["consent"].params) is None
        assert m.get("consent").summary() == "Email Opt In"
        assert "choose" in m.normalized(real_schema, list(file_df().columns)).problems(real_schema)["consent"]


def test_claude_never_sets_the_choice(real_schema):
    """Consent is manual_only, so Claude never maps it; and the choice is never taken from Claude for any field."""
    entries = [{"target_field": f, "transform": "copy", "sources": ["Email Opt In"],
                "params": {"opt_outs": "skip"}, "confidence": "high", "reason": "x"}
               for f in ("consent", "transactions_last")]
    out, dropped = parse_mapping_entries(entries, real_schema, list(file_df().columns))
    assert out.get("consent").transform == "none" and "consent: only a person maps this field" in dropped
    assert out.get("transactions_last").sources == ["Email Opt In"]
    assert "opt_outs" not in out.get("transactions_last").params


def _imports_page(tmp_path, import_id):
    """AppTest of the Imports page for one import (function pages can't be opened with AppTest.switch_page)."""
    wrapper = tmp_path / "imports_page.py"
    wrapper.write_text(
        "import runpy, streamlit as st\n"
        "real = st.navigation\n"
        "def nav(pages, **kw):\n"
        "    real(pages, **kw)\n"
        "    page = next(p for p in pages if p.url_path == 'imports')\n"
        "    return type('Nav', (), {'run': lambda self: page._page()})()\n"
        "st.navigation = nav\n"
        "try:\n"
        f"    runpy.run_path({os.path.join(ROOT, 'app.py')!r}, run_name='__main__')\n"
        "finally:\n"
        "    st.navigation = real  # AppTest runs in this process: don't leave the patch for later tests\n")
    at = AppTest.from_file(str(wrapper), default_timeout=60)
    at.query_params["id"] = str(import_id)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_rerun_of_an_import_from_before_asks_first(app_data, tmp_path, real_schema):
    """Imports made before 0.22.0 have consent without the choice: Re-run asks for it instead of writing false."""
    old = consent()  # what 0.21 stored
    iid = app_data.create_import(username="local", filename="old.csv", schema_label=real_schema.label,
                                 data=b"Phone,Email Opt In\n516-555-0101,Y\n516-555-0102,N\n",
                                 schema_json=real_schema.model_dump_json(), mapping=old,
                                 read_options={"header_row": 0})
    run_import(app_data, iid)
    at = _imports_page(tmp_path, iid)
    [radio] = [r for r in at.radio if "Before re-running" in r.label]
    [button] = [b for b in at.button if "Re-run this import" in b.label]
    assert button.disabled
    radio.set_value("skip").run()
    [button] = [b for b in at.button if "Re-run this import" in b.label]
    assert not button.disabled
    button.click().run()
    new = max(app_data.list_imports(), key=lambda r: r.id)
    assert new.id != iid and new.parent_id == iid and new.mapping.get("consent").params == {"opt_outs": "skip"}
