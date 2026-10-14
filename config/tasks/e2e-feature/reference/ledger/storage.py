"""CSV persistence.

The store is a *file* (typically ``ledger.csv``). Every other ledger file for
the same book lives alongside it in the same directory -- see ``LedgerStore.dir``.
"""

from __future__ import annotations

import csv
import io
import os

from ledger.parser import read_transactions, write_transactions
from ledger.models import (Transaction, ValidationError, format_date, format_money,
                           parse_date, parse_money)

RECURRING_HEADER = ("id", "date", "account", "category", "amount", "memo",
                    "frequency", "end")
BUDGETS_HEADER = ("category", "limit")


def _read_csv(path: str) -> list[list[str]]:
    """Read a CSV file; a missing file is an empty result, not an error."""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as fh:
        return [row for row in csv.reader(fh) if any(c.strip() for c in row)]


def _write_csv(path: str, header: tuple[str, ...], rows: list[list[str]]) -> None:
    """Atomic write: temp file next to the target, then os.replace."""
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(buf.getvalue())
    os.replace(tmp, path)


class LedgerStore:
    def __init__(self, path: str | os.PathLike):
        self.path = os.fspath(path)

    @property
    def dir(self) -> str:
        d = os.path.dirname(os.path.abspath(self.path))
        return d

    @property
    def _recurring_path(self) -> str:
        return os.path.join(self.dir, "recurring.csv")

    @property
    def _budgets_path(self) -> str:
        return os.path.join(self.dir, "budgets.csv")

    def exists(self) -> bool:
        return os.path.exists(self.path)

    def load(self) -> list[Transaction]:
        if not os.path.exists(self.path):
            return []
        with open(self.path, encoding="utf-8") as fh:
            return read_transactions(fh.read(), source=self.path)

    def save(self, txns: list[Transaction]) -> None:
        os.makedirs(self.dir, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(write_transactions(txns))
        os.replace(tmp, self.path)

    def append(self, txn: Transaction) -> None:
        txns = self.load()
        txns.append(txn)
        self.save(txns)

    # ---------------------------------------------------------- recurring ---

    def load_recurring(self) -> list["Recurrence"]:
        from ledger.recurring import Recurrence, FREQUENCIES

        rows = _read_csv(self._recurring_path)
        if not rows:
            return []
        header = [h.strip().lower() for h in rows[0]]
        idx = {h: i for i, h in enumerate(header)}
        out: list[Recurrence] = []
        for lineno, row in enumerate(rows[1:], start=2):

            def cell(name: str) -> str:
                i = idx.get(name)
                if i is None or i >= len(row):
                    return ""
                return row[i].strip()

            try:
                frequency = cell("frequency")
                if frequency not in FREQUENCIES:
                    raise ValidationError(f"unknown frequency {frequency!r}")
                end_raw = cell("end")
                out.append(
                    Recurrence(
                        id=cell("id"),
                        account=cell("account"),
                        category=cell("category"),
                        amount=parse_money(cell("amount")),
                        memo=cell("memo"),
                        anchor=parse_date(cell("date")),
                        frequency=frequency,
                        end=parse_date(end_raw) if end_raw else None,
                    )
                )
            except ValidationError as exc:
                raise ValidationError(f"recurring.csv: row {lineno}: {exc}") from exc
        return out

    def save_recurring(self, rows: list["Recurrence"]) -> None:
        payload = [
            [r.id, format_date(r.anchor), r.account, r.category,
             format_money(r.amount), r.memo, r.frequency,
             format_date(r.end) if r.end else ""]
            for r in rows
        ]
        _write_csv(self._recurring_path, RECURRING_HEADER, payload)

    # ------------------------------------------------------------ budgets ---

    def load_budgets(self) -> dict[str, int]:
        rows = _read_csv(self._budgets_path)
        if not rows:
            return {}
        header = [h.strip().lower() for h in rows[0]]
        ci, li = header.index("category"), header.index("limit")
        out: dict[str, int] = {}
        for row in rows[1:]:
            if len(row) <= max(ci, li):
                continue
            out[row[ci].strip()] = parse_money(row[li])
        return out

    def save_budgets(self, budgets: dict[str, int]) -> None:
        payload = [[cat, format_money(limit)] for cat, limit in sorted(budgets.items())]
        _write_csv(self._budgets_path, BUDGETS_HEADER, payload)

