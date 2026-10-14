from datetime import date

import pytest

from ledger.models import (
    Transaction,
    ValidationError,
    format_money,
    format_date,
    parse_date,
    parse_money,
)


def test_parse_money_plain_integer():
    # a bare integer is a whole-dollar amount, so it round-trips format_money
    assert parse_money("1234") == 123400
    assert parse_money("0") == 0


def test_parse_money_decimal():
    assert parse_money("12.34") == 1234
    assert parse_money("-7") == -700
    assert parse_money("0.05") == 5
    assert parse_money("7.5") == 750


def test_parse_money_rejects_garbage():
    for bad in ("", "abc", "1.2.3", "12,34", "1234x"):
        with pytest.raises(ValidationError):
            parse_money(bad)


def test_format_money_roundtrip():
    assert format_money(0) == "0.00"
    assert format_money(5) == "0.05"
    assert format_money(-1234) == "-12.34"
    assert format_money(100000) == "1000.00"


def test_parse_date():
    assert parse_date("2026-01-31") == date(2026, 1, 31)
    assert parse_date(" 2026-01-31 ") == date(2026, 1, 31)
    with pytest.raises(ValidationError):
        parse_date("31/01/2026")
    with pytest.raises(ValidationError):
        parse_date("2026-13-01")


def test_format_date():
    assert format_date(date(2026, 3, 5)) == "2026-03-05"


def test_transaction_is_immutable():
    t = Transaction("tx-1", "checking", "groceries", -100, "milk", date(2026, 1, 1))
    with pytest.raises(Exception):
        t.amount = 0


def test_transaction_row_roundtrip():
    t = Transaction("tx-1", "checking", "groceries", -1234, "milk", date(2026, 1, 1))
    assert t.as_row() == ["tx-1", "2026-01-01", "checking", "groceries", "-12.34", "milk"]
