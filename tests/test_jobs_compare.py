"""Background import jobs (run_import, ImportRunner), file retention, and the before/after comparison view.

Imports run in the background of a shared web server. A job that fails, is cancelled or is cut off by a restart
must say so, or someone assumes a customer list went in when it didn't. Old customer files are purged while the
history record stays. The comparison is what a person checks before uploading, so it must line every converted row
up with its file row (including extra rows for second numbers), and it must escape whatever text the file contains.
"""

import io
import time

import pandas as pd

from converter.compare import (FILTERS, build_compare_html, column_plan, comparison_xlsx, field_change_summary,
                               filter_rows, status_counts)
from converter.engine import source_rows
from converter.heuristics import auto_map
from converter.jobs import ImportRunner, run_import
from converter.mapping import profile_columns
from converter.storage import Store
from tests.conftest import read_sample


def make_import(store, samples, schema, name="pos_guest_export.csv", **kw):
    """Queue an import of a sample file the way the upload screen does, and return its id.

    The raw bytes, schema, auto-mapping and the sheet/header row the reader picked are stored with the record,
    so the background job can re-read the file exactly as the user saw it.
    """
    rr = read_sample(samples, name, **kw)
    m = auto_map(schema, profile_columns(rr.df))
    data = (samples / name).read_bytes()
    return store.create_import(username="noah", filename=name, data=data, schema_label=schema.label,
                               schema_json=schema.model_dump_json(), mapping=m,
                               read_options={"sheet": rr.sheet, "header_row": rr.header_row})


def test_run_import_writes_everything(tmp_path, samples, schema):
    """A completed job records its status, progress, counts and timings and stores every file the app offers.

    The summary keeps the failed values so the history page can show what went wrong without re-running.
    """
    st = Store(tmp_path / "c.db")
    iid = make_import(st, samples, schema)
    rec = run_import(st, iid)
    assert rec.status == "completed" and rec.progress == 1.0 and rec.stage == "Done"
    assert (rec.total_rows, rec.imported, rec.rejected, rec.duplicates, rec.blank_rows) == (9, 7, 2, 1, 1)
    assert rec.started_at and rec.finished_at and rec.elapsed().total_seconds() >= 0
    for kind in ("output", "rejects", "summary", "audit", "source_table"):
        assert st.file_path(rec, kind) is not None, kind
    assert "bad row" in rec.summary["message"]
    # "555-1234" is the POS sample's 7-digit phone with no area code.
    assert rec.summary["failed_values"]["phone"] == ["555-1234"]


def test_xlsx_import_with_sheet(tmp_path, samples, schema):
    """The chosen Excel sheet is stored with the import and used by the job, not the workbook's first sheet."""
    st = Store(tmp_path / "c.db")
    iid = make_import(st, samples, schema, "loyalty_members.xlsx", sheet="Members")
    rec = run_import(st, iid)
    assert rec.status == "completed" and rec.imported == 6


def test_failure_and_cancel_and_interrupt(tmp_path, samples, schema):
    """Three ways a job ends without completing, each recorded so nobody assumes the list was imported.

    An unreadable stored file fails with an error, a cancel requested before the job runs cancels it, and a job
    still queued when the server restarts is marked interrupted when the new runner starts.
    """
    st = Store(tmp_path / "c.db")
    iid = make_import(st, samples, schema)
    st.source_path(st.get_import(iid)).write_bytes(b"")  # corrupt the stored file
    rec = run_import(st, iid)
    assert rec.status == "failed" and "empty" in rec.error.lower()

    iid2 = make_import(st, samples, schema)
    st.request_cancel(iid2)
    assert run_import(st, iid2).status == "cancelled"

    iid3 = make_import(st, samples, schema)  # queued when the "server restarts"
    runner = ImportRunner(st, workers=1)
    assert st.get_import(iid3).status == "interrupted"
    runner.pool.shutdown()


def test_background_runner_progress(tmp_path, samples, schema):
    """Several imports submitted to the thread pool all complete.

    The loop polls with a 20-second deadline, so a stuck worker fails the test instead of hanging the suite.
    """
    st = Store(tmp_path / "c.db")
    runner = ImportRunner(st, workers=2)
    ids = [make_import(st, samples, schema) for _ in range(3)]
    for i in ids:
        runner.submit(i)
    deadline = time.time() + 20
    while time.time() < deadline and st.active_imports():
        time.sleep(0.1)
    assert all(st.get_import(i).status == "completed" for i in ids)
    runner.pool.shutdown()


def test_purge_keeps_record(tmp_path, samples, schema):
    """Retention: files of old finished imports are deleted (customer data isn't kept forever), the record stays."""
    st = Store(tmp_path / "c.db")
    iid = make_import(st, samples, schema)
    run_import(st, iid)
    # Backdate the finish time so the import is outside the 30-day retention window.
    with st._conn() as con:
        con.execute("UPDATE imports SET finished_at='2000-01-01T00:00:00+00:00' WHERE id=?", (iid,))
    assert st.purge_files(30) == 1
    rec = st.get_import(iid)
    assert rec.files_purged and rec.imported == 7 and st.file_path(rec, "output") is None
    assert not (st.files_root / str(iid)).exists()


def test_comparison(tmp_path, samples, schema):
    """The comparison data and views built from a finished import agree with each other and with the file.

    Covers source rows lined up with converted rows, status counts and filters (including search by name and by
    file row number), the HTML table (escaped, script-free, extra rows and phone origin marked), the per-field
    change summary and the Excel export.
    """
    st = Store(tmp_path / "c.db")
    rec = run_import(st, make_import(st, samples, schema))
    folder = st.files_root / str(rec.id)
    raw_src, audit = pd.read_parquet(folder / "source.parquet"), pd.read_parquet(folder / "audit.parquet")
    # Smith's mobile and home numbers became two rows: 10 file rows → 11 converted rows.
    assert len(raw_src) == 10 and len(audit) == 11 and audit["_extra"].sum() == 1
    src = source_rows(raw_src, audit)
    assert len(src) == 11 and source_rows(src, audit) is src
    assert src.iloc[1].equals(src.iloc[2]) and audit.iloc[2]["_row"] == audit.iloc[1]["_row"]
    plan = column_plan(schema, rec.mapping, list(src.columns))
    assert plan.feeds["Guest Name"] == ["First Name", "Last Name"] and plan.source_columns[0] == "Mobile #"

    counts = status_counts(audit, schema)
    assert counts["rejected"] == 2 and counts["duplicate"] == 1 and counts["empty"] == 1
    assert set(counts) == set(FILTERS)
    # filter_rows returns positions in the audit table; "#14" searches by file row number (row 14 in Excel).
    assert filter_rows(audit, src, schema, "rejected") == [7, 10]
    assert filter_rows(audit, src, schema, "all", "patel") == [8]
    assert filter_rows(audit, src, schema, "all", "#14") == [10]

    h = build_compare_html(src, audit, list(range(len(audit))), schema, plan)
    assert "Bad row" in h and "missing area code" in h and "c-fmt" in h and "→ First Name, Last Name" in h
    assert "<script" not in h and "6 +" in h  # the extra row is marked
    assert h.count('class="o each"') == 9 and "this row&#x27;s number" in h  # which number each row came from
    # values are escaped
    src.iloc[0, 0] = "<b>x</b>"
    assert "&lt;b&gt;x&lt;/b&gt;" in build_compare_html(src, audit, [0], schema, plan)

    summary = field_change_summary(audit, schema, rec.mapping)
    phone = summary[summary["Field"] == "Phone"].iloc[0]
    assert phone["Reformatted"] == 8 and phone["Rejected rows"] == 2

    x = comparison_xlsx(src, audit, schema, plan, "test")
    back = pd.read_excel(io.BytesIO(x), header=None, dtype=str)
    assert back.iloc[3, 0] == "File row" and back.iloc[4, 1] == "Imported"
