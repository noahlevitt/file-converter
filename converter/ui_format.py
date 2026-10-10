"""Small display helpers shared by the web UI (times, durations, status labels).

Overview
--------
Pure formatting functions with no Streamlit dependency, so they are easy to unit-test. ``app.py`` uses them on
the Imports tracker, the import detail page, the template list and the user list:

* :data:`STATUS_LABELS`, :func:`status_text`, :func:`status_color` — how an import status is shown
  (label, emoji icon, ``st.badge`` colour).
* :func:`fmt_dt` — a stored UTC timestamp as a short local time ("Today 3:05 PM", "Oct 9, 3:05 PM").
* :func:`fmt_duration` / :func:`fmt_eta` — elapsed time and time-left estimates.
* :func:`fmt_size` — file sizes ("512 B", "2.0 KB", "5.1 MB").
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .storage import ImportRecord, parse_ts

# Import status -> (label, icon, colour). Colours are Streamlit badge colour names. Covers every status an import
# can have (see jobs.py for the lifecycle).
STATUS_LABELS = {
    "queued": ("Queued", "⏳", "gray"),
    "running": ("Running", "🔵", "blue"),
    "completed": ("Completed", "🟢", "green"),
    "failed": ("Failed", "🔴", "red"),
    "cancelled": ("Cancelled", "⚪", "gray"),
    "interrupted": ("Interrupted", "🟠", "orange"),
}


def status_text(rec: ImportRecord) -> str:
    """Status cell text for the Imports table, e.g. "🟢 Completed" or "🟢 Completed · ⚠ 12 bad".

    A completed import with rejected rows gets a warning suffix with the bad-row count, so imports that need
    attention stand out in the list. An unknown status is shown as-is without an icon.
    """
    label, icon, _ = STATUS_LABELS.get(rec.status, (rec.status, "", "gray"))
    if rec.status == "completed" and rec.rejected:
        return f"{icon} {label} · ⚠ {rec.rejected:,} bad"
    return f"{icon} {label}"


def status_color(status: str) -> str:
    """Streamlit badge colour for a status ("green" for completed, …); "gray" for unknown statuses."""
    return STATUS_LABELS.get(status, ("", "", "gray"))[2]


def fmt_dt(value: str | None, tz: str, with_date: bool = True) -> str:
    """Format a stored ISO timestamp (UTC) as a short, friendly local time.

    Args:
        value: ISO-8601 timestamp as stored in the database (e.g. "2026-10-10T17:05:00+00:00"); ``None``/"" -> "".
        tz: IANA time zone to show it in (``APP_TIMEZONE``). An invalid zone falls back to UTC instead of failing.
        with_date: ``False`` returns only the clock time.

    Examples (tz "America/New_York", today = 2026-10-10)::

        "2026-10-10T17:05:00+00:00" -> "Today 1:05 PM"
        "2026-10-09T03:00:00+00:00" -> "Oct 8, 11:00 PM"
        "2025-12-31T17:00:00+00:00" -> "Dec 31 2025, 12:00 PM"   (year shown only when it isn't this year)
        with_date=False             -> "1:05 PM"
    """
    dt = parse_ts(value)
    if dt is None:
        return ""
    try:
        local = dt.astimezone(ZoneInfo(tz))
    except Exception:
        local = dt.astimezone(timezone.utc)
    now = datetime.now(local.tzinfo)
    # 12-hour clock without a leading zero, built by hand: strftime's "%-I" doesn't work on Windows.
    clock = f"{local.hour % 12 or 12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"  # portable (no %-I)
    if not with_date:
        return clock
    if local.date() == now.date():
        return f"Today {clock}"
    day = f"{local:%b} {local.day}" + ("" if local.year == now.year else f" {local.year}")
    return f"{day}, {clock}"


def fmt_duration(td: timedelta | None) -> str:
    """Compact duration: "45s", "3m 07s", "1h 05m" (seconds dropped above an hour); ``None`` -> "".

    Negative durations (clock skew between stored timestamps) are shown as "0s".
    """
    if td is None:
        return ""
    s = max(0, int(td.total_seconds()))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def fmt_eta(td: timedelta | None) -> str:
    """Rough time-left text for a running import (from ``ImportRecord.eta``).

    Deliberately vague because the estimate is a straight-line extrapolation of progress:

    * ``None`` (not running, or under 5% progress) -> "estimating…"
    * under 10 s -> "a few seconds"
    * under a minute -> rounded to 10 s, e.g. 42 s -> "about 40s"
    * otherwise -> whole minutes, e.g. 150 s -> "about 2 min" (Python rounds halves to even)
    """
    if td is None:
        return "estimating…"
    s = td.total_seconds()
    if s < 10:
        return "a few seconds"
    if s < 60:
        return f"about {int(round(s, -1))}s"
    return f"about {int(round(s / 60))} min"


def fmt_size(n: int) -> str:
    """Human file size with 1024-based units: 512 -> "512 B", 2048 -> "2.0 KB", 5_300_000 -> "5.1 MB".

    Bytes are shown without decimals, larger units with one; GB is the largest unit (e.g. "1536.0 GB" rather
    than TB). The final ``return`` is never reached because the loop always returns at "GB"; it only keeps the
    function's return type obvious.
    """
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"
