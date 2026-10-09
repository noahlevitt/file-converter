"""Files with several phone columns (Toast: phone, phone2, phone3, phone4) and phone-setting columns."""

import pandas as pd

from converter.engine import convert
from converter.heuristics import auto_map, is_consent_column, is_phone_header
from converter.mapping import FieldMapping, Mapping, profile_columns
from converter.output import write_summary


def toast_like():
    return pd.DataFrame({
        "firstName": ["Ana", "Ben", "Cy", "Di", "Ed"],
        "phone": ["201-555-0111", "2015550112", "", "", "201-555-0150"],
        "phone2": ["(201) 555-0111", "2015550198", "", "555-1234", ""],
        "phone3": ["2015550177", "123", "", "", ""],
        "phone4": ["", "", "", "", ""],
        "phoneMarketingPreference": ["true", "false", "", "true", ""],
        "phone4MarketingPreference": ["", "", "", "", ""],
        "lastVisitDate": ["10/01/2026", "10/02/2026", "10/03/2026", "10/04/2026", "10/05/2026"],
    })


def test_phone_setting_columns_are_not_phone_numbers(real_schema):
    m = auto_map(real_schema, profile_columns(toast_like()))
    phone = m.get("phone_number")
    assert phone.transform == "each_row" and phone.sources == ["phone", "phone2", "phone3", "phone4"]
    assert not any("Preference" in c for c in m.used_sources())
    assert is_consent_column("phone4MarketingPreference") and not is_phone_header("phone4MarketingPreference")
    for h in ["Phone Type", "phoneVerified", "Phone Ext", "SMS Opt In"]:
        assert not is_phone_header(h), h


def test_one_row_per_different_number(real_schema):
    df = toast_like()
    m = auto_map(real_schema, profile_columns(df))
    res = convert(df, real_schema, m)
    out = res.output
    # Ana: 201-555-0111 twice (two formats) counts once, plus 201-555-0177 → 2 rows.
    # Ben: 0112 and 0198 → 2 rows; his "123" is skipped. Cy: no number → bad row. Di: only a 7-digit number → bad row.
    # Ed: one number → 1 row.
    assert out["phone_number"].tolist() == ["2015550111", "2015550177", "2015550112", "2015550198", "2015550150"]
    assert out["transactions_last"].tolist() == ["10/01/2026 12:00 AM"] * 2 + ["10/02/2026 12:00 AM"] * 2 + [
        "10/05/2026 12:00 AM"]
    assert (res.total_rows, res.imported, res.rejected, res.extra_rows, res.skipped_values) == (5, 5, 2, 2, 1)
    msg = res.flag_message()
    assert "5 rows ready to import from 5 file rows (2 extra rows" in msg and "1 invalid extra number" in msg
    assert "missing area code" in res.rejects.loc[res.rejects["firstName"] == "Di", "Reason"].iloc[0]
    assert res.rejects.columns[3] == "phone_number"  # which number of the file row it was

    a = res.audit
    assert a["_row"].tolist() == [2, 2, 3, 3, 4, 5, 6] and a["_extra"].tolist() == [False, True, False, True,
                                                                                   False, False, False]
    assert "extra value skipped" in a.iloc[2]["_issues"]
    summary = write_summary(res, real_schema, "toast.csv", "noah", "x").decode()
    assert "extra rows for 2nd/3rd" in summary and "Extra numbers skipped" in summary


def test_same_number_in_two_columns_is_one_row(real_schema):
    df = pd.DataFrame({"Mobile": ["2015550111"], "Home Phone": ["+1 (201) 555-0111"]})
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Mobile", "Home Phone"], transform="each_row")})
    res = convert(df, real_schema, m)
    assert res.imported == 1 and res.extra_rows == 0 and res.duplicates == 0


def test_first_non_empty_still_available(real_schema):
    df = toast_like()
    m = Mapping(fields={"phone_number": FieldMapping(sources=["phone", "phone2", "phone3"], transform="coalesce")})
    res = convert(df, real_schema, m)
    # One row per file row, first number found; Cy (none) and Di (7 digits) are bad rows.
    assert (res.imported, res.rejected, res.extra_rows) == (3, 2, 0)
    assert res.output["phone_number"].tolist() == ["2015550111", "2015550112", "2015550150"]


def test_only_one_field_can_make_extra_rows(real_schema):
    m = Mapping(fields={"phone_number": FieldMapping(sources=["a", "b"], transform="each_row"),
                        "birthday": FieldMapping(sources=["c", "d"], transform="each_row")})
    assert "only one field" in m.problems(real_schema)["birthday"]
