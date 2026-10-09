"""Extraction transforms: how a target field's raw value is pulled from one or more source columns.

A mapping assigns each target field a transform + source columns + params. The transform produces
a raw string; the field's type normalizer (normalizers.py) then formats and validates it.
Claude may only choose from this registry — it never writes code at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import usaddress
from nameparser import HumanName

from .normalizers import clean_text


@dataclass(frozen=True)
class TransformInfo:
    name: str
    label: str
    description: str
    min_sources: int
    max_sources: int | None  # None = unlimited
    params: tuple[str, ...] = ()


NAME_PARTS = ("first", "middle", "last", "first_middle", "title", "suffix", "full")
ADDRESS_PARTS = ("line1", "line2", "city", "state", "zip")
DATE_ORDERS = ("auto", "MDY", "DMY", "YMD")

TRANSFORMS: dict[str, TransformInfo] = {
    t.name: t
    for t in [
        TransformInfo("none", "Leave blank / default", "No source column; output is blank or the field default.", 0, 0),
        TransformInfo("copy", "Copy column", "Use the source column's value as-is (then type-normalized).", 1, 1,
                      ("date_order", "input_format", "region")),
        TransformInfo("coalesce", "First non-empty", "Use the first non-empty value across several columns, in order "
                      "(e.g. Mobile, then Phone).", 2, None, ("date_order", "input_format", "region")),
        TransformInfo("each_row", "One row per value", "Creates a separate output row for each different value "
                      "across these columns (e.g. Phone, Phone 2, Phone 3); every other field is copied onto each row. "
                      "Only one field can use this.", 2, None, ("date_order", "input_format", "region")),
        TransformInfo("concat", "Combine columns", "Join several columns with a separator (default a space).", 2, None,
                      ("separator",)),
        TransformInfo("name_part", "Part of a full name", "Extract first/middle/last/etc. from a full-name column "
                      "(handles 'Last, First').", 1, 1, ("part",)),
        TransformInfo("address_part", "Part of a full address", "Extract street line, unit, city, state or ZIP from a "
                      "single-line address column.", 1, 1, ("part",)),
        TransformInfo("split", "Split column", "Split a column by a separator and take the Nth piece (0 = first).",
                      1, 1, ("separator", "index")),
        TransformInfo("map_values", "Translate values", "Replace specific source values with output values "
                      "(e.g. 'Y' → 'true'); unlisted values pass through.", 1, 1, ("value_map",)),
        TransformInfo("constant", "Fixed value", "Every row gets the same fixed value.", 0, 0, ("value",)),
    ]
}


def _src(row: dict[str, str], sources: list[str]) -> list[str]:
    return [clean_text(row.get(s, "")) for s in sources]


@lru_cache(maxsize=100_000)
def _name_parts(full: str) -> dict[str, str]:
    n = HumanName(full)
    first_middle = " ".join(p for p in [n.first, n.middle] if p)
    return {
        "first": n.first,
        "middle": n.middle,
        "last": " ".join(p for p in [n.last] if p),
        "first_middle": first_middle,
        "title": n.title,
        "suffix": n.suffix,
        "full": str(n).strip() or full,
    }


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
    try:
        tagged, _ = usaddress.tag(full)
    except usaddress.RepeatedLabelError:
        return {"line1": full, "line2": "", "city": "", "state": "", "zip": ""}
    out = {}
    for part, tags in _ADDRESS_TAGS.items():
        out[part] = " ".join(tagged[t] for t in tags if t in tagged).strip().strip(",").strip()
    if not out["line1"] and not out["city"]:
        out["line1"] = full
    return out


def apply_transform(transform: str, row: dict[str, str], sources: list[str], params: dict) -> str:
    vals = _src(row, sources)
    if transform == "none":
        return ""
    if transform == "copy":
        return " ".join(v for v in vals if v)
    if transform in ("coalesce", "each_row"):  # each_row: the engine expands rows; alone it acts like coalesce
        return next((v for v in vals if v), "")
    if transform == "concat":
        sep = params.get("separator")
        sep = " " if sep is None else sep
        return sep.join(v for v in vals if v)
    if transform == "name_part":
        return _name_parts(vals[0])[params.get("part") or "first"] if vals and vals[0] else ""
    if transform == "address_part":
        return _address_parts(vals[0])[params.get("part") or "line1"] if vals and vals[0] else ""
    if transform == "split":
        if not vals or not vals[0]:
            return ""
        sep = params.get("separator") or ","
        pieces = [p.strip() for p in vals[0].split(sep)]
        idx = int(params.get("index") or 0)
        return pieces[idx] if -len(pieces) <= idx < len(pieces) else ""
    if transform == "map_values":
        if not vals or not vals[0]:
            return ""
        table = {str(m.get("from", "")).strip().lower(): str(m.get("to", "")) for m in params.get("value_map") or []}
        return table.get(vals[0].lower(), vals[0])
    if transform == "constant":
        return str(params.get("value") or "")
    raise ValueError(f"Unknown transform '{transform}'")


def check_transform(transform: str, sources: list[str], params: dict) -> str | None:
    """Return a problem description if the transform/sources/params combination is invalid."""
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

