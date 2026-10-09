"""Editing the mapping from the source-column side, skipping columns, and catching columns in the wrong field."""

import json
from types import SimpleNamespace as NS

import pandas as pd

from converter.ai import ClaudeAssistant
from converter.checks import column_warnings, run_checks
from converter.editor import apply_column_edit, apply_row_edit
from converter.engine import convert
from converter.heuristics import auto_map, is_phone_header
from converter.mapping import FieldMapping, Mapping, profile_columns
from converter.services import merge


def _file(**extra):
    data = {
        "Name": ["Ana", "Ben", "Cy", "Di", "Ed"],
        "Phone": ["2015550111", "2015550112", "2015550113", "2015550114", "2015550115"],
        "Occupation": ["5 De Mayo 12 00", "201 461 3645", "", "", ""],
        "Last Visit": ["10/01/2026 7:00 PM"] * 5,
    }
    data.update(extra)
    return pd.DataFrame(data)


# ---------------------------------------------------------------- rule mapping

def test_stray_phone_in_occupation_is_not_a_phone_column(real_schema):
    df = _file()
    m = auto_map(real_schema, profile_columns(df))
    assert m.get("phone_number").sources == ["Phone"]
    assert "Occupation" not in m.used_sources()


def test_stray_phone_column_not_used_even_without_a_real_phone_column(real_schema):
    df = _file().drop(columns="Phone")
    m = auto_map(real_schema, profile_columns(df))
    assert "Occupation" not in m.get("phone_number").sources


def test_phone_columns_still_found(real_schema):
    df = _file(**{"Home Phone": ["2015550199", "", "", "", ""], "Col 7": ["2015550101", "2015550102",
                                                                          "2015550103", "2015550104", ""]})
    m = auto_map(real_schema, profile_columns(df.drop(columns="Phone")))
    assert set(m.get("phone_number").sources) == {"Home Phone", "Col 7"}  # phone header, or clearly phone values


def test_phone_header_words():
    for h in ["Mobile", "Cell #", "Contact Number", "Tel.", "SMS Number", "Phone No"]:
        assert is_phone_header(h), h
    for h in ["Occupation", "Order Number", "Member No", "Fax", "Account #", "Notes"]:
        assert not is_phone_header(h), h


# ---------------------------------------------------------------- mistake logic

def test_column_warning_for_value_typed_into_wrong_column(real_schema):
    df = _file()
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Phone", "Occupation"], transform="coalesce")})
    w = column_warnings(real_schema, m, profile_columns(df))
    assert list(w) == ["Occupation"]
    field, msg = w["Occupation"]
    assert field == "phone_number" and "1 of its 2" in msg and "5 De Mayo" in msg


def test_no_warning_for_real_phone_columns(real_schema):
    df = _file(Mobile=["2015550101", "", "", "", "n/a"])
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Mobile", "Phone"], transform="coalesce")})
    assert column_warnings(real_schema, m, profile_columns(df)) == {}


def test_mistake_check_reports_the_column(real_schema):
    df = _file()
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Phone", "Occupation"], transform="coalesce")})
    res = convert(df, real_schema, m)
    findings = run_checks(real_schema, m, df, res.audit, profile_columns(df))
    assert any(f.column == "Occupation" and f.severity == "warning" and "Skip" in f.suggestion for f in findings)


# ---------------------------------------------------------------- editing from the column side

def _base(real_schema, df):
    return auto_map(real_schema, profile_columns(df)).normalized(real_schema, list(df.columns))


def test_send_column_to_another_field_replaces_previous_source(real_schema):
    df = _file()
    m = _base(real_schema, df)
    m2, msgs = apply_column_edit(real_schema, m, "Occupation", targets=["phone_number"])
    assert m2.get("phone_number").sources == ["Occupation"] and m2.get("phone_number").origin == "user"
    assert "Phone" not in m2.used_sources()
    assert any("was Phone" in x for x in msgs)
    # and back
    m3, _ = apply_column_edit(real_schema, m2, "Phone", targets=["phone_number"])
    assert m3.get("phone_number").sources == ["Phone"]


def test_clear_goes_to_removes_column_from_coalesce(real_schema):
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Phone", "Occupation"], transform="coalesce")})
    m2, msgs = apply_column_edit(real_schema, m, "Occupation", targets=[])
    assert m2.get("phone_number").sources == ["Phone"] and m2.get("phone_number").transform == "copy"
    assert msgs == ["'Occupation' no longer goes to phone_number."]


def test_column_to_several_fields_and_last_source_removed(real_schema):
    m = Mapping(fields={"transactions_last": FieldMapping(sources=["Last Visit"], transform="copy")})
    m2, _ = apply_column_edit(real_schema, m, "Last Visit", targets=["transactions_last", "last_contacted"])
    assert m2.targets_of("Last Visit") == ["transactions_last", "last_contacted"]
    m3, _ = apply_column_edit(real_schema, m2, "Last Visit", targets=["last_contacted"])
    assert m3.get("transactions_last").transform == "none" and m3.targets_of("Last Visit") == ["last_contacted"]


def test_consent_cannot_be_picked(real_schema):
    m2, msgs = apply_column_edit(real_schema, Mapping(), "Occupation", targets=["consent"])
    assert m2.targets_of("Occupation") == [] and "always blank" in msgs[0]


def test_skip_and_unskip(real_schema):
    df = _file()
    cols = list(df.columns)
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Phone", "Occupation"], transform="coalesce")},
                column_notes={"Occupation": "someone typed phones here"})
    m2, _ = apply_column_edit(real_schema, m, "Occupation", skip=True)
    assert m2.excluded_columns == ["Occupation"] and "Occupation" not in m2.used_sources()
    assert m2.column_notes == {"Occupation": "someone typed phones here"}

    # Skipped columns stay out when anything later tries to map them (template, rules, Claude via merge).
    sneaky = Mapping(fields={"phone_number": FieldMapping(sources=["Occupation"], transform="copy")})
    merged = merge(m2, sneaky).normalized(real_schema, cols)
    assert "Occupation" not in merged.used_sources() and merged.excluded_columns == ["Occupation"]

    # Unticking Skip doesn't map it anywhere by itself; picking a field does, and un-skips it.
    m3, _ = apply_column_edit(real_schema, m2, "Occupation", skip=False)
    assert m3.excluded_columns == [] and "Occupation" not in m3.used_sources()
    m4, _ = apply_column_edit(real_schema, m2, "Occupation", targets=["birthday"])
    assert m4.excluded_columns == [] and m4.get("birthday").sources == ["Occupation"]


def test_choosing_skipped_column_in_field_grid_unskips_and_keeps_notes(real_schema):
    m = Mapping(excluded_columns=["Occupation"], column_notes={"Phone": "main number"}, general_note="g")
    m2, err = apply_row_edit(real_schema, m, "birthday", {"Source column(s)": ["Occupation"]}, "How",
                             "Source column(s)", "Option")
    assert err is None and m2.excluded_columns == [] and m2.get("birthday").sources == ["Occupation"]
    assert m2.column_notes == {"Phone": "main number"} and m2.general_note == "g"


def test_skipped_columns_survive_a_template_round_trip(real_schema):
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Phone"], transform="copy")},
                excluded_columns=["Occupation", "Gone"])
    back = Mapping.model_validate_json(m.model_dump_json()).normalized(real_schema, list(_file().columns))
    assert back.excluded_columns == ["Occupation"]  # columns missing from the file are dropped
    assert Mapping.model_validate({"fields": {}}).excluded_columns == []  # older templates still load


def test_claude_never_sees_skipped_columns(real_schema):
    captured = {}

    class Client:
        class messages:
            @staticmethod
            def create(**kw):
                captured.update(kw)
                return NS(stop_reason="end_turn", content=[NS(type="text", text=json.dumps(
                    {"mappings": [], "notes": ""}))], usage=None)

    df = _file()
    ClaudeAssistant("k", client=Client()).propose_mapping(real_schema, profile_columns(df),
                                                          current=Mapping(excluded_columns=["Occupation"]))
    sent = captured["messages"][0]["content"]
    sent = sent if isinstance(sent, str) else json.dumps(sent)
    assert "Occupation" not in sent and "5 De Mayo" not in sent
