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
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

NO_YEAR = 4  # marker year for "month and day only" (a leap year, so Feb 29 is valid)
MIN_YEAR, MAX_YEAR = 1900, 2100
_EXCEL_EPOCH = datetime(1899, 12, 30)

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "febr": 2, "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9,
    "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
    # Spanish
    "ene": 1, "enero": 1, "febrero": 2, "marzo": 3, "abr": 4, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
    "ago": 8, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "dic": 12,
    "diciembre": 12,
}
WEEKDAYS = ("monday|mon|tuesday|tues|tue|wednesday|weds|wed|thursday|thurs|thur|thu|friday|fri|saturday|sat|"
            "sunday|sun|lunes|martes|miercoles|jueves|viernes|sabado|domingo")
_FILLER = r"\b(of|the|at|on|de|del|en|el|hrs|hours|h)\b"
_FIXED_ZONES = {"utc": 0, "gmt": 0, "z": 0, "ast": -4, "adt": -3}
# US zone abbreviations are read as the region's local time (people write "EST" year-round to mean Eastern).
_NAMED_ZONES = {"et": "America/New_York", "est": "America/New_York", "edt": "America/New_York",
                "ct": "America/Chicago", "cst": "America/Chicago", "cdt": "America/Chicago",
                "mt": "America/Denver", "mst": "America/Denver", "mdt": "America/Denver",
                "pt": "America/Los_Angeles", "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles",
                "akst": "America/Anchorage", "akdt": "America/Anchorage", "hst": "Pacific/Honolulu"}
_TZ_NAMES_RE = re.compile(r"\b(" + "|".join(sorted(list(_FIXED_ZONES) + list(_NAMED_ZONES), key=len, reverse=True))
                          + r")\b")
DEFAULT_TZ = "America/New_York"
_ZONE_WORDS = {"eastern": "America/New_York", "central": "America/Chicago", "mountain": "America/Denver",
               "pacific": "America/Los_Angeles", "alaska": "America/Anchorage", "hawaii": "Pacific/Honolulu",
               "arizona": "America/Phoenix"}
_ZONE_WORDS_RE = re.compile(r"\b(" + "|".join(_ZONE_WORDS) + r")(?:\s+(?:standard|daylight|prevailing))?(?:\s+time)?\b")


def _is_zone_text(text: str) -> bool:
    t = text.strip().lower()
    return bool(_TZ_NAMES_RE.fullmatch(t) or _ZONE_WORDS_RE.fullmatch(t))

_TIME_RE = re.compile(
    r"(?<![\d/.\-])(\d{1,2})[:h](\d{2})(?::(\d{2})(?:[.,]\d{1,9})?)?\s*(am|pm)?(?!\d)")
_TIME_AMPM_RE = re.compile(r"(?<![\d/:\-])(\d{1,2})(?:\.(\d{2}))?\s*(am|pm)\b")
_DIGITS_RE = re.compile(r"^\d+$")
_DECIMAL_RE = re.compile(r"^\d+\.\d+$")


class DateError(ValueError):
    pass


def _offset(sign: str, hh: str, mm: str | None) -> tzinfo:
    delta = timedelta(hours=int(hh), minutes=int(mm or 0))
    return timezone(-delta if sign == "-" else delta)


_LABEL_WORDS = {"birthday", "bday", "dob", "born", "birth", "date", "of", "anniversary", "anniv", "wedding", "b",
                "day", "unknown", "year", "n", "a", "na"}


def _prep(value: str) -> tuple[str, tzinfo | None]:
    """Normalize the text and pull out any time zone (returned separately)."""
    v = unicodedata.normalize("NFKC", value)
    v = v.replace("\u200b", "").replace("\ufeff", "").strip()
    m = re.match(r'^=?"(.*)"$', v)
    if m:  # Excel text-protection wrapper ="10/09/2026"
        v = m.group(1)
    v = v.strip().lstrip("'").strip().lower()
    v = re.sub(r"[\u2010-\u2015\u2212]", "-", v)          # unicode dashes
    if re.fullmatch(r"\d{5},\d+", v):                      # Excel serial with a decimal comma
        v = v.replace(",", ".")
    # Zone in parentheses: "(EST)", "(Eastern Daylight Time)" → keep the zone, drop the brackets.
    v = re.sub(r"\(([^)]*)\)", lambda m: f" {m.group(1)} " if _is_zone_text(m.group(1)) else " ", v)
    m = re.match(r"^(\d{8})t(\d{4}(?:\d{2})?)(.*)$", v)    # basic ISO 20261009T154500Z
    if m:
        t = m.group(2)
        v = f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]} {t[:2]}:{t[2:4]}" + (f":{t[4:]}" if len(t) > 4 else "") + m.group(3)
    v = re.sub(r"(\d{4}-\d{1,2}-\d{1,2})t(\d)", r"\1 \2", v)  # ISO "T"
    tz: tzinfo | None = None
    m = _ZONE_WORDS_RE.search(v)                            # "Eastern", "Pacific Time", "Central Daylight Time"
    if m:
        tz = ZoneInfo(_ZONE_WORDS[m.group(1)])
        v = v[: m.start()] + " " + v[m.end():]
    if re.search(r"\d(?:\.\d+)?z\b", v):                   # ISO "Z"
        tz = tz or timezone.utc
        v = re.sub(r"(\d)z\b", r"\1", v)
    # numeric UTC offsets after a time: 15:45:00+05:30, 15:45 -0400, 3:45 pm gmt-0400
    m = re.search(r"(\d{1,2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?(?:\s*[ap]\.?m\.?)?)\s*(?:gmt|utc)?\s*([+-])(\d{2})(?::?(\d{2}))?\b", v)
    if m:
        tz = _offset(m.group(2), m.group(3), m.group(4))
        v = v[: m.start()] + m.group(1) + v[m.end():]
    m = re.search(r"\b(?:gmt|utc)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?\b", v)
    if m:
        tz = _offset(m.group(1), m.group(2), m.group(3))
        v = v[: m.start()] + " " + v[m.end():]
    m = _TZ_NAMES_RE.search(v)
    if m:
        name = m.group(1)
        if tz is None:
            tz = ZoneInfo(_NAMED_ZONES[name]) if name in _NAMED_ZONES else timezone(timedelta(hours=_FIXED_ZONES[name]))
        v = _TZ_NAMES_RE.sub(" ", v)
    v = re.sub(r"\b12\s*noon\b|\bnoon\b", " 12:00 pm ", v)
    v = re.sub(r"\b12\s*midnight\b|\bmidnight\b", " 12:00 am ", v)
    v = re.sub(r"\b([ap])\.\s?m\.?(?=\W|$)", r"\1m", v)    # a.m. / p.m.
    v = re.sub(r"(\d)\s*([ap])(?![a-z])", r"\1 \2m", v)     # 3:45p → 3:45 pm
    v = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", v)           # ordinals
    v = re.sub(r"'(\d{2})\b", r"\1", v)                      # '26
    v = re.sub(r"\b(" + WEEKDAYS + r")\b\.?", " ", v)
    v = v.replace("@", " ")
    v = re.sub(_FILLER, " ", v)
    v = re.sub(r"[,;]", " ", v)
    return re.sub(r"\s+", " ", v).strip(), tz


def _to_zone(dt: datetime, src: tzinfo | None, target: str) -> datetime:
    """Convert an aware moment to the import's zone (naive result). No source zone → unchanged."""
    if src is None:
        return dt
    return dt.replace(tzinfo=src).astimezone(ZoneInfo(target)).replace(tzinfo=None)


def _year(text: str, month: int, day: int, today: date) -> int:
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
    if not 1 <= m <= 12:
        raise DateError("month is not 1–12")
    try:
        return datetime(y, m, d, hh, mi)
    except (ValueError, OverflowError) as exc:  # OverflowError: e.g. day 51694619661 from "1 51694619661"
        raise DateError("not a real calendar date") from exc


def _md(a: int, b: int, order: str) -> tuple[int, int]:
    """Month/day from two numbers, using the column order unless the values only fit one way."""
    m, d = (b, a) if order == "DMY" else (a, b)
    if m > 12 and d <= 12:
        m, d = d, m
    if m > 12:
        raise DateError("no month in 1–12")
    return m, d


def _compact(digits: str, order: str, today: date, allow_no_year: bool, serial_hint: bool,
             target_tz: str = DEFAULT_TZ) -> datetime:
    n = len(digits)
    val = int(digits)
    if n == 5 or (serial_hint and n <= 7):
        if 20_000 <= val <= 80_000 or (serial_hint and 1 <= val <= 2_958_465):
            return _EXCEL_EPOCH + timedelta(days=val)
    if n == 10 and 946_684_800 <= val <= 4_102_444_800:  # Unix seconds (a moment in UTC)
        return datetime.fromtimestamp(val, tz=ZoneInfo(target_tz)).replace(tzinfo=None, second=0)
    if n == 13 and 946_684_800_000 <= val <= 4_102_444_800_000:  # Unix milliseconds
        return datetime.fromtimestamp(val / 1000, tz=ZoneInfo(target_tz)).replace(tzinfo=None, second=0,
                                                                                   microsecond=0)
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
        for (m, d), ytxt in ((first, c), ((int(b), int(c)), a)):
            try:
                return _make(_year(ytxt, m, d, today), m, d)
            except DateError:
                continue
        raise DateError("6-digit number is not a date")
    if n in (12, 14):
        y, m, d, hh, mi = int(digits[:4]), int(digits[4:6]), int(digits[6:8]), int(digits[8:10]), int(digits[10:12])
        if hh > 23 or mi > 59:
            raise DateError("invalid time")
        return _make(y, m, d, hh, mi)
    if n in (3, 4) and allow_no_year and not (MIN_YEAR <= val <= MAX_YEAR):
        digits = digits.zfill(4)
        m, d = _md(int(digits[:2]), int(digits[2:]), order)
        return _make(NO_YEAR, m, d)
    if n == 4 and MIN_YEAR <= val <= MAX_YEAR:
        raise DateError("only a year — no month or day")
    raise DateError("number is not a recognizable date")


def _extract_time(s: str) -> tuple[str, int | None, int | None]:
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
    """Return (datetime, has_time). Month/day-only values get year NO_YEAR. Raises DateError."""
    today = today or date.today()
    s, src_tz = _prep(value)
    if not s:
        raise DateError("empty")
    dt, has_time = _parse_prepped(s, order, allow_no_year, today, serial_hint, target_tz)
    if has_time and dt.year != NO_YEAR:
        dt = _to_zone(dt, src_tz, target_tz)
    return dt, has_time


def _parse_prepped(s: str, order: str, allow_no_year: bool, today: date, serial_hint: bool,
                   target_tz: str) -> tuple[datetime, bool]:

    # Whole value is a number: Excel serial / epoch / compact digits.
    if allow_no_year and re.fullmatch(r"\d{1,2}\.\d{1,2}", s):  # 09.12 in a birthday column = Sep 12
        mm, dd = _md(int(s.split(".")[0]), int(s.split(".")[1]), order)
        return _make(NO_YEAR, mm, dd), False
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
    tokens = re.findall(r"[a-z]+|\d+", s)
    words = [t for t in tokens if not t.isdigit()]
    nums = [t for t in tokens if t.isdigit()]
    months = [MONTHS[w] for w in words if w in MONTHS]
    if allow_no_year:
        # Labels and year placeholders: "Birthday: 9/12", "DOB 9/12", "09/12/XXXX", "YYYY-09-12".
        words = [w for w in words if w not in _LABEL_WORDS and not re.fullmatch(r"x+|y+", w)]
    unknown = [w for w in words if w not in MONTHS]
    if unknown:
        raise DateError(f"unrecognized word '{unknown[0]}'")
    if len(months) > 1:
        raise DateError("more than one month name")

    # A trailing HHMM after a complete date: "10/09/2026 1545".
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
            if mm > 12 and dd <= 12:
                mm, dd = dd, mm
            year = int(a)
            if year == 0 and allow_no_year:  # 0000-09-12: month and day only
                return _make(NO_YEAR, mm, dd), False
            return _make(year, mm, dd, hh, mi), has_time
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
