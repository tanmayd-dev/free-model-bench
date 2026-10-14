"""Hidden: the `forecast` command."""

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from datetime import date  # noqa: E402

from ledger.cli import main  # noqa: E402
from ledger.recurring import Recurrence  # noqa: E402
from ledger.storage import LedgerStore  # noqa: E402


def store(tmp_path, rows=()):
    st = LedgerStore(tmp_path / "ledger.csv")
    if rows:
        st.save_recurring(list(rows))
    return st


def run(tmp_path, *args):
    return main(["--ledger", str(tmp_path / "ledger.csv"), *args])


def R(**kw):
    base = dict(id="r1", account="checking", category="rent", amount=-120000,
                memo="monthly rent", anchor=date(2026, 1, 1), frequency="monthly")
    base.update(kw)
    return Recurrence(**base)


def test_no_recurring_entries_message(tmp_path, capsys):
    store(tmp_path)
    rc = run(tmp_path, "forecast", "--from", "2026-01-01", "--to", "2026-12-31")
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == "no recurring entries"


def test_forecast_line_format_is_exact(tmp_path, capsys):
    store(tmp_path, [R()])
    rc = run(tmp_path, "forecast", "--from", "2026-02-01", "--to", "2026-02-01")
    out = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert out == ["2026-02-01  checking  rent           -1200.00  monthly rent"]


def test_forecast_window_is_inclusive_at_both_ends(tmp_path, capsys):
    store(tmp_path, [R()])
    run(tmp_path, "forecast", "--from", "2026-01-15", "--to", "2026-03-15")
    dates = [l.split()[0] for l in capsys.readouterr().out.splitlines()]
    assert dates == ["2026-01-15", "2026-02-15", "2026-03-15"]


def test_forecast_window_excludes_dates_outside(tmp_path, capsys):
    store(tmp_path, [R()])
    run(tmp_path, "forecast", "--from", "2026-01-16", "--to", "2026-03-14")
    dates = [l.split()[0] for l in capsys.readouterr().out.splitlines()]
    assert dates == ["2026-02-15"]


def test_forecast_months_defaults_to_three_inclusive(tmp_path, capsys):
    store(tmp_path, [R(anchor=date(2026, 1, 1), frequency="daily",
                       category="x", amount=-1, memo="")])
    run(tmp_path, "forecast", "--from", "2026-01-01", "--months", "3")
    dates = [l.split()[0] for l in capsys.readouterr().out.splitlines()]
    assert dates[0] == "2026-01-01"
    assert dates[-1] == "2026-04-01", "--months window must be inclusive"
    assert len(dates) == 31 + 28 + 31 + 1


def test_forecast_sorted_by_date_then_id(tmp_path, capsys):
    store(tmp_path, [
        R(id="zz", frequency="daily", anchor=date(2026, 1, 1),
          category="b", amount=-100, memo="b"),
        R(id="aa", frequency="daily", anchor=date(2026, 1, 1),
          category="a", amount=-200, memo="a"),
    ])
    run(tmp_path, "forecast", "--from", "2026-01-01", "--to", "2026-01-01")
    lines = capsys.readouterr().out.splitlines()
    assert [l.split()[2] for l in lines] == ["a", "b"], "sorted by (date, id)"


def test_forecast_does_not_deduplicate(tmp_path, capsys):
    store(tmp_path, [R(id="a"), R(id="b")])
    run(tmp_path, "forecast", "--from", "2026-01-01", "--to", "2026-01-01")
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2


def test_forecast_handles_month_end_clamping(tmp_path, capsys):
    store(tmp_path, [R(anchor=date(2026, 1, 31))])
    run(tmp_path, "forecast", "--from", "2026-01-01", "--to", "2026-04-30")
    dates = [l.split()[0] for l in capsys.readouterr().out.splitlines()]
    assert dates == ["2026-01-31", "2026-02-28", "2026-03-31", "2026-04-30"]


def test_forecast_csv_format(tmp_path, capsys):
    store(tmp_path, [R()])
    run(tmp_path, "forecast", "--from", "2026-02-01", "--to", "2026-02-01",
        "--format", "csv")
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "date,account,category,amount,memo"
    assert lines[1] == "2026-02-01,checking,rent,-1200.00,monthly rent"


def test_forecast_empty_window_prints_nothing(tmp_path, capsys):
    store(tmp_path, [R()])
    rc = run(tmp_path, "forecast", "--from", "2026-06-01", "--to", "2026-05-01")
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == ""


def test_forecast_bad_date_returns_error(tmp_path, capsys):
    store(tmp_path, [R()])
    rc = run(tmp_path, "forecast", "--from", "2026-13-45")
    assert rc == 1


def test_forecast_bad_format_returns_error(tmp_path, capsys):
    store(tmp_path, [R()])
    rc = run(tmp_path, "forecast", "--from", "2026-01-01", "--format", "json")
    assert rc == 1


def test_forecast_reads_only_the_named_ledger(tmp_path, capsys):
    store(tmp_path, [R()])
    rc = run(tmp_path, "forecast", "--from", "2026-02-01", "--to", "2026-02-01")
    assert rc == 0
