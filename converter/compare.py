"""Side-by-side comparison of an import: original file vs converted file, cell by cell.

Built from two tables stored with every completed import:
  source.parquet  – the uploaded file exactly as read (text)
  audit.parquet   – per row: file row number, status (imported/rejected/duplicate/empty), reason,
                    and per target field: converted value, value before normalizing, cell status

Overview
--------
Both tables are written by ``jobs.run_import`` into the import's folder and loaded by the web app
(``app.load_import_tables``). Before they reach this module, ``engine.source_rows`` lines the source rows up with
the audit rows, so row *i* of ``source`` is the file row that produced audit row *i* -- even when one row per
phone number made several converted rows from one file row (several phone columns, or several numbers in one
cell). Every function here relies on that
alignment and on both frames having a default ``RangeIndex`` (index labels == positions).

Audit columns used here (see ``engine.ConversionResult.audit``):
  _row (file row number), _extra (True for the 2nd/3rd... row made from one file row), _status, _reason,
  _issues (JSON {field_name: reason}), and per target field ``out.<name>`` (converted value), ``raw.<name>``
  (value before normalizing) and ``st.<name>`` (cell status code, one of ``engine.CELL_*`` or "" for empty).

Responsibilities and main entry points
--------------------------------------
* ``column_plan`` -- decide the order of the source columns and describe which target fields each one feeds
  (and where each target field comes from). Shared by the HTML view and the Excel export.
* ``status_counts`` / ``filter_rows`` -- the filter buttons above the table ("Rows with changes",
  "Problems only", "Bad rows", ...) and the free-text search; they return the matching row positions.
* ``build_compare_html`` -- render one page of rows as an HTML table (original on the left, converted on the
  right, colour-coded by cell status, with hover tooltips). Shown with ``st.html``.
* ``field_change_summary`` -- the "What changed" tab: per target field, how many cells were unchanged,
  reformatted, built, defaulted, fixed, repaired, blanked or rejected, with a few examples.
* ``comparison_xlsx`` -- the same side-by-side view, all rows, as a downloadable Excel workbook.

Cell status colour code (identical in the HTML legend/CSS and in the Excel fills):
  unchanged = no colour, reformatted = blue, built from part of / several columns = purple,
  default/fixed value = grey (italic in HTML), approved Claude fix = green, repaired typo = orange,
  invalid -> left blank = yellow, invalid/missing -> row rejected = red.
"""

from __future__ import annotations

import html
import io
import json
from dataclasses import dataclass

import pandas as pd

from .engine import (CELL_BLANKED, CELL_DEFAULT, CELL_DERIVED, CELL_FIXED, CELL_FORMAT, CELL_REJECT, CELL_REPAIRED,
                     CELL_SAME)
from .mapping import Mapping
from .normalizers import ADDED_AREA_CODE_RE, clean_text
from .schema import TargetSchema

# Cell statuses that count as "the converted value differs from (or doesn't simply copy) the original". Used by
# the "Rows with changes" filter. Everything except CELL_SAME and "" (empty cell) is a change.
CHANGED_CELLS = {CELL_FORMAT, CELL_DERIVED, CELL_DEFAULT, CELL_FIXED, CELL_REPAIRED, CELL_BLANKED, CELL_REJECT}
ATTENTION_CELLS = [CELL_BLANKED, CELL_REPAIRED]  # imported, but worth a look ("Problems only")

# Filter modes for the side-by-side view: key -> button label. The keys are understood by filter_rows() and
# status_counts(); the app shows "label (count)" and hides buttons with a zero count (except all/changed).
FILTERS = {
    "all": "All rows",
    "changed": "Rows with changes",
    "problems": "Problems only",
    "rejected": "Bad rows",
    "duplicate": "Duplicates",
    "imported": "Imported rows",
    "empty": "Empty rows",
}

# Plain-language meaning of each cell status code, used in the hover tooltip of converted cells.
CELL_LABELS = {
    CELL_SAME: "unchanged",
    CELL_FORMAT: "reformatted",
    CELL_DERIVED: "built from part of / several columns",
    CELL_DEFAULT: "default / fixed value",
    CELL_FIXED: "fixed (approved Claude suggestion)",
    CELL_REPAIRED: "repaired from a likely typo — check it",
    CELL_BLANKED: "invalid — left blank",
    CELL_REJECT: "invalid or missing — row rejected",
}

# Row status (audit "_status") -> (badge text, CSS modifier class for the .cmp-b badge). The badge text is also
# what the Excel export writes in its "Status" column.
STATUS_BADGE = {
    "imported": ("Imported", "ok"),
    "rejected": ("Bad row", "bad"),
    "duplicate": ("Duplicate", "dup"),
    "empty": ("Empty", "empty"),
}


@dataclass
class ColumnPlan:
    """How the source columns are laid out and labelled in the comparison (built by ``column_plan``).

    Attributes:
        source_columns: display order of the original file's columns -- columns used by the mapping first (in the
            order of the target fields they feed), then the unused ones in file order, so related columns on the
            left and right are roughly aligned.
        unused: source columns that feed no target field (greyed out, labelled "not imported").
        feeds: source column -> headers of the target fields it feeds (shown under the column header).
        origin: target field name -> short "where it comes from" text (shown under the converted header).
        each_field: name of the field that can make several rows from one file row (``Mapping.row_field``: the
            phone field using ``each_row``, or copied from one column whose cells may hold several numbers), or "".
        each_sources: that field's source columns; used to underline the exact source cell each extra row was
            made from.
    """

    source_columns: list[str]          # mapped columns first (in target-field order), then unused ones
    unused: set[str]
    feeds: dict[str, list[str]]        # source column -> target headers it feeds
    origin: dict[str, str]             # target field name -> short "from …" description
    each_field: str = ""               # field that gets one row per different number (Mapping.row_field), if any
    each_sources: tuple[str, ...] = ()


def column_plan(schema: TargetSchema, mapping: Mapping, source_columns: list[str]) -> ColumnPlan:
    """Build the ``ColumnPlan`` for an import from its schema, mapping and the source file's columns.

    Args:
        schema: the target schema snapshot of the import.
        mapping: the mapping the import ran with.
        source_columns: column names of ``source.parquet`` in file order.

    Returns:
        A ``ColumnPlan``. Pure function, no side effects.
    """
    ordered: list[str] = []
    feeds: dict[str, list[str]] = {c: [] for c in source_columns}
    origin: dict[str, str] = {}
    for f in schema.fields:
        fm = mapping.get(f.name)
        # Fields the import format always leaves blank have no source; their sources (if any) are not counted.
        if f.fixed_blank:
            origin[f.name] = "always blank (fixed)"
            continue
        # Mapped -> the mapping's own summary ("first non-empty of A, B", "fixed value 'x'", ...); unmapped ->
        # whether the schema fills a default or the field simply stays empty.
        origin[f.name] = fm.summary() if fm.transform != "none" else ("default" if f.default else "not mapped")
        for s in fm.sources:
            # Mapping sources missing from this file (e.g. a template made for another layout) are ignored.
            if s in feeds:
                feeds[s].append(f.header)
                if s not in ordered:
                    ordered.append(s)
    unused = {c for c in source_columns if not feeds[c]}
    ordered += [c for c in source_columns if c in unused]
    # The field that can make several rows from one file row (each_row, or a phone field copied from one column whose
    # cells may hold several numbers); remember it and its source columns.
    each = mapping.row_field(schema)
    return ColumnPlan(ordered, unused, feeds, origin, each.name if each else "",
                      tuple(mapping.get(each.name).sources) if each else ())


def changed_mask(audit: pd.DataFrame, schema: TargetSchema) -> pd.Series:
    """Boolean Series (aligned with ``audit``): True for rows where at least one cell status is in ``CHANGED_CELLS``.

    Only ``st.<field>`` columns of fields in the schema are considered; if there are none (e.g. an empty audit),
    every row is False.
    """
    st_cols = [f"st.{f.name}" for f in schema.fields if f"st.{f.name}" in audit.columns]
    if not st_cols:
        return pd.Series(False, index=audit.index)
    return audit[st_cols].isin(CHANGED_CELLS).any(axis=1)


def filter_rows(audit: pd.DataFrame, source: pd.DataFrame, schema: TargetSchema, mode: str = "all",
                search: str = "") -> list[int]:
    """Return the row positions to show for a filter mode and an optional search text.

    Args:
        audit: the import's audit table.
        source: source rows aligned with ``audit`` (see module docstring).
        schema: target schema (to know which status columns exist).
        mode: a ``FILTERS`` key:
            * "all"      -- every row;
            * "changed"  -- rows with any changed cell, plus all bad and duplicate rows;
            * "problems" -- bad and duplicate rows, plus imported rows with a cell that was left blank as invalid
              or repaired from a likely typo (``ATTENTION_CELLS``): things a person should look at;
            * "rejected" / "duplicate" / "imported" / "empty" -- rows with exactly that row status.
            Unknown modes behave like "all".
        search: case-insensitive substring searched in every original cell and every converted cell; the text
            (with an optional leading "#") also matches a file row number exactly, so "#120" finds row 120.

    Returns:
        Index labels of the matching audit rows, in file order. With the default ``RangeIndex`` these are
        positions, which is what ``build_compare_html`` expects (it uses ``iloc``). The caller pages this list.
    """
    mask = pd.Series(True, index=audit.index)
    status = audit["_status"]
    if mode == "changed":
        mask &= changed_mask(audit, schema) | status.isin(["rejected", "duplicate"])
    elif mode == "problems":
        attention = audit[[c for c in audit.columns if c.startswith("st.")]].isin(ATTENTION_CELLS).any(axis=1)
        mask &= status.isin(["rejected", "duplicate"]) | attention
    elif mode in ("rejected", "duplicate", "imported", "empty"):
        mask &= status == mode
    if search.strip():
        q = search.strip().lower()
        # Plain substring search (regex=False) so characters like "(" or "+" in phone numbers need no escaping.
        hay = source.astype(str).apply(lambda col: col.str.lower().str.contains(q, regex=False, na=False))
        out_cols = [c for c in audit.columns if c.startswith("out.")]
        hay_out = audit[out_cols].astype(str).apply(lambda col: col.str.lower().str.contains(q, regex=False,
                                                                                           na=False))
        row_hit = audit["_row"].astype(str) == q.lstrip("#")
        mask &= hay.any(axis=1) | hay_out.any(axis=1) | row_hit
    return list(audit.index[mask])


def status_counts(audit: pd.DataFrame, schema: TargetSchema) -> dict[str, int]:
    """Number of rows each ``FILTERS`` mode would show (ignoring search), for the "label (count)" buttons.

    The "changed" and "problems" definitions must stay identical to ``filter_rows`` so a button's count matches
    the rows it shows. Note: ``blanked`` below covers both left-blank and repaired cells (``ATTENTION_CELLS``).
    """
    vc = audit["_status"].value_counts()
    blanked = audit[[c for c in audit.columns if c.startswith("st.")]].isin(ATTENTION_CELLS).any(axis=1)
    return {
        "all": len(audit),
        "changed": int((changed_mask(audit, schema) | audit["_status"].isin(["rejected", "duplicate"])).sum()),
        "problems": int((audit["_status"].isin(["rejected", "duplicate"]) | blanked).sum()),
        "rejected": int(vc.get("rejected", 0)),
        "duplicate": int(vc.get("duplicate", 0)),
        "imported": int(vc.get("imported", 0)),
        "empty": int(vc.get("empty", 0)),
    }


# ---------------------------------------------------------------------------
# HTML table
# ---------------------------------------------------------------------------

# Stylesheet emitted in front of every rendered table (st.html renders it in the page, so it is self-contained).
# Layout:
#   .cmpw            scroll container (max 640px high, scrolls both ways) around the table
#   table.cmp        the comparison table; cells don't wrap and long values are cut with an ellipsis (the full
#                    value is in the cell's title tooltip)
#   thead tr.g       first header row: group labels "Original file" (th.gsrc, grey) | gap | "Converted" (th.gout,
#                    blue); sticky at top:0
#   thead tr.h       second header row: one header per column with a .sub line saying where it goes / comes from;
#                    sticky at top:30px, just under tr.g
#   .gap             narrow grey separator column between the original and converted halves
#   .meta            the "Row" column; sticky on the left so the file row number stays visible while scrolling
#   .unused          source columns not used by the mapping (greyed)
#   .sub / .was / .why  small secondary lines: header detail / original value (struck through) / problem reason
#   .cmp-b.<kind>    row status badge: ok (green, imported), bad (red, rejected), dup (grey), empty (light grey)
# Row classes (tr.r-<status>): rejected rows get a pink tint on the original side and diagonal hatching on the
# converted cells that weren't themselves the cause; duplicate and empty rows are shown in grey text.
# td.o.each underlines the source cell a phone row was made from (one row per phone number); td.o.eachp underlines
# only the <u> part of a cell holding several numbers.
# Cell status classes on converted cells are "c-" + the engine.CELL_* code, so they match automatically:
#   c-fmt reformatted (blue)   c-drv built/derived (purple)   c-def default (grey, italic)   c-fix Claude fix (green)
#   c-blk invalid, left blank (yellow)   c-rpr repaired typo (orange, bold)   c-rej rejected cause (red, bold)
# The swatch colours in _LEGEND and XLSX_FILL below repeat these hex values; keep them in sync (DARK_CSS has the
# dark-mode versions).
CSS = """
<style>
.cmpw{max-height:640px;overflow:auto;border:1px solid #d9dde3;border-radius:8px;background:#fff}
table.cmp{border-collapse:separate;border-spacing:0;font-size:13px;line-height:1.35;width:max-content;min-width:100%}
table.cmp th,table.cmp td{padding:5px 9px;border-bottom:1px solid #eceef1;border-right:1px solid #f1f2f4;
  vertical-align:top;white-space:nowrap;max-width:260px;overflow:hidden;text-overflow:ellipsis;color:#1f2328}
table.cmp thead th{position:sticky;background:#f6f7f9;z-index:2;text-align:left;font-weight:600}
table.cmp thead tr.g th{top:0;height:30px;font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:#57606a}
table.cmp thead tr.h th{top:30px;box-shadow:0 1px 0 #d9dde3}
table.cmp th.gsrc{background:#eef1f5}
table.cmp th.gout{background:#eaf2ff;color:#1d4ed8}
table.cmp td.gap,table.cmp th.gap{background:#f6f7f9;min-width:10px;max-width:10px;padding:0;
  border-left:2px solid #c9ced6;border-right:2px solid #c9ced6}
table.cmp .meta{position:sticky;left:0;z-index:1;background:#fff}
table.cmp thead .meta{z-index:3;background:#f6f7f9}
table.cmp .sub{display:block;font-weight:400;font-size:11px;color:#6e7781;margin-top:2px}
table.cmp th .sub{max-width:240px;overflow:hidden;text-overflow:ellipsis}
table.cmp td .was{display:block;font-size:11px;color:#6e7781;text-decoration:line-through;
  text-decoration-color:#9aa1a9}
table.cmp td .why{display:block;font-size:11px;color:#b42318;white-space:normal;min-width:180px;max-width:320px}
table.cmp th.unused,table.cmp td.unused{color:#9aa1a9;background:#fafafb}
.cmp-b{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;font-weight:600}
.cmp-b.ok{background:#e7f6ec;color:#116329}.cmp-b.bad{background:#fde8e8;color:#b42318}
.cmp-b.dup{background:#eef0f3;color:#57606a}.cmp-b.empty{background:#f6f7f9;color:#9aa1a9}
tr.r-rejected td.o{background:#fff6f6}
table.cmp td.o.each{font-weight:600;text-decoration:underline;text-decoration-color:#2563eb;text-underline-offset:3px}
table.cmp td.o.eachp u{font-weight:600;text-decoration-color:#2563eb;text-underline-offset:3px}
tr.r-rejected td.n:not(.c-rej){color:#9aa1a9;background-image:repeating-linear-gradient(135deg,transparent 0 6px,
  rgba(180,35,24,.05) 6px 12px)}
tr.r-duplicate td{color:#8b949e}
tr.r-empty td{color:#b6bcc4}
td.c-fmt{background:#e8f1ff}
td.c-drv{background:#f2ecff}
td.c-def{background:#f3f4f6;font-style:italic}
td.c-fix{background:#e3f6e9}
td.c-blk{background:#fff4d6}
td.c-rpr{background:#ffe2c6;font-weight:600}
td.c-rej{background:#fde1e1;color:#b42318;font-weight:600}
td .blank{color:#9aa1a9;font-style:italic;font-weight:400}
.cmp-legend{display:flex;flex-wrap:wrap;gap:10px;font-size:12px;color:#57606a;margin:4px 0 8px}
.cmp-legend span{display:inline-flex;align-items:center;gap:5px}
.cmp-legend i{display:inline-block;width:14px;height:14px;border-radius:3px;border:1px solid #d9dde3}
</style>
"""


# Dark-mode colours for the same table, layered on top of CSS when the user has dark mode on (see app.py's light/dark
# toggle). Every light rule above that sets a colour has a dark counterpart here; the meaning of each colour stays the
# same (blue = reformatted, orange = repaired, red = rejected, ...), only darker tints with light text.
DARK_CSS = """
<style>
.cmpw{border-color:#3a3f4b;background:#0e1117}
table.cmp th,table.cmp td{border-bottom-color:#262a33;border-right-color:#1f232b;color:#e6edf3}
table.cmp thead th{background:#1a1d24}
table.cmp thead tr.g th{color:#a3acb9}
table.cmp thead tr.h th{box-shadow:0 1px 0 #3a3f4b}
table.cmp th.gsrc{background:#1f232b}
table.cmp th.gout{background:#172a46;color:#93c5fd}
table.cmp td.gap,table.cmp th.gap{background:#1a1d24;border-left-color:#3a3f4b;border-right-color:#3a3f4b}
table.cmp .meta{background:#0e1117}
table.cmp thead .meta{background:#1a1d24}
table.cmp .sub,table.cmp td .was{color:#9aa4af}
table.cmp td .was{text-decoration-color:#6b7280}
table.cmp td .why{color:#ff9b9b}
table.cmp th.unused,table.cmp td.unused{color:#6b7280;background:#13161c}
.cmp-b.ok{background:#12321f;color:#7ee2a8}.cmp-b.bad{background:#3d1717;color:#ff9b9b}
.cmp-b.dup{background:#262a33;color:#a3acb9}.cmp-b.empty{background:#1a1d24;color:#6b7280}
tr.r-rejected td.o{background:#2a1515}
table.cmp td.o.each{text-decoration-color:#60a5fa}
table.cmp td.o.eachp u{text-decoration-color:#60a5fa}
tr.r-rejected td.n:not(.c-rej){color:#6b7280;background-image:repeating-linear-gradient(135deg,transparent 0 6px,
  rgba(255,120,120,.08) 6px 12px)}
tr.r-duplicate td{color:#7d8590}
tr.r-empty td{color:#5c6370}
td.c-fmt{background:#16304f}
td.c-drv{background:#2a2147}
td.c-def{background:#262a33}
td.c-fix{background:#15331f}
td.c-blk{background:#3a2f0b}
td.c-rpr{background:#4a2a0f}
td.c-rej{background:#4a1717;color:#ff9b9b}
td .blank{color:#6b7280}
.cmp-legend{color:#a3acb9}
.cmp-legend i{border-color:#3a3f4b}
</style>
"""
# Legend swatches per mode: (CSS class, light colour, dark colour, label). Keep in sync with CSS / DARK_CSS.
_LEGEND = [("c-fmt", "#e8f1ff", "#16304f", "reformatted"),
           ("c-drv", "#f2ecff", "#2a2147", "built from part of / several columns"),
           ("c-def", "#f3f4f6", "#262a33", "default / fixed value"),
           ("c-fix", "#e3f6e9", "#15331f", "fixed by approved Claude suggestion"),
           ("c-rpr", "#ffe2c6", "#4a2a0f", "repaired from a likely typo — check it"),
           ("c-blk", "#fff4d6", "#3a2f0b", "invalid → left blank"),
           ("c-rej", "#fde1e1", "#4a1717", "invalid / missing → row rejected")]


def legend_html(each_row: bool = False, dark: bool = False) -> str:
    """Return the colour legend shown above the table: one swatch + label per changed-cell status.

    Args:
        each_row: True when rows show the source cell their number came from (several phone columns, or a file
            row with several numbers in one cell); adds an explanation of the underlined source cell and of the
            "N +" row labels used for the extra rows.
        dark: use the dark-mode swatch colours (matching ``DARK_CSS``).

    The first tuple element of ``_LEGEND`` (CSS class) is informational only; the swatch uses the inline colour,
    which must match the corresponding ``td.c-*`` rule in ``CSS`` / ``DARK_CSS``. Labels are HTML-escaped.
    """
    spans = "".join(f'<span><i style="background:{dark_color if dark else light_color}"></i>{html.escape(label)}</span>'
                    for _, light_color, dark_color, label in _LEGEND)
    if each_row:
        spans += ("<span><u><b>underlined</b></u> = the number that row was made from · <b>N +</b> = another row "
                  "from file row N (one row per different number)</span>")
    return f'<div class="cmp-legend">{spans}<span>· hover any cell for details</span></div>'


def _e(value) -> str:
    """HTML-escape any value for safe insertion into the table (``None`` -> "").

    Every value from the uploaded file, the mapping or the schema goes through this, because file contents are
    untrusted and the HTML is rendered directly in the page. Escaping also covers quotes, so it is safe inside
    ``title="..."`` attributes.
    """
    return html.escape("" if value is None else str(value))


def build_compare_html(source: pd.DataFrame, audit: pd.DataFrame, rows: list[int], schema: TargetSchema,
                       plan: ColumnPlan, file_label: str = "Original file", target_label: str = "Converted",
                       show_was: bool = True, dark: bool = False) -> str:
    """Render the side-by-side table for the given rows as one HTML string (CSS + legend + table).

    Args:
        source: source rows aligned with ``audit``.
        audit: the import's audit table.
        rows: positions of the rows to render -- normally one page of ``filter_rows`` output; the app pages the
            result (50-250 rows) to keep the HTML small.
        schema: target schema snapshot of the import (defines the converted columns, in schema order).
        plan: the ``ColumnPlan`` (source column order, unused columns, header sub-labels).
        file_label / target_label: texts of the two group headers.
        show_was: also show the original value (struck through) under converted cells whose value was changed
            (reformatted, Claude-fixed, repaired, or blanked) -- the "Show original under changed values" toggle.
        dark: render with the dark-mode colours (``DARK_CSS``) when the user has dark mode on.

    Table structure (columns, left to right):
        Row (sticky; file row number, "N +" for extra rows from one file row) | Status (badge + bad-row reason) |
        one column per source column (``td.o``) | gap | one column per target field (``td.n``, coloured by cell
        status; "*" in the header marks required fields).
    Every cell has a ``title`` tooltip with the full value and, for converted cells, the status meaning, the
    original value and the problem reason.

    Returns:
        HTML to pass to ``st.html``. No side effects.
    """
    cols = plan.source_columns
    fields = schema.fields
    # File rows that became several rows (their source position appears on an "extra" row). With several phone
    # columns every row underlines the cell its number came from; with one phone column only these rows do, since
    # elsewhere the phone cell is simply the number.
    expanded: set = set()
    if plan.each_field and {"_extra", "_src"} <= set(audit.columns):
        expanded = set(audit.loc[audit["_extra"].astype(bool), "_src"].tolist())
    several_columns = len(plan.each_sources) > 1
    parts = [CSS + (DARK_CSS if dark else ""),
             legend_html(bool(plan.each_field) and (several_columns or bool(expanded)), dark),
             '<div class="cmpw"><table class="cmp"><thead>']
    # Header row 1 (tr.g): group labels spanning the original and converted halves; the 2 meta columns are blank.
    parts.append(
        f'<tr class="g"><th class="meta" colspan="2"></th>'
        f'<th class="gsrc" colspan="{len(cols)}">{_e(file_label)}</th><th class="gap"></th>'
        f'<th class="gout" colspan="{len(fields)}">{_e(target_label)}</th></tr>'
    )
    # Header row 2 (tr.h): one header per column. Source headers say which target fields they feed ("→ ...") or
    # "not imported"; converted headers say where the value comes from ("← ...").
    head = ['<tr class="h"><th class="meta">Row</th><th>Status</th>']
    for c in cols:
        if c in plan.unused:
            head.append(f'<th class="unused" title="Not used in the converted file">{_e(c)}'
                        f'<span class="sub">not imported</span></th>')
        else:
            to = ", ".join(plan.feeds[c])
            head.append(f'<th title="Used for: {_e(to)}">{_e(c)}<span class="sub">→ {_e(to)}</span></th>')
    head.append('<th class="gap"></th>')
    for f in fields:
        src = plan.origin.get(f.name, "")
        head.append(f'<th title="{_e(f.header)} ← {_e(src)}">{_e(f.header)}{"*" if f.required else ""}'
                    f'<span class="sub">← {_e(src)}</span></th>')
    head.append("</tr></thead><tbody>")
    parts.append("".join(head))

    # Only the requested rows are converted to dicts (fast even for very large imports).
    src_records = source.iloc[rows].to_dict("records") if rows else []
    aud_records = audit.iloc[rows].to_dict("records") if rows else []
    for srow, arow in zip(src_records, aud_records):
        status = arow["_status"]
        label, cls = STATUS_BADGE.get(status, (status, "empty"))
        # Per-field problem reasons for this row: {field_name: reason}.
        issues = json.loads(arow["_issues"]) if arow.get("_issues") else {}
        row_label = _e(arow["_row"])
        # Extra rows (a file row's 2nd, 3rd... phone number) repeat the file row number with a "+" suffix.
        if arow.get("_extra"):
            row_label = (f'<span title="Another number from the same file row — one row per different number">'
                         f'{row_label} +</span>')
        # Row class r-<status> drives the row-level styling (pink/hatched for rejected, grey for duplicate/empty).
        cells = [f'<tr class="r-{status}"><td class="meta">{row_label}</td>'
                 f'<td><span class="cmp-b {cls}">{label}</span>'
                 + (f'<span class="why">{_e(arow["_reason"])}</span>' if arow["_reason"] else "") + "</td>"]
        # --- original (left) half -------------------------------------------------------------------------
        # For one row per phone number, the raw value of that field is the number this row was made from (a whole
        # cell, or one number's piece of a cell holding several); find and underline the first source cell holding
        # it (only one cell is marked even if the value repeats). For a piece, only that part of the cell is
        # underlined, so the rows made from one cell can be told apart.
        underline = plan.each_field and (several_columns or arow.get("_src") in expanded)
        used_value = str(arow.get(f"raw.{plan.each_field}", "")) if underline else ""
        if used_value and not any(used_value in clean_text(srow.get(c, "")) for c in plan.each_sources):
            # 'Phone + area code column' put the row's area code in front of a 7-digit number: "(516) 555-0177"
            # was "555-0177" in the file.
            used_value = ADDED_AREA_CODE_RE.sub("", used_value, count=1)
        marked = False
        for c in cols:
            v = srow.get(c, "")
            klass = "o unused" if c in plan.unused else "o"
            title = f"{c}: {v}"
            body = _e(v)
            if used_value and not marked and c in plan.each_sources and used_value in clean_text(v):
                s = "" if v is None else str(v)
                at = s.find(used_value)
                if s.strip() != used_value and at >= 0:
                    klass += " eachp"  # one number of a cell holding several: underline just that number
                    body = f"{_e(s[:at])}<u>{_e(used_value)}</u>{_e(s[at + len(used_value):])}"
                else:
                    klass += " each"  # the whole cell is this row's number (or its exact text can't be located)
                title += " — this row's number"
                marked = True
            cells.append(f'<td class="{klass}" title="{_e(title)}">{body}</td>')
        cells.append('<td class="gap"></td>')
        # --- converted (right) half -----------------------------------------------------------------------
        for f in fields:
            out = arow.get(f"out.{f.name}", "")
            raw = arow.get(f"raw.{f.name}", "")
            st = arow.get(f"st.{f.name}", "")
            reason = issues.get(f.name, "")
            # Tooltip: "<header>: <status meaning> — original: <raw> — <reason>".
            tip = f"{f.header}: {CELL_LABELS.get(st, '')}"
            if raw and raw != out:
                tip += f" — original: {raw}"
            if reason:
                tip += f" — {reason}"
            # CSS class "c-<status code>" colours the cell; unchanged and empty cells get no colour class.
            klass = f' class="n c-{st}"' if st and st != CELL_SAME else ' class="n"'
            # Rejected cell: there is no output value, so show the offending original value (or "missing" for an
            # empty required field). Blanked cell: show "(left blank)". Both add the reason underneath, and a
            # blanked cell can also show the value that was dropped.
            if st in (CELL_REJECT, CELL_BLANKED):
                body = (_e(raw) if raw else '<span class="blank">missing</span>') if st == CELL_REJECT else \
                    '<span class="blank">(left blank)</span>'
                body += f'<span class="why">{_e(reason)}</span>'
                if st == CELL_BLANKED and show_was and raw:
                    body = f'{body}<span class="was">{_e(raw)}</span>'
            else:
                # Normal cell: the converted value; for value-changing statuses optionally the struck-through
                # original underneath. Derived/default cells don't show it -- their "original" isn't one cell.
                body = _e(out)
                if show_was and st in (CELL_FORMAT, CELL_FIXED, CELL_REPAIRED) and raw and raw != out:
                    body += f'<span class="was">{_e(raw)}</span>'
            cells.append(f'<td{klass} title="{_e(tip)}">{body}</td>')
        cells.append("</tr>")
        parts.append("".join(cells))
    if not rows:
        # One full-width message row; +3 covers the Row, Status and gap columns.
        parts.append(f'<tr><td colspan="{len(cols) + len(fields) + 3}" style="padding:24px;color:#6e7781">'
                     "No rows match this filter.</td></tr>")
    parts.append("</tbody></table></div>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# What changed — per field
# ---------------------------------------------------------------------------

def field_change_summary(audit: pd.DataFrame, schema: TargetSchema, mapping: Mapping) -> pd.DataFrame:
    """Per target field, count how each cell was produced across the whole import ("What changed" tab).

    Empty rows are excluded. One output row per schema field that has a status column, with columns:
    Field, From (mapping summary), one count per cell status (Unchanged, Reformatted, Built/split, Default,
    Claude fix, Repaired typo, Left blank (invalid), Rejected rows), Empty (cells with no value) and Examples --
    up to three distinct "original → converted" pairs taken from repaired, reformatted or Claude-fixed cells, so
    the user can see what a reformat actually did.

    Returns:
        A DataFrame ready for ``st.dataframe``. No side effects.
    """
    live = audit[audit["_status"] != "empty"]
    rows = []
    for f in schema.fields:
        st = live.get(f"st.{f.name}")
        if st is None:
            continue
        vc = st.value_counts()
        fmt_mask = st.isin([CELL_REPAIRED, CELL_FORMAT, CELL_FIXED])
        examples = []
        if fmt_mask.any():
            sub = live.loc[fmt_mask, [f"raw.{f.name}", f"out.{f.name}"]].drop_duplicates().head(3)
            examples = [f"{a} → {b}" for a, b in sub.itertuples(index=False)]
        rows.append({
            "Field": f.header,
            "From": mapping.get(f.name).summary(),
            "Unchanged": int(vc.get(CELL_SAME, 0)),
            "Reformatted": int(vc.get(CELL_FORMAT, 0)),
            "Built/split": int(vc.get(CELL_DERIVED, 0)),
            "Default": int(vc.get(CELL_DEFAULT, 0)),
            "Claude fix": int(vc.get(CELL_FIXED, 0)),
            "Repaired typo": int(vc.get(CELL_REPAIRED, 0)),
            "Left blank (invalid)": int(vc.get(CELL_BLANKED, 0)),
            "Rejected rows": int(vc.get(CELL_REJECT, 0)),
            "Empty": int((st == "").sum()),
            "Examples": "   ·   ".join(examples),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Excel export of the side-by-side view
# ---------------------------------------------------------------------------

# Background fill per cell status in the Excel export -- the same colours as the HTML td.c-* classes.
# CELL_SAME and empty cells are not listed and get the plain text format.
XLSX_FILL = {CELL_FORMAT: "#E8F1FF", CELL_DERIVED: "#F2ECFF", CELL_DEFAULT: "#F3F4F6", CELL_FIXED: "#E3F6E9",
             CELL_REPAIRED: "#FFE2C6",
             CELL_BLANKED: "#FFF4D6", CELL_REJECT: "#FDE1E1"}


def comparison_xlsx(source: pd.DataFrame, audit: pd.DataFrame, schema: TargetSchema, plan: ColumnPlan,
                    title: str) -> bytes:
    """Build the side-by-side view of *all* rows (no filter, no paging) as an .xlsx workbook in memory.

    Args:
        source: source rows aligned with ``audit``.
        audit: the import's audit table.
        schema: target schema snapshot (converted columns, in schema order).
        plan: the ``ColumnPlan`` (same column order and labels as the HTML view).
        title: text for cell A1 (the app passes "Import #<id> — <file> → <format>").

    Returns:
        The workbook as bytes (the app offers it as a download; nothing is saved in the import folder).

    Sheet "Side by side" layout (0-based rows/columns):
        row 0  title
        row 1  colour key (text)
        row 2  group labels: "ORIGINAL FILE" over the source columns, "CONVERTED" over the target columns
               (merged when they span more than one column -- merge_range needs at least two cells)
        row 3  headers: File row | Status | Reason | Notes | source columns ("→ fields" or "(not imported)") |
               gap | target fields ("← origin"); wrapped text, 45pt high
        row 4+ one row per audit row, in file order
        Columns 0-3 hold the row number, row status, bad-row reason and per-field problem notes; the original
        values start at column 4, followed by a narrow grey gap column and the converted values. Panes are
        frozen below the headers and right of the "File row" column.

    Converted cells get the ``XLSX_FILL`` colour of their status; for a rejected cell the original value (or
    "(missing)") is written, because the converted value is empty. All file values are written as text.
    """
    buf = io.BytesIO()
    import xlsxwriter

    # constant_memory: each finished row is flushed to a temporary file, so very large imports don't hold the whole
    # sheet in memory (this requires writing row by row, top to bottom -- as done below).
    # strings_to_numbers/formulas/urls = False: keep every value exactly as text -- no leading zeros lost from ZIP
    # codes or phone numbers, no "=..." cell from the file ever becoming a live formula, no auto-hyperlinks.
    wb = xlsxwriter.Workbook(buf, {"constant_memory": True, "strings_to_numbers": False,
                                   "strings_to_formulas": False, "strings_to_urls": False})
    ws = wb.add_worksheet("Side by side")
    f_title = wb.add_format({"bold": True, "font_size": 12})
    f_grp_src = wb.add_format({"bold": True, "bg_color": "#EEF1F5", "border": 1})
    f_grp_out = wb.add_format({"bold": True, "bg_color": "#EAF2FF", "font_color": "#1D4ED8", "border": 1})
    f_head = wb.add_format({"bold": True, "bg_color": "#F6F7F9", "border": 1, "text_wrap": True, "valign": "top"})
    # num_format "@" = Excel's Text format, so values typed into the cells later aren't reinterpreted either.
    f_text = wb.add_format({"num_format": "@"})
    f_unused = wb.add_format({"num_format": "@", "font_color": "#9AA1A9"})
    f_gap = wb.add_format({"bg_color": "#C9CED6"})
    # Font colour of the "Status" column per row status (mirrors the HTML badges).
    f_status = {"imported": wb.add_format({"font_color": "#116329"}),
                "rejected": wb.add_format({"font_color": "#B42318", "bold": True}),
                "duplicate": wb.add_format({"font_color": "#57606A"}),
                "empty": wb.add_format({"font_color": "#9AA1A9"})}
    f_cell = {k: wb.add_format({"num_format": "@", "bg_color": v}) for k, v in XLSX_FILL.items()}

    # Column positions: 4 fixed columns (File row, Status, Reason, Notes), then the source block, one gap column,
    # then the converted block.
    cols, fields = plan.source_columns, schema.fields
    c_src0 = 4
    c_gap = c_src0 + len(cols)
    c_out0 = c_gap + 1
    ws.write(0, 0, title, f_title)
    # Colour key. It names the most common colours; the grey (default) and orange (repaired) fills are explained
    # by the HTML legend only.
    ws.write(1, 0, "Blue = reformatted · purple = built from other columns · green = Claude fix · "
                   "yellow = invalid, left blank · red = invalid/missing, row rejected")
    # Group labels: merge across the block when it is wider than one column, otherwise write a single cell.
    ws.merge_range(2, c_src0, 2, c_gap - 1, "ORIGINAL FILE", f_grp_src) if len(cols) > 1 else \
        ws.write(2, c_src0, "ORIGINAL FILE", f_grp_src)
    ws.merge_range(2, c_out0, 2, c_out0 + len(fields) - 1, "CONVERTED", f_grp_out) if len(fields) > 1 else \
        ws.write(2, c_out0, "CONVERTED", f_grp_out)
    headers = ["File row", "Status", "Reason", "Notes"]
    for i, h in enumerate(headers):
        ws.write(3, i, h, f_head)
    # Source headers: name + second line saying which target fields the column feeds, or "(not imported)".
    for j, c in enumerate(cols):
        ws.write(3, c_src0 + j, c + ("\n(not imported)" if c in plan.unused else "\n→ " + ", ".join(plan.feeds[c])),
                 f_head)
    ws.write(3, c_gap, "", f_gap)
    # Target headers: header + second line saying where the value comes from.
    for j, f in enumerate(fields):
        ws.write(3, c_out0 + j, f"{f.header}\n← {plan.origin.get(f.name, '')}", f_head)
    ws.set_row(3, 45)
    # Keep the 4 title/header rows and the "File row" column visible while scrolling.
    ws.freeze_panes(4, 1)
    # Column widths: row number, status, reason + notes (wide), source block, gap (narrow), converted block.
    ws.set_column(0, 0, 8)
    ws.set_column(1, 1, 11)
    ws.set_column(2, 3, 34)
    ws.set_column(c_src0, c_gap - 1, 18)
    ws.set_column(c_gap, c_gap, 2)
    ws.set_column(c_out0, c_out0 + len(fields) - 1, 18)

    src_records = source.to_dict("records")
    aud_records = audit.to_dict("records")
    r = 4
    for srow, arow in zip(src_records, aud_records):
        status = arow["_status"]
        issues = json.loads(arow["_issues"]) if arow.get("_issues") else {}
        # "Notes" column: every per-field problem of the row as "<field header>: <reason>; ...".
        notes = "; ".join(f"{schema.field(k).header}: {v}" for k, v in issues.items() if k in schema.field_names)
        ws.write_number(r, 0, int(arow["_row"]))
        ws.write(r, 1, STATUS_BADGE.get(status, (status,))[0], f_status.get(status))
        ws.write_string(r, 2, arow["_reason"] or "")
        ws.write_string(r, 3, notes)
        # write_string (not write) so nothing is ever converted to a number, formula or URL.
        for j, c in enumerate(cols):
            ws.write_string(r, c_src0 + j, str(srow.get(c, "") or ""), f_unused if c in plan.unused else f_text)
        ws.write(r, c_gap, "", f_gap)
        for j, f in enumerate(fields):
            st = arow.get(f"st.{f.name}", "")
            val = arow.get(f"out.{f.name}", "") or ""
            if st == CELL_REJECT:
                val = arow.get(f"raw.{f.name}", "") or "(missing)"
            ws.write_string(r, c_out0 + j, str(val), f_cell.get(st, f_text))
        r += 1
    wb.close()
    return buf.getvalue()
