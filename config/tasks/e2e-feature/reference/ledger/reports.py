"""Aggregation / reporting over a list of transactions."""

from __future__ import annotations

from datetime import date

from ledger.dates import add_months
from ledger.models import Transaction


def by_category(txns: list[Transaction]) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in txns:
        out[t.category] = out.get(t.category, 0) + t.amount
    return dict(sorted(out.items()))


def income(txns: list[Transaction]) -> int:
    return sum(t.amount for t in txns if t.amount > 0)


def expense(txns: list[Transaction]) -> int:
    return sum(t.amount for t in txns if t.amount < 0)


def summary(txns: list[Transaction]) -> dict:
    return {
        "count": len(txns),
        "income": income(txns),
        "expense": expense(txns),
        "net": income(txns) + expense(txns),
        "categories": by_category(txns),
    }


def for_month(txns: list[Transaction], year: int, month: int) -> list[Transaction]:
    return [t for t in txns if t.occurred.year == year and t.occurred.month == month]


def rollup(txns: list[Transaction], months: int, anchor: date) -> list[dict]:
    """Monthly totals for ``months`` months ending at ``anchor`` (newest first).

    ``anchor`` is expected to be the first of a month, which is why the
    approximate ``add_months`` in ledger.dates is good enough here.
    """
    out = []
    for i in range(months):
        m = add_months(anchor, -i)
        rows = for_month(txns, m.year, m.month)
        out.append(
            {
                "label": f"{m.year:04d}-{m.month:02d}",
                "income": income(rows),
                "expense": expense(rows),
                "net": income(rows) + expense(rows),
                "count": len(rows),
            }
        )
    return out
