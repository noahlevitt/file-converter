"""Command-line interface (handy for testing and scripting).

  python -m converter.cli inspect  FILE                 show detected columns and the proposed mapping
  python -m converter.cli convert  FILE [--out DIR]     convert using a saved template or auto-mapping
  python -m converter.cli templates                     list saved templates
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import get_settings
from .engine import convert
from .mapping import profile_columns
from .output import output_filename, rejects_filename, timestamp, write_output, write_rejects, write_summary
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
    settings, schema, store, path, rr, mapping, source, _ = _load(args)
    problems = mapping.problems(schema)
    if problems:
        for k, v in problems.items():
            print(f"PROBLEM {schema.field(k).header}: {v}", file=sys.stderr)
        sys.exit("Mapping is incomplete; fix it in the web app or pass --template.")
    rows = [rr.excel_row_number(i) for i in range(len(rr.df))]
    res = convert(rr.df, schema, mapping, row_numbers=rows)
    out_dir = Path(args.out or ".")
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = timestamp()
    out_path = out_dir / output_filename(schema, path.name, ts)
    out_path.write_bytes(write_output(res.output, schema))
    print(res.flag_message())
    print(f"Converted file: {out_path}")
    if not res.rejects.empty:
        rej = out_dir / rejects_filename(path.name, len(res.rejects), ts)
        rej.write_bytes(write_rejects(res))
        print(f"Rejected rows:  {rej}")
    (out_dir / f"{path.stem}_SUMMARY_{ts}.txt").write_bytes(write_summary(res, schema, path.name, "cli", __version__))
    if args.save_template:
        store.save_template(args.save_template, list(rr.df.columns), mapping,
                            {"sheet": rr.sheet, "header_row": rr.header_row}, schema.name, "cli")
        print(f"Saved template '{args.save_template}'.")
    store.log_job("cli", path.name, args.template, schema.label, res.summary_dict(), args.ai)
    if res.rejected:
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
