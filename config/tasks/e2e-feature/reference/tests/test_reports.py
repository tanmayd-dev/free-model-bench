from datetime import date

from ledger.models import Transaction
from ledger.reports import by_category, expense, for_month, income, rollup, summary


def T(cat, amount, day, account="checking"):
    return Transaction("tx", account, cat, amount, "", date(2026, 1, day))


def test_by_category_sums_signed_amounts():
    txns = [T("groceries", -100, 3), T("groceries", -50, 4), T("salary", 1000, 5)]
    assert by_category(txns) == {"groceries": -150, "salary": 1000}


def test_income_and_expense_split():
    txns = [T("salary", 1000, 1), T("rent", -700, 2), T("refund", 50, 3)]
    assert income(txns) == 1050
    assert expense(txns) == -700


def test_summary_totals():
    txns = [T("salary", 1000, 1), T("rent", -700, 2)]
    s = summary(txns)
    assert s["count"] == 2
    assert s["net"] == 300
    assert s["categories"] == {"rent": -700, "salary": 1000}


def test_for_month_filters_by_year_and_month():
    txns = [
        T("a", -1, 1),
        Transaction("tx", "checking", "b", -1, "", date(2026, 2, 1)),
        Transaction("tx", "checking", "c", -1, "", date(2025, 1, 1)),
    ]
    assert [t.category for t in for_month(txns, 2026, 1)] == ["a"]


def test_rollup_returns_newest_first():
    txns = [
        T("a", -100, 1),
        Transaction("tx", "checking", "b", -200, "", date(2025, 12, 15)),
        Transaction("tx", "checking", "c", -300, "", date(2025, 11, 15)),
    ]
    rows = rollup(txns, 3, date(2026, 1, 1))
    assert [r["label"] for r in rows] == ["2026-01", "2025-12", "2025-11"]
    assert [r["net"] for r in rows] == [-100, -200, -300]
