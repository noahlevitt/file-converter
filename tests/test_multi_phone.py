"""Files with several phone columns (Toast: phone, phone2, phone3, phone4) and phone-setting columns.

Every phone number is a separate text recipient. Each different number must become its own row (or the second
number never gets the restaurant's texts), the same number written twice must not be imported twice, and
"phone…MarketingPreference" style columns next to the numbers must never be read as numbers or mapped to any
field (they count as consent columns, which are never imported).
"""

import pandas as pd

from converter.editor import apply_row_edit
from converter.engine import convert
from converter.heuristics import auto_map, is_consent_column, is_phone_header
from converter.mapping import FieldMapping, Mapping, profile_columns
from converter.output import write_summary


def toast_like():
    """A Toast-style guest export with four phone columns and two phone marketing-preference columns.

    Each guest is a different case: Ana has one number in two formats plus another, Ben has two numbers and a junk
    '123', Cy has no number, Di only a broken one ('555-12'), and Ed has one.
    """
    return pd.DataFrame({
        "firstName": ["Ana", "Ben", "Cy", "Di", "Ed"],
        "phone": ["201-555-0111", "2015550112", "", "", "201-555-0150"],
        "phone2": ["(201) 555-0111", "2015550198", "", "555-12", ""],
        "phone3": ["2015550177", "123", "", "", ""],
        "phone4": ["", "", "", "", ""],
        # Header contains "phone" but the values are true/false opt-in flags.
        "phoneMarketingPreference": ["true", "false", "", "true", ""],
        "phone4MarketingPreference": ["", "", "", "", ""],
        "lastVisitDate": ["10/01/2026", "10/02/2026", "10/03/2026", "10/04/2026", "10/05/2026"],
    })


def test_phone_setting_columns_are_not_phone_numbers(real_schema):
    """All four phone columns are mapped as one row per number; preference and other phone-setting columns are not.

    Preference columns are recognised as consent columns (which are never imported), and headers such as phone
    type, verified flag, extension and SMS opt-in are not taken for phone numbers.
    """
    m = auto_map(real_schema, profile_columns(toast_like()))
    phone = m.get("phone_number")
    assert phone.transform == "each_row" and phone.sources == ["phone", "phone2", "phone3", "phone4"]
    assert not any("Preference" in c for c in m.used_sources())
    assert is_consent_column("phone4MarketingPreference") and not is_phone_header("phone4MarketingPreference")
    for h in ["Phone Type", "phoneVerified", "Phone Ext", "SMS Opt In"]:
        assert not is_phone_header(h), h


def test_one_row_per_different_number(real_schema):
    """Each different number becomes its own row with the guest's other data copied.

    The same number in two formats counts once, an invalid extra number is skipped and counted, and a guest with no
    valid number is a bad row. Output, counts, flag message, rejects, audit and summary must all agree.
    """
    df = toast_like()
    m = auto_map(real_schema, profile_columns(df))
    res = convert(df, real_schema, m)
    out = res.output
    # Ana: 201-555-0111 twice (two formats) counts once, plus 201-555-0177 → 2 rows.
    # Ben: 0112 and 0198 → 2 rows; his "123" is skipped. Cy: no number → bad row. Di: only a broken number → bad row.
    # Ed: one number → 1 row.
    assert out["phone_number"].tolist() == ["2015550111", "2015550177", "2015550112", "2015550198", "2015550150"]
    assert out["transactions_last"].tolist() == ["10/01/2026 12:00 AM"] * 2 + ["10/02/2026 12:00 AM"] * 2 + [
        "10/05/2026 12:00 AM"]
    assert (res.total_rows, res.imported, res.rejected, res.extra_rows, res.skipped_values) == (5, 5, 2, 2, 1)
    msg = res.flag_message()
    assert "5 rows ready to import from 5 file rows (2 extra rows" in msg and "1 extra phone number skipped" in msg
    assert "too few digits" in res.rejects.loc[res.rejects["firstName"] == "Di", "Reason"].iloc[0]
    assert res.rejects.columns[3] == "phone_number"  # which number of the file row it was

    # _row is the file row number (the header is row 1); _extra marks the rows added for a guest's 2nd number.
    a = res.audit
    assert a["_row"].tolist() == [2, 2, 3, 3, 4, 5, 6] and a["_extra"].tolist() == [False, True, False, True,
                                                                                   False, False, False]
    assert "extra value skipped" in a.iloc[2]["_issues"]
    summary = write_summary(res, real_schema, "toast.csv", "noah", "x").decode()
    assert "extra rows for 2nd/3rd" in summary and "Extra numbers skipped" in summary


def test_same_number_in_two_columns_is_one_row(real_schema):
    """The same number in two columns (written differently) gives one row: no extra row and no duplicate."""
    df = pd.DataFrame({"Mobile": ["2015550111"], "Home Phone": ["+1 (201) 555-0111"]})
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Mobile", "Home Phone"], transform="each_row")})
    res = convert(df, real_schema, m)
    assert res.imported == 1 and res.extra_rows == 0 and res.duplicates == 0


def test_phones_always_one_row_per_number_even_from_old_templates(real_schema):
    """An old template using 'First non-empty' for the phone is upgraded, so it can't drop customers' 2nd numbers."""
    df = toast_like()
    # A template saved before this rule used 'First non-empty' on the phone field: it is upgraded.
    m = Mapping(fields={"phone_number": FieldMapping(sources=["phone", "phone2", "phone3"], transform="coalesce")})
    assert m.normalized(real_schema, list(df.columns)).get("phone_number").transform == "each_row"
    res = convert(df, real_schema, m)
    assert (res.imported, res.rejected, res.extra_rows) == (5, 2, 2)


def test_first_non_empty_cannot_be_picked_for_phones(real_schema):
    """The editor refuses 'First non-empty' for the phone and 'One row per phone number' for any other field.

    Each refusal explains why, and the other field falls back to first non-empty.
    """
    m = Mapping(fields={"phone_number": FieldMapping(sources=["phone", "phone2"], transform="each_row")})
    m2, err = apply_row_edit(real_schema, m, "phone_number", {"How": "First non-empty"}, "How", "Source column(s)",
                             "Option")
    assert m2.get("phone_number").transform == "each_row" and "one row per number" in err
    m3, err = apply_row_edit(real_schema, m, "birthday", {"How": "One row per phone number",
                                                          "Source column(s)": ["a", "b"]},
                             "How", "Source column(s)", "Option")
    assert m3.get("birthday").transform == "coalesce" and "only for the phone field" in err
    # one row per phone number on another field is turned into first non-empty
    other = Mapping(fields={"birthday": FieldMapping(sources=["a", "b"], transform="each_row")})
    assert other.normalized(real_schema, ["a", "b"]).get("birthday").transform == "coalesce"


def test_texting_activity_columns_are_not_phone_columns(real_schema):
    """Empty "Last SMS Received" / "SMS Last" / "Last Text Sent" columns are dates, not extra phone columns (0.17.0).

    A phone word made them phone headers, and an empty phone header is kept for templates ("Phone 4"), so a file
    in the import format itself (in_sms_last empty) had its in_sms_last column fed to the phone field.
    """
    for h in ["Last SMS Received", "SMS Last", "Last Text", "Last Text Sent", "in_sms_last", "Recent SMS",
              "First Text Date", "Text Received Timestamp"]:
        assert not is_phone_header(h), h
        df = pd.DataFrame({"Phone": ["5165550100", "9175551234"], h: ["", ""]})
        assert auto_map(real_schema, profile_columns(df)).get("phone_number").sources == ["Phone"], h
    for h in ["Mobile", "Cell Phone", "Phone 4", "Text Number", "SMS Phone"]:
        assert is_phone_header(h), h
    # The import format's own file, read back in: only phone_number feeds the phone field.
    df = pd.DataFrame({f.header: ["", ""] for f in real_schema.fields})
    df["phone_number"] = ["5165550100", "9175551234"]
    assert auto_map(real_schema, profile_columns(df)).get("phone_number").sources == ["phone_number"]
