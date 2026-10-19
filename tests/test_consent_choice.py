"""Consent settings, per column (0.22.0; reworked in 0.23.0).

A consent value changes subscriptions: true subscribes the customer, false opts them out of texts, blank changes
nothing. A client's columns record different things, so whoever maps a column to consent says what it records:
  * Opt-ins only (e.g. Email Opt In): yes → true; no → blank (not opting into email isn't opting out of texts);
  * Opt-ins and opt-outs (a column of text opt-ins and opt-outs): yes → true; no → false;
  * Opt-outs only (e.g. Unsubscribed, Opt Out): yes → false; no → blank.
Values naming a status ("Subscribed", "Opted out") keep their meaning. Several consent columns are read together:
an opt-out in any of them wins, then an opt-in, then blank. Nothing is assumed: the import can't start until every
consent column has its setting.

0.22.0 had one choice for the whole field (opt-ins only / opt-ins and opt-outs, param "opt_outs"); its mappings
become per-column settings. Found by the independent test of 0.22.0 and kept here: a re-run of an older import must
ask first; the grid's preview waits for the setting; "Opt-Out", "1.0" and full-width letters are yes/no values;
Claude never sets a setting.
"""

import os

import pandas as pd
import pytest
import streamlit as st
from pydantic import ValidationError
from streamlit.testing.v1 import AppTest

from converter import numberlog
from converter.ai import _output_schema, _transform_docs, parse_mapping_entries
from converter.compare import build_compare_html, column_plan, comparison_xlsx, field_change_summary
from converter.editor import (apply_column_edit, apply_row_edit, goes_to_cell, goes_to_options, option_text,
                              parse_option, read_goes_to, row_problem)
from converter.engine import CELL_OPTOUT, CELL_UNSUB, convert, preview_values, source_rows
from converter.jobs import run_import
from converter.mapping import FieldMapping, Mapping, consent_modes, consent_rule, parse_consent_mode, profile_columns
from converter.output import write_summary
from converter.schema import FieldSpec
from converter.services import initial_mapping
from converter.storage import Store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRANSLATE = {"value_map": [{"from": "Yes", "to": "true"}, {"from": "No", "to": "false"}]}
PHONES = [f"51655501{i:02d}" for i in range(1, 30)]


def consent(modes=None, transform="consent", sources=("Email Opt In",), **params):
    """A mapping of Phone + consent; ``modes``: one setting for every column ("in") or {column: setting}."""
    if isinstance(modes, str):
        modes = {s: modes for s in sources}
    if modes:
        params["modes"] = modes
    return Mapping(fields={"phone_number": FieldMapping(sources=["Phone"], transform="copy"),
                           "consent": FieldMapping(sources=list(sources), transform=transform, params=params,
                                                   origin="user")})


def run(schema, columns: dict, m):
    df = pd.DataFrame({"Phone": PHONES[:len(next(iter(columns.values())))], **columns})
    return convert(df, schema, m)


def out(res):
    return res.output["consent"].tolist()


# --- one column ----------------------------------------------------------------------------------------------------

def test_opt_ins_only(real_schema):
    res = run(real_schema, {"Email Opt In": ["Y", "N", "", "maybe"]}, consent("in"))
    assert out(res) == ["true", "", "", ""]
    assert res.audit["st.consent"].tolist() == ["fmt", CELL_OPTOUT, "", "blk"]


def test_opt_ins_and_opt_outs(real_schema):
    res = run(real_schema, {"Email Opt In": ["Y", "N", "", "maybe"]}, consent("both"))
    assert out(res) == ["true", "false", "", ""]


def test_opt_outs_only_reads_a_yes_as_an_opt_out(real_schema):
    """An Unsubscribed / Opt Out column: its yes opts the customer out, its no changes nothing."""
    m = consent("out", sources=("Unsubscribed",))
    res = run(real_schema, {"Unsubscribed": ["Yes", "No", "", "maybe", "TRUE", "x", "0"]}, m)
    assert out(res) == ["false", "", "", "", "false", "false", ""]
    assert res.audit["st.consent"].tolist() == [CELL_UNSUB, CELL_OPTOUT, "", "blk", CELL_UNSUB, CELL_UNSUB,
                                                CELL_OPTOUT]


@pytest.mark.parametrize("mode,expected", [("in", ["true", "true", "", ""]),
                                           ("both", ["true", "true", "false", "false"]),
                                           ("out", ["", "", "false", "false"])])
def test_values_naming_a_status_keep_their_meaning(real_schema, mode, expected):
    """"Subscribed" is an opt-in and "Opted out" an opt-out in every setting; a setting only says which are imported."""
    res = run(real_schema, {"Status": ["Subscribed", "Opt-In", "Opted out", "Unsubscribed"]},
              consent(mode, sources=("Status",)))
    assert out(res) == expected


@pytest.mark.parametrize("yes,no", [("Y", "N"), ("yes", "no"), ("1", "0"), ("TRUE", "FALSE"), ("1.0", "0.0"),
                                    ("Ｙ", "Ｎ"), ("on", "off")])
def test_spellings_of_yes_and_no(real_schema, yes, no):
    cols = {"C": [yes, no]}
    assert out(run(real_schema, cols, consent("in", sources=("C",)))) == ["true", ""]
    assert out(run(real_schema, cols, consent("both", sources=("C",)))) == ["true", "false"]
    assert out(run(real_schema, cols, consent("out", sources=("C",)))) == ["false", ""]


@pytest.mark.parametrize("value,expected", [("Opt-Out", "false"), ("opted-out", "false"), ("Un-subscribed", "false"),
                                            ("Opt_In", "true"), ("maybe", "")])
def test_more_spellings(real_schema, value, expected):
    assert out(run(real_schema, {"Email Opt In": [value]}, consent("both"))) == [expected]


def test_translated_values_keep_their_meaning(real_schema):
    """A translated value means what it says (true / yes = opt-in, false / no = opt-out) in every setting; the setting
    only decides which are imported. Found by the independent test of 0.23.0: an Unsubscribed column translated
    "Yes=false; No=true" (as written before 0.23.0) and set to opt-outs only was turned around — everyone who hadn't
    unsubscribed was opted out."""
    cols = {"Email Opt In": ["Yes", "No"]}
    for mode, expected in (("in", ["true", ""]), ("both", ["true", "false"]), ("out", ["", "false"])):
        assert out(run(real_schema, cols, consent(mode, "map_values", **TRANSLATE))) == expected
    unsub = {"value_map": [{"from": "Yes", "to": "false"}, {"from": "No", "to": "true"}]}
    cols = {"Unsubscribed": ["Yes", "No", "No"]}
    for mode, expected in (("in", ["", "true", "true"]), ("both", ["false", "true", "true"]),
                           ("out", ["false", "", ""])):
        res = run(real_schema, cols, consent(mode, "map_values", sources=("Unsubscribed",), **unsub))
        assert out(res) == expected
    # A value the translation doesn't list is read like any other (here a plain yes in an opt-outs-only column).
    stop = {"value_map": [{"from": "STOP", "to": "false"}]}
    res = run(real_schema, {"SMS": ["STOP", "Active", "Y"]}, consent("out", "map_values", sources=("SMS",), **stop))
    assert out(res) == ["false", "", "false"]


def test_new_translations_say_opt_in_opt_out_or_nothing(real_schema):
    """Found by the second independent test of 0.23.0: "DNT=yes; OK=no" on an opt-outs-only Do Not Text column is
    read one way by one person and the other way by the next, and each reading flips who gets texted. So the mapping
    page takes only opt-in, opt-out or nothing as a consent translation."""
    f = real_schema.field("consent")
    for text in ("DNT=yes; OK=no", "DNT=Y", "DNT=true; OK=false", "DNT=1", "=opt-out", "DNT=maybe", "DNT=Active"):
        params, err = parse_option("map_values", f, text)
        assert params == {} and "opt-in, opt-out or nothing" in err, text
    assert "translated twice" in parse_option("map_values", f, "dnt=opt-out; DNT=opt-in")[1]
    assert parse_option("map_values", f, "dnt=opt-out; DNT=Opted Out")[1] is None  # the same meaning twice is fine
    params, err = parse_option("map_values", f, "DNT=opt-out; OK=; Joined=Subscribed")
    assert not err and [p["to"] for p in params["value_map"]] == ["opt-out", "", "Subscribed"]
    # Other fields translate to anything, as before.
    assert parse_option("map_values", real_schema.field("transactions_last"), "x=yes")[1] is None
    cols = {"Do Not Text": ["DNT", "OK", ""]}
    for mode, expected in (("out", ["false", "", ""]), ("both", ["false", "", ""]), ("in", ["", "", ""])):
        m = consent(mode, "map_values", sources=("Do Not Text",), **params)
        assert out(run(real_schema, cols, m)) == expected


def test_an_empty_cell_is_never_translated(real_schema):
    """An older stored translation with an empty "from" ("=false") mustn't fill empty cells (a blank changes
    nothing), as before 0.23.0."""
    stored = {"value_map": [{"from": "Y", "to": "true"}, {"from": "", "to": "false"}]}
    res = run(real_schema, {"SMS": ["Y", "", "N/A"]}, consent("both", "map_values", sources=("SMS",), **stored))
    assert out(res) == ["true", "", ""]


def test_a_fixed_value_needs_no_setting(real_schema):
    """A fixed value is itself the decision (e.g. a file that is a list of unsubscribes: false for everyone)."""
    m = consent(None, "constant", sources=(), value="false")
    assert m.normalized(real_schema, ["Phone"]).problems(real_schema) == {}
    assert out(run(real_schema, {"Other": ["a", "b"]}, m)) == ["false", "false"]


# --- several columns -----------------------------------------------------------------------------------------------

EMAIL_UNSUB = {"Email Opt In": "in", "Unsubscribed": "out"}


@pytest.mark.parametrize("email,unsub,expected,status,raw", [
    ("Y", "No", "true", "fmt", "Email Opt In: Y"),
    ("Y", "Yes", "false", CELL_UNSUB, "Unsubscribed: Yes"),        # an opt-out wins
    ("N", "Yes", "false", CELL_UNSUB, "Unsubscribed: Yes"),
    ("N", "No", "", CELL_OPTOUT, "Email Opt In: N"),               # nothing to import
    ("", "", "", "", ""),
    ("maybe", "", "", "blk", "Email Opt In: maybe"),
    ("maybe", "Yes", "false", CELL_UNSUB, "Unsubscribed: Yes"),    # an unreadable value doesn't block the others
])
def test_an_opt_in_column_and_an_unsubscribe_column(real_schema, email, unsub, expected, status, raw):
    m = consent(EMAIL_UNSUB, sources=("Email Opt In", "Unsubscribed"))
    res = run(real_schema, {"Email Opt In": [email], "Unsubscribed": [unsub]}, m)
    assert (out(res)[0], res.audit["st.consent"][0], res.audit["raw.consent"][0]) == (expected, status, raw)


def test_an_sms_no_beats_an_email_yes(real_schema):
    m = consent({"SMS Opt In": "both", "Email Opt In": "in"}, sources=("SMS Opt In", "Email Opt In"))
    res = run(real_schema, {"SMS Opt In": ["N", "Y", ""], "Email Opt In": ["Y", "N", "Y"]}, m)
    assert out(res) == ["false", "true", "true"]


def test_the_first_opt_out_names_the_column(real_schema):
    m = consent({"A": "both", "B": "out"}, sources=("A", "B"))
    res = run(real_schema, {"A": ["N", "Y"], "B": ["Yes", "Yes"]}, m)
    assert res.audit["raw.consent"].tolist() == ["A: N", "B: Yes"]


def test_counts_in_the_converted_file(real_schema):
    """The banner and summary count the converted file: true, false, and blanks left by a setting."""
    m = consent(EMAIL_UNSUB, sources=("Email Opt In", "Unsubscribed"))
    res = run(real_schema, {"Phone": ["5165550101", "5165550102", "5165550103", "bad", "5165550101"],
                            "Email Opt In": ["Y", "N", "Y", "Y", "Y"], "Unsubscribed": ["", "", "Yes", "", ""]}, m)
    assert res.consent_counts == {"consent": {"true": 1, "false": 1, "left_blank": 1}}
    assert ("Consent in the converted file: 1 true (subscribed), 1 false (unsubscribed), 1 left blank by the "
            "consent settings") in res.flag_message()
    assert res.summary_dict()["consent_counts"] == res.consent_counts
    text = write_summary(res, real_schema, "f.csv", "noah", "0.23.0")
    assert b"Consent true / false:                1 / 1" in text and b"Consent left blank by setting:       1" in text


def test_a_fixed_value_is_counted_and_unmapped_consent_is_not(real_schema):
    res = run(real_schema, {"Other": ["a", "b"]}, consent(None, "constant", sources=(), value="false"))
    assert res.consent_counts == {"consent": {"true": 0, "false": 2, "left_blank": 0}}
    res = run(real_schema, {"Other": ["a"]}, consent(None, "none", sources=()))
    assert res.consent_counts == {} and "Consent in the converted file" not in res.flag_message()


# --- found by the independent test of 0.23.0 ----------------------------------------------------------------------

def test_a_0_22_import_with_several_columns_gives_the_same_result(real_schema):
    """0.22.0 combined consent columns with First non-empty and one choice. Its re-run keeps First non-empty (the
    first column with a value decides) with that choice on each column, so nobody who opted into SMS is unsubscribed
    because they declined email."""
    for opt_outs, expected in (("import", ["true", "false", "true"]), ("skip", ["true", "", "true"])):
        stored = FieldMapping.model_validate({"sources": ["SMS Opt In", "Email Opt In"], "transform": "coalesce",
                                              "params": {"opt_outs": opt_outs}, "origin": "user"})
        m = Mapping(fields={"phone_number": FieldMapping(sources=["Phone"], transform="copy"), "consent": stored})
        assert m.normalized(real_schema, ["Phone", "SMS Opt In", "Email Opt In"]).get("consent").transform == "coalesce"
        res = run(real_schema, {"SMS Opt In": ["Y", "N", ""], "Email Opt In": ["N", "Y", "Y"]}, m)
        assert out(res) == expected
        mode = "opt-ins and opt-outs" if opt_outs == "import" else "opt-ins only"
        assert stored.summary() == f"first non-empty of SMS Opt In ({mode}), Email Opt In ({mode})"


def test_reports_show_the_settings_of_any_consent_mapping(real_schema):
    """What changed, Mapping used, the side-by-side header and Number history read the mapping as stored: a copy or
    First non-empty with settings (0.22.0 imports, re-runs of older ones) shows them too."""
    assert consent("in", "copy").get("consent").summary() == "Email Opt In (opt-ins only)"
    m = consent(EMAIL_UNSUB, "coalesce", sources=("Email Opt In", "Unsubscribed"))
    assert numberlog.consent_source(real_schema, m) == (
        "first non-empty of Email Opt In (opt-ins only), Unsubscribed (opt-outs only)")
    assert consent(None, "copy").get("consent").summary() == "Email Opt In"  # as 0.21 stored it


def test_first_non_empty_is_only_kept_as_older_imports_stored_it(real_schema):
    """First non-empty with per-column settings would let an earlier column hide an opt-out column's opt-out (found
    by the second independent test): a person mapping consent gets Consent columns, and editing a stored First
    non-empty turns it into that."""
    both = consent(EMAIL_UNSUB, sources=("Email Opt In", "Unsubscribed"))
    m, err = edit(real_schema, both, how="First non-empty")
    assert m.get("consent").transform == "consent" and "only kept for imports made before 0.23.0" in err
    legacy = consent(EMAIL_UNSUB, "coalesce", sources=("Email Opt In", "Unsubscribed"))
    assert out(run(real_schema, {"Email Opt In": ["Y"], "Unsubscribed": ["Yes"]}, legacy)) == ["true"]  # as stored
    m2, msgs = apply_column_edit(real_schema, legacy, "SMS", ["consent"], settings={"consent": "both"})
    assert m2.get("consent").transform == "consent" and "now read together" in msgs[0]
    m3, err = edit(real_schema, legacy, opt="Email Opt In: opt-ins and opt-outs; Unsubscribed: opt-outs only")
    assert not err and m3.get("consent").transform == "consent"
    assert consent_modes(m3.get("consent")) == {"Email Opt In": "both", "Unsubscribed": "out"}
    assert option_text(legacy.get("consent"), real_schema.field("consent")) == (
        "Email Opt In: opt-ins only; Unsubscribed: opt-outs only")
    m4, _ = apply_column_edit(real_schema, legacy, "Email Opt In", [])
    assert (m4.get("consent").transform, consent_modes(m4.get("consent"))) == ("consent", {"Unsubscribed": "out"})
    # Any other change by a person too: a setting changed with Goes to, or a column removed from three.
    m5, _ = apply_column_edit(real_schema, legacy, "Email Opt In", ["consent"], settings={"consent": "out"})
    assert m5.get("consent").transform == "consent"
    three = consent({**EMAIL_UNSUB, "SMS": "both"}, "coalesce", sources=("Email Opt In", "Unsubscribed", "SMS"))
    m6, _ = apply_column_edit(real_schema, three, "SMS", [])
    assert m6.get("consent").transform == "consent" and consent_modes(m6.get("consent")) == EMAIL_UNSUB


def test_goes_to_replacing_a_fixed_value_or_translation_says_so(real_schema):
    _, msgs = apply_column_edit(real_schema, consent(None, "constant", sources=(), value="false"), "Opt Out",
                                ["consent"], settings={"consent": "out"})
    assert msgs == ["consent now comes from 'Opt Out' (opt-outs only; was fixed value 'false')."]
    _, msgs = apply_column_edit(real_schema, consent("in", "map_values", **TRANSLATE), "Opt Out", ["consent"],
                                settings={"consent": "out"})
    assert msgs == ["consent now comes from 'Opt Out' (opt-outs only; was Translate values: Email Opt In)."]


@pytest.mark.parametrize("value,mode,expected", [("-1", "both", ""), ("-1", "out", ""), ("Active", "out", ""),
                                                 ("Active", "both", "true"), ("Inactive", "out", ""),
                                                 ("Inactive", "both", "false")])
def test_values_that_mustnt_opt_anyone_out(real_schema, value, mode, expected):
    """"-1" (unknown in some systems) isn't a yes; "Active" is a subscription status (an opt-in), so an opt-outs-only
    column never unsubscribes it; "Inactive" is a plain no (it may only mean a customer who hasn't visited)."""
    assert out(run(real_schema, {"C": [value]}, consent(mode, sources=("C",)))) == [expected]


def test_templates_never_keep_consent_settings(real_schema):
    """A custom format may let templates keep a yes/no column with opt_out_choice (no manual_only); its settings are
    still chosen for each file."""
    custom = real_schema.model_copy(deep=True)
    custom.fields[[f.name for f in custom.fields].index("consent")] = FieldSpec(
        name="consent", type="boolean", true_value="true", false_value="false", opt_out_choice=True)
    kept = consent("in").without_manual(custom).get("consent")
    assert kept.sources == ["Email Opt In"] and kept.params == {}
    assert consent("in").without_manual(real_schema).get("consent").transform == "none"


def test_two_settings_for_one_column_are_asked_again(real_schema):
    m = consent({"Opt Out": "out", "Opt-Out": "in"}, sources=("Opt Out",))
    n = m.normalized(real_schema, ["Phone", "Opt-Out"]).get("consent")
    assert n.sources == ["Opt-Out"] and n.params == {}


# --- the setting is required -----------------------------------------------------------------------------------------

def test_every_consent_column_needs_its_setting(real_schema):
    cols = ["Phone", "Email Opt In", "Unsubscribed"]
    m = consent({"Email Opt In": "in"}, sources=("Email Opt In", "Unsubscribed")).normalized(real_schema, cols)
    problem = m.problems(real_schema)["consent"]
    assert "'Unsubscribed'" in problem and "Email Opt In" not in problem and "opt-outs only" in problem
    assert row_problem(real_schema, m, "consent") == problem
    done = consent(EMAIL_UNSUB, sources=("Email Opt In", "Unsubscribed")).normalized(real_schema, cols)
    assert done.problems(real_schema) == {}
    unmapped = Mapping(fields={"phone_number": FieldMapping(sources=["Phone"], transform="copy")})
    assert unmapped.normalized(real_schema, cols).problems(real_schema) == {}


def test_an_unset_column_is_ignored_by_the_engine(real_schema):
    """The import can't start without the setting; if a conversion runs anyway, the column changes nobody."""
    assert out(run(real_schema, {"Email Opt In": ["Y", "N"]}, consent())) == ["", ""]


def test_preview_waits_for_the_setting(real_schema):
    df = pd.DataFrame({"Phone": PHONES[:2], "Email Opt In": ["Y", "N"]})
    assert preview_values(df, real_schema, consent())["consent"] == "choose below what each column records"
    assert preview_values(df, real_schema, consent("both"))["consent"] == "true | false"


def test_schema_attribute(real_schema):
    assert real_schema.field("consent").opt_out_choice
    with pytest.raises(ValidationError):
        FieldSpec(name="visits", type="integer", opt_out_choice=True)


@pytest.mark.parametrize("text,mode", [("Opt-ins only", "in"), ("in", "in"), ("opt in only", "in"),
                                       ("Opt-ins and opt-outs", "both"), ("full", "both"), ("BOTH", "both"),
                                       ("Opt-outs only", "out"), ("opt_out_only", "out"), ("maybe", None),
                                       (None, None), (3, None)])
def test_setting_names(text, mode):
    assert parse_consent_mode(text) == mode


# --- stored mappings -------------------------------------------------------------------------------------------------

def test_a_0_22_choice_becomes_each_columns_setting(real_schema):
    fm = FieldMapping.model_validate({"sources": ["A", "B"], "transform": "coalesce", "params": {"opt_outs": "skip"},
                                      "origin": "user"})
    assert fm.params == {"modes": {"A": "in", "B": "in"}}
    stored = Mapping.model_validate_json(consent(None, "copy").model_dump_json().replace(
        '"params":{}', '"params":{"opt_outs":"import"}', 1).replace('"params":{}', '"params":{"opt_outs":"import"}'))
    assert consent_modes(stored.get("consent")) == {"Email Opt In": "both"}
    fixed = FieldMapping.model_validate({"transform": "constant", "params": {"value": "false", "opt_outs": "skip"}})
    assert fixed.params == {"value": "false", "modes": {}}
    odd = FieldMapping.model_validate({"sources": ["A"], "transform": "copy", "params": {"opt_outs": ["skip"]}})
    assert odd.params == {}


def test_normalized_keeps_each_columns_setting(real_schema):
    m = consent({"email opt in": "in", "Unsubscribed": "out", "Gone": "both"}, "copy",
                sources=("email opt in", "Unsubscribed", "Gone"))
    n = m.normalized(real_schema, ["Phone", "Email Opt In", "Unsubscribed"]).get("consent")
    assert (n.transform, n.sources, n.params) == ("consent", ["Email Opt In", "Unsubscribed"],
                                                  {"modes": {"Email Opt In": "in", "Unsubscribed": "out"}})
    # A column skipped by the reviewer takes its setting with it; the others keep theirs.
    m.excluded_columns = ["Unsubscribed"]
    n = m.normalized(real_schema, ["Phone", "Email Opt In", "Unsubscribed"]).get("consent")
    assert n.params == {"modes": {"Email Opt In": "in"}}
    # Odd values aren't settings; other fields never keep them.
    bad = consent({"Email Opt In": "sometimes"}).normalized(real_schema, ["Phone", "Email Opt In"]).get("consent")
    assert bad.params == {}
    other = Mapping(fields={"transactions_last": FieldMapping(sources=["Last Visit"], transform="copy",
                                                              params={"modes": {"Last Visit": "in"}})})
    assert other.normalized(real_schema, ["Last Visit"]).get("transactions_last").params == {}


def test_consent_reads_columns_with_consent_columns_only(real_schema):
    c, other = real_schema.field("consent"), real_schema.field("transactions_last")
    assert [consent_rule(c, t, 2) for t in ("copy", "concat", "split", "consent")] == ["consent"] * 4
    assert [consent_rule(c, t, 1) for t in ("none", "constant", "map_values")] == ["none", "constant", "map_values"]
    # First non-empty of several columns stays (how older imports combined them); of one column it's a copy.
    assert consent_rule(c, "coalesce", 2) == "coalesce" and consent_rule(c, "coalesce", 1) == "consent"
    assert consent_rule(other, "consent", 1) == "copy" and consent_rule(other, "consent", 2) == "coalesce"
    m = Mapping(fields={"transactions_last": FieldMapping(sources=["A", "B"], transform="consent")})
    assert m.normalized(real_schema, ["A", "B"]).get("transactions_last").transform == "coalesce"


def test_summary_names_each_columns_setting(real_schema):
    two = consent({"Email Opt In": "in"}, sources=("Email Opt In", "Unsubscribed")).get("consent")
    assert two.summary() == "Email Opt In (opt-ins only) + Unsubscribed (setting not chosen)"
    assert consent("out", "map_values", **TRANSLATE).get("consent").summary() == (
        "Translate values: Email Opt In (opt-outs only)")


# --- editing ----------------------------------------------------------------------------------------------------------

def edit(schema, m, field="consent", **changes):
    labels = {"how": "How", "src": "Source", "opt": "Option"}
    return apply_row_edit(schema, m, field, {labels[k]: v for k, v in changes.items()}, "How", "Source", "Option")


def test_columns_table_maps_a_column_with_its_setting(real_schema):
    m, msgs = apply_column_edit(real_schema, consent(None, "none", sources=()), "Email Opt In", ["consent"],
                                settings={"consent": "in"})
    assert (m.get("consent").transform, consent_modes(m.get("consent"))) == ("consent", {"Email Opt In": "in"})
    assert msgs == ["consent now comes from 'Email Opt In' (opt-ins only)."]
    # A second consent column adds up, with its own setting.
    m, msgs = apply_column_edit(real_schema, m, "Unsubscribed", ["consent"], settings={"consent": "out"})
    assert consent_modes(m.get("consent")) == EMAIL_UNSUB
    assert msgs == ["consent now also reads 'Unsubscribed' (opt-outs only)."]
    # Another setting for a column consent already reads changes just that one.
    m, msgs = apply_column_edit(real_schema, m, "Email Opt In", ["consent"], settings={"consent": "both"})
    assert consent_modes(m.get("consent")) == {"Email Opt In": "both", "Unsubscribed": "out"}
    assert msgs == ["consent: 'Email Opt In' is now opt-ins and opt-outs."]
    # Removing or skipping a column: the others keep their settings.
    m2, _ = apply_column_edit(real_schema, m, "Email Opt In", [])
    assert (m2.get("consent").transform, consent_modes(m2.get("consent"))) == ("consent", {"Unsubscribed": "out"})
    m3, _ = apply_column_edit(real_schema, m, "Unsubscribed", skip=True)
    assert consent_modes(m3.get("consent")) == {"Email Opt In": "both"}


def test_a_column_without_a_setting_is_asked_for(real_schema):
    m, msgs = apply_column_edit(real_schema, consent(None, "none", sources=()), "Email Opt In", ["consent"])
    assert m.get("consent").params == {} and "choose its setting below the field table" in msgs[0]
    assert "choose what 'Email Opt In' records" in row_problem(real_schema, m, "consent")


def test_goes_to_shows_and_reads_the_setting(real_schema):
    options = goes_to_options(real_schema)
    assert ["consent — opt-ins only", "consent — opt-ins and opt-outs", "consent — opt-outs only",
            "consent — setting not chosen"] == [o for o in options if o.startswith("consent")]
    assert "consent" not in options and "phone_number" in options
    m = consent({"Email Opt In": "in"}, sources=("Email Opt In", "Unsubscribed"))
    assert goes_to_cell(real_schema, m, "Email Opt In") == ["consent — opt-ins only"]
    assert goes_to_cell(real_schema, m, "Unsubscribed") == ["consent — setting not chosen"]
    assert read_goes_to(real_schema, ["consent — opt-ins only", "phone_number"]) == (
        ["consent", "phone_number"], {"consent": "in"})
    # Two settings picked for one column: the last one picked counts; "not chosen" never replaces one.
    assert read_goes_to(real_schema, ["consent — setting not chosen", "consent — opt-outs only",
                                      "consent — setting not chosen"]) == (["consent"], {"consent": "out"})
    assert read_goes_to(real_schema, ["consent — opt-ins only", "consent — opt-outs only"])[1] == {"consent": "out"}


def test_grid_columns_get_consent_columns_and_no_setting(real_schema):
    m, err = edit(real_schema, consent(None, "none", sources=()), src=["Email Opt In", "Unsubscribed"])
    assert not err and (m.get("consent").transform, m.get("consent").params) == ("consent", {})
    # Settings typed in the Option cell: with several columns each is named (a bare setting would change them all:
    # found by the independent test of 0.23.0); with one column a bare setting is enough.
    m1, err = edit(real_schema, m, opt="opt-outs only")
    assert "name each one" in err and consent_modes(m1.get("consent")) == {}
    one, err = edit(real_schema, consent(None, sources=("Unsubscribed",)), opt="out")
    assert not err and consent_modes(one.get("consent")) == {"Unsubscribed": "out"}
    m, err = edit(real_schema, m, opt="Email Opt In: opt-ins only; unsubscribed = out")
    assert not err and consent_modes(m.get("consent")) == EMAIL_UNSUB
    assert option_text(m.get("consent"), real_schema.field("consent")) == (
        "Email Opt In: opt-ins only; Unsubscribed: opt-outs only")
    m2, err = edit(real_schema, m, opt="Email Opt In: sometimes")
    assert "consent settings look like" in err and consent_modes(m2.get("consent")) == EMAIL_UNSUB
    # Removing a column in the grid keeps the other's setting; adding one leaves it to be chosen.
    m3, _ = edit(real_schema, m, src=["Unsubscribed", "SMS"])
    assert consent_modes(m3.get("consent")) == {"Unsubscribed": "out"}
    assert "'SMS'" in row_problem(real_schema, m3, "consent")


def test_how_choices_for_consent(real_schema):
    m, err = edit(real_schema, consent("in"), how="Copy column")
    assert m.get("consent").transform == "consent" and not err  # a copy of a consent column is 'Consent columns'
    m, err = edit(real_schema, consent("in"), how="Split column")
    assert m.get("consent").transform == "consent" and "reads each column with its own consent setting" in err
    m, err = edit(real_schema, consent("in"), how="Translate values")
    assert not err and m.get("consent").params == {"modes": {"Email Opt In": "in"}}
    m, err = edit(real_schema, m, opt="Yes=opt-in; No=opt-out")
    assert not err and m.get("consent").params == {"modes": {"Email Opt In": "in"}, "value_map": [
        {"from": "Yes", "to": "opt-in"}, {"from": "No", "to": "opt-out"}]}
    m, _ = edit(real_schema, m, how="Leave blank")
    m, _ = edit(real_schema, m, how="Fixed value", opt="false")
    assert m.get("consent").params == {"value": "false"} and not row_problem(real_schema, m, "consent")
    other = Mapping(fields={"transactions_last": FieldMapping(sources=["A"], transform="copy")})
    o, err = edit(real_schema, other, field="transactions_last", how="Consent columns")
    assert o.get("transactions_last").transform == "copy" and "only for consent" in err


# --- reports ----------------------------------------------------------------------------------------------------------

def test_side_by_side_what_changed_workbook(real_schema):
    df = pd.DataFrame({"Phone": PHONES[:3], "Email Opt In": ["Y", "N", "Y"], "Unsubscribed": ["", "", "Yes"]})
    m = consent(EMAIL_UNSUB, sources=("Email Opt In", "Unsubscribed")).normalized(real_schema, list(df.columns))
    res = convert(df, real_schema, m)
    plan = column_plan(real_schema, m, list(df.columns))
    h = build_compare_html(source_rows(df, res.audit), res.audit, [0, 1, 2], real_schema, plan)
    assert 'class="n c-out"' in h and "(left blank: consent setting)" in h and 'class="n c-uns"' in h
    assert "Unsubscribed: Yes" in h and "unsubscribed by an opt-outs-only column" in h
    assert "← Email Opt In (opt-ins only) + Unsubscribed (opt-outs only)" in h
    row = field_change_summary(res.audit, real_schema, m).set_index("Field").loc["consent"]
    assert (row["Left blank (consent setting)"], row["Unsubscribed (opt-out column)"]) == (1, 1)
    assert comparison_xlsx(source_rows(df, res.audit), res.audit, real_schema, plan, "t")[:2] == b"PK"


def test_number_history_records_each_column_and_setting(tmp_path, real_schema):
    st_ = Store(tmp_path / "c.db")
    data = b"Phone,Email Opt In,Unsubscribed\n516-555-0101,Y,No\n516-555-0102,Y,Yes\n516-555-0103,N,\n"
    iid = st_.create_import(username="noah", filename="email.csv", data=data, schema_label=real_schema.label,
                            schema_json=real_schema.model_dump_json(),
                            mapping=consent(EMAIL_UNSUB, sources=("Email Opt In", "Unsubscribed")),
                            read_options={"header_row": 0})
    rec = run_import(st_, iid)
    assert rec.summary["consent_counts"] == {"consent": {"true": 1, "false": 1, "left_blank": 1}}
    [e] = numberlog.lookup(st_, "5165550102", real_schema).entries
    assert (e["consent"], e["consent_value"]) == ("false", "Email Opt In: Y / Unsubscribed: Yes")
    assert e["consent_from"] == "Email Opt In (opt-ins only) + Unsubscribed (opt-outs only)"
    [e] = numberlog.lookup(st_, "5165550103", real_schema).entries
    assert (e["consent"], e["consent_value"]) == ("", "Email Opt In: N")


def test_rules_and_templates_still_never_map_consent(real_schema):
    df = pd.DataFrame({"Phone": PHONES[:2], "Email Opt In": ["Y", "N"], "Unsubscribed": ["No", "Yes"]})
    m = initial_mapping(real_schema, df, profile_columns(df), None, None, use_ai=False).mapping
    assert m.get("consent").transform == "none"


def test_claude_never_sees_or_sets_consent_settings(real_schema):
    """Consent is manual_only, so Claude never maps it; its "Consent columns" transform and settings are a
    person's only."""
    assert "consent (" not in _transform_docs()
    enum = _output_schema(real_schema, ["A"])["properties"]["mappings"]["items"]["properties"]["transform"]["enum"]
    assert "consent" not in enum
    entries = [{"target_field": f, "transform": t, "sources": ["Email Opt In"], "params": {"modes": {"x": "in"}},
                "confidence": "high", "reason": "x"}
               for f, t in (("consent", "copy"), ("transactions_last", "copy"), ("calls_last", "consent"))]
    got, dropped = parse_mapping_entries(entries, real_schema, ["Phone", "Email Opt In"])
    assert got.get("consent").transform == "none" and "consent: only a person maps this field" in dropped
    assert got.get("transactions_last").params == {}
    assert got.get("calls_last").transform == "none" and "calls_last: unknown transform 'consent'" in dropped


# --- the app ----------------------------------------------------------------------------------------------------------

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


def _start_disabled(at) -> bool:
    return next(b for b in at.button if "Start import" in b.label).disabled


def test_mapping_page_asks_each_column_and_blocks_until_answered(app_data):
    did = app_data.create_draft("local", "optin.csv", b"Name,Phone,Email Opt In,Unsubscribed\n"
                                                      b"Ann,516-555-0101,Y,No\nBo,516-555-0102,N,Yes\n")
    at = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=60)
    at.query_params["draft"] = did
    at.run()
    assert not at.exception and not [r for r in at.radio if "Opt In" in r.label or "Unsub" in r.label]
    m = at.session_state["mapping"]
    m.fields["consent"] = FieldMapping(sources=["Email Opt In", "Unsubscribed"], transform="consent",
                                       params={"modes": {"Email Opt In": "in"}}, origin="user")
    at.session_state["mapping"] = m
    at.run()
    email, unsub = [r for r in at.radio if r.label in ("`Email Opt In`", "`Unsubscribed`")]
    assert (email.value, unsub.value) == ("in", None)
    assert any("'Unsubscribed'" in w.value for w in at.warning) and _start_disabled(at)
    unsub.set_value("out").run()
    assert not at.exception
    assert consent_modes(at.session_state["mapping"].get("consent")) == EMAIL_UNSUB
    assert not _start_disabled(at)


def test_a_setting_changed_on_a_kept_first_non_empty_reads_the_columns_together(app_data):
    """A 0.22.0 draft restored after the upgrade keeps First non-empty until a person changes it; changing a
    column's setting under the field table is such a change."""
    did = app_data.create_draft("local", "two.csv", b"Phone,Email Opt In,Unsubscribed\n516-555-0101,Y,Yes\n")
    at = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=60)
    at.query_params["draft"] = did
    at.run()
    m = at.session_state["mapping"]
    m.fields["consent"] = FieldMapping(sources=["Email Opt In", "Unsubscribed"], transform="coalesce",
                                       params={"modes": {"Email Opt In": "in", "Unsubscribed": "in"}}, origin="user")
    at.session_state["mapping"] = m
    at.run()
    assert any("First non-empty" in c.value for c in at.caption)
    unsub = next(r for r in at.radio if r.label == "`Unsubscribed`")
    unsub.set_value("out").run()
    fm = at.session_state["mapping"].get("consent")
    assert (fm.transform, consent_modes(fm)) == ("consent", EMAIL_UNSUB)


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
    """Imports made before 0.22.0 have consent columns without a setting: Re-run asks for each first."""
    old = consent(None, "coalesce", sources=("Email Opt In", "Unsubscribed"))  # as 0.21 stored it
    iid = app_data.create_import(username="local", filename="old.csv", schema_label=real_schema.label,
                                 data=b"Phone,Email Opt In,Unsubscribed\n516-555-0101,Y,No\n516-555-0102,N,Yes\n",
                                 schema_json=real_schema.model_dump_json(), mapping=old,
                                 read_options={"header_row": 0})
    run_import(app_data, iid)
    at = _imports_page(tmp_path, iid)
    radios = [r for r in at.radio if "Before re-running" in r.label]
    assert len(radios) == 2
    assert next(b for b in at.button if "Re-run this import" in b.label).disabled
    radios[0].set_value("in").run()
    assert next(b for b in at.button if "Re-run this import" in b.label).disabled
    [r for r in at.radio if "Before re-running" in r.label][1].set_value("out").run()
    button = next(b for b in at.button if "Re-run this import" in b.label)
    assert not button.disabled
    button.click().run()
    new = max(app_data.list_imports(), key=lambda r: r.id)
    assert new.id != iid and new.parent_id == iid and consent_modes(new.mapping.get("consent")) == EMAIL_UNSUB
    # Its columns are now read together (found by the second independent test: First non-empty let Email Opt In = Y
    # hide Unsubscribed = Yes even though the person had just said Unsubscribed records opt-outs).
    assert new.mapping.get("consent").transform == "consent"
