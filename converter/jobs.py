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

The folder is ``DATA_DIR/imports/<id>/`` (see ``Store.import_dir``).

Import lifecycle (the ``imports.status`` column)
------------------------------------------------
::

    queued ──► running ──► completed
       │          ├──────► failed       (any exception; message stored in `error`, traceback in the server log)
       │          └──────► cancelled    (user pressed Cancel; noticed at the next progress check)
       ├────────────────► cancelled    (cancel requested before a worker picked it up)
       └── (server restart) ► interrupted   (queued or running when the process stopped; can be re-run)

* **queued** — ``Store.create_import`` stored the snapshot and the original file; waiting for a free worker.
* **running** — ``Store.start_import`` set ``started_at``; :func:`run_import` is reading/converting/writing.
* **completed** — ``Store.complete_import`` stored the counts, summary and file names; progress = 1.
* **failed / cancelled** — ``Store.finish_import`` stored the final status, stage text and optional error.
* **interrupted** — set by ``Store.mark_interrupted`` when a new :class:`ImportRunner` starts. Threads don't
  survive a restart, so anything still queued/running at that point can never finish on its own.

Progress is a fraction 0–1 plus a stage text. :class:`ProgressTracker` throttles database writes (at most one
every 0.5 s unless forced) and checks the cancel flag at most once a second; cancelling works by raising
:class:`ImportCancelled` from inside the progress callback, which unwinds the engine wherever it is.

Retention: :meth:`ImportRunner.purge` deletes the stored files of finished imports older than
``FILE_RETENTION_DAYS`` (records and counts stay), at start-up and then at most once an hour, piggy-backing on
:meth:`ImportRunner.submit`.

Main entry points: :class:`ImportRunner` (web app, one per server process) and :func:`run_import` (also called
directly and synchronously by the CLI, so CLI imports appear in the web app's Imports list too).
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
    """Raised by :class:`ProgressTracker` when the user has requested cancellation of the running import.

    It is raised from inside the progress callback, so it propagates out of ``engine.convert`` (or any other step
    that reports progress) and is caught in :func:`run_import`, which records the import as "cancelled".
    """
    pass


class ProgressTracker:
    """Throttles progress writes and checks for cancellation.

    An instance is the ``progress(fraction, stage)`` callback for one running import. The engine calls it very
    often (every field and every 2,000 rows); writing each call to SQLite would slow the import and contend with
    other users' requests, so:

    * a progress row update happens at most every ``write_every`` seconds (default 0.5 s), unless ``force=True``
      (used for milestones such as "Reading file" / "Writing files" so the stage text is never skipped);
    * the cancel flag is read from the database at most every ``cancel_every`` seconds (default 1 s). The first
      call always checks, because the timers start at 0.

    Times use ``time.monotonic()`` so wall-clock changes don't affect throttling.
    """

    def __init__(self, store: Store, import_id: int, write_every: float = 0.5, cancel_every: float = 1.0):
        """Remember the store and import id; ``write_every`` / ``cancel_every`` are the throttle intervals (s)."""
        self.store = store
        self.id = import_id
        self.write_every = write_every
        self.cancel_every = cancel_every
        self._last_write = 0.0
        self._last_cancel_check = 0.0

    def __call__(self, fraction: float, stage: str, force: bool = False) -> None:
        """Report progress (0–1) and stage text; raise :class:`ImportCancelled` if cancellation was requested.

        Side effects: may UPDATE the import's ``progress``/``stage`` (only while its status is "running") and may
        read its ``cancel_requested`` flag.
        """
        now = time.monotonic()
        if force or now - self._last_write >= self.write_every:
            self.store.update_progress(self.id, fraction, stage)
            self._last_write = now
        # The cancel check is throttled separately (and is not bypassed by force=True).
        if now - self._last_cancel_check >= self.cancel_every:
            self._last_cancel_check = now
            if self.store.is_cancel_requested(self.id):
                raise ImportCancelled()


def run_import(store: Store, import_id: int) -> ImportRecord:
    """Run one import to completion (synchronously). The outcome is recorded on the import (see Raises).

    Steps and the progress range each one reports:

    ==========  ==========================================================================================
    0.01        read the original upload from the import folder with the stored read options
    0.08        file read; ``source.parquet`` written (used by the side-by-side comparison)
    0.08–0.92   ``engine.convert`` (its own 0–1 progress is scaled into this range)
    0.93        write the output file, ``rejected.csv``, ``summary.txt`` and ``audit.parquet``
    0.99        finishing; then ``complete_import`` stores counts, summary and file names (progress 1.0)
    ==========  ==========================================================================================

    Args:
        store: The shared :class:`~converter.storage.Store`.
        import_id: Id of an import created with ``Store.create_import``.

    Returns:
        The import record as stored after the run (status completed / failed / cancelled), or the unchanged
        record if it was not "queued" (already ran, or marked interrupted at start-up).

    Raises:
        KeyError: only if no import with this id exists. Every other problem — unreadable file, bad mapping,
        cancellation — is recorded on the import instead of raised, because in the web app this runs in a pool
        thread where an exception would otherwise vanish silently.

    Side effects: updates the import row (status, progress, stage, counts, error) and writes files into
    ``DATA_DIR/imports/<id>/``.
    """
    rec = store.get_import(import_id)
    if rec is None:
        raise KeyError(import_id)
    if rec.status != "queued":
        return rec  # already ran, or was marked interrupted at startup
    if rec.cancel_requested:
        # Cancel was pressed while the import was still waiting for a worker: don't start it at all.
        store.finish_import(import_id, "cancelled", stage="Cancelled before it started")
        return store.get_import(import_id)

    store.start_import(import_id)
    track = ProgressTracker(store, import_id)
    try:
        # Everything comes from the snapshot stored with the import (schema JSON, read options, mapping,
        # overrides), not from current settings/templates/schema file, so edits made after the user pressed
        # "Start import" can't change an import that is already queued or running.
        schema = TargetSchema.model_validate_json(rec.schema_json)
        folder = store.import_dir(import_id)
        track(0.01, "Reading file", force=True)
        data = store.source_path(rec).read_bytes()
        opts = rec.read_options
        rr = read_file(rec.filename, data, sheet=opts.get("sheet"), header_row=opts.get("header_row", "auto"))
        rr.df.to_parquet(folder / "source.parquet", index=False)
        track(0.08, f"Read {len(rr.df):,} rows", force=True)

        # Original file row numbers (header and title rows counted) so rejects say "row 17" like Excel does.
        rows = [rr.excel_row_number(i) for i in range(len(rr.df))]
        result = convert(rr.df, schema, rec.mapping, row_numbers=rows, overrides=rec.overrides,
                         timezone=opts.get("timezone"),
                         progress=lambda f, stage: track(0.08 + 0.84 * f, stage))

        track(0.93, "Writing files", force=True)
        ts = timestamp()
        # files: logical kind -> file name inside the import folder. "*_download" entries are the friendlier
        # names offered when the user downloads (e.g. "guests_REJECTED_3rows_20261010-1342.csv"); the stored
        # names stay fixed so other code can find them.
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
        # The summary dict is stored as JSON on the import row; failed values are capped at 500 per field to keep
        # the row small.
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
    """Thread pool that runs queued imports in the background of the web server.

    The web app creates exactly one instance per server process (``st.cache_resource``) and calls
    :meth:`submit` right after ``Store.create_import``. Up to ``workers`` imports run at the same time
    (``IMPORT_WORKERS``); further submissions wait in the executor's queue while their status stays "queued".

    Creating a runner has side effects (see :meth:`__init__`), which is why there must be only one.
    """

    def __init__(self, store: Store, workers: int = 2, retention_days: int = 30):
        """Start the pool, recover from a previous run and purge old files.

        Args:
            store: Shared storage.
            workers: Number of worker threads (thread names "import_0", "import_1", …).
            retention_days: ``FILE_RETENTION_DAYS``; files of finished imports older than this are deleted
                (0 or less = keep forever).

        Side effects: marks every import still "queued"/"running" in the database as "interrupted" — they belonged
        to a previous server process whose threads are gone — logs a warning with the count, and runs
        :meth:`purge` once.
        """
        self.store = store
        self.retention_days = retention_days
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="import")
        self._lock = threading.Lock()  # serializes purge() runs
        self._last_purge = 0.0
        interrupted = store.mark_interrupted()
        if interrupted:
            log.warning("Marked %d import(s) as interrupted after restart", interrupted)
        self.purge()

    def submit(self, import_id: int) -> None:
        """Queue an already-created import to run on the pool, and purge old files if the last purge was > 1 h ago.

        Returns immediately; the result is recorded on the import by :func:`run_import` (the returned Future is
        not kept). Hooking the hourly purge onto submissions avoids running a separate timer thread.
        """
        self.pool.submit(run_import, self.store, import_id)
        if time.monotonic() - self._last_purge > 3600:
            self.purge()

    def cancel(self, import_id: int) -> None:
        """Request cancellation of a queued or running import (no effect on finished ones).

        Only sets the ``cancel_requested`` flag. A queued import is cancelled when a worker picks it up; a running
        one stops at its next progress check via :class:`ImportCancelled` (each field start and every 2,000 rows,
        usually within seconds; not while the file is being read or the output written).
        """
        self.store.request_cancel(import_id)

    def purge(self) -> None:
        """Delete stored files of finished imports older than ``retention_days``; never raises.

        The import records and their counts are kept (marked ``files_purged``); only the folder contents go.
        Errors are logged, not raised, because a purge failure must not break submitting an import.
        """
        with self._lock:
            self._last_purge = time.monotonic()
            try:
                n = self.store.purge_files(self.retention_days)
                if n:
                    log.info("Deleted stored files of %d import(s) older than %d days", n, self.retention_days)
            except Exception:
                log.exception("File purge failed")
