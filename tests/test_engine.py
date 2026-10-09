import pandas as pd

from converter.engine import convert
from converter.heuristics import auto_map
from converter.mapping import FieldMapping, Mapping, profile_columns
from tests.conftest import read_sample


def run(samples, schema, name, **kw):
    rr = read_sample(samples, name, **kw)
    m = auto_map(schema, profile_columns(rr.df))
    return rr, m, convert(rr.df, schema, m, [rr.excel_row_number(i) for i in range(len(rr.df))])


def test_pos_export_end_to_end(samples, schema):
    rr, m, res = run(samples, schema, "pos_guest_export.csv")
    assert res.total_rows == 9 and res.blank_rows == 1
    assert res.rejected == 2  # 7-digit phone, missing phone
    assert res.duplicates == 1  # same phone + same signup date
    assert res.imported == 6
    out = res.output
    # Same phone, different signup date is NOT a duplicate: both rows import.
    jose = out[out["Phone"] == "+15165550100"]
    assert sorted(jose["Signup Date"]) == ["10/01/2026", "10/05/2026"]
    smith = out[out["Last Name"] == "Smith"].iloc[0]
    assert smith["First Name"] == "John" and smith["Email"] == ""  # invalid optional email blanked
    patel = out[out["Last Name"] == "Patel"].iloc[0]
    assert patel["Address 2"] == "Suite 200" and patel["Zip"] == "11753" and patel["State"] == "NY"
    assert set(res.rejects["Status"]) == {"Rejected", "Duplicate"}
    assert "File Row" in res.rejects.columns
    assert "bad row" in res.flag_message()


def test_loyalty_xlsx(samples, schema):
    rr, m, res = run(samples, schema, "loyalty_members.xlsx", sheet="Members")
    out = res.output
    assert res.duplicates == 1 and res.rejected == 0
    assert out.iloc[1]["Zip"] == "07030" and out.iloc[1]["State"] == "NJ"
    assert out.iloc[0]["First Name"] == "John" and out.iloc[0]["Last Name"] == "McDonald"
    tara = out[out["First Name"] == "Tara"].iloc[0]
    assert tara["Signup Date"] == "09/05/2026"  # Excel serial number in a date column
    bad = out[out["Last Name"] == "Zip"].iloc[0]
    assert bad["Zip"] == "" and res.blanked_values == 1


def test_day_first_column(samples, schema):
    _, _, res = run(samples, schema, "eu_style_semicolon.csv")
    assert list(res.output["Signup Date"]) == ["03/15/2026", "04/01/2026", "02/28/2026"]


def test_dedupe_requires_all_keys(schema):
    df = pd.DataFrame({
        "Phone": ["5165550100", "516-555-0100", "5165550100", "5165550100"],
        "Date": ["10/01/2026", "10/01/2026", "10/02/2026", ""],
    })
    m = Mapping(fields={"phone": FieldMapping(sources=["Phone"], transform="copy"),
                        "signup_date": FieldMapping(sources=["Date"], transform="copy")})
    res = convert(df, schema, m)
    assert res.imported == 3 and res.duplicates == 1


def test_overrides_skip_normalization(schema):
    df = pd.DataFrame({"Phone": ["516 5OO 0100", "5165550131"]})
    m = Mapping(fields={"phone": FieldMapping(sources=["Phone"], transform="copy")})
    assert convert(df, schema, m).rejected == 1
    res = convert(df, schema, m, overrides={"phone": {"516 5OO 0100": "+15165550100"}})
    assert res.rejected == 0 and res.output["Phone"].tolist() == ["+15165550100", "+15165550131"]


def test_required_missing_and_defaults(schema):
    df = pd.DataFrame({"Phone": ["", "5165550131"], "Name": ["A", "B"]})
    m = Mapping(fields={"phone": FieldMapping(sources=["Phone"], transform="copy"),
                        "notes": FieldMapping(transform="constant", params={"value": "Imported"})})
    res = convert(df, schema, m)
    assert res.rejected == 1 and res.reason_counts["Phone: required value is missing"] == 1
    assert res.output["Notes"].tolist() == ["Imported"]
