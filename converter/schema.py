"""Target schema: the fields the destination system accepts, and how output files must look.

The schema lives in a YAML file (schemas/target_schema.yaml). Everything else in the tool is
driven by it, so supporting the real import format only requires editing that file.

Overview
--------
Responsibilities
    * Define the pydantic models that describe the import format: :class:`FieldSpec` (one output column),
      :class:`OutputSpec` (how the file is written), :class:`DedupeSpec` (what counts as a duplicate row) and
      :class:`TargetSchema` (all of it together).
    * Validate the YAML when it is loaded, so a mistake in the schema file fails loudly at start-up instead of
      producing a wrong import file (duplicate field names, unknown dedupe keys, enum without values, ...).
    * Fill in derived defaults (output header = field name, ``on_invalid`` from ``required``, and dedupe
      ``["*"]`` written in the YAML expanded to every field that isn't fixed blank; with no dedupe keys, dedupe is
      off).

Main entry points
    * :func:`load_schema` - read and validate the YAML file (used by the web app and the CLI).
    * :class:`TargetSchema` - ``fields``, ``field(name)``, ``mappable_fields``, ``label``.
    * :data:`TYPE_DESCRIPTIONS` - plain-English description of each field type (used in Claude's prompts).

Data flow
    ``target_schema.yaml`` -> :func:`load_schema` -> ``TargetSchema`` -> used by the auto-mapper
    (``heuristics.py``: names, synonyms, types), the mapping checks (``mapping.py``/``editor.py``), the type
    normalisers (``normalizers.py``: format, case, countries, ...), the engine (required / on_invalid / defaults
    / dedupe) and the writer (``output.py``: ``OutputSpec``).

Example YAML field::

    - name: phone_number
      header: phone_number
      type: phone
      required: true
      format: digits10
      countries: [US, PR]
      synonyms: [phone, mobile, cell]
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

# The value types a target field can have. Each type has its own normaliser in normalizers.py (cleaning,
# formatting and validation) and some get value-type evidence during auto-mapping (heuristics.TYPED).
FieldType = Literal[
    "string", "phone", "date", "datetime", "email", "zip", "state", "integer", "decimal", "boolean", "enum"
]

# Short plain-English description of each field type. Shown to Claude in the mapping / repair prompts (ai.py), so
# it knows what the normaliser will do with a value; keep one entry per FieldType.
TYPE_DESCRIPTIONS: dict[str, str] = {
    "string": "free text (trimmed, whitespace collapsed)",
    "phone": "telephone number, normalized and validated",
    "date": "calendar date",
    "datetime": "date with time",
    "email": "email address (lower-cased, syntax-validated)",
    "zip": "US ZIP code (leading zeros restored)",
    "state": "US state / territory, output as 2-letter USPS code",
    "integer": "whole number",
    "decimal": "number with decimals",
    "boolean": "yes/no value",
    "enum": "one of a fixed list of allowed values",
}


class FieldSpec(BaseModel):
    """One column of the output file: its name, type, output format and validation rules.

    Loaded from one entry of ``fields:`` in the schema YAML. After validation (:meth:`_defaults`) ``header`` is
    never empty and ``on_invalid`` is always set.

    Example::

        FieldSpec(name="birthday", type="date", format="%m/%d/%Y", no_year_value=1600)
        # header -> "birthday", on_invalid -> "blank" (optional field)
    """

    # Internal id, e.g. "phone_number". Keys the mapping, templates, dedupe keys and Claude's answers.
    name: str = Field(description="Internal field id (snake_case). Used in templates and dedupe keys.")
    # Column header in the output file; defaults to `name` when left empty.
    header: str = Field("", description="Exact column header written to the output file.")
    # Value type: picks the normaliser (formatting + validation) and the auto-mapper's value evidence.
    type: FieldType = "string"
    # Required: a row without a usable value for this field is rejected (unless a `default` fills it).
    required: bool = False
    # Output format, meaning depends on the type (strftime pattern for dates, phone/zip style keyword).
    format: str | None = Field(
        None,
        description=(
            "Output format. date/datetime: strftime pattern (e.g. %m/%d/%Y). "
            "phone: E164 | digits10 | digits11 | national | dashed. zip: zip5 | zip5_4."
        ),
    )
    # Human description of the field; included in Claude's mapping prompt and in describe().
    description: str = ""
    # Other header names that mean this field ("mobile", "cell #", ...). Main input of the rule-based matcher.
    synonyms: list[str] = Field(default_factory=list, description="Other header names that mean this field.")
    # Maximum output length in characters (None = no limit). Longer values are invalid unless `truncate` is set.
    max_length: int | None = None
    # string fields: cut long values to max_length instead of treating them as invalid.
    truncate: bool = Field(False, description="string only: truncate to max_length instead of treating as invalid")
    # Re-casing of string values: upper / lower / title, or "name" (person-name aware capitalisation such as
    # "MCDONALD" -> "McDonald"). title/name only touch values that are all upper- or all lower-case.
    case: Literal["none", "upper", "lower", "title", "name"] = "none"
    # enum fields: the only values accepted in the output (validated to be non-empty for enums).
    allowed_values: list[str] = Field(default_factory=list, description="enum only")
    # Value written when the source is empty (not used for invalid values; see on_invalid).
    default: str | None = Field(None, description="Value used when the source is empty")
    # Invalid value policy: "reject" the whole row or "blank" the value and keep the row.
    # None in the YAML means: reject for required fields, blank for optional ones (filled in by _defaults).
    on_invalid: Literal["reject", "blank"] | None = Field(
        None,
        description="What to do when a value can't be converted: reject the row or blank the value. "
        "Defaults to reject for required fields and blank for optional fields.",
    )
    # phone fields: ISO country codes accepted; the first one is the default region for numbers without a
    # country code (so "201 555 0100" is read as a US number).
    countries: list[str] = Field(default_factory=lambda: ["US"], description="phone only: allowed ISO country codes")
    # boolean fields: the text written for yes / no values (e.g. "Y" / "N", "1" / "0").
    true_value: str = "true"
    false_value: str = "false"
    # Always blank on every row, whatever the file says (e.g. consent: any value would change subscriptions).
    # Such fields are excluded from mapping, defaults, Claude's suggestions and reviewer edits.
    fixed_blank: bool = Field(
        False,
        description="Always output blank on every row. The field can't be mapped, defaulted, set by Claude or "
        "edited by users (e.g. a consent column where any value would change subscription status).",
    )
    # Filled only when a person maps a column to it on the mapping page: never by the rules, a template, Claude or a
    # default (e.g. consent: a value changes subscriptions, so a human must choose the column every time).
    manual_only: bool = Field(
        False,
        description="Only filled when a person maps a column to it by hand on the mapping page; never by the "
        "automatic rules, a saved template, Claude or a default.",
    )
    # boolean fields (0.22.0; per column since 0.23.0): whoever maps a column to this field chooses what it records,
    # its consent setting: opt-ins only (a no is left blank), opt-ins and opt-outs (a no is written as false) or
    # opt-outs only (a yes is written as false, a no left blank). Mapping param "modes": {column: "in" | "both" |
    # "out"} (mapping.CONSENT_MODES); several columns are combined and an opt-out in any of them wins. For consent:
    # an email opt-in column's "No" isn't a text opt-out, and an Unsubscribed column's "Yes" is one.
    opt_out_choice: bool = Field(
        False,
        description="boolean only: whoever maps a column to this field chooses what each column records (its consent "
        "setting): opt-ins only, opt-ins and opt-outs, or opt-outs only; importing is blocked until they choose.",
    )
    # date/datetime fields: takes a date column whose header names no particular activity ("Date", "Start Time",
    # "Timestamp"; see heuristics.auto_map step 7). "call_log" = first choice in files that look like call logs,
    # "default" = first choice in other files; each is the other's second choice. None = only by its own name.
    generic_date: Literal["call_log", "default"] | None = Field(
        None,
        description="date/datetime only: receives a generically named date column (Date, Start Time, Timestamp) - "
        "'call_log' in files that look like call logs, 'default' in other files.",
    )
    # date fields: year written for a month + day without a year (e.g. birthday "9/12" -> 09/12/1600).
    no_year_value: int | None = Field(
        None,
        description="date only: year to output when the source has a month and day but no year (e.g. 1600).",
    )

    @model_validator(mode="after")
    def _defaults(self) -> "FieldSpec":
        """Fill derived defaults and reject contradictory settings (runs after the fields are parsed).

        * ``header`` defaults to ``name``.
        * A ``fixed_blank`` field can't be required or have a default - both would put a value in it.
        * A ``manual_only`` field can't be required, have a default or be fixed_blank: a default would fill it
          without a person choosing a column, and a required one would block every file nobody mapped by hand.
        * ``no_year_value`` only makes sense for ``date`` fields, ``generic_date`` for date/datetime fields,
          ``opt_out_choice`` for boolean fields.
        * ``on_invalid`` defaults to "reject" for required fields and "blank" for optional ones: a bad value in
          a required field makes the row unusable, a bad optional value is just dropped.
        * An ``enum`` field needs ``allowed_values``.

        Raises ``ValueError`` (reported by pydantic as a validation error naming the field) on a bad combination.
        """
        if not self.header:
            self.header = self.name
        if self.fixed_blank and (self.required or self.default not in (None, "")):
            raise ValueError(f"fixed_blank field '{self.name}' can't be required or have a default")
        if self.manual_only and (self.required or self.default not in (None, "") or self.fixed_blank):
            raise ValueError(f"manual_only field '{self.name}' can't be required, have a default or be fixed_blank")
        if self.no_year_value is not None and self.type != "date":
            raise ValueError(f"no_year_value only applies to date fields ('{self.name}' is {self.type})")
        if self.generic_date and self.type not in ("date", "datetime"):
            raise ValueError(f"generic_date only applies to date/datetime fields ('{self.name}' is {self.type})")
        if self.opt_out_choice and self.type != "boolean":
            raise ValueError(f"opt_out_choice only applies to boolean fields ('{self.name}' is {self.type})")
        if self.on_invalid is None:
            self.on_invalid = "reject" if self.required else "blank"
        if self.type == "enum" and not self.allowed_values:
            raise ValueError(f"enum field '{self.name}' needs allowed_values")
        return self

    def describe(self) -> str:
        """One-line human description of the field, for prompts and help text.

        Examples::

            "phone_number (phone, required) — format digits10 — Consumer's mobile number"
            "consent — always blank (fixed)"
        """
        if self.fixed_blank:
            return f"{self.name} — always blank (fixed)"
        bits = [f"{self.name} ({self.type}{', required' if self.required else ''}"
                f"{', mapped by hand only' if self.manual_only else ''})"]
        if self.format:
            bits.append(f"format {self.format}")
        if self.allowed_values:
            bits.append("allowed: " + ", ".join(self.allowed_values))
        if self.description:
            bits.append(self.description)
        return " — ".join(bits)


class OutputSpec(BaseModel):
    """How the converted file is written (``output:`` in the schema YAML); consumed by ``output.write_output``.

    Example: ``format: csv, delimiter: ",", encoding: utf-8`` -> a UTF-8 comma-separated file with a header row.
    """

    # "csv" (text) or "xlsx" (Excel; every column written as text so leading zeros and '+' are kept).
    format: Literal["csv", "xlsx"] = "csv"
    # csv only: field separator.
    delimiter: str = ","
    # csv only: text encoding of the file (characters the encoding can't represent are replaced).
    encoding: str = "utf-8"
    # Write the field headers as the first row.
    include_header: bool = True
    # csv only: quote every value instead of only values that need it.
    quote_all: bool = False
    # csv only: line ending ("\n", or "\r\n" for systems that expect Windows line endings).
    line_terminator: str = "\n"
    # xlsx only: worksheet name (Excel allows at most 31 characters; longer names are cut).
    sheet_name: str = "Import"
    # Part of the output file name: "<source name>_<suffix>_<timestamp>.csv".
    filename_suffix: str = "converted"


class DedupeSpec(BaseModel):
    """Which output rows count as duplicates (``dedupe:`` in the schema YAML).

    Two accepted rows are duplicates when they have the same output value (ignoring case) in every field listed
    in ``keys``.
    ``keys: ["*"]`` means every mappable field (expanded by :meth:`TargetSchema._check`), so a row is dropped
    only when it is identical to an earlier one. An empty list switches deduplication off.
    """

    # Field names compared to detect a duplicate; ["*"] = all fields except fixed-blank ones.
    keys: list[str] = Field(default_factory=list, description="Field names that together identify a duplicate row")
    # Which of the duplicate rows is kept: the first or the last one in the file.
    keep: Literal["first", "last"] = "first"


class TargetSchema(BaseModel):
    """The whole import format: name/version, output file settings, dedupe rule and the ordered field list.

    ``fields`` is in output-column order. Build it with :func:`load_schema`; validation (:meth:`_check`) ensures
    field names are unique and dedupe keys exist.

    Example::

        schema = load_schema("schemas/target_schema.yaml")
        schema.label                      # "Generic import format v1.0"
        schema.field("phone_number").type  # "phone"
    """

    # Display name of the format (shown in the UI, summaries and Claude's prompt).
    name: str
    # Schema version, always kept as text (YAML `1.0` would otherwise be a float).
    version: str = "1"
    # Free-text description of the format.
    description: str = ""
    # How the output file is written.
    output: OutputSpec = Field(default_factory=OutputSpec)
    # How duplicate rows are detected.
    dedupe: DedupeSpec = Field(default_factory=DedupeSpec)
    # Output columns, in order.
    fields: list[FieldSpec]

    @field_validator("version", mode="before")
    @classmethod
    def _version_str(cls, v):
        """Accept a number in the YAML (``version: 1.0``) and store it as text ("1.0")."""
        return str(v)

    @model_validator(mode="after")
    def _check(self) -> "TargetSchema":
        """Validate the schema as a whole once all fields are parsed.

        * Field names must be unique (they key mappings and templates).
        * ``dedupe.keys == ["*"]`` is expanded to every field that isn't fixed-blank.
        * Every dedupe key must be a field name.

        Raises ``ValueError`` listing the offending names.
        """
        names = [f.name for f in self.fields]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate field names: {sorted(dupes)}")
        # "*" = compare all fields; fixed-blank ones are always empty, so they'd add nothing to the comparison.
        if self.dedupe.keys == ["*"]:
            self.dedupe.keys = [f.name for f in self.fields if not f.fixed_blank]
        unknown = [k for k in self.dedupe.keys if k not in names]
        if unknown:
            raise ValueError(f"dedupe keys not in fields: {unknown}")
        return self

    def field(self, name: str) -> FieldSpec:
        """The field with internal name ``name``; raises ``KeyError`` when the schema has no such field."""
        for f in self.fields:
            if f.name == name:
                return f
        raise KeyError(name)

    @property
    def field_names(self) -> list[str]:
        """Internal names of all fields, in output-column order (fixed-blank fields included)."""
        return [f.name for f in self.fields]

    @property
    def mappable_fields(self) -> list[FieldSpec]:
        """Fields that take a value from the source file (excludes fixed-blank fields).

        Used wherever a mapping is built or checked, so a fixed-blank field (e.g. consent) can never get a source.
        """
        return [f for f in self.fields if not f.fixed_blank]

    @property
    def auto_fields(self) -> list[FieldSpec]:
        """Mappable fields the rules, templates and Claude may fill (excludes ``manual_only`` ones, e.g. consent)."""
        return [f for f in self.mappable_fields if not f.manual_only]

    @property
    def label(self) -> str:
        """Name plus version for display and job records, e.g. "Generic import format v1.0"."""
        return f"{self.name} v{self.version}"


def load_schema(path: str | Path) -> TargetSchema:
    """Read the schema YAML at ``path`` and return the validated :class:`TargetSchema`.

    ``yaml.safe_load`` is used so the file can't create arbitrary Python objects. Raises pydantic's
    ``ValidationError`` when the file doesn't describe a valid schema, and ``OSError`` when it can't be read.
    """
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return TargetSchema.model_validate(raw)
