"""No downloaded file may carry a live spreadsheet formula from a client's file ("CSV injection").

The rejected-rows CSV repeats the client's original values and is opened by staff in Excel. A cell such as
=HYPERLINK("http://x","click") or -2+3+cmd|' /C calc'!A0 would run as a formula there. Such cells get a leading
apostrophe (text); everything else in the file is unchanged. The side-by-side workbook and an Excel output file
write every value as text.
"""

import io

import openpyxl
import pandas as pd
import pytest

from converter.engine import convert
from converter.mapping import FieldMapping, Mapping
from converter.output import neutralise_csv, neutralise_formula, write_output, write_rejects

LINK = '=HYPERLINK("http://x","click")'


@pytest.mark.parametrize("value,expected", [
    (LINK, "'" + LINK),
    ("@SUM(A1:A9)", "'@SUM(A1:A9)"),
    ("-2+3+cmd|' /C calc'!A0", "'-2+3+cmd|' /C calc'!A0"),
    ("+1 (516) 555-0100", "'+1 (516) 555-0100"),
    (" =1+1", "' =1+1"),        # a space in front doesn't stop every spreadsheet
    ("\t=1+1", "'\t=1+1"),
    ("'=1+1", "'=1+1"),          # already safe: not changed twice
])
def test_would_be_formulas_get_an_apostrophe(value, expected):
    assert neutralise_formula(value) == expected


@pytest.mark.parametrize("value", ["5165550100", "516-555-0100", "(516) 555-0100", "-12", "+1", "3.5", "-", "--",
                                   "Joe Smith", "10/01/2026", "", "a=b"])
def test_ordinary_values_are_unchanged(value):
    """Numbers, lone dashes, phones without '+' and text are written exactly as in the file."""
    assert neutralise_formula(value) == value


def test_rejects_file_is_safe(real_schema):
    """Formulas in a rejected row's values and in a column name are neutralised; the converted file is unchanged."""
    df = pd.DataFrame({"Phone": [LINK, "5165550100"], "=Notes": ["@SUM(1)", "+1 (516) 555-0100"]})
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Phone"], transform="copy")})
    res = convert(df, real_schema, m, row_numbers=[2, 3])
    rows = pd.read_csv(io.BytesIO(write_rejects(res)), dtype=str, keep_default_na=False, encoding="utf-8-sig")
    assert list(rows.columns) == ["File Row", "Status", "Reason", "Phone", "'=Notes"]
    assert rows.iloc[0]["Phone"] == "'" + LINK and rows.iloc[0]["'=Notes"] == "'@SUM(1)"
    assert rows.iloc[0]["File Row"] == "2"
    assert res.output["phone_number"].tolist() == ["5165550100"]


def test_older_rejects_files_are_fixed_on_download():
    """A rejects file written before the fix is neutralised when downloaded; a safe file comes back byte for byte."""
    old = ('﻿File Row,Status,Reason,Phone\n2,Rejected,invalid phone,"=HYPERLINK(""http://x"",""c"")"\n'
           '3,Rejected,invalid phone,516555\n').encode("utf-8")
    fixed = neutralise_csv(old).decode("utf-8-sig")
    assert fixed.splitlines()[1] == '2,Rejected,invalid phone,"\'=HYPERLINK(""http://x"",""c"")"'
    assert fixed.splitlines()[2] == "3,Rejected,invalid phone,516555"
    assert neutralise_csv(neutralise_csv(old)) == neutralise_csv(old)
    safe = "﻿File Row,Status\n2,Rejected\n".encode("utf-8")
    assert neutralise_csv(safe) is safe


def test_excel_output_writes_text_not_formulas(schema, tmp_path):
    """An Excel output file stores a value starting with '=' as text, exactly as converted."""
    xlsx_schema = schema.model_copy(deep=True)
    xlsx_schema.output.format = "xlsx"
    p = tmp_path / "o.xlsx"
    p.write_bytes(write_output(pd.DataFrame({"Phone": ["+15165550100"], "Note": ["=1+1"]}), xlsx_schema))
    cell = openpyxl.load_workbook(p).active["B2"]
    assert cell.data_type == "s" and cell.value == "=1+1"
