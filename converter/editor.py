"""Translate between a Mapping and the rows of the mapping-review grid in the web UI.

The grid has one "Option" text cell per field whose meaning depends on the transform; these helpers
format and parse it so the UI code stays thin and the logic is unit-testable.
"""

from __future__ import annotations

import re
from typing import Any

from .mapping import FieldMapping, Mapping
from .schema import FieldSpec, TargetSchema
from .transforms import ADDRESS_PARTS, DATE_ORDERS, NAME_PARTS, TRANSFORMS, check_transform

LABELS = {name: info.label for name, info in TRANSFORMS.items()}
FROM_LABEL = {label: name for name, label in LABELS.items()}

OPTION_HELP = (
    "Depends on 'How': date fields → auto / MDY / DMY / YMD (or a strptime pattern like %d.%m.%Y); "
    "name part → first / middle / last / first_middle / title / suffix / full; "
    "address part → line1 / line2 / city / state / zip; combine → separator; "
    "split → separator and piece, e.g. ', #2'; translate → Y=true; N=false; fixed value → the value."
)


def option_text(fm: FieldMapping, f: FieldSpec) -> str:
    t, p = fm.transform, fm.params
    if t in ("name_part", "address_part"):
        return str(p.get("part", "first" if t == "name_part" else "line1"))
    if t == "concat":
        sep = p.get("separator", " ")
        return "(space)" if sep == " " else str(sep)
    if t == "split":
        return f"{p.get('separator', ',')} #{int(p.get('index') or 0) + 1}"
    if t == "map_values":
        return "; ".join(f"{m.get('from', '')}={m.get('to', '')}" for m in p.get("value_map", []))
    if t == "constant":
        return str(p.get("value", ""))
    if t in ("copy", "coalesce", "each_row"):
        if f.type in ("date", "datetime"):
            return str(p.get("input_format") or p.get("date_order") or "auto")
        if f.type == "phone" and p.get("region"):
            return str(p["region"])
    return ""


def parse_option(transform: str, f: FieldSpec, text: str | None) -> tuple[dict[str, Any], str | None]:
    text = (text or "").strip()
    if transform in ("name_part", "address_part"):
        allowed = NAME_PARTS if transform == "name_part" else ADDRESS_PARTS
        part = text.lower() or ("first" if transform == "name_part" else "line1")
        if part not in allowed:
            return {}, f"option must be one of: {', '.join(allowed)}"
        return {"part": part}, None
    if transform == "concat":
        return {"separator": " " if text in ("", "(space)") else text}, None
    if transform == "split":
        m = re.match(r"^(.*?)\s*#\s*(\d+)$", text)
        if text and not m:
            return {}, "split option looks like ', #2' (separator, then # and the piece number)"
        sep = (m.group(1) if m else ",") or ","
        piece = int(m.group(2)) if m else 1
        return {"separator": sep, "index": max(piece, 1) - 1}, None
    if transform == "map_values":
        pairs = []
        for chunk in [c for c in re.split(r"[;\n]", text) if c.strip()]:
            if "=" not in chunk:
                return {}, "translations look like: Y=true; N=false"
            a, b = chunk.split("=", 1)
            pairs.append({"from": a.strip(), "to": b.strip()})
        return {"value_map": pairs}, None
    if transform == "constant":
        return {"value": text}, None
    if transform in ("copy", "coalesce", "each_row"):
        if f.type in ("date", "datetime") and text:
            if "%" in text:
                return {"input_format": text}, None
            if text.upper() not in DATE_ORDERS and text.lower() != "auto":
                return {}, "date option must be auto, MDY, DMY or YMD (or a %-pattern)"
            return ({"date_order": text.upper()} if text.lower() != "auto" else {}), None
        if f.type == "phone" and text:
            if not re.fullmatch(r"[A-Za-z]{2}", text):
                return {}, "phone option is a 2-letter country code, e.g. US"
            return {"region": text.upper()}, None
    return {}, None


def apply_row_edit(schema: TargetSchema, mapping: Mapping, field_name: str, changes: dict[str, Any],
                   col_how: str, col_sources: str, col_option: str) -> tuple[Mapping, str | None]:
    """Apply one edited grid row to the mapping. Returns (new mapping, option parse error)."""
    f = schema.field(field_name)
    if f.fixed_blank:
        return mapping, "always blank — the import format requires this column to stay empty"
    fm = mapping.get(field_name).model_copy(deep=True)
    error = None

    if col_sources in changes:
        fm.sources = [s for s in (changes[col_sources] or []) if s]
    if col_how in changes:
        new_t = FROM_LABEL.get(changes[col_how], "none")
        if new_t != fm.transform:
            fm.transform = new_t
            fm.params = {}
            if new_t in ("none", "constant"):
                fm.sources = []
    # Sensible automatic transform when only sources were chosen.
    several = "each_row" if f.type == "phone" else "coalesce"  # several phone columns → one row per number
    if fm.transform == "none" and fm.sources:
        fm.transform = "copy" if len(fm.sources) == 1 else several
    elif fm.transform == "copy" and len(fm.sources) > 1:
        fm.transform = several
    elif fm.transform in ("coalesce", "concat", "each_row") and len(fm.sources) == 1:
        fm.transform = "copy"
    elif fm.transform not in ("none", "constant") and not fm.sources and col_sources in changes:
        fm.transform = "none"
        fm.params = {}

    if col_option in changes:
        params, error = parse_option(fm.transform, f, changes[col_option])
        if error is None:
            fm.params = params
    elif fm.transform in ("name_part", "address_part") and "part" not in fm.params:
        fm.params = parse_option(fm.transform, f, "")[0]

    fm.origin = "user"
    fm.confidence = ""
    fm.reason = "set by reviewer"
    fields = dict(mapping.fields)
    fields[field_name] = fm
    # Picking a skipped column as a source un-skips it: the reviewer chose it explicitly.
    excluded = [c for c in mapping.excluded_columns if c not in fm.sources]
    return mapping.model_copy(update={"fields": fields, "excluded_columns": excluded}), error


def _without_column(fm: FieldMapping, column: str) -> FieldMapping:
    """Field mapping with one source column removed; falls back to 'not mapped' when nothing usable is left."""
    sources = [s for s in fm.sources if s != column]
    if not sources:
        return FieldMapping(origin="user", reason="set by reviewer")
    transform = fm.transform
    if transform in ("coalesce", "concat", "each_row") and len(sources) == 1:
        transform = "copy"
    params = fm.params if transform == fm.transform else {}
    if check_transform(transform, sources, params):
        return FieldMapping(origin="user", reason="set by reviewer")
    return FieldMapping(sources=sources, transform=transform, params=params, origin="user", reason="set by reviewer")


def apply_column_edit(schema: TargetSchema, mapping: Mapping, column: str, targets: list[str] | None = None,
                      skip: bool | None = None) -> tuple[Mapping, list[str]]:
    """Edit the mapping from the source column's side (the "Columns in your file" table).

    targets: target field names this column should go to (None = unchanged, [] = not imported). A field that
    gains this column takes it as its only source; whatever fed that field before is dropped from it. Phone fields
    are the exception: the column is added to the others and each different number becomes its own row.
    skip: True removes the column from every field and keeps it out (also of templates and Claude's suggestions).
    Returns (new mapping, messages describing what changed).
    """
    fields = {k: v.model_copy(deep=True) for k, v in mapping.fields.items()}
    excluded = list(mapping.excluded_columns)
    messages: list[str] = []
    current = [n for n, fm in fields.items() if fm.transform != "none" and column in fm.sources]

    if skip:
        targets = []
        if column not in excluded:
            excluded.append(column)
    elif skip is False and column in excluded:
        excluded.remove(column)

    if targets is not None:
        wanted = []
        for name in targets:
            if name not in schema.field_names:
                continue
            f = schema.field(name)
            if f.fixed_blank:
                messages.append(f"{f.header} is always blank and can't take a column.")
                continue
            wanted.append(name)
        for name in current:
            if name not in wanted:
                fields[name] = _without_column(fields[name], column)
                messages.append(f"'{column}' no longer goes to {schema.field(name).header}.")
        for name in wanted:
            if name in current:
                continue
            f = schema.field(name)
            before = fields.get(name, FieldMapping())
            has_sources = before.transform not in ("none", "constant") and bool(before.sources)
            if f.type == "phone" and has_sources:
                # Phone columns add up: each different number becomes its own row.
                fields[name] = FieldMapping(sources=[*before.sources, column], transform="each_row", origin="user",
                                            reason="set by reviewer")
                messages.append(f"{f.header} now also uses '{column}' — one row per different number "
                                f"({', '.join(fields[name].sources)}).")
                continue
            if has_sources:
                messages.append(f"{f.header} now comes from '{column}' (was {', '.join(before.sources)}).")
            else:
                messages.append(f"{f.header} now comes from '{column}'.")
            fields[name] = FieldMapping(sources=[column], transform="copy", origin="user", reason="set by reviewer")
        if wanted and column in excluded:
            excluded.remove(column)

    return mapping.model_copy(update={"fields": fields, "excluded_columns": excluded}), messages


def row_problem(schema: TargetSchema, mapping: Mapping, field_name: str) -> str:
    f = schema.field(field_name)
    if f.fixed_blank:
        return ""
    fm = mapping.get(field_name)
    p = check_transform(fm.transform, fm.sources, fm.params)
    if p:
        return p
    if f.required and fm.transform == "none" and not f.default:
        return "required — choose a source column"
    return ""
