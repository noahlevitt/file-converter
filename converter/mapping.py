"""Mapping spec (target field → source columns + transform + params), column profiling, and fingerprints.

Overview
--------
Responsibilities
    * **Mapping model** - :class:`FieldMapping` says how ONE target field gets its value (which source columns,
      which transform from ``transforms.TRANSFORMS``, which params, and who decided it). :class:`Mapping` holds
      one per schema field plus the reviewer's column notes and skipped ("excluded") columns. It is what the
      rules, Claude, templates and the reviewer all produce and edit, and what the engine executes.
    * **Normalisation** - :meth:`Mapping.normalized` makes any mapping (from a template saved on another file,
      from Claude, from the UI) safe for the current file and schema: unknown fields/columns dropped, skipped
      columns removed, transforms fixed up (:func:`phone_rule`), invalid combinations reset to "not mapped".
    * **Problems** - :meth:`Mapping.problems` lists what blocks a conversion (missing required fields,
      wrong number of source columns, two "one row per phone number" fields).
    * **Row field** - :meth:`Mapping.row_field` names the phone field whose different numbers each get their own
      output row (from several columns, or several numbers in one cell).
    * **Header fingerprints** - :func:`header_fingerprint` / :func:`header_similarity` recognise a file layout
      seen before, so a saved template can be reused (``storage.Store.find_template``).
    * **Column profiling** - :func:`profile_columns` summarises each source column (fill counts, samples, share
      of values that look like a phone / email / date / ZIP / state) for the auto-mapper and Claude's prompt.

Data flow
    DataFrame (``reader.read_file``) -> :func:`profile_columns` -> ``heuristics.auto_map`` / template / Claude
    -> :class:`Mapping` -> :meth:`Mapping.normalized` -> review grid edits (``editor.py``) ->
    :meth:`Mapping.problems` -> ``engine.convert``. Templates store the mapping together with the headers and
    their fingerprint.

Example mapping entry (JSON as saved in a template)::

    "phone_number": {"sources": ["Mobile", "Home Phone"], "transform": "each_row", "params": {},
                     "origin": "rule", "confidence": "medium", "reason": "2 phone columns: one row per ..."}
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, Field

from . import normalizers as N
from .schema import FieldSpec, TargetSchema
from .transforms import TRANSFORMS, check_transform

# Who set a field's mapping: a saved template, the rule-based matcher (heuristics.py), Claude ("ai"), the reviewer
# in the web UI ("user"), or nobody yet (""). Shown in the review grid as "Mapped by".
Origin = Literal["template", "rule", "ai", "user", ""]

# Every param name any transform reads (the union of TransformInfo.params in transforms.py):
#   date_order / input_format - how to read source dates ("MDY", or a strptime pattern like "%d.%m.%Y")
#   region                   - default country for phone numbers without a country code ("US")
#   part                     - which part of a name / address ("first", "line1", ...)
#   separator / index        - combine: joiner; split: separator and 0-based piece number
#   value_map                - translate: [{"from": "Y", "to": "true"}, ...]
#   value                    - fixed value for every row
# The per-transform filter is FieldMapping.clean_params.
ALLOWED_PARAMS = {"date_order", "input_format", "region", "part", "separator", "index", "value_map", "value"}


def phone_rule(field_type: str, transform: str) -> str:
    """Several phone columns ALWAYS give one row per different number; 'One row per phone number' is only for phones.

    Also upgrades older templates that used 'First non-empty' on the phone field.

    Inputs: the target field's type and the requested transform name. Output: the transform to actually use.

    * phone field + "coalesce" (First non-empty) -> "each_row": taking only the first number would silently drop
      the others, and every number of a contact should be imported.
    * any other field + "each_row" -> "coalesce": expanding rows is only defined for the phone field.
    * everything else is returned unchanged.

    Examples: ``phone_rule("phone", "coalesce") -> "each_row"``, ``phone_rule("date", "each_row") ->
    "coalesce"``, ``phone_rule("phone", "copy") -> "copy"``.
    """
    if field_type == "phone" and transform == "coalesce":
        return "each_row"
    if field_type != "phone" and transform == "each_row":
        return "coalesce"
    return transform


class FieldMapping(BaseModel):
    """How one target field gets its value: source columns + transform + params, and where that choice came from.

    The default instance (no sources, transform "none") means "not mapped": the output is blank or the field's
    default.

    Example::

        FieldMapping(sources=["Customer Name"], transform="name_part", params={"part": "first"},
                     origin="rule", confidence="medium", reason="split full name in 'Customer Name'")
    """

    # Source column names, in order (order matters for coalesce / concat / each_row).
    sources: list[str] = Field(default_factory=list)
    # Transform name, a key of transforms.TRANSFORMS ("none", "copy", "coalesce", "each_row", "concat", ...).
    transform: str = "none"
    # Transform options, e.g. {"part": "first"} or {"date_order": "DMY"}; see ALLOWED_PARAMS.
    params: dict[str, Any] = Field(default_factory=dict)
    # Who set this mapping (template / rule / ai / user).
    origin: Origin = ""
    # How sure the rules or Claude were; empty for reviewer choices. Template fields keep the confidence they were
    # saved with.
    confidence: Literal["high", "medium", "low", ""] = ""
    # Short explanation shown to the reviewer, e.g. "header 'Mobile' matches" or "set by reviewer".
    reason: str = ""

    def clean_params(self) -> dict[str, Any]:
        """Params the current transform actually reads, without empty values.

        Params of another transform (left over after the reviewer changed "How") and empty values (None, "", [])
        are dropped, so saved templates stay clean and an empty value never overrides a transform's default.
        Example: transform "copy" with ``{"part": "first", "date_order": "DMY", "region": ""}`` ->
        ``{"date_order": "DMY"}``. Unknown transforms keep no params.
        """
        allowed = set(TRANSFORMS[self.transform].params) if self.transform in TRANSFORMS else set()
        return {k: v for k, v in self.params.items() if k in allowed and v not in (None, "", [])}

    def summary(self) -> str:
        """Short human-readable description of the mapping, for the UI, the CLI and job summaries.

        Examples::

            copy of "Mobile"                       -> "Mobile"
            name_part first of "Customer Name"     -> "Part of a full name: Customer Name (first)"
            each_row of "Phone", "Phone 2"         -> "one row per value in Phone, Phone 2"
            constant "NY"                          -> "fixed value 'NY'"
            not mapped                             -> "—"
        """
        if self.transform == "none":
            return "—"
        if self.transform == "constant":
            return f"fixed value '{self.params.get('value', '')}'"
        src = " + ".join(self.sources)
        info = TRANSFORMS.get(self.transform)
        extra = ""
        if self.transform in ("name_part", "address_part"):
            extra = f" ({self.params.get('part', '')})"
        elif self.transform == "split":
            # Pieces are stored 0-based but shown 1-based ("piece 1" = first piece).
            extra = f" (split on '{self.params.get('separator', ',')}', piece {int(self.params.get('index') or 0) + 1})"
        elif self.transform == "coalesce":
            return "first non-empty of " + ", ".join(self.sources)
        elif self.transform == "each_row":
            return "one row per value in " + ", ".join(self.sources)
        # A plain copy shows just the column; other transforms are prefixed with their UI label.
        label = "" if self.transform == "copy" else f"{info.label if info else self.transform}: "
        return f"{label}{src}{extra}"


class Mapping(BaseModel):
    """The complete mapping for one file: one :class:`FieldMapping` per target field, plus reviewer notes.

    ``fields`` is keyed by the schema's internal field names. Saved as JSON in templates and import records,
    produced by the rules / Claude / templates and edited in the review grid. Always run :meth:`normalized`
    before using a mapping on a (new) file.
    """

    # Target field name -> how it is filled.
    fields: dict[str, FieldMapping] = Field(default_factory=dict)
    # Reviewer notes, saved with templates and imports: what a source column is, where it goes, how to read it.
    column_notes: dict[str, str] = Field(default_factory=dict)
    # Free-text note about the whole file / layout.
    general_note: str = ""
    # Source columns the reviewer marked "skip": never imported, and rules/templates/Claude can't map them.
    excluded_columns: list[str] = Field(default_factory=list)

    def get(self, name: str) -> FieldMapping:
        """Mapping of field ``name``; an empty "not mapped" FieldMapping when the field has no entry.

        Note: the returned default is a new object, so changing it does not change this mapping.
        """
        return self.fields.get(name) or FieldMapping()

    def targets_of(self, column: str) -> list[str]:
        """Target fields that read from this source column.

        Used for the "Goes to" cell of the column-side table. Example: a "Customer Name" column split into first
        and last name -> ``["first_name", "last_name"]``. Unmapped fields are ignored.
        """
        return [n for n, fm in self.fields.items() if fm.transform != "none" and column in fm.sources]

    def used_sources(self) -> set[str]:
        """All source columns that feed at least one mapped field (everything else is not imported)."""
        return {s for fm in self.fields.values() if fm.transform != "none" for s in fm.sources}

    def normalized(self, schema: TargetSchema, columns: list[str]) -> "Mapping":
        """Ensure every schema field is present, drop unknown fields and sources missing from the file.

        Makes a mapping from anywhere (a template saved on another file, Claude's answer, the UI) consistent with
        the current schema and the file's ``columns``. Returns a NEW mapping; ``self`` is not changed.

        Rules, per schema field:

        1. Fixed-blank fields are always "not mapped", with a reason saying so. Manual-only fields (consent,
           last_contacted) keep a mapping only when a person made it (``origin == "user"``): whatever the rules,
           a template or Claude put there is removed, so they are never filled without a human choosing the column.
        2. Source columns are matched to the file case-insensitively and rewritten in the file's spelling
           (a template made on "PHONE" works on "Phone"); failing that, ignoring spaces and punctuation when only
           one column fits ("Last Visit Date" works on "Last_Visit_Date", as templates are matched that way);
           columns missing from the file or skipped by the reviewer are removed.
        3. An unknown transform becomes "none"; "none" and "constant" never keep sources.
        4. :func:`phone_rule`: "First non-empty" on a phone field becomes "One row per phone number", and
           "One row per phone number" on any other field becomes "First non-empty".
        5. A multi-column transform (coalesce / concat / each_row) left with one source becomes a plain copy
           (e.g. a template for "Phone" + "Phone 2" used on a file that only has "Phone").
        6. A transform that needs sources but has none left, or that :func:`~converter.transforms.check_transform`
           rejects (wrong number of columns, bad part name, ...), resets the field to "not mapped".
        7. Params are reduced to those the transform reads (:meth:`FieldMapping.clean_params`).

        Fields not in the schema are dropped. Excluded columns and column notes are kept only for columns in this
        file (in the file's spelling; blank notes dropped); the general note is kept as is.
        """
        # lower-case name -> the file's spelling of that column
        lookup = {c.lower(): c for c in columns}
        # Fallback: letters and digits only ("Last_Visit_Date" = "Last Visit Date" = "last-visit-date"), as template
        # matching compares headers (header_fingerprint). Only used when exactly one column of the file has that form.
        by_form: dict[str, list[str]] = {}
        for c in columns:
            by_form.setdefault(norm_header(c), []).append(c)

        def find(name: str) -> str | None:
            """The file's column for a name from the mapping, or None when the file has no such column."""
            if name.lower() in lookup:
                return lookup[name.lower()]
            same = by_form.get(norm_header(name), [])
            return same[0] if len(same) == 1 and norm_header(name) else None

        # Skipped columns that exist in this file, in the file's spelling, without duplicates (order kept).
        excluded = list(dict.fromkeys(c for c in map(find, self.excluded_columns) if c))
        skip = {c.lower() for c in excluded}
        out: dict[str, FieldMapping] = {}
        for f in schema.fields:
            if f.fixed_blank:
                out[f.name] = FieldMapping(reason="always blank — fixed by the import format")
                continue
            # Deep copy: the input mapping (e.g. a cached template) must not be modified.
            fm = self.fields.get(f.name, FieldMapping()).model_copy(deep=True)
            if f.manual_only and fm.transform != "none" and fm.origin != "user":
                out[f.name] = FieldMapping(reason="mapped by hand only")
                continue
            fm.sources = [c for c in map(find, fm.sources) if c and c.lower() not in skip]
            if fm.transform not in TRANSFORMS:
                fm.transform = "none"
            if fm.transform in ("none", "constant"):
                fm.sources = []
            fm.transform = phone_rule(f.type, fm.transform)
            if fm.transform in ("coalesce", "concat", "each_row") and len(fm.sources) == 1:
                fm.transform = "copy"
            if fm.transform not in ("none", "constant") and not fm.sources:
                fm = FieldMapping()  # its source column isn't in this file
            elif check_transform(fm.transform, fm.sources, fm.params):
                fm = FieldMapping()  # invalid combination (e.g. a copy of two columns): better unmapped than wrong
            fm.params = fm.clean_params()
            out[f.name] = fm
        notes = {find(c): n for c, n in self.column_notes.items() if find(c) and n.strip()}
        return Mapping(fields=out, column_notes=notes, general_note=self.general_note, excluded_columns=excluded)

    def problems(self, schema: TargetSchema) -> dict[str, str]:
        """Problems that block a conversion, as {field name: message}; empty when the mapping can be run.

        * an invalid transform / source / params combination (message from ``check_transform``);
        * a required field that is not mapped and has no default ("required field has no source column");
        * more than one field using "One row per phone number" - rows can only be expanded for one field, so
          every such field after the first gets a message naming the field that already does.

        Fixed-blank fields are never checked. Example: ``{"phone_number": "required field has no source column"}``.
        """
        issues = {}
        for f in schema.mappable_fields:
            fm = self.get(f.name)
            p = check_transform(fm.transform, fm.sources, fm.params)
            if p:
                issues[f.name] = p
            elif f.required and fm.transform == "none" and not f.default:
                issues[f.name] = "required field has no source column"
        multi = [f.name for f in schema.mappable_fields if self.get(f.name).transform == "each_row"]
        for name in multi[1:]:
            issues[name] = (f"only one field can use 'One row per phone number' ({schema.field(multi[0]).header} "
                            "already does)")
        return issues

    def without_manual(self, schema: TargetSchema) -> "Mapping":
        """A copy without the hand-mapped-only fields (consent, last_contacted): what a saved template may keep.

        A template is applied automatically to the next file, and these fields must only ever be filled by a person
        choosing the column for that file, so they are left out of templates.
        """
        out = self.model_copy(deep=True)
        for f in schema.fields:
            if f.manual_only:
                out.fields[f.name] = FieldMapping()
        return out

    def row_field(self, schema: TargetSchema) -> FieldSpec | None:
        """The field whose numbers each get their own output row, or None when no field does.

        That is the field using 'One row per phone number' (several phone columns), otherwise the first phone field
        copied from a single column: one cell can hold several numbers too ("516-555-0100 / 917-555-1234"), and each
        different number gets its own row either way. Phone fields built any other way (combined columns, a fixed
        value) never expand rows.

        Used by the engine (which rows to expand), the side-by-side view (which cells to underline) and the mapping
        page (the "one row per number" note). Call it on a normalized mapping, as those callers do.
        """
        fields = schema.mappable_fields
        return (next((f for f in fields if self.get(f.name).transform == "each_row"), None)
                or next((f for f in fields if f.type == "phone" and self.get(f.name).transform == "copy"), None))


# ---------------------------------------------------------------------------
# Header fingerprints (used to recognise a file layout seen before)
# ---------------------------------------------------------------------------

# The stand-in names the reader gives the columns of a file without a header row ("Column 1", "Column 2", ...).
_GENERIC_COLUMN_RE = re.compile(r"Column \d+")


def has_generic_columns(columns: list[str]) -> bool:
    """True when every column has the reader's stand-in name ("Column 1", "Column 2", ...): no header row.

    Such names say nothing about what a file holds - every headerless file with 3 columns is "Column 1-3" - so
    they can't identify a saved template; ``storage.Store.find_template`` never matches them and the mapping page
    offers the templates to pick by hand instead.
        ["Column 1", "Column 2"] → True;  ["Column 1", "Phone"] → False;  [] → False
    """
    return bool(columns) and all(_GENERIC_COLUMN_RE.fullmatch(str(c)) for c in columns)


def norm_header(h: str) -> str:
    """Header reduced to lower-case letters and digits, for comparisons that ignore case, spaces and punctuation.

    Examples: ``norm_header("Cell #") -> "cell"``, ``norm_header("First_Name") -> "firstname"``,
    ``norm_header("E-mail Address") -> "emailaddress"``, ``norm_header("#") -> ""``.
    """
    return re.sub(r"[^a-z0-9]+", "", h.lower())


def header_fingerprint(columns: list[str]) -> str:
    """Short id (16 hex chars) of a file's header SET, used to find a saved template for the exact same layout.

    Headers are normalised with :func:`norm_header`, de-duplicated and sorted before hashing, so column order,
    case, spacing and punctuation don't matter: ``["Phone", "First Name"]`` and ``["first_name", "PHONE"]``
    give the same fingerprint. Headers with no letters or digits are ignored. SHA-1 is only used as a compact,
    stable key here, not for security.
    """
    key = "|".join(sorted({norm_header(c) for c in columns if norm_header(c)}))
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def header_similarity(a: list[str], b: list[str]) -> float:
    """How alike two header lists are, 0.0-1.0: the Jaccard index of their normalised header sets.

    ``|common| / |all distinct|``. Used to find a saved template for a similar layout (e.g. the same export
    with one extra column) when no fingerprint matches exactly; ``storage.Store.find_template`` requires 0.75 by
    default. Example: ``["Phone", "Name", "Email"]`` vs ``["phone", "name", "zip"]`` -> 2 / 4 = 0.5.
    Returns 0.0 when either list has no usable header.
    """
    sa = {norm_header(x) for x in a if norm_header(x)}
    sb = {norm_header(x) for x in b if norm_header(x)}
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# ---------------------------------------------------------------------------
# Column profiling
# ---------------------------------------------------------------------------

class ColumnProfile(BaseModel):
    """Summary of one source column: what the auto-mapper, the checks and Claude know about it.

    Built by :func:`profile_columns`. Only headers and these few sample values are sent to Claude, never the
    whole file.

    Example::

        ColumnProfile(name="Mobile", non_empty=950, total=1000, unique=940,
                      samples=["(201) 555-0111", ...], shares={"phone": 0.98, "email": 0.0, "date": 0.0,
                      "zip": 0.0, "state": 0.0}, max_length=14)
    """

    # Column header as it appears in the DataFrame (already made unique by the reader).
    name: str
    # Number of rows with a value (after cleaning; "n/a", "null", "-" ... count as empty).
    non_empty: int
    # Number of data rows in the file.
    total: int
    # Number of distinct non-empty values.
    unique: int
    # First distinct values (at most n_samples, each cut to 120 characters).
    samples: list[str]
    shares: dict[str, float]  # share of sampled values that look like each type
    # (computed over the distinct values, up to sample_size of them; keys: phone, email, date, zip, state; empty
    # dict for an empty column)
    # Length of the longest value, in characters.
    max_length: int

    @property
    def fill_rate(self) -> float:
        """Share of rows that have a value (0.0-1.0); 0.0 for a file without data rows."""
        return self.non_empty / self.total if self.total else 0.0

    def best_type(self) -> tuple[str, float]:
        """The value type most values look like, and its share; ``("string", 0.0)`` for an empty column.

        Example: ``shares={"phone": 0.9, "date": 0.1, ...}`` -> ``("phone", 0.9)``. Ties go to the type listed
        first in ``shares`` (phone, email, date, zip, state).
        """
        if not self.shares:
            return "string", 0.0
        t = max(self.shares, key=self.shares.get)
        return t, self.shares[t]


def profile_columns(df: pd.DataFrame, sample_size: int = 300, n_samples: int = 15) -> list[ColumnProfile]:
    """Profile every column of the (text-only) DataFrame, in column order.

    Inputs: ``df`` from ``reader.read_file``; ``sample_size`` = how many distinct values are tested for their
    type; ``n_samples`` = how many distinct values are kept as examples (shown to the reviewer and to Claude).

    Why distinct values: the type shares say what KINDS of values a column holds, so a value repeated on 500 rows
    counts once and can't drown out the rest; testing at most ``sample_size`` values keeps big files fast.

    Example: a column ["2015550111", "2015550111", "n/a", "foo"] -> non_empty 3 ("n/a" counts as empty),
    unique 2, shares["phone"] 0.5 (1 of the 2 distinct values).
    """
    profiles = []
    total = len(df)
    for col in df.columns:
        # Clean every cell the same way the converter will (whitespace collapsed, "null"/"n/a"/"-" -> "").
        series = [N.clean_text(v) for v in df[col].tolist()]
        non_empty = [v for v in series if v]
        uniq = list(dict.fromkeys(non_empty))  # distinct values, in order of first appearance
        sample = uniq[:sample_size]
        shares: dict[str, float] = {}
        if sample:
            n = len(sample)
            # Share of the sampled distinct values each lightweight detector accepts. Each detector is applied on
            # its own, so the shares need not add up to 1 (free text matches none of them).
            shares = {
                # Cells with several numbers count: each number becomes its own row (see normalizers.split_phones).
                "phone": sum(N.looks_like_phones(v) for v in sample) / n,
                "email": sum(N.looks_like_email(v) for v in sample) / n,
                "date": sum(N.looks_like_date(v) for v in sample) / n,
                "zip": sum(N.looks_like_zip(v) for v in sample) / n,
                "state": sum(N.looks_like_state(v) for v in sample) / n,
            }
        profiles.append(
            ColumnProfile(
                name=col,
                non_empty=len(non_empty),
                total=total,
                unique=len(uniq),
                samples=[v[:120] for v in uniq[:n_samples]],
                shares={k: round(v, 3) for k, v in shares.items()},
                max_length=max((len(v) for v in non_empty), default=0),
            )
        )
    return profiles
