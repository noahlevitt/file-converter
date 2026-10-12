"""Orchestration shared by the web UI and the CLI: initial mapping, merging AI changes, validating repairs.

Overview
--------
This module glues the deterministic pieces (rules in ``heuristics``, saved templates in ``storage``, the engine's
checks in ``checks``) to the optional Claude assistant in ``ai``. Both ``app.py`` and ``cli.py`` call these
functions so the web app and the command line behave the same way.

Main entry points
    * :func:`make_assistant` — build a :class:`~converter.ai.ClaudeAssistant` if AI is configured, else ``None``.
    * :func:`initial_mapping` — the first mapping proposed for an uploaded file. The order of preference is:

        1. **Exact template** — a saved template whose header set matches this file exactly (similarity 1.0) is
           reused as-is. No rules, no AI call: a layout the team already reviewed is trusted.
        2. **Similar template** — if a saved template's headers are similar enough (``Store.find_template``
           default: at least 75% Jaccard overlap of normalized header names), its field mappings are laid over
           the rule-based mapping for every column that still exists in this file.
        3. **Rules** — ``heuristics.auto_map`` matches headers and sampled value types to target fields; used for
           every field the similar template didn't cover (or for everything when there is no template).
        4. **Claude (optional)** — when ``use_ai`` is true and an assistant exists, Claude refines the result of
           steps 2–3. Only headers and a few sample values per column are sent. If the call fails, the rule-based
           mapping is returned with a note. In the web app this step runs only with ``AI_AUTO_MAP=true``; in the
           CLI only with ``--ai``.

      Files without a header row don't use templates at all (never matched, never saved; see
      ``Store.find_template`` / ``Store.save_template``).
    * :func:`template_mapping` — a saved template's mapping fitted to the current file.
    * :func:`merge` — apply a partial mapping (e.g. Claude's suggested changes) on top of an existing one.
    * :func:`validate_repairs` — double-check Claude's "Suggest fixes" value repairs before they are offered.
    * :func:`check_for_mistakes` — rule checks plus an optional Claude review of a conversion.

Nothing here writes to disk or the database; the functions return new objects and leave their inputs (including
saved templates) unchanged. The only external side effect is the optional Claude API call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from . import normalizers as N
from .ai import AIError, AIReviewResult, ClaudeAssistant, accept_repair
from .checks import Finding, build_review_payload, run_checks
from .config import Settings
from .engine import source_rows
from .heuristics import auto_map
from .mapping import ColumnProfile, FieldMapping, Mapping, has_generic_columns
from .schema import TargetSchema
from .storage import Store, Template


@dataclass
class MappingSuggestion:
    """The result of :func:`initial_mapping`, as shown at the top of the mapping step.

    Attributes:
        mapping: The proposed mapping, already normalized against the schema and the file's columns.
        source: Where it came from — "template" (exact template match), "rules" (rules, possibly laid over a similar
            template) or "rules+ai" (rules/similar template refined by Claude).
        template: The exact or most similar saved template that was used, or ``None``.
        similarity: Header similarity (0.0–1.0) to ``template``; 1.0 for an exact match. When no template passed
            the threshold this may still hold the best score seen (for information only).
        notes: Plain-language messages for the user (template matched, Claude's notes, ignored suggestions, AI
            failures).
        ai_usage: Token usage reported by the Claude call (empty when Claude wasn't called).
    """

    mapping: Mapping
    source: str  # "template" | "rules" | "rules+ai"
    template: Template | None = None
    similarity: float = 0.0
    notes: list[str] = field(default_factory=list)
    ai_usage: dict[str, int] = field(default_factory=dict)


def make_assistant(settings: Settings) -> ClaudeAssistant | None:
    """Create the Claude assistant from settings, or return ``None`` when AI is unavailable.

    AI is unavailable when ``AI_ENABLED`` is false or ``ANTHROPIC_API_KEY`` is empty (``Settings.ai_available``).
    Callers treat ``None`` as "rules only" and hide or disable the Claude buttons. Creating the assistant builds an
    Anthropic client but makes no API call, so this is cheap; the web app caches one instance per process.
    """
    if not settings.ai_available:
        return None
    return ClaudeAssistant(
        api_key=settings.anthropic_api_key,
        mapping_model=settings.mapping_model,
        repair_model=settings.repair_model,
        sample_values=settings.ai_sample_values,
    )


def merge(base: Mapping, changes: Mapping) -> Mapping:
    """Apply field changes on top of base; base keeps its notes.

    Every field present in ``changes`` replaces the base's mapping for that field entirely (sources, transform,
    params, origin, confidence and reason); fields absent from ``changes`` are kept from ``base``. Column notes,
    the general note and excluded (skipped) columns always come from ``base`` — Claude's change sets never carry
    notes, and the reviewer's notes must survive an AI round-trip.

    Example: base maps ``birthday <- "DOB"``; changes map ``birthday <- "Birthday"`` and ``anniversary <- "Anniv"``
    -> result maps both from ``changes``, every other field from ``base``.

    Deep copies are made, so neither input is modified. The result is NOT normalized; callers call
    ``.normalized(schema, columns)`` afterwards to drop invalid sources and fill in missing fields.
    """
    fields = {k: v.model_copy(deep=True) for k, v in base.fields.items()}
    for k, v in changes.fields.items():
        fields[k] = v.model_copy(deep=True)
    return Mapping(fields=fields, column_notes=dict(base.column_notes), general_note=base.general_note,
                   excluded_columns=list(base.excluded_columns))


def initial_mapping(schema: TargetSchema, df: pd.DataFrame, profiles: list[ColumnProfile], store: Store | None,
                    assistant: ClaudeAssistant | None, use_ai: bool = True) -> MappingSuggestion:
    """Propose the first mapping for a freshly read file: exact template → similar template → rules → Claude.

    Args:
        schema: The target import format.
        df: The file as read (only its column names are used here).
        profiles: Column profiles (sample values and type shares) from ``mapping.profile_columns``; used by the
            rules and sent to Claude.
        store: Template storage, or ``None`` to skip template matching (e.g. in tests).
        assistant: Claude assistant from :func:`make_assistant`, or ``None`` for rules only.
        use_ai: Whether Claude may refine the mapping. The web app passes ``AI_AUTO_MAP``; the CLI passes ``--ai``.

    Returns:
        A :class:`MappingSuggestion`. Each field's ``origin`` records where it came from ("template", "rule", "ai")
        so the UI can show "Mapped by: Template / Rules / Claude".

    Side effects: one network call to Claude when step 3 runs; none otherwise (templates are only read, their
    use count is not incremented here).
    """
    columns = list(df.columns)

    # 1. Saved template for this exact header set → reuse, no AI call.
    # find_template compares a fingerprint of normalized header names (case/punctuation-insensitive, order
    # ignored), so "Phone #" and "phone" count as the same header and a re-ordered export still matches.
    if store is not None:
        tpl, sim = store.find_template(columns, schema.name)
        if tpl is not None and sim == 1.0:
            return MappingSuggestion(template_mapping(schema, tpl, columns), "template", tpl, sim,
                                     [f"Matched saved template '{tpl.name}'."])

    # 2. Rules.
    rules = auto_map(schema, profiles).normalized(schema, columns)
    notes: list[str] = []
    if has_generic_columns(columns):
        notes.append("This file has no header row: templates aren't used for it, and columns can only be "
                     "recognised from their values (phone numbers). Choose where each column goes under Columns in "
                     "your file.")
    near_tpl, near_sim = (None, 0.0)
    if store is not None:
        # Same lookup as step 1; here it can only return a *similar* template (>= the similarity threshold) or None.
        near_tpl, near_sim = store.find_template(columns, schema.name)
        if near_tpl is not None:
            # Similar layout seen before: start from that template for the columns that still exist.
            # Template decisions beat rule guesses because a person reviewed them; fields the template left
            # unmapped (or whose columns are gone) keep the rule-based mapping.
            tm = near_tpl.mapping.normalized(schema, columns)
            manual = {f.name for f in schema.fields if f.manual_only}
            for name, fm in tm.fields.items():
                if fm.transform != "none" and name not in manual:  # a person maps those for each file
                    fm.origin = "template"
                    rules.fields[name] = fm
            # Notes: rule notes win on a clash; the template's general note is used when the rules have none.
            rules.column_notes = {**tm.column_notes, **rules.column_notes}
            rules.general_note = rules.general_note or tm.general_note
            if tm.excluded_columns:
                # Columns the reviewer marked "skip" in the template stay skipped; re-normalize so any rule that
                # read from them is dropped.
                rules.excluded_columns = list(tm.excluded_columns)
                rules = rules.normalized(schema, columns)
            notes.append(f"Columns are {near_sim:.0%} similar to saved template '{near_tpl.name}'; "
                         "its rules were applied where the columns still exist.")

    # 3. Claude refines the mapping (only headers + a few sample values are sent).
    # Claude sees the rule/template mapping as a starting point; its answer is validated (unknown fields, columns
    # or transforms are dropped and reported in res.dropped), merged on top, and normalized again.
    if use_ai and assistant is not None:
        try:
            res = assistant.propose_mapping(schema, profiles, current=rules)
            merged = merge(rules, res.mapping).normalized(schema, columns)
            if res.notes:
                notes.append(f"Claude: {res.notes}")
            if res.dropped:
                # Show at most five so a badly confused answer doesn't flood the page.
                notes.append("Ignored invalid suggestions: " + "; ".join(res.dropped[:5]))
            return MappingSuggestion(merged, "rules+ai", near_tpl, near_sim, notes, res.usage)
        except AIError as exc:
            # Claude is a nice-to-have: any API/validation failure falls back to the rule-based mapping.
            notes.append(f"AI mapping unavailable ({exc}); using rule-based mapping.")
    return MappingSuggestion(rules, "rules", near_tpl, near_sim, notes)


def template_mapping(schema: TargetSchema, tpl: Template, columns: list[str]) -> Mapping:
    """A saved template's mapping fitted to this file: ``Mapping.normalized``, every mapped field marked "template".

    Used for an exact template match (:func:`initial_mapping`) and by the CLI's ``--template``. Columns the template
    uses that the file lacks are dropped, current rules are enforced (phones one row per number), hand-mapped-only
    fields (consent, last_contacted) are never filled from a template, and the saved template itself is not
    changed.
    """
    m = tpl.mapping.normalized(schema, columns)
    for fm in m.fields.values():
        if fm.transform != "none":
            fm.origin = "template"
    # Normalizing again drops hand-mapped-only fields (consent, last_contacted) now that they aren't "user" choices.
    return m.normalized(schema, columns)


def validate_repairs(schema: TargetSchema, mapping: Mapping, field_name: str, pairs: list[tuple[str, str]],
                     timezone: str | None = None) -> tuple[dict[str, str], list[tuple[str, str, str]]]:
    """Return ({raw: final_output}, [(raw, suggestion, why_rejected)]).

    Used by "Suggest fixes": Claude proposes ``(raw value, repaired value)`` pairs for values of ``field_name``
    that failed to convert. Each suggestion must pass two independent gates before it is offered to the user:

    1. ``ai.accept_repair`` — the repair may only *reformat* the original, never invent data. For example a phone
       repair must reuse the original's digits in order (no added digits), an email must stay very close to the
       original spelling. Failing -> rejected with "changes the data instead of reformatting it".
    2. :func:`_validate_final` — the repaired value must convert with the same normalizer the engine uses.
       Failing -> rejected with the normalizer's error (e.g. an impossible date such as 02/30).

    Accepted values are returned already in final output form, keyed by the raw value, ready to be stored as the
    import's ``overrides`` (the engine then outputs them without normalizing again).

    Args:
        schema: Target schema containing ``field_name``.
        mapping: Current mapping (its params for the field, e.g. ``date_order``, are used for validation).
        field_name: Target field the values belong to.
        pairs: Claude's ``(raw, suggestion)`` pairs.
        timezone: IANA time zone of the import, for datetime fields.

    No side effects.
    """
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
    """Normalize one repaired value for field ``f``; return ``(final_output, error)`` (error ``None`` if valid).

    Dates/datetimes: if the suggestion is already in the field's output format (``f.format``, e.g. "%m/%d/%Y" for
    birthday in the real schema; "%Y-%m-%d" / "%Y-%m-%d %H:%M:%S" when the field sets none), it is accepted as-is.
    Why: re-parsing a single value such as "03/04/2026" on its own could not tell month-first from day-first, so
    trusting the exact output format avoids a silent day/month swap. Otherwise the value goes through the normal
    date pipeline (``N.prepare`` on this one value, then ``N.normalize``), with the import's time zone when given.

    Other types: normalized with an empty context, i.e. without file-level evidence such as the file's other
    numbers — a repaired phone must be valid on its own.
    """
    if f.type in ("date", "datetime"):
        out_fmt = f.format or ("%Y-%m-%d %H:%M:%S" if f.type == "datetime" else "%Y-%m-%d")
        try:
            dt = datetime.strptime(value.strip(), out_fmt)
            return dt.strftime(out_fmt), None
        except ValueError:
            pass  # not already in output format -> fall through to the full parser
        ctx = N.prepare(f, [value], params)
        if timezone:
            ctx["tz"] = timezone
        return N.normalize(value, f, params, ctx)
    return N.normalize(value, f, params, {})


def empty_field_mapping() -> FieldMapping:
    """Return an unmapped field ("Leave blank / default") marked as the user's own choice.

    ``origin="user"`` distinguishes "the reviewer deliberately cleared this field" from "nothing matched"
    (origin ""), which the UI shows in the "Mapped by" column ("You").
    """
    return FieldMapping(origin="user")


def check_for_mistakes(schema: TargetSchema, mapping: Mapping, source: pd.DataFrame, audit: pd.DataFrame,
                       profiles: list[ColumnProfile], assistant: ClaudeAssistant | None, use_claude: bool,
                       mask_phones: bool = True, timezone_label: str = "", scope: str = ""
                       ) -> tuple[list[Finding], AIReviewResult | None, str | None]:
    """Rule checks always; Claude's review on top when requested and configured. Returns (findings, ai, error).

    Args:
        schema, mapping: The target format and the mapping that produced ``audit``.
        source: Source rows (the whole file, or a preview sample) as text.
        audit: The engine's per-row audit table for those rows (``ConversionResult.audit``).
        profiles: Column profiles, sent to Claude for context.
        assistant: Claude assistant or ``None``.
        use_claude: True when Claude should review too (the app passes True whenever an assistant is configured).
        mask_phones: Partly mask phone digits in the sample rows sent to Claude (``AI_MASK_PHONES``).
        timezone_label: Human label of the import's time zone, used in the finding about times that carried their
            own time zone and were converted.
        scope: Text describing what the rows cover (e.g. "preview of the first 1,000 rows before import" or "all
            12,000 rows of a finished import"), passed into Claude's payload so it can judge the stats.

    Returns:
        ``(findings, ai_result, error)``: rule findings plus any Claude findings (marked ``source="claude"``);
        Claude's full result (including suggested mapping changes) or ``None``; and an error message when the
        Claude call failed (the rule findings are still returned in that case).

    Side effects: at most one Claude API call. Nothing is stored.
    """
    # With 'One row per phone number' the audit can have more rows than the source; line them up row for row.
    source = source_rows(source, audit)
    findings = run_checks(schema, mapping, source, audit, profiles, timezone_label=timezone_label)
    if not (use_claude and assistant is not None):
        return findings, None, None
    # The payload is a small sample of original-vs-converted rows plus the rule findings, never the whole file.
    payload = build_review_payload(schema, mapping, source, audit, findings, mask_phones=mask_phones,
                                   stats_scope=scope)
    try:
        res = assistant.review(schema, profiles, mapping, payload)
    except AIError as exc:
        return findings, None, str(exc)
    # Convert Claude's loosely-typed findings into Finding objects so the UI lists them with the rule findings.
    for x in res.findings:
        findings.append(Finding(x.get("severity", "info"), x.get("field", ""), x.get("column", ""),
                                x.get("issue", ""), x.get("suggestion", ""), source="claude"))
    return findings, res, None
