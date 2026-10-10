"""Command-line interface (handy for testing and scripting).

  python -m converter.cli inspect  FILE                 show detected columns and the proposed mapping
  python -m converter.cli convert  FILE [--out DIR]     convert using a saved template or auto-mapping
  python -m converter.cli templates                     list saved templates

Overview
--------
The CLI uses the same settings (``.env`` / environment), database, templates and import pipeline as the web app:

* ``inspect`` reads the file and prints how it was read (rows, columns, sheet, header row, reader warnings) and
  the mapping that would be used, field by field, plus any mapping problems. Nothing is written.
* ``convert`` builds the same mapping, refuses to run if it has problems, optionally saves it as a template, then
  creates an import (user "cli") and runs it synchronously through :func:`converter.jobs.run_import`. The import
  therefore also appears under Imports in the web app. The converted file, the rejected-rows CSV (only when there
  are bad or duplicate rows) and the summary are copied to ``--out``.
* ``templates`` lists saved templates with their column count, use count and target schema.

Options shared by ``inspect`` and ``convert``:
    --sheet NAME          Excel sheet to read (default, or a name not in the workbook: the first sheet).
    --header-row N        1-based header row number, "none" (file has no header row) or "auto" (default: detect).
    --template NAME       use this saved template instead of auto-mapping (no rules, no Claude).
    --ai                  let Claude refine the auto-mapping (needs ANTHROPIC_API_KEY and AI_ENABLED).
    --schema PATH         target schema YAML (default: SCHEMA_PATH).
    --timezone ZONE       client time zone, e.g. America/Denver (default: APP_TIMEZONE); used by ``convert``.
``convert`` only:
    --out DIR             folder for the result files (created if missing; default: current folder).
    --save-template NAME  save the mapping that was used as a template (overwrites one with the same name).

Examples::

    python -m converter.cli inspect sample_data/pos_guest_export.csv
    python -m converter.cli inspect export.xlsx --sheet Members --header-row 3
    python -m converter.cli convert export.csv --out converted/ --save-template "Acme POS"
    python -m converter.cli convert export.csv --template "Acme POS" --timezone America/Denver

Exit codes:
    0   success (for ``convert``: every row converted, or only duplicates removed)
    1   error, with the message on stderr: unreadable/unsupported file, unknown ``--template``, incomplete mapping
        ("Mapping is incomplete; ..."), or the import failed/was cancelled
    2   ``convert`` finished and wrote its files, but some rows were rejected (see the REJECTED csv) — non-zero so
        scripts notice bad rows. argparse also uses 2 for invalid command-line usage.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from . import __version__
from .config import get_settings
from .jobs import run_import
from .mapping import profile_columns
from .reader import ReadError, read_file
from .schema import load_schema
from .services import initial_mapping, make_assistant
from .storage import Store


def _header_arg(value: str | None):
    """Translate ``--header-row`` into the reader's ``header_row`` argument.

    ``None`` / "auto" -> "auto" (detect); "none" (any case) -> ``None`` (no header row, columns become
    "Column 1", "Column 2", …); a number -> 0-based index, e.g. "4" -> 3 (the user counts rows like Excel does).
    A non-numeric value other than auto/none raises ``ValueError``.
    """
    if value is None or value == "auto":
        return "auto"
    if value.lower() == "none":
        return None
    return int(value) - 1


def _load(args):
    """Read the file named on the command line and work out its mapping (shared by ``inspect`` and ``convert``).

    With ``--template`` the saved template's mapping is used (normalized to this file's columns; fields whose
    columns are missing become blank). Otherwise :func:`converter.services.initial_mapping` runs: exact template
    → similar template → rules → Claude (only with ``--ai`` and a configured API key).

    Returns:
        ``(settings, schema, store, path, read_result, mapping, source, notes)`` where ``source`` describes where
        the mapping came from ("template", "rules", "rules+ai" or "template '<name>'") and ``notes`` are the
        messages from ``initial_mapping``.

    Exits with status 1 if ``--template`` names a template that doesn't exist. ``ReadError`` from the reader is
    handled by :func:`main`. Side effects: opens (and if needed creates) the SQLite database; one Claude call
    with ``--ai``.
    """
    settings = get_settings()
    schema = load_schema(args.schema or settings.schema_path)
    store = Store(settings.db_path)
    path = Path(args.file)
    rr = read_file(path.name, path.read_bytes(), sheet=args.sheet, header_row=_header_arg(args.header_row))
    profiles = profile_columns(rr.df, n_samples=settings.ai_sample_values)
    if args.template:
        tpl = store.get_template(args.template)
        if tpl is None:
            sys.exit(f"Template '{args.template}' not found.")
        mapping, source = tpl.mapping.normalized(schema, list(rr.df.columns)), f"template '{tpl.name}'"
        notes: list[str] = []
    else:
        assistant = make_assistant(settings) if args.ai else None
        sug = initial_mapping(schema, rr.df, profiles, store, assistant, use_ai=args.ai)
        mapping, source, notes = sug.mapping, sug.source, sug.notes
    return settings, schema, store, path, rr, mapping, source, notes


def cmd_inspect(args) -> None:
    """``inspect``: print how the file was read and the proposed mapping; writes nothing.

    Output layout::

        <file>: <rows> rows, <n> columns[, sheet '<sheet>'], header on row <n> | no header row
          ! <reader warning>                       (one per warning, e.g. ragged rows)
        Mapping source: rules | rules+ai | template | template '<name>'
          - <note>                                 (template matched, Claude's notes, ...)
         * <field header>  <- <mapping summary>   <confidence> <reason>     ("*" = required field)
          PROBLEM <field header>: <problem>        (e.g. required field has no source column)
    """
    _, schema, _, path, rr, mapping, source, notes = _load(args)
    print(f"{path.name}: {len(rr.df):,} rows, {len(rr.df.columns)} columns"
          + (f", sheet '{rr.sheet}'" if rr.sheet else "")
          + (f", header on row {rr.header_row + 1}" if rr.header_row is not None else ", no header row"))
    for w in rr.warnings:
        print("  ! " + w)
    print(f"Mapping source: {source}")
    for n in notes:
        print("  - " + n)
    for f in schema.fields:
        fm = mapping.get(f.name)
        mark = "*" if f.required else " "
        print(f" {mark} {f.header:<18} <- {fm.summary():<45} {fm.confidence:<6} {fm.reason}")
    problems = mapping.problems(schema)
    for k, v in problems.items():
        print(f"  PROBLEM {schema.field(k).header}: {v}")


def cmd_convert(args) -> None:
    """Runs through the same import pipeline as the web app, so CLI imports also appear under Imports.

    Steps:
        1. Build the mapping like ``inspect``. If ``Mapping.problems`` reports anything (e.g. the required phone
           field has no source column), print each problem to stderr and exit 1 — nothing is imported.
        2. With ``--save-template NAME``, save the mapping plus read options (sheet, header row) as a template
           (created by "cli"), so later files with the same headers are matched automatically.
        3. Create an import record (user "cli", the file's bytes, schema snapshot, mapping, read options incl. the
           time zone) and run it right here with :func:`converter.jobs.run_import` — synchronously, no thread pool.
           Exit 1 with "Import #<id> failed: ..." if it did not complete.
        4. Copy the result files from the import folder into ``--out``: the converted file (its stored name), the
           rejected-rows CSV under its download name (``*_REJECTED_<n>rows_*.csv``, only when rows were rejected
           or removed as duplicates) and the summary under its download name (``*_SUMMARY_*.txt``).
        5. Exit 2 if any rows were rejected (bad rows); duplicates alone don't change the exit code.

    Side effects: writes a row to the imports table, files under ``DATA_DIR/imports/<id>/``, the copied result
    files in ``--out`` and, with ``--save-template``, a template row.
    """
    settings, schema, store, path, rr, mapping, source, _ = _load(args)
    problems = mapping.problems(schema)
    if problems:
        for k, v in problems.items():
            print(f"PROBLEM {schema.field(k).header}: {v}", file=sys.stderr)
        sys.exit("Mapping is incomplete; fix it in the web app or pass --template.")
    if args.save_template:
        store.save_template(args.save_template, list(rr.df.columns), mapping,
                            {"sheet": rr.sheet, "header_row": rr.header_row}, schema.name, "cli")
        print(f"Saved template '{args.save_template}'.")
    # Store the header row that was actually used (detected or given), so the import re-reads the file the same
    # way. ai_used records whether --ai was passed.
    import_id = store.create_import(
        username="cli", filename=path.name, data=path.read_bytes(), schema_label=schema.label,
        schema_json=schema.model_dump_json(), mapping=mapping,
        read_options={"sheet": rr.sheet, "header_row": rr.header_row, "timezone": args.timezone or settings.timezone},
        template_name=args.template or args.save_template, ai_used=args.ai)
    rec = run_import(store, import_id)
    if rec.status != "completed":
        sys.exit(f"Import #{rec.id} {rec.status}: {rec.error or ''}")
    out_dir = Path(args.out or ".")
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Import #{rec.id}: {rec.summary.get('message', '')}")
    # (kind, key of the friendlier download name in rec.files); None = keep the stored file name.
    for kind, download_name in (("output", None), ("rejects", "rejects_download"), ("summary", "summary_download")):
        src = store.file_path(rec, kind)
        if src is None or (kind == "rejects" and not (rec.rejected or rec.duplicates)):
            continue  # file missing, or an empty rejects file not worth copying
        dest = out_dir / (rec.files.get(download_name) if download_name else src.name)
        shutil.copyfile(src, dest)
        print(f"  {kind:<8} {dest}")
    if rec.rejected:
        sys.exit(2)  # non-zero so scripts notice bad rows


def cmd_templates(args) -> None:
    """``templates``: list saved templates, one per line: name, column count, use count and schema name.

    ``args`` is unused (the command has no options). Read-only.
    """
    settings = get_settings()
    for t in Store(settings.db_path).list_templates():
        print(f"{t.name:<35} {len(t.headers):>3} columns  used {t.use_count}x  ({t.schema_name})")


def main(argv: list[str] | None = None) -> None:
    """Parse the command line and dispatch to ``cmd_inspect`` / ``cmd_convert`` / ``cmd_templates``.

    Args:
        argv: Arguments without the program name (default: ``sys.argv[1:]``); tests pass a list.

    A ``ReadError`` (unsupported file type, empty or unreadable file) is turned into a plain error message and
    exit status 1 instead of a traceback. See the module docstring for the exit codes.
    """
    ap = argparse.ArgumentParser(prog="converter", description=f"Import file converter v{__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    # inspect and convert share the file-reading and mapping options; convert adds --out / --save-template.
    for name in ("inspect", "convert"):
        p = sub.add_parser(name)
        p.add_argument("file")
        p.add_argument("--sheet")
        p.add_argument("--header-row", help="1-based row number of the header, 'none', or 'auto' (default)")
        p.add_argument("--template", help="use this saved template instead of auto-mapping")
        p.add_argument("--ai", action="store_true", help="let Claude refine the mapping (needs ANTHROPIC_API_KEY)")
        p.add_argument("--schema", help="path to a target schema YAML (default: SCHEMA_PATH)")
        p.add_argument("--timezone", help="client time zone, e.g. America/Denver (default: APP_TIMEZONE)")
        if name == "convert":
            p.add_argument("--out", help="output folder (default: current folder)")
            p.add_argument("--save-template", metavar="NAME", help="save the mapping used as a template")
    sub.add_parser("templates")
    args = ap.parse_args(argv)
    try:
        {"inspect": cmd_inspect, "convert": cmd_convert, "templates": cmd_templates}[args.cmd](args)
    except ReadError as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
