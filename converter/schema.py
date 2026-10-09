"""Target schema: the fields the destination system accepts, and how output files must look.

The schema lives in a YAML file (schemas/target_schema.yaml). Everything else in the tool is
driven by it, so supporting the real import format only requires editing that file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

FieldType = Literal[
    "string", "phone", "date", "datetime", "email", "zip", "state", "integer", "decimal", "boolean", "enum"
]

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
    name: str = Field(description="Internal field id (snake_case). Used in templates and dedupe keys.")
    header: str = Field("", description="Exact column header written to the output file.")
    type: FieldType = "string"
    required: bool = False
    format: str | None = Field(
        None,
        description=(
            "Output format. date/datetime: strftime pattern (e.g. %m/%d/%Y). "
            "phone: E164 | digits10 | digits11 | national | dashed. zip: zip5 | zip5_4."
        ),
    )
    description: str = ""
    synonyms: list[str] = Field(default_factory=list, description="Other header names that mean this field.")
    max_length: int | None = None
    truncate: bool = Field(False, description="string only: truncate to max_length instead of treating as invalid")
    case: Literal["none", "upper", "lower", "title", "name"] = "none"
    allowed_values: list[str] = Field(default_factory=list, description="enum only")
    default: str | None = Field(None, description="Value used when the source is empty")
    on_invalid: Literal["reject", "blank"] | None = Field(
        None,
        description="What to do when a value can't be converted: reject the row or blank the value. "
        "Defaults to reject for required fields and blank for optional fields.",
    )
    countries: list[str] = Field(default_factory=lambda: ["US"], description="phone only: allowed ISO country codes")
    true_value: str = "true"
    false_value: str = "false"
    fixed_blank: bool = Field(
        False,
        description="Always output blank on every row. The field can't be mapped, defaulted, set by Claude or "
        "edited by users (e.g. a consent column where any value would change subscription status).",
    )
    no_year_value: int | None = Field(
        None,
        description="date only: year to output when the source has a month and day but no year (e.g. 1600).",
    )

    @model_validator(mode="after")
    def _defaults(self) -> "FieldSpec":
        if not self.header:
            self.header = self.name
        if self.fixed_blank and (self.required or self.default not in (None, "")):
            raise ValueError(f"fixed_blank field '{self.name}' can't be required or have a default")
        if self.no_year_value is not None and self.type != "date":
            raise ValueError(f"no_year_value only applies to date fields ('{self.name}' is {self.type})")
        if self.on_invalid is None:
            self.on_invalid = "reject" if self.required else "blank"
        if self.type == "enum" and not self.allowed_values:
            raise ValueError(f"enum field '{self.name}' needs allowed_values")
        return self

    def describe(self) -> str:
        if self.fixed_blank:
            return f"{self.name} — always blank (fixed)"
        bits = [f"{self.name} ({self.type}{', required' if self.required else ''})"]
        if self.format:
            bits.append(f"format {self.format}")
        if self.allowed_values:
            bits.append("allowed: " + ", ".join(self.allowed_values))
        if self.description:
            bits.append(self.description)
        return " — ".join(bits)


class OutputSpec(BaseModel):
    format: Literal["csv", "xlsx"] = "csv"
    delimiter: str = ","
    encoding: str = "utf-8"
    include_header: bool = True
    quote_all: bool = False
    line_terminator: str = "\n"
    sheet_name: str = "Import"
    filename_suffix: str = "converted"


class DedupeSpec(BaseModel):
    keys: list[str] = Field(default_factory=list, description="Field names that together identify a duplicate row")
    keep: Literal["first", "last"] = "first"


class TargetSchema(BaseModel):
    name: str
    version: str = "1"
    description: str = ""
    output: OutputSpec = Field(default_factory=OutputSpec)
    dedupe: DedupeSpec = Field(default_factory=DedupeSpec)
    fields: list[FieldSpec]

    @field_validator("version", mode="before")
    @classmethod
    def _version_str(cls, v):
        return str(v)

    @model_validator(mode="after")
    def _check(self) -> "TargetSchema":
        names = [f.name for f in self.fields]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate field names: {sorted(dupes)}")
        if self.dedupe.keys == ["*"]:
            self.dedupe.keys = [f.name for f in self.fields if not f.fixed_blank]
        unknown = [k for k in self.dedupe.keys if k not in names]
        if unknown:
            raise ValueError(f"dedupe keys not in fields: {unknown}")
        return self

    def field(self, name: str) -> FieldSpec:
        for f in self.fields:
            if f.name == name:
                return f
        raise KeyError(name)

    @property
    def field_names(self) -> list[str]:
        return [f.name for f in self.fields]

    @property
    def mappable_fields(self) -> list[FieldSpec]:
        """Fields that take a value from the source file (excludes fixed-blank fields)."""
        return [f for f in self.fields if not f.fixed_blank]

    @property
    def label(self) -> str:
        return f"{self.name} v{self.version}"


def load_schema(path: str | Path) -> TargetSchema:
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return TargetSchema.model_validate(raw)
