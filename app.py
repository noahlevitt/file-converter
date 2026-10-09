"""Import File Converter — internal web app.

Pages: New import · Imports (tracker, progress, results, side-by-side comparison) · Templates · Target format · Users

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
from converter.compare import (FILTERS, build_compare_html, column_plan, comparison_xlsx, field_change_summary,
                               filter_rows, status_counts)
from converter.config import get_settings
from converter.checks import column_warnings
from converter.editor import (FROM_LABEL, LABELS, OPTION_HELP, apply_column_edit, apply_row_edit, option_text,
                              row_problem)
from converter.engine import convert, preview_values, source_rows
from converter.jobs import ImportRunner
from converter.mapping import Mapping, profile_columns
from converter.reader import SUPPORTED_EXTENSIONS, ReadError, read_file
from converter.schema import TargetSchema, load_schema
from converter.services import (check_for_mistakes, initial_mapping, make_assistant, merge,
                                validate_repairs)
from converter.storage import ImportRecord, Store
from converter.ui_format import fmt_dt, fmt_duration, fmt_eta, fmt_size, status_color, status_text

APP_TITLE = os.getenv("APP_TITLE", "Import File Converter")
settings = get_settings()
TZ = settings.timezone

st.set_page_config(page_title=APP_TITLE, page_icon="🔄", layout="wide")

US_ZONES = {"Eastern": "America/New_York", "Central": "America/Chicago", "Mountain": "America/Denver",
            "Arizona": "America/Phoenix", "Pacific": "America/Los_Angeles", "Alaska": "America/Anchorage",
            "Hawaii": "Pacific/Honolulu"}
ZONE_LABEL = {v: k for k, v in US_ZONES.items()}
COL_FIELD, COL_TYPE, COL_HOW, COL_SRC, COL_OPT = "Field", "Type", "How", "Source column(s)", "Option"
COL_PREVIEW, COL_CHECK, COL_BY, COL_WHY = "Sample output", "Check", "Mapped by", "Why"


# ---------------------------------------------------------------------------
# Shared resources (one per server process)
# ---------------------------------------------------------------------------

@st.cache_resource
def get_store() -> Store:
    return Store(settings.db_path)


@st.cache_resource
def get_runner() -> ImportRunner:
    return ImportRunner(get_store(), workers=settings.import_workers, retention_days=settings.file_retention_days)


@st.cache_resource
def _schema_cache(path: str, mtime: float) -> TargetSchema:
    return load_schema(path)


def get_schema() -> TargetSchema:
    path = settings.schema_path
    return _schema_cache(str(path), path.stat().st_mtime)  # reloads automatically when the YAML changes


@st.cache_resource
def get_assistant():
    return make_assistant(settings)


@st.cache_data(max_entries=6, show_spinner=False)
def load_import_tables(import_id: int, folder: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    d = Path(folder)
    audit = pd.read_parquet(d / "audit.parquet")
    return source_rows(pd.read_parquet(d / "source.parquet"), audit), audit


store = get_store()
runner = get_runner()
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
# New import: state helpers
# ---------------------------------------------------------------------------

FILE_KEYS = ["file_sig", "file_name", "file_bytes", "read", "profiles", "mapping", "map_meta", "pending",
             "edit_errors", "ai_used", "template_name", "read_error", "timezone", "notes_result", "check_result"]


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
    with st.spinner("Matching columns" + (" (asking Claude)…" if use_ai and assistant and settings.ai_auto_map
                                          else "…")):
        sug = initial_mapping(schema, rr.df, ss.profiles, store, assistant, use_ai=use_ai and settings.ai_auto_map)
    ss.mapping = sug.mapping
    ss.map_meta = {"source": sug.source, "notes": sug.notes,
                   "template": sug.template.name if sug.template else None, "similarity": sug.similarity}
    ss.template_name = sug.template.name if (sug.template and sug.similarity == 1.0) else Path(ss.file_name).stem
    tpl_tz = sug.template.read_options.get("timezone") if sug.template else None
    ss.timezone = tpl_tz if tpl_tz in ZONE_LABEL else (settings.timezone if settings.timezone in ZONE_LABEL
                                                       else "America/New_York")
    ss.ai_used = sug.source == "rules+ai"
    ss.pending = None
    ss.notes_result = None
    ss.check_result = None
    ss.edit_errors = []
    ss.editor_version += 1


def template_key() -> str:
    return f"template_name_{ss.file_sig[1][:10]}" if ss.get("file_sig") else "template_name"


def origin_label(fm) -> str:
    names = {"template": "Template", "rule": "Rules", "ai": "Claude", "user": "You", "": ""}
    if fm.transform == "none":
        return ""
    label = names.get(fm.origin, fm.origin)
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
    ss.editor_version += 1


def mapping_diff(sch: TargetSchema, before: Mapping, changes: Mapping) -> pd.DataFrame:
    rows = []
    for name, fm in changes.fields.items():
        old = before.get(name)
        if (old.transform, old.sources, old.params) == (fm.transform, fm.sources, fm.params):
            continue
        rows.append({"Field": sch.field(name).header, "Before": old.summary(), "After": fm.summary(),
                     "Why": fm.reason})
    return pd.DataFrame(rows, columns=["Field", "Before", "After", "Why"])


def start_import() -> int:
    rr = ss.read
    tpl_name = None
    typed = (ss.get(template_key()) or "").strip()
    if ss.get("remember_mapping", True) and typed:
        tpl_name = typed
    read_options = {"sheet": rr.sheet, "header_row": rr.header_row, "timezone": ss.get("timezone")}
    if ss.get("remember_mapping", True) and typed:
        store.save_template(tpl_name, list(rr.df.columns), ss.mapping, read_options, schema.name, user["username"])
        store.mark_template_used(tpl_name)
    import_id = store.create_import(
        username=user["username"], filename=ss.file_name, data=ss.file_bytes, schema_label=schema.label,
        schema_json=schema.model_dump_json(), mapping=ss.mapping, read_options=read_options, template_name=tpl_name,
        ai_used=bool(ss.get("ai_used")))
    runner.submit(import_id)
    return import_id


def rerun_import(rec: ImportRecord, overrides: dict | None = None, ai_used: bool | None = None,
                 mapping: Mapping | None = None) -> int:
    """Start a new import from a previous one's original file and mapping, with the current target format."""
    data = store.source_path(rec).read_bytes()
    merged = dict(rec.overrides)
    for k, v in (overrides or {}).items():
        merged.setdefault(k, {}).update(v)
    new_id = store.create_import(
        username=user["username"], filename=rec.filename, data=data, schema_label=schema.label,
        schema_json=schema.model_dump_json(), mapping=mapping or rec.mapping, read_options=rec.read_options,
        overrides=merged, template_name=rec.template_name,
        ai_used=rec.ai_used if ai_used is None else ai_used, parent_id=rec.id)
    runner.submit(new_id)
    return new_id


def open_import(import_id: int) -> None:
    st.switch_page(PAGE_IMPORTS, query_params={"id": str(import_id)})


# ---------------------------------------------------------------------------
# Page: New import
# ---------------------------------------------------------------------------

def page_new_import() -> None:
    st.header("New import")
    st.caption(f"Target format: **{schema.label}** · supported files: "
               f"{', '.join(e.lstrip('.') for e in SUPPORTED_EXTENSIONS)}")

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
        st.info("Upload a CSV or Excel file. Columns are matched automatically and you review the mapping; the "
                "import then runs in the background and appears under **Imports**, where you can follow its "
                "progress and compare the original and converted files side by side.")
        return
    if ss.get("read_error"):
        st.error(ss.read_error)
        return
    rr = ss.read
    df = rr.df

    # ---------------- Step 1: file
    st.subheader("1 · File")
    info = [f"**{ss.file_name}**", fmt_size(len(ss.file_bytes)), f"{len(df):,} rows", f"{len(df.columns)} columns"]
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
    tz_col, _ = st.columns([2, 3])
    tz_label = tz_col.selectbox(
        "Client time zone", list(US_ZONES), index=list(US_ZONES).index(ZONE_LABEL.get(ss.get("timezone"), "Eastern")),
        key=f"tz_{ss.file_sig[1][:10]}",
        help="Times in the file that include a zone (Z, UTC, EST, +00:00…) and Unix timestamps are converted to "
             "this zone. Times without a zone are kept exactly as written.")
    if US_ZONES[tz_label] != ss.get("timezone"):
        ss.timezone = US_ZONES[tz_label]

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
        st.info("Columns matched by rules and Claude. Check the rows marked low/medium before importing.")
    else:
        st.info("Columns matched automatically. Check the rows marked low/medium, edit anything in the grid, or "
                "change where each column goes under Columns in your file below.")
    for n in meta["notes"]:
        if not n.startswith("Matched saved template"):
            st.caption("ℹ️ " + n)

    mapping: Mapping = ss.mapping
    preview = preview_values(df, schema, mapping, timezone=ss.get("timezone"))
    field_warn = {fname: f"'{col}' may not be a {schema.field(fname).type} column — see Columns in your file"
                  for col, (fname, _) in column_warnings(schema, mapping, ss.profiles).items()}
    grid = pd.DataFrame([
        {
            COL_FIELD: f.header + (" *" if f.required else "") + (" 🔒" if f.fixed_blank else ""),
            COL_TYPE: f.type,
            COL_HOW: LABELS[mapping.get(f.name).transform],
            COL_SRC: mapping.get(f.name).sources,
            COL_OPT: option_text(mapping.get(f.name), f),
            COL_PREVIEW: preview.get(f.name, ""),
            COL_CHECK: ("⚠ " + p) if (p := row_problem(schema, mapping, f.name) or field_warn.get(f.name, "")) else "",
            COL_BY: "Fixed" if f.fixed_blank else origin_label(mapping.get(f.name)),
            COL_WHY: "Always blank — required by the import format; can't be changed" if f.fixed_blank
            else mapping.get(f.name).reason,
        }
        for f in schema.fields
    ])
    key = f"map_editor_{ss.editor_version}"
    st.data_editor(
        grid, key=key, on_change=on_map_edit, args=(key,), hide_index=True, num_rows="fixed", width="stretch",
        disabled=[COL_FIELD, COL_TYPE, COL_PREVIEW, COL_CHECK, COL_BY, COL_WHY],
        column_config={
            COL_FIELD: st.column_config.TextColumn(help="Target field. * = required. 🔒 = always blank (locked).",
                                                   pinned=True),
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
    each = next((f for f in schema.mappable_fields if mapping.get(f.name).transform == "each_row"), None)
    if each is not None:
        st.caption(f"ℹ️ A customer with different numbers in {', '.join(mapping.get(each.name).sources)} gets "
                   f"one row per number in **{each.header}**, with the other columns copied from their original row.")
    unused = [c for c in df.columns if c not in mapping.used_sources()]
    if unused:
        skipped = set(mapping.excluded_columns)
        st.caption("Columns not imported: " + ", ".join(f"`{c}`" + (" (skipped)" if c in skipped else "")
                                                         for c in unused))

    _notes_and_checks(df)

    # ---------------- Step 3: start
    st.subheader("3 · Start import")
    problems = ss.mapping.problems(schema)
    if problems:
        st.warning("Fix before importing: " + "; ".join(f"**{schema.field(k).header}** — {v}"
                                                        for k, v in problems.items()))
    c1, c2, c3 = st.columns([1.2, 2.5, 1.3], vertical_alignment="bottom")
    c1.checkbox("Remember this mapping", value=True, key="remember_mapping",
                help="Saves the mapping as a template; the next file with the same columns maps instantly.")
    c2.text_input("Template name", value=ss.get("template_name") or "", key=template_key(),
                  disabled=not ss.get("remember_mapping", True))
    if c3.button("Start import", type="primary", disabled=bool(problems), width="stretch"):
        import_id = start_import()
        reset_file_state()
        ss.uploader_version += 1
        open_import(import_id)


COL_GOES, COL_SKIP, COL_NOTE = "Goes to", "Skip", "Your note"


def on_columns_edit(key: str, columns: list[str]) -> None:
    """Edits in the "Columns in your file" table: where a column goes, skipping it, and notes."""
    state = ss.get(key, {})
    mapping: Mapping = ss.mapping
    by_header = {f.header: f.name for f in schema.mappable_fields}
    notes = dict(mapping.column_notes)
    messages: list[str] = []
    for idx, changes in state.get("edited_rows", {}).items():
        col = columns[int(idx)]
        if COL_NOTE in changes:
            text = (changes[COL_NOTE] or "").strip()
            if text:
                notes[col] = text
            else:
                notes.pop(col, None)
        targets = None
        if COL_GOES in changes:
            targets = [by_header[h] for h in (changes[COL_GOES] or []) if h in by_header]
        skip = changes.get(COL_SKIP)
        if targets is not None or skip is not None:
            mapping, msgs = apply_column_edit(schema, mapping, col, targets=targets, skip=skip)
            messages.extend(msgs)
    ss.mapping = mapping.model_copy(update={"column_notes": notes})
    ss.column_messages = messages
    ss.notes_version = ss.get("notes_version", 0) + 1
    ss.editor_version += 1


def on_general_note(key: str) -> None:
    ss.mapping = ss.mapping.model_copy(update={"general_note": (ss.get(key) or "").strip()})


def _show_changes(changes: Mapping, base: Mapping, columns: list[str], key: str, clear=None) -> None:
    """Diff of proposed field changes with Accept / Discard (New import page). `clear` hides them afterwards."""
    clear = clear or (lambda: ss.pop(key, None))
    diff = mapping_diff(schema, base, changes)
    if diff.empty:
        st.caption("No mapping changes proposed.")
        return
    st.markdown("**Proposed mapping changes**")
    st.dataframe(diff, hide_index=True, width="stretch")
    a1, a2, _ = st.columns([1, 1, 3])
    if a1.button("Accept changes", type="primary", width="stretch", key=f"accept_{key}"):
        changed = Mapping(fields={k: v for k, v in changes.fields.items()
                                  if schema.field(k).header in set(diff["Field"])})
        ss.mapping = merge(base, changed).normalized(schema, columns)
        ss.ai_used = True
        clear()
        ss.editor_version += 1
        st.rerun()
    if a2.button("Discard", width="stretch", key=f"discard_{key}"):
        clear()
        st.rerun()


def _findings_table(findings, sch: TargetSchema) -> None:
    if not findings:
        st.success("No problems found.", icon="✅")
        return
    n_err = sum(f.severity == "error" for f in findings)
    n_warn = sum(f.severity == "warning" for f in findings)
    st.markdown(f"**{len(findings)} finding{'s' if len(findings) != 1 else ''}** · {n_err} error(s) · "
                f"{n_warn} warning(s)")
    st.dataframe(pd.DataFrame([f.as_row(sch) for f in findings]), hide_index=True, width="stretch",
                 column_config={"": st.column_config.TextColumn(width="small"),
                                "Issue": st.column_config.TextColumn(width="large"),
                                "Suggestion": st.column_config.TextColumn(width="large")})


def _notes_and_checks(df: pd.DataFrame) -> None:
    columns = list(df.columns)
    mapping: Mapping = ss.mapping
    with st.container(border=True):
        st.markdown("**Columns in your file**")
        st.caption("Change where a column goes with **Goes to** (clear it to leave the column out), or tick "
                   "**Skip** to keep a column out of the import completely — rules, saved templates and Claude "
                   "won't map it again. Picking a field here replaces whatever fed that field before — except "
                   "the phone field: each phone column you add gives customers one row per different number. "
                   "You can also write a note on what a column is, where it goes or how to read it — e.g. "
                   "*“this is the last visit date”*, *“dates are day-first”*. "
                   + ("Claude applies the notes when you click *Apply notes*. " if assistant else
                      "Claude isn't configured, so notes are saved with the template as documentation. ")
                   + "Everything here is saved with the template and the import.")
        warnings = column_warnings(schema, mapping, ss.profiles)
        samples = {p.name: p.samples for p in ss.profiles}
        excluded = set(mapping.excluded_columns)
        notes_df = pd.DataFrame([{
            "Column": c,
            "Sample values": " | ".join(samples.get(c, [])[:3]),
            COL_GOES: [schema.field(n).header for n in mapping.targets_of(c) if n in schema.field_names],
            COL_SKIP: c in excluded,
            COL_CHECK: ("⚠ " + warnings[c][1]) if c in warnings else ("Skipped" if c in excluded else ""),
            COL_NOTE: mapping.column_notes.get(c, ""),
        } for c in columns])
        nkey = f"notes_editor_{ss.editor_version}_{ss.get('notes_version', 0)}"
        st.data_editor(notes_df, key=nkey, on_change=on_columns_edit, args=(nkey, columns), hide_index=True,
                       num_rows="fixed", width="stretch", disabled=["Column", "Sample values", COL_CHECK],
                       column_config={
                           "Column": st.column_config.TextColumn(pinned=True),
                           "Sample values": st.column_config.TextColumn(width="medium"),
                           COL_GOES: st.column_config.MultiselectColumn(
                               options=[f.header for f in schema.mappable_fields], width="medium",
                               help="Field(s) this column goes to. Empty = not imported. Picking a field makes this "
                                    "column its only source."),
                           COL_SKIP: st.column_config.CheckboxColumn(
                               help="Leave this column out of the import and stop it from being mapped again."),
                           COL_CHECK: st.column_config.TextColumn(width="medium"),
                           COL_NOTE: st.column_config.TextColumn(width="large"),
                       })
        for msg in ss.pop("column_messages", None) or []:
            st.caption("✔️ " + msg)
        if warnings:
            st.warning("Check " + ", ".join(f"**{c}**" for c in warnings) + " — " +
                       ("its" if len(warnings) == 1 else "their") + " values don't fit the field "
                       + ("it goes" if len(warnings) == 1 else "they go") + " to. Clear **Goes to** or tick "
                       "**Skip** if that's a mistake.")
        gkey = f"general_note_{ss.file_sig[1][:10]}"
        st.text_area("General note (optional)", value=mapping.general_note, key=gkey, on_change=on_general_note,
                     args=(gkey,), height=68,
                     placeholder="e.g. all dates in this file are day-first · times are in Pacific time")

        b1, b2, _ = st.columns([1.3, 1.3, 2])
        n_notes = len([n for n in ss.mapping.column_notes.values() if n]) + bool(ss.mapping.general_note)
        apply_clicked = b1.button("Apply notes with Claude", type="primary", width="stretch",
                                  disabled=not (assistant and n_notes),
                                  help=None if assistant else "Set ANTHROPIC_API_KEY on the server to enable.")
        check_clicked = b2.button("Check for mistakes", width="stretch",
                                  help="Runs rule checks on the first 1,000 rows" +
                                       (" and asks Claude to review a sample." if assistant else "."))
        if apply_clicked:
            try:
                with st.spinner("Claude is applying your notes…"):
                    res = assistant.apply_notes(schema, ss.profiles, ss.mapping, ss.mapping.column_notes,
                                                ss.mapping.general_note)
                ss.notes_result = res
            except AIError as exc:
                st.error(str(exc))
        res = ss.get("notes_result")
        if res is not None:
            if res.summary:
                st.markdown(f"**Claude:** {res.summary}")
            if res.note_results:
                icon = {"applied": "✅ applied", "already_correct": "✔️ already correct", "not_applied": "⚠️ not applied"}
                st.dataframe(pd.DataFrame([{"Note on": r.get("column"), "Result": icon.get(r.get("status"), ""),
                                            "Details": r.get("message", "")} for r in res.note_results]),
                             hide_index=True, width="stretch",
                             column_config={"Details": st.column_config.TextColumn(width="large")})
            if res.dropped:
                st.caption("Ignored invalid suggestions: " + "; ".join(res.dropped[:5]))
            _show_changes(res.mapping, ss.mapping, columns, "notes_result")

        if check_clicked:
            head = df.head(1000)
            with st.spinner("Checking for mistakes" + (" (rules + Claude)…" if assistant else "…")):
                prev = convert(head, schema, ss.mapping, timezone=ss.get("timezone"))
                findings, ai_res, err = check_for_mistakes(
                    schema, ss.mapping, head, prev.audit, ss.profiles, assistant, use_claude=bool(assistant),
                    mask_phones=settings.ai_mask_phones, timezone_label=ZONE_LABEL.get(ss.get("timezone"), ""),
                    scope=f"preview of the first {len(head):,} rows before import")
            ss.check_result = {"findings": findings, "ai": ai_res, "error": err, "rows": len(head)}
        chk = ss.get("check_result")
        if chk is not None:
            st.markdown(f"**Mistake check** · first {chk['rows']:,} rows"
                        + (" · rules + Claude" if chk["ai"] else " · rules"))
            if chk["error"]:
                st.warning(f"Claude's review didn't run ({chk['error']}). Rule checks are below.")
            if chk["ai"] and chk["ai"].summary:
                st.markdown(f"**Claude:** {chk['ai'].summary}")
            _findings_table(chk["findings"], schema)
            if chk["ai"] and chk["ai"].mapping.fields and not chk.get("changes_done"):
                _show_changes(chk["ai"].mapping, ss.mapping, columns, "check_changes",
                              clear=lambda: chk.update(changes_done=True))


# ---------------------------------------------------------------------------
# Page: Imports (list + detail)
# ---------------------------------------------------------------------------

def page_imports() -> None:
    qid = st.query_params.get("id")
    if qid and str(qid).isdigit():
        page_import_detail(int(qid))
        return

    st.header("Imports")
    f1, f2, f3, f4 = st.columns([1.2, 2.6, 2, 0.8], vertical_alignment="bottom")
    who = f1.segmented_control("Show", ["Everyone", "Mine"], default="Everyone", key="imp_who")
    statuses = f2.pills("Status", ["running", "queued", "completed", "failed", "cancelled", "interrupted"],
                        selection_mode="multi", key="imp_status",
                        format_func=lambda s: s.capitalize())
    search = f3.text_input("Search", placeholder="file name, template or #id", key="imp_search")
    if f4.button("↻ Refresh", width="stretch"):
        st.rerun()

    active = store.active_imports()
    live = st.fragment(run_every=1.5 if active else None)(_import_list)
    live(who == "Mine", list(statuses or []), search, bool(active))


def _import_list(mine: bool, statuses: list[str], search: str, polling: bool) -> None:
    active = store.active_imports()
    if polling and not active:
        st.rerun(scope="app")  # everything finished: switch the page back to static mode

    if active:
        st.markdown(f"**In progress ({len(active)})**")
        for rec in active:
            with st.container(border=True):
                a, b = st.columns([5, 1.2], vertical_alignment="center")
                with a:
                    st.markdown(f"**#{rec.id} · {rec.filename}** · {rec.username} · {fmt_size(rec.file_size)}")
                    pct = int(rec.progress * 100)
                    if rec.status == "queued":
                        st.progress(0.0, text=f"Waiting for a free worker · queued {fmt_duration(rec.elapsed())}")
                    else:
                        st.progress(rec.progress, text=f"{pct}% · {rec.stage} · {fmt_duration(rec.elapsed())} "
                                                       f"elapsed · {fmt_eta(rec.eta())} left")
                with b:
                    if st.button("Open", key=f"open_{rec.id}", width="stretch"):
                        open_import(rec.id)
                    if st.button("Cancel", key=f"cancel_{rec.id}", width="stretch",
                                 disabled=rec.cancel_requested):
                        runner.cancel(rec.id)
                        st.toast(f"Cancelling import #{rec.id}…")

    recs = store.list_imports(limit=500, username=user["username"] if mine else None,
                              statuses=statuses or None, search=search or None)
    if not recs:
        st.info("No imports yet." if not (mine or statuses or search) else "No imports match these filters.")
        return
    table = pd.DataFrame([{
        "#": r.id,
        "File": r.filename,
        "By": r.username,
        "Status": status_text(r),
        "Progress": r.progress,
        "Started": fmt_dt(r.started_at or r.created_at, TZ),
        "Duration": fmt_duration(r.elapsed()),
        "Rows": r.total_rows,
        "Converted": r.imported,
        "Bad rows": r.rejected,
        "Duplicates": r.duplicates,
        "Template": r.template_name or "",
    } for r in recs])
    st.caption("Select a row to open the import.")
    event = st.dataframe(
        table, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
        key="imports_table",
        column_config={
            "#": st.column_config.NumberColumn(width="small", format="%d"),
            "Progress": st.column_config.ProgressColumn(min_value=0, max_value=1, format="percent", width="small"),
            "Rows": st.column_config.NumberColumn(format="localized"),
            "Converted": st.column_config.NumberColumn(format="localized"),
            "Bad rows": st.column_config.NumberColumn(format="localized"),
            "Duplicates": st.column_config.NumberColumn(format="localized"),
        },
    )
    sel = event.selection.rows if event and event.selection else []
    if sel:
        open_import(int(table.iloc[sel[0]]["#"]))


def page_import_detail(import_id: int) -> None:
    if st.button("← All imports"):
        st.query_params.clear()
        st.rerun()
    rec = store.get_import(import_id)
    if rec is None:
        st.error(f"Import #{import_id} not found.")
        return

    st.header(f"Import #{rec.id} · {rec.filename}")
    meta = [f"by **{rec.username}**", f"uploaded {fmt_dt(rec.created_at, TZ)}", fmt_size(rec.file_size),
            f"target: {rec.schema_label}"]
    if rec.template_name:
        meta.append(f"template: {rec.template_name}")
    if rec.ai_used:
        meta.append("Claude used")
    st.caption(" · ".join(meta))
    if rec.parent_id:
        if st.button(f"Re-run of import #{rec.parent_id} — open original", type="tertiary"):
            open_import(rec.parent_id)

    if rec.active:
        st.fragment(run_every=1)(_live_progress)(import_id)
        return

    if rec.status in ("failed", "cancelled", "interrupted"):
        st.badge(rec.status.capitalize(), color=status_color(rec.status))
        if rec.error:
            st.error(rec.error)
        else:
            st.warning(f"This import was {rec.status}.")
        _rerun_button(rec)
        return

    _completed_view(rec)


def _live_progress(import_id: int) -> None:
    rec = store.get_import(import_id)
    if rec is None or not rec.active:
        st.rerun(scope="app")
    st.badge(rec.status.capitalize(), color=status_color(rec.status))
    if rec.status == "queued":
        st.progress(0.0, text="Waiting for a free worker…")
    else:
        st.progress(rec.progress, text=f"{int(rec.progress * 100)}% · {rec.stage}")
    m = st.columns(4)
    m[0].metric("Elapsed", fmt_duration(rec.elapsed()))
    m[1].metric("Time left", fmt_eta(rec.eta()) if rec.status == "running" else "—")
    m[2].metric("Started", fmt_dt(rec.started_at, TZ, with_date=False) if rec.started_at else "not yet")
    m[3].metric("Progress", f"{int(rec.progress * 100)}%")
    if st.button("Cancel import", disabled=rec.cancel_requested):
        runner.cancel(rec.id)
        st.toast("Cancelling…")
    if rec.cancel_requested:
        st.caption("Cancelling…")


def _rerun_button(rec: ImportRecord) -> None:
    if rec.files_purged or not store.source_path(rec).exists():
        st.caption("The original file is no longer stored, so this import can't be re-run.")
        return
    if st.button("↻ Re-run this import", help="Runs the same original file and mapping again with the current "
                                              "target format. Creates a new import."):
        open_import(rerun_import(rec))


def _completed_view(rec: ImportRecord) -> None:
    rec_schema = TargetSchema.model_validate_json(rec.schema_json)
    s = rec.summary
    if rec.rejected:
        st.error(f"**{rec.rejected:,} bad row{'s were' if rec.rejected != 1 else ' was'} NOT imported.** "
                 "See *Side by side → Bad rows* or download *Rejected rows* for the reason on each row.  \n"
                 + s.get("message", ""), icon="⚠️")
    elif not rec.imported:
        st.error("No rows could be converted. " + s.get("message", ""))
    else:
        st.success(s.get("message", "Completed."), icon="✅")
    for w in s.get("warnings", []):
        st.caption("ℹ️ File note: " + w)

    m = st.columns(6)
    m[0].metric("Rows in file", f"{rec.total_rows:,}")
    extra = s.get("extra_rows", 0)
    m[1].metric("Converted", f"{rec.imported:,}",
                help=f"Includes {extra:,} extra row{'s' if extra != 1 else ''} for customers with more than one "
                     "phone number (one row per number)." if extra else None)
    m[2].metric("Bad rows", f"{rec.rejected:,}")
    m[3].metric("Duplicates removed", f"{rec.duplicates:,}")
    m[4].metric("Empty rows", f"{rec.blank_rows:,}")
    m[5].metric("Took", fmt_duration(rec.elapsed()))

    if rec.files_purged:
        st.info(f"The files for this import were deleted after {settings.file_retention_days} days. "
                "The counts above are kept.")
        return

    # Downloads
    folder = store.files_root / str(rec.id)
    d = st.columns(5)
    out_p = store.file_path(rec, "output")
    if out_p:
        d[0].download_button(f"⬇ Converted file ({rec.imported:,})", out_p.read_bytes(), out_p.name, type="primary",
                             width="stretch", on_click="ignore", disabled=not rec.imported)
    rej_p = store.file_path(rec, "rejects")
    if rej_p:
        n_rej = (rec.rejected or 0) + (rec.duplicates or 0)
        d[1].download_button(f"⬇ Rejected rows ({n_rej:,})", rej_p.read_bytes(),
                             rec.files.get("rejects_download", "rejected.csv"), mime="text/csv", width="stretch",
                             on_click="ignore", disabled=not n_rej)
    sum_p = store.file_path(rec, "summary")
    if sum_p:
        d[2].download_button("⬇ Summary", sum_p.read_bytes(), rec.files.get("summary_download", "summary.txt"),
                             mime="text/plain", width="stretch", on_click="ignore")
    src_p = store.source_path(rec)
    if src_p.exists():
        d[3].download_button("⬇ Original file", src_p.read_bytes(), rec.filename, width="stretch",
                             on_click="ignore")
    xkey = f"cmp_xlsx_{rec.id}"
    src_df, audit = load_import_tables(rec.id, str(folder))
    plan = column_plan(rec_schema, rec.mapping, list(src_df.columns))
    if xkey in ss:
        d[4].download_button("⬇ Side-by-side (Excel)", ss[xkey], f"{Path(rec.filename).stem}_side_by_side.xlsx",
                             width="stretch", on_click="ignore")
    elif d[4].button("Prepare side-by-side Excel", width="stretch"):
        with st.spinner("Building the side-by-side workbook…"):
            ss[xkey] = comparison_xlsx(src_df, audit, rec_schema, plan,
                                       f"Import #{rec.id} — {rec.filename} → {rec.schema_label}")
        st.rerun()

    tabs = st.tabs(["Side by side", "What changed", "Converted file", "Bad & duplicate rows", "Mapping used"])
    with tabs[0]:
        _side_by_side(rec, rec_schema, src_df, audit, plan)
    with tabs[1]:
        st.caption("How each target field was produced across all rows of the file.")
        st.dataframe(field_change_summary(audit, rec_schema, rec.mapping), hide_index=True, width="stretch",
                     column_config={"Examples": st.column_config.TextColumn(width="large")})
        reasons = [{"Problem": r, "Rows": c, "Effect": "row not imported"} for r, c in s.get("reasons", {}).items()]
        reasons += [{"Problem": r, "Rows": c, "Effect": "value left blank"} for r, c in s.get("blanked", {}).items()]
        reasons += [{"Problem": r, "Rows": c, "Effect": "extra number skipped (row imported with its other number)"}
                    for r, c in s.get("skipped", {}).items()]
        if reasons:
            st.markdown("**Problems found**")
            st.dataframe(pd.DataFrame(reasons), hide_index=True, width="stretch")
        fills = s.get("fill_rates", {})
        if fills:
            st.markdown("**How full each column is in the converted file**")
            st.dataframe(pd.DataFrame([{"Field": k, "Filled": v} for k, v in fills.items()]), hide_index=True,
                         width="stretch", column_config={
                             "Filled": st.column_config.ProgressColumn(min_value=0, max_value=1, format="percent")})
    with tabs[2]:
        imported = audit[audit["_status"] == "imported"]
        out = pd.DataFrame({f.header: imported[f"out.{f.name}"].values for f in rec_schema.fields})
        st.caption(f"{len(out):,} rows" + (" · showing the first 1,000" if len(out) > 1000 else ""))
        st.dataframe(out.head(1000), hide_index=True, width="stretch")
    with tabs[3]:
        bad = audit[audit["_status"].isin(["rejected", "duplicate"])]
        if bad.empty:
            st.caption("None.")
        else:
            view = pd.concat([
                pd.DataFrame({"File row": bad["_row"].values, "Status": bad["_status"].str.capitalize().values,
                              "Reason": bad["_reason"].values}),
                src_df.loc[bad.index].reset_index(drop=True)], axis=1)
            st.dataframe(view, hide_index=True, width="stretch",
                         column_config={"Reason": st.column_config.TextColumn(width="large")})
    with tabs[4]:
        st.dataframe(pd.DataFrame([{
            "Field": f.header, "Built from": rec.mapping.get(f.name).summary(),
            "Option": option_text(rec.mapping.get(f.name), f), "Mapped by": origin_label(rec.mapping.get(f.name)),
        } for f in rec_schema.fields]), hide_index=True, width="stretch")
        if rec.mapping.column_notes or rec.mapping.general_note:
            st.markdown("**Notes on columns**")
            notes_rows = [{"Column": c, "Note": n} for c, n in rec.mapping.column_notes.items()]
            if rec.mapping.general_note:
                notes_rows.append({"Column": "(general)", "Note": rec.mapping.general_note})
            st.dataframe(pd.DataFrame(notes_rows), hide_index=True, width="stretch")
        n_fix = sum(len(v) for v in rec.overrides.values())
        if n_fix:
            st.caption(f"{n_fix} value fix{'es' if n_fix != 1 else ''} approved from Claude suggestions were applied.")
        tz = rec.read_options.get("timezone")
        st.caption(f"Read options: sheet {rec.read_options.get('sheet') or '—'}, header row "
                   f"{'none' if rec.read_options.get('header_row') is None else rec.read_options['header_row'] + 1}"
                   f", client time zone {ZONE_LABEL.get(tz, tz or 'Eastern')}")

    st.divider()
    _import_check_section(rec, rec_schema, src_df, audit)
    a, b = st.columns([1, 3])
    with a:
        _rerun_button(rec)
    with b:
        _repair_section(rec, rec_schema, audit)


@st.cache_data(max_entries=6, show_spinner=False)
def _import_profiles(import_id: int, folder: str):
    src, _ = load_import_tables(import_id, folder)
    return profile_columns(src, n_samples=settings.ai_sample_values)


def _import_check_section(rec: ImportRecord, rec_schema: TargetSchema, src_df: pd.DataFrame,
                          audit: pd.DataFrame) -> None:
    ckey = f"import_check_{rec.id}"
    c1, c2 = st.columns([1.3, 3.7], vertical_alignment="center")
    if c1.button("Check this import for mistakes", key=f"btn_{ckey}", width="stretch"):
        profiles = _import_profiles(rec.id, str(store.files_root / str(rec.id)))
        with st.spinner("Checking for mistakes" + (" (rules + Claude)…" if assistant else "…")):
            findings, ai_res, err = check_for_mistakes(
                rec_schema, rec.mapping, src_df, audit, profiles, assistant, use_claude=bool(assistant),
                mask_phones=settings.ai_mask_phones,
                timezone_label=ZONE_LABEL.get(rec.read_options.get("timezone"), ""),
                scope=f"all {len(audit):,} rows of a finished import")
        ss[ckey] = {"findings": findings, "ai": ai_res, "error": err}
    c2.caption("Rule checks over every row" + (" plus a Claude review of a sample of rows (phone digits partly "
                                               "hidden)." if assistant else ". Set ANTHROPIC_API_KEY to add a "
                                               "Claude review."))
    chk = ss.get(ckey)
    if chk is None:
        return
    if chk["error"]:
        st.warning(f"Claude's review didn't run ({chk['error']}). Rule checks are below.")
    if chk["ai"] and chk["ai"].summary:
        st.markdown(f"**Claude:** {chk['ai'].summary}")
    _findings_table(chk["findings"], rec_schema)
    if chk["ai"] and chk["ai"].mapping.fields:
        diff = mapping_diff(rec_schema, rec.mapping, chk["ai"].mapping)
        if not diff.empty:
            st.markdown("**Claude's suggested mapping changes**")
            st.dataframe(diff, hide_index=True, width="stretch")
            can_rerun = not rec.files_purged and store.source_path(rec).exists()
            if st.button("Re-run the import with these changes", type="primary", disabled=not can_rerun,
                         key=f"rerun_{ckey}"):
                changed = Mapping(fields={k: v for k, v in chk["ai"].mapping.fields.items()
                                          if rec_schema.field(k).header in set(diff["Field"])})
                new_map = merge(rec.mapping, changed)
                open_import(rerun_import(rec, ai_used=True, mapping=new_map))


def _side_by_side(rec: ImportRecord, rec_schema: TargetSchema, src_df: pd.DataFrame, audit: pd.DataFrame,
                  plan) -> None:
    counts = status_counts(audit, rec_schema)
    c1, c2 = st.columns([3.2, 1.6], vertical_alignment="bottom")
    mode = c1.segmented_control(
        "Show", [k for k in FILTERS if counts.get(k, 0) or k in ("all", "changed")],
        format_func=lambda k: f"{FILTERS[k]} ({counts.get(k, 0):,})", default="all", key=f"cmp_mode_{rec.id}")
    search = c2.text_input("Find", placeholder="value or row number", key=f"cmp_q_{rec.id}")
    rows = filter_rows(audit, src_df, rec_schema, mode or "all", search)

    o1, o2, o3 = st.columns([1.6, 1, 2.4], vertical_alignment="bottom")
    show_was = o1.toggle("Show original under changed values", value=True, key=f"cmp_was_{rec.id}")
    per_page = o2.selectbox("Rows per page", [50, 100, 250], index=1, key=f"cmp_pp_{rec.id}")
    pages = max(1, -(-len(rows) // per_page))
    page = o3.number_input(f"Page (of {pages:,})", min_value=1, max_value=pages, value=1, step=1,
                           key=f"cmp_page_{rec.id}_{mode}_{search}_{per_page}")
    start = (page - 1) * per_page
    page_rows = rows[start:start + per_page]
    st.caption(f"Showing rows {start + 1 if page_rows else 0:,}–{start + len(page_rows):,} of {len(rows):,}. "
               "Left: the original file exactly as uploaded. Right: what was produced for the import. "
               "Headers show where each column went.")
    st.html(build_compare_html(src_df, audit, page_rows, rec_schema, plan, file_label=f"Original · {rec.filename}",
                               target_label=f"Converted · {rec.schema_label}", show_was=show_was))


def _repair_section(rec: ImportRecord, rec_schema: TargetSchema, audit: pd.DataFrame) -> None:
    failed = {k: v for k, v in rec.summary.get("failed_values", {}).items() if k in rec_schema.field_names}
    if not failed or not assistant:
        return
    n_vals = sum(len(v) for v in failed.values())
    pkey = f"repairs_{rec.id}"
    with st.expander(f"Try to fix {n_vals:,} unconvertible value{'s' if n_vals != 1 else ''} with Claude"):
        st.caption("Claude only reformats values (e.g. 'Jan 5th 24', '516 5OO 0100'). Every suggestion is re-checked "
                   "and suggestions that add or change data are refused. Approved fixes run as a new import.")
        if st.button("Suggest fixes", key=f"suggest_{rec.id}"):
            proposals = {}
            try:
                with st.spinner("Claude is reviewing the values…"):
                    for fname, values in failed.items():
                        f = rec_schema.field(fname)
                        good = [v for v in audit.loc[audit["_status"] == "imported", f"out.{fname}"].tolist() if v][:5]
                        pairs, _ = assistant.repair_values(f, values, good)
                        accepted, rejected = validate_repairs(rec_schema, rec.mapping, fname, pairs,
                                                              timezone=rec.read_options.get("timezone"))
                        fixed = set(accepted) | {r[0] for r in rejected}
                        proposals[fname] = {"accepted": accepted, "rejected": rejected,
                                            "unfixable": [v for v in values if v not in fixed]}
                ss[pkey] = proposals
            except AIError as exc:
                st.error(str(exc))
        props = ss.get(pkey)
        if props:
            rows = []
            for fname, p in props.items():
                h = rec_schema.field(fname).header
                rows += [{"Field": h, "Original": a, "Fix": b, "Status": "✓ will apply"} for a, b in p["accepted"].items()]
                rows += [{"Field": h, "Original": a, "Fix": b, "Status": f"✗ refused: {why}"}
                         for a, b, why in p["rejected"]]
                rows += [{"Field": h, "Original": a, "Fix": "", "Status": "can't be fixed"} for a in p["unfixable"]]
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
            n_ok = sum(len(p["accepted"]) for p in props.values())
            if n_ok and st.button(f"Apply {n_ok} fix{'es' if n_ok != 1 else ''} and re-run", type="primary",
                                  key=f"apply_{rec.id}"):
                new_id = rerun_import(rec, overrides={k: p["accepted"] for k, p in props.items() if p["accepted"]},
                                      ai_used=True)
                ss.pop(pkey, None)
                open_import(new_id)


# ---------------------------------------------------------------------------
# Pages: templates, target format, users
# ---------------------------------------------------------------------------

def page_templates() -> None:
    st.header("Saved templates")
    st.caption("A template remembers how one file layout maps to the target format. When a file with the same "
               "columns is uploaded, its template is applied automatically.")
    templates = store.list_templates(schema.name)
    if not templates:
        st.info("No templates yet. They are saved when you start an import with 'Remember this mapping' on.")
        return
    st.dataframe(pd.DataFrame([{
        "Name": t.name, "Columns": len(t.headers), "Times used": t.use_count, "Created by": t.created_by,
        "Updated": fmt_dt(t.updated_at, TZ), "Updated by": t.updated_by,
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


def page_schema() -> None:
    st.header("Target format")
    st.markdown(f"**{schema.label}** — {schema.description}")
    st.caption(f"Defined in `{settings.schema_path}`. Edit that file on the server to change the target format; "
               "the app picks up changes automatically.")
    def _notes(f) -> str:
        if f.fixed_blank:
            return "🔒 always blank on every row — can't be mapped or edited"
        bits = []
        if f.no_year_value is not None:
            bits.append(f"month/day with no year → year {f.no_year_value}")
        if f.type == "datetime" and f.format:
            bits.append("date-only values get 12:00 AM")
        if f.type == "phone":
            bits.append("allowed: " + ", ".join(f.countries))
        return "; ".join(bits)

    st.dataframe(pd.DataFrame([{
        "#": i + 1, "Output header": f.header, "Type": "always blank" if f.fixed_blank else f.type,
        "Required": "yes" if f.required else "", "Format": f.format or "",
        "If invalid": "" if f.fixed_blank else ("reject row" if f.on_invalid == "reject" else "leave blank"),
        "Notes": _notes(f),
    } for i, f in enumerate(schema.fields)]), hide_index=True, width="stretch")
    o = schema.output
    st.markdown(f"**Output file:** {o.format.upper()}"
                + (f", delimiter `{repr(o.delimiter)}`, encoding {o.encoding}" if o.format == "csv" else "")
                + (", header row included" if o.include_header else ", no header row"))
    if schema.dedupe.keys:
        st.markdown("**Duplicates:** a row is removed only when **all** of these match an earlier row: "
                    + ", ".join(f"`{schema.field(k).header}`" for k in schema.dedupe.keys)
                    + ". Rows with the same phone but any different date are kept.")
    st.markdown(f"**Stored files:** each import's original and converted files are kept for "
                f"{settings.file_retention_days} days" if settings.file_retention_days > 0
                else "**Stored files:** kept indefinitely")


def page_users() -> None:
    st.header("Users")
    users = store.list_users()
    st.dataframe(pd.DataFrame([{
        "User": u["username"], "Admin": "yes" if u["is_admin"] else "", "Created": fmt_dt(u["created_at"], TZ),
        "Last sign-in": fmt_dt(u["last_login"], TZ)} for u in users]), hide_index=True, width="stretch")
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


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------

_landing = os.getenv("DEFAULT_PAGE", "new")  # "new" or "imports": which page opens first
PAGE_NEW = st.Page(page_new_import, title="New import", icon="⬆️", url_path="new", default=_landing != "imports")
PAGE_IMPORTS = st.Page(page_imports, title="Imports", icon="📋", url_path="imports", default=_landing == "imports")
PAGE_TEMPLATES = st.Page(page_templates, title="Templates", icon="🧩", url_path="templates")
PAGE_SCHEMA = st.Page(page_schema, title="Target format", icon="🎯", url_path="target-format")
PAGE_USERS = st.Page(page_users, title="Users", icon="👥", url_path="users")

nav = st.navigation([PAGE_NEW, PAGE_IMPORTS, PAGE_TEMPLATES, PAGE_SCHEMA] + ([PAGE_USERS] if user["is_admin"] else []))

with st.sidebar:
    running = store.active_imports()
    if running:
        st.caption(f"🔵 {len(running)} import{'s' if len(running) != 1 else ''} in progress")
    st.divider()
    st.caption(f"Signed in as **{user['username']}**" + (" (admin)" if user["is_admin"] else ""))
    if settings.auth_mode != "none":
        with st.expander("My account"):
            with st.form("pw_self", clear_on_submit=True):
                p1 = st.text_input("New password", type="password")
                if st.form_submit_button("Change password"):
                    try:
                        store.set_password(user["username"], p1)
                        st.success("Changed.")
                    except Exception as exc:
                        st.error(str(exc))
            if st.button("Sign out"):
                logout()
                st.rerun()
    st.caption("Claude: " + (f"connected ({settings.mapping_model})" if assistant else "not configured"))
    st.caption(f"Target: {schema.label} · v{__version__}")

nav.run()
