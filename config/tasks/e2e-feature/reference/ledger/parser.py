"""CSV import/export for transactions."""

from __future__ import annotations

import csv
import io

from ledger.models import Transaction, ValidationError, format_money, parse_date, parse_money

COLUMNS = ("id", "date", "account", "category", "amount", "memo")


def read_transactions(text: str, *, source: str = "<input>") -> list[Transaction]:
    """Parse CSV text into transactions.

    Raises ValidationError with a 1-based row number (plus header offset) when
    a row is malformed, and when a required column is missing entirely.
    """
    if not text.strip():
        return []
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return []
    header = [h.strip().lower() for h in header]
    missing = [c for c in ("date", "account", "category", "amount") if c not in header]
    if missing:
        raise ValidationError(f"{source}: missing required column(s): {', '.join(missing)}")
    idx = {name: i for i, name in enumerate(header)}

    out: list[Transaction] = []
    for lineno, row in enumerate(reader, start=2):
        if not row or all(not c.strip() for c in row):
            continue

        def cell(name: str, default: str = "") -> str:
            i = idx.get(name)
            if i is None or i >= len(row):
                return default
            return row[i].strip()

        if "id" in idx and cell("id"):
            tx_id = cell("id")
        else:
            tx_id = f"tx-{len(out) + 1:04d}"
        try:
            occurred = parse_date(cell("date"))
        except ValidationError as exc:
            raise ValidationError(f"{source}: row {lineno}: {exc}") from exc
        account = cell("account")
        category = cell("category")
        if not account:
            raise ValidationError(f"{source}: row {lineno}: account must not be empty")
        if not category:
            raise ValidationError(f"{source}: row {lineno}: category must not be empty")
        try:
            amount = parse_money(cell("amount"))
        except ValidationError as exc:
            raise ValidationError(f"{source}: row {lineno}: {exc}") from exc
        out.append(
            Transaction(
                id=tx_id,
                account=account,
                category=category,
                amount=amount,
                memo=cell("memo"),
                occurred=occurred,
            )
        )
    return out


def write_transactions(txns: list[Transaction]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(COLUMNS)
    for t in txns:
        writer.writerow(t.as_row())
    return buf.getvalue()
