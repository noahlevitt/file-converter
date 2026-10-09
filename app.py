"""Import File Converter — internal web app.

Run locally:   streamlit run app.py
Run on server: docker compose up -d   (see README.md)
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pandas as pd
import streamlit as st

from converter import __version__
from converter.ai import AIError
from converter.auth import logout, require_login
from converter.config import get_settings
from converter.editor import FROM_LABEL, LABELS, OPTION_HELP, apply_row_edit, option_text, row_problem
from converter.engine import ConversionResult, convert, preview_values
from converter.mapping import Mapping, profile_columns
from converter.output import (output_filename, rejects_filename, timestamp, write_output, write_rejects,
                              write_summary)
from converter.reader import SUPPORTED_EXTENSIONS, ReadError, read_file
from converter.schema import TargetSchema, load_schema
from converter.services import initial_mapping, make_assistant, merge, validate_repairs
from converter.storage import Store

APP_TITLE = os.getenv("APP_TITLE", "Import File Converter")
settings = get_settings()

st.set_page_config(page_title=APP_TITLE, page_icon="🔄", layout="wide")

COL_FIELD, COL_TYPE, COL_HOW, COL_SRC, COL_OPT = "Field", "Type", "How", "Source column(s)", "Option"
COL_PREVIEW, COL_CHECK, COL_BY, COL_WHY = "Sample output", "Check", "Mapped by", "Why"


# ---------------------------------------------------------------------------
# Shared resources
# ---------------------------------------------------------------------------

@st.cache_resource
def get_store() -> Store:
    return Store(settings.db_path)


@st.cache_resource
def _schema_cache(path: str, mtime: float) -> TargetSchema:
    return load_schema(path)


def get_schema() -> TargetSchema:
    path = settings.schema_path
    return _schema_cache(str(path), path.stat().st_mtime)  # reloads automatically when the YAML changes


@st.cache_resource
def get_assistant():
    return make_assistant(settings)


store = get_store()
try:
    schema = get_schema()
except Exception as exc:  # bad YAML should be obvious, not a stack trace
    st.error(f"The target schema file could not be loaded ({settings.schema_path}): {exc}")
    st.stop()
assistant = get_assistant()
user = require_login(store, settings, APP_TITLE)

ss = st.session_state
ss.setdefault("editor_version", 0)
ss.setdefault("uploader_version", 0)


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

FILE_KEYS = ["file_sig", "file_name", "file_bytes", "read", "profiles", "mapping", "map_meta", "result",
             "overrides", "pending", "repair_proposals", "edit_errors", "ai_used", "template_name", "last_outputs"]


def reset_file_state() -> None:
    for k in FILE_KEYS:
        ss.pop(k, None)


def load_and_map(sheet: str | None = None, header_row: int | None | str = "auto", use_ai: bool = True) -> None:
    try:
        rr = read_file(ss.file_name, ss.file_bytes, sheet=sheet, header_row=header_row)
    except ReadError as exc:
        ss.read = None
        ss.read_error = str(exc)
        return
    ss.read_error = None
    ss.read = rr
    ss.profiles = profile_columns(rr.df, n_samples=settings.ai_sample_values)
    with st.spinner("Matching columns" + (" (asking Claude)…" if use_ai and assistant else "…")):
        sug = initial_mapping(schema, rr.df, ss.profiles, store, assistant, use_ai=use_ai)
    ss.mapping = sug.mapping
    ss.map_meta = {"source": sug.source, "notes": sug.notes,
                   "template": sug.template.name if sug.template else None, "similarity": sug.similarity}
    ss.template_name = sug.template.name if (sug.template and sug.similarity == 1.0) else Path(ss.file_name).stem
    ss.ai_used = sug.source == "rules+ai"
    ss.result = None
    ss.overrides = {}
    ss.pending = None
    ss.repair_proposals = None
    ss.edit_errors = []
    ss.editor_version += 1


def template_key() -> str:
    return f"template_name_{ss.file_sig[1][:10]}" if ss.get("file_sig") else "template_name"


def origin_label(fm) -> str:
    names = {"template": "Template", "rule": "Rules", "ai": "Claude", "user": "You", "": ""}
    label = names.get(fm.origin, fm.origin)
    if fm.transform == "none":
        return ""
    return f"{label} · {fm.confidence}" if fm.confidence else label


def on_map_edit(key: str) -> None:
    state = ss.get(key, {})
    mapping: Mapping = ss.mapping
    errors = []
    for idx, changes in state.get("edited_rows", {}).items():
        f = schema.fields[int(idx)]
        mapping, err = apply_row_edit(schema, mapping, f.name, changes, COL_HOW, COL_SRC, COL_OPT)
        if err:
            errors.append(f"{f.header}: {err}")
    ss.mapping = mapping  # incomplete rows stay visible and are flagged in the Check column
    ss.edit_errors = errors
    ss.result = None
    ss.editor_version += 1


def mapping_diff(before: Mapping, changes: Mapping) -> pd.DataFrame:
    rows = []
    for name, fm in changes.fields.items():
        old = before.get(name)
        if (old.transform, old.sources, old.params) == (fm.transform, fm.sources, fm.params):
            continue
        rows.append({"Field": schema.field(name).header, "Before": old.summary(), "After": fm.summary(),
                     "Why": fm.reason})
    return pd.DataFrame(rows, columns=["Field", "Before", "After", "Why"])


def run_conversion() -> None:
    rr = ss.read
    rows = [rr.excel_row_number(i) for i in range(len(rr.df))]
    with st.spinner(f"Converting {len(rr.df):,} rows…"):
        result = convert(rr.df, schema, ss.mapping, row_numbers=rows, overrides=ss.get("overrides") or {})
    ss.result = result
    ts = timestamp()
    ss.last_outputs = {
        "output_name": output_filename(schema, ss.file_name, ts),
        "output_bytes": write_output(result.output, schema),
        "rejects_name": rejects_filename(ss.file_name, len(result.rejects), ts),
        "rejects_bytes": write_rejects(result),
        "summary_name": f"{Path(ss.file_name).stem}_SUMMARY_{ts}.txt",
        "summary_bytes": write_summary(result, schema, ss.file_name, user["username"], __version__),
    }
    tpl_name = None
    typed = (ss.get(template_key()) or "").strip()
    if ss.get("remember_mapping", True) and typed:
        tpl_name = typed
        store.save_template(tpl_name, list(rr.df.columns), ss.mapping,
                            {"sheet": rr.sheet, "header_row": rr.header_row}, schema.name, user["username"])
        store.mark_template_used(tpl_name)
    store.log_job(user["username"], ss.file_name, tpl_name, schema.label, result.summary_dict(),
                  bool(ss.get("ai_used")))


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

def page_convert() -> None:
    st.header("Convert a file")
    st.caption(f"Target format: **{schema.label}** · one file at a time · "
               f"supported: {', '.join(e.lstrip('.') for e in SUPPORTED_EXTENSIONS)}")

    top = st.columns([4, 1], vertical_alignment="bottom")
    with top[0]:
        up = st.file_uploader("Upload a file", type=[e.lstrip(".") for e in SUPPORTED_EXTENSIONS],
                              key=f"uploader_{ss.uploader_version}", label_visibility="collapsed")
    with top[1]:
        if ss.get("file_name") and st.button("Start over", width="stretch"):
            reset_file_state()
            ss.uploader_version += 1
            st.rerun()

    if up is not None:
        data = up.getvalue()
        sig = (up.name, hashlib.sha1(data).hexdigest())
        if ss.get("file_sig") != sig:
            reset_file_state()
            ss.file_sig, ss.file_name, ss.file_bytes = sig, up.name, data
            load_and_map()

    if ss.get("file_name") and "read" not in ss:
        load_and_map()

    if not ss.get("file_name"):
        st.info("Upload a CSV or Excel file to begin. Columns are matched automatically; you review the mapping "
                "before anything is converted.")
        return
    if ss.get("read_error"):
        st.error(ss.read_error)
        return
    rr = ss.read
    df = rr.df

    # ---------------- Step 1: file
    st.subheader("1 · File")
    info = [f"**{ss.file_name}**", f"{len(df):,} rows", f"{len(df.columns)} columns"]
    if rr.sheet:
        info.append(f"sheet '{rr.sheet}'")
    if rr.encoding:
        info.append(f"encoding {rr.encoding}")
    if rr.delimiter:
        info.append("delimiter " + {"\t": "TAB", ",": "comma", ";": "semicolon", "|": "pipe"}.get(rr.delimiter,
                                                                                                    rr.delimiter))
    st.markdown(" · ".join(info))
    for w in rr.warnings:
        st.warning(w)

    with st.expander("File options and raw preview", expanded=bool(rr.warnings)):
        c1, c2, c3 = st.columns([2, 2, 1], vertical_alignment="bottom")
        sheet = rr.sheet
        if rr.sheet_names and len(rr.sheet_names) > 1:
            sheet = c1.selectbox("Sheet", rr.sheet_names, index=rr.sheet_names.index(rr.sheet))
        hdr_default = 0 if rr.header_row is None else rr.header_row + 1
        hdr = c2.number_input("Header is on row (0 = file has no header row)", min_value=0, max_value=100,
                              value=hdr_default, step=1)
        if c3.button("Re-read file", width="stretch"):
            load_and_map(sheet=sheet, header_row=None if hdr == 0 else int(hdr) - 1)
            st.rerun()
        st.caption("First rows exactly as they appear in the file:")
        st.dataframe(pd.DataFrame(rr.raw_preview), hide_index=False, width="stretch")

    # ---------------- Step 2: mapping
    st.subheader("2 · Review column mapping")
    meta = ss.map_meta
    if meta["source"] == "template":
        st.success(f"Recognised this layout — using saved template **{meta['template']}**.")
    elif meta["source"] == "rules+ai":
        st.info("Columns matched by rules and Claude. Check the rows marked low/medium before converting.")
    else:
        st.info("Columns matched by rules" + ("" if assistant else " (Claude is not configured)") +
                ". Check every mapping before converting.")
    for n in meta["notes"]:
        if n.startswith("Matched saved template"):
            continue
        st.caption("ℹ️ " + n)

    mapping: Mapping = ss.mapping
    preview = preview_values(df, schema, mapping)
    grid = pd.DataFrame([
        {
            COL_FIELD: f.header + (" *" if f.required else ""),
            COL_TYPE: f.type,
            COL_HOW: LABELS[mapping.get(f.name).transform],
            COL_SRC: mapping.get(f.name).sources,
            COL_OPT: option_text(mapping.get(f.name), f),
            COL_PREVIEW: preview.get(f.name, ""),
            COL_CHECK: ("⚠ " + p) if (p := row_problem(schema, mapping, f.name)) else "",
            COL_BY: origin_label(mapping.get(f.name)),
            COL_WHY: mapping.get(f.name).reason,
        }
        for f in schema.fields
    ])
    key = f"map_editor_{ss.editor_version}"
    st.data_editor(
        grid,
        key=key,
        on_change=on_map_edit,
        args=(key,),
        hide_index=True,
        num_rows="fixed",
        width="stretch",
        disabled=[COL_FIELD, COL_TYPE, COL_PREVIEW, COL_CHECK, COL_BY, COL_WHY],
        column_config={
            COL_FIELD: st.column_config.TextColumn(help="Target field. * = required.", pinned=True),
            COL_HOW: st.column_config.SelectboxColumn(options=list(FROM_LABEL), required=True,
                                                      help="How the value is built from the source column(s)."),
            COL_SRC: st.column_config.MultiselectColumn(options=list(df.columns),
                                                        help="Column(s) from the uploaded file. Order matters for "
                                                             "'First non-empty' and 'Combine columns'."),
            COL_OPT: st.column_config.TextColumn(help=OPTION_HELP),
            COL_PREVIEW: st.column_config.TextColumn(help="Converted values from the first rows of the file."),
            COL_WHY: st.column_config.TextColumn(width="large"),
        },
    )
    for e in ss.get("edit_errors") or []:
        st.error(e)

    unused = [c for c in df.columns if c not in mapping.used_sources()]
    if unused:
        st.caption("Columns not imported: " + ", ".join(f"`{c}`" for c in unused))

    # Claude: instructions + re-map
    with st.container(border=True):
        st.markdown("**Instructions for Claude**")
        if not assistant:
            st.caption("Claude is not configured (set ANTHROPIC_API_KEY on the server). Mapping still works by "
                       "rules and manual edits.")
        with st.form("instruction_form", clear_on_submit=False, border=False):
            instruction = st.text_area(
                "Tell Claude how to treat specific columns",
                placeholder="e.g. 'Cust Ph' is the phone number · Join Date is day-first · put 'VIP Level' into "
                            "Notes · split 'Contact' into first and last name",
                label_visibility="collapsed", height=80, disabled=not assistant,
            )
            b1, b2 = st.columns([1, 1])
            ask = b1.form_submit_button("Apply instruction", type="primary", disabled=not assistant,
                                        width="stretch")
            remap = b2.form_submit_button("Re-check whole mapping with Claude", disabled=not assistant,
                                          width="stretch")
        if ask and instruction.strip():
            try:
                with st.spinner("Claude is reading your instruction…"):
                    res = assistant.apply_instruction(schema, ss.profiles, mapping, instruction)
                ss.pending = {"kind": "instruction", "changes": res.mapping, "notes": res.notes,
                              "dropped": res.dropped}
            except AIError as exc:
                st.error(str(exc))
        if remap:
            try:
                with st.spinner("Claude is reviewing the mapping…"):
                    res = assistant.propose_mapping(schema, ss.profiles, current=mapping)
                ss.pending = {"kind": "remap", "changes": res.mapping, "notes": res.notes, "dropped": res.dropped}
            except AIError as exc:
                st.error(str(exc))

        pending = ss.get("pending")
        if pending:
            diff = mapping_diff(mapping, pending["changes"])
            if pending["notes"]:
                st.markdown(f"**Claude:** {pending['notes']}")
            if pending["dropped"]:
                st.caption("Ignored invalid suggestions: " + "; ".join(pending["dropped"][:5]))
            if diff.empty:
                st.caption("No mapping changes proposed.")
                if st.button("OK"):
                    ss.pending = None
                    st.rerun()
            else:
                st.dataframe(diff, hide_index=True, width="stretch")
                a1, a2, _ = st.columns([1, 1, 3])
                if a1.button("Accept changes", type="primary", width="stretch"):
                    changed = Mapping(fields={k: v for k, v in pending["changes"].fields.items()
                                              if schema.field(k).header in set(diff["Field"])})
                    ss.mapping = merge(mapping, changed).normalized(schema, list(df.columns))
                    ss.ai_used = True
                    ss.pending = None
                    ss.result = None
                    ss.editor_version += 1
                    st.rerun()
                if a2.button("Discard", width="stretch"):
                    ss.pending = None
                    st.rerun()

    # ---------------- Step 3: convert
    st.subheader("3 · Convert")
    problems = ss.mapping.problems(schema)
    if problems:
        st.warning("Fix before converting: " + "; ".join(f"**{schema.field(k).header}** — {v}"
                                                         for k, v in problems.items()))
    c1, c2, c3 = st.columns([1.2, 2.5, 1.3], vertical_alignment="bottom")
    c1.checkbox("Remember this mapping", value=True, key="remember_mapping",
                help="Saves the mapping as a template; the next file with the same columns maps instantly.")
    c2.text_input("Template name", value=ss.get("template_name") or "", key=template_key(),
                  disabled=not ss.get("remember_mapping", True))
    if c3.button("Convert file", type="primary", disabled=bool(problems), width="stretch"):
        run_conversion()

    result: ConversionResult | None = ss.get("result")
    if result is not None:
        show_result(result)


def show_result(result: ConversionResult) -> None:
    out = ss.last_outputs
    if result.rejected:
        st.error(f"**{result.rejected:,} bad row{'s' if result.rejected != 1 else ''} were NOT imported.** "
                 "Download *Rejected rows* to see the reason for each one.  \n" + result.flag_message(), icon="⚠️")
    elif result.imported == 0:
        st.error("No rows could be converted. " + result.flag_message())
    else:
        st.success("✅ " + result.flag_message())

    m = st.columns(5)
    m[0].metric("Rows in file", f"{result.total_rows:,}")
    m[1].metric("Converted", f"{result.imported:,}")
    m[2].metric("Bad rows", f"{result.rejected:,}")
    m[3].metric("Duplicates removed", f"{result.duplicates:,}")
    m[4].metric("Empty rows skipped", f"{result.blank_rows:,}")

    d = st.columns(3)
    d[0].download_button(f"⬇ Converted file ({result.imported:,} rows)", out["output_bytes"], out["output_name"],
                         type="primary", width="stretch", on_click="ignore",
                         disabled=result.imported == 0)
    d[1].download_button(f"⬇ Rejected rows ({len(result.rejects):,})", out["rejects_bytes"], out["rejects_name"],
                         mime="text/csv", width="stretch", on_click="ignore", disabled=result.rejects.empty)
    d[2].download_button("⬇ Summary", out["summary_bytes"], out["summary_name"], mime="text/plain",
                         width="stretch", on_click="ignore")

    if result.reason_counts or result.blanked_counts:
        rows = [{"Problem": r, "Rows": c, "Effect": "row not imported"} for r, c in result.reason_counts.most_common()]
        rows += [{"Problem": r, "Rows": c, "Effect": "value left blank"} for r, c in result.blanked_counts.most_common()]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

    t1, t2, t3 = st.tabs(["Converted rows", "Rejected & duplicate rows", "Field fill rate"])
    with t1:
        st.dataframe(result.output.head(500), hide_index=True, width="stretch")
        if result.imported > 500:
            st.caption(f"Showing 500 of {result.imported:,} rows.")
    with t2:
        if result.rejects.empty:
            st.caption("None.")
        else:
            st.dataframe(result.rejects.head(1000), hide_index=True, width="stretch")
    with t3:
        st.dataframe(pd.DataFrame([{"Field": k, "Filled": f"{v:.0%}"} for k, v in result.fill_rates.items()]),
                     hide_index=True, width="stretch")

    if result.failed_values and assistant:
        n_vals = sum(len(v) for v in result.failed_values.values())
        with st.expander(f"Try to fix {n_vals:,} unconvertible value{'s' if n_vals != 1 else ''} with Claude"):
            st.caption("Claude only reformats values (e.g. 'Jan 5th 24', '516 5OO 0100'). Every suggestion is "
                       "re-checked; suggestions that add or change data are refused. Nothing changes until you apply.")
            if st.button("Suggest fixes"):
                proposals = {}
                try:
                    with st.spinner("Claude is reviewing the values…"):
                        for fname, values in result.failed_values.items():
                            f = schema.field(fname)
                            good = [v for v in result.output[f.header].tolist() if v][:5]
                            pairs, _ = assistant.repair_values(f, values, good)
                            accepted, rejected = validate_repairs(schema, ss.mapping, fname, pairs)
                            fixed = set(accepted) | {r[0] for r in rejected}
                            proposals[fname] = {"accepted": accepted, "rejected": rejected,
                                                "unfixable": [v for v in values if v not in fixed]}
                    ss.repair_proposals = proposals
                except AIError as exc:
                    st.error(str(exc))
            props = ss.get("repair_proposals")
            if props:
                rows = []
                for fname, p in props.items():
                    h = schema.field(fname).header
                    rows += [{"Field": h, "Original": a, "Fix": b, "Status": "✓ will apply"}
                             for a, b in p["accepted"].items()]
                    rows += [{"Field": h, "Original": a, "Fix": b, "Status": f"✗ refused: {why}"}
                             for a, b, why in p["rejected"]]
                    rows += [{"Field": h, "Original": a, "Fix": "", "Status": "can't be fixed"} for a in p["unfixable"]]
                st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
                n_ok = sum(len(p["accepted"]) for p in props.values())
                if n_ok and st.button(f"Apply {n_ok} fix{'es' if n_ok != 1 else ''} and re-convert", type="primary"):
                    overrides = ss.get("overrides") or {}
                    for fname, p in props.items():
                        overrides.setdefault(fname, {}).update(p["accepted"])
                    ss.overrides = overrides
                    ss.ai_used = True
                    ss.repair_proposals = None
                    run_conversion()
                    st.rerun()


def page_templates() -> None:
    st.header("Saved templates")
    st.caption("A template remembers how one file layout maps to the target format. When a file with the same "
               "columns is uploaded, its template is applied automatically.")
    templates = store.list_templates(schema.name)
    if not templates:
        st.info("No templates yet. They are saved when you convert a file with 'Remember this mapping' on.")
        return
    st.dataframe(pd.DataFrame([{
        "Name": t.name, "Columns": len(t.headers), "Times used": t.use_count, "Created by": t.created_by,
        "Updated": t.updated_at.replace("T", " "), "Updated by": t.updated_by,
    } for t in templates]), hide_index=True, width="stretch")

    name = st.selectbox("Template", [t.name for t in templates])
    t = next(x for x in templates if x.name == name)
    st.markdown("**Columns in this layout:** " + ", ".join(f"`{h}`" for h in t.headers))
    st.dataframe(pd.DataFrame([{
        "Field": f.header, "Mapping": t.mapping.get(f.name).summary(), "Option": option_text(t.mapping.get(f.name), f)
    } for f in schema.fields]), hide_index=True, width="stretch")

    c1, c2, c3 = st.columns([2, 1, 1], vertical_alignment="bottom")
    new_name = c1.text_input("Rename to", value=t.name)
    if c2.button("Rename", width="stretch") and new_name.strip() and new_name.strip() != t.name:
        try:
            store.rename_template(t.name, new_name)
            st.rerun()
        except Exception as exc:
            st.error(str(exc))
    can_delete = user["is_admin"] or user["username"] == t.created_by
    if c3.button("Delete", width="stretch", disabled=not can_delete,
                 help=None if can_delete else "Only the creator or an admin can delete"):
        store.delete_template(t.name)
        st.rerun()
    st.download_button("Download template (JSON)", json.dumps({
        "name": t.name, "headers": t.headers, "mapping": t.mapping.model_dump(), "read_options": t.read_options,
        "schema": t.schema_name}, indent=2), f"template_{t.name}.json", mime="application/json", on_click="ignore")


def page_history() -> None:
    st.header("Conversion history")
    jobs = store.list_jobs(500)
    if not jobs:
        st.info("No conversions yet.")
        return
    df = pd.DataFrame(jobs).rename(columns={
        "created_at": "When", "username": "User", "filename": "File", "template_name": "Template",
        "total_rows": "Rows", "imported": "Converted", "rejected": "Bad rows", "duplicates": "Duplicates",
        "blank_rows": "Empty", "blanked": "Blanked values", "ai_used": "Claude used"})
    df["When"] = df["When"].str.replace("T", " ")
    df["Claude used"] = df["Claude used"].map({1: "yes", 0: ""})
    st.dataframe(df.drop(columns=["id"]), hide_index=True, width="stretch")


def page_schema() -> None:
    st.header("Target format")
    st.markdown(f"**{schema.label}** — {schema.description}")
    st.caption(f"Defined in `{settings.schema_path}`. Edit that file on the server to change the target format; "
               "the app picks up changes automatically.")
    st.dataframe(pd.DataFrame([{
        "Output header": f.header, "Field id": f.name, "Type": f.type, "Required": "yes" if f.required else "",
        "Format": f.format or "", "If invalid": "reject row" if f.on_invalid == "reject" else "leave blank",
        "Max length": str(f.max_length or ""), "Allowed values": ", ".join(f.allowed_values),
    } for f in schema.fields]), hide_index=True, width="stretch")
    o = schema.output
    st.markdown(f"**Output file:** {o.format.upper()}"
                + (f", delimiter `{repr(o.delimiter)}`, encoding {o.encoding}" if o.format == "csv" else "")
                + (", header row included" if o.include_header else ", no header row"))
    if schema.dedupe.keys:
        st.markdown("**Duplicates:** a row is removed only when **all** of these match an earlier row: "
                    + ", ".join(schema.field(k).header for k in schema.dedupe.keys)
                    + ". Rows with the same phone but a different date are kept.")


def page_admin() -> None:
    st.header("Users")
    users = store.list_users()
    st.dataframe(pd.DataFrame(users).rename(columns={"username": "User", "is_admin": "Admin", "created_at": "Created",
                                                     "last_login": "Last sign-in"}),
                 hide_index=True, width="stretch")
    c1, c2 = st.columns(2)
    with c1, st.form("add_user", clear_on_submit=True):
        st.markdown("**Add user**")
        u = st.text_input("Username")
        p = st.text_input("Temporary password (8+ characters)", type="password")
        a = st.checkbox("Administrator")
        if st.form_submit_button("Add user", type="primary"):
            try:
                store.add_user(u, p, a)
                st.success(f"Added {u}.")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))
    with c2, st.form("manage_user"):
        st.markdown("**Manage user**")
        names = [x["username"] for x in users]
        target = st.selectbox("User", names)
        newpw = st.text_input("New password (leave empty to keep)", type="password")
        make_admin = st.checkbox("Administrator", value=bool(next((x["is_admin"] for x in users
                                                                  if x["username"] == target), False)))
        delete = st.checkbox("Delete this user")
        if st.form_submit_button("Save"):
            try:
                if delete:
                    if target == user["username"]:
                        st.error("You can't delete your own account.")
                    else:
                        store.delete_user(target)
                else:
                    if newpw:
                        store.set_password(target, newpw)
                    store.set_admin(target, make_admin)
                st.rerun()
            except Exception as exc:
                st.error(str(exc))
    with st.form("own_password"):
        st.markdown("**Change my password**")
        p1 = st.text_input("New password", type="password")
        if st.form_submit_button("Change"):
            try:
                store.set_password(user["username"], p1)
                st.success("Password changed.")
            except Exception as exc:
                st.error(str(exc))


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

with st.sidebar:
    st.markdown(f"### 🔄 {APP_TITLE}")
    pages = ["Convert", "Templates", "History", "Target format"] + (["Users"] if user["is_admin"] else [])
    page = st.radio("Page", pages, label_visibility="collapsed")
    st.divider()
    st.caption(f"Signed in as **{user['username']}**" + (" (admin)" if user["is_admin"] else ""))
    if settings.auth_mode != "none" and st.button("Sign out"):
        logout()
        st.rerun()
    if not user["is_admin"]:
        with st.expander("Change my password"):
            with st.form("pw_self"):
                p1 = st.text_input("New password", type="password")
                if st.form_submit_button("Change"):
                    try:
                        store.set_password(user["username"], p1)
                        st.success("Changed.")
                    except Exception as exc:
                        st.error(str(exc))
    st.caption("Claude: " + (f"connected ({settings.mapping_model})" if assistant else "not configured"))
    st.caption(f"Target: {schema.label}")
    st.caption(f"Version {__version__}")

{"Convert": page_convert, "Templates": page_templates, "History": page_history, "Target format": page_schema,
 "Users": page_admin}[page]()
