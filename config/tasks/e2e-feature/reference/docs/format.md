# On-disk formats

All ledger files live **in the same directory**, next to the main ledger CSV.
`LedgerStore.path` is the main ledger CSV; `LedgerStore.dir` is its directory.

## ledger.csv

```
id,date,account,category,amount,memo
tx-0001,2026-01-03,checking,groceries,-12.34,weekly shop
tx-0002,2026-01-05,checking,salary,2500.00,paycheck
```

* `id` — opaque string, unique within the file.
* `date` — `YYYY-MM-DD`.
* `amount` — decimal string, at most two decimal places, negative = expense.
* Rows are written in insertion order. Blank lines are ignored on read.

## General rules for any file added to this format

* UTF-8, `newline=""` when writing so `\r\n` never leaks in.
* Header row first, `\n` line endings.
* Write to `<name>.csv.tmp` then `os.replace` onto `<name>.csv`.
* A missing file reads as "empty", not as an error.
