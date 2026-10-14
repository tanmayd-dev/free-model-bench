"""Command line interface.

Entry point contract (stable, do not break):

    main(argv: list[str] | None = None) -> int

Exit codes: 0 success, 1 usage/validation error, 2 policy violation.
"""

from __future__ import annotations

import argparse
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

    try:
        return int(args.func(args, sys.stdout))
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 -- deliberate top-level guard
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
