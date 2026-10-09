"""Side-by-side comparison of an import: original file vs converted file, cell by cell.

Built from two tables stored with every completed import:
  source.parquet  – the uploaded file exactly as read (text)
  audit.parquet   – per row: file row number, status (imported/rejected/duplicate/empty), reason,
                    and per target field: converted value, value before normalizing, cell status
"""

from __future__ import annotations

import html
import io
import json
from dataclasses import dataclass

import pandas as pd

from .engine import (CELL_BLANKED, CELL_DEFAULT, CELL_DERIVED, CELL_FIXED, CELL_FORMAT, CELL_REJECT, CELL_SAME)
from .mapping import Mapping
from .schema import TargetSchema

CHANGED_CELLS = {CELL_FORMAT, CELL_DERIVED, CELL_DEFAULT, CELL_FIXED, CELL_BLANKED, CELL_REJECT}

FILTERS = {
    "all": "All rows",
    "changed": "Rows with changes",
    "problems": "Problems only",
    "rejected": "Bad rows",
    "duplicate": "Duplicates",
    "imported": "Imported rows",
    "empty": "Empty rows",
}

CELL_LABELS = {
    CELL_SAME: "unchanged",
    CELL_FORMAT: "reformatted",
    CELL_DERIVED: "built from part of / several columns",
    CELL_DEFAULT: "default / fixed value",
    CELL_FIXED: "fixed (approved Claude suggestion)",
    CELL_BLANKED: "invalid — left blank",
    CELL_REJECT: "invalid or missing — row rejected",
}

STATUS_BADGE = {
    "imported": ("Imported", "ok"),
    "rejected": ("Bad row", "bad"),
    "duplicate": ("Duplicate", "dup"),
    "empty": ("Empty", "empty"),
}


@dataclass
class ColumnPlan:
    source_columns: list[str]          # mapped columns first (in target-field order), then unused ones
    unused: set[str]
    feeds: dict[str, list[str]]        # source column -> target headers it feeds
    origin: dict[str, str]             # target field name -> short "from …" description


def column_plan(schema: TargetSchema, mapping: Mapping, source_columns: list[str]) -> ColumnPlan:
    ordered: list[str] = []
    feeds: dict[str, list[str]] = {c: [] for c in source_columns}
    origin: dict[str, str] = {}
    for f in schema.fields:
        fm = mapping.get(f.name)
        if f.fixed_blank:
            origin[f.name] = "always blank (fixed)"
            continue
        origin[f.name] = fm.summary() if fm.transform != "none" else ("default" if f.default else "not mapped")
        for s in fm.sources:
            if s in feeds:
                feeds[s].append(f.header)
                if s not in ordered:
                    ordered.append(s)
    unused = {c for c in source_columns if not feeds[c]}
    ordered += [c for c in source_columns if c in unused]
    return ColumnPlan(ordered, unused, feeds, origin)


def changed_mask(audit: pd.DataFrame, schema: TargetSchema) -> pd.Series:
    st_cols = [f"st.{f.name}" for f in schema.fields if f"st.{f.name}" in audit.columns]
    if not st_cols:
        return pd.Series(False, index=audit.index)
    return audit[st_cols].isin(CHANGED_CELLS).any(axis=1)


def filter_rows(audit: pd.DataFrame, source: pd.DataFrame, schema: TargetSchema, mode: str = "all",
                search: str = "") -> list[int]:
    mask = pd.Series(True, index=audit.index)
    status = audit["_status"]
    if mode == "changed":
        mask &= changed_mask(audit, schema) | status.isin(["rejected", "duplicate"])
    elif mode == "problems":
        blanked = audit[[c for c in audit.columns if c.startswith("st.")]].eq(CELL_BLANKED).any(axis=1)
        mask &= status.isin(["rejected", "duplicate"]) | blanked
    elif mode in ("rejected", "duplicate", "imported", "empty"):
        mask &= status == mode
    if search.strip():
        q = search.strip().lower()
        hay = source.astype(str).apply(lambda col: col.str.lower().str.contains(q, regex=False, na=False))
        out_cols = [c for c in audit.columns if c.startswith("out.")]
        hay_out = audit[out_cols].astype(str).apply(lambda col: col.str.lower().str.contains(q, regex=False,
                                                                                           na=False))
        row_hit = audit["_row"].astype(str) == q.lstrip("#")
        mask &= hay.any(axis=1) | hay_out.any(axis=1) | row_hit
    return list(audit.index[mask])


def status_counts(audit: pd.DataFrame, schema: TargetSchema) -> dict[str, int]:
    vc = audit["_status"].value_counts()
    blanked = audit[[c for c in audit.columns if c.startswith("st.")]].eq(CELL_BLANKED).any(axis=1)
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
tr.r-rejected td.n:not(.c-rej){color:#9aa1a9;background-image:repeating-linear-gradient(135deg,transparent 0 6px,
  rgba(180,35,24,.05) 6px 12px)}
tr.r-duplicate td{color:#8b949e}
tr.r-empty td{color:#b6bcc4}
td.c-fmt{background:#e8f1ff}
td.c-drv{background:#f2ecff}
td.c-def{background:#f3f4f6;font-style:italic}
td.c-fix{background:#e3f6e9}
td.c-blk{background:#fff4d6}
td.c-rej{background:#fde1e1;color:#b42318;font-weight:600}
td .blank{color:#9aa1a9;font-style:italic;font-weight:400}
.cmp-legend{display:flex;flex-wrap:wrap;gap:10px;font-size:12px;color:#57606a;margin:4px 0 8px}
.cmp-legend span{display:inline-flex;align-items:center;gap:5px}
.cmp-legend i{display:inline-block;width:14px;height:14px;border-radius:3px;border:1px solid #d9dde3}
</style>
"""


def legend_html() -> str:
    items = [("c-fmt", "#e8f1ff", "reformatted"), ("c-drv", "#f2ecff", "built from part of / several columns"),
             ("c-def", "#f3f4f6", "default / fixed value"), ("c-fix", "#e3f6e9", "fixed by approved Claude suggestion"),
             ("c-blk", "#fff4d6", "invalid → left blank"), ("c-rej", "#fde1e1", "invalid / missing → row rejected")]
    spans = "".join(f'<span><i style="background:{color}"></i>{html.escape(label)}</span>'
                    for _, color, label in items)
    return f'<div class="cmp-legend">{spans}<span>· hover any cell for details</span></div>'


def _e(value) -> str:
    return html.escape("" if value is None else str(value))


def build_compare_html(source: pd.DataFrame, audit: pd.DataFrame, rows: list[int], schema: TargetSchema,
                       plan: ColumnPlan, file_label: str = "Original file", target_label: str = "Converted",
                       show_was: bool = True) -> str:
    cols = plan.source_columns
    fields = schema.fields
    parts = [CSS, legend_html(), '<div class="cmpw"><table class="cmp"><thead>']
    parts.append(
        f'<tr class="g"><th class="meta" colspan="2"></th>'
        f'<th class="gsrc" colspan="{len(cols)}">{_e(file_label)}</th><th class="gap"></th>'
        f'<th class="gout" colspan="{len(fields)}">{_e(target_label)}</th></tr>'
    )
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

    src_records = source.iloc[rows].to_dict("records") if rows else []
    aud_records = audit.iloc[rows].to_dict("records") if rows else []
    for srow, arow in zip(src_records, aud_records):
        status = arow["_status"]
        label, cls = STATUS_BADGE.get(status, (status, "empty"))
        issues = json.loads(arow["_issues"]) if arow.get("_issues") else {}
        cells = [f'<tr class="r-{status}"><td class="meta">{_e(arow["_row"])}</td>'
                 f'<td><span class="cmp-b {cls}">{label}</span>'
                 + (f'<span class="why">{_e(arow["_reason"])}</span>' if arow["_reason"] else "") + "</td>"]
        for c in cols:
            v = srow.get(c, "")
            klass = ' class="o unused"' if c in plan.unused else ' class="o"'
            cells.append(f'<td{klass} title="{_e(c)}: {_e(v)}">{_e(v)}</td>')
        cells.append('<td class="gap"></td>')
        for f in fields:
            out = arow.get(f"out.{f.name}", "")
            raw = arow.get(f"raw.{f.name}", "")
            st = arow.get(f"st.{f.name}", "")
            reason = issues.get(f.name, "")
            tip = f"{f.header}: {CELL_LABELS.get(st, '')}"
            if raw and raw != out:
                tip += f" — original: {raw}"
            if reason:
                tip += f" — {reason}"
            klass = f' class="n c-{st}"' if st and st != CELL_SAME else ' class="n"'
            if st in (CELL_REJECT, CELL_BLANKED):
                body = (_e(raw) if raw else '<span class="blank">missing</span>') if st == CELL_REJECT else \
                    '<span class="blank">(left blank)</span>'
                body += f'<span class="why">{_e(reason)}</span>'
                if st == CELL_BLANKED and show_was and raw:
                    body = f'{body}<span class="was">{_e(raw)}</span>'
            else:
                body = _e(out)
                if show_was and st in (CELL_FORMAT, CELL_FIXED) and raw and raw != out:
                    body += f'<span class="was">{_e(raw)}</span>'
            cells.append(f'<td{klass} title="{_e(tip)}">{body}</td>')
        cells.append("</tr>")
        parts.append("".join(cells))
    if not rows:
        parts.append(f'<tr><td colspan="{len(cols) + len(fields) + 3}" style="padding:24px;color:#6e7781">'
                     "No rows match this filter.</td></tr>")
    parts.append("</tbody></table></div>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# What changed — per field
# ---------------------------------------------------------------------------

def field_change_summary(audit: pd.DataFrame, schema: TargetSchema, mapping: Mapping) -> pd.DataFrame:
    live = audit[audit["_status"] != "empty"]
    rows = []
    for f in schema.fields:
        st = live.get(f"st.{f.name}")
        if st is None:
            continue
        vc = st.value_counts()
        fmt_mask = st.isin([CELL_FORMAT, CELL_FIXED])
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
            "Left blank (invalid)": int(vc.get(CELL_BLANKED, 0)),
            "Rejected rows": int(vc.get(CELL_REJECT, 0)),
            "Empty": int((st == "").sum()),
            "Examples": "   ·   ".join(examples),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Excel export of the side-by-side view
# ---------------------------------------------------------------------------

XLSX_FILL = {CELL_FORMAT: "#E8F1FF", CELL_DERIVED: "#F2ECFF", CELL_DEFAULT: "#F3F4F6", CELL_FIXED: "#E3F6E9",
             CELL_BLANKED: "#FFF4D6", CELL_REJECT: "#FDE1E1"}


def comparison_xlsx(source: pd.DataFrame, audit: pd.DataFrame, schema: TargetSchema, plan: ColumnPlan,
                    title: str) -> bytes:
    buf = io.BytesIO()
    import xlsxwriter

    wb = xlsxwriter.Workbook(buf, {"constant_memory": True, "strings_to_numbers": False,
                                   "strings_to_formulas": False, "strings_to_urls": False})
    ws = wb.add_worksheet("Side by side")
    f_title = wb.add_format({"bold": True, "font_size": 12})
    f_grp_src = wb.add_format({"bold": True, "bg_color": "#EEF1F5", "border": 1})
    f_grp_out = wb.add_format({"bold": True, "bg_color": "#EAF2FF", "font_color": "#1D4ED8", "border": 1})
    f_head = wb.add_format({"bold": True, "bg_color": "#F6F7F9", "border": 1, "text_wrap": True, "valign": "top"})
    f_text = wb.add_format({"num_format": "@"})
    f_unused = wb.add_format({"num_format": "@", "font_color": "#9AA1A9"})
    f_gap = wb.add_format({"bg_color": "#C9CED6"})
    f_status = {"imported": wb.add_format({"font_color": "#116329"}),
                "rejected": wb.add_format({"font_color": "#B42318", "bold": True}),
                "duplicate": wb.add_format({"font_color": "#57606A"}),
                "empty": wb.add_format({"font_color": "#9AA1A9"})}
    f_cell = {k: wb.add_format({"num_format": "@", "bg_color": v}) for k, v in XLSX_FILL.items()}

    cols, fields = plan.source_columns, schema.fields
    c_src0 = 4
    c_gap = c_src0 + len(cols)
    c_out0 = c_gap + 1
    ws.write(0, 0, title, f_title)
    ws.write(1, 0, "Blue = reformatted · purple = built from other columns · green = Claude fix · "
                   "yellow = invalid, left blank · red = invalid/missing, row rejected")
    ws.merge_range(2, c_src0, 2, c_gap - 1, "ORIGINAL FILE", f_grp_src) if len(cols) > 1 else \
        ws.write(2, c_src0, "ORIGINAL FILE", f_grp_src)
    ws.merge_range(2, c_out0, 2, c_out0 + len(fields) - 1, "CONVERTED", f_grp_out) if len(fields) > 1 else \
        ws.write(2, c_out0, "CONVERTED", f_grp_out)
    headers = ["File row", "Status", "Reason", "Notes"]
    for i, h in enumerate(headers):
        ws.write(3, i, h, f_head)
    for j, c in enumerate(cols):
        ws.write(3, c_src0 + j, c + ("\n(not imported)" if c in plan.unused else "\n→ " + ", ".join(plan.feeds[c])),
                 f_head)
    ws.write(3, c_gap, "", f_gap)
    for j, f in enumerate(fields):
        ws.write(3, c_out0 + j, f"{f.header}\n← {plan.origin.get(f.name, '')}", f_head)
    ws.set_row(3, 45)
    ws.freeze_panes(4, 1)
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
        notes = "; ".join(f"{schema.field(k).header}: {v}" for k, v in issues.items() if k in schema.field_names)
        ws.write_number(r, 0, int(arow["_row"]))
        ws.write(r, 1, STATUS_BADGE.get(status, (status,))[0], f_status.get(status))
        ws.write_string(r, 2, arow["_reason"] or "")
        ws.write_string(r, 3, notes)
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
