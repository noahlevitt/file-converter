"""One odd cell costs only that cell, never the whole file; very long cells are quick (0.15.3).

Before: a garbage date such as "10/09/2026 12:1111…" stopped the mapping page and failed the import, "+1 " repeated
~1,000 times ran out of recursion depth, headers "Phone, Phone, Phone (2)" crashed the page and "Phone, phone,
Phone (2)" silently dropped a phone column. A Notes column of long text (not even imported) made the mapping page
take 4–23 s. Now each of these is one value with a reason (or no issue at all), with no extra cost for normal files.
"""

import io
import logging
import threading
import time

import pandas as pd
import pytest

from converter import normalizers as N
from converter.engine import convert, preview_values
from converter.mapping import FieldMapping, Mapping, profile_columns
from converter.reader import _unique_headers, read_file
from converter.services import initial_mapping


def upload_and_import(schema, csv_text: str):
    """The app's flow: read, profile, map automatically, preview, convert. Returns (ReadResult, mapping, result)."""
    rr = read_file("x.csv", csv_text.encode())
    m = initial_mapping(schema, rr.df, profile_columns(rr.df), None, None, use_ai=False).mapping
    preview_values(rr.df, schema, m)
    return rr, m, convert(rr.df, schema, m, row_numbers=[rr.excel_row_number(i) for i in range(len(rr.df))])


def test_garbage_date_is_one_bad_value(real_schema):
    """The date library's arithmetic error becomes "not a date" for that cell; every row still imports."""
    rr, m, res = upload_and_import(real_schema, "Phone,Last Visit\n5165550100,10/09/2026 12:" + "1" * 30 +
                                   "\n9175551234,10/01/2026\n")
    assert res.imported == 2 and res.output["transactions_last"].tolist() == ["", "10/01/2026 12:00 AM"]
    assert sum(res.blanked_counts.values()) == 1


def test_many_plus_ones_dont_run_out_of_depth(real_schema):
    """'+1 ' repeated: no recursion error, also in a worker thread (as in the app); the value gets a result."""
    value = "+1 " * 300 + "5165550100"   # 910 characters: under the phone length limit
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Phone"], transform="copy")})
    out = {}
    t = threading.Thread(target=lambda: out.update(r=convert(pd.DataFrame({"Phone": [value]}), real_schema, m)))
    t.start()
    t.join()
    assert out["r"].output["phone_number"].tolist() == ["5165550100"]   # the extra +1s are ignored, and flagged
    assert out["r"].repaired_values == 1


@pytest.mark.parametrize("header,expected", [
    (["Phone", "Phone", "Phone (2)"], ["Phone", "Phone (3)", "Phone (2)"]),
    (["Phone", "phone", "Phone (2)"], ["Phone", "phone (3)", "Phone (2)"]),
    (["Name", "name", " ", "Phone  Number", "Name"], ["Name", "name (2)", "Column 3", "Phone Number", "Name (3)"]),
    (["", "Column 1", "x"], ["Column 1 (2)", "Column 1", "x"]),
    (["", "", ""], ["Column 1", "Column 2", "Column 3"]),
])
def test_column_names_are_unique_ignoring_case(header, expected):
    assert _unique_headers(header) == expected


@pytest.mark.parametrize("header", ["Phone,Phone,Phone (2)", "Phone,phone,Phone (2)"])
def test_colliding_phone_headers_keep_every_column(real_schema, header):
    """All three phone columns are kept and used: one row per different number."""
    rr, m, res = upload_and_import(real_schema, header + "\n5165550100,9175551234,6315550188\n")
    assert len(m.get("phone_number").sources) == 3
    assert sorted(res.output["phone_number"]) == ["5165550100", "6315550188", "9175551234"]


def test_six_phone_columns_unchanged(real_schema):
    """Files with several phone columns: every column used, one row per different number, as before."""
    cols = ["Phone", "Mobile", "Home", "Work", "Phone 2", "Phone 3"]
    rows = [["5165550100", "9175551234", "", "6315550188", "5165550100", "7185550142"],
            ["", "2015550111", "2015550111", "", "", ""]]
    text = "Name," + ",".join(cols) + "\n" + "\n".join(f"n{i}," + ",".join(r) for i, r in enumerate(rows))
    rr, m, res = upload_and_import(real_schema, text)
    assert sorted(m.get("phone_number").sources) == sorted(cols)
    assert res.imported == 4 + 1   # 4 different numbers on the first row, 1 on the second


def test_unexpected_error_costs_only_that_value(real_schema, monkeypatch, caplog):
    """Safety net: a normalizer failing unexpectedly gives that value a reason and is logged; the import goes on."""
    real = N.NORMALIZERS["datetime"]

    def flaky(v, *a):
        if v == "boom":
            raise RuntimeError("simulated bug")
        return real(v, *a)

    monkeypatch.setitem(N.NORMALIZERS, "datetime", flaky)
    m = Mapping(fields={"phone_number": FieldMapping(sources=["Phone"], transform="copy"),
                        "transactions_last": FieldMapping(sources=["Visit"], transform="copy")})
    df = pd.DataFrame({"Phone": ["5165550100", "9175551234"], "Visit": ["boom", "10/01/2026"]})
    with caplog.at_level(logging.ERROR):
        res = convert(df, real_schema, m)
    assert res.imported == 2 and res.output["transactions_last"].tolist() == ["", "10/01/2026 12:00 AM"]
    assert any(N.UNREADABLE_REASON in k for k in res.blanked_counts)
    assert "Unexpected error converting a value" in caplog.text


def test_detectors_and_splitting_never_fail(monkeypatch):
    """Guessing column types and splitting phone cells answer safely even when something inside fails."""
    monkeypatch.setattr(N, "_parse_phone", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    assert N.looks_like_phone("516-555-0100") is False
    monkeypatch.setattr(N, "_split_phones", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    assert N.split_phones("516-555-0100 / 917-555-1234", N._DETECT_PHONE_FIELD, {}) == [
        ("516-555-0100 / 917-555-1234", False)]


def test_too_long_values_are_rejected_at_once(real_schema):
    """A phone over 1,000 characters or a date over 200 is text, not a value: rejected with a plain reason, fast."""
    phone, date = real_schema.field("phone_number"), real_schema.field("transactions_last")
    t = time.time()
    assert N.normalize("(1" * 16000, date, {}, {}) == ("", "too long to be a date (32,000 characters)")
    assert N.normalize("1" * 30000 + "x", phone, {}, {}) == ("", "too long to be a phone number (30,001 characters)")
    assert time.time() - t < 0.5
    # Real values are far below the limits: the longest date format and a cell with 30 labelled numbers.
    assert N.normalize("Saturday, October 10, 2026 at 3:15:22 PM Eastern Daylight Time", date, {},
                       {"tz": "America/New_York"})[1] is None
    cell = ", ".join(f"Cell: (516) 555-{1000 + i}" for i in range(30))
    assert len(cell) < 1000 and len(N.split_phones(cell, phone, {})) == 30


def test_long_notes_column_doesnt_slow_the_upload(real_schema):
    """1,000 rows with 30,000-character notes: the mapping page used to take ~23 s."""
    note = ("customer called about order 12 pizza delivery 5 pm table for 4 paid $45.20 birthday 03/15 " * 400)[:30000]
    buf = io.StringIO()
    buf.write("Phone,Last Visit,Notes\n")
    for i in range(1000):
        buf.write(f'51655{i:05d},10/01/2026,"{note}"\n')
    t = time.time()
    rr, m, res = upload_and_import(real_schema, buf.getvalue())
    assert time.time() - t < 8
    assert m.get("phone_number").sources == ["Phone"] and res.imported == 1000
