"""Number history (0.21.0): where each number and its consent came from, kept after the files are deleted.

Before: an import's files are deleted after FILE_RETENTION_DAYS (30 by default). After that nothing said which
file, row or column a customer's consent came from, so a complaint about a text ("I never opted in") couldn't be
answered. Now every converted number is recorded with its import, file row, phone and consent cells as written in
the file, and the consent value sent; the Number history page looks a number up.
"""

import threading
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from converter import numberlog
from converter.config import get_settings
from converter.jobs import ImportRunner, run_import
from converter.mapping import FieldMapping, Mapping
from converter.storage import Store

CSV = (b"Name,Phone,SMS Opt In\n"
       b"Ann,(516) 555-0177,Y\n"
       b"Bo,631-555-0188,N\n"
       b"Cy,555-1234,Y\n"                # 7 digits: a bad row, not recorded
       b"Bo,631-555-0188,N\n"            # the same row again: a duplicate, not recorded
       b"Di,917-555-1234,\n")

CONSENT = FieldMapping(sources=["SMS Opt In"], transform="map_values",
                       params={"value_map": [{"from": "Y", "to": "true"}, {"from": "N", "to": "false"}]},
                       origin="user")  # consent is only ever mapped by a person


def make_import(store, schema, consent=True, name="guests.csv", data=CSV):
    fields = {"phone_number": FieldMapping(sources=["Phone"], transform="copy")}
    if consent:
        fields["consent"] = CONSENT
    return store.create_import(username="noah", filename=name, data=data, schema_label=schema.label,
                               schema_json=schema.model_dump_json(), mapping=Mapping(fields=fields),
                               read_options={"header_row": "auto"})


def age(store, import_id, days):
    """Pretend an import finished `days` ago."""
    when = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with store._conn() as con:
        con.execute("UPDATE imports SET finished_at=?, created_at=? WHERE id=?", (when, when, import_id))


def test_completed_import_records_its_numbers(tmp_path, real_schema):
    st = Store(tmp_path / "c.db")
    iid = make_import(st, real_schema)
    rec = run_import(st, iid)
    assert rec.status == "completed" and (rec.imported, rec.rejected, rec.duplicates) == (3, 1, 1)
    assert st.numbers_logged(iid) == 3

    res = numberlog.lookup(st, "516.555.0177", real_schema)
    assert res.number == "5165550177" and res.error == ""
    [e] = res.entries
    assert (e["import_id"], e["filename"], e["username"], e["file_row"]) == (iid, "guests.csv", "noah", 2)
    assert (e["phone_value"], e["consent"], e["consent_value"]) == ("(516) 555-0177", "true", "Y")
    assert e["consent_from"] == "Translate values: SMS Opt In (Y=true; N=false)"
    assert e["imported_at"] == rec.finished_at

    bo = numberlog.lookup(st, "(631) 555-0188", real_schema).entries
    assert [(x["file_row"], x["consent"]) for x in bo] == [(3, "false")]  # the duplicate row isn't recorded
    di = numberlog.lookup(st, "9175551234", real_schema).entries
    assert (di[0]["consent"], di[0]["consent_value"]) == ("", "")


def test_consent_not_mapped(tmp_path, real_schema):
    st = Store(tmp_path / "c.db")
    iid = make_import(st, real_schema, consent=False)
    run_import(st, iid)
    [e] = numberlog.lookup(st, "5165550177", real_schema).entries
    assert (e["consent"], e["consent_value"], e["consent_from"]) == ("", "", "")


def test_newest_import_first_and_kept_after_the_files_are_deleted(tmp_path, real_schema):
    st = Store(tmp_path / "c.db")
    first = make_import(st, real_schema)
    run_import(st, first)
    age(st, first, 40)
    second = make_import(st, real_schema, consent=False, name="guests_october.csv")
    run_import(st, second)
    assert st.purge_files(30) == 1 and st.get_import(first).files_purged
    entries = numberlog.lookup(st, "5165550177", real_schema).entries
    assert [(e["import_id"], e["filename"]) for e in entries] == [(second, "guests_october.csv"), (first, "guests.csv")]
    assert entries[1]["consent"] == "true"


def test_number_record_days(tmp_path, real_schema, monkeypatch):
    st = Store(tmp_path / "c.db")
    old, new = make_import(st, real_schema), make_import(st, real_schema)
    run_import(st, old)
    run_import(st, new)
    age(st, old, 400)
    assert st.purge_number_log(0) == 0  # 0 = keep forever (the default)
    assert st.purge_number_log(365) == 3
    assert (st.numbers_logged(old), st.numbers_logged(new)) == (0, 3)
    assert st.get_import(old).imported == 3  # the import's record stays
    monkeypatch.setenv("NUMBER_RECORD_DAYS", "365")
    assert get_settings().number_record_days == 365
    monkeypatch.delenv("NUMBER_RECORD_DAYS")
    assert get_settings().number_record_days == 0


def test_backfill_records_earlier_imports(tmp_path, real_schema):
    """Imports from before 0.21.0 (nothing recorded) are recorded from their audit file while it still exists."""
    st = Store(tmp_path / "c.db")
    kept, purged, too_old = (make_import(st, real_schema) for _ in range(3))
    for iid in (kept, purged, too_old):
        run_import(st, iid)
        st.log_numbers(iid, [])  # as if completed before 0.21.0
    age(st, purged, 40)
    st.purge_files(30)
    age(st, too_old, 20)
    assert st.imports_to_log() == [kept, too_old]
    assert numberlog.backfill(st, record_days=10) == 1  # too_old is past NUMBER_RECORD_DAYS: not brought back
    assert [st.numbers_logged(i) for i in (kept, purged, too_old)] == [3, 0, 0]
    [e] = numberlog.lookup(st, "5165550177", real_schema).entries
    assert (e["import_id"], e["consent"], e["consent_value"]) == (kept, "true", "Y")  # the cell, from source.parquet
    assert numberlog.backfill(st) == 1 and st.numbers_logged(too_old) == 3
    assert numberlog.backfill(st) == 0  # nothing left to do


def test_recording_again_replaces(tmp_path, real_schema):
    st = Store(tmp_path / "c.db")
    iid = make_import(st, real_schema)
    run_import(st, iid)
    audit = pd.read_parquet(st.file_path(st.get_import(iid), "audit"))
    assert numberlog.record(st, iid, audit, real_schema) == 3 and st.numbers_logged(iid) == 3
    # Without the file and mapping, the consent value before normalizing (after the Y=true translation) is kept.
    assert numberlog.lookup(st, "5165550177", real_schema).entries[0]["consent_value"] == "true"


def test_a_recording_failure_doesnt_fail_the_import(tmp_path, real_schema, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(numberlog, "record", broken)
    st = Store(tmp_path / "c.db")
    iid = make_import(st, real_schema)
    assert run_import(st, iid).status == "completed"
    assert st.numbers_logged(iid) == 0 and st.imports_to_log() == [iid]  # recorded on the next start


@pytest.mark.parametrize("text,error", [("", "type a phone number"), ("555-1234", "missing area code"),
                                        ("hello", "")])
def test_lookup_of_something_that_isnt_a_number(tmp_path, real_schema, text, error):
    res = numberlog.lookup(Store(tmp_path / "c.db"), text, real_schema)
    assert res.error and error in res.error and res.entries == []


def test_unknown_number(tmp_path, real_schema):
    res = numberlog.lookup(Store(tmp_path / "c.db"), "516 555 0100", real_schema)
    assert (res.number, res.error, res.entries) == ("5165550100", "", [])


def test_consent_source(real_schema):
    assert numberlog.consent_source(real_schema, Mapping(fields={"consent": CONSENT})) == (
        "Translate values: SMS Opt In (Y=true; N=false)")
    assert numberlog.consent_source(real_schema, Mapping(fields={
        "consent": FieldMapping(sources=["Opt In"], transform="copy", origin="user")})) == "Opt In"
    assert numberlog.consent_source(real_schema, Mapping(fields={
        "consent": FieldMapping(transform="constant", params={"value": "true"}, origin="user")})
    ) == "fixed value 'true'"
    assert numberlog.consent_source(real_schema, Mapping()) == ""
    # A manual-only field ignores what rules, templates or Claude put there.
    assert numberlog.consent_source(real_schema, Mapping(fields={
        "consent": FieldMapping(sources=["Opt In"], transform="copy", origin="rule")})) == ""


def test_runner_purges_and_backfills(tmp_path, real_schema):
    st = Store(tmp_path / "c.db")
    old, earlier = make_import(st, real_schema), make_import(st, real_schema)
    run_import(st, old)
    run_import(st, earlier)
    age(st, old, 100)
    st.log_numbers(earlier, [])
    runner = ImportRunner(st, workers=1, retention_days=0, record_days=30)
    deadline = time.time() + 10
    while any(t.name == "number-log-backfill" for t in threading.enumerate()) and time.time() < deadline:
        time.sleep(0.05)
    assert (st.numbers_logged(old), st.numbers_logged(earlier)) == (0, 3)
    runner.pool.shutdown(wait=True)
