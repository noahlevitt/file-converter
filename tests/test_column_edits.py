"""Editing the mapping from the source-column side, skipping columns, and catching columns in the wrong field.

A free-text column with one stray phone number in it must never become a phone column: its junk would be read as
numbers and could text a stranger. Edits made from the column side must update the fields predictably, a skipped
column must stay out everywhere (templates, rules, and what is sent to Claude), and consent can never be picked.
"""

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
    """A five-customer file used throughout this module; keyword arguments add or replace columns.

    'Occupation' is the trap: someone typed free text and one real-looking phone number into it, so 1 of its
    2 non-blank values is a phone. That is enough to look phone-ish but it is not a phone column.
    """
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
    """Auto-map picks only the real Phone column, not a free-text column that happens to hold one number."""
    df = _file()
    m = auto_map(real_schema, profile_columns(df))
    assert m.get("phone_number").sources == ["Phone"]
    assert "Occupation" not in m.used_sources()


def test_stray_phone_column_not_used_even_without_a_real_phone_column(real_schema):
    """With no phone column at all, auto-map still doesn't fall back to the stray-phone column.

    An unmapped phone field is visible and easy to fix; a wrong one quietly imports junk.
    """
    df = _file().drop(columns="Phone")
    m = auto_map(real_schema, profile_columns(df))
    assert "Occupation" not in m.get("phone_number").sources


def test_phone_columns_still_found(real_schema):
    """The stray-phone guard must not hide real phone columns, found either by header or by their values."""
    # 'Home Phone' is almost empty but has a phone header; 'Col 7' has a meaningless header but 4 of 5 values are
    # phone numbers.
    df = _file(**{"Home Phone": ["2015550199", "", "", "", ""], "Col 7": ["2015550101", "2015550102",
                                                                          "2015550103", "2015550104", ""]})
    m = auto_map(real_schema, profile_columns(df.drop(columns="Phone")))
    assert set(m.get("phone_number").sources) == {"Home Phone", "Col 7"}  # phone header, or clearly phone values


def test_phone_header_words():
    """Headers that mean a textable number are recognised; number-like headers that aren't phones are not.

    Order, member and account numbers would text random digits, and a fax line can't receive texts.
    """
    for h in ["Mobile", "Cell #", "Contact Number", "Tel.", "SMS Number", "Phone No"]:
        assert is_phone_header(h), h
    for h in ["Occupation", "Order Number", "Member No", "Fax", "Account #", "Notes"]:
        assert not is_phone_header(h), h


# ---------------------------------------------------------------- mistake logic

def test_column_warning_for_value_typed_into_wrong_column(real_schema):
    """If a mapping does use the stray-phone column, it is flagged with how many values are phones and a sample.

    Showing '5 De Mayo' next to the field name makes it obvious to the user that this is the wrong column.
    """
    df = _file()
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Phone", "Occupation"], transform="coalesce")})
    w = column_warnings(real_schema, m, profile_columns(df))
    assert list(w) == ["Occupation"]
    field, msg = w["Occupation"]
    assert field == "phone_number" and "1 of its 2" in msg and "5 De Mayo" in msg


def test_no_warning_for_real_phone_columns(real_schema):
    """A sparse phone column with an 'n/a' placeholder is still a real phone column and gets no warning.

    False alarms would teach users to ignore the warnings that matter.
    """
    df = _file(Mobile=["2015550101", "", "", "", "n/a"])
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Mobile", "Phone"], transform="coalesce")})
    assert column_warnings(real_schema, m, profile_columns(df)) == {}


def test_mistake_check_reports_the_column(real_schema):
    """The full mistake check reports the wrong column as a warning on 'Occupation' that suggests ticking Skip."""
    df = _file()
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Phone", "Occupation"], transform="coalesce")})
    res = convert(df, real_schema, m)
    findings = run_checks(real_schema, m, df, res.audit, profile_columns(df))
    assert any(f.column == "Occupation" and f.severity == "warning" and "Skip" in f.suggestion for f in findings)


# ---------------------------------------------------------------- editing from the column side

def _base(real_schema, df):
    """The rule-based mapping normalized for this file's columns, i.e. what the app shows before any edit."""
    return auto_map(real_schema, profile_columns(df)).normalized(real_schema, list(df.columns))


def test_send_column_to_another_field_replaces_previous_source(real_schema):
    """Sending a column to a single-source field replaces the old source, marks it user-chosen and says what it was.

    Sending the original column back restores the first mapping, so an edit can always be undone.
    """
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Phone"], transform="copy"),
                        "transactions_last": FieldMapping(sources=["Last Visit"], transform="copy")})
    m2, msgs = apply_column_edit(real_schema, m, "Visit Date", targets=["transactions_last"])
    assert m2.get("transactions_last").sources == ["Visit Date"] and m2.get("transactions_last").origin == "user"
    assert "Last Visit" not in m2.used_sources()
    assert any("was Last Visit" in x for x in msgs)
    # and back
    m3, _ = apply_column_edit(real_schema, m2, "Last Visit", targets=["transactions_last"])
    assert m3.get("transactions_last").sources == ["Last Visit"]


def test_adding_a_column_to_the_phone_field_adds_a_number(real_schema):
    """A second column on phone_number switches to one row per number, so both numbers get texts.

    Removing that column again goes back to a plain copy of the first one.
    """
    df = _file(**{"Phone 2": ["2015550199", "", "", "", ""]})
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Phone"], transform="copy")})
    m2, msgs = apply_column_edit(real_schema, m, "Phone 2", targets=["phone_number"])
    assert m2.get("phone_number").sources == ["Phone", "Phone 2"] and m2.get("phone_number").transform == "each_row"
    assert "one row per different number" in msgs[0]
    assert convert(df, real_schema, m2).imported == 6  # Ana has two numbers → two rows
    m3, _ = apply_column_edit(real_schema, m2, "Phone 2", targets=[])
    assert m3.get("phone_number").sources == ["Phone"] and m3.get("phone_number").transform == "copy"


def test_clear_goes_to_removes_column_from_coalesce(real_schema):
    """Clearing a column's 'goes to' removes it from a multi-source field and, with one source left, makes it a copy.

    The user gets a plain-English confirmation of what changed.
    """
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Phone", "Occupation"], transform="coalesce")})
    m2, msgs = apply_column_edit(real_schema, m, "Occupation", targets=[])
    assert m2.get("phone_number").sources == ["Phone"] and m2.get("phone_number").transform == "copy"
    assert msgs == ["'Occupation' no longer goes to phone_number."]


def test_column_to_several_fields_and_last_source_removed(real_schema):
    """One column can feed several fields; removing a field's only source leaves that field unmapped ('none')."""
    m = Mapping(fields={"transactions_last": FieldMapping(sources=["Last Visit"], transform="copy")})
    m2, _ = apply_column_edit(real_schema, m, "Last Visit", targets=["transactions_last", "in_sms_last"])
    assert m2.targets_of("Last Visit") == ["transactions_last", "in_sms_last"]
    m3, _ = apply_column_edit(real_schema, m2, "Last Visit", targets=["in_sms_last"])
    assert m3.get("transactions_last").transform == "none" and m3.targets_of("Last Visit") == ["in_sms_last"]


def test_consent_cannot_be_picked(real_schema):
    """Consent can't be chosen for any column: it is refused with an 'always blank' message.

    Consent must come from the customer, never from a spreadsheet column, or people who never agreed get texts.
    """
    m2, msgs = apply_column_edit(real_schema, Mapping(), "Occupation", targets=["consent"])
    assert m2.targets_of("Occupation") == [] and "always blank" in msgs[0]


def test_skip_and_unskip(real_schema):
    """Skip removes a column from every field, keeps its note, and holds against anything that maps it later.

    Unticking Skip maps nothing by itself; choosing a field for the column both maps it and un-skips it.
    """
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
    """Choosing a skipped column from the field grid (the other editing side) un-skips it and keeps all notes."""
    m = Mapping(excluded_columns=["Occupation"], column_notes={"Phone": "main number"}, general_note="g")
    # The last three arguments are the grid's column titles: How, Source column(s), Option.
    m2, err = apply_row_edit(real_schema, m, "birthday", {"Source column(s)": ["Occupation"]}, "How",
                             "Source column(s)", "Option")
    assert err is None and m2.excluded_columns == [] and m2.get("birthday").sources == ["Occupation"]
    assert m2.column_notes == {"Phone": "main number"} and m2.general_note == "g"


def test_skipped_columns_survive_a_template_round_trip(real_schema):
    """Skipped columns are saved in templates and come back for the next file, minus columns that file lacks.

    Templates saved before Skip existed still load, with nothing skipped.
    """
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Phone"], transform="copy")},
                excluded_columns=["Occupation", "Gone"])
    back = Mapping.model_validate_json(m.model_dump_json()).normalized(real_schema, list(_file().columns))
    assert back.excluded_columns == ["Occupation"]  # columns missing from the file are dropped
    assert Mapping.model_validate({"fields": {}}).excluded_columns == []  # older templates still load


def test_claude_never_sees_skipped_columns(real_schema):
    """Neither the name nor the values of a skipped column are sent to Claude.

    People skip columns because they hold junk or private data, so it must not leave the app.
    """
    captured = {}

    class Client:
        """Fake Anthropic client: records the request instead of calling the API."""

        class messages:
            """Stands in for `client.messages`."""

            @staticmethod
            def create(**kw):
                """Capture the request and answer with an empty mapping."""
                captured.update(kw)
                return NS(stop_reason="end_turn", content=[NS(type="text", text=json.dumps(
                    {"mappings": [], "notes": ""}))], usage=None)

    df = _file()
    ClaudeAssistant("k", client=Client()).propose_mapping(real_schema, profile_columns(df),
                                                          current=Mapping(excluded_columns=["Occupation"]))
    # The message content may be plain text or a list of blocks; serialise it so the check covers both.
    sent = captured["messages"][0]["content"]
    sent = sent if isinstance(sent, str) else json.dumps(sent)
    assert "Occupation" not in sent and "5 De Mayo" not in sent
