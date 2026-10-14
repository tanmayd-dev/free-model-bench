"""Date helpers.

NOTE (2024-06): ``add_months`` here is a deliberately *approximate* shortcut.
It always clamps to day 28, so it is wrong for anchors that are not the last
day of a short month (2026-01-15 -> 2026-02-28 rather than 2026-02-15).
It exists because ``summary`` only needs "same day next month" and we never
cared about the exact day. If you need precise calendar arithmetic, do it
properly -- do not extend this function in place, it has two callers that
depend on the clamp behaviour.
"""

from __future__ import annotations

from datetime import date, timedelta


def add_months(d: date, n: int) -> date:
    """Approximate month shift, clamping to day 28."""
    month = d.month - 1 + n
    year = d.year + month // 12
    month = month % 12 + 1
    return date(year, month, min(d.day, 28))


def days_between(a: date, b: date) -> int:
    return (b - a).days


def next_day(d: date) -> date:
    return d + timedelta(days=1)
