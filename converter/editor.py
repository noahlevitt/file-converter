"""Translate between a Mapping and the rows of the mapping-review grid in the web UI.

The grid has one "Option" text cell per field whose meaning depends on the transform; these helpers
format and parse it so the UI code stays thin and the logic is unit-testable.

Overview
--------
The web app (``app.py``) shows the mapping in two editable tables:

* **Field side** - one row per target field: Field | Type | How (transform label) | Source column(s) | Option |
  Check. :func:`option_text` fills the Option cell, :func:`apply_row_edit` applies an edited row back to the
  :class:`~converter.mapping.Mapping`, :func:`row_problem` fills the Check cell.
* **Column side** ("Columns in your file") - one row per source column: Goes to (target fields; consent with the
  column's consent setting, "consent — opt-outs only") | Skip | Your note. :func:`goes_to_options` /
  :func:`goes_to_cell` fill "Goes to", :func:`read_goes_to` reads it back, :func:`apply_column_edit` applies "Goes
  to" and "Skip" edits (notes are handled in ``app.py``).

Main entry points
    :func:`option_text`, :func:`parse_option`, :func:`apply_row_edit`, :func:`apply_column_edit`,
    :func:`row_problem`, :func:`goes_to_options`, :func:`goes_to_cell`, :func:`read_goes_to`, plus the label
    tables :data:`LABELS` / :data:`FROM_LABEL` and :data:`OPTION_HELP`.

Data flow
    ``Mapping`` -> grid rows (``LABELS``, :func:`option_text`, :func:`row_problem`) -> reviewer edits the grid ->
    Streamlit reports the edited cells as ``{column label: new value}`` -> :func:`apply_row_edit` /
    :func:`apply_column_edit` -> new ``Mapping`` (the input mapping is never modified) -> grid is redrawn.

Every edit marks the touched fields as the reviewer's choice (``origin="user"``, reason "set by reviewer").
Phone fields follow the "several phone columns -> one row per number" rule (see ``mapping.phone_rule``); an area
code column added to the phone field makes it 'Phone + area code column'. Consent (0.23.0) reads each of its columns
with that column's consent setting (``mapping.consent_rule``, ``mapping.CONSENT_MODES``): its columns add up like
the phone field's, and a column keeps its setting while it stays.
"""

from __future__ import annotations

import re
from typing import Any

from . import normalizers as N
from .mapping import (CONSENT_MODES, FIELD_PARAMS, FieldMapping, Mapping, consent_modes, consent_problem, consent_rule,
                      is_area_code_header, parse_consent_mode, phone_rule)
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
    "split → separator and piece, e.g. ', #2'; translate → Y=true; N=false (consent: to opt-in, opt-out or "
    "nothing, e.g. DNT=opt-out; OK=); fixed value → the value; consent columns → each column's setting, e.g. "
    "Email Opt In: opt-ins only; Unsubscribed: opt-outs only."
)


def consent_option_text(fm: FieldMapping) -> str:
    """The Option cell of consent read with "Consent columns": the setting of each column that has one, e.g.
    "opt-ins only" (one column) or "Email Opt In: opt-ins only; Unsubscribed: opt-outs only"."""
    modes = consent_modes(fm)
    if len(fm.sources) == 1:
        return CONSENT_MODES.get(modes.get(fm.sources[0], ""), "")
    return "; ".join(f"{c}: {CONSENT_MODES[m]}" for c, m in modes.items())


def parse_consent_option(text: str | None, sources: list[str]) -> tuple[dict[str, str], str | None]:
    """Consent settings typed in the Option cell -> ({column: setting}, error).

    "opt-outs only" (or "out", "both", "full", "in", ...) sets the column when consent has one; "Email Opt In:
    opt-ins only; Unsubscribed = out" sets each named column (case-insensitive). Blank sets nothing. Unknown columns
    or settings, or a setting without a column name when consent has several columns (it would silently change
    them all), give an error and nothing is set.
    """
    text = (text or "").strip()
    if not text:
        return {}, None
    if parse_consent_mode(text):
        if len(sources) > 1:
            return {}, ("consent has several columns: name each one, e.g. Email Opt In: opt-ins only; Unsubscribed: "
                        "opt-outs only")
        return {c: parse_consent_mode(text) for c in sources}, None
    by_lower = {c.lower(): c for c in sources}
    modes: dict[str, str] = {}
    for chunk in [c for c in re.split(r"[;\n]", text) if c.strip()]:
        m = re.match(r"^(.*?)\s*[:=]\s*([^:=]*)$", chunk.strip())
        col = by_lower.get(m.group(1).strip().lower()) if m else None
        mode = parse_consent_mode(m.group(2)) if m else None
        if col is None or mode is None:
            return {}, ("consent settings look like: Email Opt In: opt-ins only; Unsubscribed: opt-outs only "
                        "(settings: opt-ins only, opt-ins and opt-outs, opt-outs only)")
        modes[col] = mode
    return modes, None


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
    if t == "consent" or (t == "coalesce" and f.opt_out_choice):
        return consent_option_text(fm)
    if t in ("copy", "coalesce", "each_row", "area_code"):
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
        parse_option("map_values", consent, "DNT=opt-out; OK=")
            -> ({"value_map": [{"from": "DNT", "to": "opt-out"}, {"from": "OK", "to": ""}]}, None)
        parse_option("map_values", consent, "DNT=yes")    -> ({}, "for consent, translate each value to ...")
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
        if f.opt_out_choice:
            # Consent (0.23.0): a translation says what each value means — opt-in, opt-out or nothing. true / false
            # / yes / no are refused: whether "DNT=yes" means "yes, opted out" or "yes, subscribed" depends on how
            # the column is read, and each wrong reading flips who gets texted.
            # "Active" is left out too: it reads as an opt-in, but "DNT=active" looks like "the DNT flag is active".
            seen: dict[str, str] = {}
            for p in pairs:
                to = p["to"]
                if (not p["from"] or (to and N.consent_reading(to) not in ("in", "out"))
                        or N._boolean_key(to) == "active"):
                    return {}, ("for consent, translate each value to opt-in, opt-out or nothing, e.g. "
                                "DNT=opt-out; OK= (true / false and yes / no could be read either way)")
                meaning = N.consent_reading(to) if to else ""
                if seen.setdefault(p["from"].lower(), meaning) != meaning:
                    return {}, f"'{p['from']}' is translated twice, to different things"
        return {"value_map": pairs}, None
    if transform == "constant":
        return {"value": text}, None
    if transform in ("copy", "coalesce", "each_row", "area_code"):
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
          transform left with one column becomes a copy (not when that How was just chosen: the row then waits
          for its second column), and clearing all sources unmaps the field.
        * Option: parsed with :func:`parse_option`; on a parse error the old params stay and the error is
          returned. Name/address parts get their default part when none is set yet.
        * The field is marked as set by the reviewer, and any chosen column that was skipped is un-skipped.

    The error is None when everything applied cleanly. Fixed-blank fields can't be edited: the mapping is
    returned unchanged with an explanation. Example: choosing "Phone" and "Phone 2" as sources of phone_number
    -> transform "each_row" (one row per different number).
    """
    f = schema.field(field_name)
    if f.fixed_blank:
        return mapping, "always blank — the target format keeps this column empty"
    before = mapping.get(field_name)
    fm = before.model_copy(deep=True)
    error = None

    if col_sources in changes:
        # The multiselect may report None (cleared) or contain empty entries.
        fm.sources = [s for s in (changes[col_sources] or []) if s]
    if col_how in changes:
        new_t = FROM_LABEL.get(changes[col_how], "none")  # unknown / cleared label -> not mapped
        if phone_rule(f.type, new_t) != new_t:
            # Not allowed for this field type: apply the allowed equivalent and tell the reviewer why.
            error = ("several phone columns always give one row per number" if f.type == "phone"
                     else f"'{LABELS[new_t]}' is only for the phone field")
            new_t = phone_rule(f.type, new_t)
        if f.opt_out_choice and new_t == "coalesce":
            # First non-empty is only kept as imports made before 0.23.0 stored it; a person mapping consent now
            # gets its columns read together (an opt-out in any of them wins).
            error = (f"{f.header} reads its columns together, each with its consent setting "
                     f"('{LABELS['consent']}'); 'First non-empty' is only kept for imports made before 0.23.0")
            new_t = "consent"
        if consent_rule(f, new_t, len(fm.sources)) != new_t:
            if new_t != "copy":  # a copy of consent columns simply is 'Consent columns'
                error = (f"{f.header} reads each column with its own consent setting ('{LABELS['consent']}')"
                         if f.opt_out_choice else f"'{LABELS[new_t]}' is only for consent")
            new_t = consent_rule(f, new_t, len(fm.sources))
        if new_t != fm.transform:
            fm.transform = new_t
            # Params of the old transform mean nothing to the new one; the field's own (consent settings) stay.
            fm.params = {k: v for k, v in fm.params.items() if k in FIELD_PARAMS}
            if new_t in ("none", "constant"):
                fm.sources = []
    # Sensible automatic transform when only sources were chosen.
    # Several phone columns → one row per number; several consent columns → each with its setting, an opt-out wins.
    several = "each_row" if f.type == "phone" else "consent" if f.opt_out_choice else "coalesce"
    if fm.transform == "none" and fm.sources:
        fm.transform = "copy" if len(fm.sources) == 1 else several
    elif fm.transform == "copy" and len(fm.sources) > 1:
        fm.transform = several
    elif (fm.transform in ("coalesce", "concat", "each_row", "area_code") and len(fm.sources) == 1
          and col_sources in changes and col_how not in changes and len(before.sources) > 1):
        # A column was removed from a multi-column transform. (A multi-column How chosen while one column is
        # picked stays, so the second column can be added; the Check column says one is missing.)
        fm.transform = "copy"
    elif fm.transform not in ("none", "constant") and not fm.sources and col_sources in changes:
        # The reviewer removed every source column -> not mapped. (Choosing a How before any column is left
        # as is: the row stays incomplete and the Check column says what's missing.)
        fm.transform = "none"
        fm.params = {}
    # Consent reading columns always uses "Consent columns" (or "Translate values"); no other field can. A First
    # non-empty kept from an import made before 0.23.0 becomes it too once a person edits it.
    fm.transform = consent_rule(f, fm.transform, len(fm.sources))
    if f.opt_out_choice and fm.transform == "coalesce":
        fm.transform = "consent"

    if col_option in changes and fm.transform == "consent":
        # Consent settings typed in the Option cell; the other columns keep theirs.
        modes, opt_error = parse_consent_option(changes[col_option], fm.sources)
        if opt_error is None:
            fm.params = {**fm.params, "modes": {**consent_modes(fm), **modes}}
        error = error or opt_error
    elif col_option in changes:
        # Parsed against the (possibly just changed) transform.
        params, opt_error = parse_option(fm.transform, f, changes[col_option])
        if opt_error is None:
            fm.params = {**{k: v for k, v in fm.params.items() if k in FIELD_PARAMS}, **params}
        error = error or opt_error  # report the first problem
    elif fm.transform in ("name_part", "address_part") and "part" not in fm.params:
        # Default part (first name / street line) so the row is valid right away.
        fm.params = parse_option(fm.transform, f, "")[0]

    # Consent settings belong to columns: a column keeps its setting while it stays, a new one has none yet.
    if f.opt_out_choice and fm.transform not in ("none", "constant") and consent_modes(fm):
        fm.params["modes"] = consent_modes(fm)
    else:
        fm.params.pop("modes", None)
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
    * A multi-column transform (first non-empty / combine / one row per number / phone + area code column) left
      with one column -> copy; params are kept only when the transform stays the same (they belong to that
      transform). 'Phone + area code column' left with only its area code column -> not mapped; without its last
      area code column -> one row per phone number (or copy).
    * Anything ``check_transform`` rejects afterwards (e.g. a combine that needs 2+ columns) -> not mapped.

    The result is always marked as the reviewer's choice. Example: each_row of ["Phone", "Phone 2"] without
    "Phone 2" -> copy of ["Phone"].
    """
    sources = [s for s in fm.sources if s != column]
    if not sources:
        return FieldMapping(origin="user", reason="set by reviewer")
    transform = fm.transform
    if isinstance(fm.params.get("modes"), dict):
        # The column takes its consent setting with it; the others keep theirs.
        fm = fm.model_copy(update={"params": {**fm.params, "modes": {c: m for c, m in fm.params["modes"].items()
                                                                      if c != column}}})
    if transform == "area_code" and all(is_area_code_header(s) for s in sources):
        return FieldMapping(origin="user", reason="set by reviewer")  # an area code alone is no phone number
    if transform == "area_code" and is_area_code_header(column) and not any(map(is_area_code_header, sources)):
        transform = "each_row"  # its area code column is gone: the phone columns are read as they are
    if transform in ("coalesce", "concat", "each_row", "area_code") and len(sources) == 1:
        transform = "copy"
    params = fm.params if transform == fm.transform else {}
    if isinstance(fm.params.get("modes"), dict):
        # Consent: the other columns keep their settings and are read together (a First non-empty kept from 0.22.0
        # becomes that too, as with any edit by a person).
        params = {**params, "modes": fm.params["modes"]}
        transform = "consent" if transform in ("copy", "coalesce") else transform
    if check_transform(transform, sources, params):
        return FieldMapping(origin="user", reason="set by reviewer")
    return FieldMapping(sources=sources, transform=transform, params=params, origin="user", reason="set by reviewer")


def apply_column_edit(schema: TargetSchema, mapping: Mapping, column: str, targets: list[str] | None = None,
                      skip: bool | None = None, settings: dict[str, str] | None = None) -> tuple[Mapping, list[str]]:
    """Edit the mapping from the source column's side (the "Columns in your file" table).

    targets: target field names this column should go to (None = unchanged, [] = not imported). A field that
    gains this column takes it as its only source; whatever fed that field before is dropped from it. Phone fields
    are the exception: the column is added to the others and each different number becomes its own row; an area
    code column ("Area Code") added to the phone field gives its 7-digit numbers their area code ('Phone + area
    code column').
    skip: True removes the column from every field and keeps it out (also of templates and Claude's suggestions).
    settings: consent settings chosen with the column, {field name: "in" | "both" | "out"} (0.23.0; for fields with
    ``opt_out_choice``). Consent columns add up like phone columns: the column is added to consent's other columns
    with its setting (none = asked under the field table); choosing another setting for a column consent already
    reads changes just that column's.
    Returns (new mapping, messages describing what changed).

    ``skip=False`` un-skips the column; choosing a target for a skipped column also un-skips it. ``skip=True``
    wins over ``targets``. Unknown field names are ignored, and fixed-blank fields refuse the column
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
        settings = settings or {}
        for name in wanted:
            f = schema.field(name)
            before = fields.get(name, FieldMapping())
            mode = settings.get(name) if f.opt_out_choice and settings.get(name) in CONSENT_MODES else None
            if name in current:
                # Already reads it: leave its transform and params alone, except a newly chosen consent setting.
                if mode and before.transform != "constant" and consent_modes(before).get(column) != mode:
                    fields[name] = before.model_copy(update={
                        "params": {**before.params, "modes": {**consent_modes(before), column: mode}},
                        # A person's edit of a First non-empty kept from 0.22.0: its columns are now read together.
                        "transform": "consent" if before.transform == "coalesce" else before.transform,
                        "origin": "user", "confidence": "", "reason": "set by reviewer"})
                    messages.append(f"{f.header}: '{column}' is now {CONSENT_MODES[mode]}.")
                continue
            has_sources = before.transform not in ("none", "constant") and bool(before.sources)
            if f.opt_out_choice:
                # Consent columns add up, each with its own setting (an opt-out in any of them wins; a 'First
                # non-empty' kept from an older import becomes that too). A translation or a fixed value is
                # replaced: it can't take a second column.
                keep = before.transform in ("consent", "coalesce") and has_sources
                sources = [*before.sources, column] if keep else [column]
                modes = {**(consent_modes(before) if keep else {}), **({column: mode} if mode else {})}
                fields[name] = FieldMapping(sources=sources, transform="consent",
                                            params={"modes": modes} if modes else {}, origin="user",
                                            reason="set by reviewer")
                how = CONSENT_MODES[mode] if mode else "choose its setting below the field table"
                if keep and before.transform == "coalesce":
                    # A First non-empty kept from an older import: from now on its columns are read together.
                    messages.append(f"{f.header} now also reads '{column}' ({how}); its columns are now read "
                                    "together: an opt-out in any of them wins (was first non-empty).")
                elif keep:
                    messages.append(f"{f.header} now also reads '{column}' ({how}).")
                elif before.transform != "none":
                    messages.append(f"{f.header} now comes from '{column}' ({how}; was {before._summary_text()}).")
                else:
                    messages.append(f"{f.header} now comes from '{column}' ({how}).")
                continue
            if f.type == "phone" and has_sources:
                # Phone columns add up: each different number becomes its own row. An area code column gives the
                # 7-digit numbers their area code ('Phone + area code column', which also gives one row per number).
                area = before.transform == "area_code" or is_area_code_header(column)
                fields[name] = FieldMapping(sources=[*before.sources, column], origin="user", reason="set by reviewer",
                                            transform="area_code" if area else "each_row",
                                            params=dict(before.params) if area else {})
                if is_area_code_header(column):
                    messages.append(f"{f.header} now also uses '{column}' — 7-digit numbers get their area code "
                                    f"from it ({', '.join(fields[name].sources)}).")
                else:
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
    * "required — choose a source column" for an unmapped required field without a default;
    * for consent (``opt_out_choice``), the columns whose consent setting is still to be chosen
      (``mapping.consent_problem``).

    Fixed-blank fields never have a problem. (The "only one 'One row per phone number' field" rule is checked for
    the whole mapping by ``Mapping.problems``.)
    """
    f = schema.field(field_name)
    if f.fixed_blank:
        return ""
    fm = mapping.get(field_name)
    p = check_transform(fm.transform, fm.sources, fm.params) or consent_problem(f, fm)
    if p:
        return p
    if f.required and fm.transform == "none" and not f.default:
        return "required — choose a source column"
    return ""


# "Goes to" of a consent column that has no setting yet (picked in the field grid): shown, and can be picked.
NO_SETTING = "setting not chosen"


def goes_to_options(sch: TargetSchema) -> list[str]:
    """The choices of "Goes to" under Columns in your file: each field's output header, and for consent (fields
    with opt_out_choice) one choice per consent setting ("consent — opt-ins only", ..., "consent — setting not
    chosen"), so a consent column is mapped together with what it records."""
    out = []
    for f in sch.mappable_fields:
        out += ([f"{f.header} — {label}" for label in [*CONSENT_MODES.values(), NO_SETTING]] if f.opt_out_choice
                else [f.header])
    return out


def goes_to_cell(sch: TargetSchema, mapping: Mapping, column: str) -> list[str]:
    """The "Goes to" cell of one column: the headers of the fields it feeds, consent with the column's setting."""
    cell = []
    for name in mapping.targets_of(column):
        if name not in sch.field_names:
            continue
        f = sch.field(name)
        mode = consent_modes(mapping.get(name)).get(column) if f.opt_out_choice else None
        cell.append(f"{f.header} — {CONSENT_MODES[mode] if mode else NO_SETTING}" if f.opt_out_choice else f.header)
    return cell


def read_goes_to(sch: TargetSchema, labels: list[str]) -> tuple[list[str], dict[str, str]]:
    """An edited "Goes to" cell -> (field names, {consent field: setting}). When several settings of one consent
    field are picked, the last one picked counts ("setting not chosen" never replaces a setting)."""
    names: list[str] = []
    settings: dict[str, str] = {}
    by_label = {}
    for f in sch.mappable_fields:
        if f.opt_out_choice:
            by_label.update({f"{f.header} — {label}": (f.name, mode) for mode, label in CONSENT_MODES.items()})
            by_label[f"{f.header} — {NO_SETTING}"] = (f.name, None)
        else:
            by_label[f.header] = (f.name, None)
    for label in labels:
        if label not in by_label:
            continue
        name, mode = by_label[label]
        if name not in names:
            names.append(name)
        if mode:
            settings[name] = mode
    return names, settings
