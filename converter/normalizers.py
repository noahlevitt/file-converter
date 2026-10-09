"""Type normalizers: turn a raw extracted value into the exact output format, or report why it can't be.

Each target field type has one normalizer. Column-level context (e.g. whether a date column is
month-first or day-first) is inferred once per column in `prepare()` so ambiguous values are
resolved consistently instead of value by value.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any

import phonenumbers
from dateutil import parser as du_parser
from email_validator import EmailNotValidError, validate_email
from nameparser import HumanName

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


@lru_cache(maxsize=200_000)
def _parse_phone(value: str, region: str, countries: tuple[str, ...], fmt: str) -> tuple[str, str | None]:
    fixed, err = _fix_numeric_artifacts(value)
    if err:
        return "", err
    candidates: list[phonenumbers.PhoneNumber] = []
    try:
        for match in phonenumbers.PhoneNumberMatcher(fixed, region, leniency=phonenumbers.Leniency.POSSIBLE):
            candidates.append(match.number)
    except Exception:
        pass
    if not candidates:
        digits = re.sub(r"\D", "", fixed)
        if not digits:
            return "", "no phone number found"
        try:
            candidates.append(phonenumbers.parse(fixed, region))
        except phonenumbers.NumberParseException:
            return "", "not a phone number"
    for num in candidates:
        if not phonenumbers.is_valid_number(num):
            continue
        cc = phonenumbers.region_code_for_number(num)
        if countries and cc not in countries:
            continue
        return _format_phone(num, fmt), None
    num = candidates[0]
    if phonenumbers.is_valid_number(num):
        cc = phonenumbers.region_code_for_number(num)
        return "", f"number is from {cc}, not an allowed country ({', '.join(countries)})"
    digits = re.sub(r"\D", "", fixed)
    if len(digits) < 10:
        return "", f"too few digits ({len(digits)})"
    if len(digits) > 11 and not fixed.strip().startswith("+"):
        return "", f"too many digits ({len(digits)})"
    return "", "invalid phone number (bad area code or exchange)"


def normalize_phone(value: str, field: FieldSpec, params: dict, ctx: dict) -> tuple[str, str | None]:
    region = (params.get("region") or (field.countries[0] if field.countries else "US")).upper()
    countries = tuple(c.upper() for c in field.countries)
    return _parse_phone(value, region, countries, field.format or "E164")


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

DATE_FORMATS_MDY = ["%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y", "%m.%d.%Y"]
DATE_FORMATS_DMY = ["%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d-%m-%y", "%d.%m.%Y"]
DATE_FORMATS_YMD = ["%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d"]
DATE_FORMATS_TEXT = ["%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y", "%d %b %Y", "%d %B %Y", "%d-%b-%Y",
                     "%d-%b-%y", "%a, %b %d, %Y", "%A, %B %d, %Y"]
TIME_SUFFIXES = ["", " %H:%M:%S", " %H:%M", " %I:%M %p", " %I:%M:%S %p", "T%H:%M:%S", "T%H:%M:%SZ", " %H:%M:%S.%f",
                 "T%H:%M:%S.%f"]

ORDER_FORMATS = {"MDY": DATE_FORMATS_MDY, "DMY": DATE_FORMATS_DMY, "YMD": DATE_FORMATS_YMD}

_PARTIAL_MD_RE = re.compile(r"^(\d{1,2})[/\-.](\d{1,2})$")
_PARTIAL_TEXT_FORMATS = ["%b %d", "%B %d", "%d %b", "%d %B", "%d-%b", "%b-%d", "%b. %d", "%B, %d"]
_ZERO_DATE_RE = re.compile(r"^0{1,4}[-/.]0{1,2}[-/.]0{1,4}(\s+0{1,2}:0{2}(:0{2})?)?$")
_NO_YEAR = 4  # internal marker year for "month and day only" (a leap year, so Feb 29 is valid)
_NUMERIC_ONLY_RE = re.compile(r"^\d+(\.\d+)?$")
_ORDINAL_RE = re.compile(r"(\d+)(st|nd|rd|th)\b", re.IGNORECASE)
_EXCEL_EPOCH = datetime(1899, 12, 30)
MIN_YEAR, MAX_YEAR = 1900, 2100


def _all_formats(order_first: str) -> list[str]:
    orders = [order_first] + [o for o in ("MDY", "DMY", "YMD") if o != order_first]
    base = [f for o in orders for f in ORDER_FORMATS[o]] + DATE_FORMATS_TEXT
    return [b + t for b in base for t in TIME_SUFFIXES]


def _strip_tz(value: str) -> str:
    return re.sub(r"(\s*(UTC|GMT|[+-]\d{2}:?\d{2}|Z))$", "", value, flags=re.IGNORECASE).strip()


def prepare_date(field: FieldSpec, values: list[str], params: dict) -> dict:
    """Infer one format / day-month order for the whole column."""
    sample = [v for v in dict.fromkeys(v for v in values if v)][:500]
    ctx: dict[str, Any] = {"order": "MDY", "format": None, "serial": False, "epoch": False}

    requested = (params.get("date_order") or "auto").upper()
    if params.get("input_format"):
        ctx["format"] = params["input_format"]
    if not sample:
        if requested in ORDER_FORMATS:
            ctx["order"] = requested
        return ctx

    numeric = [v for v in sample if _NUMERIC_ONLY_RE.match(v)]
    if len(numeric) >= 0.8 * len(sample):
        nums = [float(v) for v in numeric]
        if all(10_000 <= n <= 80_000 for n in nums):
            ctx["serial"] = True
        elif all(946_684_800 <= n <= 4_102_444_800 for n in nums):  # 2000-01-01 .. 2100-01-01 (unix seconds)
            ctx["epoch"] = True

    # Evidence for day-first vs month-first from values where one part is > 12.
    mdy_votes = dmy_votes = 0
    for v in sample:
        m = re.match(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})", v)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > 12 >= b:
                dmy_votes += 1
            elif b > 12 >= a:
                mdy_votes += 1

    if requested in ORDER_FORMATS:
        ctx["order"] = requested
    else:
        ctx["order"] = "DMY" if dmy_votes > mdy_votes else "MDY"
    ctx["evidence"] = {"month_first": mdy_votes, "day_first": dmy_votes}

    if ctx["format"] is None and not (ctx["serial"] or ctx["epoch"]):
        best_fmt, best_hits = None, 0
        cleaned = [_strip_tz(_ORDINAL_RE.sub(r"\1", v)) for v in sample]
        for fmt in _all_formats(ctx["order"]):
            hits = 0
            for v in cleaned:
                try:
                    datetime.strptime(v, fmt)
                    hits += 1
                except ValueError:
                    pass
            if hits > best_hits:
                best_fmt, best_hits = fmt, hits
                if hits == len(cleaned):
                    break
        if best_fmt and best_hits >= 0.5 * len(cleaned):
            ctx["format"] = best_fmt
    return ctx


def _dateutil_parse(value: str, dayfirst: bool, yearfirst: bool) -> datetime | None:
    try:
        a = du_parser.parse(value, dayfirst=dayfirst, yearfirst=yearfirst, default=datetime(2000, 1, 1))
        b = du_parser.parse(value, dayfirst=dayfirst, yearfirst=yearfirst, default=datetime(2004, 2, 2))
    except (ValueError, OverflowError, TypeError):
        return None
    if (a.year, a.month, a.day) != (b.year, b.month, b.day):
        return None  # some date component was missing and got filled in by default
    return a.replace(tzinfo=None)


def parse_date(value: str, ctx: dict, allow_no_year: bool = False) -> tuple[datetime | None, str | None]:
    v = _strip_tz(_ORDINAL_RE.sub(r"\1", value.strip()))
    if _NUMERIC_ONLY_RE.match(v):
        n = float(v)
        # A bare number of 20000–80000 in a date column is an Excel serial date (1954–2119).
        if (ctx.get("serial") and 1 <= n <= 2_958_465) or (20_000 <= n <= 80_000 and not ctx.get("epoch")):
            return _EXCEL_EPOCH + timedelta(days=n), None
    if ctx.get("epoch") and _NUMERIC_ONLY_RE.match(v):
        return datetime.fromtimestamp(float(v), tz=timezone.utc).replace(tzinfo=None), None

    if allow_no_year:
        m = _PARTIAL_MD_RE.match(v)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            month, day = (b, a) if ctx.get("order") == "DMY" else (a, b)
            try:
                return datetime(_NO_YEAR, month, day), None
            except ValueError:
                return None, "not a real calendar date"
        for f in _PARTIAL_TEXT_FORMATS:
            try:
                d = datetime.strptime(f"{v} 2000", f"{f} %Y")  # explicit (leap) year: no ambiguity, Feb 29 ok
                return datetime(_NO_YEAR, d.month, d.day), None
            except ValueError:
                continue

    fmt = ctx.get("format")
    if fmt:
        try:
            return datetime.strptime(v, fmt), None
        except ValueError:
            pass
    order = ctx.get("order", "MDY")
    for f in ORDER_FORMATS[order] + DATE_FORMATS_YMD + DATE_FORMATS_TEXT:
        for t in TIME_SUFFIXES:
            try:
                return datetime.strptime(v, f + t), None
            except ValueError:
                continue
    if _NUMERIC_ONLY_RE.match(v):
        return None, "number is not a recognizable date"
    dt = _dateutil_parse(v, dayfirst=(order == "DMY"), yearfirst=(order == "YMD"))
    if dt is None:
        return None, "unrecognized or incomplete date"
    return dt, None


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
    no_year = dt.year == _NO_YEAR or (no_year_value is not None and dt.year == no_year_value)
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
    if _NUMERIC_ONLY_RE.match(v):
        return False
    if not re.search(r"\d", v):
        return False
    dt, err = parse_date(v, {"order": "MDY"}, allow_no_year=True)
    return err is None and dt is not None and (dt.year == _NO_YEAR or MIN_YEAR <= dt.year <= MAX_YEAR)
