# Ledger

A tiny, dependency-free personal finance ledger for the command line.

```
python -m ledger --ledger my.csv summary --month 2026-01
python -m ledger --ledger my.csv add --date 2026-01-05 \
    --account checking --category groceries --amount -12.34 --memo "weekly shop"
python -m ledger --ledger my.csv import examples/transactions.csv
python -m ledger --ledger my.csv list --month 2026-01
```

## Layout

```
ledger/
  models.py     value types: Transaction, money & date parsing
  dates.py      date helpers
  parser.py     CSV <-> Transaction
  storage.py    LedgerStore (file-backed persistence)
  reports.py    by_category / summary / rollup
  cli.py        argparse CLI, main(argv) -> int
tests/          the visible test suite -- `pytest -q` must stay green
docs/format.md  the on-disk CSV formats
examples/       sample data
```

## Conventions

* Money is an **integer number of cents**; negative means expense.
* Dates are `datetime.date`, serialized `YYYY-MM-DD`.
* Persistence is plain CSV with a header row (see `docs/format.md`).
* Exit codes: `0` success, `1` usage/validation error, `2` policy violation.
* `ledger.cli.main(argv) -> int` **never raises** -- unexpected exceptions are
  reported on stderr and returned as `1`.
* **No third-party dependencies.** Python 3.14, stdlib only.
* `LedgerStore.save()` must stay atomic (write temp file, then `os.replace`).
