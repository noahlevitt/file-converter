"""Regression check: run a fixed set of files through the converter and compare the results between two versions.

What it does
------------
Every file in ``scripts/regression/corpus.zip`` (400 generated customer exports: varied columns, header rows,
phone and date spellings, title lines; no real customer data) and in ``sample_data/`` is read, mapped
automatically (rules only, no Claude) and converted, exactly as the app does without a reviewer. For each file
the result is summarised: the columns read, which columns the mapping used, a hash of the converted file, the
imported / bad-row counts and the bad-row reasons. Two summaries are then compared file by file.

Used before every release: a change meant to fix one thing must not change the output of files it wasn't aimed
at. Any difference is either expected (and explained in the changelog) or a regression.

Usage (from the repository root)::

    python scripts/regression/regression.py check v0.21.2      # this working tree vs. a tagged version
    python scripts/regression/regression.py run out.json       # summarise this working tree only
    python scripts/regression/regression.py compare a.json b.json

``check`` runs the tagged version in a temporary ``git worktree`` (removed afterwards) with the SAME corpus and
sample files, so only the code differs. Exit code 1 when results differ, 0 when they are identical.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent


def corpus() -> list[tuple[str, bytes]]:
    """(file name, bytes) for every regression file: the zipped corpus, then sample_data/ (in name order)."""
    files = []
    with zipfile.ZipFile(HERE / "corpus.zip") as z:
        files += [(n, z.read(n)) for n in sorted(z.namelist())]
    files += [(p.name, p.read_bytes()) for p in sorted((REPO / "sample_data").iterdir()) if p.is_file()]
    return files


def run(code_root: Path, files: list[tuple[str, bytes]]) -> dict:
    """Summarise every file with the converter found in `code_root` (imported fresh, so two roots don't mix)."""
    sys.path.insert(0, str(code_root))
    try:
        from converter.engine import convert
        from converter.mapping import profile_columns
        from converter.output import write_output
        from converter.reader import ReadError, read_file
        from converter.schema import load_schema
        from converter.services import initial_mapping
        schema = load_schema(code_root / "schemas" / "target_schema.yaml")
        out = {}
        for name, data in files:
            try:
                rr = read_file(name, data)
                m = initial_mapping(schema, rr.df, profile_columns(rr.df), None, None, use_ai=False).mapping
                r = convert(rr.df, schema, m, row_numbers=[rr.excel_row_number(i) for i in range(len(rr.df))])
                out[name] = {"cols": list(rr.df.columns),
                             "map": {k: v.sources for k, v in m.fields.items() if v.transform != "none"},
                             "out": hashlib.sha1(write_output(r.output, schema)).hexdigest(),
                             "imported": r.imported, "rejected": r.rejected, "reasons": dict(r.reason_counts)}
            except ReadError as e:
                out[name] = {"readerror": str(e)[:100]}
            except Exception as e:  # noqa: BLE001 - a crash is a result to compare, not a reason to stop
                out[name] = {"crash": f"{type(e).__name__}: {str(e)[:100]}"}
        return out
    finally:
        sys.path.remove(str(code_root))
        for mod in [m for m in sys.modules if m == "converter" or m.startswith("converter.")]:
            del sys.modules[mod]


def compare(a: dict, b: dict) -> list[str]:
    """One line per difference: '<file>: <what> <before> -> <after>'."""
    lines = []
    for name in sorted(set(a) | set(b)):
        x, y = a.get(name), b.get(name)
        if x == y:
            continue
        if x is None or y is None:
            lines.append(f"{name}: only in {'after' if x is None else 'before'}")
            continue
        for key in sorted(set(x) | set(y)):
            if x.get(key) != y.get(key):
                lines.append(f"{name}: {key} {json.dumps(x.get(key))[:200]} -> {json.dumps(y.get(key))[:200]}")
    return lines


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "run":
        res = run(REPO, corpus())
        Path(argv[1]).write_text(json.dumps(res, indent=0, sort_keys=True))
        print(f"{len(res)} files; {sum('crash' in v for v in res.values())} crashes -> {argv[1]}")
        return 0
    if len(argv) == 3 and argv[0] == "compare":
        diff = compare(*(json.loads(Path(p).read_text()) for p in argv[1:]))
        print("\n".join(diff) or "identical")
        return 1 if diff else 0
    if len(argv) == 2 and argv[0] == "check":
        files = corpus()
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "old"
            subprocess.run(["git", "-C", str(REPO), "worktree", "add", "-f", "--detach", str(old), argv[1]],
                           check=True, capture_output=True)
            try:
                before = run(old, files)
            finally:
                subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(old)], capture_output=True)
                subprocess.run(["git", "-C", str(REPO), "worktree", "prune"], capture_output=True)
        after = run(REPO, files)
        diff = compare(before, after)
        crashes = sum("crash" in v for v in after.values())
        print("\n".join(diff) or f"identical: {len(after)} files, {crashes} crashes")
        return 1 if diff else 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
