"""Mapping spec (target field → source columns + transform + params), column profiling, and fingerprints."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, Field

from . import normalizers as N
from .schema import TargetSchema
from .transforms import TRANSFORMS, check_transform

Origin = Literal["template", "rule", "ai", "user", ""]

ALLOWED_PARAMS = {"date_order", "input_format", "region", "part", "separator", "index", "value_map", "value"}


class FieldMapping(BaseModel):
    sources: list[str] = Field(default_factory=list)
    transform: str = "none"
    params: dict[str, Any] = Field(default_factory=dict)
    origin: Origin = ""
    confidence: Literal["high", "medium", "low", ""] = ""
    reason: str = ""

    def clean_params(self) -> dict[str, Any]:
        allowed = set(TRANSFORMS[self.transform].params) if self.transform in TRANSFORMS else set()
        return {k: v for k, v in self.params.items() if k in allowed and v not in (None, "", [])}

    def summary(self) -> str:
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
            extra = f" (split on '{self.params.get('separator', ',')}', piece {int(self.params.get('index') or 0) + 1})"
        elif self.transform == "coalesce":
            return "first non-empty of " + ", ".join(self.sources)
        label = "" if self.transform == "copy" else f"{info.label if info else self.transform}: "
        return f"{label}{src}{extra}"


class Mapping(BaseModel):
    fields: dict[str, FieldMapping] = Field(default_factory=dict)

    def get(self, name: str) -> FieldMapping:
        return self.fields.get(name) or FieldMapping()

    def used_sources(self) -> set[str]:
        return {s for fm in self.fields.values() if fm.transform != "none" for s in fm.sources}

    def normalized(self, schema: TargetSchema, columns: list[str]) -> "Mapping":
        """Ensure every schema field is present, drop unknown fields and sources missing from the file."""
        lookup = {c.lower(): c for c in columns}
        out: dict[str, FieldMapping] = {}
        for f in schema.fields:
            if f.fixed_blank:
                out[f.name] = FieldMapping(reason="always blank — fixed by the import format")
                continue
            fm = self.fields.get(f.name, FieldMapping()).model_copy(deep=True)
            fm.sources = [lookup[s.lower()] for s in fm.sources if s.lower() in lookup]
            if fm.transform not in TRANSFORMS:
                fm.transform = "none"
            if fm.transform in ("none", "constant"):
                fm.sources = []
            if fm.transform in ("coalesce", "concat") and len(fm.sources) == 1:
                fm.transform = "copy"
            if fm.transform not in ("none", "constant") and not fm.sources:
                fm = FieldMapping()  # its source column isn't in this file
            elif check_transform(fm.transform, fm.sources, fm.params):
                fm = FieldMapping()
            fm.params = fm.clean_params()
            out[f.name] = fm
        return Mapping(fields=out)

    def problems(self, schema: TargetSchema) -> dict[str, str]:
        issues = {}
        for f in schema.mappable_fields:
            fm = self.get(f.name)
            p = check_transform(fm.transform, fm.sources, fm.params)
            if p:
                issues[f.name] = p
            elif f.required and fm.transform == "none" and not f.default:
                issues[f.name] = "required field has no source column"
        return issues


# ---------------------------------------------------------------------------
# Header fingerprints (used to recognise a file layout seen before)
# ---------------------------------------------------------------------------

def norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", h.lower())


def header_fingerprint(columns: list[str]) -> str:
    key = "|".join(sorted({norm_header(c) for c in columns if norm_header(c)}))
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def header_similarity(a: list[str], b: list[str]) -> float:
    sa = {norm_header(x) for x in a if norm_header(x)}
    sb = {norm_header(x) for x in b if norm_header(x)}
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# ---------------------------------------------------------------------------
# Column profiling
# ---------------------------------------------------------------------------

class ColumnProfile(BaseModel):
    name: str
    non_empty: int
    total: int
    unique: int
    samples: list[str]
    shares: dict[str, float]  # share of sampled values that look like each type
    max_length: int

    @property
    def fill_rate(self) -> float:
        return self.non_empty / self.total if self.total else 0.0

    def best_type(self) -> tuple[str, float]:
        if not self.shares:
            return "string", 0.0
        t = max(self.shares, key=self.shares.get)
        return t, self.shares[t]


def profile_columns(df: pd.DataFrame, sample_size: int = 300, n_samples: int = 15) -> list[ColumnProfile]:
    profiles = []
    total = len(df)
    for col in df.columns:
        series = [N.clean_text(v) for v in df[col].tolist()]
        non_empty = [v for v in series if v]
        uniq = list(dict.fromkeys(non_empty))
        sample = uniq[:sample_size]
        shares: dict[str, float] = {}
        if sample:
            n = len(sample)
            shares = {
                "phone": sum(N.looks_like_phone(v) for v in sample) / n,
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
