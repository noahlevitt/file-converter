"""Claude integration.

Claude is optional. Everything it suggests is shown for approval first:
  1. propose_mapping   – map unfamiliar columns onto the target schema (only when AI_AUTO_MAP=true)
  2. apply_notes       – turn reviewer notes on columns ("this is the last visit", "goes to anniversary",
                         "dates are day-first") into mapping changes
  3. review            – look for mistakes in a mapping or a finished import (optional button)
  4. repair_values     – suggest fixes for values the deterministic parsers rejected (optional button)

Claude is only called when a user clicks one of those buttons (plus automatic mapping if AI_AUTO_MAP=true).

Claude never touches the full file and never writes code: it only picks transforms and parameters from a
fixed registry, and every value it suggests is re-validated by the deterministic normalizers.

Responsibilities
----------------
* Build the prompts (system prompt generated from the target schema and the transform registry) and the JSON
  Schemas that constrain Claude's answers (structured outputs).
* Make the API calls (:class:`ClaudeAssistant`), turning every failure into :class:`AIError`.
* Validate what comes back before anything else sees it (:func:`parse_mapping_entries`, :func:`accept_repair`).

Main entry points
-----------------
* :class:`ClaudeAssistant` - ``propose_mapping``, ``apply_notes``, ``review``, ``repair_values``. Created by
  ``services.make_assistant`` only when an API key is configured (and ``AI_ENABLED`` isn't false); otherwise
  "Apply notes" is disabled, "Suggest fixes" is hidden, "Check for mistakes" runs rule checks only, and the tool
  works fully without Claude.
* :func:`parse_mapping_entries` - validate mapping entries returned by Claude (also used by tests).
* :func:`accept_repair` - refuse value "repairs" that add or change data (used by ``services.validate_repairs``).
* :func:`build_system_prompt` - the shared system prompt for the three mapping-related calls.

What is sent to Claude
----------------------
Never the full file. Per call:

* mapping / notes / review: the column headers with, per column, the fill count, the number of distinct values,
  up to ``sample_values`` distinct sample values (``AI_SAMPLE_VALUES``, default 15; each cut to 120 characters by
  ``mapping.profile_columns``) and which value types the column looks like. Columns the reviewer marked "skip"
  are left out entirely. Plus the current mapping and the reviewer's notes.
* review ("Check for mistakes") additionally: the payload built by ``checks.build_review_payload`` - up to 30
  sample rows (original next to converted values), per-field cell-status counts, row-status counts, unused
  columns and the rule-based findings. Phone digits in those sample rows are partly masked (``516#####00``)
  unless ``AI_MASK_PHONES=false``. (The per-column sample values above are not masked.)
* repair ("Suggest fixes"): only the distinct values of one field that failed to convert (at most
  ``MAX_REPAIR_VALUES``) and up to 5 output values from that field that converted fine, as a format example.

How answers are constrained and validated
-----------------------------------------
1. Structured outputs: every call passes a JSON Schema (``output_config.format``), so the answer is JSON of a
   known shape. Enums limit names to the schema's *mappable* fields (fixed-blank fields such as consent can't
   even be named), the registry's transforms, the file's non-skipped columns and the known parameter names.
2. :func:`parse_mapping_entries` re-checks every entry anyway (names resolved case-insensitively, fixed-blank
   fields refused, transform/source/parameter combinations checked with ``transforms.check_transform``) and
   reports what it dropped, so the reviewer can see it.
3. Callers merge the result onto the current mapping and run ``Mapping.normalized`` again (``services.py``).
4. Nothing is applied until a person accepts it. Repairs additionally pass :func:`accept_repair` and the
   deterministic normalizers (``services.validate_repairs``); approved repairs are passed to the engine as
   ``overrides`` for a re-run of the import.

Why Claude never transforms data directly
-----------------------------------------
Claude chooses *how* to convert (a mapping); ``engine.py`` does the converting, deterministically, for every
row. That keeps customer data on the server, makes results reproducible and reusable as templates, keeps cost
and time independent of file size, and means Claude can't invent or alter values in the import: the worst a bad
suggestion can do is propose a wrong mapping, which a person sees and the rule checks look for.

Cost and latency settings
-------------------------
* Models: ``mapping_model`` (default ``claude-sonnet-5-5``, ``MAPPING_MODEL``) for mapping, notes and review;
  the cheaper ``repair_model`` (default ``claude-haiku-5-5``, ``REPAIR_MODEL``) for value repairs.
* Effort: "medium" for mapping, notes and review (they need judgment), "low" for repairs (plain reformatting).
* Prompt caching: the system prompt is sent as one block marked ``cache_control: ephemeral``. It depends only on
  the target schema, so consecutive mapping/notes/review calls for the same schema can read it from the cache
  instead of paying for it again; the cache-read token count is returned in each call's usage.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from rapidfuzz import fuzz

from .mapping import ColumnProfile, FieldMapping, Mapping
from .schema import TYPE_DESCRIPTIONS, FieldSpec, TargetSchema
from .transforms import ADDRESS_PARTS, DATE_ORDERS, NAME_PARTS, TRANSFORMS, check_transform

# Most distinct failed values sent in one "Suggest fixes" call per field: bounds prompt size, cost and the
# length of the answer (one entry per value must fit in max_tokens). Further values stay unconvertible.
MAX_REPAIR_VALUES = 200
# Pseudo column name for the reviewer's general note (not about one column) in apply_notes; it is added to the
# allowed `column` values of note_results so Claude can report on it like any column note.
GENERAL_NOTE = "(general note)"


class AIError(Exception):
    """Raised when a Claude call fails or returns something unusable.

    Covers network/API errors, a refusal, an answer cut off at max_tokens, an empty answer and malformed JSON.
    The message is written for end users. Callers catch it and carry on without Claude: ``services`` falls back to
    the rule-based mapping or returns the rule findings only, and the app shows the message.
    """


@dataclass
class AIMappingResult:
    """Result of :meth:`ClaudeAssistant.propose_mapping` (automatic mapping: ``AI_AUTO_MAP=true`` or CLI ``--ai``)."""
    mapping: Mapping  # only the fields Claude proposed / changed
    notes: str  # Claude's remarks for the reviewer (ambiguities, columns left unused on purpose)
    usage: dict[str, int] = field(default_factory=dict)  # input/output/cache-read token counts of the call
    dropped: list[str] = field(default_factory=list)  # entries rejected during validation


@dataclass
class AINotesResult:
    """Result of :meth:`ClaudeAssistant.apply_notes`: proposed mapping changes plus one outcome per reviewer note."""
    mapping: Mapping                       # only the fields that change
    note_results: list[dict[str, str]]     # {column, status, message} per note
    summary: str  # one or two plain sentences for the reviewer
    usage: dict[str, int] = field(default_factory=dict)  # token counts of the call (empty when no call was made)
    dropped: list[str] = field(default_factory=list)  # proposed changes rejected during validation, with reasons


@dataclass
class AIReviewResult:
    """Result of :meth:`ClaudeAssistant.review` ("Check for mistakes"): findings plus optional mapping changes."""
    findings: list[dict[str, str]]         # {severity, field, column, issue, suggestion}
    mapping: Mapping                       # proposed field changes (may be empty)
    summary: str  # overall verdict in plain words ("nothing looks wrong" when there are no findings)
    usage: dict[str, int] = field(default_factory=dict)  # token counts of the call
    dropped: list[str] = field(default_factory=list)  # proposed changes rejected during validation, with reasons


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def _transform_docs() -> str:
    """Describe every transform in the registry for the system prompt, one bullet per transform.

    Generated from ``transforms.TRANSFORMS`` (name, how many source columns it takes, its parameters and its
    description), so the prompt can never offer Claude a transform the engine doesn't have, or miss a new one.
    """
    lines = []
    for t in TRANSFORMS.values():
        src = "no sources" if t.max_sources == 0 else (
            f"{t.min_sources}+ sources" if t.max_sources is None else f"exactly {t.min_sources} source")
        params = f"; params: {', '.join(t.params)}" if t.params else ""
        lines.append(f"- {t.name} ({src}{params}): {t.description}")
    return "\n".join(lines)


def _schema_docs(schema: TargetSchema) -> str:
    """Describe the target schema for the system prompt, generated from the schema YAML.

    One line per *mappable* field: internal name, output header, type, required/optional, output format, allowed
    values, default, description, the no-year rule (month/day without a year is output with e.g. year 1600) and
    synonyms. Fixed-blank fields (consent) are listed separately as never mappable, with an explicit rule that
    consent/opt-in/subscription columns must not be mapped anywhere. The dedupe keys are mentioned so Claude
    understands which fields decide duplicates.
    """
    lines = []
    for f in schema.mappable_fields:
        bits = [f"type={f.type} ({TYPE_DESCRIPTIONS[f.type]})", "REQUIRED" if f.required else "optional"]
        if f.format:
            bits.append(f"output format={f.format}")
        if f.allowed_values:
            bits.append("allowed values=" + ", ".join(f.allowed_values))
        if f.default:
            bits.append(f"default when empty={f.default}")
        line = f"- {f.name} [header \"{f.header}\"]: " + "; ".join(bits)
        if f.description:
            line += f". {f.description}"
        if f.no_year_value is not None:
            line += f". Month and day without a year are output with year {f.no_year_value} automatically"
        if f.synonyms:
            line += f". Also called: {', '.join(f.synonyms)}"
        lines.append(line)
    fixed = [f.name for f in schema.fields if f.fixed_blank]
    if fixed:
        lines.append(f"\nAlways blank and not mappable (the engine forces them empty; do not return entries for them): "
                     f"{', '.join(fixed)}. Source columns about consent, opt-in, subscription or opt-out status must "
                     "NOT be mapped to any field.")
    if schema.dedupe.keys:
        lines.append(f"\nRows are de-duplicated when all of these fields match: {', '.join(schema.dedupe.keys)}.")
    return "\n".join(lines)


def build_system_prompt(schema: TargetSchema) -> str:
    """The system prompt shared by ``propose_mapping``, ``apply_notes`` and ``review``.

    It explains the task (Claude maps columns, a deterministic engine applies the mapping, a person reviews it),
    then lists the target schema (:func:`_schema_docs`), the transforms (:func:`_transform_docs`), the parameters
    and the rules: copy names exactly, prefer leaving a field blank to guessing, send every phone column to the
    phone field with each_row (one row per different number, never coalesce), never map columns *about* phones
    (preference, type, opt-in) as phone numbers, cells with several numbers are split automatically (not a
    problem to report), never invent data or touch consent.

    It depends only on the schema - nothing file-specific goes in here - so it is identical across calls and can
    be served from the prompt cache (see ``ClaudeAssistant._call``). File details go in the user message.
    """
    return f"""You map the columns of an uploaded customer/contact file onto a fixed target import schema.
A deterministic engine then applies your mapping to every row; a person reviews your mapping first.

TARGET SCHEMA "{schema.label}":
{_schema_docs(schema)}

TRANSFORMS (how a target field gets its raw value; the engine then normalizes and validates by field type):
{_transform_docs()}

PARAMETERS:
- part: for name_part one of {', '.join(NAME_PARTS)}; for address_part one of {', '.join(ADDRESS_PARTS)}
- date_order: {', '.join(DATE_ORDERS)} — set DMY only when sample dates show day-first (a first number above 12).
  Excel serial numbers, ISO dates and month names are handled automatically.
- input_format: optional Python strptime pattern when the date format is unusual
- region: ISO country for phone numbers written without a country code (default US)
- separator / index: for split and concat
- value_map: list of {{from, to}} pairs for map_values (e.g. translate Y/N or codes into allowed values)
- value: for constant

RULES:
- Use only the target field names, transforms and source column names given. Copy column names exactly.
- Map a column only when its header or its sample values support it. When nothing fits, use transform "none".
  Mapping a wrong column is worse than leaving a field blank — the reviewer can fill gaps.
- Each source column normally feeds one target field. Exceptions: a full-name column may feed first and last
  name via name_part; a single-line address column may feed street/unit/city/state/zip via address_part.
- Several phone columns (Phone, Phone 2, Mobile, Home…): put all of them on the phone field with each_row, so
  every different number becomes its own row (never coalesce for phones). Columns ABOUT a phone (e.g. phoneMarketingPreference, Phone Type,
  SMS Opt In) are not phone numbers — never map them to the phone field.
- A phone cell holding several numbers ("516-555-0100 / 917-555-1234") is split by the engine into one row per
  number automatically; map that column normally and don't report it as a problem.
- Never invent data. Use constant only when the user explicitly asks for a fixed value. Never use constant
  or a guessed column for consent, opt-in or subscription fields.
- confidence: high = header and values clearly match; medium = probable; low = a guess the reviewer must check.
- reason: one short sentence a non-technical reviewer can understand.
"""


def _profile_payload(profiles: list[ColumnProfile], n_samples: int) -> list[dict[str, Any]]:
    """What Claude sees of the file: one small summary per column, never the rows themselves.

    Per column: name, how many cells are filled ("filled/total"), the number of distinct values, up to
    ``n_samples`` distinct sample values (already cut to 120 characters by ``profile_columns``), and
    ``looks_like``: the share of sampled values that look like a phone/email/date/ZIP/state.
    """
    out = []
    for p in profiles:
        # Only mention types at least 20% of the sampled values look like; lower shares are mostly noise.
        evidence = {k: v for k, v in p.shares.items() if v >= 0.2}
        out.append({
            "column": p.name,
            "filled": f"{p.non_empty}/{p.total}",
            "unique_values": p.unique,
            "samples": p.samples[:n_samples],
            "looks_like": evidence,
        })
    return out


def _mapping_payload(mapping: Mapping) -> dict[str, Any]:
    """The current mapping in compact JSON form: {field: {transform, sources, params}} for mapped fields only.

    Unmapped fields are omitted (the prompts say "fields not listed are blank") to keep the message short.
    Origin, confidence and reason are internal bookkeeping and are not sent.
    """
    return {
        name: {"transform": fm.transform, "sources": fm.sources, "params": fm.params}
        for name, fm in mapping.fields.items()
        if fm.transform != "none"
    }


# The functions below build JSON Schemas for structured outputs: the API guarantees Claude's answer is JSON
# matching the schema. Enums are filled from the target schema and the file's columns, so Claude can only name
# real fields, transforms and columns. "additionalProperties": false everywhere keeps out unexpected keys. The
# answers are still validated afterwards (parse_mapping_entries), since some rules (e.g. how many source columns a
# transform takes) can't be expressed in the schema.
def _output_schema(schema: TargetSchema, columns: list[str]) -> dict[str, Any]:
    """Answer format for ``propose_mapping``: ``{mappings: [mapping item...], notes: str}``.

    ``columns`` are the source columns Claude may use (skipped columns already removed).
    """
    return {
        "type": "object",
        "properties": {"mappings": {"type": "array", "items": _mapping_item_schema(schema, columns)},
                       "notes": {"type": "string"}},
        "required": ["mappings", "notes"],
        "additionalProperties": False,
    }


def _notes_schema(schema: TargetSchema, columns: list[str]) -> dict[str, Any]:
    """Answer format for ``apply_notes``: ``{changes: [mapping item...], note_results: [...], summary: str}``.

    ``note_results`` has one entry per reviewer note: the column it was about (one of ``columns`` or
    ``GENERAL_NOTE``), a status (applied / already_correct / not_applied) and a short message, so the reviewer
    gets feedback on every note, including the ones that couldn't be applied.
    """
    return {
        "type": "object",
        "properties": {
            "changes": {"type": "array", "items": _mapping_item_schema(schema, columns)},
            "note_results": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "column": {"type": "string", "enum": [*columns, GENERAL_NOTE]},
                    "status": {"type": "string", "enum": ["applied", "already_correct", "not_applied"]},
                    "message": {"type": "string"},
                },
                "required": ["column", "status", "message"],
                "additionalProperties": False,
            }},
            "summary": {"type": "string"},
        },
        "required": ["changes", "note_results", "summary"],
        "additionalProperties": False,
    }


def _review_schema(schema: TargetSchema, columns: list[str]) -> dict[str, Any]:
    """Answer format for ``review``: ``{findings: [...], changes: [mapping item...], summary: str}``.

    Each finding has a severity (error / warning / info), the target field and/or source column it is about
    (``""`` when not applicable), the issue and a suggestion. ``changes`` holds mapping fixes Claude is
    confident about; the reviewer can accept them.
    """
    return {
        "type": "object",
        "properties": {
            "findings": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["error", "warning", "info"]},
                    "field": {"type": "string", "enum": [*(f.name for f in schema.mappable_fields), ""]},
                    "column": {"type": "string", "enum": [*columns, ""]},
                    "issue": {"type": "string"},
                    "suggestion": {"type": "string"},
                },
                "required": ["severity", "field", "column", "issue", "suggestion"],
                "additionalProperties": False,
            }},
            "changes": {"type": "array", "items": _mapping_item_schema(schema, columns)},
            "summary": {"type": "string"},
        },
        "required": ["findings", "changes", "summary"],
        "additionalProperties": False,
    }


def _mapping_item_schema(schema: TargetSchema, columns: list[str]) -> dict[str, Any]:
    """JSON Schema of one mapping entry, shared by the mapping, notes and review answers.

    target_field: one of the schema's mappable fields (fixed-blank fields such as consent are not in the enum).
    transform: one of the registry's transforms. sources: a list of the given source columns.
    params: only the known parameter names, with enums where the values are a fixed set (name/address part,
    date order). confidence (high/medium/low) and reason are shown to the reviewer.
    Every top-level key is required, so Claude always states a confidence and a reason; nothing inside ``params``
    is required, so parameters a transform doesn't use are simply left out.
    """
    params_props: dict[str, Any] = {
        "part": {"type": "string", "enum": sorted(set(NAME_PARTS) | set(ADDRESS_PARTS))},
        "separator": {"type": "string"},
        "index": {"type": "integer"},
        "date_order": {"type": "string", "enum": list(DATE_ORDERS)},
        "input_format": {"type": "string"},
        "region": {"type": "string"},
        "value": {"type": "string"},
        "value_map": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"from": {"type": "string"}, "to": {"type": "string"}},
                "required": ["from", "to"],
                "additionalProperties": False,
            },
        },
    }
    item = {
        "type": "object",
        "properties": {
            "target_field": {"type": "string", "enum": [f.name for f in schema.mappable_fields]},
            "transform": {"type": "string", "enum": list(TRANSFORMS)},
            "sources": {"type": "array", "items": {"type": "string", "enum": columns}},
            "params": {"type": "object", "properties": params_props, "additionalProperties": False},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "reason": {"type": "string"},
        },
        "required": ["target_field", "transform", "sources", "params", "confidence", "reason"],
        "additionalProperties": False,
    }
    return item


# Answer format for repair_values: {repairs: [{original, repaired}]}. "repaired" is "" when a value can't be fixed
# without guessing. Fixed (not built per call) because it doesn't depend on the schema or the file.
REPAIR_SCHEMA = {
    "type": "object",
    "properties": {
        "repairs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"original": {"type": "string"}, "repaired": {"type": "string"}},
                "required": ["original", "repaired"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["repairs"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

def _without_skipped(profiles: list[ColumnProfile], current: Mapping | None) -> list[ColumnProfile]:
    """Columns the reviewer marked "skip" are not shown to Claude, so it can't map them again.

    Removing them from the profiles also removes them from the ``columns`` enum of the answer schema, so Claude
    can neither see their values nor name them as a source.
    """
    skipped = set(current.excluded_columns) if current else set()
    return [p for p in profiles if p.name not in skipped]


class ClaudeAssistant:
    """The four Claude operations of the tool. Each method makes exactly one API call (or none when there is
    nothing to ask) and returns validated suggestions; none of them changes a mapping, a file or the database.

    Use ``services.make_assistant(settings)`` to create one; it returns None when there is no API key or
    ``AI_ENABLED=false``.
    """
    def __init__(self, api_key: str, mapping_model: str = "claude-sonnet-5-5",
                 repair_model: str = "claude-haiku-5-5", sample_values: int = 15, client: Any = None):
        """Set up the assistant.

        api_key: Anthropic API key (``ANTHROPIC_API_KEY``). Not used when ``client`` is given.
        mapping_model: model for propose_mapping, apply_notes and review (``MAPPING_MODEL``).
        repair_model: model for repair_values (``REPAIR_MODEL``); a cheaper model is enough for reformatting.
        sample_values: how many distinct sample values per column to send (``AI_SAMPLE_VALUES``).
        client: an object with ``messages.create(**kwargs)``. Tests pass a fake client here; the real
                ``anthropic`` package is only imported when none is given, so the app runs without it when
                Claude isn't configured. The real client retries twice and times out after 180 seconds.
        """
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=api_key, max_retries=2, timeout=180)
        self.client = client
        self.mapping_model = mapping_model
        self.repair_model = repair_model
        self.sample_values = sample_values

    # -- low-level ----------------------------------------------------------
    def _call(self, *, model: str, system: str, user: str, schema: dict, effort: str, max_tokens: int
              ) -> tuple[dict, dict[str, int]]:
        """Send one request to the Messages API and return (parsed JSON answer, token usage).

        model / max_tokens: chosen per operation by the caller.
        system: the system prompt, sent as a single text block marked for prompt caching.
        user: the file-specific message (column profiles, mapping, notes, review sample or failed values).
        schema: JSON Schema the answer must follow (structured outputs), so the reply is parsed as plain JSON.
        effort: how much effort the model spends on the answer - "medium" for mapping/notes/review, "low" for
                repairs (cheaper and faster).

        Usage is returned as {input_tokens, output_tokens, cache_read_tokens} for cost reporting.
        Raises AIError for any failure (request error, refusal, cut-off or empty answer, invalid JSON), so callers
        only need to handle one exception type. The answer's *content* is not validated here - callers do that.
        """
        try:
            resp = self.client.messages.create(
                model=model,
                max_tokens=max_tokens,
                # cache_control marks the end of the cacheable prefix: an identical system prompt sent again soon
                # after (same schema) is read from the prompt cache at a lower price and latency.
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                # Effort level plus structured output: the answer must be JSON matching `schema`.
                output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            )
        except Exception as exc:  # anthropic.APIError and network errors
            raise AIError(f"Claude request failed: {exc}") from exc

        # A refusal or a cut-off answer can't be trusted to be complete JSON, so both are errors, not partial data.
        stop = getattr(resp, "stop_reason", None)
        if stop == "refusal":
            raise AIError("Claude declined this request.")
        if stop == "max_tokens":
            raise AIError("Claude's answer was cut off (too many columns?). Try again or map manually.")
        # The JSON answer is in the first text block (other block types, if any, are ignored).
        text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), None)
        if not text:
            raise AIError("Claude returned no answer.")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AIError("Claude returned malformed JSON.") from exc
        # getattr with defaults: a client (e.g. a fake one in tests) may not report usage or cache reads.
        u = getattr(resp, "usage", None)
        usage = {
            "input_tokens": getattr(u, "input_tokens", 0) or 0,
            "output_tokens": getattr(u, "output_tokens", 0) or 0,
            "cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        }
        return data, usage

    # -- mapping ------------------------------------------------------------
    def propose_mapping(self, schema: TargetSchema, profiles: list[ColumnProfile],
                        current: Mapping | None = None) -> AIMappingResult:
        """Ask Claude for a full mapping of the file's columns onto the schema (automatic mapping).

        Only called by ``services.initial_mapping`` when no saved template matches the file's headers exactly,
        and only when allowed: in the web app with ``AI_AUTO_MAP=true``, in the CLI with ``--ai``. ``current`` is
        the rule-based (or near-template) first pass; it is shown to Claude as a starting point that may contain
        mistakes, and its skipped columns are hidden from Claude.

        Sends: column profiles (headers + sample values) and the first-pass mapping. Claude must return one entry
        per mappable field (transform "none" when nothing fits), hence the larger max_tokens.
        Returns only the validated entries; the caller merges them onto ``current`` and re-normalizes.
        Raises AIError (the caller then keeps the rule-based mapping).
        """
        profiles = _without_skipped(profiles, current)
        columns = [p.name for p in profiles]
        user = (
            "SOURCE FILE COLUMNS (with sample values):\n"
            + json.dumps(_profile_payload(profiles, self.sample_values), ensure_ascii=False, indent=1)
        )
        if current and current.fields:
            user += ("\n\nA rule-based first pass produced this mapping (it may contain mistakes):\n"
                     + json.dumps(_mapping_payload(current), ensure_ascii=False))
        user += ("\n\nReturn exactly one entry for every mappable target field. Put anything the reviewer should know "
                 "(ambiguities, columns you left unused on purpose) in notes; keep notes under 80 words.")
        data, usage = self._call(model=self.mapping_model, system=build_system_prompt(schema), user=user,
                                 schema=_output_schema(schema, columns), effort="medium", max_tokens=16000)
        mapping, dropped = parse_mapping_entries(data.get("mappings", []), schema, columns)
        return AIMappingResult(mapping=mapping, notes=str(data.get("notes", "")).strip(), usage=usage,
                               dropped=dropped)

    def apply_notes(self, schema: TargetSchema, profiles: list[ColumnProfile], current: Mapping,
                    column_notes: dict[str, str], general_note: str = "") -> AINotesResult:
        """Turn the reviewer's notes ("this column is X", "goes to Y", "dates are day-first") into mapping changes.

        Behind the "Apply notes with Claude" button. The notes are treated as authoritative: the reviewer knows the
        file better than the headers do.

        column_notes: {source column: note}. Notes on skipped columns or columns not in the file are ignored.
        general_note: an optional note about the whole file, sent under the ``GENERAL_NOTE`` pseudo column.

        Sends: column profiles, the current mapping and the notes. Returns only the fields whose mapping changes
        (validated), one result per note (applied / already_correct / not_applied with a reason) and a summary.
        When there are no non-empty notes, returns immediately without calling Claude (no cost).
        Raises AIError.
        """
        profiles = _without_skipped(profiles, current)
        columns = [p.name for p in profiles]
        notes = [{"column": c, "note": n.strip()} for c, n in column_notes.items() if n.strip() and c in columns]
        if general_note.strip():
            notes.append({"column": GENERAL_NOTE, "note": general_note.strip()})
        if not notes:
            return AINotesResult(Mapping(), [], "No notes to apply.")
        # Changes are requested as full definitions of the changed fields only, so unchanged fields can't be
        # disturbed by accident; "stop using a column" is expressed as transform "none" on that field.
        user = (
            "SOURCE FILE COLUMNS (with sample values):\n"
            + json.dumps(_profile_payload(profiles, self.sample_values), ensure_ascii=False, indent=1)
            + "\n\nCURRENT MAPPING (target field → transform/sources/params; fields not listed are blank):\n"
            + json.dumps(_mapping_payload(current), ensure_ascii=False, indent=1)
            + "\n\nREVIEWER NOTES (each is about one source column, or general):\n"
            + json.dumps(notes, ensure_ascii=False, indent=1)
            + "\n\nThe reviewer knows this file. Treat each note as authoritative about what the column contains, "
              "where it goes, or how to read it, and change the mapping to match. Return in `changes` ONLY the "
              "target fields whose mapping must change (full new definition each); to stop using a column for a "
              "field, set that field's transform to \"none\". A column that should not be imported simply stays "
              "unused. Give exactly one `note_results` entry per note: applied, already_correct, or not_applied "
              "with the reason (e.g. the format has no such field, or consent is always blank). `summary`: one or "
              "two plain sentences."
        )
        data, usage = self._call(model=self.mapping_model, system=build_system_prompt(schema), user=user,
                                 schema=_notes_schema(schema, columns), effort="medium", max_tokens=8000)
        mapping, dropped = parse_mapping_entries(data.get("changes", []), schema, columns)
        # Defensive: keep only well-formed note results even though the answer schema already requires objects.
        results = [r for r in data.get("note_results", []) if isinstance(r, dict)]
        return AINotesResult(mapping, results, str(data.get("summary", "")).strip(), usage, dropped)

    def review(self, schema: TargetSchema, profiles: list[ColumnProfile], current: Mapping,
               payload: dict[str, Any]) -> AIReviewResult:
        """Look for mistakes in a mapping/conversion, using a sample of original vs converted rows.

        Behind "Check for mistakes" (on the New import preview and on finished imports). Called by
        ``services.check_for_mistakes`` after the rule checks, which are passed in ``payload["rule_findings"]`` so
        Claude can add to them instead of repeating them.

        payload: built by ``checks.build_review_payload`` - row-status counts, mapping summary, unused columns,
                 cell-status counts per field, rule findings and up to 30 sample rows (original vs converted).
                 When ``payload["phones_masked"]`` is true, phone digits in the samples are partly hidden and the
                 prompt tells Claude so (so it doesn't report the "#" as a problem).

        Sends: column profiles, the current mapping, the reviewer notes (if any) and the payload.
        Returns the findings (severity, field/column, issue, suggestion), mapping changes Claude is confident
        about (validated; may be empty) and a summary. Raises AIError.
        """
        profiles = _without_skipped(profiles, current)
        columns = [p.name for p in profiles]
        user = (
            "SOURCE FILE COLUMNS (with sample values):\n"
            + json.dumps(_profile_payload(profiles, self.sample_values), ensure_ascii=False, indent=1)
            + "\n\nCURRENT MAPPING (field → transform/sources/params):\n"
            + json.dumps(_mapping_payload(current), ensure_ascii=False, indent=1)
            + (f"\n\nREVIEWER NOTES: {json.dumps(current.column_notes, ensure_ascii=False)}"
               + (f" General: {current.general_note}" if current.general_note else "")
               if current.column_notes or current.general_note else "")
            + "\n\nCONVERSION RESULTS (sample rows show the original values next to what was produced; "
              + ("digits in phone numbers are partly masked with #; " if payload.get("phones_masked") else "")
              + "rule_findings were already found automatically):\n"
            + json.dumps(payload, ensure_ascii=False, indent=1, default=str)
            + "\n\nFind mistakes a careful reviewer would catch: a column mapped to the wrong field; a useful column "
              "left unused; activity columns mixed up (calls vs transactions vs contacted vs inbound SMS vs "
              "redemptions vs spins); birthday vs anniversary swapped; day/month swapped; wrong century; times "
              "shifted; values that converted but look implausible. Each finding: severity (error = data will be "
              "wrong, warning = probably wrong, info = worth a look), the field and/or column, the issue and a "
              "concrete suggestion. Don't repeat rule_findings unless you add something new. Put mapping changes "
              "in `changes` only when you are confident (full definition per changed field), otherwise leave it "
              "empty. If nothing looks wrong, return no findings and say so in `summary`."
        )
        data, usage = self._call(model=self.mapping_model, system=build_system_prompt(schema), user=user,
                                 schema=_review_schema(schema, columns), effort="medium", max_tokens=12000)
        mapping, dropped = parse_mapping_entries(data.get("changes", []), schema, columns)
        # Drop malformed or empty findings (a finding without an issue text tells the reviewer nothing).
        findings = [x for x in data.get("findings", []) if isinstance(x, dict) and x.get("issue")]
        return AIReviewResult(findings, mapping, str(data.get("summary", "")).strip(), usage, dropped)

    # -- value repair -------------------------------------------------------
    def repair_values(self, f: FieldSpec, values: list[str], good_examples: list[str] | None = None
                      ) -> tuple[list[tuple[str, str]], dict[str, int]]:
        """Return Claude's suggested (original, repaired) pairs. Callers must re-validate each suggestion.

        Behind "Suggest fixes" on a finished import, once per field that has unconvertible values.

        f: the target field the values belong to (its type, output format and allowed values are described).
        values: the raw values that failed to convert (``ConversionResult.failed_values[field]``). Empty values
                and repeats are removed and at most ``MAX_REPAIR_VALUES`` are sent; the rest stay unfixed.
        good_examples: a few output values of the same field that converted fine (up to 5 are sent), so Claude
                       sees what a correct value looks like.

        Claude is told to only *reformat* (e.g. "Jan 5th 24", "516-5OO-0100") and to return "" for anything it
        would have to guess. Uses the cheaper repair model with low effort. Returns ([(original, repaired)], usage),
        keeping only pairs whose original is one of the values sent and whose repair is non-empty; ([], {})
        without calling Claude when there is nothing to send. Raises AIError.

        The caller (``services.validate_repairs``) must then check each pair with :func:`accept_repair` and run the
        repaired value through the deterministic normalizer; a person approves the survivors, which the engine
        then applies as ``overrides`` when the import is re-run.
        """
        values = list(dict.fromkeys(v for v in values if v))[:MAX_REPAIR_VALUES]
        if not values:
            return [], {}
        # Target format to describe: the field's own format, else the normalizer's default for its type.
        fmt = f.format or {"date": "%Y-%m-%d", "datetime": "%Y-%m-%d %H:%M:%S", "phone": "E164"}.get(f.type, "")
        # Separate short system prompt: repairs don't need the schema or the transform registry.
        system = (
            "You fix formatting problems in spreadsheet values so a strict parser can read them. "
            "Rules: only reformat what is already in the value — never invent, guess or complete missing digits, "
            "letters, dates or parts. If a value cannot be fixed without guessing, return an empty string for it. "
            "Return one entry per input value with the original copied exactly."
        )
        user = (
            f"Target field: {f.name} — {f.type} ({TYPE_DESCRIPTIONS[f.type]})"
            + (f", output format {fmt}" if fmt else "")
            + (f", allowed values: {', '.join(f.allowed_values)}" if f.allowed_values else "")
            + (f".\nValues in this column that parsed fine look like: {', '.join(good_examples[:5])}"
               if good_examples else "")
            + "\n\nValues that failed to parse (JSON list):\n"
            + json.dumps(values, ensure_ascii=False)
        )
        data, usage = self._call(model=self.repair_model, system=system, user=user, schema=REPAIR_SCHEMA,
                                 effort="low", max_tokens=8000)
        # Ignore entries for values we didn't send (Claude must copy the original exactly) and "can't fix" ("").
        allowed = set(values)
        pairs = [(r["original"], r["repaired"].strip()) for r in data.get("repairs", [])
                 if r.get("original") in allowed and r.get("repaired", "").strip()]
        return pairs, usage


# ---------------------------------------------------------------------------
# Validation of Claude output
# ---------------------------------------------------------------------------

def parse_mapping_entries(entries: list[dict], schema: TargetSchema, columns: list[str]
                          ) -> tuple[Mapping, list[str]]:
    """Validate mapping entries from Claude and turn them into a Mapping; never trust the answer blindly.

    entries: the ``mappings`` / ``changes`` list from Claude's answer (dicts shaped like the mapping item schema).
    columns: the source columns Claude was allowed to use (skipped columns excluded).

    Structured outputs already constrain names, but this is the authoritative check (it also protects against a
    misbehaving model or client and enforces rules the JSON Schema can't express). Per entry:

    * target field and column names are matched case-insensitively and replaced by their real spelling;
    * unknown fields, fixed-blank fields (consent) and unknown transforms are dropped;
    * unknown source columns are dropped (and reported); repeated sources are kept once;
    * empty parameters are removed; "none" and "constant" never have sources;
    * a multi-column transform given a single column becomes a plain copy (as ``Mapping.normalized`` does);
    * the transform/source count/parameters combination must pass ``transforms.check_transform``.

    Returns (Mapping with the valid entries, marked ``origin="ai"``, a confidence and a reason of at most 300
    characters; list of human-readable reasons for everything dropped, shown to the reviewer).
    """
    fields_ci = {n.lower(): n for n in schema.field_names}
    cols_ci = {c.lower(): c for c in columns}
    out: dict[str, FieldMapping] = {}
    dropped: list[str] = []
    for e in entries:
        target = fields_ci.get(str(e.get("target_field", "")).lower())
        transform = str(e.get("transform", "")).lower()
        if target is None:
            dropped.append(f"unknown target field '{e.get('target_field')}'")
            continue
        if schema.field(target).fixed_blank:
            dropped.append(f"{target}: always blank, can't be mapped")
            continue
        if transform not in TRANSFORMS:
            dropped.append(f"{target}: unknown transform '{transform}'")
            continue
        sources = []
        unknown = False
        for s in e.get("sources", []) or []:
            real = cols_ci.get(str(s).lower())
            if real is None:
                dropped.append(f"{target}: unknown column '{s}'")
                unknown = True
            elif real not in sources:
                sources.append(real)
        params = {k: v for k, v in (e.get("params") or {}).items() if v not in (None, "", [])}
        if transform in ("none", "constant"):
            sources = []
        # A multi-column transform left with one column (e.g. after dropping an unknown one) is just a copy.
        if transform in ("coalesce", "concat", "each_row") and len(sources) == 1:
            transform = "copy"
        problem = check_transform(transform, sources, params)
        if problem:
            # When an unknown column caused the problem it was already reported above; don't report it twice.
            if not unknown:
                dropped.append(f"{target}: {problem}")
            continue
        # A missing or unexpected confidence is treated as "low", so the reviewer is asked to check it.
        conf = str(e.get("confidence", "")).lower()
        out[target] = FieldMapping(
            sources=sources, transform=transform, params=params, origin="ai",
            confidence=conf if conf in ("high", "medium", "low") else "low",
            reason=str(e.get("reason", "")).strip()[:300],
        )
    return Mapping(fields=out), dropped


def accept_repair(f: FieldSpec, original: str, repaired: str) -> bool:
    """Guard against Claude inventing data: a repaired phone must reuse the original's digits, etc.

    Returns True when ``repaired`` is plausibly a *reformatting* of ``original`` for field ``f``:

    * phone: the original must contain at least 10 digits (after reading spelled-out digits and look-alike letters
      as digits, see :func:`_phone_letters_to_digits`), and the repaired number's last 10 digits must appear in
      the original in the same order. Claude may drop characters (a duplicate country code, an extension) but
      never add, change or reorder a digit.
    * email: at least 80% similar to the original, ignoring case (spaces are removed from the original first, so
      a repair that only deletes stray spaces scores as identical).
    * zip: the repaired ZIP's first 5 digits must appear in the original's digits (padded to 5 with leading zeros,
      so "7030" may become "07030").
    * string: at least 70% similar to the original, ignoring case.
    * other types (dates, states, numbers, enums…): accepted here; the caller re-runs the deterministic
      normalizer on the repaired value, which rejects anything that doesn't convert.

    An empty repair ("can't fix") is never accepted. Similarity is ``rapidfuzz.fuzz.ratio`` (0-100).
    """
    if not repaired:
        return False
    if f.type == "phone":
        od = re.sub(r"\D", "", _phone_letters_to_digits(original))
        rd = re.sub(r"\D", "", repaired)
        rd10 = rd[-10:]
        # Every digit of the repaired number must already be in the original, in order; Claude may not add any.
        return len(od) >= 10 and len(rd10) == 10 and _is_subsequence(rd10, od)
    if f.type == "email":
        return fuzz.ratio(original.lower().replace(" ", ""), repaired.lower()) >= 80
    if f.type in ("zip",):
        return re.sub(r"\D", "", repaired)[:5] in re.sub(r"\D", "", original).zfill(5)
    if f.type in ("string",):
        return fuzz.ratio(original.lower(), repaired.lower()) >= 70
    return True


# Spelled-out digits as people type or dictate them ("five one six…", "oh" for zero), matched as whole words.
_NUMBER_WORDS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
                 "six": "6", "seven": "7", "eight": "8", "nine": "9"}


def _phone_letters_to_digits(value: str) -> str:
    """Read the digits a person meant in a phone value, for :func:`accept_repair`'s "no new digits" check.

    Spelled-out digits become digits ("five one six" -> "5 1 6"), and inside numeric-looking chunks the letters
    commonly typed for digits do too (O/o -> 0, l/I -> 1), so "516 555-O1OO" counts as containing 5165550100.
    Without this, a correct repair of such a value would look like it added digits and be refused.
    Only used for that comparison; the original value itself is not changed anywhere.

    Limitation: a chunk is a run of at least 4 digits / look-alike letters / ``().-`` characters, and spaces end
    a chunk. A 3-character group between spaces (the "5OO" in "516 5OO 0100") is therefore not converted, and a
    repair of such a value is refused for having too few digits; "516-5OO-0100" works.
    """
    v = re.sub(r"\b(" + "|".join(_NUMBER_WORDS) + r")\b", lambda m: _NUMBER_WORDS[m.group(1).lower()], value,
               flags=re.IGNORECASE)
    # Letters commonly typed instead of digits (O for 0, l/I for 1) inside otherwise numeric chunks.
    def fix(m: re.Match) -> str:
        """Translate look-alike letters in one chunk, but only if it already has 3+ real digits, so a run of
        letters and punctuation that merely resembles digits (e.g. "lOO.") is left alone."""
        chunk = m.group(0)
        if sum(ch.isdigit() for ch in chunk) >= 3:
            return chunk.translate(str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1"}))
        return chunk

    # Chunks: runs of 4+ digits, look-alike letters and phone punctuation, e.g. "5OO-0l00" or "(516)".
    return re.sub(r"[\dOolI().\-]{4,}", fix, v)


def _is_subsequence(needle: str, hay: str) -> bool:
    """True if every character of ``needle`` appears in ``hay`` in the same order (not necessarily adjacent).

    ``ch in it`` advances the shared iterator past the match, so each later character must be found after the
    previous one: "5165550100" is a subsequence of "1 516 555 0100 x12", but "5165550100" is not of "0100555516".
    """
    it = iter(hay)
    return all(ch in it for ch in needle)
