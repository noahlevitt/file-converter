"""Orchestration shared by the web UI and the CLI: initial mapping, merging AI changes, validating repairs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from . import normalizers as N
from .ai import AIError, AIReviewResult, ClaudeAssistant, accept_repair
from .checks import Finding, build_review_payload, run_checks
from .config import Settings
from .heuristics import auto_map
from .mapping import ColumnProfile, FieldMapping, Mapping
from .schema import TargetSchema
from .storage import Store, Template


@dataclass
class MappingSuggestion:
    mapping: Mapping
    source: str  # "template" | "rules" | "rules+ai"
    template: Template | None = None
    similarity: float = 0.0
    notes: list[str] = field(default_factory=list)
    ai_usage: dict[str, int] = field(default_factory=dict)


def make_assistant(settings: Settings) -> ClaudeAssistant | None:
    if not settings.ai_available:
        return None
    return ClaudeAssistant(
        api_key=settings.anthropic_api_key,
        mapping_model=settings.mapping_model,
        repair_model=settings.repair_model,
        sample_values=settings.ai_sample_values,
    )


def merge(base: Mapping, changes: Mapping) -> Mapping:
    """Apply field changes on top of base; base keeps its notes."""
    fields = {k: v.model_copy(deep=True) for k, v in base.fields.items()}
    for k, v in changes.fields.items():
        fields[k] = v.model_copy(deep=True)
    return Mapping(fields=fields, column_notes=dict(base.column_notes), general_note=base.general_note,
                   excluded_columns=list(base.excluded_columns))


def initial_mapping(schema: TargetSchema, df: pd.DataFrame, profiles: list[ColumnProfile], store: Store | None,
                    assistant: ClaudeAssistant | None, use_ai: bool = True) -> MappingSuggestion:
    columns = list(df.columns)

    # 1. Saved template for this exact header set → reuse, no AI call.
    if store is not None:
        tpl, sim = store.find_template(columns, schema.name)
        if tpl is not None and sim == 1.0:
            m = tpl.mapping.normalized(schema, columns)
            for fm in m.fields.values():
                if fm.transform != "none":
                    fm.origin = "template"
            return MappingSuggestion(m, "template", tpl, sim, [f"Matched saved template '{tpl.name}'."])

    # 2. Rules.
    rules = auto_map(schema, profiles).normalized(schema, columns)
    notes: list[str] = []
    near_tpl, near_sim = (None, 0.0)
    if store is not None:
        near_tpl, near_sim = store.find_template(columns, schema.name)
        if near_tpl is not None:
            # Similar layout seen before: start from that template for the columns that still exist.
            tm = near_tpl.mapping.normalized(schema, columns)
            for name, fm in tm.fields.items():
                if fm.transform != "none":
                    fm.origin = "template"
                    rules.fields[name] = fm
            rules.column_notes = {**tm.column_notes, **rules.column_notes}
            rules.general_note = rules.general_note or tm.general_note
            if tm.excluded_columns:
                rules.excluded_columns = list(tm.excluded_columns)
                rules = rules.normalized(schema, columns)
            notes.append(f"Columns are {near_sim:.0%} similar to saved template '{near_tpl.name}'; "
                         "its rules were applied where the columns still exist.")

    # 3. Claude refines the mapping (only headers + a few sample values are sent).
    if use_ai and assistant is not None:
        try:
            res = assistant.propose_mapping(schema, profiles, current=rules)
            merged = merge(rules, res.mapping).normalized(schema, columns)
            if res.notes:
                notes.append(f"Claude: {res.notes}")
            if res.dropped:
                notes.append("Ignored invalid suggestions: " + "; ".join(res.dropped[:5]))
            return MappingSuggestion(merged, "rules+ai", near_tpl, near_sim, notes, res.usage)
        except AIError as exc:
            notes.append(f"AI mapping unavailable ({exc}); using rule-based mapping.")
    return MappingSuggestion(rules, "rules", near_tpl, near_sim, notes)


def validate_repairs(schema: TargetSchema, mapping: Mapping, field_name: str, pairs: list[tuple[str, str]],
                     timezone: str | None = None) -> tuple[dict[str, str], list[tuple[str, str, str]]]:
    """Return ({raw: final_output}, [(raw, suggestion, why_rejected)])."""
    f = schema.field(field_name)
    params = mapping.get(field_name).params
    accepted: dict[str, str] = {}
    rejected: list[tuple[str, str, str]] = []
    for raw, suggestion in pairs:
        if not accept_repair(f, raw, suggestion):
            rejected.append((raw, suggestion, "changes the data instead of reformatting it"))
            continue
        final, err = _validate_final(f, suggestion, params, timezone)
        if err:
            rejected.append((raw, suggestion, err))
        else:
            accepted[raw] = final
    return accepted, rejected


def _validate_final(f, value: str, params: dict, timezone: str | None = None) -> tuple[str, str | None]:
    if f.type in ("date", "datetime"):
        out_fmt = f.format or ("%Y-%m-%d %H:%M:%S" if f.type == "datetime" else "%Y-%m-%d")
        try:
            dt = datetime.strptime(value.strip(), out_fmt)
            return dt.strftime(out_fmt), None
        except ValueError:
            pass
        ctx = N.prepare(f, [value], params)
        if timezone:
            ctx["tz"] = timezone
        return N.normalize(value, f, params, ctx)
    return N.normalize(value, f, params, {})


def empty_field_mapping() -> FieldMapping:
    return FieldMapping(origin="user")


def check_for_mistakes(schema: TargetSchema, mapping: Mapping, source: pd.DataFrame, audit: pd.DataFrame,
                       profiles: list[ColumnProfile], assistant: ClaudeAssistant | None, use_claude: bool,
                       mask_phones: bool = True, timezone_label: str = "", scope: str = ""
                       ) -> tuple[list[Finding], AIReviewResult | None, str | None]:
    """Rule checks always; Claude's review on top when requested and configured. Returns (findings, ai, error)."""
    findings = run_checks(schema, mapping, source, audit, profiles, timezone_label=timezone_label)
    if not (use_claude and assistant is not None):
        return findings, None, None
    payload = build_review_payload(schema, mapping, source, audit, findings, mask_phones=mask_phones,
                                   stats_scope=scope)
    try:
        res = assistant.review(schema, profiles, mapping, payload)
    except AIError as exc:
        return findings, None, str(exc)
    for x in res.findings:
        findings.append(Finding(x.get("severity", "info"), x.get("field", ""), x.get("column", ""),
                                x.get("issue", ""), x.get("suggestion", ""), source="claude"))
    return findings, res, None
