"""Background import runner.

An import is created (status "queued") with a snapshot of everything it needs: the uploaded file, the read
options, the mapping, approved fixes and the target schema. A small thread pool runs queued imports and writes
progress to the database so every user's browser can watch it. Results are written to the import's folder:

    source.<ext>      the original upload
    source.parquet    the file as read (text), for the side-by-side view
    audit.parquet     per-row / per-cell conversion record (see engine.ConversionResult.audit)
    <output file>     the converted file in the target format
    rejected.csv      rejected + duplicate rows with reasons
    summary.txt       human-readable summary
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import __version__
from .engine import convert
from .output import output_filename, rejects_filename, timestamp, write_output, write_rejects, write_summary
from .reader import read_file
from .schema import TargetSchema
from .storage import ImportRecord, Store

log = logging.getLogger(__name__)


class ImportCancelled(Exception):
    pass


class ProgressTracker:
    """Throttles progress writes and checks for cancellation."""

    def __init__(self, store: Store, import_id: int, write_every: float = 0.5, cancel_every: float = 1.0):
        self.store = store
        self.id = import_id
        self.write_every = write_every
        self.cancel_every = cancel_every
        self._last_write = 0.0
        self._last_cancel_check = 0.0

    def __call__(self, fraction: float, stage: str, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_write >= self.write_every:
            self.store.update_progress(self.id, fraction, stage)
            self._last_write = now
        if now - self._last_cancel_check >= self.cancel_every:
            self._last_cancel_check = now
            if self.store.is_cancel_requested(self.id):
                raise ImportCancelled()


def run_import(store: Store, import_id: int) -> ImportRecord:
    """Run one import to completion (synchronously). Never raises; the outcome is recorded on the import."""
    rec = store.get_import(import_id)
    if rec is None:
        raise KeyError(import_id)
    if rec.status != "queued":
        return rec  # already ran, or was marked interrupted at startup
    if rec.cancel_requested:
        store.finish_import(import_id, "cancelled", stage="Cancelled before it started")
        return store.get_import(import_id)

    store.start_import(import_id)
    track = ProgressTracker(store, import_id)
    try:
        schema = TargetSchema.model_validate_json(rec.schema_json)
        folder = store.import_dir(import_id)
        track(0.01, "Reading file", force=True)
        data = store.source_path(rec).read_bytes()
        opts = rec.read_options
        rr = read_file(rec.filename, data, sheet=opts.get("sheet"), header_row=opts.get("header_row", "auto"))
        rr.df.to_parquet(folder / "source.parquet", index=False)
        track(0.08, f"Read {len(rr.df):,} rows", force=True)

        rows = [rr.excel_row_number(i) for i in range(len(rr.df))]
        result = convert(rr.df, schema, rec.mapping, row_numbers=rows, overrides=rec.overrides,
                         timezone=opts.get("timezone"),
                         progress=lambda f, stage: track(0.08 + 0.84 * f, stage))

        track(0.93, "Writing files", force=True)
        ts = timestamp()
        files = {
            "source": store.source_path(rec).name,
            "output": output_filename(schema, rec.filename, ts),
            "rejects": "rejected.csv",
            "rejects_download": rejects_filename(rec.filename, len(result.rejects), ts),
            "summary": "summary.txt",
            "summary_download": f"{Path(rec.filename).stem}_SUMMARY_{ts}.txt",
            "audit": "audit.parquet",
            "source_table": "source.parquet",
        }
        (folder / files["output"]).write_bytes(write_output(result.output, schema))
        (folder / files["rejects"]).write_bytes(write_rejects(result))
        (folder / files["summary"]).write_bytes(
            write_summary(result, schema, rec.filename, rec.username, __version__))
        result.audit.to_parquet(folder / files["audit"], index=False)
        track(0.99, "Finishing", force=True)
        summary = result.summary_dict()
        summary["fill_rates"] = result.fill_rates
        summary["failed_values"] = {k: v[:500] for k, v in result.failed_values.items()}
        summary["warnings"] = rr.warnings
        store.complete_import(import_id, summary, files)
    except ImportCancelled:
        store.finish_import(import_id, "cancelled", stage="Cancelled by user")
    except Exception as exc:  # record the failure for the user; full trace goes to the server log
        log.error("Import %s failed:\n%s", import_id, traceback.format_exc())
        store.finish_import(import_id, "failed", error=f"{type(exc).__name__}: {exc}", stage="Failed")
    return store.get_import(import_id)


class ImportRunner:
    """Thread pool that runs queued imports in the background of the web server."""

    def __init__(self, store: Store, workers: int = 2, retention_days: int = 30):
        self.store = store
        self.retention_days = retention_days
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="import")
        self._lock = threading.Lock()
        self._last_purge = 0.0
        interrupted = store.mark_interrupted()
        if interrupted:
            log.warning("Marked %d import(s) as interrupted after restart", interrupted)
        self.purge()

    def submit(self, import_id: int) -> None:
        self.pool.submit(run_import, self.store, import_id)
        if time.monotonic() - self._last_purge > 3600:
            self.purge()

    def cancel(self, import_id: int) -> None:
        self.store.request_cancel(import_id)

    def purge(self) -> None:
        with self._lock:
            self._last_purge = time.monotonic()
            try:
                n = self.store.purge_files(self.retention_days)
                if n:
                    log.info("Deleted stored files of %d import(s) older than %d days", n, self.retention_days)
            except Exception:
                log.exception("File purge failed")
