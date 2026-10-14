# Our project: Ledger

You are being asked to add a feature to an existing, working codebase. This
briefing is the whole context; treat it as authoritative where it conflicts
with your assumptions.

## What it is

`Ledger` is a tiny, dependency-free personal finance CLI. The repository root
is your current working directory. Read `README.md` and `docs/format.md`
first, then the code under `ledger/`.

```
ledger/
  __init__.py    __main__.py
  models.py      Transaction, date & money parsing/formatting, ValidationError
  dates.py       date helpers
  parser.py      CSV  <->  list[Transaction]
  storage.py     LedgerStore (file-backed persistence)
  reports.py     by_category / summary / for_month / rollup
  cli.py         argparse CLI;  main(argv) -> int
tests/           visible suite; `pytest -q` must stay green
docs/format.md   on-disk formats
examples/        sample data
```

## Hard conventions (the tests will enforce these)

* Money is an **integer number of cents** internally. Every amount the user
  *types* is a decimal in **dollars** with at most two decimal places
  (`"12.34" -> 1234`, `"1234" -> 123400`). `format_money` renders cents as
  `"12.34"` / `"-12.34"` with exactly two decimals.
* Dates are `datetime.date`, serialized `YYYY-MM-DD`.
* All ledger files live **in the same directory**, next to the main ledger
  CSV (`LedgerStore.dir`). A missing file reads as "empty", not as an error.
* Writing any file must be **atomic**: write `<name>.csv.tmp`, then
  `os.replace` onto `<name>.csv`.
* Exit codes: `0` success, `1` usage/validation error, `2` policy violation.
* `ledger.cli.main(argv) -> int` **never raises**.
* **No third-party dependencies.** Python 3.14, stdlib only. Tests use
  `pytest` (installed) but production code must not import it.
* Do **not** modify anything under `tests/`.

---

# Feature request

## 1. Month arithmetic (used by everything below)

Month shifts must preserve the day-of-month when that day exists in the target
month, and otherwise clamp to the **last day of the target month**:

| anchor      | +1 month | why |
|-------------|----------|-----|
| 2026-01-15  | 2026-02-15 | day 15 exists |
| 2026-01-31  | 2026-02-28 | Feb has 28 days in 2026 |
| 2024-01-31  | 2024-02-29 | 2024 is a leap year |
| 2026-05-31  | 2026-06-30 | Jun has 30 days |
| 2026-03-31  | 2026-04-30 | Apr has 30 days |

Do **not** reuse `ledger.dates.add_months` for this — it is documented as an
approximation that clamps to day 28. It must keep its current behaviour for
its two existing callers.

## 2. Recurring transactions — new module `ledger/recurring.py`

```python
@dataclass(frozen=True)
class Recurrence:
    id: str
    account: str
    category: str
    amount: int                 # cents, negative = expense
    memo: str
    anchor: date                # first ever occurrence, always included
    frequency: str              # "daily" | "weekly" | "monthly"
    end: date | None = None     # inclusive last possible date; None = forever
```

```python
def iter_occurrences(r: Recurrence, start: date, end: date) -> Iterator[date]
```

Yields occurrence dates **in ascending order**, satisfying all of:

* dates are >= `r.anchor` (nothing before the anchor),
* dates are <= `r.end` when `r.end` is not `None`,
* dates are within `[start, end]` — **both endpoints inclusive**,
* `daily` = every calendar day from the anchor;
  `weekly` = every 7 calendar days from the anchor;
  `monthly` = the month rule in section 1 applied to the anchor,
  successively (never go backwards, never skip a month),
* an empty window (`start > end`) yields nothing,
* an unknown `frequency` raises `ValidationError`.

Note the anchor can fall outside `[start, end]`; that only clips, it does not
shift the whole sequence.

## 3. Persistence — extend `ledger.storage.LedgerStore`

Add four methods to the **existing** `LedgerStore` class (do not create a
second store type):

```python
def load_recurring(self) -> list[Recurrence]
def save_recurring(self, rows: list[Recurrence]) -> None
def load_budgets(self) -> dict[str, int]      # category -> limit in cents
def save_budgets(self, budgets: dict[str, int]) -> None
```

Files, both living in `self.dir`, obeying every rule in `docs/format.md`
(header row, UTF-8, `\n`, atomic replace, missing file == empty):

* `recurring.csv` — header
  `id,date,account,category,amount,memo,frequency,end`
  where `date` is the anchor, `amount` is a decimal in dollars, and `end` is
  empty when `None`.
* `budgets.csv` — header `category,limit`, `limit` a decimal in dollars.

`load_recurring()` must raise `ValidationError` on a malformed row, exactly
the way `read_transactions` does (message must include `recurring.csv`).

## 4. `forecast` command

```
ledger forecast [--from YYYY-MM-DD] [--to YYYY-MM-DD] [--months N] [--format list|csv]
```

Projects every stored recurrence onto a date window and prints them.

* Window: `start` defaults to `date.today()`; `to` defaults to the month rule
  applied to `start` shifted by `--months` months (default `--months 3`),
  computed **directly from `start`, never accumulated** — the same way
  monthly occurrences are computed. `--to` is **inclusive**. An explicit
  `--to` overrides that; an explicit `--from` sets `start`.
  So `forecast --from 2026-01-01 --months 3` covers
  `[2026-01-01, 2026-04-01]` inclusive, and
  `forecast --from 2026-01-31 --months 1` covers
  `[2026-01-31, 2026-02-28]` inclusive.
* `--format list` (the default) prints, **sorted by (date, recurrence id)**,
  one line per occurrence:

  ```
  2026-02-01  checking  rent           -1200.00  monthly rent
  2026-03-01  checking  rent           -1200.00  monthly rent
  ```

  Match this exactly:
  `f"{d}  {r.account}  {r.category:<12} {fmt:>10}  {r.memo}"` where
  `d` is the date, `fmt` is `format_money(r.amount)`.
* `--format csv` prints a header `date,account,category,amount,memo` then the
  same rows as CSV using `ledger.parser`'s conventions (no id column).
* No recurrences stored: print exactly `no recurring entries` and exit `0`.
* An empty window (`start > to`) with recurrences stored prints nothing and
  exits `0`.
* Overlapping/duplicate recurrences are **not** de-duplicated — every
  occurrence of every recurrence is printed.
* Exit `0` on success; exit `1` on a bad `--from`/`--to`/`--months`.

## 5. `budget` command group

```
ledger budget set --category CAT --amount LIMIT
ledger budget clear --category CAT
ledger budget list
ledger budget check [--projected] [--from YYYY-MM-DD] [--to YYYY-MM-DD] [--months N]
```

* `set` — upserts the limit (dollars on input) for `CAT`, prints
  `set <CAT> <format_money(limit)>`, exit `0`.
* `clear` — removes `CAT`, prints `cleared <CAT>`, exit `0` even if absent.
* `list` — one line per budget sorted by category:
  `<CAT> <format_money(limit)>`; when there are none print exactly
  `(no budgets)`. Exit `0`.
* `check` — for every budget category, **actual** spend is the sum of the
  `amount` of all ledger transactions in that category with `amount < 0`
  (a negative number). A category is **over** when `abs(spend) > limit`.
  Prints one line per budget, sorted by category:

  ```
  groceries -50.00/200.00  OK
  rent -1324.50/1200.00  OVER
  ```

  Match this exactly:
  `f"{cat} {fmt(spend)}/{fmt(limit)}  {'OVER' if over else 'OK'}"` where
  `fmt` is `format_money`. Exit `0` when nothing is over, **`2`** when at
  least one is over. No budgets configured → print `(no budgets)` and exit `0`.
* `--projected` — the spend additionally includes every forecasted occurrence
  from `ledger.recurring.iter_occurrences` whose **expense** (`amount < 0`)
  falls inside the same window rules as `forecast`. Positive forecasted
  amounts are never subtracted. **Occurrences are added even when an
  identical real transaction already exists — there is no de-duplication.**
  Window defaults are the same as `forecast`.

## 6. Wiring

* `ledger budget ...` must be a subcommand of the existing parser (so
  `--ledger` still precedes it), and `ledger forecast ...` likewise.
* Reuse `LedgerStore`, `format_money`, `parse_money`, `ValidationError`.
* Keep the visible `tests/` suite green.

## 7. Out of scope

* No `--dry-run`, no config files, no color/emoji, no network, no new
  third-party packages, no changes to `docs/format.md`'s existing sections.
