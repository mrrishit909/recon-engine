"""Pull records from Stripe, Plaid and QuickBooks and normalise them into Txn.

Written against each provider's documented REST responses and tested with recorded-shape fixtures
(tests/test_connectors.py). Not yet exercised against live accounts: that needs real credentials.
Each function returns (records, new cursor); the cursor is stored so the next poll continues where this one stopped.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from .models import Txn, to_minor

STRIPE_KINDS = {"charge": "charge", "payment": "charge", "refund": "refund", "payment_refund": "refund",
                "adjustment": "chargeback"}   # Stripe books lost disputes as 'adjustment' balance transactions


def _ts(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, tz=UTC)


def _stripe_pages(client: httpx.Client, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    while True:
        page = client.get(path, params=params).raise_for_status().json()
        items += page["data"]
        if not page.get("has_more") or not page["data"]:
            return items
        params = {**params, "starting_after": page["data"][-1]["id"]}


def fetch_stripe(client: httpx.Client, cursor: str = "0") -> tuple[list[Txn], str]:
    """Payouts created since the cursor, plus the balance transactions (charges, refunds, disputes) inside each.
    Other balance-transaction types are left out on purpose: the payout then fails the composition check and a
    person looks at it, instead of the engine guessing."""
    txns: list[Txn] = []
    newest = int(cursor)
    for p in _stripe_pages(client, "/v1/payouts", {"limit": 100, "created[gt]": cursor}):
        newest = max(newest, p["created"])
        trace = (p.get("trace_id") or {}).get("value")
        txns.append(Txn(source="stripe", external_id=p["id"], kind="payout", amount_minor=p["amount"],
                        currency=p["currency"].upper(), occurred_at=_ts(p["arrival_date"]), reference=trace))
        for bt in _stripe_pages(client, "/v1/balance_transactions",
                                {"limit": 100, "payout": p["id"], "expand[]": "data.source"}):
            kind = STRIPE_KINDS.get(bt["type"])
            if kind is None:
                continue
            src = bt.get("source")
            customer = ((src.get("billing_details") or {}).get("email") or src.get("customer")) if isinstance(src, dict) else None
            txns.append(Txn(source="stripe", external_id=bt["id"], kind=kind, amount_minor=bt["amount"],  # type: ignore[arg-type]
                            fee_minor=bt["fee"], currency=bt["currency"].upper(), occurred_at=_ts(bt["created"]),
                            reference=src.get("id") if isinstance(src, dict) else src, payout_id=p["id"],
                            customer=customer))
    return txns, str(newest)


def fetch_plaid(client: httpx.Client, client_id: str, secret: str, access_token: str, cursor: str = "") -> tuple[list[Txn], str]:
    """Settled bank credits from /transactions/sync. Plaid amounts are positive for money leaving the account."""
    txns: list[Txn] = []
    while True:
        page = client.post("/transactions/sync", json={"client_id": client_id, "secret": secret,
                                                       "access_token": access_token, "cursor": cursor,
                                                       "count": 500}).raise_for_status().json()
        # ponytail: only 'added' is read; handle 'modified' / 'removed' when a bank is seen restating transactions
        for t in page["added"]:
            amount = Decimal(str(t["amount"]))
            if t.get("pending") or amount >= 0:
                continue
            currency = t.get("iso_currency_code") or "USD"
            txns.append(Txn(source="plaid", external_id=t["transaction_id"], kind="deposit",
                            amount_minor=to_minor(-amount, currency), currency=currency,
                            occurred_at=datetime.fromisoformat(t["date"]).replace(tzinfo=UTC),
                            reference=t.get("name"), account=t.get("account_id")))
        cursor = page["next_cursor"]
        if not page.get("has_more"):
            return txns, cursor


def fetch_quickbooks(client: httpx.Client, realm_id: str, cursor: str = "1970-01-01T00:00:00Z") -> tuple[list[Txn], str]:
    """Customer payments recorded in QuickBooks Online (the accounting side of money received outside the processor)."""
    txns: list[Txn] = []
    newest, start = cursor, 1
    while True:
        query = (f"SELECT * FROM Payment WHERE MetaData.LastUpdatedTime > '{cursor}' "
                 f"ORDERBY MetaData.LastUpdatedTime STARTPOSITION {start} MAXRESULTS 500")
        page = client.get(f"/v3/company/{realm_id}/query", params={"query": query},
                          headers={"Accept": "application/json"}).raise_for_status().json()
        payments = page.get("QueryResponse", {}).get("Payment", [])
        for p in payments:
            currency = (p.get("CurrencyRef") or {}).get("value", "USD")
            txns.append(Txn(source="quickbooks", external_id=str(p["Id"]), kind="payment",
                            amount_minor=to_minor(Decimal(str(p["TotalAmt"])), currency), currency=currency,
                            occurred_at=datetime.fromisoformat(p["TxnDate"]).replace(tzinfo=UTC),
                            reference=p.get("PaymentRefNum"), customer=(p.get("CustomerRef") or {}).get("name")))
            newest = max(newest, p.get("MetaData", {}).get("LastUpdatedTime", newest))
        if len(payments) < 500:
            return txns, newest
        start += 500
