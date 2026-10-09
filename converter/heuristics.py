"""Rule-based auto-mapping: header names + synonyms + value-type evidence. Runs before (or without) Claude."""

from __future__ import annotations

import re

from rapidfuzz import fuzz

from .mapping import ColumnProfile, FieldMapping, Mapping, norm_header
from .schema import FieldSpec, TargetSchema

TYPED = {"phone": "phone", "email": "email", "date": "date", "datetime": "date", "zip": "zip", "state": "state"}
FULL_NAME_HEADERS = {"name", "fullname", "customername", "customer", "contactname", "contact", "guestname", "guest",
                     "clientname", "client", "membername", "member", "patronname", "subscribername", "subscriber",
                     "recipientname", "recipient", "displayname", "nombre"}
FULL_ADDRESS_HEADERS = {"address", "fulladdress", "mailingaddress", "streetaddress", "homeaddress", "deliveryaddress",
                        "billingaddress", "shippingaddress", "customeraddress", "addr"}
MOBILE_HINTS = ("mobile", "cell", "sms", "text", "wireless")
FILLER_WORDS = {"date", "time", "datetime", "timestamp", "dt", "last", "latest", "recent", "most", "at", "on", "of",
                "the", "a", "an", "and", "when", "day"}
# Consent / subscription columns are never auto-mapped: consent must not be derived from source files.
CONSENT_WORDS = {"opt", "optin", "optout", "opted", "consent", "consented", "subscribe", "subscribed", "subscriber",
                 "subscription", "unsubscribe", "unsubscribed", "stop", "stopped", "dnd", "tcpa", "permission"}
# Headers too vague to match a field on their own (a column called "Date" could be any date field).
GENERIC_HEADERS = {"date", "time", "datetime", "timestamp", "created", "createdat", "updated", "updatedat", "name",
                   "number", "value", "status", "type", "notes", "last", "other", "info", "data"}

_FULL_ADDR_RE = re.compile(r",.*\b\d{5}(?:-\d{4})?\s*$|,\s*[A-Za-z .]+,?\s+[A-Z]{2}\b")


def _words(text: str) -> list[str]:
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)  # camelCase → camel Case
    return [w for w in re.split(r"[^a-z0-9]+", text.lower()) if w]


def _core(text: str) -> str:
    """Header without words that carry no meaning on their own ("date", "last", "time"…)."""
    return "".join(w for w in _words(text) if w not in FILLER_WORDS)


def _candidates(f: FieldSpec) -> list[str]:
    names = [f.name, f.header, f.name.replace("_", " "), *f.synonyms]
    return list(dict.fromkeys(names))


def is_consent_column(column: str) -> bool:
    words = set(_words(column))
    return bool(words & CONSENT_WORDS) or any(k in norm_header(column) for k in ("optin", "optout", "donot"))


def header_score(column: str, f: FieldSpec) -> float:
    h = norm_header(column)
    if not h:
        return 0.0
    hc = _core(column)
    if h in GENERIC_HEADERS:
        return 100.0 if any(h == norm_header(n) for n in _candidates(f)) else 0.0
    best = 0.0
    for name in _candidates(f):
        c = norm_header(name)
        if not c:
            continue
        if h == c:
            return 100.0
        cc = _core(name)
        if hc and cc and hc == cc:
            best = max(best, 92.0)  # same meaningful words, e.g. "Spin Date" vs "last spin"
        if len(c) >= 5 and c in h:
            best = max(best, 82.0)
        elif len(h) >= 6 and h not in GENERIC_HEADERS and h in c:
            best = max(best, 65.0)  # header is only part of a synonym (e.g. "Customer" in "customer last name")
        # Fuzzy similarity on the meaningful part only, so "Join Date" doesn't look like "Spin Date".
        if hc and cc:
            best = max(best, fuzz.ratio(hc, cc))
        elif not hc and not cc:
            best = max(best, fuzz.ratio(h, c))
    return best


def score(column: ColumnProfile, f: FieldSpec) -> float:
    if is_consent_column(column.name):
        return 0.0
    hs = header_score(column.name, f)
    kind = TYPED.get(f.type)
    if column.non_empty == 0:
        return hs * 0.5
    if kind:
        share = column.shares.get(kind, 0.0)
        s = 0.55 * hs + 45 * share
        if share < 0.2 and hs < 100:
            s -= 25
        if kind == "date" and hs < 80:
            s = min(s, 50)  # files often have several date columns: the header must clearly name the field
        return s
    # string / numeric / enum targets: header evidence only, penalise strongly-typed columns
    best_type, best_share = column.best_type()
    if best_share >= 0.8 and best_type in ("phone", "email", "date") and hs < 100:
        hs -= 40
    return hs


def _confidence(s: float, hs: float = 0.0, share: float | None = None) -> str:
    if hs >= 100:
        return "high" if share is None or share >= 0.3 else "medium"
    return "high" if s >= 85 else "medium" if s >= 70 else "low"


def auto_map(schema: TargetSchema, profiles: list[ColumnProfile]) -> Mapping:
    fields: dict[str, FieldMapping] = {f.name: FieldMapping() for f in schema.fields}
    used: set[str] = set()
    by_name = {p.name: p for p in profiles}

    # 1. Direct matches, best scores first.
    scored = []
    for f in schema.mappable_fields:
        threshold = 60 if f.type in TYPED else 72
        for p in profiles:
            s = score(p, f)
            if s >= threshold:
                scored.append((s, f.name, p.name))
    scored.sort(reverse=True)
    for s, fname, col in scored:
        if fields[fname].transform != "none" or col in used:
            continue
        f = schema.field(fname)
        hs = header_score(col, f)
        kind = TYPED.get(f.type)
        share = by_name[col].shares.get(kind, 0.0) if kind else 0.0
        if hs >= 100:
            reason = f"header '{col}' matches"
        elif kind and share >= 0.8 and hs < 70:
            reason = f"{share:.0%} of values in '{col}' look like {kind} values"
        else:
            reason = f"header '{col}' is similar ({hs:.0f}/100)" + (f"; {share:.0%} look like {kind}" if kind else "")
        fields[fname] = FieldMapping(sources=[col], transform="copy", origin="rule",
                                     confidence=_confidence(s, hs, share if kind else None), reason=reason)
        used.add(col)

    # 2. Phone: prefer mobile columns, fall back to other phone columns.
    for f in schema.fields:
        if f.type != "phone":
            continue
        fm = fields[f.name]
        phone_cols = [p.name for p in profiles if (p.shares.get("phone", 0) >= 0.5 or header_score(p.name, f) >= 82)
                      and p.name not in used - set(fm.sources) and not is_consent_column(p.name)]
        if fm.transform == "none":
            strong = [p for p in profiles if p.name not in used and p.shares.get("phone", 0) >= 0.9
                      and not is_consent_column(p.name)]
            if strong:
                phone_cols = [p.name for p in strong]
        if len(phone_cols) >= 2:
            ordered = sorted(phone_cols, key=lambda c: (not any(h in c.lower() for h in MOBILE_HINTS), phone_cols.index(c)))
            fields[f.name] = FieldMapping(sources=ordered, transform="coalesce", origin="rule", confidence="medium",
                                          reason="several phone columns; using mobile first, then the others")
            used.update(ordered)
        elif len(phone_cols) == 1 and fm.transform == "none":
            fields[f.name] = FieldMapping(sources=phone_cols, transform="copy", origin="rule", confidence="low",
                                          reason=f"values in '{phone_cols[0]}' look like phone numbers")
            used.update(phone_cols)

    # 3. Email by value type alone (unambiguous).
    for f in schema.fields:
        if f.type == "email" and fields[f.name].transform == "none":
            cands = [p for p in profiles if p.name not in used and p.shares.get("email", 0) >= 0.8
                     and not is_consent_column(p.name)]
            if len(cands) == 1:
                fields[f.name] = FieldMapping(sources=[cands[0].name], transform="copy", origin="rule",
                                              confidence="low", reason="values look like email addresses")
                used.add(cands[0].name)

    # 4. Full name → first / last.
    name_targets = {f.name: f for f in schema.fields if f.type == "string" and f.name in ("first_name", "last_name")}

    def _from_full_name_column(n: str) -> bool:
        fm = fields[n]
        return fm.transform == "copy" and norm_header(fm.sources[0]) in FULL_NAME_HEADERS

    if name_targets and all(fields[n].transform == "none" or _from_full_name_column(n) for n in name_targets):
        for n in name_targets:
            if _from_full_name_column(n):
                used.discard(fields[n].sources[0])
                fields[n] = FieldMapping()
        full = next((p for p in profiles if norm_header(p.name) in FULL_NAME_HEADERS and p.name not in used), None)
        if full is None:
            full = next((p for p in profiles if "name" in norm_header(p.name) and p.name not in used
                         and not any(k in norm_header(p.name) for k in ("first", "last", "user", "file", "store",
                                                                         "company", "business", "location"))), None)
        if full is not None:
            for n in name_targets:
                part = "first" if n == "first_name" else "last"
                fields[n] = FieldMapping(sources=[full.name], transform="name_part", params={"part": part},
                                         origin="rule", confidence="medium",
                                         reason=f"split full name in '{full.name}'")
            used.add(full.name)

    # 5. Full single-line address → street / unit / city / state / zip.
    addr_targets = {"address1": "line1", "address2": "line2", "city": "city", "state": "state", "zip": "zip"}
    present = {k: v for k, v in addr_targets.items() if k in fields}
    if present:
        current = fields.get("address1")
        addr_col = None
        if current and current.transform == "copy" and current.sources:
            p = by_name.get(current.sources[0])
            if p and _mostly_full_addresses(p):
                addr_col = p.name
        if addr_col is None and current is not None and current.transform == "none":
            for p in profiles:
                if p.name not in used and (norm_header(p.name) in FULL_ADDRESS_HEADERS) and _mostly_full_addresses(p):
                    addr_col = p.name
                    break
        if addr_col:
            for target, part in present.items():
                if target == "address1" or fields[target].transform == "none":
                    fields[target] = FieldMapping(sources=[addr_col], transform="address_part", params={"part": part},
                                                  origin="rule", confidence="medium",
                                                  reason=f"parsed from full address in '{addr_col}'")
            used.add(addr_col)

    return Mapping(fields=fields)


def _mostly_full_addresses(p: ColumnProfile) -> bool:
    if not p.samples:
        return False
    hits = sum(bool(_FULL_ADDR_RE.search(s)) for s in p.samples)
    return hits >= max(1, 0.5 * len(p.samples))
