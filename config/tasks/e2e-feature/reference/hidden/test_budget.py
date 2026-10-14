"""Hidden: the `budget` command group."""

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from datetime import date  # noqa: E402

from ledger.cli import main  # noqa: E402
from ledger.models import Transaction  # noqa: E402
from ledger.recurring import Recurrence  # noqa: E402
from ledger.storage import LedgerStore  # noqa: E402


def store(tmp_path, txns=(), rows=(), budgets=None):
    st = LedgerStore(tmp_path / "ledger.csv")
    if txns:
        st.save(list(txns))
    if rows:
        st.save_recurring(list(rows))
    if budgets is not None:
        st.save_budgets(budgets)
    return st


def run(tmp_path, *args):
    return main(["--ledger", str(tmp_path / "ledger.csv"), *args])


def jan(day, category, amount):
    return Transaction("tx", "checking", category, amount, "", date(2026, 1, day))


def test_budget_set_prints_and_persists(tmp_path, capsys):
    store(tmp_path)
    rc = run(tmp_path, "budget", "set", "--category", "groceries", "--amount", "200")
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == "set groceries 200.00"
    assert store(tmp_path).load_budgets() == {"groceries": 20000}


def test_budget_set_upserts(tmp_path, capsys):
    store(tmp_path, budgets={"groceries": 20000})
    run(tmp_path, "budget", "set", "--category", "groceries", "--amount", "300.50")
    assert store(tmp_path).load_budgets() == {"groceries": 30050}


def test_budget_list_sorted(tmp_path, capsys):
    store(tmp_path, budgets={"rent": 120000, "groceries": 20000})
    rc = run(tmp_path, "budget", "list")
    out = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert out == ["groceries 200.00", "rent 1200.00"]


def test_budget_list_empty(tmp_path, capsys):
    store(tmp_path)
    assert run(tmp_path, "budget", "list") == 0
    assert capsys.readouterr().out.strip() == "(no budgets)"


def test_budget_clear(tmp_path, capsys):
    store(tmp_path, budgets={"a": 100, "b": 200})
    rc = run(tmp_path, "budget", "clear", "--category", "a")
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == "cleared a"
    assert store(tmp_path).load_budgets() == {"b": 200}


def test_budget_clear_absent_category_still_succeeds(tmp_path, capsys):
    store(tmp_path)
    assert run(tmp_path, "budget", "clear", "--category", "nope") == 0
    assert capsys.readouterr().out.strip() == "cleared nope"


def test_budget_check_empty(tmp_path, capsys):
    store(tmp_path, txns=[jan(3, "groceries", -5000)])
    rc = run(tmp_path, "budget", "check")
    assert rc == 0
    assert capsys.readouterr().out.strip() == "(no budgets)"


def test_budget_check_ok(tmp_path, capsys):
    store(tmp_path, txns=[jan(3, "groceries", -5000)], budgets={"groceries": 20000})
    rc = run(tmp_path, "budget", "check")
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == "groceries -50.00/200.00  OK"


def test_budget_check_over_returns_policy_exit_code(tmp_path, capsys):
    store(tmp_path, txns=[jan(3, "groceries", -132450)], budgets={"groceries": 120000})
    rc = run(tmp_path, "budget", "check")
    out = capsys.readouterr().out
    assert rc == 2, "an over-budget result must exit 2 (policy violation)"
    assert out.strip() == "groceries -1324.50/1200.00  OVER"


def test_budget_check_exactly_at_limit_is_ok(tmp_path, capsys):
    store(tmp_path, txns=[jan(3, "groceries", -20000)], budgets={"groceries": 20000})
    rc = run(tmp_path, "budget", "check")
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == "groceries -200.00/200.00  OK"


def test_budget_check_ignores_positive_amounts(tmp_path, capsys):
    store(tmp_path, txns=[jan(3, "groceries", -10000), jan(4, "groceries", 15000)],
          budgets={"groceries": 20000})
    rc = run(tmp_path, "budget", "check")
    out = capsys.readouterr().out
    assert rc == 0, "a refund must not make a category look over budget"
    assert out.strip() == "groceries -100.00/200.00  OK"


def test_budget_check_sorted_and_reports_each_category(tmp_path, capsys):
    store(tmp_path, txns=[jan(3, "rent", -150000), jan(4, "groceries", -5000)],
          budgets={"rent": 120000, "groceries": 20000})
    rc = run(tmp_path, "budget", "check")
    out = capsys.readouterr().out.splitlines()
    assert rc == 2
    assert [l.split()[0] for l in out] == ["groceries", "rent"]
    assert out[1] == "rent -1500.00/1200.00  OVER"


def test_projected_budget_includes_forecast(tmp_path, capsys):
    store(
        tmp_path,
        txns=[jan(2, "coffee", -350)],
        rows=[Recurrence("r1", "checking", "coffee", -350, "daily coffee",
                         date(2026, 1, 2), "daily", None)],
        budgets={"coffee": 500},
    )

    assert run(tmp_path, "budget", "check") == 0
    assert capsys.readouterr().out.strip() == "coffee -3.50/5.00  OK"

    rc = run(tmp_path, "budget", "check", "--projected",
             "--from", "2026-01-02", "--to", "2026-01-04")
    out = capsys.readouterr().out
    assert rc == 2, "forecasted spend must push the category over"
    assert out.strip() == "coffee -14.00/5.00  OVER"


def test_projected_window_excludes_dates_outside(tmp_path, capsys):
    store(
        tmp_path,
        rows=[Recurrence("r1", "checking", "coffee", -350, "",
                         date(2026, 1, 2), "daily", None)],
        budgets={"coffee": 1000},
    )
    rc = run(tmp_path, "budget", "check", "--projected",
             "--from", "2026-01-02", "--to", "2026-01-02")
    out = capsys.readouterr().out
    assert rc == 0, "only occurrences inside the window may be counted"
    assert out.strip() == "coffee -3.50/10.00  OK"


def test_projected_never_subtracts_income(tmp_path, capsys):
    store(
        tmp_path,
        rows=[Recurrence("r1", "checking", "salary", 250000, "",
                         date(2026, 1, 2), "monthly", None)],
        budgets={"salary": 100},
    )
    rc = run(tmp_path, "budget", "check", "--projected",
             "--from", "2026-01-01", "--to", "2026-01-31")
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == "salary 0.00/1.00  OK"


def test_budget_check_without_ledger_file(tmp_path, capsys):
    store(tmp_path, budgets={"x": 100})
    rc = run(tmp_path, "budget", "check")
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == "x 0.00/1.00  OK"


def test_budget_set_missing_amount_is_a_usage_error(tmp_path, capsys):
    store(tmp_path)
    rc = run(tmp_path, "budget", "set", "--category", "x")
    assert rc == 1
