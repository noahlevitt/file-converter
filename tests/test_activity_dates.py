"""Every file's last date reaches a last-activity field - one date per row; last_contacted only by hand.

Files often name their date column only "Date", "Start Time" or "Timestamp". Those dates must still be imported:
in a call log as calls_last, otherwise as transactions_last (fields marked ``generic_date`` in the target format).
A row has one last date: a column named for its activity ("Last Visit") wins, then the best generic date, and only
when the file has neither, a creation date ("Date Created") - better than no date at all, never alongside a real
last date. Birthday, expiry and opt-in dates are never used. last_contacted (when we last texted the customer) is
only filled when a person maps a column to it.
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
    # two generic dates: only one last date per row - the one that says "last"
    ({"Phone": PHONES, "Date": OLD, "Last Updated": DATES}, {"transactions_last": ["Last Updated"]}),
])
def test_other_files_date_goes_to_transactions_last(real_schema, columns, expected):
    """Outside call logs a generic date is the latest visit/transaction; only one date is used per row."""
    m = mapped(real_schema, pd.DataFrame(columns))
    m.pop("phone_number")
    assert m == expected


CREATION_HEADERS = ["Date Created", "Created At", "createdAt", "Sign Up Date", "Member Since", "First Visit",
                    "Joined"]


@pytest.mark.parametrize("header", CREATION_HEADERS)
def test_creation_date_is_the_fallback_when_there_is_no_other_date(real_schema, header):
    """A file whose only date is a creation date imports it rather than no date at all."""
    m = mapped(real_schema, pd.DataFrame({"Phone": PHONES, header: OLD}))
    assert m["transactions_last"] == [header]


@pytest.mark.parametrize("header", CREATION_HEADERS)
@pytest.mark.parametrize("other", ["Last Visit", "Date", "Last Reward Redeemed"])
def test_creation_date_never_alongside_a_last_date(real_schema, header, other):
    """With a real last date (named or generic) the creation date is not imported: one last date per row."""
    m = mapped(real_schema, pd.DataFrame({"Phone": PHONES, header: OLD, other: DATES}))
    assert header not in sum(m.values(), []) and other in sum(m.values(), [])


def test_call_log_with_only_a_creation_date(real_schema):
    """A call log whose only date is 'Date Created' still gets it, as the last call."""
    m = mapped(real_schema, pd.DataFrame({"Date Created": DATES, "From": PHONES, "To": ["5163000366"] * 3}))
    assert m["calls_last"] == ["Date Created"]


@pytest.mark.parametrize("header", ["Expiration Date", "Opt-In Date", "Due Date"])
def test_dates_that_are_never_a_last_activity(real_schema, header):
    """Expiry, due and opt-in dates are never used, not even as the only date."""
    m = mapped(real_schema, pd.DataFrame({"Phone": PHONES, header: DATES}))
    assert set(m) == {"phone_number"}


def test_end_time_is_not_used_as_a_second_activity(real_schema):
    """'Start Time' is the last call; the call's 'End Time' doesn't also become the last transaction."""
    df = pd.DataFrame({"Start Time": DATES, "End Time": DATES, "Caller ID": PHONES})
    m = mapped(real_schema, df)
    assert m["calls_last"] == ["Start Time"] and "transactions_last" not in m


def test_named_activity_columns_are_unchanged(real_schema):
    """Columns that name their activity keep their own field (both, when a file names both); no generic date is
    added on top."""
    df = pd.DataFrame({"Phone": PHONES, "Last Visit": DATES, "Last Call": DATES, "Date": OLD})
    m = mapped(real_schema, df)
    assert m["transactions_last"] == ["Last Visit"] and m["calls_last"] == ["Last Call"]
    assert "Date" not in sum(m.values(), [])


def test_last_contacted_only_by_hand(real_schema):
    """last_contacted (when we last texted the customer) is never filled automatically, only when a person maps it.

    The rules don't map it, a column naming it isn't reused as another date, a mapping a person didn't make is
    dropped, a saved template leaves it out - and a person can still choose it.
    """
    df = pd.DataFrame({"Phone": PHONES, "Last Campaign Sent": DATES, "Last Message Sent": DATES})
    m = auto_map(real_schema, profile_columns(df))
    assert m.get("last_contacted").transform == "none"
    assert not ({"Last Campaign Sent", "Last Message Sent"} & m.used_sources())  # not reused as another date
    auto = Mapping(fields={"phone_number": FieldMapping(sources=["Phone"], transform="copy"),
                           "last_contacted": FieldMapping(sources=["Last Campaign Sent"], transform="copy",
                                                          origin="template")})
    assert set(convert(df, real_schema, auto).output["last_contacted"]) == {""}
    m2, _ = apply_column_edit(real_schema, m, "Last Campaign Sent", targets=["last_contacted"])
    assert m2.get("last_contacted").origin == "user"
    assert convert(df, real_schema, m2).output["last_contacted"].iloc[0] == "10/01/2026 03:15 PM"
    assert m2.without_manual(real_schema).get("last_contacted").transform == "none"  # what a template keeps


@pytest.mark.parametrize("value,expected", [
    ("01/01/1900", "01/01/1900 12:00 AM"),
    ("01/01/1970 00:00:00", "01/01/1970 12:00 AM"),  # a common "empty" timestamp - still imported as written
    ("01/01/2099", "01/01/2099 12:00 AM"),           # future
])
def test_activity_dates_are_imported_as_written(real_schema, value, expected):
    """Only birthday and anniversary use a placeholder year (1600). Activity dates are imported as they are, even
    when they look like a placeholder or lie in the future (decided: no blanking, no substitute date)."""
    df = pd.DataFrame({"Phone": PHONES[:1], "Last Visit": [value]})
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Phone"], transform="copy"),
                        "transactions_last": FieldMapping(sources=["Last Visit"], transform="copy")})
    res = convert(df, real_schema, m)
    assert res.output["transactions_last"].tolist() == [expected] and res.blanked_values == 0


# ---------------------------------------------------------------- 0.18.0

@pytest.mark.parametrize("other", [{"Do Not Call": ["N", "Y", "N"]}, {"Call Opt In": ["Y", "Y", "N"]},
                                   {"DNC": ["N", "N", "Y"]}, {"Membership Duration": ["3 years", "1 year", "2 years"]},
                                   {"Phone Extension": ["12", "", "3"]}])
def test_columns_that_dont_make_a_call_log(real_schema, other):
    """A consent column about calls, a membership duration or a phone extension isn't a phone system's export."""
    m = mapped(real_schema, pd.DataFrame({"Phone": PHONES, "Date": DATES, **other}))
    assert m.get("transactions_last") == ["Date"] and "calls_last" not in m


@pytest.mark.parametrize("header", ["Date Added", "Import Date", "Imported On", "Date Uploaded"])
def test_added_and_import_dates_are_creation_dates(real_schema, header):
    """Used only when the file has no other date, never instead of one."""
    assert mapped(real_schema, pd.DataFrame({"Phone": PHONES, header: OLD}))["transactions_last"] == [header]
    assert mapped(real_schema, pd.DataFrame({"Phone": PHONES, header: OLD, "Date": DATES}))["transactions_last"] == [
        "Date"]


@pytest.mark.parametrize("values,expected", [
    (["46304.65625", "46305.5", "46306"], "10/09/2026 03:45 PM"),       # Excel serials
    (["1791575100", "1791661500", "1791747900"], "10/09/2026 03:45 PM"),  # Unix seconds
    (["1791575100000", "1791661500000", "1791747900000"], "10/09/2026 03:45 PM"),  # Unix milliseconds
    (["20261009", "20261010", "20261011"], "10/09/2026 12:00 AM"),       # compact
])
def test_named_date_column_in_a_machine_format_is_mapped(real_schema, values, expected):
    """A "Last Visit" column of serial numbers or timestamps scored too low to be mapped before 0.18.0."""
    df = pd.DataFrame({"Phone": PHONES, "Last Visit": values})
    m = auto_map(real_schema, profile_columns(df))
    assert m.get("transactions_last").sources == ["Last Visit"] and m.get("transactions_last").confidence == "high"
    assert convert(df, real_schema, m, timezone="America/New_York").output["transactions_last"].iloc[0] == expected


@pytest.mark.parametrize("values,expected", [(["31302", "33000", "30000"], "09/12/1985"),
                                             (["09/12/1600", "03/04/1600", "05/06/1600"], "09/12/1600"),
                                             (["19850912", "19900304", "19700506"], "09/12/1985")])
def test_named_birthday_column_in_an_unusual_format_is_mapped(real_schema, values, expected):
    df = pd.DataFrame({"Phone": PHONES, "Birthday": values})
    m = auto_map(real_schema, profile_columns(df))
    assert m.get("birthday").sources == ["Birthday"]
    assert convert(df, real_schema, m).output["birthday"].iloc[0] == expected


def test_counts_are_not_dates(real_schema):
    """A "Visits" count column next to the phone is still not a date column."""
    df = pd.DataFrame({"Phone": PHONES, "Visits": ["3", "12", "1"], "Last Visits": ["3", "12", "1"]})
    assert "transactions_last" not in mapped(real_schema, df)


@pytest.mark.parametrize("date_col,time_col,field", [
    ("Last Visit Date", "Last Visit Time", "transactions_last"), ("Visit Date", "Visit Time", "transactions_last"),
    ("lastOrderDate", "lastOrderTime", "transactions_last"), ("Date of Last Visit", "Time of Last Visit",
                                                             "transactions_last"),
    ("Last Call Date", "Last Call Time", "calls_last"),
])
def test_named_date_and_time_columns_are_combined(real_schema, date_col, time_col, field):
    """The time used to be dropped (every row 12:00 AM)."""
    df = pd.DataFrame({"Phone": PHONES, date_col: ["10/01/2026", "10/02/2026", ""],
                       time_col: ["3:15 PM", "13:00", "9:30 AM"]})
    m = auto_map(real_schema, profile_columns(df))
    assert m.get(field).sources == [date_col, time_col] and m.get(field).transform == "concat"
    assert convert(df, real_schema, m).output[field].tolist()[:2] == ["10/01/2026 03:15 PM", "10/02/2026 01:00 PM"]


def test_date_column_that_has_times_is_not_combined(real_schema):
    """A date column that already holds the time keeps it; the time column isn't added (it would be read twice)."""
    df = pd.DataFrame({"Phone": PHONES, "Last Visit Date": DATES, "Last Visit Time": ["3:15 PM", "1:00 PM", "9:30 AM"]})
    assert mapped(real_schema, df)["transactions_last"] == ["Last Visit Date"]
