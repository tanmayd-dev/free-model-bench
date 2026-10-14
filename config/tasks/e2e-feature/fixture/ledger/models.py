"""Value types shared across the ledger package."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

DATE_FMT = "%Y-%m-%d"
_MONEY_RE = re.compile(r"^-?\d+(\.\d{1,2})?$")


class ValidationError(ValueError):
    """Raised when user-supplied data cannot be parsed or is out of range."""


def parse_date(value: str) -> date:
    try:
        return datetime.strptime(value.strip(), DATE_FMT).date()
    except ValueError as exc:
        raise ValidationError(f"invalid date {value!r}, expected YYYY-MM-DD") from exc


def format_date(value: date) -> str:
    return value.strftime(DATE_FMT)


def parse_money(value: str) -> int:
    """Parse a decimal amount in DOLLARS into cents.

    ``"12.34" -> 1234``, ``"1234" -> 123400``, ``"-7" -> -700``.

    At most two decimal places are accepted, so ``parse_money`` round-trips
    ``format_money`` exactly.
    """
    value = str(value).strip()
    if not _MONEY_RE.match(value):
        raise ValidationError(f"invalid amount {value!r}, expected e.g. 12.34 or -7")
    neg = value.startswith("-")
    whole, _, frac = value.lstrip("-").partition(".")
    frac = (frac + "00")[:2]
    cents = int(whole) * 100 + int(frac)
    return -cents if neg else cents


def format_money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100}.{abs(cents) % 100:02d}"


@dataclass(frozen=True)
class Transaction:
    id: str
    account: str
    category: str
    amount: int  # cents; negative = expense
    memo: str
    occurred: date

    def as_row(self) -> list[str]:
        return [
            self.id,
            format_date(self.occurred),
            self.account,
            self.category,
            format_money(self.amount),
            self.memo,
        ]
