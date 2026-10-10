"""Extraction transforms: how a target field's raw value is pulled from one or more source columns.

Overview
--------
A mapping (see ``mapping.Mapping``) assigns every target field a *transform*, a list of *source columns* and a
dict of *params*. For each row, :func:`apply_transform` turns those into one raw string; the field's type
normalizer (``normalizers.py``) then formats and validates that string (phone → 10 digits, dates → MM/DD/YYYY,
ZIP padding, …). Transforms only *extract*; they never validate or reformat by type.

Responsibilities
    * :data:`TRANSFORMS` — the registry of every allowed transform, with a human label, a description included in
      Claude's system prompt, the allowed number of source columns and the params it accepts.
    * :func:`apply_transform` — run one transform on one row (called by ``engine.convert`` for every cell).
    * :func:`check_transform` — validate a transform/sources/params combination before it is used (called by
      ``Mapping.normalized`` and ``Mapping.problems``).

Why a fixed registry: Claude may only choose from this registry — it never writes code at runtime. Anything
Claude proposes is checked against :data:`TRANSFORMS` and :func:`check_transform`, so a bad suggestion can at
worst produce a wrong (but reviewable) mapping, never arbitrary behaviour.

Quick examples (row = ``{"Name": "SMITH, JOHN", "Phone": "516-555-0100", "Phone 2": ""}``)::

    apply_transform("name_part", row, ["Name"], {"part": "first"})      # -> "JOHN"
    apply_transform("coalesce", row, ["Phone 2", "Phone"], {})          # -> "516-555-0100"
    apply_transform("constant", row, [], {"value": "NY"})               # -> "NY"
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import usaddress
from nameparser import HumanName

from .normalizers import clean_text


@dataclass(frozen=True)
class TransformInfo:
    """Static description of one transform in the registry.

    Attributes:
        name: Registry key stored in mappings/templates (e.g. ``"name_part"``). Never rename: saved templates and
            import records refer to transforms by this name.
        label: Short human label shown in the mapping editor (e.g. "Part of a full name").
        description: One-sentence explanation included in Claude's system prompt (the UI shows ``label`` and
            ``editor.OPTION_HELP`` instead), so it is the instruction Claude follows when choosing a transform.
        min_sources: Minimum number of source columns the transform needs (0 for "none" / "constant").
        max_sources: Maximum number of source columns, or ``None`` for "any number".
        params: Names of the params this transform reads. ``FieldMapping.clean_params`` drops any other param, so
            stale or invented params never reach :func:`apply_transform`.
    """

    name: str
    label: str
    description: str
    min_sources: int
    max_sources: int | None  # None = unlimited
    params: tuple[str, ...] = ()


# Allowed values for the "part" param of name_part (see _name_parts for what each one returns).
NAME_PARTS = ("first", "middle", "last", "first_middle", "title", "suffix", "full")
# Allowed values for the "part" param of address_part (see _ADDRESS_TAGS for how each is built).
ADDRESS_PARTS = ("line1", "line2", "city", "state", "zip")
# Allowed values for the "date_order" param read by the date normalizers ("auto" = detect once per column).
DATE_ORDERS = ("auto", "MDY", "DMY", "YMD")

# The registry, keyed by transform name. Order matters only for display (it is the order of the UI drop-down).
# Params listed here but not used by apply_transform itself (date_order, input_format, region) are passed through
# to the type normalizer for the field, e.g. "date_order": "DMY" makes a date column read as day-first.
TRANSFORMS: dict[str, TransformInfo] = {
    t.name: t
    for t in [
        # none: field not mapped -> "" (the normalizer/engine then applies the field default, if any).
        TransformInfo("none", "Leave blank / default", "No source column; output is blank or the field default.", 0, 0),
        # copy: one column as-is, e.g. "  516-555-0100 " -> "516-555-0100" (whitespace/null tokens cleaned).
        TransformInfo("copy", "Copy column", "Use the source column's value as-is (then type-normalized).", 1, 1,
                      ("date_order", "input_format", "region")),
        # coalesce: first non-empty value, e.g. Last Visit="" , Created="2024-01-15" -> "2024-01-15".
        # Mapping.normalized turns coalesce on a phone field into each_row (see mapping.phone_rule).
        TransformInfo("coalesce", "First non-empty", "Use the first non-empty value across several columns, in order "
                      "(e.g. Last Visit, then Created). Not for phone numbers: several phone columns always give one "
                      "row per number.", 2, None, ("date_order", "input_format", "region")),
        # each_row: Phone="516-555-0100", Phone 2="917-555-0123" -> TWO output rows, one per number. The row
        # expansion happens in engine._expand_rows; apply_transform alone treats it like coalesce.
        TransformInfo("each_row", "One row per phone number", "Phone field only, and always used when it has "
                      "several columns (e.g. Phone, Phone 2, Phone 3): each different number gets its own output row, "
                      "with every other field copied from the original row.", 2, None, ("region",)),
        # concat: join non-empty values, e.g. First="Ana", Last="Garcia" -> "Ana Garcia" (separator " " default).
        TransformInfo("concat", "Combine columns", "Join several columns with a separator (default a space).", 2, None,
                      ("separator",)),
        # name_part: "SMITH, JOHN" + part=first -> "JOHN"; "Dr. Priya Patel MD" + part=suffix -> "MD".
        TransformInfo("name_part", "Part of a full name", "Extract first/middle/last/etc. from a full-name column "
                      "(handles 'Last, First').", 1, 1, ("part",)),
        # address_part: "12 Main St Apt 4B, Hicksville, NY 11801" + part=line2 -> "Apt 4B"; part=zip -> "11801".
        TransformInfo("address_part", "Part of a full address", "Extract street line, unit, city, state or ZIP from a "
                      "single-line address column.", 1, 1, ("part",)),
        # split: "a, b, c" + separator="," index=1 -> "b"; index=-1 -> "c"; index out of range -> "".
        TransformInfo("split", "Split column", "Split a column by a separator and take the Nth piece (0 = first).",
                      1, 1, ("separator", "index")),
        # map_values: value_map=[{"from": "Y", "to": "true"}]: "y" -> "true" (case-insensitive), "N" -> "N".
        TransformInfo("map_values", "Translate values", "Replace specific source values with output values "
                      "(e.g. 'Y' → 'true'); unlisted values pass through.", 1, 1, ("value_map",)),
        # constant: every row gets params["value"], e.g. value="NY" -> "NY" for each row.
        TransformInfo("constant", "Fixed value", "Every row gets the same fixed value.", 0, 0, ("value",)),
    ]
}


def _src(row: dict[str, str], sources: list[str]) -> list[str]:
    """Return the cleaned value of each source column for one row, in the order of ``sources``.

    Missing columns read as "". ``clean_text`` collapses whitespace, strips control characters and turns null-like
    tokens ("N/A", "null", "-", "nan", …) into "", so every transform sees the same notion of "empty".

    Example: ``_src({"A": " x ", "B": "N/A"}, ["A", "B", "C"])`` -> ``["x", "", ""]``.
    """
    return [clean_text(row.get(s, "")) for s in sources]


@lru_cache(maxsize=100_000)
def _name_parts(full: str) -> dict[str, str]:
    """Split a full name into its parts with ``nameparser.HumanName``.

    Handles "First Last", "Last, First", titles and suffixes. Examples::

        "SMITH, JOHN"         -> first="JOHN", last="SMITH", full="JOHN SMITH"
        "Dr. Priya Patel MD"  -> title="Dr.", first="Priya", last="Patel", suffix="MD"
        "Mary Ann O'Neil"     -> first="Mary", middle="Ann", first_middle="Mary Ann", last="O'Neil"

    Returns a dict with every key of :data:`NAME_PARTS` (missing parts are ""). ``full`` is the re-assembled name
    in "First Last" order, falling back to the input if nameparser yields nothing. Casing is left untouched; the
    field's normalizer applies any case rule.

    Cached (LRU, 100k entries) because the same name is parsed once per name field (first, last, …) and the same
    customer often appears on many rows; nameparser is relatively slow. The returned dict is shared between
    callers through the cache, so it must not be mutated.
    """
    n = HumanName(full)
    first_middle = " ".join(p for p in [n.first, n.middle] if p)
    return {
        "first": n.first,
        "middle": n.middle,
        "last": " ".join(p for p in [n.last] if p),  # always a str ("" when there is no last name)
        "first_middle": first_middle,
        "title": n.title,
        "suffix": n.suffix,
        "full": str(n).strip() or full,
    }


# usaddress component labels that make up each address part. Tags are joined with spaces in this order, so e.g.
# AddressNumber="12" + StreetName="Main" + StreetNamePostType="St" -> line1 "12 Main St". PO boxes and building names
# also land in line1; apartment/suite/unit goes to line2. For an intersection ("Main St & 1st Ave") line1 keeps only
# the first street and the "&" (usaddress tags the second street with labels that aren't listed here).
_ADDRESS_TAGS = {
    "line1": ["AddressNumberPrefix", "AddressNumber", "AddressNumberSuffix", "StreetNamePreModifier",
              "StreetNamePreDirectional", "StreetNamePreType", "StreetName", "StreetNamePostType",
              "StreetNamePostDirectional", "StreetNamePostModifier", "USPSBoxType", "USPSBoxID", "BuildingName",
              "CornerOf", "IntersectionSeparator", "LandmarkName", "SubaddressType", "SubaddressIdentifier"],
    "line2": ["OccupancyType", "OccupancyIdentifier"],
    "city": ["PlaceName"],
    "state": ["StateName"],
    "zip": ["ZipCode"],
}


@lru_cache(maxsize=100_000)
def _address_parts(full: str) -> dict[str, str]:
    """Split a single-line US address into line1 / line2 / city / state / zip with ``usaddress``.

    Examples::

        "12 Main St Apt 4B, Hicksville, NY 11801"
            -> line1="12 Main St", line2="Apt 4B", city="Hicksville", state="NY", zip="11801"
        "77 Jericho Tpke Suite 200, Jericho, NY 11753"
            -> line1="77 Jericho Tpke", line2="Suite 200", city="Jericho", state="NY", zip="11753"

    State is returned as written ("New York", "N.Y.", "NY"); the state normalizer converts it later.

    Fallbacks, so data is never silently lost:
        * usaddress raises ``RepeatedLabelError`` for strings it can't tag unambiguously (e.g. two street numbers);
          the whole value then goes to line1 and the other parts are blank.
        * If neither a street line nor a city was recognised, the whole value is used as line1.

    Cached for the same reason as :func:`_name_parts` (one parse per address field per row is expensive). The
    returned dict is shared through the cache and must not be mutated.
    """
    try:
        tagged, _ = usaddress.tag(full)
    except usaddress.RepeatedLabelError:
        return {"line1": full, "line2": "", "city": "", "state": "", "zip": ""}
    out = {}
    for part, tags in _ADDRESS_TAGS.items():
        # Join the tagged components in _ADDRESS_TAGS order and trim stray commas left by "Street, City, ST" input.
        out[part] = " ".join(tagged[t] for t in tags if t in tagged).strip().strip(",").strip()
    if not out["line1"] and not out["city"]:
        out["line1"] = full
    return out


def apply_transform(transform: str, row: dict[str, str], sources: list[str], params: dict) -> str:
    """Extract the raw value for one target field from one source row.

    Args:
        transform: A key of :data:`TRANSFORMS` (e.g. "copy", "name_part").
        row: One source row as ``{column name: cell text}`` (the engine passes text-only records).
        sources: The source columns to read, in priority/joining order.
        params: Transform params (only the ones listed in the registry entry are meaningful here).

    Returns:
        The raw string for the field ("" when there is nothing to extract). The value is cleaned text but not yet
        type-normalized; the engine passes it to the field's normalizer next.

    Raises:
        ValueError: for a transform name not in the registry. Mappings are normalized before conversion, so this
            only happens on programming errors.

    Pure function with no side effects; behaviour per transform (input -> output):
        * none:         anything -> ""
        * copy:         "  Ana " -> "Ana" (if several sources are ever given, non-empty values are space-joined)
        * coalesce:     ["", "2024-01-15"] -> "2024-01-15" (first non-empty, in source order)
        * each_row:     same as coalesce here; engine._expand_rows creates the extra rows before this is used
        * concat:       ["516", "555-0100"] + separator "-" -> "516-555-0100"; empty values are skipped
        * name_part:    "SMITH, JOHN" + part "last" -> "SMITH" (default part "first")
        * address_part: "12 Main St Apt 4B, Hicksville, NY 11801" + part "city" -> "Hicksville" (default "line1")
        * split:        "a, b , c" + separator "," index 1 -> "b" (pieces are stripped; default separator ",")
        * map_values:   "y" with value_map [{"from": "Y", "to": "true"}] -> "true"; unlisted values unchanged
        * constant:     params["value"] for every row (or "" when no value is set)
    """
    vals = _src(row, sources)
    if transform == "none":
        return ""
    if transform == "copy":
        return " ".join(v for v in vals if v)
    if transform in ("coalesce", "each_row"):  # each_row: the engine expands rows; alone it acts like coalesce
        return next((v for v in vals if v), "")
    if transform == "concat":
        # A missing separator means a space. (Mapping.normalized drops empty-string params via clean_params, so a
        # blank separator chosen in the UI also ends up as a space.)
        sep = params.get("separator")
        sep = " " if sep is None else sep
        return sep.join(v for v in vals if v)
    if transform == "name_part":
        # Empty source -> "" (don't call nameparser on ""); unknown parts were already rejected by check_transform.
        return _name_parts(vals[0])[params.get("part") or "first"] if vals and vals[0] else ""
    if transform == "address_part":
        return _address_parts(vals[0])[params.get("part") or "line1"] if vals and vals[0] else ""
    if transform == "split":
        if not vals or not vals[0]:
            return ""
        sep = params.get("separator") or ","
        pieces = [p.strip() for p in vals[0].split(sep)]
        # index may arrive as a string from the UI/Claude ("1"); negative indexes count from the end like Python.
        idx = int(params.get("index") or 0)
        # Out-of-range index -> "" instead of IndexError, so rows with fewer pieces just produce a blank value.
        return pieces[idx] if -len(pieces) <= idx < len(pieces) else ""
    if transform == "map_values":
        if not vals or not vals[0]:
            return ""
        # Lookup is case- and surrounding-whitespace-insensitive on the "from" side; the "to" value is kept as typed.
        table = {str(m.get("from", "")).strip().lower(): str(m.get("to", "")) for m in params.get("value_map") or []}
        return table.get(vals[0].lower(), vals[0])
    if transform == "constant":
        return str(params.get("value") or "")
    raise ValueError(f"Unknown transform '{transform}'")


def check_transform(transform: str, sources: list[str], params: dict) -> str | None:
    """Return a problem description if the transform/sources/params combination is invalid, else ``None``.

    Checks, in order:
        1. the transform exists in :data:`TRANSFORMS`;
        2. the number of source columns is within ``min_sources``..``max_sources``
           (e.g. "concat" with one column -> "'Combine columns' needs at least 2 source column(s)");
        3. name_part / address_part use a known part (missing part = the default "first" / "line1");
        4. split's index is an integer (or integer-like string such as "2").

    The returned text is user-facing: it is shown next to the field in the mapping editor and in the CLI
    ("PROBLEM ..."), and ``Mapping.normalized`` resets any field that fails this check to "none" so an invalid
    template or Claude suggestion cannot reach the engine. No side effects.
    """
    info = TRANSFORMS.get(transform)
    if info is None:
        return f"unknown transform '{transform}'"
    n = len(sources)
    if n < info.min_sources:
        return f"'{info.label}' needs at least {info.min_sources} source column(s)"
    if info.max_sources is not None and n > info.max_sources:
        return f"'{info.label}' takes at most {info.max_sources} source column(s)"
    if transform == "name_part" and (params.get("part") or "first") not in NAME_PARTS:
        return f"name part must be one of {', '.join(NAME_PARTS)}"
    if transform == "address_part" and (params.get("part") or "line1") not in ADDRESS_PARTS:
        return f"address part must be one of {', '.join(ADDRESS_PARTS)}"
    if transform == "split":
        try:
            int(params.get("index") or 0)
        except (TypeError, ValueError):
            return "split index must be a number"
    return None

