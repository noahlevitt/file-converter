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
    def __init__(self, payload=None, stop="end_turn", raise_exc=None):
        self.calls = []
        outer = self

        class M:
            def create(self, **kw):
                outer.calls.append(kw)
                if raise_exc:
                    raise raise_exc
                return NS(stop_reason=stop, content=[NS(type="thinking"), NS(type="text", text=json.dumps(payload))],
                          usage=NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=0))

        self.messages = M()


# ---------------------------------------------------------------- heuristics
def test_auto_map_pos(samples, schema):
    rr = read_sample(samples, "pos_guest_export.csv")
    m = auto_map(schema, profile_columns(rr.df))
    assert m.get("phone").transform == "coalesce" and m.get("phone").sources == ["Mobile #", "Home Phone"]
    assert m.get("first_name").transform == "name_part" and m.get("first_name").params == {"part": "first"}
    assert m.get("city").transform == "address_part"
    assert m.get("signup_date").sources == ["Join Date"]
    assert m.get("birthday").sources == ["Birthday"]


def test_auto_map_no_header_uses_value_types(samples, schema):
    rr = read_sample(samples, "no_header.tsv")
    m = auto_map(schema, profile_columns(rr.df))
    assert m.get("phone").sources == ["Column 1"] and m.get("email").sources == ["Column 2"]


# ---------------------------------------------------------------- AI layer
def test_ai_mapping_validates_output(samples, schema):
    rr = read_sample(samples, "eu_style_semicolon.csv")
    prof = profile_columns(rr.df)
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
    assert call["model"] == "claude-sonnet-5-5"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    # Only headers + sample values go to Claude, not the whole file.
    assert "Giulia" in call["messages"][0]["content"]


def test_ai_failure_falls_back_to_rules(samples, schema):
    rr = read_sample(samples, "eu_style_semicolon.csv")
    fake = FakeClient(raise_exc=RuntimeError("network down"))
    sug = initial_mapping(schema, rr.df, profile_columns(rr.df), None, ClaudeAssistant("k", client=fake))
    assert sug.source == "rules" and any("unavailable" in n for n in sug.notes)
    assert sug.mapping.get("phone").sources == ["Cell"]


def test_ai_refusal_and_cutoff_raise(schema):
    a = ClaudeAssistant("k", client=FakeClient({"mappings": [], "notes": ""}, stop="max_tokens"))
    with pytest.raises(AIError):
        a.propose_mapping(schema, [])


def test_parse_mapping_entries_rejects_bad_transform(schema):
    m, dropped = parse_mapping_entries(
        [{"target_field": "first_name", "transform": "name_part", "sources": ["Name"], "params": {"part": "nose"},
          "confidence": "high", "reason": ""}], schema, ["Name"])
    assert "first_name" not in m.fields and dropped


def test_repair_guard(schema):
    phone = schema.field("phone")
    assert accept_repair(phone, "516 555-O1OO", "+15165550100")
    assert accept_repair(phone, "five one six five five five zero one zero zero", "+15165550100")
    assert not accept_repair(phone, "call me", "+15165550000")
    assert not accept_repair(phone, "5165550", "+15165550199")
    accepted, rejected = validate_repairs(schema, Mapping(), "signup_date",
                                          [("Sept 3rd twenty26", "09/03/2026"), ("??", "not a date")])
    assert accepted == {"Sept 3rd twenty26": "09/03/2026"} and len(rejected) == 1


# ---------------------------------------------------------------- editor grid
def test_editor_options(schema):
    f = schema.field("signup_date")
    assert parse_option("copy", f, "dmy") == ({"date_order": "DMY"}, None)
    assert parse_option("copy", f, "%d.%m.%Y") == ({"input_format": "%d.%m.%Y"}, None)
    assert parse_option("split", schema.field("first_name"), "- #2") == ({"separator": "-", "index": 1}, None)
    assert parse_option("map_values", schema.field("notes"), "Y=VIP; N=") == (
        {"value_map": [{"from": "Y", "to": "VIP"}, {"from": "N", "to": ""}]}, None)
    assert parse_option("name_part", schema.field("first_name"), "nose")[1]
    fm = FieldMapping(sources=["A"], transform="split", params={"separator": ",", "index": 1})
    assert option_text(fm, schema.field("first_name")) == ", #2"


def test_apply_row_edit(schema):
    m = Mapping(fields={"phone": FieldMapping()})
    m2, err = apply_row_edit(schema, m, "phone", {"Source column(s)": ["Mobile", "Home"]}, "How",
                             "Source column(s)", "Option")
    assert err is None and m2.get("phone").transform == "coalesce" and m2.get("phone").origin == "user"
    m3, _ = apply_row_edit(schema, m2, "phone", {"How": "Leave blank / default"}, "How", "Source column(s)", "Option")
    assert m3.get("phone").transform == "none" and m3.get("phone").sources == []


# ---------------------------------------------------------------- storage
def test_store_users_templates_jobs(tmp_path, schema):
    s = Store(tmp_path / "t.db")
    s.add_user("Jane", "longpassword", is_admin=True)
    assert s.verify_user("jane", "longpassword")["is_admin"] is True
    assert s.verify_user("jane", "wrong") is None
    with pytest.raises(ValueError):
        s.add_user("bob", "short")

    headers = ["Guest Name", "Mobile #", "E-Mail"]
    m = Mapping(fields={"phone": FieldMapping(sources=["Mobile #"], transform="copy")})
    s.save_template("POS export", headers, m, {}, schema.name, "jane")
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
    s = Store(tmp_path / "t.db")
    rr = read_sample(samples, "eu_style_semicolon.csv")
    prof = profile_columns(rr.df)
    m = auto_map(schema, prof)
    s.save_template("EU", list(rr.df.columns), m, {}, schema.name, "x")
    fake = FakeClient({"mappings": [], "notes": ""})
    sug = initial_mapping(schema, rr.df, prof, s, ClaudeAssistant("k", client=fake))
    assert sug.source == "template" and not fake.calls


# ---------------------------------------------------------------- output
def test_output_keeps_text(schema, tmp_path):
    df = pd.DataFrame({"Phone": ["+15165550100"], "Zip": ["07030"]})
    csv_bytes = write_output(df, schema)
    assert csv_bytes.decode() == "Phone,Zip\n+15165550100,07030\n"
    xlsx_schema = schema.model_copy(deep=True)
    xlsx_schema.output.format = "xlsx"
    p = tmp_path / "o.xlsx"
    p.write_bytes(write_output(df, xlsx_schema))
    back = pd.read_excel(p, dtype=str)
    assert back.iloc[0]["Zip"] == "07030" and back.iloc[0]["Phone"] == "+15165550100"
