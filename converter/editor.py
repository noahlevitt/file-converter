"""Translate between a Mapping and the rows of the mapping-review grid in the web UI.

The grid has one "Option" text cell per field whose meaning depends on the transform; these helpers
format and parse it so the UI code stays thin and the logic is unit-testable.

Overview
--------
The web app (``app.py``) shows the mapping in two editable tables:

* **Field side** - one row per target field: Field | Type | How (transform label) | Source column(s) | Option |
  Check. :func:`option_text` fills the Option cell, :func:`apply_row_edit` applies an edited row back to the
  :class:`~converter.mapping.Mapping`, :func:`row_problem` fills the Check cell.
* **Column side** ("Columns in your file") - one row per source column: Goes to (target fields) | Skip | Your
  note. :func:`apply_column_edit` applies "Goes to" and "Skip" edits (notes are handled in ``app.py``).

Main entry points
    :func:`option_text`, :func:`parse_option`, :func:`apply_row_edit`, :func:`apply_column_edit`,
    :func:`row_problem`, plus the label tables :data:`LABELS` / :data:`FROM_LABEL` and :data:`OPTION_HELP`.

Data flow
    ``Mapping`` -> grid rows (``LABELS``, :func:`option_text`, :func:`row_problem`) -> reviewer edits the grid ->
    Streamlit reports the edited cells as ``{column label: new value}`` -> :func:`apply_row_edit` /
    :func:`apply_column_edit` -> new ``Mapping`` (the input mapping is never modified) -> grid is redrawn.

Every edit marks the touched fields as the reviewer's choice (``origin="user"``, reason "set by reviewer").
Phone fields follow the "several phone columns -> one row per number" rule (see ``mapping.phone_rule``).
"""

from __future__ import annotations

import re
from typing import Any

from .mapping import FieldMapping, Mapping, phone_rule
from .schema import FieldSpec, TargetSchema
from .transforms import ADDRESS_PARTS, DATE_ORDERS, NAME_PARTS, TRANSFORMS, check_transform

# Transform name -> label shown in the "How" column ("copy" -> "Copy column", "each_row" -> "One row per phone
# number"), and the reverse, used for the select box options and to read an edited "How" cell back.
LABELS = {name: info.label for name, info in TRANSFORMS.items()}
FROM_LABEL = {label: name for name, label in LABELS.items()}

# Tooltip of the "Option" column: what to type there for each kind of transform / field type.
OPTION_HELP = (
    "Depends on 'How': date fields → auto / MDY / DMY / YMD (or a strptime pattern like %d.%m.%Y); "
    "name part → first / middle / last / first_middle / title / suffix / full; "
    "address part → line1 / line2 / city / state / zip; combine → separator; "
    "split → separator and piece, e.g. ', #2'; translate → Y=true; N=false; fixed value → the value."
)


def option_text(fm: FieldMapping, f: FieldSpec) -> str:
    """Text for the grid's "Option" cell: the field mapping's params written the way a reviewer types them.

    The inverse of :func:`parse_option`. What the text means depends on the transform (and, for copy-like
    transforms, on the field type):

    ==============================  ====================================  =====================
    transform / field               params                                Option text
    ==============================  ====================================  =====================
    name_part / address_part        ``{"part": "last"}``                  ``last``
    concat (Combine columns)        ``{"separator": " "}``                ``(space)``
    split                           ``{"separator": ",", "index": 1}``    ``, #2`` (1-based)
    map_values (Translate values)   ``{"value_map": [{Y->true}, ...]}``   ``Y=true; N=false``
    constant (Fixed value)          ``{"value": "NY"}``                   ``NY``
    copy-like, date field           ``{"date_order": "DMY"}``             ``DMY`` (or ``auto``)
    copy-like, phone field          ``{"region": "CA"}``                  ``CA``
    ==============================  ====================================  =====================

    Returns "" when the transform has no option. Missing params show their default (part "first"/"line1",
    separator space / ",", piece 1, date order "auto").
    """
    t, p = fm.transform, fm.params
    if t in ("name_part", "address_part"):
        return str(p.get("part", "first" if t == "name_part" else "line1"))
    if t == "concat":
        sep = p.get("separator", " ")
        # A single space would be invisible in the grid cell, so it is shown as "(space)".
        return "(space)" if sep == " " else str(sep)
    if t == "split":
        # Pieces are stored 0-based ("index") but shown 1-based ("#2" = second piece).
        return f"{p.get('separator', ',')} #{int(p.get('index') or 0) + 1}"
    if t == "map_values":
        return "; ".join(f"{m.get('from', '')}={m.get('to', '')}" for m in p.get("value_map", []))
    if t == "constant":
        return str(p.get("value", ""))
    if t in ("copy", "coalesce", "each_row"):
        # Copy-like transforms only have options for dates (how to read them) and phones (default country).
        # An explicit strptime pattern wins over a date order.
        if f.type in ("date", "datetime"):
            return str(p.get("input_format") or p.get("date_order") or "auto")
        if f.type == "phone" and p.get("region"):
            return str(p["region"])
    return ""


def parse_option(transform: str, f: FieldSpec, text: str | None) -> tuple[dict[str, Any], str | None]:
    """Parse an "Option" cell typed by the reviewer into transform params.

    Inputs: the row's transform name, the target field and the cell text (None/blank = default). Output:
    ``(params, error)`` - ``error`` is None on success, otherwise a message for the reviewer and ``params`` is
    ``{}`` (the caller then keeps the previous params).

    Examples::

        parse_option("name_part", f, "Last")        -> ({"part": "last"}, None)
        parse_option("concat", f, "(space)")        -> ({"separator": " "}, None)
        parse_option("split", f, ", #2")            -> ({"separator": ",", "index": 1}, None)
        parse_option("map_values", f, "Y=true; N=false")
            -> ({"value_map": [{"from": "Y", "to": "true"}, {"from": "N", "to": "false"}]}, None)
        parse_option("copy", date_field, "dmy")     -> ({"date_order": "DMY"}, None)
        parse_option("copy", date_field, "%d.%m.%Y") -> ({"input_format": "%d.%m.%Y"}, None)
        parse_option("copy", phone_field, "ca")     -> ({"region": "CA"}, None)
        parse_option("copy", phone_field, "Canada") -> ({}, "phone option is a 2-letter country code, e.g. US")
    """
    text = (text or "").strip()
    if transform in ("name_part", "address_part"):
        allowed = NAME_PARTS if transform == "name_part" else ADDRESS_PARTS
        # Case-insensitive; blank = the most common part (first name / street line).
        part = text.lower() or ("first" if transform == "name_part" else "line1")
        if part not in allowed:
            return {}, f"option must be one of: {', '.join(allowed)}"
        return {"part": part}, None
    if transform == "concat":
        # Blank or "(space)" -> a single space (the cell is stripped, so a typed space would be lost anyway).
        return {"separator": " " if text in ("", "(space)") else text}, None
    if transform == "split":
        # "<separator> #<piece>": everything before the last "#<digits>" is the separator, e.g. " - #3".
        m = re.match(r"^(.*?)\s*#\s*(\d+)$", text)
        if text and not m:
            return {}, "split option looks like ', #2' (separator, then # and the piece number)"
        sep = (m.group(1) if m else ",") or ","
        piece = int(m.group(2)) if m else 1
        # Stored 0-based; "#0" is treated as the first piece.
        return {"separator": sep, "index": max(piece, 1) - 1}, None
    if transform == "map_values":
        # Pairs separated by ";" or new lines; only the first "=" splits, so "a=b=c" maps "a" to "b=c".
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
            # A "%" means an explicit strptime pattern; otherwise a date order (auto / MDY / DMY / YMD).
            if "%" in text:
                return {"input_format": text}, None
            if text.upper() not in DATE_ORDERS and text.lower() != "auto":
                return {}, "date option must be auto, MDY, DMY or YMD (or a %-pattern)"
            # "auto" is the default, so it is stored as no param at all.
            return ({"date_order": text.upper()} if text.lower() != "auto" else {}), None
        if f.type == "phone" and text:
            # Default country for numbers written without a country code.
            if not re.fullmatch(r"[A-Za-z]{2}", text):
                return {}, "phone option is a 2-letter country code, e.g. US"
            return {"region": text.upper()}, None
    # Transforms without options (none, copy of a text field, ...): any text is ignored.
    return {}, None


def apply_row_edit(schema: TargetSchema, mapping: Mapping, field_name: str, changes: dict[str, Any],
                   col_how: str, col_sources: str, col_option: str) -> tuple[Mapping, str | None]:
    """Apply one edited grid row to the mapping. Returns (new mapping, option parse error).

    Inputs:
        ``field_name`` - the field of the edited row.
        ``changes``    - only the cells the reviewer changed, keyed by grid column label, e.g.
                         ``{"How": "Part of a full name", "Option": "last"}`` (Streamlit's ``edited_rows``).
        ``col_how`` / ``col_sources`` / ``col_option`` - the labels of the How, Source column(s) and Option
                         columns, so this module doesn't depend on the UI's wording.

    Behaviour:
        * Sources: replaced by the chosen columns (in the order picked).
        * How: the label is turned into a transform; choosing another transform resets its params, and "Leave
          blank" / "Fixed value" drop the sources. "First non-empty" on the phone field becomes "One row per
          phone number" (and that one is refused for other fields) - the edit is corrected, with a message.
        * Sensible automatic transform: choosing sources without a How gives "Copy column" (one column) or the
          multi-column default (phone: one row per number; other fields: first non-empty); a multi-column
          transform left with one column becomes a copy, and clearing all sources unmaps the field.
        * Option: parsed with :func:`parse_option`; on a parse error the old params stay and the error is
          returned. Name/address parts get their default part when none is set yet.
        * The field is marked as set by the reviewer, and any chosen column that was skipped is un-skipped.

    The error is None when everything applied cleanly. Fixed-blank fields can't be edited: the mapping is
    returned unchanged with an explanation. Example: choosing "Phone" and "Phone 2" as sources of phone_number
    -> transform "each_row" (one row per different number).
    """
    f = schema.field(field_name)
    if f.fixed_blank:
        return mapping, "always blank — the import format requires this column to stay empty"
    fm = mapping.get(field_name).model_copy(deep=True)
    error = None

    if col_sources in changes:
        # The multiselect may report None (cleared) or contain empty entries.
        fm.sources = [s for s in (changes[col_sources] or []) if s]
    if col_how in changes:
        new_t = FROM_LABEL.get(changes[col_how], "none")  # unknown / cleared label -> not mapped
        if phone_rule(f.type, new_t) != new_t:
            # Not allowed for this field type: apply the allowed equivalent and tell the reviewer why.
            error = ("several phone columns always give one row per number" if f.type == "phone"
                     else "'One row per phone number' is only for the phone field")
            new_t = phone_rule(f.type, new_t)
        if new_t != fm.transform:
            fm.transform = new_t
            fm.params = {}  # params of the old transform mean nothing to the new one
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
        # The reviewer removed every source column -> not mapped. (Choosing a How before any column is left
        # as is: the row stays incomplete and the Check column says what's missing.)
        fm.transform = "none"
        fm.params = {}

    if col_option in changes:
        # Parsed against the (possibly just changed) transform.
        params, opt_error = parse_option(fm.transform, f, changes[col_option])
        if opt_error is None:
            fm.params = params
        error = error or opt_error  # report the first problem
    elif fm.transform in ("name_part", "address_part") and "part" not in fm.params:
        # Default part (first name / street line) so the row is valid right away.
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
    """Field mapping with one source column removed; falls back to 'not mapped' when nothing usable is left.

    * No column left -> not mapped.
    * A multi-column transform (first non-empty / combine / one row per number) left with one column -> copy;
      params are kept only when the transform stays the same (they belong to that transform).
    * Anything ``check_transform`` rejects afterwards (e.g. a combine that needs 2+ columns) -> not mapped.

    The result is always marked as the reviewer's choice. Example: each_row of ["Phone", "Phone 2"] without
    "Phone 2" -> copy of ["Phone"].
    """
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

    ``skip=False`` un-skips the column; choosing a target for a skipped column also un-skips it. ``skip=True``
    wins over ``targets``. Unknown field names are ignored, and fixed-blank fields (consent) refuse the column
    with a message. A field that loses this column keeps its other columns where that still makes sense (see
    :func:`_without_column`).

    Examples (messages use the fields' output headers)::

        "Mobile" -> phone_number, which already reads "Phone":
            phone_number = each_row of ["Phone", "Mobile"]
            "phone_number now also uses 'Mobile' — one row per different number (Phone, Mobile)."
        "Visit" -> transactions_last, which read "Created":
            transactions_last = copy of ["Visit"]
            "transactions_last now comes from 'Visit' (was Created)."
        skip=True on "Notes":
            every field drops "Notes"; "Notes" is added to excluded_columns.
    """
    # Work on copies so the caller's mapping stays untouched.
    fields = {k: v.model_copy(deep=True) for k, v in mapping.fields.items()}
    excluded = list(mapping.excluded_columns)
    messages: list[str] = []
    # Fields that read this column right now (what the "Goes to" cell showed before the edit).
    current = [n for n, fm in fields.items() if fm.transform != "none" and column in fm.sources]

    if skip:
        targets = []  # skipping = goes nowhere
        if column not in excluded:
            excluded.append(column)
    elif skip is False and column in excluded:
        excluded.remove(column)

    if targets is not None:
        # Keep only real, mappable target fields.
        wanted = []
        for name in targets:
            if name not in schema.field_names:
                continue
            f = schema.field(name)
            if f.fixed_blank:
                messages.append(f"{f.header} is always blank and can't take a column.")
                continue
            wanted.append(name)
        # Fields that no longer want this column lose it (other sources stay where possible).
        for name in current:
            if name not in wanted:
                fields[name] = _without_column(fields[name], column)
                messages.append(f"'{column}' no longer goes to {schema.field(name).header}.")
        # Fields that newly want this column.
        for name in wanted:
            if name in current:
                continue  # already reads it: leave its transform and params alone
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
            # Any other field: this column replaces whatever fed the field before (plain copy).
            if has_sources:
                messages.append(f"{f.header} now comes from '{column}' (was {', '.join(before.sources)}).")
            else:
                messages.append(f"{f.header} now comes from '{column}'.")
            fields[name] = FieldMapping(sources=[column], transform="copy", origin="user", reason="set by reviewer")
        # Sending a column somewhere means it is wanted: it can't stay skipped.
        if wanted and column in excluded:
            excluded.remove(column)

    return mapping.model_copy(update={"fields": fields, "excluded_columns": excluded}), messages


def row_problem(schema: TargetSchema, mapping: Mapping, field_name: str) -> str:
    """Text for the grid's "Check" cell of one field: what is wrong with its mapping, or "" when it's fine.

    * an invalid transform / sources / params combination, e.g. "'Combine columns' needs at least 2 source
      column(s)" (a How chosen before its columns);
    * "required — choose a source column" for an unmapped required field without a default.

    Fixed-blank fields never have a problem. (The "only one 'One row per phone number' field" rule is checked for
    the whole mapping by ``Mapping.problems``.)
    """
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
