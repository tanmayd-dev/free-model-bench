import pytest

from ledger.models import ValidationError
from ledger.parser import read_transactions, write_transactions

GOOD = """\
id,date,account,category,amount,memo
tx-0001,2026-01-03,checking,groceries,-12.34,weekly shop
tx-0002,2026-01-05,checking,salary,2500.00,paycheck
tx-0003,2026-01-06,savings,transfer,-100.00,
"""


def test_parse_good_rows():
    txns = read_transactions(GOOD)
    assert len(txns) == 3
    assert txns[0].amount == -1234
    assert txns[1].amount == 250000
    assert txns[2].memo == ""


def test_missing_id_column_generates_ids():
    text = "date,account,category,amount\n2026-01-03,checking,groceries,-12.34\n"
    txns = read_transactions(text)
    assert txns[0].id == "tx-0001"


def test_missing_required_column():
    with pytest.raises(ValidationError, match="missing required column"):
        read_transactions("date,account\n2026-01-03,checking\n")


def test_bad_date_reports_row_number():
    text = "date,account,category,amount\n2026-01-03,checking,groceries,-12.34\nnope,checking,groceries,-1\n"
    with pytest.raises(ValidationError, match="row 3"):
        read_transactions(text)


def test_empty_account_rejected():
    text = "date,account,category,amount\n2026-01-03,,groceries,-12.34\n"
    with pytest.raises(ValidationError, match="account"):
        read_transactions(text)


def test_blank_lines_skipped():
    text = "date,account,category,amount\n2026-01-03,checking,groceries,-12.34\n\n,,,\n"
    assert len(read_transactions(text)) == 1


def test_roundtrip():
    txns = read_transactions(GOOD)
    again = read_transactions(write_transactions(txns))
    assert again == txns


def test_empty_input():
    assert read_transactions("") == []
    assert read_transactions("   \n") == []
