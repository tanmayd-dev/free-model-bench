"""Hidden: recurring date arithmetic and persistence."""

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from datetime import date  # noqa: E402

import pytest  # noqa: E402

from ledger.models import ValidationError  # noqa: E402


def R(**kw):
    from ledger.recurring import Recurrence

    base = dict(
        id="r1",
        account="checking",
        category="rent",
        amount=-120000,
        memo="monthly rent",
        anchor=date(2026, 1, 1),
        frequency="monthly",
    )
    base.update(kw)
    return Recurrence(**base)


def occ(r, start, end):
    from ledger.recurring import iter_occurrences

    return list(iter_occurrences(r, start, end))


YEAR_WIDE = (date(2000, 1, 1), date(2035, 12, 31))


# ---------------------------------------------------------------- monthly ----


def test_monthly_preserves_day_when_it_exists():
    r = R(anchor=date(2026, 1, 15))
    got = occ(r, date(2026, 1, 1), date(2026, 3, 31))
    assert got == [date(2026, 1, 15), date(2026, 2, 15), date(2026, 3, 15)]


def test_monthly_clamps_to_february_in_a_common_year():
    r = R(anchor=date(2026, 1, 31))
    got = occ(r, date(2026, 1, 1), date(2026, 4, 30))
    # critically: each occurrence is computed from the ANCHOR, not accumulated
    # from the previous occurrence (which would drift to 03-28).
    assert got == [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30)]


def test_monthly_clamps_to_february_in_a_leap_year():
    r = R(anchor=date(2024, 1, 31))
    got = occ(r, date(2024, 1, 1), date(2024, 3, 31))
    assert got == [date(2024, 1, 31), date(2024, 2, 29), date(2024, 3, 31)]


def test_monthly_clamps_to_thirty_day_months():
    r = R(anchor=date(2026, 5, 31))
    got = occ(r, date(2026, 5, 1), date(2026, 9, 30))
    assert got == [date(2026, 5, 31), date(2026, 6, 30),
                   date(2026, 7, 31), date(2026, 8, 31), date(2026, 9, 30)]


def test_monthly_never_accumulates_from_the_previous_occurrence():
    r = R(anchor=date(2026, 1, 31))
    got = occ(r, date(2026, 1, 1), date(2026, 6, 30))
    assert date(2026, 3, 31) in got, "must not drift to 03-28 after clamping"
    assert date(2026, 4, 30) in got
    assert date(2026, 5, 31) in got


def test_monthly_window_start_may_be_after_the_anchor():
    r = R(anchor=date(2026, 6, 15))
    got = occ(r, date(2026, 1, 1), date(2026, 8, 31))
    assert got[0] == date(2026, 6, 15)
    assert got == [date(2026, 6, 15), date(2026, 7, 15), date(2026, 8, 15)]


def test_monthly_window_clips_without_shifting_the_sequence():
    r = R(anchor=date(2026, 1, 31))
    got = occ(r, date(2026, 2, 15), date(2026, 3, 15))
    assert got == [date(2026, 2, 28)]


# ------------------------------------------------------------ daily/weekly ----


def test_daily_yields_every_calendar_day():
    r = R(frequency="daily", anchor=date(2026, 1, 1))
    got = occ(r, date(2026, 1, 1), date(2026, 1, 3))
    assert got == [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)]


def test_daily_crosses_a_month_boundary():
    r = R(frequency="daily", anchor=date(2026, 1, 30))
    got = occ(r, date(2026, 1, 31), date(2026, 2, 2))
    assert got == [date(2026, 1, 31), date(2026, 2, 1), date(2026, 2, 2)]


def test_weekly_steps_seven_days_from_the_anchor():
    r = R(frequency="weekly", anchor=date(2026, 1, 1))
    got = occ(r, date(2026, 1, 1), date(2026, 1, 31))
    assert got == [date(2026, 1, 1), date(2026, 1, 8),
                   date(2026, 1, 15), date(2026, 1, 22), date(2026, 1, 29)]


def test_weekly_anchor_inside_the_window_is_still_respected():
    r = R(frequency="weekly", anchor=date(2026, 6, 1))
    got = occ(r, date(2026, 1, 1), date(2026, 6, 30))
    assert got == [date(2026, 6, 1), date(2026, 6, 8), date(2026, 6, 15),
                   date(2026, 6, 22), date(2026, 6, 29)]


# ------------------------------------------------------------- boundaries ----


def test_window_endpoints_are_inclusive():
    r = R(anchor=date(2026, 1, 15))
    assert occ(r, date(2026, 1, 15), date(2026, 1, 15)) == [date(2026, 1, 15)]
    assert occ(r, date(2026, 1, 16), date(2026, 2, 14)) == []


def test_empty_window_yields_nothing():
    r = R(anchor=date(2026, 1, 15))
    assert occ(r, date(2026, 3, 1), date(2026, 2, 1)) == []
    assert occ(r, date(2026, 2, 1), date(2026, 2, 1)) == []


def test_end_date_is_respected():
    r = R(anchor=date(2026, 1, 15), end=date(2026, 2, 15))
    got = occ(r, *YEAR_WIDE)
    assert got == [date(2026, 1, 15), date(2026, 2, 15)]


def test_end_date_clipped_by_window():
    r = R(anchor=date(2026, 1, 15), end=date(2026, 3, 15))
    got = occ(r, date(2026, 1, 1), date(2026, 2, 28))
    assert got == [date(2026, 1, 15), date(2026, 2, 15)]


def test_end_before_anchor_yields_nothing():
    r = R(anchor=date(2026, 1, 15), end=date(2026, 1, 1))
    assert occ(r, *YEAR_WIDE) == []


def test_results_are_ascending_and_unique():
    r = R(frequency="monthly", anchor=date(2026, 1, 31))
    got = occ(r, date(2026, 1, 1), date(2028, 12, 31))
    assert got == sorted(got)
    assert len(got) == len(set(got))
    assert len(got) == 36


def test_window_entirely_before_the_anchor():
    r = R(anchor=date(2026, 6, 15))
    assert occ(r, date(2026, 1, 1), date(2026, 6, 1)) == []


def test_unknown_frequency_raises_validation_error():
    r = R(frequency="yearly")
    with pytest.raises(ValidationError):
        occ(r, *YEAR_WIDE)


# ------------------------------------------------------------ persistence ----


def _store(tmp_path):
    from ledger.storage import LedgerStore

    return LedgerStore(tmp_path / "ledger.csv")


def test_recurring_roundtrip(tmp_path):
    from ledger.recurring import Recurrence

    st = _store(tmp_path)
    rows = [
        Recurrence("r1", "checking", "rent", -120000, "monthly rent",
                   date(2026, 1, 31), "monthly", None),
        Recurrence("r2", "savings", "transfer", -5000, "rainy day",
                   date(2026, 2, 1), "weekly", date(2026, 6, 30)),
        Recurrence("r3", "cash", "coffee", -350, "", date(2026, 3, 5), "daily",
                   date(2026, 3, 7)),
    ]
    st.save_recurring(rows)
    assert (pathlib.Path(st.dir) / "recurring.csv").exists()
    loaded = st.load_recurring()
    assert loaded == rows


def test_recurring_file_lives_beside_the_ledger_with_the_right_header(tmp_path):
    from ledger.recurring import Recurrence

    st = _store(tmp_path)
    st.save_recurring([Recurrence("r1", "checking", "rent", -120000, "m",
                                  date(2026, 1, 1), "monthly", None)])
    path = tmp_path / "recurring.csv"
    assert path.exists(), "recurring.csv must live next to ledger.csv"
    lines = path.read_text().splitlines()
    assert lines[0] == "id,date,account,category,amount,memo,frequency,end"
    assert lines[1].startswith("r1,2026-01-01,checking,rent,-1200.00")


def test_missing_recurring_file_reads_as_empty(tmp_path):
    assert _store(tmp_path).load_recurring() == []


def test_no_tmp_file_left_behind(tmp_path):
    from ledger.recurring import Recurrence

    st = _store(tmp_path)
    st.save_recurring([Recurrence("r1", "checking", "rent", -1, "",
                                  date(2026, 1, 1), "monthly", None)])
    assert not list(tmp_path.glob("*.tmp"))


def test_malformed_recurring_row_raises_validation_error(tmp_path):
    from ledger.recurring import Recurrence

    st = _store(tmp_path)
    st.save_recurring([Recurrence("r1", "checking", "rent", -1, "",
                                  date(2026, 1, 1), "monthly", None)])
    p = tmp_path / "recurring.csv"
    p.write_text(p.read_text().replace("2026-01-01", "not-a-date"))
    with pytest.raises(ValidationError, match="recurring.csv"):
        st.load_recurring()


def test_empty_end_field_is_none(tmp_path):
    from ledger.recurring import Recurrence

    st = _store(tmp_path)
    st.save_recurring([Recurrence("r1", "a", "b", -1, "", date(2026, 1, 1),
                                  "monthly", None)])
    assert st.load_recurring()[0].end is None


# ---------------------------------------------------------------- budgets ----


def test_budget_roundtrip(tmp_path):
    st = _store(tmp_path)
    st.save_budgets({"groceries": 20000, "rent": 120000})
    assert st.load_budgets() == {"groceries": 20000, "rent": 120000}


def test_budget_file_header(tmp_path):
    st = _store(tmp_path)
    st.save_budgets({"rent": 120000})
    lines = (tmp_path / "budgets.csv").read_text().splitlines()
    assert lines[0] == "category,limit"
    assert lines[1] == "rent,1200.00"


def test_missing_budget_file_reads_as_empty(tmp_path):
    assert _store(tmp_path).load_budgets() == {}


def test_budget_clear_by_saving_without_the_key(tmp_path):
    st = _store(tmp_path)
    st.save_budgets({"a": 100, "b": 200})
    st.save_budgets({"b": 200})
    assert st.load_budgets() == {"b": 200}
