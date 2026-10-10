"""Date / date-time parser built to read anything people and systems put in a date column.

Handles, in any mix within a column:
  numeric       10/9/2026, 10-09-26, 10.09.2026, 10 09 2026, 2026-10-09, 2026/10/9, 9/10/2026 (day-first when
                the column shows it), 10/09 (no year)
  month names   October 9, 2026 · October 9th 2026 · Oct. 9 '26 · 9 October 2026 · 9, October, 2026 ·
                9th of October 2026 · 09-Oct-26 · Oct-09-2026 · 2026 Oct 9 · OCTOBER 9 · Sept 9 ·
                Spanish (9 de octubre de 2026)
  weekdays      Friday, October 9, 2026 · Fri Oct 9 2026 (weekday is ignored)
  times         3:45 PM · 3:45pm · 3:45 p.m. · 3pm · 15:45 · 15:45:12.123 · 15h45 · noon · midnight ·
                1545 after a date · time before or after the date · "at" / "@"
  machine       2026-10-09T15:45:00Z · …+05:30 · RFC 2822 (Fri, 09 Oct 2026 15:45:00 +0000) ·
                JavaScript (Fri Oct 09 2026 15:45:00 GMT-0400 (Eastern Daylight Time)) · zone names
                (EST, PDT, UTC, ET…). A time that carries a zone, and every Unix timestamp, is converted to the
                import's time zone; a time with no zone is kept exactly as written.
  compact       20261009 · 10092026 · 100926 · 261009 · 202610091545 · 20261009154500
  spreadsheet   Excel serial numbers (46304, 46304.65625), ="10/09/2026", leading apostrophe
  epoch         Unix seconds (10 digits) and milliseconds (13 digits)

Two-digit years become 20yy unless that is more than a year in the future, then 19yy (08/09/85 → 1985,
08/09/26 → 2026). Ambiguous numeric dates are month-first (US) unless the column clearly shows day-first; a
value that only fits one way (13/10/2026) is read that way regardless.

What this module is responsible for
-----------------------------------
Turning one date/time text into a naive `datetime` plus whether it had a time, or raising DateError with a short
reason the user sees ("no year", "not a real calendar date", "unrecognized word 'asap'", ...). It knows nothing
about output formats or the target schema: normalizers.py decides how the result is written (MM/DD/YYYY
HH:MM AM/PM, the 1600 rule for birthdays without a year, blanks for "0000-00-00", ...).

Entry points
------------
parse(value, order="MDY", allow_no_year=False, today=None, serial_hint=False, target_tz=DEFAULT_TZ)
    -> (datetime, has_time). The only public parser. Month/day-only values (accepted with allow_no_year) get the
    marker year NO_YEAR (4). Raises DateError, a ValueError subclass.
DateError, NO_YEAR, MIN_YEAR, MAX_YEAR, DEFAULT_TZ
    Shared with normalizers.py and checks.py.

How it is called: normalizers.prepare_date samples the column once to choose `order` and `serial_hint`;
normalizers.parse_date then calls parse() for every distinct value with those, allow_no_year for birthday-type
fields, and target_tz = the import's time zone (ctx["tz"], set by the engine from the user's choice).
normalizers.looks_like_date (column profiling) and checks.py call parse() with allow_no_year=True and defaults.

How a value is parsed
---------------------
1. _prep: clean the text (NFKC, lower case, Excel wrappers, unicode dashes, ISO "T", ordinals, a.m./p.m.,
   noon/midnight; weekdays and filler words such as "of"/"at"/"de" removed) and take out any time zone.
2. _parse_prepped: an all-digit value goes to _compact (Excel serial, Unix epoch, compact digits); otherwise
   the time is taken out (_extract_time), the rest is split into words and numbers, and the date is built from a
   month name plus numbers, or from 2–3 numbers using the column order (_md) and the two-digit-year rule (_year).
3. parse: a value that had a time and a zone is converted to target_tz (_to_zone).

Time zones: a zone written in the value (Z, +05:30, GMT-4, UTC, EST/EDT/ET/PT..., "Eastern", "Pacific Time",
"(Eastern Daylight Time)") is honoured, and US abbreviations are read as that region's local time all year
("EST" in July is still New York time, because people write EST year-round). Unix timestamps are UTC moments
and are always converted. Times without a zone are kept exactly as written: they are the business's own local
times as the source system exported them.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

# Callers test `dt.year == NO_YEAR`; normalizers writes the field's no_year_value (1600) in its place. Year 4 can't
# be a real year in a contact file (MIN_YEAR is 1900).
NO_YEAR = 4  # marker year for "month and day only" (a leap year, so Feb 29 is valid)
# Plausible years for contact data; a 4-digit number outside this range is not taken as a year.
MIN_YEAR, MAX_YEAR = 1900, 2100
# Day 0 of Excel serial dates. Excel numbers days from 1900-01-01 = 1 but wrongly treats 1900 as a leap year, so
# counting from 1899-12-30 gives the dates Excel shows for every serial from 61 (1900-03-01) on: 46304 → 2026-10-09.
_EXCEL_EPOCH = datetime(1899, 12, 30)

# Month names and abbreviations → month number. Keys are lower case because _prep lower-cases everything. "febr"
# and "sept" are common abbreviations; the Spanish names cover Spanish-language exports and hand-typed dates.
MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "febr": 2, "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9,
    "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
    # Spanish
    "ene": 1, "enero": 1, "febrero": 2, "marzo": 3, "abr": 4, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
    "ago": 8, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "dic": 12,
    "diciembre": 12,
}
# Regex alternation of weekday names and abbreviations (English, and Spanish without accents). _prep removes them:
# they add nothing to the date and might even contradict it ("Friday, October 9, 2026").
WEEKDAYS = ("monday|mon|tuesday|tues|tue|wednesday|weds|wed|thursday|thurs|thur|thu|friday|fri|saturday|sat|"
            "sunday|sun|lunes|martes|miercoles|jueves|viernes|sabado|domingo")
# Filler words removed as whole words: "9th of October", "at 3:45", "on Oct 9", "9 de octubre de 2026", "15 hrs".
# Only whole words match (\b), so the "h" in the time "15h45" stays.
_FILLER = r"\b(of|the|at|on|de|del|en|el|hrs|hours|h)\b"
# Zone names with a fixed UTC offset in hours: UTC/GMT/Z, and Atlantic time (Puerto Rico, US Virgin Islands).
_FIXED_ZONES = {"utc": 0, "gmt": 0, "z": 0, "ast": -4, "adt": -3}
# US zone abbreviations are read as the region's local time (people write "EST" year-round to mean Eastern).
_NAMED_ZONES = {"et": "America/New_York", "est": "America/New_York", "edt": "America/New_York",
                "ct": "America/Chicago", "cst": "America/Chicago", "cdt": "America/Chicago",
                "mt": "America/Denver", "mst": "America/Denver", "mdt": "America/Denver",
                "pt": "America/Los_Angeles", "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles",
                "akst": "America/Anchorage", "akdt": "America/Anchorage", "hst": "Pacific/Honolulu"}
# Regex: any key of _FIXED_ZONES or _NAMED_ZONES as a whole word (longest first, the safe order for an alternation).
# Applied to lower-cased text: matches "est" in "3:45 pm est", "utc", "pdt", a lone "z".
_TZ_NAMES_RE = re.compile(r"\b(" + "|".join(sorted(list(_FIXED_ZONES) + list(_NAMED_ZONES), key=len, reverse=True))
                          + r")\b")
# Zone of the import when the caller gives none (Eastern).
DEFAULT_TZ = "America/New_York"
# Spelled-out US zone names → IANA zone; also used for "(Eastern Daylight Time)" in JavaScript date strings.
_ZONE_WORDS = {"eastern": "America/New_York", "central": "America/Chicago", "mountain": "America/Denver",
               "pacific": "America/Los_Angeles", "alaska": "America/Anchorage", "hawaii": "Pacific/Honolulu",
               "arizona": "America/Phoenix"}
# Regex: a _ZONE_WORDS name, optionally followed by "standard"/"daylight"/"prevailing" and optionally by "time", as
# whole words: "eastern", "pacific time", "central daylight time", "mountain standard time".
_ZONE_WORDS_RE = re.compile(r"\b(" + "|".join(_ZONE_WORDS) + r")(?:\s+(?:standard|daylight|prevailing))?(?:\s+time)?\b")


def _is_zone_text(text: str) -> bool:
    """Whether `text` is nothing but a zone name: "EST", "utc", "Eastern Daylight Time" → True; "cell" → False.

    Used by _prep to keep zone names written in parentheses and drop any other parenthesised text.
    """
    t = text.strip().lower()
    return bool(_TZ_NAMES_RE.fullmatch(t) or _ZONE_WORDS_RE.fullmatch(t))

# Regex for a clock time: 1–2 digit hour, ":" or "h", 2-digit minutes, optional ":SS" with an optional fraction
# (".123" or ",123"), optional "am"/"pm" (already normalized by _prep). Not preceded by a digit, "/", "." or "-" (so
# it can't start inside a date or a longer number) and not followed by a digit.
# Matches "3:45", "15:45:12.123", "3:45 pm", "15h45".
_TIME_RE = re.compile(
    r"(?<![\d/.\-])(\d{1,2})[:h](\d{2})(?::(\d{2})(?:[.,]\d{1,9})?)?\s*(am|pm)?(?!\d)")
# Regex for an hour with am/pm and no colon: "3pm", "3 pm", "3.45pm" (a dot between hour and minutes). Not preceded
# by a digit, "/", ":" or "-".
_TIME_AMPM_RE = re.compile(r"(?<![\d/:\-])(\d{1,2})(?:\.(\d{2}))?\s*(am|pm)\b")
# The whole (prepared) value is digits only: Excel serials, Unix epochs and compact dates such as "20261009".
_DIGITS_RE = re.compile(r"^\d+$")
# The whole value is a decimal number: an Excel serial with a time of day ("46304.65625" = 2026-10-09 15:45).
_DECIMAL_RE = re.compile(r"^\d+\.\d+$")


class DateError(ValueError):
    """A value isn't a date. The message is a short reason shown to the user ("no year", "invalid time", ...).

    Subclass of ValueError, so callers that already catch ValueError also catch it.
    """
    pass


def _offset(sign: str, hh: str, mm: str | None) -> tzinfo:
    """Fixed-offset tzinfo from a parsed UTC offset: ("-", "04", "00") → UTC-04:00, ("+", "5", "30") → UTC+05:30."""
    delta = timedelta(hours=int(hh), minutes=int(mm or 0))
    return timezone(-delta if sign == "-" else delta)


# Words that can surround a birthday or anniversary without being part of it, ignored when allow_no_year is set:
# "Birthday: 9/12", "DOB 9/12", "Born Sep 12", "9/12/n/a" (tokens "n" and "a"), "year unknown".
_LABEL_WORDS = {"birthday", "bday", "dob", "born", "birth", "date", "of", "anniversary", "anniv", "wedding", "b",
                "day", "unknown", "year", "n", "a", "na"}


def _prep(value: str) -> tuple[str, tzinfo | None]:
    """Normalize the text and pull out any time zone (returned separately).

    Returns (clean lower-case text, tzinfo or None). The text keeps only what _parse_prepped needs: numbers,
    separators, month names, a time and "am"/"pm". Examples:
        "Fri Oct 09 2026 15:45:00 GMT-0400 (Eastern Daylight Time)" → ("oct 09 2026 15:45:00", UTC-04:00)
        "10/09/2026 3:45 PM EST"                → ("10/09/2026 3:45 pm", America/New_York)
        "20261009T194500Z"                      → ("2026-10-09 19:45:00", UTC)
        "the 9th of October, 2026 at 3:45 p.m." → ("9 october 2026 3:45 pm", None)
        "46304,65625"                           → ("46304.65625", None)
    When the value has several zone notations, a numeric offset wins over any zone name (it is exact).
    """
    # NFKC turns full-width digits and letters into ASCII; then zero-width spaces and byte-order marks are dropped.
    v = unicodedata.normalize("NFKC", value)
    v = v.replace("\u200b", "").replace("\ufeff", "").strip()
    # Regex: the whole value in double quotes, optionally after "=" (Excel's ="..." text formula).
    m = re.match(r'^=?"(.*)"$', v)
    if m:  # Excel text-protection wrapper ="10/09/2026"
        v = m.group(1)
    # A leading apostrophe is Excel's "store as text" marker. Everything is lower case from here on (the regexes
    # rely on it).
    v = v.strip().lstrip("'").strip().lower()
    # U+2010–U+2015 (hyphens, en/em dashes, horizontal bar) and U+2212 (minus sign) → "-".
    v = re.sub(r"[\u2010-\u2015\u2212]", "-", v)          # unicode dashes
    # Regex: exactly 5 digits, a comma and more digits: "46304,65625" (European decimal comma) → "46304.65625".
    if re.fullmatch(r"\d{5},\d+", v):                      # Excel serial with a decimal comma
        v = v.replace(",", ".")
    # Zone in parentheses: "(EST)", "(Eastern Daylight Time)" → keep the zone, drop the brackets.
    # Regex: "(", anything but ")", ")". Zone names are kept without the brackets; any other text ("(cell)") goes.
    v = re.sub(r"\(([^)]*)\)", lambda m: f" {m.group(1)} " if _is_zone_text(m.group(1)) else " ", v)
    # Regex: 8 digits, "t" (the lower-cased ISO "T"), 4 or 6 digits, then the rest ("z", an offset...). Rebuilt as
    # "2026-10-09 19:45:00" + rest: "20261009t194500z" → "2026-10-09 19:45:00z".
    m = re.match(r"^(\d{8})t(\d{4}(?:\d{2})?)(.*)$", v)    # basic ISO 20261009T154500Z
    if m:
        t = m.group(2)
        v = f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]} {t[:2]}:{t[2:4]}" + (f":{t[4:]}" if len(t) > 4 else "") + m.group(3)
    # Regex: a YYYY-M-D date directly followed by "t" and a digit: "2026-10-09t15:45" → "2026-10-09 15:45".
    v = re.sub(r"(\d{4}-\d{1,2}-\d{1,2})t(\d)", r"\1 \2", v)  # ISO "T"
    tz: tzinfo | None = None
    # Spelled-out zone (see _ZONE_WORDS_RE); taken out of the text once read.
    m = _ZONE_WORDS_RE.search(v)                            # "Eastern", "Pacific Time", "Central Daylight Time"
    if m:
        tz = ZoneInfo(_ZONE_WORDS[m.group(1)])
        v = v[: m.start()] + " " + v[m.end():]
    # Regex: a digit (optionally followed by a fraction, as in "00.000z") directly followed by "z" at the end of a
    # word: the ISO UTC marker. Only the "z" is removed; a spelled-out zone found above takes precedence.
    if re.search(r"\d(?:\.\d+)?z\b", v):                   # ISO "Z"
        tz = tz or timezone.utc
        v = re.sub(r"(\d)z\b", r"\1", v)
    # numeric UTC offsets after a time: 15:45:00+05:30, 15:45 -0400, 3:45 pm gmt-0400
    # Regex: a time (H:MM, optional :SS and fraction, optional am/pm; kept as group 1), optional "gmt"/"utc", then
    # a sign, 2-digit hours and optional minutes with or without a colon. The offset is removed, the time kept.
    m = re.search(r"(\d{1,2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?(?:\s*[ap]\.?m\.?)?)\s*(?:gmt|utc)?\s*([+-])(\d{2})(?::?(\d{2}))?\b", v)
    if m:
        tz = _offset(m.group(2), m.group(3), m.group(4))
        v = v[: m.start()] + m.group(1) + v[m.end():]
    # Regex: "gmt"/"utc", a sign and 1–2 digit hours with optional minutes, anywhere: "gmt-4", "utc+5:30", "gmt +0530".
    m = re.search(r"\b(?:gmt|utc)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?\b", v)
    if m:
        tz = _offset(m.group(1), m.group(2), m.group(3))
        v = v[: m.start()] + " " + v[m.end():]
    # Zone abbreviations (EST, PDT, UTC, Z...): used only when no zone was found yet, but always removed from the text.
    m = _TZ_NAMES_RE.search(v)
    if m:
        name = m.group(1)
        if tz is None:
            tz = ZoneInfo(_NAMED_ZONES[name]) if name in _NAMED_ZONES else timezone(timedelta(hours=_FIXED_ZONES[name]))
        v = _TZ_NAMES_RE.sub(" ", v)
    # "noon"/"12 noon" and "midnight"/"12 midnight" become clock times that _extract_time understands.
    v = re.sub(r"\b12\s*noon\b|\bnoon\b", " 12:00 pm ", v)
    v = re.sub(r"\b12\s*midnight\b|\bmidnight\b", " 12:00 am ", v)
    # Regexes below: "a.m."/"p. m." → "am"/"pm"; a lone "a"/"p" right after a digit → "am"/"pm" ("3:45p" →
    # "3:45 pm"); ordinal suffixes after a number ("9th" → "9"); an apostrophe before a 2-digit year ("'26" → "26").
    v = re.sub(r"\b([ap])\.\s?m\.?(?=\W|$)", r"\1m", v)    # a.m. / p.m.
    v = re.sub(r"(\d)\s*([ap])(?![a-z])", r"\1 \2m", v)     # 3:45p → 3:45 pm
    v = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", v)           # ordinals
    v = re.sub(r"'(\d{2})\b", r"\1", v)                      # '26
    # Weekday names (with an optional trailing dot, "Fri.") are dropped; the date itself decides the day.
    v = re.sub(r"\b(" + WEEKDAYS + r")\b\.?", " ", v)
    # "@" as in "10/09/2026 @ 3:45 PM"; then filler words; commas and semicolons are only separators.
    v = v.replace("@", " ")
    v = re.sub(_FILLER, " ", v)
    v = re.sub(r"[,;]", " ", v)
    return re.sub(r"\s+", " ", v).strip(), tz


def _to_zone(dt: datetime, src: tzinfo | None, target: str) -> datetime:
    """Convert an aware moment to the import's zone (naive result). No source zone → unchanged.

    `dt` is the naive wall time as written, `src` the zone written with it and `target` an IANA zone name:
        (2026-10-09 19:45, UTC, "America/New_York") → 2026-10-09 15:45
        (2026-10-09 15:45, None, "America/Denver")  → 2026-10-09 15:45   no zone: kept as written
    zoneinfo applies daylight saving time for the date on both sides.
    """
    if src is None:
        return dt
    return dt.replace(tzinfo=src).astimezone(ZoneInfo(target)).replace(tzinfo=None)


def _year(text: str, month: int, day: int, today: date) -> int:
    """The 4-digit year for a year written with 2 or 4 digits; raises DateError for any other length.

    Two-digit years become 20yy unless that date would be more than a year after `today`, then 19yy, because
    contact files hold past dates (birthdays, last visits) and only the occasional near-future one. With today =
    2026-10-09: "85" → 1985, "26" → 2026, "45" → 1945 (2045 is too far ahead), "00" → 2000.
    `month` and `day` are only used for that comparison; if they aren't a real date (Feb 30) the 20yy year is
    returned and _make reports the bad date.
    """
    if len(text) == 4:
        return int(text)
    if len(text) == 2:
        yy = int(text)
        candidate = 2000 + yy
        try:
            if date(candidate, month, day) > today + timedelta(days=365):
                return 1900 + yy
        except ValueError:
            pass
        return candidate
    raise DateError("year must have 2 or 4 digits")


def _make(y: int, m: int, d: int, hh: int = 0, mi: int = 0) -> datetime:
    """datetime(y, m, d, hh, mi) with user-facing errors: "month is not 1–12" or "not a real calendar date".

    Also catches OverflowError, e.g. a day of 51694619661 from a phone number ("1 51694619661") read as a date.
    """
    if not 1 <= m <= 12:
        raise DateError("month is not 1–12")
    try:
        return datetime(y, m, d, hh, mi)
    except (ValueError, OverflowError) as exc:  # OverflowError: e.g. day 51694619661 from "1 51694619661"
        raise DateError("not a real calendar date") from exc


def _md(a: int, b: int, order: str) -> tuple[int, int]:
    """Month/day from two numbers, using the column order unless the values only fit one way.

    `a` and `b` are the first and second number as written; `order` is the column order ("DMY" = day first,
    anything else = month first). Returns (month, day):
        (3, 5, "MDY") → (3, 5);  (3, 5, "DMY") → (5, 3)
        (13, 10, "MDY") → (10, 13)   only fits day-first, so it is read that way whatever the column says
        (13, 14, any) → DateError("no month in 1–12")
    """
    m, d = (b, a) if order == "DMY" else (a, b)
    if m > 12 and d <= 12:
        m, d = d, m
    if m > 12:
        raise DateError("no month in 1–12")
    return m, d


def _compact(digits: str, order: str, today: date, allow_no_year: bool, serial_hint: bool,
             target_tz: str = DEFAULT_TZ) -> datetime:
    """Read a value that is only digits (after _prep). Returns a naive datetime or raises DateError.

    Tried in this order (n = number of digits):
      - 5 digits (up to 7 with serial_hint): an Excel serial day from 20,000 to 80,000 (1954–2119), or any serial
        up to 2,958,465 (9999-12-31) when the column is serial: "46304" → 2026-10-09.
      - 10 digits from 2000 to 2100: Unix seconds, converted to target_tz, seconds dropped:
        "1791575100" → 2026-10-09 15:45 (New York). 13 digits: Unix milliseconds, "1791575100000" → the same.
      - 8 digits: YYYYMMDD when that is a real date with a plausible year ("20261009"), else MMDDYYYY (DDMMYYYY
        for day-first columns): "10092026".
      - 6 digits: MMDDYY (DDMMYY for day-first columns), else YYMMDD: "100926" and "261009" → 2026-10-09.
      - 12 or 14 digits: YYYYMMDDHHMM[SS], seconds ignored: "202610091545" → 2026-10-09 15:45.
      - 3–4 digits with allow_no_year that aren't a year 1900–2100: MMDD / MDD, no year: "0912" → Sep 12 (NO_YEAR).
    A 4-digit year alone ("1985") gives "only a year — no month or day"; anything else "number is not a
    recognizable date".
    """
    n = len(digits)
    val = int(digits)
    # Excel serial: 20,000–80,000 is 1954-10-03 to 2119-01-11, where a 5-digit number is far more likely a date than
    # anything else. In a serial column (serial_hint) the whole Excel range is accepted.
    if n == 5 or (serial_hint and n <= 7):
        if 20_000 <= val <= 80_000 or (serial_hint and 1 <= val <= 2_958_465):
            return _EXCEL_EPOCH + timedelta(days=val)
    # 946,684,800–4,102,444,800 = 2000-01-01 to 2100-01-01 UTC. A Unix timestamp is always a UTC moment, so it is
    # converted to the import's zone right here (and reported without a time so parse() doesn't convert it again).
    if n == 10 and 946_684_800 <= val <= 4_102_444_800:  # Unix seconds (a moment in UTC)
        return datetime.fromtimestamp(val, tz=ZoneInfo(target_tz)).replace(tzinfo=None, second=0)
    if n == 13 and 946_684_800_000 <= val <= 4_102_444_800_000:  # Unix milliseconds
        return datetime.fromtimestamp(val / 1000, tz=ZoneInfo(target_tz)).replace(tzinfo=None, second=0,
                                                                                   microsecond=0)
    # YYYYMMDD first; if the first 4 digits aren't a plausible year or the date isn't real, MMDDYYYY / DDMMYYYY.
    if n == 8:
        y = int(digits[:4])
        if MIN_YEAR <= y <= MAX_YEAR:
            try:
                return _make(y, int(digits[4:6]), int(digits[6:8]))
            except DateError:
                pass
        m, d = _md(int(digits[:2]), int(digits[2:4]), order)
        return _make(int(digits[4:]), m, d)
    if n == 6:  # fixed positions: MMDDYY (or DDMMYY for day-first columns), then YYMMDD
        a, b, c = digits[:2], digits[2:4], digits[4:]
        first = (int(b), int(a)) if order == "DMY" else (int(a), int(b))
        # Try (month, day) in column order with the last 2 digits as the year, then YYMMDD; the first real date wins.
        for (m, d), ytxt in ((first, c), ((int(b), int(c)), a)):
            try:
                return _make(_year(ytxt, m, d, today), m, d)
            except DateError:
                continue
        raise DateError("6-digit number is not a date")
    # Timestamps without separators: YYYYMMDDHHMM or YYYYMMDDHHMMSS (the output has no seconds).
    if n in (12, 14):
        y, m, d, hh, mi = int(digits[:4]), int(digits[4:6]), int(digits[6:8]), int(digits[8:10]), int(digits[10:12])
        if hh > 23 or mi > 59:
            raise DateError("invalid time")
        return _make(y, m, d, hh, mi)
    # Month and day only ("912", "0912") in birthday-type columns; a number from 1900 to 2100 is a year instead.
    if n in (3, 4) and allow_no_year and not (MIN_YEAR <= val <= MAX_YEAR):
        digits = digits.zfill(4)
        m, d = _md(int(digits[:2]), int(digits[2:]), order)
        return _make(NO_YEAR, m, d)
    if n == 4 and MIN_YEAR <= val <= MAX_YEAR:
        raise DateError("only a year — no month or day")
    raise DateError("number is not a recognizable date")


def _extract_time(s: str) -> tuple[str, int | None, int | None]:
    """Find and remove a clock time. Returns (rest of the text, hour 0–23, minute) or (text, None, None).

    Tries _TIME_RE ("3:45", "15h45") first, then _TIME_AMPM_RE ("3 pm"). 12-hour times become 24-hour:
    "12:30 am" → 0:30, "12 pm" → 12:00, "3:45 pm" → 15:45. Seconds are checked but dropped (output has minutes).
        "10/09/2026 3:45 pm" → ("10/09/2026", 15, 45)
        "10/09/2026 15h45"   → ("10/09/2026", 15, 45)
    Raises DateError("invalid time") for minutes or seconds over 59, hour 0 or over 23 with am/pm, or an hour
    over 23.
    """
    m = _TIME_RE.search(s)
    if m:
        hh, mi = int(m.group(1)), int(m.group(2))
        sec = int(m.group(3)) if m.group(3) else 0
        ampm = m.group(4)
    else:
        m = _TIME_AMPM_RE.search(s)
        if not m:
            return s, None, None
        hh, mi, sec, ampm = int(m.group(1)), int(m.group(2) or 0), 0, m.group(3)
    if mi > 59 or sec > 59:
        raise DateError("invalid time")
    if ampm:
        # An hour of 13–23 with am/pm ("15:45 pm") is taken as 24-hour time; 0 or 24+ with am/pm is an error.
        if not 1 <= hh <= 12:
            if hh == 0 or hh > 23:
                raise DateError("invalid time")
        elif ampm == "am":
            hh = 0 if hh == 12 else hh
        else:
            hh = 12 if hh == 12 else hh + 12
    elif hh > 23:
        raise DateError("invalid time")
    rest = (s[: m.start()] + " " + s[m.end():]).strip()
    return re.sub(r"\s+", " ", rest), hh, mi


def parse(value: str, order: str = "MDY", allow_no_year: bool = False, today: date | None = None,
          serial_hint: bool = False, target_tz: str = DEFAULT_TZ) -> tuple[datetime, bool]:
    """Return (datetime, has_time). Month/day-only values get year NO_YEAR. Raises DateError.

    The module's public entry point (see the module docstring for the formats):
        value          the raw text, e.g. "Fri, 09 Oct 2026 19:45:00 +0000"
        order          "MDY" (default) or "DMY": how to read ambiguous numeric dates such as 01/04/2026
        allow_no_year  accept month and day only ("Sep 12", "9/12") with year NO_YEAR; otherwise "no year" error
        today          reference date for two-digit years (tests pass a fixed date); defaults to date.today()
        serial_hint    the column holds Excel serial numbers, so any bare number in Excel's range is one
        target_tz      IANA zone of the import; zoned times and Unix timestamps are converted into it
    The datetime is always naive: in target_tz when the value carried a zone, else as written. has_time is True
    when the value had a time of day (Unix timestamps report False: they were already converted).
        parse("10/09/2026 3:45 PM EST")     → (2026-10-09 15:45, True)
        parse("2026-10-09T19:45:00Z")       → (2026-10-09 15:45, True)    UTC → New York
        parse("Sep 12", allow_no_year=True) → (0004-09-12 00:00, False)
        parse("October 2026")               → DateError("month and year but no day")
    """
    today = today or date.today()
    s, src_tz = _prep(value)
    if not s:
        raise DateError("empty")
    dt, has_time = _parse_prepped(s, order, allow_no_year, today, serial_hint, target_tz)
    # Only real moments are converted: a value with a time and a real year (month/day-only values aren't moments).
    if has_time and dt.year != NO_YEAR:
        dt = _to_zone(dt, src_tz, target_tz)
    return dt, has_time


def _parse_prepped(s: str, order: str, allow_no_year: bool, today: date, serial_hint: bool,
                   target_tz: str) -> tuple[datetime, bool]:
    """Parse text already cleaned by _prep. Returns (naive datetime, has_time) or raises DateError.

    Decision order:
      1. "09.12" in a birthday-type column → September 12, no year (a dotted pair isn't read as a decimal).
      2. A decimal number → Excel serial with a time of day: "46304.65625" → 2026-10-09 15:45.
      3. Digits only → _compact (Excel serial, Unix epoch, compact date).
      4. Otherwise take the time out (_extract_time), split the rest into words and numbers, ignore label words
         (birthday-type columns only), reject unknown words and more than one month name, then:
           - a trailing HHMM after a complete date is the time: "10/09/2026 1545" → 15:45
           - month name + 1 number: the day, no year ("Sep 12"); + 2 numbers: day and year in either order
             ("oct 9 2026", "9 oct 2026", "2026 oct 9", "09-oct-26")
           - 3 numbers: year first when the first has 4 digits (2026-10-09), else month/day in column order and
             the year last (10/09/2026, 10/09/26); a 0000 year means "no year" in birthday-type columns
           - 2 numbers: month and day only ("9/12"); with a 4-digit one it's "month and year but no day"
           - 1 number: _compact
    Errors: "unrecognized word 'x'", "more than one month name", "month name but no day", "month and year but
    no day", "no year", "too many numbers", "unrecognized date", plus those of _make, _md, _compact and
    _extract_time.
    """

    # Whole value is a number: Excel serial / epoch / compact digits.
    # Regex: the whole value is 1–2 digits, a dot, 1–2 digits. Checked before the decimal rule, which would take
    # "09.12" for a number.
    if allow_no_year and re.fullmatch(r"\d{1,2}\.\d{1,2}", s):  # 09.12 in a birthday column = Sep 12
        mm, dd = _md(int(s.split(".")[0]), int(s.split(".")[1]), order)
        return _make(NO_YEAR, mm, dd), False
    # Excel serial whose fraction of a day is the time; a fraction means the value had a time.
    if _DECIMAL_RE.match(s):
        n = float(s)
        if 20_000 <= n <= 80_000 or (serial_hint and 1 <= n <= 2_958_465):
            dt = _EXCEL_EPOCH + timedelta(days=n)
            return dt.replace(second=0, microsecond=0), (n % 1) != 0
        raise DateError("number is not a recognizable date")
    if _DIGITS_RE.match(s):
        dt = _compact(s, order, today, allow_no_year, serial_hint, target_tz)
        return dt, bool(len(s) in (12, 14) and (dt.hour or dt.minute))  # epochs are already in the target zone

    s, hh, mi = _extract_time(s)
    # Tokens: runs of letters (words) or of digits (numbers); separators (/ - . : spaces) only divide them.
    tokens = re.findall(r"[a-z]+|\d+", s)
    words = [t for t in tokens if not t.isdigit()]
    nums = [t for t in tokens if t.isdigit()]
    months = [MONTHS[w] for w in words if w in MONTHS]
    if allow_no_year:
        # Labels and year placeholders: "Birthday: 9/12", "DOB 9/12", "09/12/XXXX", "YYYY-09-12".
        # Regex "x+|y+": year placeholders such as "xxxx" or "yyyy".
        words = [w for w in words if w not in _LABEL_WORDS and not re.fullmatch(r"x+|y+", w)]
    unknown = [w for w in words if w not in MONTHS]
    if unknown:
        raise DateError(f"unrecognized word '{unknown[0]}'")
    if len(months) > 1:
        raise DateError("more than one month name")

    # A trailing HHMM after a complete date: "10/09/2026 1545".
    # Applies when there's exactly one number more than a full date needs (3 numbers, or 2 with a month name) and it
    # has 3–4 digits that make a valid HH:MM.
    if hh is None and len(nums) == (2 if months else 3) + 1 and len(nums[-1]) in (3, 4):
        t = nums[-1].zfill(4)
        if int(t[:2]) <= 23 and int(t[2:]) <= 59:
            hh, mi = int(t[:2]), int(t[2:])
            nums = nums[:-1]

    has_time = hh is not None
    hh, mi = hh or 0, mi or 0

    if months:
        m = months[0]
        if not nums:
            raise DateError("month name but no day")
        if len(nums) == 1:
            if len(nums[0]) == 4:
                raise DateError("month and year but no day")
            if not allow_no_year:
                raise DateError("no year")
            return _make(NO_YEAR, m, int(nums[0])), False
        if len(nums) > 2:
            raise DateError("too many numbers")
        # Which number is the year: the 4-digit one, else the one over 31, else the second ("oct 9 26" → 2026).
        a, b = nums
        if len(a) == 4 and len(b) <= 2:
            year_txt, day = a, int(b)
        elif len(b) == 4 and len(a) <= 2:
            year_txt, day = b, int(a)
        elif int(a) > 31 and int(b) <= 31:
            year_txt, day = a, int(b)
        else:
            year_txt, day = b, int(a)
        return _make(_year(year_txt, m, day, today), m, day, hh, mi), has_time

    if len(nums) == 3:
        a, b, c = nums
        if len(a) == 4:  # year first
            mm, dd = int(b), int(c)
            # ISO order is year-month-day; swap only when the month can't be one (2026-15-03).
            if mm > 12 and dd <= 12:
                mm, dd = dd, mm
            year = int(a)
            if year == 0 and allow_no_year:  # 0000-09-12: month and day only
                return _make(NO_YEAR, mm, dd), False
            return _make(year, mm, dd, hh, mi), has_time
        # Month/day first, year last: month/day in column order, two-digit years via _year.
        mm, dd = _md(int(a), int(b), order)
        if allow_no_year and len(c) == 4 and int(c) == 0:  # 09/12/0000
            return _make(NO_YEAR, mm, dd), False
        return _make(_year(c, mm, dd, today), mm, dd, hh, mi), has_time
    if len(nums) == 2:
        a, b = nums
        if len(a) == 4 or len(b) == 4:
            raise DateError("month and year but no day")
        if not allow_no_year:
            raise DateError("no year")
        mm, dd = _md(int(a), int(b), order)
        return _make(NO_YEAR, mm, dd), False
    if len(nums) == 1:
        return _compact(nums[0], order, today, allow_no_year, serial_hint, target_tz), False
    raise DateError("unrecognized date")
