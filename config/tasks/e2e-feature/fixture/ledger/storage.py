"""CSV persistence.

The store is a *file* (typically ``ledger.csv``). Every other ledger file for
the same book lives alongside it in the same directory -- see ``LedgerStore.dir``.
"""

from __future__ import annotations

import os

from ledger.parser import read_transactions, write_transactions
from ledger.models import Transaction


class LedgerStore:
    def __init__(self, path: str | os.PathLike):
        self.path = os.fspath(path)

    @property
    def dir(self) -> str:
        d = os.path.dirname(os.path.abspath(self.path))
        return d

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
