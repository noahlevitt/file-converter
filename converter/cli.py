"""Command-line interface (handy for testing and scripting).

  python -m converter.cli inspect  FILE                 show detected columns and the proposed mapping
  python -m converter.cli convert  FILE [--out DIR]     convert using a saved template or auto-mapping
  python -m converter.cli templates                     list saved templates
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
    if value is None or value == "auto":
        return "auto"
    if value.lower() == "none":
        return None
    return int(value) - 1


def _load(args):
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
    """Runs through the same import pipeline as the web app, so CLI imports also appear under Imports."""
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
    for kind, download_name in (("output", None), ("rejects", "rejects_download"), ("summary", "summary_download")):
        src = store.file_path(rec, kind)
        if src is None or (kind == "rejects" and not (rec.rejected or rec.duplicates)):
            continue
        dest = out_dir / (rec.files.get(download_name) if download_name else src.name)
        shutil.copyfile(src, dest)
        print(f"  {kind:<8} {dest}")
    if rec.rejected:
        sys.exit(2)  # non-zero so scripts notice bad rows


def cmd_templates(args) -> None:
    settings = get_settings()
    for t in Store(settings.db_path).list_templates():
        print(f"{t.name:<35} {len(t.headers):>3} columns  used {t.use_count}x  ({t.schema_name})")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="converter", description=f"Import file converter v{__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
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
