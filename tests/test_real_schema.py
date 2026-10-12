"""Tests against the real target format (schemas/target_schema.yaml — generic import format).

This is exactly what the import system receives, so it is pinned down tightly: header names and order, 10-digit
US phone numbers, activity times as MM/DD/YYYY HH:MM AM/PM in the import's time zone, and 1600 for birthdays and
anniversaries without a year. Consent is always blank: it can only come from the customer, so no file column,
rule, editor choice or Claude suggestion may ever fill it.
"""

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

# The import's header row, in the required order. HEADERS[2:8] are the six activity timestamp columns.
HEADERS = ["phone_number", "consent", "calls_last", "transactions_last", "last_contacted", "in_sms_last",
           "redemptions_last", "spin_last", "birthday", "anniversary"]


def norm(schema, field, value):
    """Normalize one value into a real-schema field (with only itself as column context); return (output, error)."""
    f = schema.field(field)
    return N.normalize(value, f, {}, N.prepare(f, [value], {}))


def run(samples, schema, name, **kw):
    """Read, auto-map and convert a sample file, keeping file row numbers; return (read, mapping, result)."""
    rr = read_sample(samples, name, **kw)
    m = auto_map(schema, profile_columns(rr.df))
    return rr, m, convert(rr.df, schema, m, [rr.excel_row_number(i) for i in range(len(rr.df))])


def test_headers_exact_and_in_order(real_schema):
    """The output header row matches the import format exactly and in order, even when there are no rows."""
    assert [f.header for f in real_schema.fields] == HEADERS
    out = write_output(pd.DataFrame(columns=HEADERS), real_schema).decode()
    assert out == ",".join(HEADERS) + "\n"  # every header kept even with no rows


def test_phone_is_ten_digits(real_schema):
    """phone_number is written as 10 digits without +1; Canadian, 7-digit and other foreign numbers are errors."""
    for raw in ["(212) 555-1234", "+1 212-555-1234", "12125551234", "212.555.1234", "2125551234.0"]:
        assert norm(real_schema, "phone_number", raw) == ("2125551234", None)
    assert "not a US number" in norm(real_schema, "phone_number", "(416) 555-0123")[1]  # US only
    assert norm(real_schema, "phone_number", "555-0199")[1]
    assert norm(real_schema, "phone_number", "+44 20 7946 0958")[1]


def test_activity_datetime_format(real_schema):
    """All six activity columns use MM/DD/YYYY HH:MM AM/PM the same way.

    Seconds are dropped, a date alone means midnight, UTC is converted to Eastern, and placeholders are blank.
    """
    cases = {
        "03/07/2026 09:05 AM": "03/07/2026 09:05 AM",
        "3/7/2026 9:05": "03/07/2026 09:05 AM",
        "2026-11-22 16:30:59": "11/22/2026 04:30 PM",   # seconds dropped
        "11/22/25 4:30pm": "11/22/2025 04:30 PM",
        "03/07/2026": "03/07/2026 12:00 AM",           # date only → midnight
        "2026-03-07T14:22:01Z": "03/07/2026 09:22 AM",  # UTC → Eastern (EST before DST)
        "0000-00-00 00:00:00": "",
        "Never": "",
    }
    for field in HEADERS[2:8]:
        for raw, expected in cases.items():
            assert norm(real_schema, field, raw) == (expected, None), (field, raw)


def test_birthday_anniversary_no_year_is_1600(real_schema):
    """Both date fields write 1600 for a missing year, keep real years, allow Feb 29 and reject Feb 30."""
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
    """Consent stays blank even when a mapping copies an opt-in column or sets a constant; normalizing clears it.

    Texting someone because a spreadsheet said "true" is exactly what this guards against.
    """
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


def test_consent_is_only_ever_set_by_a_person(real_schema):
    """Consent is filled only when a person maps a column to it: never by rules, Claude, a template or a default.

    Consent-like columns (including an opt-in date) aren't auto-mapped anywhere, consent is left out of the fields
    Claude may answer with, a Claude entry for it is dropped, and a mapping that didn't come from a person is
    removed. A person's choice is converted to true / false; anything else stays blank (which changes nothing).
    """
    assert is_consent_column("Opt-In Status") and is_consent_column("SMS Consent") and is_consent_column("Subscribed")
    assert not is_consent_column("Last Text Received")
    df = pd.DataFrame({"Mobile": ["2125551234", "2125551235", "2125551236", "2125551237"],
                       "Consent": ["Y", "no", "Opted Out", "maybe"], "Opt In Date": ["10/01/2026"] * 4})
    m = auto_map(real_schema, profile_columns(df)).normalized(real_schema, list(df.columns))
    assert m.get("consent").transform == "none"
    assert "Consent" not in m.used_sources() and "Opt In Date" not in m.used_sources()

    out_schema = _output_schema(real_schema, list(df.columns))
    assert "consent" not in out_schema["properties"]["mappings"]["items"]["properties"]["target_field"]["enum"]
    parsed, dropped = parse_mapping_entries(
        [{"target_field": "consent", "transform": "copy", "sources": ["Consent"], "params": {},
          "confidence": "high", "reason": ""}], real_schema, list(df.columns))
    assert parsed.get("consent").transform == "none" and any("only a person" in d for d in dropped)

    # Anything that isn't the reviewer's own choice is removed (e.g. a template or an old saved mapping).
    for origin in ("rule", "ai", "template", ""):
        auto = Mapping(fields={"consent": FieldMapping(sources=["Consent"], transform="copy", origin=origin)})
        assert auto.normalized(real_schema, list(df.columns)).get("consent").transform == "none"

    # The reviewer maps it in the grid: values become true / false, unknown values stay blank.
    m2, err = apply_row_edit(real_schema, m, "consent", {"Source column(s)": ["Consent"]}, "How", "Source column(s)",
                             "Option")
    assert not err and m2.get("consent").origin == "user"
    assert convert(df, real_schema, m2).output["consent"].tolist() == ["true", "false", "false", ""]


def test_claude_prompt_marks_consent_fixed(real_schema):
    """The system prompt tells Claude that consent and last_contacted are mapped by a person only, and that 1600
    means no year."""
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

    df = pd.DataFrame({"Mobile": ["2125551234"]})
    ClaudeAssistant("k", client=Client()).propose_mapping(real_schema, profile_columns(df))
    system = captured["system"][0]["text"]
    assert "Mapped by a person only" in system and "consent" in system and "last_contacted" in system
    assert "year 1600" in system


def test_dedupe_across_all_fields(real_schema):
    """In the real format a duplicate must match on every field; same phone with other dates is a separate row."""
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
    """The CRM sample maps each activity column by its header and ignores Opt-In, Created and Last Campaign Sent.

    "Last Campaign Sent" names last_contacted, which is always blank (it means when we last texted the customer).

    The output CSV lines are pinned exactly, including blank consent on every row.
    """
    rr, m, res = run(samples, real_schema, "restaurant_crm_export.csv")
    assert m.get("phone_number").sources == ["Mobile"]
    assert m.get("transactions_last").sources == ["Last Visit"]
    assert m.get("calls_last").sources == ["Last Call"]
    assert m.get("in_sms_last").sources == ["Last Text Received"]
    assert m.get("last_contacted").transform == "none" and "Last Campaign Sent" not in m.used_sources()
    assert m.get("redemptions_last").sources == ["Last Reward Redeemed"]
    assert m.get("birthday").sources == ["Birthday"] and m.get("anniversary").sources == ["Anniversary"]
    assert m.get("spin_last").transform == "none"
    assert "Opt-In Status" not in m.used_sources() and "Created" not in m.used_sources()

    # Rejected: no phone and a 7-digit phone. Duplicate: Maria's exact repeat (her later visit is kept).
    assert (res.imported, res.rejected, res.duplicates) == (5, 2, 1)
    csv_text = write_output(res.output, real_schema).decode().splitlines()
    assert csv_text[0] == ",".join(HEADERS)
    # Maria Lopez: every activity column filled, birthday "09/12" without a year, anniversary with its real year.
    assert csv_text[1] == ("5165550100,,10/01/2026 12:15 PM,10/05/2026 07:42 PM,,"
                           "10/04/2026 06:01 PM,09/28/2026 08:30 PM,,09/12/1600,06/20/2015")
    # Tom Becker: ISO timestamps reformatted, birthday "Mar 3" → 03/03/1600.
    assert csv_text[2] == "6315550101,,,10/03/2026 06:15 PM,,,,,03/03/1600,"
    # Daniel Cho: "0000-00-00 00:00:00" last visit → blank; "September 12th" and "12/31" → 1600.
    assert csv_text[5] == "6315550188,,,,,,,,09/12/1600,12/31/1600"
    assert all(line.split(",")[1] == "" for line in csv_text[1:])  # consent blank everywhere
    assert all(line.split(",")[4] == "" for line in csv_text[1:])  # last_contacted blank everywhere


def test_loyalty_activity_xlsx(samples, real_schema):
    """The loyalty workbook: header found below a title, Excel date-time cells formatted, no-year DOBs → 1600.

    An 'N/A' phone is a missing phone, so that row is rejected.
    """
    rr, m, res = run(samples, real_schema, "loyalty_activity.xlsx")
    # 0-based: the header is the 3rd row, below "Loyalty Activity Report" and a blank row.
    assert rr.header_row == 2
    assert m.get("spin_last").sources == ["Last Spin"] and m.get("birthday").sources == ["DOB"]
    assert m.get("redemptions_last").sources == ["Last Redeemed"]
    out = res.output
    assert out["phone_number"].tolist() == ["5165550100", "2015550111", "9175550123"]
    assert out["spin_last"].tolist() == ["10/04/2026 06:30 PM", "10/01/2026 09:05 AM", ""]
    # A real Excel date (1990), then the text values "12-Sep" and "02/29" with no year.
    assert out["birthday"].tolist() == ["02/28/1990", "09/12/1600", "02/29/1600"]
    assert res.rejected == 1  # "N/A" phone
