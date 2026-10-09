import io
import time

import pandas as pd

from converter.compare import (FILTERS, build_compare_html, column_plan, comparison_xlsx, field_change_summary,
                               filter_rows, status_counts)
from converter.heuristics import auto_map
from converter.jobs import ImportRunner, run_import
from converter.mapping import profile_columns
from converter.storage import Store
from tests.conftest import read_sample


def make_import(store, samples, schema, name="pos_guest_export.csv", **kw):
    rr = read_sample(samples, name, **kw)
    m = auto_map(schema, profile_columns(rr.df))
    data = (samples / name).read_bytes()
    return store.create_import(username="noah", filename=name, data=data, schema_label=schema.label,
                               schema_json=schema.model_dump_json(), mapping=m,
                               read_options={"sheet": rr.sheet, "header_row": rr.header_row})


def test_run_import_writes_everything(tmp_path, samples, schema):
    st = Store(tmp_path / "c.db")
    iid = make_import(st, samples, schema)
    rec = run_import(st, iid)
    assert rec.status == "completed" and rec.progress == 1.0 and rec.stage == "Done"
    assert (rec.total_rows, rec.imported, rec.rejected, rec.duplicates, rec.blank_rows) == (9, 6, 2, 1, 1)
    assert rec.started_at and rec.finished_at and rec.elapsed().total_seconds() >= 0
    for kind in ("output", "rejects", "summary", "audit", "source_table"):
        assert st.file_path(rec, kind) is not None, kind
    assert "bad row" in rec.summary["message"]
    assert rec.summary["failed_values"]["phone"] == ["555-1234"]


def test_xlsx_import_with_sheet(tmp_path, samples, schema):
    st = Store(tmp_path / "c.db")
    iid = make_import(st, samples, schema, "loyalty_members.xlsx", sheet="Members")
    rec = run_import(st, iid)
    assert rec.status == "completed" and rec.imported == 6


def test_failure_and_cancel_and_interrupt(tmp_path, samples, schema):
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
    st = Store(tmp_path / "c.db")
    iid = make_import(st, samples, schema)
    run_import(st, iid)
    with st._conn() as con:
        con.execute("UPDATE imports SET finished_at='2000-01-01T00:00:00+00:00' WHERE id=?", (iid,))
    assert st.purge_files(30) == 1
    rec = st.get_import(iid)
    assert rec.files_purged and rec.imported == 6 and st.file_path(rec, "output") is None
    assert not (st.files_root / str(iid)).exists()


def test_comparison(tmp_path, samples, schema):
    st = Store(tmp_path / "c.db")
    rec = run_import(st, make_import(st, samples, schema))
    folder = st.files_root / str(rec.id)
    src, audit = pd.read_parquet(folder / "source.parquet"), pd.read_parquet(folder / "audit.parquet")
    assert len(src) == len(audit) == 10
    plan = column_plan(schema, rec.mapping, list(src.columns))
    assert plan.feeds["Guest Name"] == ["First Name", "Last Name"] and plan.source_columns[0] == "Mobile #"

    counts = status_counts(audit, schema)
    assert counts["rejected"] == 2 and counts["duplicate"] == 1 and counts["empty"] == 1
    assert set(counts) == set(FILTERS)
    assert filter_rows(audit, src, schema, "rejected") == [6, 9]
    assert filter_rows(audit, src, schema, "all", "patel") == [7]
    assert filter_rows(audit, src, schema, "all", "#14") == [9]

    h = build_compare_html(src, audit, list(range(len(audit))), schema, plan)
    assert "Bad row" in h and "missing area code" in h and "c-fmt" in h and "→ First Name, Last Name" in h
    assert "<script" not in h
    # values are escaped
    src.iloc[0, 0] = "<b>x</b>"
    assert "&lt;b&gt;x&lt;/b&gt;" in build_compare_html(src, audit, [0], schema, plan)

    summary = field_change_summary(audit, schema, rec.mapping)
    phone = summary[summary["Field"] == "Phone"].iloc[0]
    assert phone["Reformatted"] == 7 and phone["Rejected rows"] == 2

    x = comparison_xlsx(src, audit, schema, plan, "test")
    back = pd.read_excel(io.BytesIO(x), header=None, dtype=str)
    assert back.iloc[3, 0] == "File row" and back.iloc[4, 1] == "Imported"
