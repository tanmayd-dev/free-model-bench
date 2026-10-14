"""Recurring transactions and their date arithmetic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterator

from ledger.models import ValidationError

FREQUENCIES = ("daily", "weekly", "monthly")


@dataclass(frozen=True)
class Recurrence:
    id: str
    account: str
    category: str
    amount: int  # cents; negative = expense
    memo: str
    anchor: date
    frequency: str
    end: date | None = None


def shift_months(d: date, n: int) -> date:
    """Month rule: preserve day-of-month, else clamp to the target month's end.

    Always computed from ``d`` -- never accumulated from a previous result.
    """
    month_index = d.month - 1 + n
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    first_next = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last_day = (first_next - timedelta(days=1)).day
    return date(year, month, min(d.day, last_day))


def nth_occurrence(anchor: date, frequency: str, n: int) -> date:
    if frequency == "daily":
        return anchor + timedelta(days=n)
    if frequency == "weekly":
        return anchor + timedelta(days=7 * n)
    if frequency == "monthly":
        return shift_months(anchor, n)
    raise ValidationError(
        f"unknown frequency {frequency!r}, expected one of {', '.join(FREQUENCIES)}"
    )


def _max_n(anchor: date, frequency: str, end: date) -> int:
    """Upper bound on n such that ``nth_occurrence`` can still be <= end."""
    if end < anchor:
        return -1
    if frequency == "monthly":
        return (end.year - anchor.year) * 12 + (end.month - anchor.month) + 2
    span = (end - anchor).days
    return (span // 7 if frequency == "weekly" else span) + 2


def iter_occurrences(r: Recurrence, start: date, end: date) -> Iterator[date]:
    if r.frequency not in FREQUENCIES:
        raise ValidationError(
            f"unknown frequency {r.frequency!r}, "
            f"expected one of {', '.join(FREQUENCIES)}"
        )
    if start > end:
        return
    ceiling = min(end, r.end) if r.end is not None else end
    if ceiling < start:
        return
    # n is bounded relative to the anchor, so a window that starts long after
    # the anchor still walks forward to the first occurrence inside it.
    last_n = max(_max_n(r.anchor, r.frequency, ceiling), 0)
    for n in range(0, last_n + 1):
        d = nth_occurrence(r.anchor, r.frequency, n)
        if d > ceiling:
            break
        if d >= start:
            yield d
