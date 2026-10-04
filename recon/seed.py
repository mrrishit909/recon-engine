"""A synthetic month of business, with known problems planted in it. Used by the tests and the demo.

Nothing here is real money or a real customer. Run:  python -m recon.seed   (migrates, loads, reconciles)
"""
from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from .matching import Config
from .models import Txn

START = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
DAYS = 30
AS_OF = START + timedelta(days=DAYS + 1)
CFG = Config(fx_rates={("EUR", "USD"): Decimal("1.17")})      # an illustrative rate, not a market quote
PLANTED = {"double_billing": 4, "tolerance": 7, "chargeback_a": 9, "missing_deposit": 11, "withheld": 13,
           "short_deposit": 16, "eur_payout": 19, "chargeback_b": 21, "wrong_currency": 23}


def scenario(seed: int = 7) -> list[Txn]:
    rng = random.Random(seed)
    txns: list[Txn] = []
    n = 0

    def ident(prefix: str) -> str:
        nonlocal n
        n += 1
        return f"{prefix}_{n:05d}{rng.randrange(16 ** 6):06x}"

    for d in range(DAYS):
        day = START + timedelta(days=d)
        currency = "EUR" if d == PLANTED["eur_payout"] else "USD"
        payout, trace = ident("po"), f"ST-{rng.randrange(10 ** 10):010d}"
        net = 0

        def add(kind: str, amount: int, fee: int, when: datetime, customer: str | None) -> None:
            nonlocal net
            net += amount - fee
            txns.append(Txn(source="stripe", external_id=ident("txn"), kind=kind, amount_minor=amount, fee_minor=fee,  # type: ignore[arg-type]
                            currency=currency, occurred_at=when, payout_id=payout, customer=customer))

        for _ in range(rng.randint(10, 20)):
            amount = rng.choice([1900, 4900, 9900, 14900, 29900]) * rng.randint(1, 3)
            when = day + timedelta(minutes=rng.randrange(600))
            customer = f"customer{rng.randrange(400):03d}@example.com"
            add("charge", amount, round(amount * 0.029) + 30, when, customer)
            if d == PLANTED["double_billing"] and _ == 0:           # the same card charged again 3 minutes later
                add("charge", amount, round(amount * 0.029) + 30, when + timedelta(minutes=3), customer)
        if d % 3 == 1:
            add("refund", -rng.choice([1900, 4900, 9900]), 0, day + timedelta(hours=11), None)
        if d in (PLANTED["chargeback_a"], PLANTED["chargeback_b"]):
            add("chargeback", -rng.choice([14900, 29900]), 1500, day + timedelta(hours=12), None)

        paid = net - 500 if d == PLANTED["withheld"] else net       # the processor kept 5.00 with no explanation
        arrival = (day + timedelta(days=2)).replace(hour=0, minute=0)
        txns.append(Txn(source="stripe", external_id=payout, kind="payout", amount_minor=paid, currency=currency,
                        occurred_at=arrival, reference=trace))

        landed = arrival + timedelta(days=rng.choice([0, 1, 1, 2]))
        if d == PLANTED["missing_deposit"] or landed > AS_OF:
            continue
        memo = f"STRIPE TRANSFER {trace}" if rng.random() < 0.6 else "ACH CREDIT STRIPE"
        bank_amount, bank_currency = paid, currency
        if d == PLANTED["tolerance"]:
            memo, bank_amount = "ACH CREDIT STRIPE", paid + 1                       # one cent over, no reference
        elif d == PLANTED["short_deposit"]:
            memo, bank_amount = f"STRIPE TRANSFER {trace}", paid - 10_000           # 100.00 short, with a reference
        elif d == PLANTED["eur_payout"]:
            memo, bank_amount, bank_currency = f"STRIPE TRANSFER {trace}", round(paid * 1.17 * 0.998), "USD"
        elif d == PLANTED["wrong_currency"]:
            memo, bank_currency = f"STRIPE TRANSFER {trace}", "GBP"                 # no GBP rate on file
        txns.append(Txn(source="plaid", external_id=ident("bank"), kind="deposit", amount_minor=bank_amount,
                        currency=bank_currency, occurred_at=landed, reference=memo, account="acct-operating-4821"))

    for i in range(8):                       # invoices paid by bank transfer, recorded in the accounting system
        day = START + timedelta(days=3 * i + 2)
        amount, ref = rng.randrange(200_000, 900_000), f"INV-2026-{i + 1:03d}"
        txns.append(Txn(source="quickbooks", external_id=ident("pay"), kind="payment", amount_minor=amount,
                        currency="USD", occurred_at=day, reference=ref, customer=f"Client {i + 1} Ltd"))
        txns.append(Txn(source="plaid", external_id=ident("bank"), kind="deposit", amount_minor=amount, currency="USD",
                        occurred_at=day + timedelta(days=1), reference=f"WIRE IN {ref}" if i % 2 else "WIRE IN",
                        account="acct-operating-4821"))
    txns.append(Txn(source="plaid", external_id=ident("bank"), kind="deposit", amount_minor=123_456, currency="USD",
                    occurred_at=START + timedelta(days=14), reference="ACH CREDIT UNKNOWN SENDER",
                    account="acct-operating-4821"))
    return txns


if __name__ == "__main__":  # pragma: no cover
    from . import db, service
    db.migrate()
    print("stored:", db.serializable(lambda c: service.store(c, scenario(), "process:seed")))
    print(db.serializable(lambda c: service.reconcile(c, AS_OF, cfg=CFG)))
