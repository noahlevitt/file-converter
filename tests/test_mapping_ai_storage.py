"""Auto-mapping rules, the optional Claude layer, the field-grid editor, the SQLite store and output writing.

Claude is optional and never trusted blindly: its suggestions are checked against the schema and the file's
columns, an outage falls back to the rules, and a "repair" may not invent digits that weren't in the file. Saved
templates let repeat files skip Claude entirely. The output must keep phones and ZIPs as text, so a leading zero or
a "+" isn't lost when the file is opened in Excel.
"""

import json
from types import SimpleNamespace as NS

import pandas as pd
import pytest

from converter.ai import AIError, ClaudeAssistant, accept_repair, parse_mapping_entries
from converter.editor import apply_row_edit, option_text, parse_option
from converter.heuristics import auto_map
from converter.mapping import FieldMapping, Mapping, header_fingerprint, profile_columns
from converter.output import write_output
from converter.services import initial_mapping, validate_repairs
from converter.storage import Store
from tests.conftest import read_sample


class FakeClient:
    """Stand-in for the Anthropic client, so no test calls the real API.

    Each messages.create call is recorded in `calls` and answered with `payload` as JSON text after a thinking
    block, like a real response; `raise_exc` simulates an outage.
    """

    def __init__(self, payload=None, stop="end_turn", raise_exc=None):
        """Set up the canned answer; `stop` lets a test simulate a reply cut off at max_tokens."""
        self.calls = []
        outer = self

        class M:
            """Stands in for `client.messages`."""

            def create(self, **kw):
                """Record the request, then raise the configured error or return the canned response."""
                outer.calls.append(kw)
                if raise_exc:
                    raise raise_exc
                return NS(stop_reason=stop, content=[NS(type="thinking"), NS(type="text", text=json.dumps(payload))],
                          usage=NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=0))

        self.messages = M()


# ---------------------------------------------------------------- heuristics
def test_auto_map_pos(samples, schema):
    """Rules alone map the POS export: two phone columns, a full name, a full address and the date columns."""
    rr = read_sample(samples, "pos_guest_export.csv")
    m = auto_map(schema, profile_columns(rr.df))
    assert m.get("phone").transform == "each_row" and m.get("phone").sources == ["Mobile #", "Home Phone"]
    assert m.get("first_name").transform == "name_part" and m.get("first_name").params == {"part": "first"}
    assert m.get("city").transform == "address_part"
    assert m.get("signup_date").sources == ["Join Date"]
    assert m.get("birthday").sources == ["Birthday"]


def test_auto_map_no_header_uses_value_types(samples, schema):
    """Without a header row, columns are mapped by what their values look like (phones, emails)."""
    rr = read_sample(samples, "no_header.tsv")
    m = auto_map(schema, profile_columns(rr.df))
    assert m.get("phone").sources == ["Column 1"] and m.get("email").sources == ["Column 2"]


# ---------------------------------------------------------------- AI layer
def test_ai_mapping_validates_output(samples, schema):
    """Claude's mapping is used only after validation, and the request is built the expected way.

    Field names are matched case-insensitively, entries naming a column or field that doesn't exist are dropped,
    and the call uses the configured model, a JSON-schema answer and a cached system prompt. Only headers and
    sample values are sent, not the whole file.
    """
    rr = read_sample(samples, "eu_style_semicolon.csv")
    prof = profile_columns(rr.df)
    # One good suggestion (field name in upper case), one for a column the file doesn't have, one for a field
    # the schema doesn't have.
    payload = {"notes": "ok", "mappings": [
        {"target_field": "SIGNUP_DATE", "transform": "copy", "sources": ["registered"], "params": {"date_order": "DMY"},
         "confidence": "high", "reason": "day-first"},
        {"target_field": "city", "transform": "copy", "sources": ["Nope"], "params": {}, "confidence": "high",
         "reason": "x"},
        {"target_field": "bogus", "transform": "copy", "sources": ["Cell"], "params": {}, "confidence": "high",
         "reason": "x"},
    ]}
    fake = FakeClient(payload)
    sug = initial_mapping(schema, rr.df, prof, None, ClaudeAssistant("k", client=fake))
    assert sug.source == "rules+ai"
    assert sug.mapping.get("signup_date").params == {"date_order": "DMY"}
    assert sug.mapping.get("signup_date").origin == "ai"
    assert sug.mapping.get("city").transform == "none"
    call = fake.calls[0]
    # Pins the configured model name; update this deliberately when the model is changed.
    assert call["model"] == "claude-sonnet-5-5"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    # Only headers + sample values go to Claude, not the whole file.
    assert "Giulia" in call["messages"][0]["content"]


def test_ai_failure_falls_back_to_rules(samples, schema):
    """If Claude can't be reached, the rule-based mapping is used with a note; an outage never blocks an import."""
    rr = read_sample(samples, "eu_style_semicolon.csv")
    fake = FakeClient(raise_exc=RuntimeError("network down"))
    sug = initial_mapping(schema, rr.df, profile_columns(rr.df), None, ClaudeAssistant("k", client=fake))
    assert sug.source == "rules" and any("unavailable" in n for n in sug.notes)
    assert sug.mapping.get("phone").sources == ["Cell"]


def test_ai_refusal_and_cutoff_raise(schema):
    """A reply cut off at max_tokens raises AIError instead of being half-parsed into a partial mapping."""
    a = ClaudeAssistant("k", client=FakeClient({"mappings": [], "notes": ""}, stop="max_tokens"))
    with pytest.raises(AIError):
        a.propose_mapping(schema, [])


def test_parse_mapping_entries_rejects_bad_transform(schema):
    """An entry with an invalid option (a name part called 'nose') is dropped and reported, not applied."""
    m, dropped = parse_mapping_entries(
        [{"target_field": "first_name", "transform": "name_part", "sources": ["Name"], "params": {"part": "nose"},
          "confidence": "high", "reason": ""}], schema, ["Name"])
    assert "first_name" not in m.fields and dropped


def test_repair_guard(schema):
    """Claude's value repairs are only accepted when they don't invent data.

    A repaired phone must use the original's own digits in order (letter O for zero and spelled-out digits are
    fine), so 'call me' or a 7-digit fragment can't become someone's number. Date repairs are split into
    accepted and rejected.
    """
    phone = schema.field("phone")
    assert accept_repair(phone, "516 555-O1OO", "+15165550100")
    assert accept_repair(phone, "five one six five five five zero one zero zero", "+15165550100")
    # No digits to start from, and too few digits (the repair would have to add three of its own).
    assert not accept_repair(phone, "call me", "+15165550000")
    assert not accept_repair(phone, "5165550", "+15165550199")
    accepted, rejected = validate_repairs(schema, Mapping(), "signup_date",
                                          [("Sept 3rd twenty26", "09/03/2026"), ("??", "not a date")])
    assert accepted == {"Sept 3rd twenty26": "09/03/2026"} and len(rejected) == 1


# ---------------------------------------------------------------- editor grid
def test_editor_options(schema):
    """The free-text 'Option' cell parses into transform settings and is shown back the same way.

    Covers date order, a strptime format, split separator + position, value maps and name parts; bad input
    returns an error message instead of a setting.
    """
    f = schema.field("signup_date")
    assert parse_option("copy", f, "dmy") == ({"date_order": "DMY"}, None)
    assert parse_option("copy", f, "%d.%m.%Y") == ({"input_format": "%d.%m.%Y"}, None)
    # "#2" is the 2nd part for the user and is stored 0-based (index 1).
    assert parse_option("split", schema.field("first_name"), "- #2") == ({"separator": "-", "index": 1}, None)
    assert parse_option("map_values", schema.field("notes"), "Y=VIP; N=") == (
        {"value_map": [{"from": "Y", "to": "VIP"}, {"from": "N", "to": ""}]}, None)
    assert parse_option("name_part", schema.field("first_name"), "nose")[1]
    fm = FieldMapping(sources=["A"], transform="split", params={"separator": ",", "index": 1})
    assert option_text(fm, schema.field("first_name")) == ", #2"


def test_apply_row_edit(schema):
    """Choosing two source columns for the phone makes it one row per number; 'Leave blank / default' clears it.

    The last three arguments are the grid's column titles (How, Source column(s), Option).
    """
    m = Mapping(fields={"phone": FieldMapping()})
    m2, err = apply_row_edit(schema, m, "phone", {"Source column(s)": ["Mobile", "Home"]}, "How",
                             "Source column(s)", "Option")
    assert err is None and m2.get("phone").transform == "each_row" and m2.get("phone").origin == "user"
    m3, _ = apply_row_edit(schema, m2, "phone", {"How": "Leave blank / default"}, "How", "Source column(s)", "Option")
    assert m3.get("phone").transform == "none" and m3.get("phone").sources == []


# ---------------------------------------------------------------- storage
def test_store_users_templates_jobs(tmp_path, schema):
    """Store basics: logins, template matching and import records.

    Usernames ignore case, wrong passwords fail and short ones are refused. A template matches the same headers in
    any order or case exactly and a similar header set at 75% or more. Import records keep the uploaded bytes and can be
    found by file name or by #id.
    """
    s = Store(tmp_path / "t.db")
    s.add_user("Jane", "longpassword", is_admin=True)
    assert s.verify_user("jane", "longpassword")["is_admin"] is True
    assert s.verify_user("jane", "wrong") is None
    with pytest.raises(ValueError):
        s.add_user("bob", "short")

    headers = ["Guest Name", "Mobile #", "E-Mail"]
    m = Mapping(fields={"phone": FieldMapping(sources=["Mobile #"], transform="copy")})
    s.save_template("POS export", headers, m, {}, schema.name, "jane")
    # Same headers in another order and case → exact match; one extra column → similar enough.
    t, sim = s.find_template(["e-mail", "MOBILE #", "guest name"], schema.name)
    assert t.name == "POS export" and sim == 1.0
    t2, sim2 = s.find_template(headers + ["Notes"], schema.name)
    assert t2.name == "POS export" and 0.75 <= sim2 < 1
    assert s.find_template(["Totally", "Different"], schema.name)[0] is None
    assert header_fingerprint(headers) == header_fingerprint(list(reversed(headers)))

    iid = s.create_import(username="jane", filename="f.csv", data=b"Phone\n5165550100\n", schema_label=schema.label,
                          schema_json=schema.model_dump_json(), mapping=m, read_options={})
    rec = s.get_import(iid)
    assert rec.status == "queued" and s.source_path(rec).read_bytes().startswith(b"Phone")
    assert s.list_imports(search="f.csv")[0].id == iid and s.list_imports(search=f"#{iid}")[0].id == iid


def test_template_reused_without_ai(tmp_path, samples, schema):
    """A matching saved template is used directly and Claude isn't called, which saves cost and keeps repeats stable."""
    s = Store(tmp_path / "t.db")
    rr = read_sample(samples, "eu_style_semicolon.csv")
    prof = profile_columns(rr.df)
    m = auto_map(schema, prof)
    s.save_template("EU", list(rr.df.columns), m, {}, schema.name, "x")
    fake = FakeClient({"mappings": [], "notes": ""})
    sug = initial_mapping(schema, rr.df, prof, s, ClaudeAssistant("k", client=fake))
    assert sug.source == "template" and not fake.calls


def test_template_works_when_headers_differ_in_punctuation(tmp_path, real_schema):
    """Templates match headers ignoring spaces and punctuation, so their columns are found the same way (0.16.0).

    Before, a template made on "Last Visit Date" matched a file with "Last_Visit_Date" 100%, was applied, and then
    lost that column (only case was ignored when finding it): the dates were silently left out.
    """
    s = Store(tmp_path / "t.db")
    first = pd.DataFrame({"Phone": ["5165550100"], "Last Visit Date": ["10/01/2026"], "Birth Date": ["03/15/1985"]})
    m = initial_mapping(real_schema, first, profile_columns(first), None, None, use_ai=False).mapping
    m.excluded_columns, m.column_notes = ["Notes Col"], {"Birth Date": "year is often 1900"}
    s.save_template("Client", list(first.columns) + ["Notes Col"], m, {}, real_schema.name, "x")
    second = pd.DataFrame({"Phone": ["5165550100"], "Last_Visit_Date": ["10/01/2026"], "birth-date": ["03/15/1985"],
                           "notes_col": ["x"]})
    sug = initial_mapping(real_schema, second, profile_columns(second), s, None, use_ai=False)
    assert sug.source == "template"
    assert {k: v.sources for k, v in sug.mapping.fields.items() if v.transform != "none"} == {
        "phone_number": ["Phone"], "transactions_last": ["Last_Visit_Date"], "birthday": ["birth-date"]}
    assert sug.mapping.excluded_columns == ["notes_col"]
    assert sug.mapping.column_notes == {"birth-date": "year is often 1900"}


def test_punctuation_fallback_never_guesses_between_two_columns(real_schema):
    """The exact (case-insensitive) name wins; when only the punctuation-free form fits two columns, neither is used."""
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Phone"], transform="copy"),
                        "transactions_last": FieldMapping(sources=["Last-Visit"], transform="copy")})
    assert m.normalized(real_schema, ["Phone", "Last Visit", "Last_Visit"]).get("transactions_last").transform == "none"
    m.get("transactions_last").sources = ["last_visit"]
    assert m.normalized(real_schema, ["Phone", "Last Visit", "Last_Visit"]).get("transactions_last").sources == [
        "Last_Visit"]


# ---------------------------------------------------------------- output
def test_output_keeps_text(schema, tmp_path):
    """CSV and Excel output keep values as text, so a ZIP's leading zero and a phone's '+' survive."""
    df = pd.DataFrame({"Phone": ["+15165550100"], "Zip": ["07030"]})
    csv_bytes = write_output(df, schema)
    assert csv_bytes.decode() == "Phone,Zip\n+15165550100,07030\n"
    xlsx_schema = schema.model_copy(deep=True)
    xlsx_schema.output.format = "xlsx"
    p = tmp_path / "o.xlsx"
    p.write_bytes(write_output(df, xlsx_schema))
    back = pd.read_excel(p, dtype=str)
    assert back.iloc[0]["Zip"] == "07030" and back.iloc[0]["Phone"] == "+15165550100"
