"""Type normalizers: turn a raw extracted value into the exact output format, or report why it can't be.

Each target field type has one normalizer. Column-level context (e.g. whether a date column is
month-first or day-first) is inferred once per column in `prepare()` so ambiguous values are
resolved consistently instead of value by value.

What this module is responsible for
-----------------------------------
The per-value rules of the converter. Given one cell's text (already pulled out of the source file by a
transform, see transforms.py) and the target field's `FieldSpec` (schema.py, schemas/target_schema.yaml), it
returns the exact text the import file needs, or an error string that tells the person running the import, in
plain English, why the value couldn't be converted ("missing area code (only 7 digits)", "not a date", ...).
It never drops rows itself: the engine decides from the error and the field's `on_invalid` setting whether the
row is rejected or only that value is blanked.

Main entry points
-----------------
prepare(field, values, params) -> ctx
    Called by the engine once per output column with that column's cleaned values from non-empty rows (may
    include empty strings). Builds the column context: date order, Excel-serial detection and placeholder year for
    dates (`prepare_date`), and the area codes the file uses for phones (`prepare_phone`). Other types get an empty
    dict.
normalize(value, field, params, ctx) -> (output, error)
    Called by the engine for each distinct value (the engine caches results per value). `error is None` means
    success; ("", None) means blank with no problem. Dispatches on `field.type` through `NORMALIZERS`, except for
    Excel error codes ("#REF!", "#VALUE!"…), which fail with SHEET_ERROR_REASON for every type.
clean_text(value)
    Shared cleanup (control characters, whitespace, "N/A"/"null" tokens); also used by the engine and transforms.
sheet_error(value)
    The Excel error code a cell holds instead of a value, or None (used by normalize, checks.py and app.py).
looks_like_phone / looks_like_email / looks_like_zip / looks_like_state / looks_like_date
    Cheap yes/no detectors used by column profiling, auto-mapping and the wrong-column checks (mapping.py,
    checks.py).

How the engine uses it (engine.convert, simplified):
    cleaned = [N.clean_text(v) for v in raw]
    ctx = N.prepare(field, [v for v in cleaned if v], params)
    ctx["tz"] = timezone                       # the import's IANA zone, read by the date normalizer
    out, err = N.normalize(v, field, params, ctx)
    if v in ctx.get("repairs", {}): ...        # phone typo repairs are marked so a person can check them
Fields with `fixed_blank` never reach this module: the engine writes them blank on every row.

Phone rules (real target: 10 US digits, "(516) 555-0100" → "5165550100"), in the order they apply:
  1. Clean-up (_prep_phone): full-width digits and invisible characters (NFKC), the Excel ="…" wrapper and
     leading apostrophe, unicode dashes, "tel:"/"Phone:" prefixes, letters typed for digits ("516-555-O1OO").
  2. Country-code fixes (_prep_phone): exit codes before the 1 ("011 1 516…", "001 516…" → "+1 516…") and a "+"
     whose 1 was left out ("+5165550100" → "+1 5165550100") when that gives a real North American number.
  3. Spreadsheet damage (_fix_numeric_artifacts): "5165550100.0" and "5.165550100E+09" are read back as digits;
     scientific notation that already lost digits ("5.163E+09") is rejected.
  4. A "+1" put in front of a number that already had its own "+" ("+1 +51 63 171059") is ignored.
  5. Several numbers in one cell: one labelled mobile/cell wins, then unlabelled, then home/work, fax last;
     ties go to the one written first.
  6. Extensions ("ext 12", "x12", "#12") are dropped; vanity numbers are spelled out ("1-800-FLOWERS").
  7. Only the field's `countries` are accepted (US plus territories in the real schema). A valid number from
     another country is rejected with where it's from: "not a US number — Canada (area code 514, Quebec)".
  8. Typo repair (_repair_phone, US region only) when exactly one US number fits: a foreign country code in
     front of a US number, one digit too many, an area code that doesn't exist one keystroke from an area code the
     file uses, or a missing area code when 80%+ of the file shares one (the last two restored in 0.20.0; the
     last also for "+1 555-0177" since 0.21.2). A real area code is never changed. Every repair is recorded in
     ctx["repairs"] so it can be checked.
  9. Anything else is rejected with the most specific reason found (too few/many digits, area code not in
     use, exchange not in use, ...).
Before any of this, a "Phone + area code column" mapping (0.21.0) puts the row's area code in front of a number
without one (with_area_code: "555-0177" + "516" → "(516) 555-0177"; also "555-O177", "5550177.0", "+1 555-0177");
area_code_cell reads an area code cell ("516", "(516)", "1-516").

Date rules (the text parsing itself is dateparse.py; its docstring lists every supported format):
  - Ambiguous numeric dates are month-first (US) unless the column clearly is day-first or the mapping sets
    params["date_order"]; params["input_format"] (a strptime pattern) is tried first when given.
  - Columns of bare numbers in the Excel date range are read as Excel serial dates.
  - "0000-00-00"-style placeholders become blank, not errors.
  - Times that carry a zone (and Unix timestamps) are converted to the import's zone in ctx["tz"]; times with
    no zone are kept as written. Output uses the field's strftime format ("%m/%d/%Y %I:%M %p" for activity).
  - Birthday/anniversary (`no_year_value`, 1600 in the real schema): month+day only, a year of 1900 or earlier,
    a year most of the column shares (a placeholder) or a date in the last two years / the future is written
    with the no-year value: "Sep 12" → "09/12/1600", while "09/12/1985" → "09/12/1985".

Other types: email (lower-cased, syntax only), ZIP (leading zeros restored), US state (2-letter code), integer,
decimal, boolean, enum and string (re-casing and max length).
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache, wraps
from typing import Any

import phonenumbers
import unicodedata
from phonenumbers import geocoder as _geo
from dateutil import parser as du_parser
from email_validator import EmailNotValidError, validate_email
from nameparser import HumanName

from . import dateparse as DP
from .schema import FieldSpec

log = logging.getLogger(__name__)

# Length limits. No phone or date cell is anywhere near these; longer values are notes or other text, and some of
# the parsing steps get slow on very long text (a 30,000-character cell took seconds).
#   DETECT_MAX_LEN  values longer than this are never taken for a phone, date, email, ZIP or state when guessing
#                   what a column holds (looks_like_*; header detection). A cell with three labelled numbers is ~60.
#   PHONE_MAX_LEN   a phone cell longer than this is rejected as "too long to be a phone number" (~40 labelled
#                   numbers fit).
#   DATE_MAX_LEN    a date cell longer than this is rejected as "too long to be a date" (the longest real format,
#                   "Saturday, October 10, 2026 at 3:15:22 PM Eastern Daylight Time", is ~65).
DETECT_MAX_LEN = 200
PHONE_MAX_LEN = 1000
DATE_MAX_LEN = DP.MAX_LEN
# Reason for a value that hit an unexpected error while being converted (see normalize): only that value is lost.
UNREADABLE_REASON = "couldn't be read (unexpected error, logged for the developer)"

# ---------------------------------------------------------------------------
# Shared text cleanup
# ---------------------------------------------------------------------------

# Any run of whitespace (spaces, tabs, line breaks in a cell); collapsed to one space: "a \n  b" → "a b".
_WS_RE = re.compile(r"\s+")
# ASCII control characters except tab, line feed and carriage return (those are whitespace, handled by _WS_RE), plus
# DEL. They come from copy/paste and old exports, are invisible in Excel and can break the output CSV.
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Cell contents (compared lower-cased) that mean "no value" in exports from Excel, pandas, databases and CRMs.
# "never" covers activity columns ("Last order: Never"); "nat" is pandas' Not-a-Time; "-"/"--" are dashes for empty.
_NULL_TOKENS = {"", "null", "none", "nan", "n/a", "na", "#n/a", "-", "--", "undefined", "nil", "never", "nat"}


def clean_text(value: Any) -> str:
    """Basic cleanup applied to every cell before it is normalized; returns "" for anything that means "no value".

    Removes control characters, turns non-breaking spaces into normal spaces, collapses runs of whitespace and
    trims. A value that is only a null token (see _NULL_TOKENS) becomes "", so the engine treats it as blank
    (and, for a required field, as missing) instead of as an invalid value.
        None                   → ""
        "  John    Smith "     → "John Smith"
        "N/A", "null", "-"     → ""
        "Never"                → ""      (an activity date that never happened)
    """
    if value is None:
        return ""
    s = _CTRL_RE.sub("", str(value)).replace(" ", " ")
    s = _WS_RE.sub(" ", s).strip()
    return "" if s.lower() in _NULL_TOKENS else s


# Error codes a spreadsheet shows (and exports) instead of a formula's result when the formula is broken: Excel and
# Google Sheets "#VALUE!", "#REF!", "#DIV/0!", "#NAME?", "#NUM!", "#NULL!", newer Excel "#SPILL!", "#CALC!",
# "#FIELD!", "#BLOCKED!", "#CONNECT!", "#BUSY!", "#UNKNOWN!", "#PYTHON!", "#GETTING_DATA", Google Sheets "#ERROR!",
# LibreOffice "Err:502". "#N/A" isn't here: it is a null token (people also type it to mean "not available").
_SHEET_ERROR_RE = re.compile(
    r"#(?:VALUE!|REF!|DIV/0!|NAME\?|NUM!|NULL!|SPILL!|CALC!|FIELD!|BLOCKED!|CONNECT!|BUSY!|UNKNOWN!|PYTHON!"
    r"|GETTING_DATA|ERROR!)|Err:\d{3}", re.I)


# The reason given for such a cell, whatever the field type.
SHEET_ERROR_REASON = "Excel error (broken formula in the client's file)"
# A number labelled fax in a phone cell ("Fax: 516-555-0199"): never imported, a fax line can't receive texts. Skipped
# when the row has another number, the row's reason when it has none (engine._expand_rows).
FAX_REASON = "labelled as a fax number (a fax can't receive texts)"


def sheet_error(value: str) -> str | None:
    """The spreadsheet error code a cell holds instead of a value, or None.

    Such a cell holds no data: a formula in the client's file was broken when it was exported (e.g. a lookup that
    found nothing, or a reference to a deleted column). Returns the code as written, without surrounding spaces:
        "#VALUE!" → "#VALUE!"     " #div/0! " → "#div/0!"     "Err:502" → "Err:502"     "#5 Main St" → None
    """
    v = value.strip()
    return v if _SHEET_ERROR_RE.fullmatch(v) else None


def apply_case(value: str, case: str) -> str:
    """Re-case a cleaned string for the field's `case` setting: "none", "upper", "lower", "title" or "name".

    "title" and "name" only touch text typed in a single case (ALL CAPS or all lower), because mixed case such as
    "McDonald" or "DeAngelo" was most likely typed correctly and plain title-casing would break it.
    "name" uses nameparser, which knows surname rules plain str.title() gets wrong:
        "MCDONALD" → "McDonald", "O'BRIEN" → "O'Brien", "mary-jane van dyke" → "Mary-Jane van Dyke".
    Empty values and unknown `case` values are returned unchanged.
    """
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
            # force=True: always re-case (the check above already limits this to single-case text).
            n.capitalize(force=True)
            out = str(n).strip()
            # nameparser can return "" for text it can't split into name parts; fall back to plain title case.
            return out or value.title()
        return value
    return value


# ---------------------------------------------------------------------------
# Phone
# ---------------------------------------------------------------------------

# Excel scientific notation for a long number: optional sign, a mantissa with optional decimals, "E"/"e", an optional
# "+" and the exponent. Matches "5.165550100E+09", "5.165550100e9", "5E+09"; group 1 is the mantissa ("5.165550100").
_SCI_RE = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)[eE]\+?(\d+)\s*$")
# A whole number that picked up a ".0" because the column was read as floats: "5165550100.0" → group 1 "5165550100".
_FLOAT_ARTIFACT_RE = re.compile(r"^(\d+)\.0+$")


def _fix_numeric_artifacts(value: str) -> tuple[str, str | None]:
    """Undo the damage spreadsheets do to long numbers (phones, ZIPs). Returns (value, error or None).

    Excel and pandas often store phone numbers as numbers, so they arrive as floats or in scientific notation:
        "5165550100.0"     → ("5165550100", None)
        "5.165550100E+09"  → ("5165550100", None)      all 10 digits survived in the mantissa
        "5.163E+09"        → ("5.163E+09", "stored in scientific notation; digits were lost in Excel")
        "516-555-0100"     → ("516-555-0100", None)    anything else is returned unchanged
    When the mantissa has fewer than 10 significant digits the real number can't be recovered (Excel already
    rounded it to 5163000000), so it is rejected rather than importing a wrong number. "unreadable number" is
    returned if Decimal can't read a value the regex accepted (defensive). normalize_zip reuses this and ignores
    the error.
    """
    m = _FLOAT_ARTIFACT_RE.match(value)
    if m:
        return m.group(1), None
    m = _SCI_RE.match(value)
    if m:
        mantissa = m.group(1).lstrip("+-")
        # Digits Excel kept: "5.163" → 4 significant digits, far short of a 10-digit phone number.
        significant = len(mantissa.replace(".", "").lstrip("0"))
        if significant < 10:
            return value, "stored in scientific notation; digits were lost in Excel"
        try:
            return str(int(Decimal(value.strip()))), None
        except InvalidOperation:
            return value, "unreadable number"
    return value, None


# Values a phone field's `format` may take (see _format_phone). The real schema uses "digits10".
PHONE_FORMATS = {"E164", "digits10", "digits11", "national", "dashed"}


def _format_phone(num: phonenumbers.PhoneNumber, fmt: str) -> str:
    """Format a parsed number in one of PHONE_FORMATS; an empty or unknown format falls back to E.164.

    For +1 516-555-0100:
        "E164"     → "+15165550100"
        "digits10" → "5165550100"       national number only (the real import format)
        "digits11" → "15165550100"      country code + national number
        "national" → "(516) 555-0100"
        "dashed"   → "516-555-0100"     numbers outside North America get the national format instead
    """
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


# Letters typed (or OCR'd) in place of digits: capital or small O → 0, capital I and small l → 1.
_LOOKALIKE = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1"})
# A URI scheme or label at the very start followed by ":" or "=" (case-insensitive): "tel:+1516…", "sms:516…",
# "Phone: 516…", "cell=516…". Only the prefix is removed.
_PHONE_PREFIX_RE = re.compile(r"^\s*(tel|sms|phone|ph|mobile|cell|callto|whatsapp)\s*[:=]\s*", re.I)
# An extension at the END of the value: optional ";" or ",", then "ext", "ext.", "extension", "ex", "x", "#" or the
# RFC 3966 ";ext=", then 1–6 digits. Matches " ext. 12", " x12", " #12", ";ext=12", ", extension 9". Removed before
# counting digits so the extension isn't taken for extra digits of the number.
_EXT_RE = re.compile(r"[;,]?\s*(?:ext\.?|extension|ex\.?|x|#|;ext=)\s*\d{1,6}\s*$", re.I)
# Labels written just before a number in cells holding several ("H: 516-555-1212 C: 516-555-0100"). Each pattern must
# END the text before the number (only non-word characters such as ": " or " (" may follow) and is searched in the 14
# characters before it. Single letters must stand alone (\b), so the "c" at the end of "Mac" doesn't count.
# Mobile labels (best for a texting import): mobile, cell, cel, wireless, sms, text, M, C.
_LABEL_MOBILE_RE = re.compile(r"(mobile|cell|cel|wireless|sms|text|\bm\b|\bc\b)\W*$", re.I)
# Fax labels ("Fax:", "F:"): a fax line can't receive texts, so it is the last choice.
_LABEL_FAX_RE = re.compile(r"(fax|\bf\b)\W*$", re.I)
# Landline-type labels: home, work, office, business, land line/landline, main, H, W, O. Chosen after unlabelled.
_LABEL_OTHER_RE = re.compile(r"(home|work|office|business|land\s*line|main|\bh\b|\bw\b|\bo\b)\W*$", re.I)


def _fix_lookalikes(value: str) -> str:
    """Letters typed for digits inside a number: O/o → 0, I/l → 1. "516-555-O1OO" → "516-555-0100".

    Regex: runs of 4+ characters made only of digits, O/o/I/l, parentheses, dots and dashes ("516-555-O1OO",
    "5l6-555-0100"). A run is translated only if it holds at least 3 real digits, so runs that are mostly letters
    (initials such as "I.O.O.I") are left alone.
    """
    return re.sub(r"[\dOoIl().\-]{4,}",
                  lambda m: m.group(0).translate(_LOOKALIKE) if sum(c.isdigit() for c in m.group(0)) >= 3
                  else m.group(0), value)


def _prep_phone(value: str) -> str:
    """Clean a raw phone value into text the phonenumbers matcher can read. Never fails; returns a string.

    Each step fixes something seen in real exports:
      - NFKC turns full-width digits and dashes into ASCII ("５１６－５５５" → "516-555"); zero-width
        spaces and byte-order marks are removed.
      - Excel text protection is unwrapped: ="5165550100" and a leading apostrophe ('5165550100).
      - Unicode hyphens, en/em dashes and minus signs become "-".
      - "tel:", "sms:", "Phone:", "Cell:" ... prefixes are removed.
      - Letters typed for digits inside a number: "516-555-O1OO" → "516-555-0100".
      - International exit codes before the US country code: "011 1 516 555 0100" → "+1 516 555 0100",
        "001 516 555 0100" → "+1 516 555 0100".
      - A "+" with the 1 left out: "+5165550100" → "+1 5165550100", but only when the 10 digits are a valid
        North American number; "+51 1 4567890" stays a Peru (Lima) number.
    Labels, extensions and further numbers in the cell are left for the matcher / _digits_fallback.
    """
    v = unicodedata.normalize("NFKC", value).replace("\u200b", "").replace("\ufeff", "").strip()
    # Regex: the whole value inside double quotes, optionally preceded by "=" (Excel formula that keeps text).
    m = re.match(r'^=?"(.*)"$', v)  # Excel text-protection wrapper ="5165550100"
    if m:
        v = m.group(1)
    # A leading apostrophe is Excel's "store as text" marker: '5165550100.
    v = v.strip().lstrip("'").strip()
    # U+2010–U+2015 (hyphens, figure dash, en/em dash, horizontal bar), U+2212 minus sign, U+2043 hyphen bullet.
    v = re.sub(r"[\u2010-\u2015\u2212\u2043]", "-", v)  # unicode dashes
    v = _PHONE_PREFIX_RE.sub("", v)  # tel:, sms:, Phone:
    # Letters typed for digits inside a number (516-555-O1OO, 5l6…): O→0, I/l→1. Done before vanity
    # conversion, which would otherwise read O as 6 on a keypad.
    v = _fix_lookalikes(v)
    # International exit codes before the US country code: 011 1 …, 00 1 …, +011 1 …
    # Regex: optional "+", then "011" (the US exit code) or "00" (most other countries), optional separators, and only
    # when "1" plus a digit comes next (lookahead, not consumed), i.e. the US country code follows. The exit code is
    # replaced by "+": "011 1 516…" → "+1 516…". "0044 20…" is untouched (that isn't +1).
    v = re.sub(r"^\s*\+?\s*(?:011|00)[\s.\-]*(?=1\D?\d)", "+", v)
    # "+" followed by 10 digits that can't start a country code of their own → the "1" was left out.
    # Only when that gives a real North American number; otherwise it's a foreign number (+51 1 4567890 = Lima).
    # Regex: the whole value is "+" then a number starting with 2–9 (North American area codes never start with 0 or
    # 1) made only of digits, spaces, parentheses, dots and dashes. The walrus keeps its digits for the checks.
    m = re.match(r"^\s*\+\s*([2-9][\d\s().\-]*)$", v)
    if m and len(digits := re.sub(r"\D", "", m.group(1))) == 10 and phonenumbers.is_valid_number(
            phonenumbers.PhoneNumber(country_code=1, national_number=int(digits))):
        v = "+1 " + m.group(1)
    return v


def _digits_fallback(text: str) -> tuple[phonenumbers.PhoneNumber | None, str | None]:
    """Last resort for values the matcher can't find a number in (one number, odd separators, vanity letters).

    Returns (PhoneNumber, None) for exactly 10 digits (after dropping a leading 1), else (None, reason):
        "516/555/0100 cell" → +1 5165550100          words such as "cell" or "home" are dropped
        "1-800-FLOWERS"     → +1 8003569377          vanity letters converted on a phone keypad
        "555-0100"          → "missing area code (only 7 digits)"
        "51655501"          → "too few digits (8)"
        "516555010066"      → "too many digits (12)"
        "call me"           → "no phone number found"
    "not a phone number" if libphonenumber can't parse the 10 digits (defensive). The result is NOT validated
    here; the caller checks is_valid_number and the allowed countries.
    """
    body = _EXT_RE.sub("", text).strip()
    letters = re.sub(r"[^A-Za-z]", "", body)
    if letters:
        alnum = re.sub(r"[^0-9A-Za-z]", "", body)
        # A vanity number is only phone characters and letters, 10–11 characters without punctuation, with at least
        # 3 digits and 3 letters: "1-800-FLOWERS", "516-CALL-NOW". Any other value with letters has a label in it.
        is_vanity = (re.fullmatch(r"[\s+().\-\dA-Za-z]+", body) and 10 <= len(alnum) <= 11
                     and sum(c.isdigit() for c in alnum) >= 3 and len(letters) >= 3)
        if not is_vanity:
            body = re.sub(r"[A-Za-z]+", " ", body)  # drop words like "cell" or "home"
        else:
            body = phonenumbers.convert_alpha_characters_in_number(body)  # 1-800-FLOWERS
    digits = re.sub(r"\D", "", body)
    if not digits:
        return None, "no phone number found"
    # 11 digits starting with 1: the US country code written without "+" ("1 516 555 0100").
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


# "+1" followed by a second "+" and a number: a system put +1 in front of a number that already had a country code.
# Group 1 is the inner number: "+1 +51 63 171059" → "+51 63 171059"; "+1 +15165550100" → "+15165550100".
_DOUBLE_CC_RE = re.compile(r"^\s*\+\s*1[\s.\-]*(\+\s*\d.*)$")


# Cached: one call can test up to 800 exchanges through libphonenumber, and there are only 800 possible area codes
# (200–999), so maxsize=1000 keeps all of them.
@lru_cache(maxsize=1000)
def _area_code_in_use(npa: str) -> bool:
    """Whether any number in this North American area code can exist (US, territories or Canada…).

    Only used to word a rejection: "area code 433 isn't in use" vs "155 isn't a working exchange in area code
    516". `npa` is the 3-digit area code as text and must be 2–9 followed by two digits:
        "516" → True, "433" → False, "123" → False.
    """
    if not re.fullmatch(r"[2-9]\d\d", npa):
        return False
    # libphonenumber validates by area code + exchange, so try every exchange (200–999) with a dummy line number.
    for nxx in range(200, 1000):
        if phonenumbers.is_valid_number(phonenumbers.PhoneNumber(country_code=1, national_number=int(f"{npa}{nxx}4567"))):
            return True
    return False


def _foreign_reason(num: phonenumbers.PhoneNumber) -> str:
    """Rejection reason naming the country (and area) of a valid number that isn't in the allowed countries.

    'not a US number — Canada (area code 514, Quebec)' / 'not a US number — Peru (+51, Pasco)'.
    North American (+1) numbers show the area code, because "+1" alone doesn't say which country; others show
    the country code. The place is left out when the geocoder has none or it only repeats the country name.
    """
    country = _geo.country_name_for_number(num, "en") or phonenumbers.region_code_for_number(num) or "another country"
    place = _geo.description_for_number(num, "en")
    place = "" if place == country else place
    if num.country_code == 1:
        detail = f"area code {str(num.national_number)[:3]}" + (f", {place}" if place else "")
    else:
        detail = f"+{num.country_code}" + (f", {place}" if place else "")
    return f"not a US number — {country} ({detail})"


# Cached (200,000 entries): the same values repeat across rows, columns and the profiling/detection passes, and
# phonenumbers matching is the slowest step of a conversion. All arguments are hashable for this reason.
@lru_cache(maxsize=200_000)
def _parse_phone(value: str, region: str, countries: tuple[str, ...], fmt: str) -> tuple[str, str | None]:
    """Parse one phone value into the output format, without typo repair. Returns (formatted, None) or ("", why).

    `region` is the default country for numbers written without a country code ("US"); `countries` is the tuple
    of allowed ISO regions (empty = any); `fmt` is one of PHONE_FORMATS. Typo repair lives in _repair_phone, so
    this is also safe for detectors such as looks_like_phone. With fmt "digits10" and countries ("US", ...):
        "(516) 555-0100 ext 12"            → ("5165550100", None)
        "H: 516-555-1212 C: 516-555-0100"  → ("5165550100", None)   the cell (C:) number wins
        "+1 +51 63 171059"                 → ("5163171059", None)   extra +1 ignored
        "+44 20 7946 0958"                 → ("", "not a US number — United Kingdom (+44, London)")
        "(516) 155-0100"                   → ("", "not a real US number (155 isn't a working exchange in area
                                                code 516)")
    Error strings: "stored in scientific notation; digits were lost in Excel", "no phone number found",
    "missing area code (only 7 digits)", "too few digits (N)", "too many digits (N)",
    "not a US number — <country> (<area code or +code>, <place>)" (plus " — the file put +1 in front of its own
    country code" for the doubled form), "has two country codes (+1 and +NN) and isn't a valid number either
    way", "not a real US number (area code NPA isn't in use)", "not a real US number (NXX isn't a working
    exchange in area code NPA)", "not a real US number (area code or exchange doesn't exist)".
    """
    # "+1 +51 63 171059": a system put +1 in front of a number that already had a country code.
    m = _DOUBLE_CC_RE.match(unicodedata.normalize("NFKC", value))
    if m:
        # Parse the inner number on its own (cached too). If it is fine, the extra +1 was just noise. Several "+1"s in
        # a row are all dropped first, in a loop: calling this function again for each one could run out of depth.
        inner = m.group(1)
        while more := _DOUBLE_CC_RE.match(inner):
            inner = more.group(1)
        out, err = _parse_phone(inner, region, countries, fmt)
        if err is None:
            return out, None
        if err.startswith("not a US number"):
            return "", err + " — the file put +1 in front of its own country code"
        # The inner number failed for another reason: name its country code in the message if it parses at all.
        try:
            cc = phonenumbers.parse(inner, None).country_code
        except phonenumbers.NumberParseException:
            cc = None
        return "", ("has two country codes (+1 and " + (f"+{cc}" if cc else "another") + ") and isn't a valid "
                    "number either way")
    # Clean-up, then undo Excel float/scientific-notation damage (which may reject the value outright).
    prepped = _prep_phone(value)
    fixed, err = _fix_numeric_artifacts(prepped)
    if err:
        return "", err
    # Find every number in the text; prefer ones labelled mobile/cell, then unlabelled, then home/work, then fax.
    # Leniency.POSSIBLE finds anything shaped like a number (validity is checked below, so an invalid candidate can
    # still give a specific error). Each candidate gets a priority from the label in the 14 characters before it:
    # 0 mobile/cell, 1 no label, 2 home/work/main, 3 fax. Candidates are (priority, position, number).
    candidates: list[tuple[int, int, phonenumbers.PhoneNumber]] = []
    try:
        for match in phonenumbers.PhoneNumberMatcher(fixed, region, leniency=phonenumbers.Leniency.POSSIBLE):
            before = fixed[max(0, match.start - 14): match.start]
            prio = (0 if _LABEL_MOBILE_RE.search(before) else 3 if _LABEL_FAX_RE.search(before)
                    else 2 if _LABEL_OTHER_RE.search(before) else 1)
            candidates.append((prio, match.start, match.number))
    # The matcher can raise on unusual input; that counts as "nothing found" and the digits fallback takes over.
    except Exception:
        pass
    fallback_err = None
    if not candidates:
        num, fallback_err = _digits_fallback(fixed)
        if num is not None:
            candidates.append((1, 0, num))
    if not candidates:
        return "", fallback_err or "no phone number found"
    # Best label first; equal labels keep the order in which they were written.
    candidates.sort(key=lambda c: (c[0], c[1]))
    foreign = None
    # The first candidate that is a valid number in an allowed country wins. The first valid number from another
    # country is remembered so the rejection can name that country.
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
    # Nothing usable: explain why, from the digits of the whole value (extension removed).
    digits = re.sub(r"\D", "", _EXT_RE.sub("", fixed))
    if len(digits) == 7:
        return "", "missing area code (only 7 digits)"
    if len(digits) < 10:
        return "", f"too few digits ({len(digits)})"
    # Cells with several numbers legitimately have more than 11 digits, so "too many" only for a single candidate.
    if len(digits) > 11 and len(candidates) == 1:
        return "", f"too many digits ({len(digits)})"
    # Right length for a US number but invalid: say whether the area code or the exchange (next 3 digits) is wrong.
    if len(digits) == 10 or (len(digits) == 11 and digits[0] == "1"):
        npa, nxx = digits[-10:-7], digits[-7:-4]
        if not _area_code_in_use(npa):
            return "", f"not a real US number (area code {npa} isn't in use)"
        return "", f"not a real US number ({nxx} isn't a working exchange in area code {npa})"
    return "", "not a real US number (area code or exchange doesn't exist)"


def _phone_region(field: FieldSpec, params: dict) -> str:
    """Default country for numbers written without a country code: params["region"], else the field's first allowed
    country, else "US". Upper-case ISO code, e.g. "US"."""
    return (params.get("region") or (field.countries[0] if field.countries else "US")).upper()


def normalize_phone(value: str, field: FieldSpec, params: dict, ctx: dict) -> tuple[str, str | None]:
    """Normalizer for "phone" fields: parse, and for US imports try to repair likely typos.

    Region (default country for numbers without a country code): params["region"], else the field's first allowed
    country, else "US". Output format: field.format (default "E164"; the real schema uses "digits10").
    Successful repairs (and an ignored extra "+1") are recorded as ctx["repairs"][raw value] = note; the engine
    marks those cells as repaired so a person can check them. A failed repair adds its reason to the error.
    Examples:
        "51655501444"       → ("5165550144", None), repairs note "one digit too many (a digit typed twice)"
        "5149689696"        → ("", "not a US number — Canada (area code 514, Quebec)")   a real area code is
                                                                                         never changed
        "(156) 555-0199"    → ("5165550199", None) in a file of 516 numbers: 156 doesn't exist (see _repair_phone)
        "555-0177"          → ("5165550177", None) when 80%+ of the file's numbers are 516
    The repair evidence (phone_local, phone_dominant, phone_known) comes from prepare_phone through ctx.
    """
    region = _phone_region(field, params)
    countries = tuple(c.upper() for c in field.countries)
    fmt = field.format or "E164"
    out, err = _parse_phone(value, region, countries, fmt)
    if err is None:
        # It only parsed after dropping a doubled "+1": report that like a repair so it gets checked.
        if _DOUBLE_CC_RE.match(unicodedata.normalize("NFKC", value)):
            ctx.setdefault("repairs", {})[value] = "ignored an extra +1 in front of a number that already had a +"
        return out, err
    # Repairs assume North American numbering (area codes, 10 digits), so other regions just get the error.
    if region != "US":
        return out, err
    # Only US numbers are expected, so a number that fails is most likely a typo: try to repair it.
    local = ctx.get("phone_local", ())
    fixed, note = _repair_phone(value, countries, fmt, local, ctx.get("phone_known", frozenset()),
                                ctx.get("phone_dominant"))
    if fixed:
        ctx.setdefault("repairs", {})[value] = note
        return fixed, None
    return "", err + (f" — {note}" if note else "")


def prepare_phone(field: FieldSpec, values: list[str]) -> dict:
    """Evidence from this file's valid numbers, used by the phone repairs that are allowed (see _repair_phone).

    phone_local: area codes used by at least 5% of the valid numbers (and at least twice): whether
        "+44 516 555 0100"-style values may be read as US numbers, and which area code an area code that doesn't
        exist was probably meant to be (156 in a 516 file).
    phone_dominant: the one area code used by 80%+ of the valid numbers (and at least 5 of them), or None. A 7-digit
        number gets it (restored in 0.20.0; removed in 0.9.0).
    phone_known: every valid 10-digit number in the column (breaks ties when a repair has several possible
        fixes).

    Called by prepare() once per phone column with the column's cleaned values from non-empty rows (empty strings
    are skipped). Example: a Long Island file with 5 × 516, 2 × 631 and 1 × 917 numbers → phone_local
    ("516", "631") and phone_dominant None (516 is only 62%). Only a quick digit check is used (no full parsing), so
    cells holding several numbers don't count as evidence. Example call result:
        {"phone_local": ("516", "631"), "phone_dominant": None, "phone_known": frozenset({"5165550100", ...})}
    """
    counts: dict[str, int] = {}
    known: set[str] = set()
    total = 0
    # Distinct values only (first-seen order), capped at 20,000 so huge files stay fast; that's plenty of evidence.
    for v in list(dict.fromkeys(v for v in values if v))[:20_000]:
        d = re.sub(r"\D", "", _EXT_RE.sub("", v))
        if len(d) == 11 and d[0] == "1":
            d = d[1:]
        if len(d) != 10 or not _us_number(d, tuple(c.upper() for c in field.countries)):
            continue
        counts[d[:3]] = counts.get(d[:3], 0) + 1
        known.add(d)
        total += 1
    # Most-used first, so the commonest area code is tried first and messages list it first.
    local = tuple(sorted((c for c, n in counts.items() if n >= 2 and n >= 0.05 * total), key=lambda c: -counts[c]))
    dominant = next((c for c, n in counts.items() if n >= 5 and n >= 0.8 * total), None)
    return {"phone_local": local, "phone_dominant": dominant, "phone_known": frozenset(known)}


# Cached (100,000 entries): typo repair calls this for every candidate fix of every failing value, and
# prepare_phone for every number in the column.
@lru_cache(maxsize=100_000)
def _us_number(d10: str, countries: tuple[str, ...]) -> bool:
    """Whether 10 digits are a valid North American number in one of `countries` (an empty tuple allows any).

    "5165550100" → True; "5555555555" → False (555 isn't an area code); "4165550123" → False when countries is
    ("US", ...) because 416 is Toronto.
    """
    if not re.fullmatch(r"\d{10}", d10):
        return False
    num = phonenumbers.PhoneNumber(country_code=1, national_number=int(d10))
    if not phonenumbers.is_valid_number(num):
        return False
    return not countries or phonenumbers.region_code_for_number(num) in countries


# A cell of an "Area Code" column: three digits, the first 2–9 (North American area codes), optionally in
# parentheses or with a ".0" a spreadsheet added to a number: "516", "(516)", "516.0"; also with the country code 1
# in front: "1-516", "+1 516", "+1 (516)", "1516", "(1) 516".
_AREA_CODE_CELL_RE = re.compile(r"(?:\+?\s*\(?\s*1\s*\)?[\s.\-]*)?\(?\s*([2-9]\d{2})\s*\)?(?:\.0+)?")
# The country code 1 written in front of a 7-digit number: "+1 555-0177", "+15550177", "1-555-0177", "1 555 0177".
# A bare 1 needs a separator after it ("15550177" is left alone: 8 digits run together may be a typo).
_CC_BEFORE_LOCAL_RE = re.compile(r"(?:\+\s*1|(?<![\d+])1(?=[\s.\-]))[\s.\-]*(?=\d{3}[\s.\-]?\d{4}(?!\d))")
# The "(516) " with_area_code puts in front of the number (the side-by-side view finds the file's cell without it).
ADDED_AREA_CODE_RE = re.compile(r"\(\d{3}\) (?=\d)")


def area_code_cell(value: str) -> str:
    """The area code in a cell of an Area Code column, or "" when the cell isn't one.

    "516" → "516", "(516)" → "516", "516.0" → "516", "1-516" → "516", "+1 (516)" → "516", "" → "", "11530" → "",
    "Nassau" → "", "123" → "".
    """
    m = _AREA_CODE_CELL_RE.fullmatch(value.strip())
    return m.group(1) if m else ""


def _local_form(value: str) -> str | None:
    """The value with letters typed for digits fixed, a spreadsheet ".0" removed and the country code 1 in front of
    the number dropped, when it then holds exactly 7 digits (an extension not counted): a number without its area
    code. None otherwise.
        "555-0177" → "555-0177", "555-O177" → "555-0177", "5550177.0" → "5550177", "+1 555-0177" → "555-0177",
        "1-555-0177" → "555-0177", "555-0177 x12" → "555-0177 x12", "516-555-0177" → None, "15550177" → None,
        "" → None
    """
    v = _fix_lookalikes(_FLOAT_ARTIFACT_RE.sub(r"\1", value.strip()))
    n = sum(c.isdigit() for c in _EXT_RE.sub("", v))
    if n == 8:  # maybe the country code 1 in front of the 7 digits
        v = _CC_BEFORE_LOCAL_RE.sub("", v, count=1)
        n = sum(c.isdigit() for c in _EXT_RE.sub("", v))
    return v if n == 7 else None


def is_local_number(value: str) -> bool:
    """Whether a phone value is a number without its area code (7 digits; see _local_form): "555-0177" → True."""
    return bool(value) and _local_form(value) is not None


def with_area_code(phone: str, area: str) -> str:
    """A 7-digit phone value with the area code from the row's Area Code column put in front of the number.

    Only when the value is a number without its area code (is_local_number: 7 digits, an extension not counted,
    also with letters typed for digits, a spreadsheet ".0" or the country code in front: "+1 555-0177") and `area`
    is an area code (see area_code_cell);
    anything else is returned unchanged, so full numbers in the same column stay as written:
        ("555-0177", "516")        → "(516) 555-0177"
        ("Cell: 555-0177", "516")  → "Cell: (516) 555-0177"
        ("555-O177", "516")        → "(516) 555-0177"
        ("5550177.0", "516")       → "(516) 5550177"
        ("+1 555-0177", "1-516")   → "(516) 555-0177"
        ("516-555-0177", "631")    → "516-555-0177"       already has an area code
        ("555-0177", "")           → "555-0177"           no area code in the row
    Used by the "Phone + area code column" transform (engine._expand_rows, transforms.apply_transform).
    """
    code = area_code_cell(area) if area else ""
    local = _local_form(phone) if code and phone else None
    if local is None:
        return phone
    # In front of the first digit, so any spacing inside the number ("555 - 0177") is kept.
    return re.sub(r"(?=\d)", f"({code}) ", local, count=1)


def split_area_code(values: list[str], at: int | None = None) -> tuple[str, list[int]]:
    """For one row of a "Phone + area code column" mapping: (the row's area code, positions of the phone cells).

    Used by transforms.apply_transform, which sees one row at a time; the engine instead pairs each phone column
    with its own area code column once for the whole file (mapping.area_code_pairs). `at` is the position of the
    area code column; every other cell is a phone cell. A cell of the area code column that isn't an area code
    ("", "N/A", "000") isn't a phone number either, unless it holds a whole one (7+ digits typed into the wrong
    column). With `at` None the area code is the first cell that is one, whichever column it is in, so the order
    of the columns doesn't matter:
        ["516", "555-0177"] → ("516", [1]);  ["555-0177", "516"] → ("516", [0]);  ["", "555-0177"] → ("", [0, 1])
        ["N/A", "555-0177"], at=0 → ("", [1]);  ["516-555-0177", ""], at=0 → ("", [0, 1])
    """
    if at is None:
        at = next((i for i, v in enumerate(values) if area_code_cell(v)), None)
        if at is None:
            return "", list(range(len(values)))
    area = area_code_cell(values[at])
    whole = not area and sum(c.isdigit() for c in values[at]) >= 7
    return area, [i for i in range(len(values)) if i != at or whole]


def _fmt_us(d10: str) -> str:
    """Human-readable US format for messages: "5165550100" → "(516) 555-0100" (used in "could be ... or ...")."""
    return f"({d10[:3]}) {d10[3:6]}-{d10[6:]}"


def _one_digit_off(a: str, b: str) -> bool:
    """Whether two area codes differ by one digit or by two neighbouring digits swapped.

    ("156", "516") → True (swap), ("517", "516") → True (one digit), ("212", "516") → False. Same length only.
    """
    diff = [i for i in range(len(a)) if a[i] != b[i]]
    return len(diff) == 1 or (len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]]
                              and a[diff[1]] == b[diff[0]])


def _repair_phone(value: str, countries: tuple[str, ...], fmt: str, local: tuple[str, ...],
                  known: frozenset = frozenset(), dominant: str | None = None) -> tuple[str, str]:
    """Best-effort repair of a number that isn't a valid US number. Returns (formatted, note) or ("", why not).

    Repairs are only made when exactly one US number fits; each one is reported so a person can check it.
    Approved repairs (each decided one by one by the business; see docs/CHANGELOG.md, 0.9.0 and 0.20.0):
    1. A country code (other than +1) in front of a valid US number: +44 516 555 0100 → 516-555-0100. Not done
       when the value is a real number of that country and the US area code isn't one this file uses.
    2. One digit too many: a digit typed twice, an extra digit at the end or at the start.
    3. An area code that doesn't exist anywhere in North America (_area_code_in_use: 156, 433) and is one digit
       off, or two neighbouring digits swapped, from an area code this file uses (`local`): 156 → 516. A real
       area code is never changed (514 is Quebec, even in a file of 516 numbers).
    4. 7 digits (no area code; also after a country code, +1 555-0177) when one area code makes up 80%+ of the file
       (`dominant`): 555-0177 → 516-555-0177.
    When several numbers fit, the one that already appears elsewhere in the file wins; otherwise nothing is guessed.
    Rules 3 and 4 were removed in 0.9.0 and restored in 0.20.0 (rule 3 only for area codes that don't exist).

    Each rule only runs when the ones before it found nothing. Why repair at all: the import is US-only, so a
    number that fails is far more often a typo than a real foreign number. Why so careful: a wrong guess would text
    a stranger, hence "exactly one fit", area codes only from the file itself, and a note on every repair.
    `local`, `known` and `dominant` are prepare_phone's phone_local, phone_known and phone_dominant.
    Examples in a file of 516/631 numbers (fmt "digits10"):
        "+44 516 555 0188" → ("5165550188", "removed country code +44 in front of a US number")
        "51655501444"      → ("5165550144", "one digit too many (a digit typed twice); the same number appears
                              elsewhere in this file")             when 516-555-0144 is in the file
        "5149689696"       → ("", "")                               514 exists (Quebec): not changed
        "(156) 555-0199"   → ("5165550199", "area code 156 doesn't exist; read as 516, which this file uses")
    Failures return ("", reason): "could be (516) 946-1961 or (516) 946-1966, so it wasn't guessed", "not a likely
    typo of this file's area codes (516, 631)" for an area code that doesn't exist, or "" when no rule applied (the
    caller then reports the parser's own reason).
    """
    # A ".0" a spreadsheet added ("5550177.0") isn't a digit of the number.
    body = _EXT_RE.sub("", _prep_phone(_FLOAT_ARTIFACT_RE.sub(r"\1", value.strip())))
    digits = re.sub(r"\D", "", body)
    # Digit sequences to try: as typed, and without a leading 1 (US country code) when there are 11+ digits.
    seqs = [digits] + ([digits[1:]] if digits[:1] == "1" and len(digits) >= 11 else [])
    found: dict[str, str] = {}

    def add(d10: str, note: str) -> None:
        """Record d10 as a candidate fix (the first note for a number wins) if it's a valid allowed number."""
        if _us_number(d10, countries) and d10 not in found:
            found[d10] = note

    # 1. Foreign country code in front of a US number.
    # Only when the value has a "+": strip a 1–3 digit country code (never "1") that leaves exactly 10 digits. If the
    # value is itself a valid foreign number (+44 20 7946 0958 is London) it is only repaired when the remaining
    # area code is one this file uses; otherwise it is more likely a real foreign number than a typo.
    if "+" in body:
        double = _DOUBLE_CC_RE.match(body)
        try:
            real_foreign = phonenumbers.is_valid_number(phonenumbers.parse(double.group(1) if double else body, None))
        except phonenumbers.NumberParseException:
            real_foreign = False
        for seq in seqs:
            for k in (1, 2, 3):
                if len(seq) - k == 10 and seq[:k] != "1" and (not real_foreign or seq[k:k + 3] in local):
                    add(seq[k:], f"removed country code +{seq[:k]} in front of a US number")
    # 2. One digit too many.
    # For 11 digits, try removing each digit that equals a neighbour (typed twice), plus the first and the last digit.
    # Only digits that were typed are kept: nothing is added or changed. Every valid result is a candidate; the
    # checks below decide whether one of them is safe to use.
    if not found:
        for seq in seqs:
            if len(seq) != 11:
                continue
            for i in range(11):
                doubled = (i > 0 and seq[i] == seq[i - 1]) or (i < 10 and seq[i] == seq[i + 1])
                if doubled or i in (0, 10):
                    why = ("a digit typed twice" if doubled else "an extra digit at the end" if i == 10
                           else "an extra digit at the start")
                    add(seq[:i] + seq[i + 1:], f"one digit too many ({why})")
    # 3. An area code that doesn't exist, one keystroke from an area code this file uses. Only the file's own area
    # codes are tried (against every area code, almost any typo would "match" one), and a real area code is never
    # changed: it may be a real number elsewhere.
    fake = [seq for seq in seqs if len(seq) == 10 and not _area_code_in_use(seq[:3])]
    if not found and local:
        for seq in fake:
            for code in local:
                if _one_digit_off(seq[:3], code):
                    add(code + seq[3:], f"area code {seq[:3]} doesn't exist; read as {code}, which this file uses")
    # 4. Missing area code: only when one area code covers 80%+ of the file; with a mix there is no safe guess.
    # "+1 555-0177" counts too: the country code is there, the area code isn't.
    seven = digits if len(digits) == 7 else re.sub(r"\D", "", _EXT_RE.sub("", _local_form(value) or ""))
    if not found and dominant and len(seven) == 7:
        add(dominant + seven, f"no area code; used {dominant}, the area code of most numbers in this file")
    # Several fits: prefer the single one that already appears elsewhere in the file (likely the same person).
    if len(found) > 1:
        seen = [d for d in found if d in known]
        if len(seen) == 1:
            found = {seen[0]: found[seen[0]] + "; the same number appears elsewhere in this file"}
    if len(found) == 1:
        d10, note = next(iter(found.items()))
        return _format_phone(phonenumbers.PhoneNumber(country_code=1, national_number=int(d10)), fmt), note
    if len(found) > 1:
        options = " or ".join(_fmt_us(d) for d in list(found)[:3])
        return "", f"could be {options}, so it wasn't guessed"
    if fake and local:
        return "", f"not a likely typo of this file's area codes ({', '.join(local[:4])})"
    return "", ""


# ---------------------------------------------------------------------------
# Several phone numbers in one cell
# ---------------------------------------------------------------------------

# What people type BETWEEN two numbers in one cell: comma, semicolon, slash, backslash, pipe, ampersand, line break,
# tab, or the word "or"/"and". Only searched in the text between two numbers already found, never inside a number,
# so "516/555/0100" is never split.
_PHONE_SEP_RE = re.compile(r"[,;/\\|&\n\t]|\b(?:or|and)\b", re.I)
# An extension written right after a number ("516-555-0100 x12 917-555-1234"): it belongs to the number before it.
# Used with .match(text, pos, endpos), which anchors at pos (so no "^").
_EXT_AFTER_RE = re.compile(r"\s*[;,]?\s*(?:ext\.?|extension|ex\.?|x|#)\s*\d{1,6}\b", re.I)
# Any word that says what kind of number it is (mobile, home, fax, a single-letter M/C/F/H/W/O...). Only used to
# tell whether a cell writes its labels AFTER the numbers ("516-555-0100 (cell) 516-555-0199 (fax)") rather than
# before them ("C: 516-555-0100 F: 516-555-0199").
_ANY_LABEL_RE = re.compile(r"\b(?:mobile|cell|cel|wireless|sms|text|fax|home|work|office|business|land\s*line|main|"
                           r"[mcfhwo])\b", re.I)
# Characters trimmed from both ends of each piece: spaces and the separators themselves. Parentheses are kept
# because "(516) 555-0100" starts with one and "(cell)" ends with one.
_PIECE_TRIM = " \t\n,;/\\|&-"
# A North American number written the usual ways: optional country code (+1 / 1), area code with or without
# parentheses, exchange and line, separated by nothing or by up to 3 spaces, dots or dashes. Area code and exchange
# start with 2–9, as all North American ones do. A match never starts or ends inside a run of digits (look-behind /
# look-ahead), so "51655501005165551212" holds no match and "516-555-0100/0101" holds one. A "(" may follow a digit
# directly ("(516)555-0100(917)555-1234"). Matches "516.555.0100", "+1 631 555 0199", "1-718-555-0142",
# "(516) 555-0100", "5165550100".
_NANP_RE = re.compile(r"(?:(?<![\d+])(?:\+\s*)?1[\s.\-]{0,3}|(?<![\d+])|(?=\())"
                      r"(?:\(\s*[2-9]\d\d\s*\)|[2-9]\d\d)[\s.\-]{0,3}[2-9]\d\d[\s.\-]{0,3}\d{4}(?!\d)")


def _is_fax_label(text: str) -> bool:
    """Whether a phone value is labelled as a fax line: the word "fax" or "facsimile", or a lone "F".

    Only the words of the text count, so the digits of the number don't matter:
        "Fax: 516-555-0199" → True, "516-555-0199 (F)" → True, "516-555-0199" → False,
        "Cell 516-555-0100" → False, "1-800-FAXNOW" → False (one word, not "fax").
    """
    words = [w.lower() for w in re.findall(r"[A-Za-z]+", text)]
    return any(w in ("fax", "facsimile") for w in words) or words == ["f"]


def _digit_group_spans(text: str) -> list[tuple[int, int]]:
    """Positions of the numbers in a cell like "516 555 0100 516 555 1212" (numbers separated by spaces only).

    The runs of digits are grouped, left to right, into numbers of exactly 10 digits (or 11 starting with 1). The
    positions are returned only when there is exactly ONE way to do that and it gives at least two numbers; a run of
    digits is never cut, so "51655501005165551212" stays one value (and is rejected as "too many digits"). Returns
    [] otherwise. Only called when the phonenumbers matcher found fewer than two numbers.
        "516 555 0100 516 555 1212"     → [(0, 12), (13, 25)]
        "1 516 555 0100 1 917 555 1234" → [(0, 14), (15, 29)]
        "516 555 0100 x 12"             → []      (12 digits can't be split into whole numbers)
    """
    groups = [(m.start(), m.end(), len(m.group()), m.group()[0]) for m in re.finditer(r"\d+", text)]
    # A cell with this many separate digit runs isn't a short list of numbers; don't search it.
    if len(groups) > 12:
        return []
    ways: list[list[tuple[int, int]]] = []

    def walk(i: int, acc: list[tuple[int, int]]) -> None:
        """Depth-first search for groupings of digit runs i.. into whole numbers; stops after finding two."""
        if len(ways) > 1:
            return
        if i == len(groups):
            ways.append(list(acc))
            return
        total = 0
        for j in range(i, len(groups)):
            total += groups[j][2]
            if total == 10 or (total == 11 and groups[i][3] == "1"):
                acc.append((groups[i][0], groups[j][1]))
                walk(j + 1, acc)
                acc.pop()
            if total >= 11:
                break

    walk(0, [])
    return ways[0] if len(ways) == 1 and len(ways[0]) >= 2 else []


def _separator_pieces(text: str) -> list[str]:
    """Split a cell at its separators (see _PHONE_SEP_RE) when every part holds a whole number's worth of digits.

    Each part with digits must have at least 7 (an extension at its end not counted), so a separator inside a
    number ("516/555/0100") or before a short fragment ("516-555-0100/0101", ", ext 12") prevents the split. Parts
    without digits (a stray "/" or a lone label) are dropped. Returns the parts, or [] when fewer than two qualify.
    Unlike the phonenumbers matcher, this also finds numbers that aren't valid ("51655501444 / 917-555-1234"), so they
    are reported instead of silently dropped.
        "51655501444 / 917-555-1234" → ["51655501444", "917-555-1234"]
        "516/555/0100"               → []
    """
    parts = [p.strip(_PIECE_TRIM) for p in _PHONE_SEP_RE.split(text)]
    parts = [p for p in parts if any(c.isdigit() for c in p)]
    if len(parts) < 2 or any(sum(c.isdigit() for c in _EXT_RE.sub("", p)) < 7 for p in parts):
        return []
    return parts


def split_phones(value: str, field: FieldSpec, params: dict) -> list[tuple[str, bool]]:
    """Split a cell holding several phone numbers into one piece per number: [(piece, is_fax), ...].

    Used by the engine for the field that gets one row per phone number (engine._expand_rows): each piece is then
    converted on its own, exactly like a value from a separate phone column, so every different valid number gets
    its own row. A cell with one number comes back unchanged as a single piece.

    How the numbers are told apart (nothing is guessed: no digit is added, removed or moved):
      1. A cell with fewer than 14 digits can't hold two numbers (7 + 7) and is never split.
      2. The phonenumbers matcher finds the numbers in the text ("516-555-0100 / 917-555-1234",
         "H: 516-555-1212 C: 516-555-0100", "(516)555-0100(917)555-1234"). Matches with fewer than 7 digits don't
         count as numbers.
      3. Splitting at the separators (see _separator_pieces) wins when it gives at least as many numbers: it also
         catches numbers the matcher doesn't recognise because they are invalid ("51655501444 / 917-555-1234"),
         so they are reported as skipped instead of disappearing.
      4. If neither finds two numbers, numbers separated only by spaces are found from the digit groups
         ("516 555 0100 516 555 1212"; see _digit_group_spans).
      5. Finally, numbers written in the usual North American shapes are looked for directly (see _NANP_RE). They
         replace the result of 2–4 only when they give every valid number that result gave plus at least one more:
         the matcher misses some ("516 555 0100 x12 917-555-1234", "1. 516-555-0100 2. 917-555-1234",
         "+1 631 555 0199 1-718-555-0142 1-201-555-0111") and joins others wrongly ("516.555.0100 917.555.1234"),
         and before 0.16.0 those cells silently lost a number (or both).
    The text around the numbers is shared out so each piece keeps its own label:
      - an extension right after a number stays with it ("516-555-0100 x12, 917-555-1234");
      - between two numbers, a separator (, ; / | & "or" "and" line break) splits the text: what is before it belongs
        to the first number, what is after it to the second;
      - with no separator, labels written after the numbers ("516-555-0100 (cell) 516-555-0199 (fax)": the cell
        ends with a label) go to the number before them; otherwise labels go to the number after them.
    is_fax is True for a piece labelled fax ("Fax: 516-555-0199"); the engine imports such a number only when the
    file row has no other valid number, because a fax line can't receive texts.

    Examples (field = the real schema's phone_number):
        "516-555-0100"                          → [("516-555-0100", False)]
        "516-555-0100 / 917-555-1234"           → [("516-555-0100", False), ("917-555-1234", False)]
        "H: 516-555-1212 C: 516-555-0100"       → [("H: 516-555-1212", False), ("C: 516-555-0100", False)]
        "516-555-0100 (cell), 516-555-0199 fax" → [("516-555-0100 (cell)", False), ("516-555-0199 fax", True)]
        "516-555-0100/0101"                     → [("516-555-0100/0101", False)]   the shorthand isn't expanded
        "51655501005165551212"                  → [("51655501005165551212", False)]   digits are never cut
    Pieces are taken from the cleaned-up text (see _prep_phone), so letters typed for digits are already fixed.
    A cell over PHONE_MAX_LEN characters isn't split (normalize then rejects it as too long), and an unexpected
    error while splitting leaves the cell whole, so it is converted - or rejected with a reason - as one value.
    """
    if len(value) > PHONE_MAX_LEN:
        return [(value, False)]
    try:
        return _split_phones(value, field, params)
    except Exception:  # noqa: BLE001 - one odd cell must not stop the import
        log.exception("Unexpected error splitting a phone cell")
        return [(value, False)]


def _split_phones(value: str, field: FieldSpec, params: dict) -> list[tuple[str, bool]]:
    """The work of :func:`split_phones` (same arguments and result)."""
    if sum(c.isdigit() for c in value) < 14:
        return [(value, _is_fax_label(value))]
    text, err = _fix_numeric_artifacts(_prep_phone(value))
    if err:
        return [(value, _is_fax_label(value))]
    region = _phone_region(field, params)
    spans: list[list[int]] = []
    try:
        for m in phonenumbers.PhoneNumberMatcher(text, region, leniency=phonenumbers.Leniency.POSSIBLE):
            if sum(c.isdigit() for c in m.raw_string) >= 7:
                spans.append([m.start, m.end])
    # The matcher can raise on unusual input; that counts as "no numbers found" and the digit groups are tried.
    except Exception:
        spans = []
    parts = _separator_pieces(text)
    if len(parts) >= max(2, len(spans)):
        pieces = parts
    else:
        if len(spans) < 2:
            spans = [list(s) for s in _digit_group_spans(text)]
        pieces = _span_pieces(text, spans) if len(spans) >= 2 else [value]
    # Step 5: the usual North American shapes, used only when they find every valid number above and more.
    nanp = [[m.start(), m.end()] for m in _NANP_RE.finditer(text)]
    if len(nanp) >= 2:
        countries, fmt = tuple(c.upper() for c in field.countries), field.format or "E164"

        def numbers(ps: list[str]) -> set[str]:
            return {out for out, err in (_parse_phone(p, region, countries, fmt) for p in ps) if err is None}

        candidate = _span_pieces(text, nanp)
        if numbers(candidate) > numbers(pieces):
            pieces = candidate
    return [(p, _is_fax_label(p)) for p in pieces]


def _span_pieces(text: str, spans: list[list[int]]) -> list[str]:
    """Cut `text` into one piece per number, given where the numbers are ([start, end] each, in order, at least two).

    The text between numbers is shared out so each piece keeps its own label and extension (see split_phones):
        "516-555-0100 x12 917-555-1234"   → ["516-555-0100 x12", "917-555-1234"]
        "H: 516-555-1212 C: 516-555-0100" → ["H: 516-555-1212", "C: 516-555-0100"]
    `spans` is changed in place (an extension is added to the number before it). Empty pieces are left out.
    """
    # An extension right after a number belongs to that number.
    for k in range(len(spans) - 1):
        ext = _EXT_AFTER_RE.match(text, spans[k][1], spans[k + 1][0])
        if ext:
            spans[k][1] = ext.end()
    # Labels after the numbers? The text after the last number tells.
    labels_after = bool(_ANY_LABEL_RE.search(text[spans[-1][1]:]))
    starts = [0] + [0] * (len(spans) - 1)
    ends = [0] * (len(spans) - 1) + [len(text)]
    for k in range(len(spans) - 1):
        gap_start, gap_end = spans[k][1], spans[k + 1][0]
        sep = _PHONE_SEP_RE.search(text, gap_start, gap_end)
        if sep:
            ends[k], starts[k + 1] = sep.start(), sep.end()
        elif labels_after:
            ends[k] = starts[k + 1] = gap_end
        else:
            ends[k] = starts[k + 1] = gap_start
    pieces = [text[starts[k]:ends[k]].strip(_PIECE_TRIM) for k in range(len(spans))]
    return [p for p in pieces if p]


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

# Accepted values of params["date_order"]; anything else ("auto", the default) means: decide from the column.
DATE_ORDERS = {"MDY", "DMY", "YMD"}
# All-zero "no date" placeholders from databases and exports: zeros separated by "-", "/" or ".", optionally with an
# all-zero time after a space or "T". Matches "0000-00-00", "00/00/0000", "0000-00-00 00:00:00",
# "0000-00-00T00:00:00.000".
_ZERO_DATE_RE = re.compile(r"^0{1,4}[-/.]0{1,2}[-/.]0{1,4}(\s*[t ]\s*0{1,2}:0{2}(:0{2}(\.0+)?)?)?$", re.I)
# A bare number, optionally with decimals: "46304", "46304.65625" (Excel serial dates), "20261009".
_NUMERIC_ONLY_RE = re.compile(r"^\d+(\.\d+)?$")
# dateparse's marker year (4) for "month and day only" values; never output, replaced by the field's no_year_value.
_NO_YEAR = DP.NO_YEAR
# Years accepted in output (1900–2100); others are reported as "year N is out of range".
MIN_YEAR, MAX_YEAR = DP.MIN_YEAR, DP.MAX_YEAR


def prepare_date(field: FieldSpec, values: list[str], params: dict) -> dict:
    """Decide once per column: month-first or day-first, and whether bare numbers are Excel serial dates.

    `values` are the column's cleaned values from non-empty rows (may include empty strings); `params` are the
    mapping's params for the field.
    Returns the ctx dict later passed to normalize_date / parse_date:
        order             "MDY" (default, US), "DMY" or "YMD"; params["date_order"] wins when given
        format            params["input_format"] (a strptime pattern tried before the flexible parser) or None
        serial            True when the column is (almost) all Excel serial numbers
        evidence          vote counts {"month_first": n, "day_first": n} (informational)
        placeholder_year  birthday/anniversary fields only: a year that stands for "unknown" (_placeholder_year)
        calendar          True for date-only fields: zones written with the values are ignored (see parse_date)
    The engine adds ctx["tz"] (the import's time zone) afterwards.

    Why per column: "01/04/2026" alone could be January 4 or April 1. A column that also holds "15/03/2026" and
    "28/02/2026" is clearly day-first, so the whole column is read that way; deciding value by value would mix
    both orders in one file. Day-first needs strong evidence (at least 2 votes and more than 3× the month-first
    votes) because the businesses are American:
        ["15/03/2026", "28/02/2026", "01/04/2026"] → order "DMY" (so "01/04/2026" → April 1)
        ["10/09/2026", "11/03/2026", "13/10/2026"] → order "MDY" (one odd value isn't enough)
    """
    # The first 2,000 distinct values are plenty to decide and keep huge files fast.
    sample = [v for v in dict.fromkeys(v for v in values if v)][:2000]
    ctx: dict[str, Any] = {"order": "MDY", "format": params.get("input_format") or None, "serial": False}
    requested = (params.get("date_order") or "auto").upper()

    # Excel serial dates: 80%+ of the sample are bare numbers, all within Excel's date range (up to 2,958,465 =
    # 9999-12-31) and 80%+ of them between 10,000 and 80,000 (1927–2119), where real dates fall. With this hint,
    # short or decimal numbers in the column are read as serials too instead of as compact dates.
    numeric = [v for v in sample if _NUMERIC_ONLY_RE.match(v)]
    if sample and len(numeric) >= 0.8 * len(sample):
        nums = [float(v) for v in numeric]
        if all(1 <= n <= 2_958_465 for n in nums) and sum(10_000 <= n <= 80_000 for n in nums) >= 0.8 * len(nums):
            ctx["serial"] = True

    # Evidence for day-first: values like 15/03/2026 (first part > 12) vs month-first 03/15/2026, also written
    # without separators (see _first_two_parts).
    mdy_votes = dmy_votes = 0
    no_year_ok = field.type == "date" and field.no_year_value is not None
    for v in sample:
        parts = _first_two_parts(v, no_year_ok)
        if parts is None:
            continue
        a, b = parts
        if a > 12 >= b:
            dmy_votes += 1
        elif b > 12 >= a:
            mdy_votes += 1
    # An explicit order from the mapping wins (also the order of an input format such as "%d/%m/%Y", used for the
    # values that don't match the format exactly, e.g. with a time after the date); otherwise switch to day-first
    # only on clear evidence. A value that only fits one way ("13/10/2026" in a US column) is still read correctly
    # by itself (see dateparse._md).
    if requested not in DATE_ORDERS and ctx["format"]:
        requested = _format_order(ctx["format"]) or requested
    if requested in DATE_ORDERS:
        ctx["order"] = requested
    elif dmy_votes >= 2 and dmy_votes > 3 * mdy_votes:
        ctx["order"] = "DMY"  # US default unless the column clearly is day-first
    ctx["evidence"] = {"month_first": mdy_votes, "day_first": dmy_votes}
    # Date-only fields (birthday, anniversary) hold calendar dates, not moments: a time zone written with the value
    # ("1985-09-12T00:00:00Z", a system storing the date as UTC midnight) is ignored, so the day can't shift.
    ctx["calendar"] = field.type == "date"
    # Birthday/anniversary columns only: look for a year used to mean "unknown" (1970, 2000, the year it was typed).
    if field.type == "date" and field.no_year_value is not None:
        ctx["placeholder_year"] = _placeholder_year(sample, ctx)
    return ctx


def _first_two_parts(value: str, no_year_ok: bool) -> tuple[int, int] | None:
    """The first two numbers of a date, for the day-first vote, or None when the value can't tell.

    With separators (regex: two 1–2 digit numbers at the start separated by "/", "-", "." or a space, optionally
    followed by a 2–4 digit year): "15/03/2026" → (15, 3), "03-15" → (3, 15), "15.03.26" → (15, 3).
    Without separators, only where the reading is certain: 8 digits that aren't a YYYYMMDD date ("15032026" →
    (15, 3); "20261009" → None), 6 digits that can't be YYMMDD ("150390" → (15, 3); "150326" → None, it could be
    2015-03-26), and in birthday-type columns 3–4 digits that aren't a year ("1503" → (15, 3); "1985" → None).
    """
    m = re.match(r"^\s*(\d{1,2})[/\-. ](\d{1,2})(?:[/\-. ](\d{2,4}))?\b", value)
    if m:
        return int(m.group(1)), int(m.group(2))
    v = value.strip()
    if not v.isdigit():
        return None
    if len(v) == 8 and not _yyyymmdd(v):
        return int(v[:2]), int(v[2:4])
    if len(v) == 6 and not (1 <= int(v[2:4]) <= 12 and 1 <= int(v[4:]) <= 31):
        return int(v[:2]), int(v[2:4])
    if len(v) in (3, 4) and no_year_ok and not (MIN_YEAR <= int(v) <= MAX_YEAR):
        v = v.zfill(4)
        return int(v[:2]), int(v[2:])
    return None


def _yyyymmdd(digits: str) -> bool:
    """Whether 8 digits read as a real year-month-day date with a year from 1900 to 2100 ("20261009")."""
    y, m, d = int(digits[:4]), int(digits[4:6]), int(digits[6:])
    return MIN_YEAR <= y <= MAX_YEAR and 1 <= m <= 12 and 1 <= d <= 31


def _format_order(fmt: str) -> str | None:
    """The date order of a strptime input format: "%d/%m/%Y" → "DMY", "%Y-%m-%d" → "YMD", "%m/%d/%y" → "MDY".

    Used so values that don't match the format exactly (a time after the date, a missing leading zero the format
    doesn't allow) are still read in the format's order. None when the format has no day or no month.
    """
    pos = {k: min((fmt.find(c) for c in codes if c in fmt), default=-1)
           for k, codes in (("D", ("%d", "%-d")), ("M", ("%m", "%-m", "%b", "%B")), ("Y", ("%Y", "%y")))}
    if pos["D"] < 0 or pos["M"] < 0:
        return None
    if 0 <= pos["Y"] < pos["M"]:
        return "YMD"
    return "DMY" if pos["D"] < pos["M"] else "MDY"


def _placeholder_year(sample: list[str], ctx: dict) -> int | None:
    """A year that stands for "unknown" in a birthday/anniversary column.

    Real birthdays spread over decades. When one year covers most of the column (all 1970, all 2000, or the year
    someone typed "9/12" into Excel, which silently adds the current year), it's a placeholder, not a year.

    Counted over the distinct values in `sample` that have a real year after 1900 (1900 and earlier already mean
    "no year"). A year is the placeholder when it has at least half of 10+ such values, or 80% of 5–9 of them.
    Example: "2025-09-12", "2025-03-14", "2025-07-04", "2025-12-25", "2025-01-02", "1985-06-01" → 2025, so those
    five come out as MM/DD/1600 while "1985-06-01" stays 06/01/1985. Returns None when there is no such year.
    """
    years: dict[int, int] = {}
    for v in sample:
        try:
            dt, _ = DP.parse(v, order=ctx["order"], allow_no_year=True, serial_hint=bool(ctx.get("serial")))
        except (DP.DateError, ValueError, OverflowError, TypeError):
            continue
        if dt.year != _NO_YEAR and dt.year > 1900:
            years[dt.year] = years.get(dt.year, 0) + 1
    total = sum(years.values())
    if not years:
        return None
    year, n = max(years.items(), key=lambda kv: kv[1])
    if (total >= 10 and n >= 0.5 * total) or (total >= 5 and n >= 0.8 * total):
        return year
    return None


def _dateutil_parse(value: str, dayfirst: bool) -> datetime | None:
    """Fallback parse with python-dateutil for values our own parser doesn't know. Returns None if it can't.

    dateutil fills missing parts from `default`, so "October" alone would silently become October 1, 2000. The
    value is therefore parsed twice with different defaults (2000-01-01 and 2004-02-02); if year, month or day
    differ between the two, that part was invented, and the value is refused. Any zone is dropped, not converted.
        "October"                    → None
        "2026-10-09 15:45:00 CEST"   → 2026-10-09 15:45 (zone name dateparse doesn't know; kept as written)
    """
    try:
        a = du_parser.parse(value, dayfirst=dayfirst, default=datetime(2000, 1, 1))
        b = du_parser.parse(value, dayfirst=dayfirst, default=datetime(2004, 2, 2))
    except (ValueError, OverflowError, TypeError, ArithmeticError):  # ArithmeticError: e.g. "12:1111…" (decimal)
        return None
    if (a.year, a.month, a.day) != (b.year, b.month, b.day):
        return None  # a date part was missing and got filled in by the default
    return a.replace(tzinfo=None)


def parse_date(value: str, ctx: dict, allow_no_year: bool = False) -> tuple[datetime | None, str | None]:
    """Parse a date/datetime value with the column context. Returns (naive datetime, None) or (None, reason).

    Attempts, in order:
      1. ctx["format"] (params["input_format"]) with datetime.strptime, when the mapping gives one.
      2. dateparse.parse with the column's order, Excel-serial hint and the import's zone (ctx["tz"], default
         America/New_York). Times that carry a zone come back converted to that zone, except in calendar-date
         columns (ctx["calendar"], birthday/anniversary): there the date is taken as written.
      3. dateutil (_dateutil_parse), only when dateparse failed on an unknown word or an unrecognized layout.
    `allow_no_year` lets month/day-only values through, with year dateparse.NO_YEAR (birthday-type fields).
    Errors are dateparse's messages ("no year", "not a real calendar date", "unrecognized word 'asap'", ...) or
    "not a date" for any unexpected exception, so one odd value can never stop an import.
    """
    fmt = ctx.get("format")
    # An explicit input format is tried first; a value that doesn't match it still gets the flexible parser.
    if fmt:
        try:
            return datetime.strptime(value.strip(), fmt), None
        except ValueError:
            pass
    try:
        dt, _ = DP.parse(value, order=ctx.get("order", "MDY"), allow_no_year=allow_no_year,
                         serial_hint=bool(ctx.get("serial")), target_tz=ctx.get("tz") or DP.DEFAULT_TZ,
                         convert_zones=not ctx.get("calendar"))
        return dt, None
    except DP.DateError as exc:
        msg = str(exc)
    except (ValueError, OverflowError, TypeError, ArithmeticError):  # never let one odd value stop an import
        return None, "not a date"
    # Only these two failures mean "layout not understood"; others ("no year", "invalid time") are final.
    if msg.startswith("unrecognized word") or msg == "unrecognized date":
        dt = _dateutil_parse(value, dayfirst=ctx.get("order") == "DMY")
        if dt is not None:
            return dt, None
    return None, msg


def _strftime_year(dt: datetime, fmt: str, year_text: str) -> str:
    """strftime with a literal year (portable for years like 1600 and for month/day-only values).

    `dt` supplies month, day and time; `year_text` replaces %Y, and its last two digits replace %y:
        (Sep 12, "%m/%d/%Y", "1600")               → "09/12/1600"
        (Feb 29, "%Y-%m-%d (%y) 100%%", "1600")    → "1600-02-29 (00) 100%"
    Why not dt.replace(year=1600).strftime(...): the no-year marker is year 4, and strftime of years below 1000
    differs by platform (some zero-pad, some don't). Writing the year as text avoids that.
    """
    safe = dt.replace(year=2000)  # leap year, so Feb 29 survives
    # The year goes into the format as plain text ("%m/%d/1600"); strftime copies it as-is. (A control-character
    # marker would be cut off by macOS's strftime, which stops at a NUL byte — that gave "10/18/".)
    # Regex: "%%", "%Y" or "%y". "%%" is matched (and written back as "%%") so a literal percent sign followed by Y
    # isn't taken for the year directive: "100%%Y" still prints "100%Y".
    literal = re.sub(r"%(%|Y|y)", lambda m: {"%": "%%", "Y": year_text, "y": year_text[-2:]}[m.group(1)], fmt)
    return safe.strftime(literal)


def normalize_date(value: str, field: FieldSpec, params: dict, ctx: dict) -> tuple[str, str | None]:
    """Normalizer for "date" and "datetime" fields. Returns (formatted, None), ("", None) or ("", reason).

    Output format: field.format, else "%Y-%m-%d %H:%M:%S" (datetime) or "%Y-%m-%d" (date). In the real schema,
    activity columns use "%m/%d/%Y %I:%M %p" ("2026-10-09T19:45:00Z" → "10/09/2026 03:45 PM" for a New York
    import) and birthday/anniversary use "%m/%d/%Y" with no_year_value 1600.
    Rules, in order:
      - "0000-00-00"-style values are blank, not errors.
      - Month+day without a year is accepted only when the output has no year or the field has a no_year_value.
      - The no-year value replaces the year when the value has none or (no_year_value fields only) the year is
        1900 or earlier, is the column's placeholder year, or the date is within the last two years or in the
        future, because Excel stores "9/12" typed into a cell with the year it was typed:
        "Sep 12" → "09/12/1600", "01/15/1900" → "01/15/1600", "12/25/2099" → "12/25/1600".
      - Any other year must be 1900–2100, else "year N is out of range".
    """
    out_fmt = field.format or ("%Y-%m-%d %H:%M:%S" if field.type == "datetime" else "%Y-%m-%d")
    # Regex: see _ZERO_DATE_RE ("0000-00-00", "00/00/0000 00:00").
    if _ZERO_DATE_RE.match(value.strip()):
        return "", None  # "0000-00-00" style placeholders mean no date
    # Month/day-only values are allowed when the output has no year at all, or the field has a no-year value.
    has_year = "%Y" in out_fmt or "%y" in out_fmt
    no_year_value = field.no_year_value if field.type == "date" else None
    allow_no_year = not has_year or no_year_value is not None
    dt, err = parse_date(value, ctx, allow_no_year=allow_no_year)
    if err or dt is None:
        return "", err or "unrecognized date"
    # Month+day only, the target's own no-year value (1600), or a year that means "unknown": 1900 and earlier
    # (01/15/1900), the column's placeholder year (see _placeholder_year), or a year no real customer birthday has —
    # the last two years or the future (Excel stores "9/12" typed into a cell with the year it was typed).
    no_year = dt.year == _NO_YEAR or (no_year_value is not None and (
        dt.year <= 1900 or dt.year == ctx.get("placeholder_year") or dt.date() > date.today() - timedelta(days=730)))
    if no_year:
        # Without a no_year_value the output format has no year (see allow_no_year), so "2000" never shows.
        return _strftime_year(dt, out_fmt, str(no_year_value) if no_year_value is not None else "2000"), None
    # Catches typos such as "10/09/0226" or "3026" in activity columns.
    if not (MIN_YEAR <= dt.year <= MAX_YEAR):
        return "", f"year {dt.year} is out of range"
    return dt.strftime(out_fmt), None


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

# Cached (200,000 entries): addresses repeat across rows and full RFC syntax validation is comparatively slow.
@lru_cache(maxsize=200_000)
def _parse_email(value: str) -> tuple[str, str | None]:
    """Validate and normalize one email address. Returns (address, None) or ("", "invalid email (<reason>)").

    Accepts "<John@Gmail.com>", "mailto:a@b.com" and cells with several addresses separated by ";", "," or
    spaces (the first one is used). Syntax only, no DNS/deliverability check, so it is fast and works offline.
    The address is lower-cased.
        "JOHN@GMAIL.COM" → ("john@gmail.com", None)
        "a@b"            → ("", "invalid email (the part after the @-sign is not valid)")
    The reason is the first sentence of email_validator's message, lower-cased.
    """
    v = value.strip().strip("<>").removeprefix("mailto:").strip()
    # Several addresses in one cell ("a@b.com; c@d.com" or "a@b.com, c@d.com"): keep the first.
    v = re.split(r"[;,\s]+", v)[0] if v else v
    try:
        info = validate_email(v, check_deliverability=False)
        return info.normalized.lower(), None
    except EmailNotValidError as exc:
        return "", f"invalid email ({str(exc).split('.')[0].lower()})"


def normalize_email(value: str, field: FieldSpec, params: dict, ctx: dict) -> tuple[str, str | None]:
    """Normalizer for "email" fields; see _parse_email (cached). field, params and ctx aren't needed."""
    return _parse_email(value)


# ---------------------------------------------------------------------------
# ZIP / State
# ---------------------------------------------------------------------------

# 5 digits, optionally followed by 4 more (ZIP+4) after "-", a space or nothing: "07030", "07030-1234", "070301234".
_ZIP_RE = re.compile(r"^(\d{5})(?:[-\s]?(\d{4}))?$")


def normalize_zip(value: str, field: FieldSpec, params: dict, ctx: dict) -> tuple[str, str | None]:
    """Normalizer for "zip" fields: US ZIP codes, restoring leading zeros that spreadsheets drop.

    Output: 5 digits, or "12345-6789" when field.format is "zip5_4" and the value has the +4 part.
        "7030"       → "07030"       NJ / New England ZIPs start with 0; Excel stores them as numbers
        "11801.0"    → "11801"       float artifact
        "07030-1234" → "07030"       format "zip5" (default)
        "70301234"   → "07030-1234"  format "zip5_4"
        "ABCDE"      → ("", "invalid ZIP code")
    """
    # Any error from _fix_numeric_artifacts is ignored: a mangled value simply fails the ZIP pattern below.
    v, _ = _fix_numeric_artifacts(value.strip())
    # Spaces inside an all-numeric value are dropped ("07030 1234" → "070301234"); text with letters is left alone.
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


# Lower-cased full names (plus DC and the territories) and common abbreviations → USPS 2-letter codes. Keys are
# matched after normalize_state turns dots and spaces into single spaces ("N.Y." → "n y", "Penna." → "penna").
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
# The valid 2-letter output codes ("NY", "PR", ...); a value that already is one is accepted as is.
STATE_CODES = set(US_STATES.values())


def normalize_state(value: str, field: FieldSpec, params: dict, ctx: dict) -> tuple[str, str | None]:
    """Normalizer for "state" fields: US state/territory name or abbreviation → 2-letter USPS code.

        "NY", "ny", "New York", "N.Y.", "NYC" → "NY";  "Washington D.C." → "DC";  "Penna" → "PA"
        "Atlantis" → ("", "not a US state")
    Also used by looks_like_state with field=None (the field isn't read here).
    """
    # Dots and whitespace → single spaces: "N.Y." → "n y", "Washington  D.C." → "washington d c".
    v = re.sub(r"[.\s]+", " ", value.strip().lower()).strip()
    if v.upper() in STATE_CODES:
        return v.upper(), None
    # "n y" → "ny".
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
    """Read a number written the way spreadsheets and people write money and counts. Returns None if it isn't one.

    Thousands commas, $ € £ and % signs are removed; accounting negatives in parentheses become negative:
        "1,200" → 1200, "$1,200.50" → 1200.50, "(1,200.50)" → -1200.50, "15%" → 15 (not 0.15), "abc" → None.
    A comma is always read as a thousands separator, so a European "1,5" reads as 15.
    """
    v = value.strip().replace(",", "").replace("$", "").replace("€", "").replace("£", "").replace("%", "")
    neg = v.startswith("(") and v.endswith(")")
    v = v.strip("()").strip()
    try:
        d = Decimal(v)
    except InvalidOperation:
        return None
    return -d if neg else d


def normalize_integer(value: str, field: FieldSpec, params: dict, ctx: dict) -> tuple[str, str | None]:
    """Normalizer for "integer" fields: "1,200" → "1200", "1200.00" → "1200", "1.5" → ("", "not a whole number")."""
    d = _to_decimal(value)
    if d is None or d != d.to_integral_value():
        return "", "not a whole number"
    return str(int(d)), None


def normalize_decimal(value: str, field: FieldSpec, params: dict, ctx: dict) -> tuple[str, str | None]:
    """Normalizer for "decimal" fields: "$1,200.50" → "1200.5", "abc" → ("", "not a number").

    Trailing zeros are dropped (Decimal.normalize) and the result is written in plain notation ("f"), because
    normalize() alone would turn "1200" into "1.2E+3".
    """
    d = _to_decimal(value)
    if d is None:
        return "", "not a number"
    return format(d.normalize(), "f"), None


# Words (lower-cased) meaning yes / no in opt-in, status and checkbox columns ("x" and "✓" are ticked boxes).
TRUE_TOKENS = {"y", "yes", "true", "t", "1", "x", "on", "opted in", "opt in", "optin", "subscribed", "active", "✓"}
FALSE_TOKENS = {"n", "no", "false", "f", "0", "off", "opted out", "opt out", "optout", "unsubscribed", "inactive"}
# The same, as compared by normalize_boolean (_boolean_key: no spaces, hyphens or underscores).
_TRUE_KEYS = {t.replace(" ", "") for t in TRUE_TOKENS}
_FALSE_KEYS = {t.replace(" ", "") for t in FALSE_TOKENS}


def normalize_boolean(value: str, field: FieldSpec, params: dict, ctx: dict) -> tuple[str, str | None]:
    """Normalizer for "boolean" fields: yes/no words → field.true_value / field.false_value.

    "yes", "Y", "1", "x", "Subscribed" → true_value; "no", "0", "Unsubscribed", "opted out", "Opt-Out" → false_value;
    anything else → ("", "not a yes/no value"). The real schema's consent column uses this when a person maps a
    column to it (true / false; any other value is left blank, which changes nobody's consent).
    """
    v = _boolean_key(value)
    if v in _TRUE_KEYS:
        return field.true_value, None
    if v in _FALSE_KEYS:
        return field.false_value, None
    return "", "not a yes/no value"


def _boolean_key(value: str) -> str:
    """A yes/no value reduced for comparison: full-width characters made ASCII, lower case, a spreadsheet ".0"
    removed, and spaces, hyphens and underscores dropped. "Opt-Out" → "optout", "Un-subscribed" → "unsubscribed",
    "1.0" → "1", "Ｎ" → "n" (0.22.0; before, these weren't read as yes/no)."""
    v = unicodedata.normalize("NFKC", value).strip().lower()
    v = _FLOAT_ARTIFACT_RE.sub(r"\1", v)
    return re.sub(r"[\s\-_]+", "", v)


def normalize_enum(value: str, field: FieldSpec, params: dict, ctx: dict) -> tuple[str, str | None]:
    """Normalizer for "enum" fields: case-insensitive match against field.allowed_values, output in their spelling.

    With allowed ["Gold", "Silver"]: "gold" → "Gold"; "bronze" → ("", "not one of the allowed values (Gold,
    Silver)"). At most 8 allowed values are listed in the message.
    """
    lookup = {a.lower(): a for a in field.allowed_values}
    hit = lookup.get(value.strip().lower())
    if hit is None:
        return "", f"not one of the allowed values ({', '.join(field.allowed_values[:8])})"
    return hit, None


def normalize_string(value: str, field: FieldSpec, params: dict, ctx: dict) -> tuple[str, str | None]:
    """Normalizer for "string" fields: re-case (field.case, see apply_case) and enforce field.max_length.

    Too long → cut and right-trimmed when field.truncate, else ("", "longer than N characters").
    With case="name", max_length=5, truncate=True: "MCDONALD" → "McDon".
    """
    v = apply_case(value, field.case)
    if field.max_length and len(v) > field.max_length:
        if field.truncate:
            return v[: field.max_length].rstrip(), None
        return "", f"longer than {field.max_length} characters"
    return v, None


# Field type (FieldSpec.type) → normalizer. Every normalizer takes (value, field, params, ctx), gets a value that
# clean_text already cleaned and that isn't empty, and returns (output, error or None).
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
    """Build the column context for one output field before its values are normalized (see the module docstring).

    Called by the engine once per field with the column's cleaned values from non-empty rows (for "one row per phone
    number" mappings, the values of all its source columns together). Dates → prepare_date; phones →
    prepare_phone; other types need no context ({}). The engine then adds ctx["tz"] and passes the same dict to
    every normalize() call for the column, which is how normalizers can report back (ctx["repairs"]).
    """
    if field.type in ("date", "datetime"):
        return prepare_date(field, values, params)
    if field.type == "phone":
        return prepare_phone(field, values)
    return {}


def normalize(value: str, field: FieldSpec, params: dict, ctx: dict) -> tuple[str, str | None]:
    """Return (output_value, error). Empty input returns ("", None); required-ness is checked by the engine.

    The single per-value entry point: cleans the value (clean_text), dispatches on field.type through NORMALIZERS,
    then enforces field.max_length for non-string types (strings handle it themselves, with optional truncation).
    `params` are the mapping's params for the field; `ctx` comes from prepare() for the same column.
    For the real phone_number field:
        "(516) 555-0100" → ("5165550100", None)
        "N/A"            → ("", None)       the engine then reports the required value as missing
        "555-1234"       → ("", "missing area code (only 7 digits)")
        "#DIV/0!"        → ("", "Excel error (broken formula in the client's file)")   any field type
    A phone value over PHONE_MAX_LEN or a date over DATE_MAX_LEN characters is rejected as too long; a value that
    makes a normalizer fail unexpectedly gets UNREADABLE_REASON (and is logged) instead of stopping the import.
    """
    v = clean_text(value)
    if not v:
        return "", None
    # A spreadsheet error code holds no data. Said plainly, instead of the type's own reason ("too few digits (1)"
    # for "#DIV/0!" as a phone, "unrecognized word 'value'" as a date). The code itself shows as the value in the
    # rejects report and side by side; one reason for all codes keeps them on one line of the summary.
    if sheet_error(v):
        return "", SHEET_ERROR_REASON
    # Text far longer than any phone or date (notes mapped by mistake, junk): rejected straight away, which also
    # keeps a single huge cell from taking seconds.
    if field.type == "phone" and len(v) > PHONE_MAX_LEN:
        return "", f"too long to be a phone number ({len(v):,} characters)"
    if field.type in ("date", "datetime") and len(v) > DATE_MAX_LEN:
        return "", f"too long to be a date ({len(v):,} characters)"
    # Safety net: an unexpected error on one value (a bug, or a library choking on odd text) costs only that value,
    # never the whole file. It is logged so it can be fixed.
    try:
        out, err = NORMALIZERS[field.type](v, field, params, ctx)
    except Exception:  # noqa: BLE001
        log.exception("Unexpected error converting a value for field '%s' (%s)", field.name, field.type)
        return "", UNREADABLE_REASON
    if err is None and field.type != "string" and field.max_length and len(out) > field.max_length:
        return "", f"longer than {field.max_length} characters"
    return out, err


# ---------------------------------------------------------------------------
# Lightweight detectors used for column profiling / auto-mapping
# ---------------------------------------------------------------------------

def _detector(fn):
    """Make a looks_like_* detector safe for any cell: False for text over DETECT_MAX_LEN characters (notes,
    junk: never a phone, date, email…, and slow to search) and False if it fails on an odd value, so guessing what
    a column holds never stops an upload."""
    @wraps(fn)
    def wrapper(v: str) -> bool:
        if not isinstance(v, str) or len(v) > DETECT_MAX_LEN:
            return False
        try:
            return fn(v)
        except Exception:  # noqa: BLE001 - a detector answers yes or no, nothing else
            return False
    return wrapper


@_detector
def looks_like_phone(v: str) -> bool:
    """Detector: does this look like ONE valid phone number? Used to score columns when profiling/auto-mapping.

    Stricter than the normalizer on purpose: 10–11 digits, no letters (besides "ext"/"x") and no "@", and it must
    parse as a valid US or Canadian number, so ID, order-number or ZIP+4 columns don't look like phones.
        "516-555-0100" → True; "416-555-0123" → True (Canada counts for detection); "Cell 516-555-0100" → False.
    Uses the cached _parse_phone, so profiling and conversion share work.
    """
    digits = re.sub(r"\D", "", v)
    if not (10 <= len(digits) <= 11):
        return False
    if re.search(r"[A-Za-z@]", v.replace("ext", "").replace("x", "")):
        return False
    return _parse_phone(v, "US", ("US", "CA"), "E164")[1] is None


# A US phone field for the detectors below (split_phones needs one for the default region).
_DETECT_PHONE_FIELD = FieldSpec(name="phone", type="phone")


@_detector
def looks_like_phones(v: str) -> bool:
    """Detector: one valid phone number, or a cell holding several where every one is a valid phone number.

    Cells with several numbers become one row per number (see split_phones), so for recognising a phone column by
    its values they count as phone values too. Every piece must pass looks_like_phone on its own, so notes such as
    "call 516-555-0100 or 917-555-1234" don't count.
        "516-555-0100" → True;  "(917) 555-1234 / 516-555-0199" → True;  "call 516-555-0100 or 917-555-1234" → False
    """
    if looks_like_phone(v):
        return True
    pieces = split_phones(v, _DETECT_PHONE_FIELD, {})
    return len(pieces) > 1 and all(looks_like_phone(p) for p, _ in pieces)


@_detector
def looks_like_email(v: str) -> bool:
    """Detector: something@something.tld with a 2+ letter TLD and no spaces. A fast regex, not full validation."""
    # Regex: non-"@"/space characters, "@", more of them, then "." and 2+ letters at the end: "a@b.com", not "a@b".
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$", v.strip()))


@_detector
def looks_like_zip(v: str) -> bool:
    """Detector: "11801", "07030-1234", or 3–4 digits (a ZIP that lost its leading zeros, "7030")."""
    # Regex 1: 5 digits with an optional "-" and 4 more; regex 2: 3 or 4 digits only.
    return bool(re.fullmatch(r"\d{5}(-\d{4})?", v.strip())) or bool(re.fullmatch(r"\d{3,4}", v.strip()))


@_detector
def looks_like_state(v: str) -> bool:
    """Detector: a US state/territory name or code (reuses normalize_state; the field argument isn't used)."""
    return normalize_state(v, None, {}, {})[1] is None  # type: ignore[arg-type]


@_detector
def looks_like_date(v: str) -> bool:
    """Detector: a value dateparse can read as a date with a plausible year (or month and day only).

    Bare numbers are excluded (otherwise ID, ZIP and amount columns would look like dates: "46304", "20261009"),
    and so are values without any digit ("today", "October"). "10/09/2026" and "Sep 12" → True; "46304" → False.
    Any parse error counts as False: profiling must never stop on an odd value ("+1 +51694619661").
    """
    v = v.strip()
    if _NUMERIC_ONLY_RE.match(v) or not re.search(r"\d", v):
        return False
    try:
        dt, _ = DP.parse(v, allow_no_year=True)
    except (DP.DateError, ValueError, OverflowError, TypeError):
        return False
    return dt.year == _NO_YEAR or MIN_YEAR <= dt.year <= MAX_YEAR
