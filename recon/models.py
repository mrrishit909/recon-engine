"""The one record shape every source is normalised into, and what the matcher returns."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Kind = Literal["charge", "refund", "chargeback", "payout", "payment", "deposit"]
LEDGER_SIDE: tuple[str, ...] = ("payout", "payment")       # records we expect to see arrive at the bank
COMPONENTS: tuple[str, ...] = ("charge", "refund", "chargeback")  # what a processor payout is made of
ZERO_DECIMAL = {"JPY", "KRW", "VND", "CLP", "ISK", "HUF"}  # ponytail: short list; use ISO 4217 table if more are needed


def exponent(currency: str) -> int:
    return 0 if currency in ZERO_DECIMAL else 2


def to_minor(amount: Decimal, currency: str) -> int:
    """Major units (12.34) to integer minor units (1234). Refuses amounts with too many decimals."""
    scaled = amount.scaleb(exponent(currency))
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{amount} has more decimals than {currency} allows")
    return int(scaled)


class Txn(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    id: int = 0                                  # database id (0 before it is stored)
    source: str = Field(min_length=1)
    external_id: str = Field(min_length=1)
    kind: Kind
    amount_minor: int                            # signed: refunds and chargebacks are negative
    fee_minor: int = 0
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    occurred_at: datetime
    reference: str | None = None
    payout_id: str | None = None
    customer: str | None = None                  # PII: encrypted at rest
    account: str | None = None                   # bank account metadata: encrypted at rest

    @property
    def net_minor(self) -> int:
        return self.amount_minor - self.fee_minor


@dataclass(frozen=True)
class Match:
    ledger_id: int
    bank_id: int
    pass_name: str          # reference | exact | tolerance | fx | manual
    confidence: float
    diff_minor: int = 0     # bank amount minus expected amount, in the bank's currency


@dataclass(frozen=True)
class Anomaly:
    txn_id: int
    kind: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class Result:
    matches: list[Match] = field(default_factory=list)
    anomalies: list[Anomaly] = field(default_factory=list)
