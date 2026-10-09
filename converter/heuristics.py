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

# Header words that say "this is a phone column". "number"/"no"/"#" count only without a non-phone word next to it.
PHONE_WORDS = {"phone", "phones", "phonenumber", "mobile", "cell", "cellphone", "cellular", "tel", "telephone", "ph",
               "sms", "text", "texting", "whatsapp", "wireless", "landline", "contact", "msisdn"}
# Words that turn a phone header into a header ABOUT the phone (a setting or label), not the number itself:
# "phone4MarketingPreference", "Phone Type", "SMS Opt In", "Phone Verified", "Phone Ext".
PHONE_ATTRIBUTE_WORDS = {"preference", "preferences", "pref", "prefs", "marketing", "type", "kind", "label", "status",
                         "verified", "verification", "valid", "validated", "confirmed", "consent", "opt", "optin",
                         "optout", "opted", "ext", "extension", "country", "countrycode", "code", "carrier", "flag",
                         "enabled", "disabled", "allowed", "allow", "permission", "subscribed", "unsubscribed",
                         "source", "date", "time", "updated", "created", "count", "is", "has", "can", "ok", "fax",
                         "notes", "note", "comment", "comments"}
NUMBER_WORDS = {"number", "num", "no", "nbr", "numbers"}
NOT_PHONE_WORDS = {"order", "account", "acct", "id", "member", "card", "invoice", "ticket", "zip", "postal", "store",
                   "location", "receipt", "check", "transaction", "customer", "loyalty", "reference", "ref", "employee",
                   "table", "party", "guest", "visit", "visits", "points", "count", "fax"}

_FULL_ADDR_RE = re.compile(r",.*\b\d{5}(?:-\d{4})?\s*$|,\s*[A-Za-z .]+,?\s+[A-Z]{2}\b")


def _words(text: str) -> list[str]:
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)  # camelCase → camel Case
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)  # SMSOptIn → SMS OptIn
    text = re.sub(r"([A-Za-z])(\d)", r"\1 \2", text)  # phone4 → phone 4
    text = re.sub(r"(\d)([A-Za-z])", r"\1 \2", text)  # 4Marketing → 4 Marketing
    return [w for w in re.split(r"[^a-z0-9]+", text.lower()) if w]


def _core(text: str) -> str:
    """Header without words that carry no meaning on their own ("date", "last", "time"…)."""
    return "".join(w for w in _words(text) if w not in FILLER_WORDS)


def _candidates(f: FieldSpec) -> list[str]:
    names = [f.name, f.header, f.name.replace("_", " "), *f.synonyms]
    return list(dict.fromkeys(names))


def is_consent_column(column: str) -> bool:
    words = set(_words(column))
    if words & CONSENT_WORDS or any(k in norm_header(column) for k in ("optin", "optout", "donot")):
        return True
    # "phone4MarketingPreference", "SMS Marketing Allowed", "Marketing OK": a yes/no marketing permission.
    return "marketing" in words and bool(words & {"preference", "preferences", "pref", "prefs", "allowed", "allow",
                                                  "ok", "permission", "enabled", "accepted", "agreed", "status"})


def is_phone_header(column: str) -> bool:
    """True when the column name itself says it holds phone numbers ("Mobile", "Cell #", "Contact Number")."""
    words = set(_words(column))
    if words & PHONE_ATTRIBUTE_WORDS or is_consent_column(column):
        return False
    if words & PHONE_WORDS:
        return True
    return bool(words & NUMBER_WORDS) and not (words & NOT_PHONE_WORDS) and len(words) <= 2


def enough_evidence(p: ColumnProfile, share: float, minimum: float = 0.9) -> bool:
    """Values alone say "phone" only when nearly all of them are phones and there are enough of them to tell.

    A column like "Occupation" with two values, one of which someone typed a phone number into, is not a phone
    column: 50% phone-like over 2 values is noise, not evidence.
    """
    return share >= minimum and p.unique >= min(3, p.non_empty)


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
        def _phone_column(p: ColumnProfile) -> bool:
            share = p.shares.get("phone", 0)
            if is_phone_header(p.name):
                # Named like a phone column: needs some phone numbers in it (or be empty in this file, e.g. Phone 4).
                return p.non_empty == 0 or share >= 0.3
            if any(w in PHONE_WORDS for w in _words(p.name)):
                return False  # a column ABOUT the phone (preference, type, opt-in…), never the number
            return enough_evidence(p, share)  # column name says nothing about phones: values must be clear

        phone_cols = [p.name for p in profiles if _phone_column(p)
                      and p.name not in used - set(fm.sources) and not is_consent_column(p.name)]
        if fm.transform == "none":
            strong = [p for p in profiles if p.name not in used and enough_evidence(p, p.shares.get("phone", 0))
                      and not is_consent_column(p.name)]
            if strong:
                phone_cols = [p.name for p in strong]
        if len(phone_cols) >= 2:
            ordered = sorted(phone_cols, key=lambda c: (not any(h in c.lower() for h in MOBILE_HINTS), phone_cols.index(c)))
            fields[f.name] = FieldMapping(sources=ordered, transform="each_row", origin="rule", confidence="medium",
                                          reason=f"{len(ordered)} phone columns: one row per different number")
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

    # 6. Birthday / anniversary split over a month column and a day column (and maybe a year column).
    for target, keys in (("birthday", {"birth", "birthday", "bday", "dob", "born"}),
                         ("anniversary", {"anniversary", "anniv", "wedding"})):
        if target not in fields:
            continue
        parts = _date_part_columns(profiles, keys)
        if parts:
            fields[target] = FieldMapping(sources=parts, transform="concat", params={"separator": " "}, origin="rule",
                                          confidence="medium",
                                          reason=f"{target} is split over {', '.join(repr(c) for c in parts)}")
            for name, fm in fields.items():
                if name != target and set(fm.sources) & set(parts):
                    fields[name] = FieldMapping()
            used.update(parts)

    return Mapping(fields=fields)


def _date_part_columns(profiles: list[ColumnProfile], keys: set[str]) -> list[str]:
    """['Birth Month', 'Birth Day'(, 'Birth Year')] when a date is split over separate columns, else []."""
    def find(part_words: set[str], numbers_up_to: int | None) -> str | None:
        for p in profiles:
            words = set(_words(p.name))
            if not (words & keys) or not (words & part_words):
                continue
            if numbers_up_to is not None and p.samples and not all(
                    s.strip().isdigit() and 1 <= int(s) <= numbers_up_to for s in p.samples):
                continue
            return p.name
        return None

    month = find({"month", "mon", "mo", "mm"}, None)
    day = find({"day", "dd"} , 31)
    if not (month and day) or month == day:
        return []
    year = find({"year", "yr", "yyyy"}, None)
    return [month, day] + ([year] if year and year not in (month, day) else [])


def _mostly_full_addresses(p: ColumnProfile) -> bool:
    if not p.samples:
        return False
    hits = sum(bool(_FULL_ADDR_RE.search(s)) for s in p.samples)
    return hits >= max(1, 0.5 * len(p.samples))
