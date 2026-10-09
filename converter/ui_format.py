"""Small display helpers shared by the web UI (times, durations, status labels)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .storage import ImportRecord, parse_ts

STATUS_LABELS = {
    "queued": ("Queued", "⏳", "gray"),
    "running": ("Running", "🔵", "blue"),
    "completed": ("Completed", "🟢", "green"),
    "failed": ("Failed", "🔴", "red"),
    "cancelled": ("Cancelled", "⚪", "gray"),
    "interrupted": ("Interrupted", "🟠", "orange"),
}


def status_text(rec: ImportRecord) -> str:
    label, icon, _ = STATUS_LABELS.get(rec.status, (rec.status, "", "gray"))
    if rec.status == "completed" and rec.rejected:
        return f"{icon} {label} · ⚠ {rec.rejected:,} bad"
    return f"{icon} {label}"


def status_color(status: str) -> str:
    return STATUS_LABELS.get(status, ("", "", "gray"))[2]


def fmt_dt(value: str | None, tz: str, with_date: bool = True) -> str:
    dt = parse_ts(value)
    if dt is None:
        return ""
    try:
        local = dt.astimezone(ZoneInfo(tz))
    except Exception:
        local = dt.astimezone(timezone.utc)
    now = datetime.now(local.tzinfo)
    clock = f"{local.hour % 12 or 12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"  # portable (no %-I)
    if not with_date:
        return clock
    if local.date() == now.date():
        return f"Today {clock}"
    day = f"{local:%b} {local.day}" + ("" if local.year == now.year else f" {local.year}")
    return f"{day}, {clock}"


def fmt_duration(td: timedelta | None) -> str:
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
    if td is None:
        return "estimating…"
    s = td.total_seconds()
    if s < 10:
        return "a few seconds"
    if s < 60:
        return f"about {int(round(s, -1))}s"
    return f"about {int(round(s / 60))} min"


def fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"
