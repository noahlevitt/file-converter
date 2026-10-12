"""Dates from any file reach a last-activity field; last_contacted is never filled.

Files often name their date column only "Date", "Start Time" or "Timestamp". Those dates must still be imported:
in a call log as calls_last, otherwise as transactions_last (fields marked ``generic_date`` in the target format).
Dates that aren't a customer's latest activity ("Date Created", sign-up, first visit, expiry) never are, and
last_contacted (when we last texted the customer) is always blank: no imported file contains it.
"""

import pandas as pd
import pytest

from converter.editor import apply_column_edit
from converter.engine import convert
from converter.heuristics import auto_map
from converter.mapping import FieldMapping, Mapping, profile_columns

DATES = ["10/01/2026 3:15 PM", "10/02/2026 1:00 PM", "10/03/2026 9:30 AM"]
OLD = ["01/02/2025", "03/04/2025", "05/06/2025"]
PHONES = ["5165550100", "9175551234", "6315550188"]


def mapped(real_schema, df):
    """{field: sources} of the automatic mapping (fields left unmapped are omitted)."""
    m = auto_map(real_schema, profile_columns(df)).normalized(real_schema, list(df.columns))
    return {f: fm.sources for f, fm in m.fields.items() if fm.transform != "none"}


@pytest.mark.parametrize("columns,date_col", [
    ({"Date": DATES, "Direction": ["inbound", "outbound", "inbound"], "From": PHONES, "To": ["5163000366"] * 3},
     "Date"),
    ({"Start Time": DATES, "Caller ID": PHONES, "Dialed Number": ["5163000366"] * 3}, "Start Time"),
    ({"Timestamp": DATES, "Phone": PHONES, "Duration": ["45", "0", "120"]}, "Timestamp"),
    ({"Date Created": OLD, "Call Time": DATES, "From": PHONES, "To": ["5163000366"] * 3}, "Call Time"),
])
def test_call_log_date_goes_to_calls_last(real_schema, columns, date_col):
    """In a call log (call words, or From and To columns) the date column is the last call."""
    m = mapped(real_schema, pd.DataFrame(columns))
    assert m["calls_last"] == [date_col] and "transactions_last" not in m


def test_call_log_keeps_every_number(real_schema):
    """Every number of a call log is imported, the client's own line too (cell-phone checks happen later)."""
    df = pd.DataFrame({"Date": DATES, "From": PHONES, "To": ["5163000366"] * 3})
    m = auto_map(real_schema, profile_columns(df))
    res = convert(df, real_schema, m)
    assert sorted(set(res.output["phone_number"])) == sorted(PHONES + ["5163000366"])
    assert res.output["calls_last"].tolist()[:2] == ["10/01/2026 03:15 PM"] * 2


def test_separate_date_and_time_columns_are_combined(real_schema):
    """A phone-system export with 'Date' and 'Time' columns gives one date-time."""
    df = pd.DataFrame({"Date": ["10/01/2026", "10/02/2026", "10/03/2026"], "Time": ["3:15 PM", "1:00 PM", "9:30 AM"],
                       "Type": ["Incoming", "Outgoing", "Missed"], "From Number": PHONES,
                       "To Number": ["5163000366"] * 3})
    m = auto_map(real_schema, profile_columns(df))
    assert m.get("calls_last").sources == ["Date", "Time"] and m.get("calls_last").transform == "concat"
    assert convert(df, real_schema, m).output["calls_last"].iloc[0] == "10/01/2026 03:15 PM"


@pytest.mark.parametrize("columns,expected", [
    ({"Phone": PHONES, "Date": DATES}, {"transactions_last": ["Date"]}),
    ({"Phone": PHONES, "Updated": DATES, "Date Created": OLD}, {"transactions_last": ["Updated"]}),
    # an order date wins over a sign-up date; the sign-up date and DOB never become activity dates
    ({"Phone": PHONES, "Order Date": DATES, "Sign Up Date": OLD, "DOB": ["03/15/1985", "07/04/1990", "12/25/1979"]},
     {"transactions_last": ["Order Date"], "birthday": ["DOB"]}),
    # two generic dates: the one that says "last" first, the other to the second field
    ({"Phone": PHONES, "Date": OLD, "Last Updated": DATES}, {"transactions_last": ["Last Updated"],
                                                             "calls_last": ["Date"]}),
])
def test_other_files_date_goes_to_transactions_last(real_schema, columns, expected):
    """Outside call logs a generic date is the latest visit/transaction; a second one fills calls_last."""
    m = mapped(real_schema, pd.DataFrame(columns))
    m.pop("phone_number")
    assert m == expected


@pytest.mark.parametrize("header", ["Date Created", "Created At", "createdAt", "Sign Up Date", "Member Since",
                                    "First Visit", "Expiration Date", "Opt-In Date", "Joined"])
def test_dates_that_are_not_a_last_activity_stay_out(real_schema, header):
    """Creation, sign-up, first-visit, expiry and opt-in dates are never used as a last activity."""
    m = mapped(real_schema, pd.DataFrame({"Phone": PHONES, header: DATES}))
    assert set(m) == {"phone_number"}


def test_end_time_is_not_used_as_a_second_activity(real_schema):
    """'Start Time' is the last call; the call's 'End Time' doesn't also become the last transaction."""
    df = pd.DataFrame({"Start Time": DATES, "End Time": DATES, "Caller ID": PHONES})
    m = mapped(real_schema, df)
    assert m["calls_last"] == ["Start Time"] and "transactions_last" not in m


def test_named_activity_columns_are_unchanged(real_schema):
    """Columns that name their activity keep their own field; generic dates only fill what's left."""
    df = pd.DataFrame({"Phone": PHONES, "Last Visit": DATES, "Last Call": DATES, "Date": OLD})
    m = mapped(real_schema, df)
    assert m["transactions_last"] == ["Last Visit"] and m["calls_last"] == ["Last Call"]
    assert "Date" not in sum(m.values(), [])


def test_last_contacted_is_never_filled(real_schema):
    """last_contacted is when we last texted the customer: always blank, never mapped, not even by hand."""
    df = pd.DataFrame({"Phone": PHONES, "Last Campaign Sent": DATES, "Last Message Sent": DATES})
    m = auto_map(real_schema, profile_columns(df))
    assert m.get("last_contacted").transform == "none"
    assert not ({"Last Campaign Sent", "Last Message Sent"} & m.used_sources())  # not reused as another date
    m2, msg = apply_column_edit(real_schema, m, "Last Campaign Sent", targets=["last_contacted"])
    assert m2.get("last_contacted").transform == "none" and "always blank" in " ".join(msg)
    forced = Mapping(fields={"phone_number": FieldMapping(sources=["Phone"], transform="copy"),
                             "last_contacted": FieldMapping(sources=["Last Campaign Sent"], transform="copy")})
    assert set(convert(df, real_schema, forced).output["last_contacted"]) == {""}
