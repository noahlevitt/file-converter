"""Type normalizers: turn a raw extracted value into the exact output format, or report why it can't be.

Each target field type has one normalizer. Column-level context (e.g. whether a date column is
month-first or day-first) is inferred once per column in `prepare()` so ambiguous values are
resolved consistently instead of value by value.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any

import phonenumbers
import unicodedata
from phonenumbers import geocoder as _geo
from dateutil import parser as du_parser
from email_validator import EmailNotValidError, validate_email
from nameparser import HumanName

from . import dateparse as DP
from .schema import FieldSpec

# ---------------------------------------------------------------------------
# Shared text cleanup
# ---------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_NULL_TOKENS = {"", "null", "none", "nan", "n/a", "na", "#n/a", "-", "--", "undefined", "nil", "never", "nat"}


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    s = _CTRL_RE.sub("", str(value)).replace(" ", " ")
    s = _WS_RE.sub(" ", s).strip()
    return "" if s.lower() in _NULL_TOKENS else s


def apply_case(value: str, case: str) -> str:
    if not value or case == "none":
        return value
    if case == "upper":
        return value.upper()
    if case == "lower":
        return value.lower()
    if case == "title":
        return value.title() if (value.isupper() or value.islower()) else value
    if case == "name":
        if value.isupper() or value.islower():
            n = HumanName(value)
            n.capitalize(force=True)
            out = str(n).strip()
            return out or value.title()
        return value
    return value


# ---------------------------------------------------------------------------
# Phone
# ---------------------------------------------------------------------------

_SCI_RE = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)[eE]\+?(\d+)\s*$")
_FLOAT_ARTIFACT_RE = re.compile(r"^(\d+)\.0+$")


def _fix_numeric_artifacts(value: str) -> tuple[str, str | None]:
    m = _FLOAT_ARTIFACT_RE.match(value)
    if m:
        return m.group(1), None
    m = _SCI_RE.match(value)
    if m:
        mantissa = m.group(1).lstrip("+-")
        significant = len(mantissa.replace(".", "").lstrip("0"))
        if significant < 10:
            return value, "stored in scientific notation; digits were lost in Excel"
        try:
            return str(int(Decimal(value.strip()))), None
        except InvalidOperation:
            return value, "unreadable number"
    return value, None


PHONE_FORMATS = {"E164", "digits10", "digits11", "national", "dashed"}


def _format_phone(num: phonenumbers.PhoneNumber, fmt: str) -> str:
    fmt = fmt or "E164"
    nsn = phonenumbers.national_significant_number(num)
    if fmt == "E164":
        return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)
    if fmt == "digits10":
        return nsn
    if fmt == "digits11":
        return f"{num.country_code}{nsn}"
    if fmt == "national":
        return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.NATIONAL)
    if fmt == "dashed":
        if num.country_code == 1 and len(nsn) == 10:
            return f"{nsn[:3]}-{nsn[3:6]}-{nsn[6:]}"
        return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.NATIONAL)
    return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)


_LOOKALIKE = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1"})
_PHONE_PREFIX_RE = re.compile(r"^\s*(tel|sms|phone|ph|mobile|cell|callto|whatsapp)\s*[:=]\s*", re.I)
_EXT_RE = re.compile(r"[;,]?\s*(?:ext\.?|extension|ex\.?|x|#|;ext=)\s*\d{1,6}\s*$", re.I)
_LABEL_MOBILE_RE = re.compile(r"(mobile|cell|cel|wireless|sms|text|\bm\b|\bc\b)\W*$", re.I)
_LABEL_FAX_RE = re.compile(r"(fax|\bf\b)\W*$", re.I)
_LABEL_OTHER_RE = re.compile(r"(home|work|office|business|land\s*line|main|\bh\b|\bw\b|\bo\b)\W*$", re.I)


def _prep_phone(value: str) -> str:
    v = unicodedata.normalize("NFKC", value).replace("\u200b", "").replace("\ufeff", "").strip()
    m = re.match(r'^=?"(.*)"$', v)  # Excel text-protection wrapper ="5165550100"
    if m:
        v = m.group(1)
    v = v.strip().lstrip("'").strip()
    v = re.sub(r"[\u2010-\u2015\u2212\u2043]", "-", v)  # unicode dashes
    v = _PHONE_PREFIX_RE.sub("", v)  # tel:, sms:, Phone:
    # Letters typed for digits inside a number (516-555-O1OO, 5l6…): O→0, I/l→1. Done before vanity
    # conversion, which would otherwise read O as 6 on a keypad.
    v = re.sub(r"[\dOoIl().\-]{4,}",
               lambda m: m.group(0).translate(_LOOKALIKE) if sum(c.isdigit() for c in m.group(0)) >= 3 else m.group(0), v)
    # International exit codes before the US country code: 011 1 …, 00 1 …, +011 1 …
    v = re.sub(r"^\s*\+?\s*(?:011|00)[\s.\-]*(?=1\D?\d)", "+", v)
    # "+" followed by 10 digits that can't start a country code of their own → the "1" was left out.
    # Only when that gives a real North American number; otherwise it's a foreign number (+51 1 4567890 = Lima).
    m = re.match(r"^\s*\+\s*([2-9][\d\s().\-]*)$", v)
    if m and len(digits := re.sub(r"\D", "", m.group(1))) == 10 and phonenumbers.is_valid_number(
            phonenumbers.PhoneNumber(country_code=1, national_number=int(digits))):
        v = "+1 " + m.group(1)
    return v


def _digits_fallback(text: str) -> tuple[phonenumbers.PhoneNumber | None, str | None]:
    """Last resort for values the matcher can't find a number in (one number, odd separators, vanity letters)."""
    body = _EXT_RE.sub("", text).strip()
    letters = re.sub(r"[^A-Za-z]", "", body)
    if letters:
        alnum = re.sub(r"[^0-9A-Za-z]", "", body)
        is_vanity = (re.fullmatch(r"[\s+().\-\dA-Za-z]+", body) and 10 <= len(alnum) <= 11
                     and sum(c.isdigit() for c in alnum) >= 3 and len(letters) >= 3)
        if not is_vanity:
            body = re.sub(r"[A-Za-z]+", " ", body)  # drop words like "cell" or "home"
        else:
            body = phonenumbers.convert_alpha_characters_in_number(body)  # 1-800-FLOWERS
    digits = re.sub(r"\D", "", body)
    if not digits:
        return None, "no phone number found"
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    if len(digits) == 7:
        return None, "missing area code (only 7 digits)"
    if len(digits) < 10:
        return None, f"too few digits ({len(digits)})"
    if len(digits) > 10:
        return None, f"too many digits ({len(digits)})"
    try:
        return phonenumbers.parse("+1" + digits, None), None
    except phonenumbers.NumberParseException:
        return None, "not a phone number"


_DOUBLE_CC_RE = re.compile(r"^\s*\+\s*1[\s.\-]*(\+\s*\d.*)$")


@lru_cache(maxsize=1000)
def _area_code_in_use(npa: str) -> bool:
    """Whether any number in this North American area code can exist (US, territories or Canada…)."""
    if not re.fullmatch(r"[2-9]\d\d", npa):
        return False
    for nxx in range(200, 1000):
        if phonenumbers.is_valid_number(phonenumbers.PhoneNumber(country_code=1, national_number=int(f"{npa}{nxx}4567"))):
            return True
    return False


def _foreign_reason(num: phonenumbers.PhoneNumber) -> str:
    """'not a US number — Canada (area code 514, Quebec)' / 'not a US number — Peru (+51, Pasco)'."""
    country = _geo.country_name_for_number(num, "en") or phonenumbers.region_code_for_number(num) or "another country"
    place = _geo.description_for_number(num, "en")
    place = "" if place == country else place
    if num.country_code == 1:
        detail = f"area code {str(num.national_number)[:3]}" + (f", {place}" if place else "")
    else:
        detail = f"+{num.country_code}" + (f", {place}" if place else "")
    return f"not a US number — {country} ({detail})"


@lru_cache(maxsize=200_000)
def _parse_phone(value: str, region: str, countries: tuple[str, ...], fmt: str) -> tuple[str, str | None]:
    # "+1 +51 63 171059": a system put +1 in front of a number that already had a country code.
    m = _DOUBLE_CC_RE.match(unicodedata.normalize("NFKC", value))
    if m:
        out, err = _parse_phone(m.group(1), region, countries, fmt)
        if err is None:
            return out, None
        if err.startswith("not a US number"):
            return "", err + " — the file put +1 in front of its own country code"
        try:
            cc = phonenumbers.parse(m.group(1), None).country_code
        except phonenumbers.NumberParseException:
            cc = None
        return "", ("has two country codes (+1 and " + (f"+{cc}" if cc else "another") + ") and isn't a valid "
                    "number either way")
    prepped = _prep_phone(value)
    fixed, err = _fix_numeric_artifacts(prepped)
    if err:
        return "", err
    # Find every number in the text; prefer ones labelled mobile/cell, then unlabelled, then home/work, then fax.
    candidates: list[tuple[int, int, phonenumbers.PhoneNumber]] = []
    try:
        for match in phonenumbers.PhoneNumberMatcher(fixed, region, leniency=phonenumbers.Leniency.POSSIBLE):
            before = fixed[max(0, match.start - 14): match.start]
            prio = (0 if _LABEL_MOBILE_RE.search(before) else 3 if _LABEL_FAX_RE.search(before)
                    else 2 if _LABEL_OTHER_RE.search(before) else 1)
            candidates.append((prio, match.start, match.number))
    except Exception:
        pass
    fallback_err = None
    if not candidates:
        num, fallback_err = _digits_fallback(fixed)
        if num is not None:
            candidates.append((1, 0, num))
    if not candidates:
        return "", fallback_err or "no phone number found"
    candidates.sort(key=lambda c: (c[0], c[1]))
    foreign = None
    for _, _, num in candidates:
        if not phonenumbers.is_valid_number(num):
            continue
        cc = phonenumbers.region_code_for_number(num)
        if countries and cc not in countries:
            foreign = foreign or num
            continue
        return _format_phone(num, fmt), None
    if foreign:
        return "", _foreign_reason(foreign)
    digits = re.sub(r"\D", "", _EXT_RE.sub("", fixed))
    if len(digits) == 7:
        return "", "missing area code (only 7 digits)"
    if len(digits) < 10:
        return "", f"too few digits ({len(digits)})"
    if len(digits) > 11 and len(candidates) == 1:
        return "", f"too many digits ({len(digits)})"
    if len(digits) == 10 or (len(digits) == 11 and digits[0] == "1"):
        npa, nxx = digits[-10:-7], digits[-7:-4]
        if not _area_code_in_use(npa):
            return "", f"not a real US number (area code {npa} isn't in use)"
        return "", f"not a real US number ({nxx} isn't a working exchange in area code {npa})"
    return "", "not a real US number (area code or exchange doesn't exist)"


def normalize_phone(value: str, field: FieldSpec, params: dict, ctx: dict) -> tuple[str, str | None]:
    region = (params.get("region") or (field.countries[0] if field.countries else "US")).upper()
    countries = tuple(c.upper() for c in field.countries)
    return _parse_phone(value, region, countries, field.format or "E164")


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

DATE_ORDERS = {"MDY", "DMY", "YMD"}
_ZERO_DATE_RE = re.compile(r"^0{1,4}[-/.]0{1,2}[-/.]0{1,4}(\s*[t ]\s*0{1,2}:0{2}(:0{2}(\.0+)?)?)?$", re.I)
_NUMERIC_ONLY_RE = re.compile(r"^\d+(\.\d+)?$")
_NO_YEAR = DP.NO_YEAR
MIN_YEAR, MAX_YEAR = DP.MIN_YEAR, DP.MAX_YEAR


def prepare_date(field: FieldSpec, values: list[str], params: dict) -> dict:
    """Decide once per column: month-first or day-first, and whether bare numbers are Excel serial dates."""
    sample = [v for v in dict.fromkeys(v for v in values if v)][:2000]
    ctx: dict[str, Any] = {"order": "MDY", "format": params.get("input_format") or None, "serial": False}
    requested = (params.get("date_order") or "auto").upper()

    numeric = [v for v in sample if _NUMERIC_ONLY_RE.match(v)]
    if sample and len(numeric) >= 0.8 * len(sample):
        nums = [float(v) for v in numeric]
        if all(1 <= n <= 2_958_465 for n in nums) and sum(10_000 <= n <= 80_000 for n in nums) >= 0.8 * len(nums):
            ctx["serial"] = True

    # Evidence for day-first: values like 15/03/2026 (first part > 12) vs month-first 03/15/2026.
    mdy_votes = dmy_votes = 0
    for v in sample:
        m = re.match(r"^\s*(\d{1,2})[/\-. ](\d{1,2})(?:[/\-. ](\d{2,4}))?\b", v)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > 12 >= b:
                dmy_votes += 1
            elif b > 12 >= a:
                mdy_votes += 1
    if requested in DATE_ORDERS:
        ctx["order"] = requested
    elif dmy_votes >= 2 and dmy_votes > 3 * mdy_votes:
        ctx["order"] = "DMY"  # US default unless the column clearly is day-first
    ctx["evidence"] = {"month_first": mdy_votes, "day_first": dmy_votes}
    return ctx


def _dateutil_parse(value: str, dayfirst: bool) -> datetime | None:
    try:
        a = du_parser.parse(value, dayfirst=dayfirst, default=datetime(2000, 1, 1))
        b = du_parser.parse(value, dayfirst=dayfirst, default=datetime(2004, 2, 2))
    except (ValueError, OverflowError, TypeError):
        return None
    if (a.year, a.month, a.day) != (b.year, b.month, b.day):
        return None  # a date part was missing and got filled in by the default
    return a.replace(tzinfo=None)


def parse_date(value: str, ctx: dict, allow_no_year: bool = False) -> tuple[datetime | None, str | None]:
    fmt = ctx.get("format")
    if fmt:
        try:
            return datetime.strptime(value.strip(), fmt), None
        except ValueError:
            pass
    try:
        dt, _ = DP.parse(value, order=ctx.get("order", "MDY"), allow_no_year=allow_no_year,
                         serial_hint=bool(ctx.get("serial")), target_tz=ctx.get("tz") or DP.DEFAULT_TZ)
        return dt, None
    except DP.DateError as exc:
        msg = str(exc)
    if msg.startswith("unrecognized word") or msg == "unrecognized date":
        dt = _dateutil_parse(value, dayfirst=ctx.get("order") == "DMY")
        if dt is not None:
            return dt, None
    return None, msg


def _strftime_year(dt: datetime, fmt: str, year_text: str) -> str:
    """strftime with a literal year (portable for years like 1600 and for month/day-only values)."""
    safe = dt.replace(year=2000)  # leap year, so Feb 29 survives
    marked = fmt.replace("%Y", "\x00Y").replace("%y", "\x00y")
    return safe.strftime(marked).replace("\x00Y", year_text).replace("\x00y", year_text[-2:])


def normalize_date(value: str, field: FieldSpec, params: dict, ctx: dict) -> tuple[str, str | None]:
    out_fmt = field.format or ("%Y-%m-%d %H:%M:%S" if field.type == "datetime" else "%Y-%m-%d")
    if _ZERO_DATE_RE.match(value.strip()):
        return "", None  # "0000-00-00" style placeholders mean no date
    has_year = "%Y" in out_fmt or "%y" in out_fmt
    no_year_value = field.no_year_value if field.type == "date" else None
    allow_no_year = not has_year or no_year_value is not None
    dt, err = parse_date(value, ctx, allow_no_year=allow_no_year)
    if err or dt is None:
        return "", err or "unrecognized date"
    # Month+day only, the target's own no-year value (1600), or a placeholder year systems use for
    # "unknown" (1900 and earlier, e.g. 01/15/1900) → output with the no-year value.
    no_year = dt.year == _NO_YEAR or (no_year_value is not None and dt.year <= 1900)
    if no_year:
        return _strftime_year(dt, out_fmt, str(no_year_value) if no_year_value is not None else "2000"), None
    if not (MIN_YEAR <= dt.year <= MAX_YEAR):
        return "", f"year {dt.year} is out of range"
    return dt.strftime(out_fmt), None


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

@lru_cache(maxsize=200_000)
def _parse_email(value: str) -> tuple[str, str | None]:
    v = value.strip().strip("<>").removeprefix("mailto:").strip()
    v = re.split(r"[;,\s]+", v)[0] if v else v
    try:
        info = validate_email(v, check_deliverability=False)
        return info.normalized.lower(), None
    except EmailNotValidError as exc:
        return "", f"invalid email ({str(exc).split('.')[0].lower()})"


def normalize_email(value: str, field: FieldSpec, params: dict, ctx: dict) -> tuple[str, str | None]:
    return _parse_email(value)


# ---------------------------------------------------------------------------
# ZIP / State
# ---------------------------------------------------------------------------

_ZIP_RE = re.compile(r"^(\d{5})(?:[-\s]?(\d{4}))?$")


def normalize_zip(value: str, field: FieldSpec, params: dict, ctx: dict) -> tuple[str, str | None]:
    v, _ = _fix_numeric_artifacts(value.strip())
    v = v.replace(" ", "") if re.fullmatch(r"[\d\s-]+", v) else v
    if re.fullmatch(r"\d{3,4}", v):
        v = v.zfill(5)  # Excel dropped leading zeros (NJ, New England)
    elif re.fullmatch(r"\d{7,8}", v):
        v = v.zfill(9)  # ZIP+4 without dash and with leading zeros lost
    m = _ZIP_RE.match(v)
    if not m:
        return "", "invalid ZIP code"
    zip5, plus4 = m.group(1), m.group(2)
    if (field.format or "zip5") == "zip5_4" and plus4:
        return f"{zip5}-{plus4}", None
    return zip5, None


US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "district of columbia": "DC", "washington dc": "DC",
    "washington d c": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME",
    "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH",
    "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC", "north dakota": "ND",
    "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY", "puerto rico": "PR", "guam": "GU", "us virgin islands": "VI", "virgin islands": "VI",
    "american samoa": "AS", "northern mariana islands": "MP",
    # common abbreviations
    "penn": "PA", "penna": "PA", "mass": "MA", "calif": "CA", "conn": "CT", "fla": "FL", "ill": "IL",
    "wash": "WA", "tex": "TX", "n y": "NY", "n j": "NJ", "nyc": "NY",
}
STATE_CODES = set(US_STATES.values())


def normalize_state(value: str, field: FieldSpec, params: dict, ctx: dict) -> tuple[str, str | None]:
    v = re.sub(r"[.\s]+", " ", value.strip().lower()).strip()
    if v.upper() in STATE_CODES:
        return v.upper(), None
    compact = v.replace(" ", "")
    if len(compact) == 2 and compact.upper() in STATE_CODES:
        return compact.upper(), None
    if v in US_STATES:
        return US_STATES[v], None
    return "", "not a US state"


# ---------------------------------------------------------------------------
# Numbers / booleans / enums / strings
# ---------------------------------------------------------------------------

def _to_decimal(value: str) -> Decimal | None:
    v = value.strip().replace(",", "").replace("$", "").replace("€", "").replace("£", "").replace("%", "")
    neg = v.startswith("(") and v.endswith(")")
    v = v.strip("()").strip()
    try:
        d = Decimal(v)
    except InvalidOperation:
        return None
    return -d if neg else d


def normalize_integer(value: str, field: FieldSpec, params: dict, ctx: dict) -> tuple[str, str | None]:
    d = _to_decimal(value)
    if d is None or d != d.to_integral_value():
        return "", "not a whole number"
    return str(int(d)), None


def normalize_decimal(value: str, field: FieldSpec, params: dict, ctx: dict) -> tuple[str, str | None]:
    d = _to_decimal(value)
    if d is None:
        return "", "not a number"
    return format(d.normalize(), "f"), None


TRUE_TOKENS = {"y", "yes", "true", "t", "1", "x", "on", "opted in", "opt in", "optin", "subscribed", "active", "✓"}
FALSE_TOKENS = {"n", "no", "false", "f", "0", "off", "opted out", "opt out", "optout", "unsubscribed", "inactive"}


def normalize_boolean(value: str, field: FieldSpec, params: dict, ctx: dict) -> tuple[str, str | None]:
    v = value.strip().lower()
    if v in TRUE_TOKENS:
        return field.true_value, None
    if v in FALSE_TOKENS:
        return field.false_value, None
    return "", "not a yes/no value"


def normalize_enum(value: str, field: FieldSpec, params: dict, ctx: dict) -> tuple[str, str | None]:
    lookup = {a.lower(): a for a in field.allowed_values}
    hit = lookup.get(value.strip().lower())
    if hit is None:
        return "", f"not one of the allowed values ({', '.join(field.allowed_values[:8])})"
    return hit, None


def normalize_string(value: str, field: FieldSpec, params: dict, ctx: dict) -> tuple[str, str | None]:
    v = apply_case(value, field.case)
    if field.max_length and len(v) > field.max_length:
        if field.truncate:
            return v[: field.max_length].rstrip(), None
        return "", f"longer than {field.max_length} characters"
    return v, None


NORMALIZERS = {
    "string": normalize_string,
    "phone": normalize_phone,
    "date": normalize_date,
    "datetime": normalize_date,
    "email": normalize_email,
    "zip": normalize_zip,
    "state": normalize_state,
    "integer": normalize_integer,
    "decimal": normalize_decimal,
    "boolean": normalize_boolean,
    "enum": normalize_enum,
}


def prepare(field: FieldSpec, values: list[str], params: dict) -> dict:
    if field.type in ("date", "datetime"):
        return prepare_date(field, values, params)
    return {}


def normalize(value: str, field: FieldSpec, params: dict, ctx: dict) -> tuple[str, str | None]:
    """Return (output_value, error). Empty input returns ("", None); required-ness is checked by the engine."""
    v = clean_text(value)
    if not v:
        return "", None
    out, err = NORMALIZERS[field.type](v, field, params, ctx)
    if err is None and field.type != "string" and field.max_length and len(out) > field.max_length:
        return "", f"longer than {field.max_length} characters"
    return out, err


# ---------------------------------------------------------------------------
# Lightweight detectors used for column profiling / auto-mapping
# ---------------------------------------------------------------------------

def looks_like_phone(v: str) -> bool:
    digits = re.sub(r"\D", "", v)
    if not (10 <= len(digits) <= 11):
        return False
    if re.search(r"[A-Za-z@]", v.replace("ext", "").replace("x", "")):
        return False
    return _parse_phone(v, "US", ("US", "CA"), "E164")[1] is None


def looks_like_email(v: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$", v.strip()))


def looks_like_zip(v: str) -> bool:
    return bool(re.fullmatch(r"\d{5}(-\d{4})?", v.strip())) or bool(re.fullmatch(r"\d{3,4}", v.strip()))


def looks_like_state(v: str) -> bool:
    return normalize_state(v, None, {}, {})[1] is None  # type: ignore[arg-type]


def looks_like_date(v: str) -> bool:
    v = v.strip()
    if _NUMERIC_ONLY_RE.match(v) or not re.search(r"\d", v):
        return False
    try:
        dt, _ = DP.parse(v, allow_no_year=True)
    except DP.DateError:
        return False
    return dt.year == _NO_YEAR or MIN_YEAR <= dt.year <= MAX_YEAR
