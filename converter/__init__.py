"""File conversion engine: turns arbitrary contact/customer files into the target import format.

Overview
--------
The ``converter`` package holds everything except the Streamlit page itself (``app.py``). A file flows through
it like this::

    reader.read_file ──► mapping.profile_columns ──► services.initial_mapping ──► (review in the web app)
        ──► storage.Store.create_import ──► jobs.run_import ──► engine.convert ──► output.write_*

Modules
    * ``reader``      — read CSV/TSV/TXT and Excel files as text (encoding, delimiter, header-row detection).
    * ``schema``      — the target import format, loaded from ``schemas/target_schema.yaml``.
    * ``mapping``     — the mapping model (target field ← source columns + transform), column profiles and header
      fingerprints used to recognise a file layout seen before.
    * ``heuristics``  — rule-based auto-mapping from header names and value types.
    * ``transforms``  — the fixed registry of extraction transforms (copy, combine, name part, …).
    * ``normalizers`` / ``dateparse`` — per-type formatting and validation (phones, dates, e-mails, ZIPs, …).
    * ``engine``      — deterministic conversion: apply the mapping, normalize, validate, dedupe, audit.
    * ``checks``      — rule-based mistake checks on a mapping/conversion.
    * ``ai``          — optional Claude assistant (mapping, notes, mistake review, value repairs).
    * ``services``    — orchestration shared by the web app and the CLI.
    * ``storage``     — SQLite storage for users, templates and imports.
    * ``jobs``        — background import runner (thread pool, progress, cancellation, file retention).
    * ``output``      — writing the converted file, rejected rows and summary.
    * ``compare`` / ``editor`` / ``ui_format`` — helpers for the web UI.
    * ``auth``        — Streamlit login gate.
    * ``config``      — settings from environment variables / ``.env``.
    * ``cli``         — command line: ``python -m converter.cli inspect|convert|templates``.

``__version__`` is the tool version shown in the web app's sidebar, the CLI's ``--help`` and every import's
``summary.txt``; bump it with each release.
"""

__version__ = "0.17.0"
