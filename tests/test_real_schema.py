"""Tests against the real target format (schemas/target_schema.yaml — generic import format)."""

import json
from types import SimpleNamespace as NS

import pandas as pd

from converter import normalizers as N
from converter.ai import ClaudeAssistant, _output_schema, parse_mapping_entries
from converter.editor import apply_row_edit
from converter.engine import convert
from converter.heuristics import auto_map, is_consent_column
from converter.mapping import FieldMapping, Mapping, profile_columns
from converter.output import write_output
from tests.conftest import read_sample

HEADERS = ["phone_number", "consent", "calls_last", "transactions_last", "last_contacted", "in_sms_last",
           "redemptions_last", "spin_last", "birthday", "anniversary"]


def norm(schema, field, value):
    f = schema.field(field)
    return N.normalize(value, f, {}, N.prepare(f, [value], {}))


def run(samples, schema, name, **kw):
    rr = read_sample(samples, name, **kw)
    m = auto_map(schema, profile_columns(rr.df))
    return rr, m, convert(rr.df, schema, m, [rr.excel_row_number(i) for i in range(len(rr.df))])


def test_headers_exact_and_in_order(real_schema):
    assert [f.header for f in real_schema.fields] == HEADERS
    out = write_output(pd.DataFrame(columns=HEADERS), real_schema).decode()
    assert out == ",".join(HEADERS) + "\n"  # every header kept even with no rows


def test_phone_is_ten_digits(real_schema):
    for raw in ["(212) 555-1234", "+1 212-555-1234", "12125551234", "212.555.1234", "2125551234.0"]:
        assert norm(real_schema, "phone_number", raw) == ("2125551234", None)
    assert norm(real_schema, "phone_number", "(416) 555-0123")[0] == "4165550123"  # Canada: 10 digits too
    assert norm(real_schema, "phone_number", "555-0199")[1]
    assert norm(real_schema, "phone_number", "+44 20 7946 0958")[1]


def test_activity_datetime_format(real_schema):
    cases = {
        "03/07/2026 09:05 AM": "03/07/2026 09:05 AM",
        "3/7/2026 9:05": "03/07/2026 09:05 AM",
        "2026-11-22 16:30:59": "11/22/2026 04:30 PM",   # seconds dropped
        "11/22/25 4:30pm": "11/22/2025 04:30 PM",
        "03/07/2026": "03/07/2026 12:00 AM",           # date only → midnight
        "2026-03-07T14:22:01Z": "03/07/2026 02:22 PM",  # time zone dropped, wall time kept
        "0000-00-00 00:00:00": "",
        "Never": "",
    }
    for field in HEADERS[2:8]:
        for raw, expected in cases.items():
            assert norm(real_schema, field, raw) == (expected, None), (field, raw)


def test_birthday_anniversary_no_year_is_1600(real_schema):
    for field in ("birthday", "anniversary"):
        assert norm(real_schema, field, "09/12") == ("09/12/1600", None)
        assert norm(real_schema, field, "Sep 12") == ("09/12/1600", None)
        assert norm(real_schema, field, "September 12th") == ("09/12/1600", None)
        assert norm(real_schema, field, "12-Sep") == ("09/12/1600", None)
        assert norm(real_schema, field, "02/29") == ("02/29/1600", None)
        assert norm(real_schema, field, "09/12/1600") == ("09/12/1600", None)
        assert norm(real_schema, field, "1985-07-04") == ("07/04/1985", None)
        assert norm(real_schema, field, "1990-02-28 00:00:00") == ("02/28/1990", None)
        assert norm(real_schema, field, "02/30")[1]


def test_consent_is_always_blank(real_schema):
    df = pd.DataFrame({"Phone": ["2125551234", "2125551235"], "Opt-In": ["true", "false"]})
    # Even a mapping that tries to fill consent produces blanks.
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Phone"], transform="copy"),
                        "consent": FieldMapping(sources=["Opt-In"], transform="copy")})
    res = convert(df, real_schema, m)
    assert res.output["consent"].tolist() == ["", ""]
    m2 = Mapping(fields={"phone_number": FieldMapping(sources=["Phone"], transform="copy"),
                         "consent": FieldMapping(transform="constant", params={"value": "true"})})
    assert convert(df, real_schema, m2).output["consent"].tolist() == ["", ""]
    assert m.normalized(real_schema, list(df.columns)).get("consent").transform == "none"


def test_consent_cannot_be_set_by_rules_editor_or_claude(real_schema):
    assert is_consent_column("Opt-In Status") and is_consent_column("SMS Consent") and is_consent_column("Subscribed")
    assert not is_consent_column("Last Text Received")
    df = pd.DataFrame({"Mobile": ["2125551234"], "Consent": ["true"], "Opt In Date": ["10/01/2026"]})
    m = auto_map(real_schema, profile_columns(df)).normalized(real_schema, list(df.columns))
    assert m.get("consent").transform == "none"
    assert "Consent" not in m.used_sources() and "Opt In Date" not in m.used_sources()

    m2, err = apply_row_edit(real_schema, m, "consent", {"Source column(s)": ["Consent"]}, "How", "Source column(s)",
                             "Option")
    assert err and m2.get("consent").transform == "none"

    out_schema = _output_schema(real_schema, list(df.columns))
    assert "consent" not in out_schema["properties"]["mappings"]["items"]["properties"]["target_field"]["enum"]
    parsed, dropped = parse_mapping_entries(
        [{"target_field": "consent", "transform": "copy", "sources": ["Consent"], "params": {},
          "confidence": "high", "reason": ""}], real_schema, list(df.columns))
    assert "consent" not in parsed.fields and dropped


def test_claude_prompt_marks_consent_fixed(real_schema):
    captured = {}

    class Client:
        class messages:
            @staticmethod
            def create(**kw):
                captured.update(kw)
                return NS(stop_reason="end_turn", content=[NS(type="text", text=json.dumps(
                    {"mappings": [], "notes": ""}))], usage=None)

    df = pd.DataFrame({"Mobile": ["2125551234"]})
    ClaudeAssistant("k", client=Client()).propose_mapping(real_schema, profile_columns(df))
    system = captured["system"][0]["text"]
    assert "Always blank and not mappable" in system and "consent" in system
    assert "year 1600" in system


def test_dedupe_across_all_fields(real_schema):
    df = pd.DataFrame({
        "Mobile": ["2125551234", "212-555-1234", "2125551234", "2125551234"],
        "Last Visit": ["10/05/2026 7:42 PM", "10/05/2026 7:42 PM", "10/07/2026 8:10 PM", "10/07/2026 8:10 PM"],
        "Birthday": ["09/12", "09/12", "09/12", "1990-09-12"],
    })
    m = auto_map(real_schema, profile_columns(df))
    res = convert(df, real_schema, m)
    # Row 2 duplicates row 1 exactly. Rows 3 and 4 differ from row 1 by date, and from each other by birthday.
    assert res.duplicates == 1 and res.imported == 3


def test_restaurant_crm_export(samples, real_schema):
    rr, m, res = run(samples, real_schema, "restaurant_crm_export.csv")
    assert m.get("phone_number").sources == ["Mobile"]
    assert m.get("transactions_last").sources == ["Last Visit"]
    assert m.get("calls_last").sources == ["Last Call"]
    assert m.get("in_sms_last").sources == ["Last Text Received"]
    assert m.get("last_contacted").sources == ["Last Campaign Sent"]
    assert m.get("redemptions_last").sources == ["Last Reward Redeemed"]
    assert m.get("birthday").sources == ["Birthday"] and m.get("anniversary").sources == ["Anniversary"]
    assert m.get("spin_last").transform == "none"
    assert "Opt-In Status" not in m.used_sources() and "Created" not in m.used_sources()

    assert (res.imported, res.rejected, res.duplicates) == (5, 2, 1)
    csv_text = write_output(res.output, real_schema).decode().splitlines()
    assert csv_text[0] == ",".join(HEADERS)
    assert csv_text[1] == ("5165550100,,10/01/2026 12:15 PM,10/05/2026 07:42 PM,10/03/2026 11:00 AM,"
                           "10/04/2026 06:01 PM,09/28/2026 08:30 PM,,09/12/1600,06/20/2015")
    assert csv_text[2] == "6315550101,,,10/03/2026 06:15 PM,10/03/2026 11:00 AM,,,,03/03/1600,"
    assert csv_text[5] == "6315550188,,,,,,,,09/12/1600,12/31/1600"
    assert all(line.split(",")[1] == "" for line in csv_text[1:])  # consent blank everywhere


def test_loyalty_activity_xlsx(samples, real_schema):
    rr, m, res = run(samples, real_schema, "loyalty_activity.xlsx")
    assert rr.header_row == 2
    assert m.get("spin_last").sources == ["Last Spin"] and m.get("birthday").sources == ["DOB"]
    assert m.get("redemptions_last").sources == ["Last Redeemed"]
    out = res.output
    assert out["phone_number"].tolist() == ["5165550100", "2015550111", "9175550123"]
    assert out["spin_last"].tolist() == ["10/04/2026 06:30 PM", "10/01/2026 09:05 AM", ""]
    assert out["birthday"].tolist() == ["02/28/1990", "09/12/1600", "02/29/1600"]
    assert res.rejected == 1  # "N/A" phone
