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

MAX_REPAIR_VALUES = 200
GENERAL_NOTE = "(general note)"


class AIError(Exception):
    """Raised when a Claude call fails or returns something unusable."""


@dataclass
class AIMappingResult:
    mapping: Mapping  # only the fields Claude proposed / changed
    notes: str
    usage: dict[str, int] = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)  # entries rejected during validation


@dataclass
class AINotesResult:
    mapping: Mapping                       # only the fields that change
    note_results: list[dict[str, str]]     # {column, status, message} per note
    summary: str
    usage: dict[str, int] = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)


@dataclass
class AIReviewResult:
    findings: list[dict[str, str]]         # {severity, field, column, issue, suggestion}
    mapping: Mapping                       # proposed field changes (may be empty)
    summary: str
    usage: dict[str, int] = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def _transform_docs() -> str:
    lines = []
    for t in TRANSFORMS.values():
        src = "no sources" if t.max_sources == 0 else (
            f"{t.min_sources}+ sources" if t.max_sources is None else f"exactly {t.min_sources} source")
        params = f"; params: {', '.join(t.params)}" if t.params else ""
        lines.append(f"- {t.name} ({src}{params}): {t.description}")
    return "\n".join(lines)


def _schema_docs(schema: TargetSchema) -> str:
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
- Several phone columns: use coalesce with mobile/cell columns first.
- Never invent data. Use constant only when the user explicitly asks for a fixed value. Never use constant
  or a guessed column for consent, opt-in or subscription fields.
- confidence: high = header and values clearly match; medium = probable; low = a guess the reviewer must check.
- reason: one short sentence a non-technical reviewer can understand.
"""


def _profile_payload(profiles: list[ColumnProfile], n_samples: int) -> list[dict[str, Any]]:
    out = []
    for p in profiles:
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
    return {
        name: {"transform": fm.transform, "sources": fm.sources, "params": fm.params}
        for name, fm in mapping.fields.items()
        if fm.transform != "none"
    }


def _output_schema(schema: TargetSchema, columns: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"mappings": {"type": "array", "items": _mapping_item_schema(schema, columns)},
                       "notes": {"type": "string"}},
        "required": ["mappings", "notes"],
        "additionalProperties": False,
    }


def _notes_schema(schema: TargetSchema, columns: list[str]) -> dict[str, Any]:
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

class ClaudeAssistant:
    def __init__(self, api_key: str, mapping_model: str = "claude-sonnet-5-5",
                 repair_model: str = "claude-haiku-5-5", sample_values: int = 15, client: Any = None):
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
        try:
            resp = self.client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            )
        except Exception as exc:  # anthropic.APIError and network errors
            raise AIError(f"Claude request failed: {exc}") from exc

        stop = getattr(resp, "stop_reason", None)
        if stop == "refusal":
            raise AIError("Claude declined this request.")
        if stop == "max_tokens":
            raise AIError("Claude's answer was cut off (too many columns?). Try again or map manually.")
        text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), None)
        if not text:
            raise AIError("Claude returned no answer.")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AIError("Claude returned malformed JSON.") from exc
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
        """Turn the reviewer's notes ("this column is X", "goes to Y", "dates are day-first") into mapping changes."""
        columns = [p.name for p in profiles]
        notes = [{"column": c, "note": n.strip()} for c, n in column_notes.items() if n.strip() and c in columns]
        if general_note.strip():
            notes.append({"column": GENERAL_NOTE, "note": general_note.strip()})
        if not notes:
            return AINotesResult(Mapping(), [], "No notes to apply.")
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
        results = [r for r in data.get("note_results", []) if isinstance(r, dict)]
        return AINotesResult(mapping, results, str(data.get("summary", "")).strip(), usage, dropped)

    def review(self, schema: TargetSchema, profiles: list[ColumnProfile], current: Mapping,
               payload: dict[str, Any]) -> AIReviewResult:
        """Look for mistakes in a mapping/conversion, using a sample of original vs converted rows."""
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
        findings = [x for x in data.get("findings", []) if isinstance(x, dict) and x.get("issue")]
        return AIReviewResult(findings, mapping, str(data.get("summary", "")).strip(), usage, dropped)

    # -- value repair -------------------------------------------------------
    def repair_values(self, f: FieldSpec, values: list[str], good_examples: list[str] | None = None
                      ) -> tuple[list[tuple[str, str]], dict[str, int]]:
        """Return Claude's suggested (original, repaired) pairs. Callers must re-validate each suggestion."""
        values = list(dict.fromkeys(v for v in values if v))[:MAX_REPAIR_VALUES]
        if not values:
            return [], {}
        fmt = f.format or {"date": "%Y-%m-%d", "datetime": "%Y-%m-%d %H:%M:%S", "phone": "E164"}.get(f.type, "")
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
        allowed = set(values)
        pairs = [(r["original"], r["repaired"].strip()) for r in data.get("repairs", [])
                 if r.get("original") in allowed and r.get("repaired", "").strip()]
        return pairs, usage


# ---------------------------------------------------------------------------
# Validation of Claude output
# ---------------------------------------------------------------------------

def parse_mapping_entries(entries: list[dict], schema: TargetSchema, columns: list[str]
                          ) -> tuple[Mapping, list[str]]:
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
        if transform in ("coalesce", "concat") and len(sources) == 1:
            transform = "copy"
        problem = check_transform(transform, sources, params)
        if problem:
            if not unknown:
                dropped.append(f"{target}: {problem}")
            continue
        conf = str(e.get("confidence", "")).lower()
        out[target] = FieldMapping(
            sources=sources, transform=transform, params=params, origin="ai",
            confidence=conf if conf in ("high", "medium", "low") else "low",
            reason=str(e.get("reason", "")).strip()[:300],
        )
    return Mapping(fields=out), dropped


def accept_repair(f: FieldSpec, original: str, repaired: str) -> bool:
    """Guard against Claude inventing data: a repaired phone must reuse the original's digits, etc."""
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


_NUMBER_WORDS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
                 "six": "6", "seven": "7", "eight": "8", "nine": "9"}


def _phone_letters_to_digits(value: str) -> str:
    v = re.sub(r"\b(" + "|".join(_NUMBER_WORDS) + r")\b", lambda m: _NUMBER_WORDS[m.group(1).lower()], value,
               flags=re.IGNORECASE)
    # Letters commonly typed instead of digits (O for 0, l/I for 1) inside otherwise numeric chunks.
    def fix(m: re.Match) -> str:
        chunk = m.group(0)
        if sum(ch.isdigit() for ch in chunk) >= 3:
            return chunk.translate(str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1"}))
        return chunk

    return re.sub(r"[\dOolI().\-]{4,}", fix, v)


def _is_subsequence(needle: str, hay: str) -> bool:
    it = iter(hay)
    return all(ch in it for ch in needle)
