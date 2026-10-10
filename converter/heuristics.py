"""Rule-based auto-mapping: header names + synonyms + value-type evidence. Runs before (or without) Claude.

Overview
--------
Given the target schema and a :class:`~converter.mapping.ColumnProfile` per source column (header name, fill
counts, sample values and the share of values that look like a phone / email / date / ZIP / state), this
module proposes a first :class:`~converter.mapping.Mapping` without any AI call. The result is a starting
point: ``services.initial_mapping`` normalises it, may overlay a similar saved template, may let Claude refine
it, and the reviewer can then change anything in the web UI.

Responsibilities
    * Score how well a source column fits a target field (:func:`header_score` for the header text,
      :func:`score` for header + value-type evidence).
    * Recognise columns that must be treated specially: consent / opt-in columns (never mapped,
      :func:`is_consent_column`) and phone columns vs. columns that only describe a phone
      (:func:`is_phone_header`, :func:`enough_evidence`).
    * Build the mapping in :func:`auto_map` in six ordered steps (direct matches, phone columns, email by
      values, full-name split, full-address split, birthday/anniversary split over month/day columns).

Main entry points
    * :func:`auto_map` - used by ``services.initial_mapping``.
    * :data:`TYPED`, :func:`header_score`, :func:`is_consent_column`, :func:`is_phone_header` - also used by
      ``checks.py`` to warn about values that were typed into the wrong column.

Data flow
    ``reader.read_file`` -> DataFrame -> ``mapping.profile_columns`` -> ``auto_map(schema, profiles)``
    -> ``Mapping`` -> ``Mapping.normalized(...)`` -> (template / Claude / reviewer edits) -> ``engine.convert``.

Every rule-made :class:`~converter.mapping.FieldMapping` gets ``origin="rule"``, a confidence
(high / medium / low) and a human-readable ``reason`` that the review grid shows next to the field.
"""

from __future__ import annotations

import re

from rapidfuzz import fuzz

from .mapping import ColumnProfile, FieldMapping, Mapping, norm_header
from .schema import FieldSpec, TargetSchema

# Schema field type -> key in ColumnProfile.shares that gives value-type evidence for it. A "datetime" field is
# supported by date-looking values. Field types missing here (string, integer, decimal, boolean, enum) are
# matched on the header alone, because their values don't have a recognisable shape.
TYPED = {"phone": "phone", "email": "email", "date": "date", "datetime": "date", "zip": "zip", "state": "state"}
# Normalised headers (see norm_header: lower-case, letters/digits only) of a column that holds a person's WHOLE
# name, e.g. "Customer Name" -> "customername". Step 4 of auto_map splits such a column into first/last name.
FULL_NAME_HEADERS = {"name", "fullname", "customername", "customer", "contactname", "contact", "guestname", "guest",
                     "clientname", "client", "membername", "member", "patronname", "subscribername", "subscriber",
                     "recipientname", "recipient", "displayname", "nombre"}
# Normalised headers of a single-line address column ("Mailing Address" -> "mailingaddress"). Step 5 of auto_map
# only looks at these when the street field is still unmapped, and the values must also look like full addresses.
FULL_ADDRESS_HEADERS = {"address", "fulladdress", "mailingaddress", "streetaddress", "homeaddress", "deliveryaddress",
                        "billingaddress", "shippingaddress", "customeraddress", "addr"}
# Substrings of a (lower-cased) column name that mark a mobile number. When several phone columns feed one field,
# these columns are listed first, so the mobile number is the contact's first output row (the target is SMS).
MOBILE_HINTS = ("mobile", "cell", "sms", "text", "wireless")
# Words that carry no meaning on their own in a header. _core() drops them so that "Last Visit Date" and
# "visit" are compared on "visit" only, and "Join Date" doesn't look like "Spin Date" just because of "Date".
FILLER_WORDS = {"date", "time", "datetime", "timestamp", "dt", "last", "latest", "recent", "most", "at", "on", "of",
                "the", "a", "an", "and", "when", "day"}
# Consent / subscription columns are never auto-mapped: consent must not be derived from source files.
# (The import format keeps its consent column blank on purpose - a value there would subscribe or opt out people.)
# A column such as "SMS Opt In" or "Subscribed" is also never taken as a phone, date or other field: score()
# returns 0 for it and the phone / email steps skip it. Matched as whole header words (see _words).
CONSENT_WORDS = {"opt", "optin", "optout", "opted", "consent", "consented", "subscribe", "subscribed", "subscriber",
                 "subscription", "unsubscribe", "unsubscribed", "stop", "stopped", "dnd", "tcpa", "permission"}
# Headers too vague to match a field on their own (a column called "Date" could be any date field).
# header_score() gives such a header 100 only when it is literally a name/synonym of the field, otherwise 0 -
# no fuzzy or partial matching.
GENERIC_HEADERS = {"date", "time", "datetime", "timestamp", "created", "createdat", "updated", "updatedat", "name",
                   "number", "value", "status", "type", "notes", "last", "other", "info", "data"}

# Header words that say "this is a phone column". "number"/"no"/"#" count only without a non-phone word next to it.
# ("#" itself is dropped by _words, so "Cell #" is just ["cell"] and "Account #" is just ["account"].)
PHONE_WORDS = {"phone", "phones", "phonenumber", "mobile", "cell", "cellphone", "cellular", "tel", "telephone", "ph",
               "sms", "text", "texting", "whatsapp", "wireless", "landline", "contact", "msisdn"}
# Words that turn a phone header into a header ABOUT the phone (a setting or label), not the number itself:
# "phone4MarketingPreference", "Phone Type", "SMS Opt In", "Phone Verified", "Phone Ext".
# One of these words anywhere in the header is enough: is_phone_header() then says False, and step 2 of auto_map
# never adds a column that mentions a phone word but isn't a phone header (step 1 scores columns by header and
# values, so such a column full of phone-like values could still be matched there). "fax" is here so "Fax" / "Phone Fax"
# never count as phone headers: a fax line can't receive texts.
PHONE_ATTRIBUTE_WORDS = {"preference", "preferences", "pref", "prefs", "marketing", "type", "kind", "label", "status",
                         "verified", "verification", "valid", "validated", "confirmed", "consent", "opt", "optin",
                         "optout", "opted", "ext", "extension", "country", "countrycode", "code", "carrier", "flag",
                         "enabled", "disabled", "allowed", "allow", "permission", "subscribed", "unsubscribed",
                         "source", "date", "time", "updated", "created", "count", "is", "has", "can", "ok", "fax",
                         "notes", "note", "comment", "comments"}
# "Number"-like words: a short header made of one of these (plus at most one other word) is a phone header, e.g.
# "Contact Number" is caught by "contact" anyway, but "Primary No" or "Number 2" only by this rule.
NUMBER_WORDS = {"number", "num", "no", "nbr", "numbers"}
# Words that make "number" mean some other number: "Order Number", "Member No", "Account #", "Zip Code Number".
# Only checked for the NUMBER_WORDS rule; an explicit phone word ("Customer Phone") still wins.
NOT_PHONE_WORDS = {"order", "account", "acct", "id", "member", "card", "invoice", "ticket", "zip", "postal", "store",
                   "location", "receipt", "check", "transaction", "customer", "loyalty", "reference", "ref", "employee",
                   "table", "party", "guest", "visit", "visits", "points", "count", "fax"}

# A value that looks like a complete one-line address. Two alternatives:
#   1. a comma somewhere, and a 5-digit ZIP (or ZIP+4) at the very end: "12 Main St, Springfield, NJ 07081"
#   2. a comma, a city-like run of letters/spaces/dots, an optional comma and a 2-letter UPPER-CASE state word:
#      "12 Main St, Springfield NJ" or "12 Main St, Springfield, NJ"
# A plain street line ("12 Main St") matches neither, so an ordinary "Address" column is not parsed.
_FULL_ADDR_RE = re.compile(r",.*\b\d{5}(?:-\d{4})?\s*$|,\s*[A-Za-z .]+,?\s+[A-Z]{2}\b")


def _words(text: str) -> list[str]:
    """Split a header into lower-case words, also at camelCase, ACRONYM and letter/digit boundaries.

    Exports name columns in every style, so "phone4MarketingPreference", "SMSOptIn", "Phone 4" and "phone_4" must
    all yield comparable words. Everything that isn't a letter or digit separates words and is dropped.

    Examples::

        _words("phone4MarketingPreference") -> ["phone", "4", "marketing", "preference"]
        _words("SMSOptIn")                  -> ["sms", "opt", "in"]
        _words("Cell #")                    -> ["cell"]
    """
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)  # camelCase → camel Case
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)  # SMSOptIn → SMS OptIn
    text = re.sub(r"([A-Za-z])(\d)", r"\1 \2", text)  # phone4 → phone 4
    text = re.sub(r"(\d)([A-Za-z])", r"\1 \2", text)  # 4Marketing → 4 Marketing
    # Lower-case, then split on anything that isn't a-z / 0-9 and drop the empty pieces.
    return [w for w in re.split(r"[^a-z0-9]+", text.lower()) if w]


def _core(text: str) -> str:
    """Header without words that carry no meaning on their own ("date", "last", "time"…).

    The remaining words are joined without separators so they compare like :func:`norm_header` output.

    Examples::

        _core("Last Visit Date") -> "visit"
        _core("spin_last")       -> "spin"
        _core("Date")            -> ""      (nothing meaningful left)

    Used by :func:`header_score` so that two headers that differ only in filler words still match, and so that
    fuzzy similarity is measured on the meaningful part only.
    """
    return "".join(w for w in _words(text) if w not in FILLER_WORDS)


def _candidates(f: FieldSpec) -> list[str]:
    """All names a source header may match for field ``f``, in order and without duplicates.

    That is the internal field name, the output header, the name with spaces instead of underscores, and every
    synonym from the schema. Example for ``phone_number``: ``["phone_number", "phone number", "phone", "mobile",
    ...]`` (the header "phone_number" equals the name and is dropped as a duplicate).
    """
    names = [f.name, f.header, f.name.replace("_", " "), *f.synonyms]
    # dict.fromkeys keeps the first occurrence of each name and preserves order.
    return list(dict.fromkeys(names))


def is_consent_column(column: str) -> bool:
    """True when the column holds consent / subscription / marketing-permission data.

    Such columns are never auto-mapped to anything (consent must not be derived from source files), and are not
    phone, date or email columns even when their values look like it.

    Matches when the header contains a CONSENT_WORDS word ("SMS Opt In", "Subscribed", "TCPA"), when its
    normalised form contains "optin" / "optout" / "donot" ("OPTIN_FLAG", "Do Not Text", "DoNotCall"), or when it
    is a yes/no marketing permission ("phone4MarketingPreference", "SMS Marketing Allowed", "Marketing OK").
    A plain "Marketing Source" or "Marketing Channel" is not a consent column.
    """
    words = set(_words(column))
    if words & CONSENT_WORDS or any(k in norm_header(column) for k in ("optin", "optout", "donot")):
        return True
    # "phone4MarketingPreference", "SMS Marketing Allowed", "Marketing OK": a yes/no marketing permission.
    return "marketing" in words and bool(words & {"preference", "preferences", "pref", "prefs", "allowed", "allow",
                                                  "ok", "permission", "enabled", "accepted", "agreed", "status"})


def is_phone_header(column: str) -> bool:
    """True when the column name itself says it holds phone numbers ("Mobile", "Cell #", "Contact Number").

    Rules, in order:

    1. A header about the phone rather than the number ("Phone Type", "phoneVerified", "Phone Ext",
       "phone4MarketingPreference", "Fax") or any consent column -> False.
    2. A phone word ("phone", "mobile", "cell", "tel", "sms", ...) -> True ("Home Phone", "Tel.", "SMS Number").
    3. A "number" word in a short header (at most 2 words) without a word that names another kind of number ->
       True ("Phone No" is already caught by 2, "Primary No" here); "Order Number", "Member No" -> False.

    Examples: ``is_phone_header("Mobile") -> True``, ``is_phone_header("Occupation") -> False``.
    """
    words = set(_words(column))
    if words & PHONE_ATTRIBUTE_WORDS or is_consent_column(column):
        return False
    if words & PHONE_WORDS:
        return True
    # Short "number" headers only: "Number 2" yes, "Loyalty Card Number" no (also has a NOT_PHONE word).
    return bool(words & NUMBER_WORDS) and not (words & NOT_PHONE_WORDS) and len(words) <= 2


def enough_evidence(p: ColumnProfile, share: float, minimum: float = 0.9) -> bool:
    """Values alone say "phone" only when nearly all of them are phones and there are enough of them to tell.

    A column like "Occupation" with two values, one of which someone typed a phone number into, is not a phone
    column: 50% phone-like over 2 values is noise, not evidence.

    Inputs: the column profile ``p``, the share (0..1) of its distinct sampled values that look like the type
    (e.g. ``p.shares["phone"]``) and the minimum share (default 90%). Also requires at least 3 distinct values -
    or, when the column has fewer than 3 filled cells, every filled cell distinct.

    Examples: 5 distinct values, all phones -> True; "Occupation" ["Nurse", "2015550100"] -> False (share 0.5);
    an empty column -> False (share 0).
    """
    return share >= minimum and p.unique >= min(3, p.non_empty)


def header_score(column: str, f: FieldSpec) -> float:
    """How well the header ``column`` names field ``f``, from 0 (unrelated) to 100 (exact name or synonym).

    The header is compared with every candidate name of the field (:func:`_candidates`); the best result wins:

    * 100 - same normalised text ("Mobile #" vs synonym "mobile #"; case, spaces and punctuation ignored).
    * 92  - same meaningful words once filler words are removed ("Spin Date" vs "last spin": both "spin").
    * 82  - the candidate (5+ characters) appears inside the header ("Guest Mobile Number" contains
      "mobilenumber").
    * 65  - the header (6+ characters, not generic) appears inside a candidate ("Customer" inside
      "customer last name"): weak, since the header only names part of the field.
    * fuzzy ratio (0-100, rapidfuzz) of the meaningful parts, for typos and word-order variants.

    Generic headers ("Date", "Name", "Status", ...) score 100 only on an exact match, else 0: they could mean
    any field. Returns 0.0 for a header with no letters or digits.
    """
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
        # Containment: short candidates (< 5 chars, e.g. "tel", "cell") are excluded because they occur inside
        # unrelated words ("Hotel", "Excellent").
        if len(c) >= 5 and c in h:
            best = max(best, 82.0)
        elif len(h) >= 6 and h not in GENERIC_HEADERS and h in c:
            best = max(best, 65.0)  # header is only part of a synonym (e.g. "Customer" in "customer last name")
        # Fuzzy similarity on the meaningful part only, so "Join Date" doesn't look like "Spin Date".
        if hc and cc:
            best = max(best, fuzz.ratio(hc, cc))
        elif not hc and not cc:
            # Both consist of filler words only (e.g. "Last Date" vs "date time"): compare the whole headers.
            best = max(best, fuzz.ratio(h, c))
    return best


def score(column: ColumnProfile, f: FieldSpec) -> float:
    """Overall fit (roughly 0..100) of a source column for a target field: header evidence + value evidence.

    * Consent columns -> 0: never mapped.
    * Empty column (no values in this file) -> half the header score: no value evidence, so it can't reach the
      step-1 thresholds of :func:`auto_map` on its own (an empty "Phone 4" is handled by the phone step instead).
    * Typed fields (phone, email, date, datetime, zip, state): ``0.55 * header + 45 * share`` where ``share`` is
      the fraction of the column's values that look like that type. An exact header with all values matching
      scores 100; values alone (header 0, share 1.0) give only 45, below the threshold, so values never map a
      typed field in step 1 - the phone and email steps decide those cases with stricter rules.
      Minus 25 when fewer than 20% of the values fit and the header isn't exact (a "Phone Notes" column of text).
      Date fields are capped at 50 unless the header scores 80+: files often have several date columns
      ("Created", "Last Visit", "Birthday"), so only a header that clearly names the field may claim it.
    * Other fields (string, number, enum, boolean): header score only, minus 40 when the column is clearly
      phones, emails or dates (80%+) and the header isn't exact - e.g. a "Visit Notes" string field shouldn't
      take a column full of dates just because the header is similar.

    Example: header "Cell" (synonym -> 100) with 95% phone-like values -> 0.55*100 + 45*0.95 = 97.75.
    """
    if is_consent_column(column.name):
        return 0.0
    hs = header_score(column.name, f)
    kind = TYPED.get(f.type)
    if column.non_empty == 0:
        return hs * 0.5
    if kind:
        share = column.shares.get(kind, 0.0)
        # Weights: the header counts a bit more than the values (55 vs 45), both together reach 100.
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
    """Confidence label ("high" / "medium" / "low") shown in the review grid for a step-1 match.

    Inputs: the overall :func:`score` ``s``, the :func:`header_score` ``hs`` and, for typed fields only, the
    value ``share`` (``None`` for untyped fields).

    * Exact header (``hs`` 100): "high", but only "medium" for a typed field whose values mostly don't fit
      (share below 30%) - the header says phone, the values disagree, so a reviewer should look.
    * Otherwise by score: 85+ "high", 70+ "medium", below that "low".

    Example: ``_confidence(77.5, 100, 0.5) -> "high"``; ``_confidence(66, 100, 0.2) -> "medium"``.
    """
    if hs >= 100:
        return "high" if share is None or share >= 0.3 else "medium"
    return "high" if s >= 85 else "medium" if s >= 70 else "low"


def auto_map(schema: TargetSchema, profiles: list[ColumnProfile]) -> Mapping:
    """Propose a mapping for every schema field from the source column profiles, using rules only.

    Inputs: the target schema and one :class:`~converter.mapping.ColumnProfile` per source column (from
    ``mapping.profile_columns``). Output: a :class:`~converter.mapping.Mapping` with an entry for every schema
    field; unmatched fields keep the empty ``FieldMapping()`` (transform "none"). Callers run
    ``Mapping.normalized`` on the result.

    Steps (each step only fills what earlier steps left open, unless stated otherwise):

    1. Direct matches - every (field, column) pair scored by :func:`score`; pairs above the threshold are
       assigned best-first, each field and each column at most once.
    2. Phone - collect every phone column (by header, or by clear phone values); several -> "One row per phone
       number" (``each_row``) with mobile columns first; one -> copy. Step 2 never adds phone-setting
       columns such as "phone4MarketingPreference" or a column like "Occupation" with a stray number typed into
       it (a phone-setting column whose values look like phone numbers can still be picked in step 1).
    3. Email - by values alone, when exactly one unused column is 80%+ email addresses.
    4. Full name - a whole-name column ("Customer Name") is split into first_name / last_name.
    5. Full address - a one-line address column is parsed into address1 / address2 / city / state / zip.
    6. Birthday / anniversary - a date split over "Birth Month" + "Birth Day" (+ "Birth Year") columns is
       combined (this step overrides earlier matches of those columns).

    Steps 1-3 never use consent columns (score 0 / explicit skip); steps 4-6 don't check for them (e.g. a
    "Subscriber Name" column, also a consent-looking header, can still be split into first/last name).

    Example (real schema): columns ``["phone", "phone2", "phoneMarketingPreference", "lastVisitDate"]`` with
    phone numbers in the first two -> phone_number from ``phone`` + ``phone2`` (each_row, "2 phone columns: one
    row per different number"), transactions_last copied from ``lastVisitDate`` ("header 'lastVisitDate'
    matches"), and the preference column stays unmapped.
    """
    # Every schema field starts unmapped; `used` holds source columns already taken, so that normally one
    # column feeds one field (steps 4 and 5 deliberately feed several fields from one column).
    fields: dict[str, FieldMapping] = {f.name: FieldMapping() for f in schema.fields}
    used: set[str] = set()
    by_name = {p.name: p for p in profiles}

    # 1. Direct matches, best scores first.
    # Score every (mappable field, column) pair and keep those above the threshold. Typed fields (phone, email,
    # date, ...) need 60 because values add evidence; header-only fields need 72 so a vague similarity alone
    # doesn't map a column. Fixed-blank fields (e.g. consent) are not mappable and never considered.
    scored = []
    for f in schema.mappable_fields:
        threshold = 60 if f.type in TYPED else 72
        for p in profiles:
            s = score(p, f)
            if s >= threshold:
                scored.append((s, f.name, p.name))
    # Greedy assignment, best pair first: a column that fits two fields goes to the one it fits best, and the
    # other field may still get its second-best column.
    scored.sort(reverse=True)
    for s, fname, col in scored:
        if fields[fname].transform != "none" or col in used:
            continue  # field already has a column, or the column already feeds a better-matching field
        f = schema.field(fname)
        hs = header_score(col, f)
        kind = TYPED.get(f.type)
        share = by_name[col].shares.get(kind, 0.0) if kind else 0.0
        # Explain the match in the words a reviewer understands: header, values, or a similar header.
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
    # A file can have several phone columns (Toast exports: phone, phone2, phone3, phone4). All of them feed the
    # phone field, and each different number becomes its own output row (transform "each_row"), so no number is
    # lost. Columns that are only ABOUT the phone (preference, type, opt-in, ...) must never be taken.
    for f in schema.fields:
        if f.type != "phone":
            continue
        fm = fields[f.name]

        def _phone_column(p: ColumnProfile) -> bool:
            """Whether column ``p`` holds phone numbers for the phone step.

            * Phone header ("Mobile", "Phone 2", "Cell #"): yes if 30%+ of its values look like phones, or if it
              is empty in this file (e.g. "Phone 4" in a Toast export: kept so the mapping, and a template saved
              from it, also covers files where that column is filled).
            * Header mentions a phone word but isn't a phone header ("phone4MarketingPreference", "Phone Type"):
              never - it describes the phone, it isn't the number.
            * Header says nothing about phones ("Col 7", "Occupation"): only with clear value evidence (see
              :func:`enough_evidence`), so one stray number typed into "Occupation" doesn't make it a phone column.
            """
            share = p.shares.get("phone", 0)
            if is_phone_header(p.name):
                # Named like a phone column: needs some phone numbers in it (or be empty in this file, e.g. Phone 4).
                return p.non_empty == 0 or share >= 0.3
            if any(w in PHONE_WORDS for w in _words(p.name)):
                return False  # a column ABOUT the phone (preference, type, opt-in…), never the number
            return enough_evidence(p, share)  # column name says nothing about phones: values must be clear

        # Candidates: phone columns not taken by ANOTHER field (columns step 1 gave to this phone field stay
        # eligible), and never consent columns.
        phone_cols = [p.name for p in profiles if _phone_column(p)
                      and p.name not in used - set(fm.sources) and not is_consent_column(p.name)]
        if fm.transform == "none":
            # Step 1 found no phone column by header. Then columns whose VALUES are clearly phone numbers are
            # more trustworthy than weakly named ones: if there are any, use only those.
            strong = [p for p in profiles if p.name not in used and enough_evidence(p, p.shares.get("phone", 0))
                      and not is_consent_column(p.name)]
            if strong:
                phone_cols = [p.name for p in strong]
        if len(phone_cols) >= 2:
            # Several phone columns -> one row per different number (this also replaces a step-1 single copy).
            # Mobile-looking columns first (the first number stays on the file row, the others become extra
            # rows), otherwise file order.
            ordered = sorted(phone_cols, key=lambda c: (not any(h in c.lower() for h in MOBILE_HINTS), phone_cols.index(c)))
            fields[f.name] = FieldMapping(sources=ordered, transform="each_row", origin="rule", confidence="medium",
                                          reason=f"{len(ordered)} phone columns: one row per different number")
            used.update(ordered)
        elif len(phone_cols) == 1 and fm.transform == "none":
            # A single phone column found only now (by header words or values): copy it, with low confidence.
            fields[f.name] = FieldMapping(sources=phone_cols, transform="copy", origin="rule", confidence="low",
                                          reason=f"values in '{phone_cols[0]}' look like phone numbers")
            used.update(phone_cols)

    # 3. Email by value type alone (unambiguous).
    # Only when exactly one unused column is 80%+ email addresses; with two candidates (e.g. "Email" and
    # "Work Email" with unclear headers) the choice is left to Claude or the reviewer.
    for f in schema.fields:
        if f.type == "email" and fields[f.name].transform == "none":
            cands = [p for p in profiles if p.name not in used and p.shares.get("email", 0) >= 0.8
                     and not is_consent_column(p.name)]
            if len(cands) == 1:
                fields[f.name] = FieldMapping(sources=[cands[0].name], transform="copy", origin="rule",
                                              confidence="low", reason="values look like email addresses")
                used.add(cands[0].name)

    # 4. Full name → first / last.
    # Only for schemas with string fields named first_name / last_name.
    name_targets = {f.name: f for f in schema.fields if f.type == "string" and f.name in ("first_name", "last_name")}

    def _from_full_name_column(n: str) -> bool:
        """True when step 1 mapped field ``n`` by copying a whole-name column (e.g. last_name <- "Customer Name").

        Copying a whole name into one part is wrong; such a match is undone and replaced by a split below.
        """
        fm = fields[n]
        return fm.transform == "copy" and norm_header(fm.sources[0]) in FULL_NAME_HEADERS

    # Split only when no real first/last-name column was found: every name field must be unmapped or wrongly
    # copied from a whole-name column. A file with "First Name" + "Last Name" columns is left alone.
    if name_targets and all(fields[n].transform == "none" or _from_full_name_column(n) for n in name_targets):
        for n in name_targets:
            if _from_full_name_column(n):
                # Undo the whole-name copy and release the column so it can be found again below.
                used.discard(fields[n].sources[0])
                fields[n] = FieldMapping()
        # Prefer a header that is clearly a whole name ("Name", "Customer Name", "Guest")...
        full = next((p for p in profiles if norm_header(p.name) in FULL_NAME_HEADERS and p.name not in used), None)
        if full is None:
            # ...else any unused header containing "name", except names of things other than a person
            # ("Username", "File Name", "Store Name", "Company Name", ...) and first/last-name columns.
            full = next((p for p in profiles if "name" in norm_header(p.name) and p.name not in used
                         and not any(k in norm_header(p.name) for k in ("first", "last", "user", "file", "store",
                                                                         "company", "business", "location"))), None)
        if full is not None:
            # The same column feeds both fields; the name_part transform picks the first or last part
            # (it also understands "Last, First").
            for n in name_targets:
                part = "first" if n == "first_name" else "last"
                fields[n] = FieldMapping(sources=[full.name], transform="name_part", params={"part": part},
                                         origin="rule", confidence="medium",
                                         reason=f"split full name in '{full.name}'")
            used.add(full.name)

    # 5. Full single-line address → street / unit / city / state / zip.
    # Schema field name -> part extracted by the address_part transform. Only fields present in the schema count.
    addr_targets = {"address1": "line1", "address2": "line2", "city": "city", "state": "state", "zip": "zip"}
    present = {k: v for k, v in addr_targets.items() if k in fields}
    if present:
        current = fields.get("address1")
        addr_col = None
        # Case A: step 1 copied a column into address1, but its values are whole addresses
        # ("12 Main St, Springfield, NJ 07081") -> parse that column instead of copying it.
        if current and current.transform == "copy" and current.sources:
            p = by_name.get(current.sources[0])
            if p and _mostly_full_addresses(p):
                addr_col = p.name
        # Case B: address1 still unmapped -> look for an unused full-address column (header AND values must fit).
        if addr_col is None and current is not None and current.transform == "none":
            for p in profiles:
                if p.name not in used and (norm_header(p.name) in FULL_ADDRESS_HEADERS) and _mostly_full_addresses(p):
                    addr_col = p.name
                    break
        if addr_col:
            # address1 always switches to the parsed street line; the other parts are only filled when no
            # dedicated column (e.g. a separate "City" or "Zip" column) was matched already.
            for target, part in present.items():
                if target == "address1" or fields[target].transform == "none":
                    fields[target] = FieldMapping(sources=[addr_col], transform="address_part", params={"part": part},
                                                  origin="rule", confidence="medium",
                                                  reason=f"parsed from full address in '{addr_col}'")
            used.add(addr_col)

    # 6. Birthday / anniversary split over a month column and a day column (and maybe a year column).
    # Each target is paired with the header words that identify its columns ("Birth Month", "dobDay",
    # "anniversaryMonth", "Wedding Day", ...). The parts are joined with a space ("9 12" / "Sep 25 1985") and the
    # date normaliser parses the result; a month + day without a year gets the schema's no-year value (e.g. 1600).
    for target, keys in (("birthday", {"birth", "birthday", "bday", "dob", "born"}),
                         ("anniversary", {"anniversary", "anniv", "wedding"})):
        if target not in fields:
            continue
        parts = _date_part_columns(profiles, keys)
        if parts:
            fields[target] = FieldMapping(sources=parts, transform="concat", params={"separator": " "}, origin="rule",
                                          confidence="medium",
                                          reason=f"{target} is split over {', '.join(repr(c) for c in parts)}")
            # If a part column already feeds another field (matched in step 1 by a similar header), clear that
            # field so the column isn't used twice.
            for name, fm in fields.items():
                if name != target and set(fm.sources) & set(parts):
                    fields[name] = FieldMapping()
            used.update(parts)

    return Mapping(fields=fields)


def _date_part_columns(profiles: list[ColumnProfile], keys: set[str]) -> list[str]:
    """['Birth Month', 'Birth Day'(, 'Birth Year')] when a date is split over separate columns, else [].

    ``keys`` are the header words that identify the date (e.g. {"birth", "birthday", "dob", ...}). A column is a
    part column when its header words contain one of ``keys`` AND a part word:

    * month - "month" / "mon" / "mo" / "mm"; values may be numbers or names ("9", "Sep", "September").
    * day   - "day" / "dd"; every sample value must be a whole number from 1 to 31.
    * year  - "year" / "yr" / "yyyy"; optional (may be blank for many rows).

    A month and a day column, and two different ones, are required: a single "Birthday" column holding "9/12"
    is not split (it has no part word), and neither is a "Birth Day" column holding "9/12" (not a day number).

    Example: headers ``["phone", "Birth Month", "Birth Day", "Birth Year"]`` with keys for birthday ->
    ``["Birth Month", "Birth Day", "Birth Year"]``.
    """
    def find(part_words: set[str], numbers_up_to: int | None) -> str | None:
        """First column whose header has a key word and a part word (and, optionally, only numbers 1..N)."""
        for p in profiles:
            words = set(_words(p.name))
            if not (words & keys) or not (words & part_words):
                continue
            # Value check (day column): every sample must be a whole number from 1 to `numbers_up_to`.
            # A column without samples (empty in this file) passes on the header alone.
            if numbers_up_to is not None and p.samples and not all(
                    s.strip().isdigit() and 1 <= int(s) <= numbers_up_to for s in p.samples):
                continue
            return p.name
        return None

    month = find({"month", "mon", "mo", "mm"}, None)
    day = find({"day", "dd"} , 31)
    # Both parts are needed, from two different columns (a header like "Birth Month Day" matches both words).
    if not (month and day) or month == day:
        return []
    year = find({"year", "yr", "yyyy"}, None)
    return [month, day] + ([year] if year and year not in (month, day) else [])


def _mostly_full_addresses(p: ColumnProfile) -> bool:
    """True when at least half of the column's sample values look like complete one-line addresses.

    "Complete" means street plus city/state/ZIP after a comma (see ``_FULL_ADDR_RE``), e.g.
    "12 Main St, Springfield, NJ 07081". A column of plain street lines ("12 Main St") returns False, so it is
    copied as-is instead of being parsed. A column without samples returns False.
    """
    if not p.samples:
        return False
    hits = sum(bool(_FULL_ADDR_RE.search(s)) for s in p.samples)
    # At least one hit, and at least 50% of the samples: a few incomplete addresses don't block parsing.
    return hits >= max(1, 0.5 * len(p.samples))
