"""Excel error values (#VALUE!, #REF!, #DIV/0!…) are reported as what they are: a broken formula in the client's file.

Such a cell holds no data. It used to fail with the field type's own, misleading reason ("too few digits (1)" for
#DIV/0! as a phone, "unrecognized word 'value'" as a date). The outcome is unchanged - a phone row is still a bad
row, an optional value is still left blank - only the reason, the mistake check and "Suggest fixes" differ.
"""

import pandas as pd
import pytest

from converter.checks import run_checks
from converter.engine import convert
from converter.mapping import FieldMapping, Mapping, profile_columns
from converter.normalizers import SHEET_ERROR_REASON, normalize, sheet_error

CODES = ["#VALUE!", "#REF!", "#DIV/0!", "#NAME?", "#NUM!", "#NULL!", "#SPILL!", "#CALC!", "#ERROR!", "#GETTING_DATA",
         "Err:502", "#value!", " #REF! "]


@pytest.mark.parametrize("code", CODES)
@pytest.mark.parametrize("field", ["phone_number", "transactions_last", "birthday", "consent"])
def test_every_field_type_names_the_excel_error(real_schema, code, field):
    assert normalize(code, real_schema.field(field), {}, {}) == ("", SHEET_ERROR_REASON)


@pytest.mark.parametrize("value", ["#N/A", "#n/a"])
def test_na_is_still_empty(real_schema, value):
    """#N/A keeps meaning "no value" (also typed by people), so a phone row says the number is missing."""
    assert normalize(value, real_schema.field("phone_number"), {}, {}) == ("", None)


@pytest.mark.parametrize("value", ["#5 Main St", "#1 customer", "Value!", "#REF", "Err:50", "#VALUE! 5165550100"])
def test_other_values_are_not_excel_errors(value):
    assert sheet_error(value) is None


def test_import_reasons_and_summary(real_schema):
    """The rejects report shows the reason and the code; all codes share one line in the summary."""
    df = pd.DataFrame({"Phone": ["#DIV/0!", "#REF!", "5165550100"], "Visit": ["10/01/2026", "", "#VALUE!"]})
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Phone"], transform="copy"),
                        "transactions_last": FieldMapping(sources=["Visit"], transform="copy")})
    res = convert(df, real_schema, m, row_numbers=[2, 3, 4])
    assert res.output["phone_number"].tolist() == ["5165550100"] and res.output["transactions_last"].tolist() == [""]
    assert res.rejects["Reason"].tolist() == [
        "phone_number: Excel error (broken formula in the client's file) ('#DIV/0!')",
        "phone_number: Excel error (broken formula in the client's file) ('#REF!')"]
    assert res.reason_counts == {"phone_number: Excel error (broken formula in the client's file)": 2}
    assert res.blanked_counts == {"transactions_last: Excel error (broken formula in the client's file)": 1}


def test_mistake_check_points_at_the_export_not_the_column(real_schema):
    """A column of Excel errors gets its own warning, not "this is probably the wrong column"."""
    dates = ["10/01/2026", "10/02/2026"] + ["#REF!"] * 8
    df = pd.DataFrame({"Phone": [f"516555{1000 + i}" for i in range(10)], "Visit": dates})
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Phone"], transform="copy"),
                        "transactions_last": FieldMapping(sources=["Visit"], transform="copy")})
    res = convert(df, real_schema, m)
    issues = [f.issue for f in run_checks(real_schema, m, df, res.audit, profile_columns(df))
              if f.field == "transactions_last"]
    assert any(i.startswith("8 value(s) from 'Visit' are Excel errors (e.g. '#REF!')") for i in issues)
    assert not any("can't be read" in i for i in issues)
