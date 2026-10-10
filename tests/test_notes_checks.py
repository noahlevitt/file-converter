"""Column notes (applied by Claude) and the mistake check (rules + optional Claude review).

Notes let the person who knows the file explain what a column means ("Wed" = wedding anniversary) so it lands in
the right field, and they travel with templates. The mistake check catches what would otherwise go out wrong:
sign-up dates mapped as birthdays, visits dated in the future, 7-digit phones, an opt-in column that people expect
to import as consent. In the optional Claude review the sample rows have phone numbers masked, and any consent
mapping it suggests is refused.
"""

import json
from datetime import date
from types import SimpleNamespace as NS

import pandas as pd

from converter.ai import GENERAL_NOTE, ClaudeAssistant
from converter.checks import build_review_payload, mask_phone, pick_sample_rows, run_checks
from converter.engine import convert
from converter.heuristics import auto_map
from converter.mapping import FieldMapping, Mapping, profile_columns
from converter.services import check_for_mistakes, initial_mapping, merge
from converter.storage import Store

# Fixed "today" so the "in the future" and "within the last two years" checks give the same result on any day.
TODAY = date(2026, 10, 9)


class Client:
    """Fake Anthropic client: answers every request with `payload` as JSON text and records it in `calls`."""

    def __init__(self, payload):
        """Store the canned answer and start an empty call log."""
        self.payload, self.calls = payload, []
        outer = self

        class M:
            """Stands in for `client.messages`."""

            def create(self, **kw):
                """Record the request and return the canned answer."""
                outer.calls.append(kw)
                return NS(stop_reason="end_turn", content=[NS(type="text", text=json.dumps(outer.payload))],
                          usage=NS(input_tokens=1, output_tokens=1, cache_read_input_tokens=0))
        self.messages = M()


def frame():
    """A small CRM-style file used by several tests.

    Phones are in 'Cell', last visits in 'Visited' (some with a time), wedding anniversaries sit under the unhelpful
    header 'Wed', and 'Opt In' holds yes/no flags that must never become consent.
    """
    return pd.DataFrame({
        "Cell": ["5165550100", "6315550101", "9175550123", "2015550111", "7185550145", "5165550131"],
        "Visited": ["10/05/2026 7:42 PM", "10/03/2026", "10/02/2026 1:05 PM", "09/30/2026", "10/01/2026", "10/04/2026"],
        "Wed": ["06/20/2015", "", "03/03/2010", "", "", ""],
        "Opt In": ["yes", "no", "yes", "yes", "yes", "no"],
    })


# ---------------------------------------------------------------- notes
def test_notes_travel_with_mapping_and_templates(tmp_path, real_schema):
    """Column notes and the general note survive normalize and merge and are saved and loaded with templates.

    Notes for columns the file doesn't have are dropped, so the next file of this kind comes with its explanations.
    """
    df = frame()
    m = auto_map(real_schema, profile_columns(df)).normalized(real_schema, list(df.columns))
    m.column_notes = {"Visited": "last visit date", "Wed": "wedding anniversary", "Gone": "x"}
    m.general_note = "dates are month-first"
    n = m.normalized(real_schema, list(df.columns))
    assert n.column_notes == {"Visited": "last visit date", "Wed": "wedding anniversary"}  # missing column dropped
    assert merge(n, Mapping(fields={"spin_last": FieldMapping()})).column_notes == n.column_notes
    st = Store(tmp_path / "t.db")
    st.save_template("CRM", list(df.columns), n, {}, real_schema.name, "noah")
    sug = initial_mapping(real_schema, df, profile_columns(df), st, None)
    assert sug.source == "template" and sug.mapping.column_notes["Wed"] == "wedding anniversary"
    assert sug.mapping.general_note == "dates are month-first"


def test_apply_notes_with_claude(real_schema):
    """Claude turns notes into mapping changes and reports a status per note; a change to consent is refused.

    Also checks that the notes (including the general note) are in the prompt and that the general note is a valid
    'column' in the answer schema.
    """
    df = frame()
    prof = profile_columns(df)
    m = auto_map(real_schema, prof).normalized(real_schema, list(df.columns))
    # Claude's answer: two legitimate changes and one attempt to fill consent from 'Opt In', which must be dropped.
    payload = {
        "changes": [
            {"target_field": "transactions_last", "transform": "copy", "sources": ["Visited"], "params": {},
             "confidence": "high", "reason": "note says last visit"},
            {"target_field": "anniversary", "transform": "copy", "sources": ["Wed"], "params": {},
             "confidence": "high", "reason": "note says wedding anniversary"},
            {"target_field": "consent", "transform": "copy", "sources": ["Opt In"], "params": {},
             "confidence": "high", "reason": "should be refused"},
        ],
        "note_results": [
            {"column": "Visited", "status": "applied", "message": "Mapped to transactions_last."},
            {"column": "Wed", "status": "applied", "message": "Mapped to anniversary."},
            {"column": "Opt In", "status": "not_applied", "message": "Consent is always blank."},
            {"column": GENERAL_NOTE, "status": "already_correct", "message": "Dates are read month-first."},
        ],
        "summary": "Applied 2 notes.",
    }
    client = Client(payload)
    res = ClaudeAssistant("k", client=client).apply_notes(
        real_schema, prof, m, {"Visited": "last visit date", "Wed": "wedding anniversary", "Opt In": "this is consent"},
        "dates are month-first")
    assert res.mapping.get("transactions_last").sources == ["Visited"]
    assert res.mapping.get("anniversary").sources == ["Wed"]
    assert "consent" not in res.mapping.fields and any("consent" in d for d in res.dropped)
    assert [r["status"] for r in res.note_results] == ["applied", "applied", "not_applied", "already_correct"]
    sent = client.calls[0]["messages"][0]["content"]
    assert "wedding anniversary" in sent and "(general note)" in sent
    schema_sent = client.calls[0]["output_config"]["format"]["schema"]
    assert GENERAL_NOTE in schema_sent["properties"]["note_results"]["items"]["properties"]["column"]["enum"]


def test_apply_notes_without_notes_makes_no_call(real_schema):
    """With no notes there is nothing to apply: no Claude call (no cost) and no mapping changes."""
    client = Client({})
    res = ClaudeAssistant("k", client=client).apply_notes(real_schema, profile_columns(frame()), Mapping(), {}, "")
    assert not client.calls and not res.mapping.fields


# ---------------------------------------------------------------- rule checks
def _checks(schema, df, mapping, **kw):
    """Convert the file with `mapping` and return the rule-check findings, judged against the fixed TODAY."""
    res = convert(df, schema, mapping)
    return run_checks(schema, mapping, df, res.audit, profile_columns(df), today=TODAY, **kw)


def test_rule_checks_catch_common_mistakes(real_schema):
    """One file with the usual mistakes produces one finding for each.

    A 'Created' column mapped as birthday, a last order dated after today, 7-digit phones, an opt-in column that
    won't be imported (consent stays blank) and a date column that isn't used.
    """
    # Three phones have only 7 digits; 'Last Order' starts with 12/01/2026, which is after TODAY.
    df = pd.DataFrame({
        "Phone": ["5165550100", "555-0100", "555-1234", "6315550101", "300-1111", "9175550123"],
        "Created": ["10/01/2026", "09/15/2026", "08/20/2025", "10/05/2026", "07/04/2026", "01/02/2026"],
        "Last Order": ["12/01/2026", "10/01/2026", "10/02/2026", "10/03/2026", "10/04/2026", "10/05/2026"],
        "Last Txn Date": ["10/01/2026"] * 6,
        "Opt In": ["yes"] * 6,
    })
    m = Mapping(fields={
        "phone_number": FieldMapping(sources=["Phone"], transform="copy"),
        "birthday": FieldMapping(sources=["Created"], transform="copy"),          # wrong column
        "transactions_last": FieldMapping(sources=["Last Order"], transform="copy"),
    }).normalized(real_schema, list(df.columns))
    findings = _checks(real_schema, df, m)
    text = " | ".join(f"{f.severity}:{f.field}:{f.issue}" for f in findings)
    assert "within the last two years" in text                    # birthday column holds recent dates
    assert "in the future" in text                                # 12/01/2026 last order
    assert "bad rows" in text and "7 digits" in text              # area code missing
    assert "'Opt In' is not imported" in text                     # consent note
    assert "contains dates but isn't imported" in text            # Last Txn Date unused


def test_rule_checks_wrong_column_and_mixed_order(real_schema):
    """A text column mapped to a date field is an error pointing at the wrong column; mixed date order is flagged."""
    # 'Call' mixes 15/03 and 28/02 (only possible day-first) with 03/15 and 02/28 (only possible month-first).
    df = pd.DataFrame({
        "Mobile": ["5165550100", "6315550101", "9175550123", "2015550111", "7185550145"],
        "Spin": ["blue", "red", "green", "blue", "red"],
        "Call": ["15/03/2026", "03/15/2026", "28/02/2026", "02/28/2026", "10/09/2026"],
    })
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Mobile"], transform="copy"),
                        "spin_last": FieldMapping(sources=["Spin"], transform="copy"),
                        "calls_last": FieldMapping(sources=["Call"], transform="copy")}
                ).normalized(real_schema, list(df.columns))
    findings = _checks(real_schema, df, m)
    spin = [f for f in findings if f.field == "spin_last"]
    assert spin and spin[0].severity == "error" and "wrong column" in spin[0].suggestion
    assert any("mixes month-first" in f.issue for f in findings if f.field == "calls_last")


def test_clean_file_has_no_warnings(samples, real_schema):
    """A clean file produces no errors or warnings; false alarms would teach people to ignore the check."""
    from tests.conftest import read_sample
    rr = read_sample(samples, "loyalty_activity.xlsx")
    m = auto_map(real_schema, profile_columns(rr.df))
    res = convert(rr.df, real_schema, m)
    findings = run_checks(real_schema, m, rr.df, res.audit, profile_columns(rr.df), today=TODAY)
    assert not [f for f in findings if f.severity in ("error", "warning")]


# ---------------------------------------------------------------- Claude review
def test_mask_phone():
    """Phones shown to Claude keep their shape, area code (and +1) and last two digits; the rest is hidden.

    That is enough to judge whether a column holds phones without sending customers' numbers. Values too short
    to be a phone are left alone.
    """
    assert mask_phone("(516) 555-0100") == "(516) ###-##00"
    assert mask_phone("5165550100") == "516#####00"
    assert mask_phone("+1 516-555-0100") == "+1 516-###-##00"
    assert mask_phone("555-1234") == "555-##34"
    assert mask_phone("12") == "12"


def test_review_payload_and_claude_findings(real_schema):
    """The review sends a small, sorted sample of rows with phones masked, and Claude's answer is merged in.

    Its findings come back tagged as Claude's next to the rule findings, along with its suggested mapping change.
    """
    df = frame()
    prof = profile_columns(df)
    m = auto_map(real_schema, prof).normalized(real_schema, list(df.columns))
    res = convert(df, real_schema, m)
    picked = pick_sample_rows(res.audit, limit=4)
    assert len(picked) == 4 and picked == sorted(picked)
    payload = build_review_payload(real_schema, m, df, res.audit, [], mask_phones=True)
    dumped = json.dumps(payload)
    assert "5165550100" not in dumped and "516#####00" in dumped   # phones masked both sides
    # Claude's answer: one finding about the unused 'Wed' column plus the matching mapping change.
    review = {
        "findings": [{"severity": "warning", "field": "anniversary", "column": "Wed",
                      "issue": "'Wed' looks like wedding anniversaries but isn't imported.",
                      "suggestion": "Map 'Wed' to anniversary."}],
        "changes": [{"target_field": "anniversary", "transform": "copy", "sources": ["Wed"], "params": {},
                     "confidence": "medium", "reason": "values are past dates; header 'Wed'"}],
        "summary": "One likely missed column.",
    }
    findings, ai, err = check_for_mistakes(real_schema, m, df, res.audit, prof,
                                           ClaudeAssistant("k", client=Client(review)), use_claude=True)
    assert err is None and ai.summary == "One likely missed column."
    assert any(f.source == "claude" and f.field == "anniversary" for f in findings)
    assert ai.mapping.get("anniversary").sources == ["Wed"]


def test_check_without_claude_is_rules_only(real_schema):
    """Without a Claude assistant (no API key), asking for the review quietly runs the rule checks only."""
    df = frame()
    m = auto_map(real_schema, profile_columns(df)).normalized(real_schema, list(df.columns))
    res = convert(df, real_schema, m)
    findings, ai, err = check_for_mistakes(real_schema, m, df, res.audit, profile_columns(df), None, use_claude=True)
    assert ai is None and err is None and all(f.source == "rules" for f in findings)
