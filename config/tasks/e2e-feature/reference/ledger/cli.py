"""Command line interface.

Entry point contract (stable, do not break):

    main(argv: list[str] | None = None) -> int

Exit codes: 0 success, 1 usage/validation error, 2 policy violation.
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
from datetime import date

from ledger.models import Transaction, ValidationError, format_money, parse_date, parse_money
from ledger.reports import for_month, rollup, summary
from ledger.storage import LedgerStore

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_POLICY = 2


def _store(args: argparse.Namespace) -> LedgerStore:
    return LedgerStore(args.ledger)


def cmd_add(args: argparse.Namespace, out=sys.stdout) -> int:
    store = _store(args)
    tx_id = args.id or _next_id(store.load())
    txn = Transaction(
        id=tx_id,
        account=args.account,
        category=args.category,
        amount=parse_money(args.amount),
        memo=args.memo or "",
        occurred=parse_date(args.date),
    )
    store.append(txn)
    print(f"added {txn.id} {format_money(txn.amount)} {txn.category}", file=out)
    return EXIT_OK


def _next_id(existing: list[Transaction]) -> str:
    used = {t.id for t in existing}
    n = len(existing) + 1
    while f"tx-{n:04d}" in used:
        n += 1
    return f"tx-{n:04d}"


def cmd_import(args: argparse.Namespace, out=sys.stdout) -> int:
    with open(args.file, encoding="utf-8") as fh:
        text = fh.read()
    from ledger.parser import read_transactions

    txns = read_transactions(text, source=args.file)
    store = _store(args)
    existing = store.load()
    store.save(existing + txns)
    print(f"imported {len(txns)} transaction(s)", file=out)
    return EXIT_OK


def cmd_list(args: argparse.Namespace, out=sys.stdout) -> int:
    txns = _store(args).load()
    if args.month:
        year, month = (int(x) for x in args.month.split("-", 1))
        txns = for_month(txns, year, month)
    if not txns:
        print("(no transactions)", file=out)
        return EXIT_OK
    for t in sorted(txns, key=lambda t: t.occurred):
        print(
            f"{t.occurred.isoformat()} {t.account:<12} {t.category:<14} "
            f"{format_money(t.amount):>10}  {t.memo}",
            file=out,
        )
    return EXIT_OK


def cmd_summary(args: argparse.Namespace, out=sys.stdout) -> int:
    txns = _store(args).load()
    if args.month:
        year, month = (int(x) for x in args.month.split("-", 1))
        txns = for_month(txns, year, month)
        s = summary(txns)
        print(f"{args.month}: net {format_money(s['net'])} "
              f"(income {format_money(s['income'])}, expense {format_money(s['expense'])})",
              file=out)
        for cat, amount in s["categories"].items():
            print(f"  {cat:<14} {format_money(amount):>10}", file=out)
        return EXIT_OK

    if args.months:
        anchor = date.today().replace(day=1)
        for row in rollup(txns, args.months, anchor):
            print(f"{row['label']}  net {format_money(row['net']):>10}  "
                  f"({row['count']} txns)", file=out)
        return EXIT_OK

    s = summary(txns)
    print(f"count {s['count']}", file=out)
    print(f"income {format_money(s['income'])}", file=out)
    print(f"expense {format_money(s['expense'])}", file=out)
    print(f"net {format_money(s['net'])}", file=out)
    for cat, amount in s["categories"].items():
        print(f"  {cat:<14} {format_money(amount):>10}", file=out)
    return EXIT_OK


def cmd_unknown(_args, out=sys.stdout) -> int:
    print("error: unknown command", file=sys.stderr)
    return EXIT_ERROR


# ---------------------------------------------------------------- forecast ---


def _window(args) -> tuple[date, date]:
    from ledger.recurring import shift_months

    start = parse_date(args.from_) if getattr(args, "from_", None) else date.today()
    if getattr(args, "to", None):
        end = parse_date(args.to)
    else:
        end = shift_months(start, int(getattr(args, "months", 3) or 0))
    return start, end


def _projected(recs, start: date, end: date):
    from ledger.recurring import iter_occurrences

    out = []
    for r in recs:
        for d in iter_occurrences(r, start, end):
            out.append((d, r))
    out.sort(key=lambda pair: (pair[0], pair[1].id))
    return out


def cmd_forecast(args, out=sys.stdout) -> int:
    recs = _store(args).load_recurring()
    if not recs:
        print("no recurring entries", file=out)
        return EXIT_OK
    start, end = _window(args)
    rows = _projected(recs, start, end)
    if args.format == "csv":
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(("date", "account", "category", "amount", "memo"))
        for d, r in rows:
            w.writerow([d.isoformat(), r.account, r.category,
                        format_money(r.amount), r.memo])
        out.write(buf.getvalue())
        return EXIT_OK
    for d, r in rows:
        print(f"{d}  {r.account}  {r.category:<12} {format_money(r.amount):>10}  {r.memo}",
              file=out)
    return EXIT_OK


# ------------------------------------------------------------------ budget ---


def cmd_budget_set(args, out=sys.stdout) -> int:
    store = _store(args)
    budgets = store.load_budgets()
    limit = parse_money(args.amount)
    budgets[args.category] = limit
    store.save_budgets(budgets)
    print(f"set {args.category} {format_money(limit)}", file=out)
    return EXIT_OK


def cmd_budget_clear(args, out=sys.stdout) -> int:
    store = _store(args)
    budgets = store.load_budgets()
    if args.category in budgets:
        del budgets[args.category]
        store.save_budgets(budgets)
    print(f"cleared {args.category}", file=out)
    return EXIT_OK


def cmd_budget_list(args, out=sys.stdout) -> int:
    budgets = _store(args).load_budgets()
    if not budgets:
        print("(no budgets)", file=out)
        return EXIT_OK
    for cat in sorted(budgets):
        print(f"{cat} {format_money(budgets[cat])}", file=out)
    return EXIT_OK


def cmd_budget_check(args, out=sys.stdout) -> int:
    store = _store(args)
    budgets = store.load_budgets()
    if not budgets:
        print("(no budgets)", file=out)
        return EXIT_OK

    txns = store.load()
    spend = {cat: 0 for cat in budgets}
    for t in txns:
        if t.amount < 0 and t.category in spend:
            spend[t.category] += t.amount

    if getattr(args, "projected", False):
        from ledger.recurring import iter_occurrences

        start, end = _window(args)
        recs = store.load_recurring()
        for r in recs:
            if r.amount >= 0 or r.category not in spend:
                continue
            for _ in iter_occurrences(r, start, end):
                spend[r.category] += r.amount

    over = False
    for cat in sorted(budgets):
        limit = budgets[cat]
        is_over = abs(spend[cat]) > limit
        over = over or is_over
        status = "OVER" if is_over else "OK"
        print(f"{cat} {format_money(spend[cat])}/{format_money(limit)}  {status}", file=out)
    return EXIT_POLICY if over else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ledger", description="Tiny personal finance ledger.")
    p.add_argument("--ledger", default="ledger.csv", help="path to the ledger CSV")
    sub = p.add_subparsers(dest="command")

    add = sub.add_parser("add", help="append a single transaction")
    add.add_argument("--date", required=True)
    add.add_argument("--account", required=True)
    add.add_argument("--category", required=True)
    add.add_argument("--amount", required=True)
    add.add_argument("--memo", default="")
    add.add_argument("--id", default="")
    add.set_defaults(func=cmd_add)

    imp = sub.add_parser("import", help="import a transactions CSV")
    imp.add_argument("file")
    imp.set_defaults(func=cmd_import)

    lst = sub.add_parser("list", help="list transactions")
    lst.add_argument("--month", default="", help="YYYY-MM filter")
    lst.set_defaults(func=cmd_list)

    sm = sub.add_parser("summary", help="summarise transactions")
    sm.add_argument("--month", default="", help="YYYY-MM filter")
    sm.add_argument("--months", type=int, default=0, help="trailing N months")
    sm.set_defaults(func=cmd_summary)

    fc = sub.add_parser("forecast", help="project recurring entries onto a window")
    fc.add_argument("--from", dest="from_", default="", help="YYYY-MM-DD (inclusive)")
    fc.add_argument("--to", default="", help="YYYY-MM-DD (inclusive)")
    fc.add_argument("--months", type=int, default=3, help="window length when --to is absent")
    fc.add_argument("--format", choices=("list", "csv"), default="list")
    fc.set_defaults(func=cmd_forecast)

    bud = sub.add_parser("budget", help="category budgets")
    bsub = bud.add_subparsers(dest="budget_command")

    bset = bsub.add_parser("set", help="set a category limit")
    bset.add_argument("--category", required=True)
    bset.add_argument("--amount", required=True, help="limit in dollars")
    bset.set_defaults(func=cmd_budget_set)

    bclr = bsub.add_parser("clear", help="remove a category limit")
    bclr.add_argument("--category", required=True)
    bclr.set_defaults(func=cmd_budget_clear)

    blst = bsub.add_parser("list", help="show limits")
    blst.set_defaults(func=cmd_budget_list)

    bchk = bsub.add_parser("check", help="compare spend against limits")
    bchk.add_argument("--projected", action="store_true")
    bchk.add_argument("--from", dest="from_", default="", help="YYYY-MM-DD (inclusive)")
    bchk.add_argument("--to", default="", help="YYYY-MM-DD (inclusive)")
    bchk.add_argument("--months", type=int, default=3)
    bchk.set_defaults(func=cmd_budget_check)

    return p


def main(argv: list[str] | None = None) -> int:
    """Parse argv and run one command.

    Contract: **this function never raises.** Anything unexpected is reported
    on stderr and returned as EXIT_ERROR, so a broken command can never take
    the process down with a traceback.
    """
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        # argparse exits 2 on usage errors; this project reserves 2 for policy
        # violations, so argparse failures are reported as a generic error.
        return EXIT_ERROR

    if not getattr(args, "command", None):
        parser.print_help(sys.stdout)
        return EXIT_ERROR
    if args.command == "budget" and not getattr(args, "budget_command", None):
        try:
            parser.parse_args(["budget", "--help"])
        except SystemExit:
            pass
        return EXIT_ERROR

    try:
        return int(args.func(args, sys.stdout))
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 -- deliberate top-level guard
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
