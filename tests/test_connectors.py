"""Connectors against fake HTTP transports that answer in each provider's documented response shape."""
from __future__ import annotations

import json

import httpx

from recon.connectors import fetch_plaid, fetch_quickbooks, fetch_stripe


def client(handler: object) -> httpx.Client:
    return httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def stripe_handler(request: httpx.Request) -> httpx.Response:
    q = request.url.params
    if request.url.path == "/v1/payouts":
        if "starting_after" not in q:
            return httpx.Response(200, json={"has_more": True, "data": [
                {"id": "po_1", "amount": 14378, "currency": "usd", "created": 1785000000, "arrival_date": 1785100000,
                 "trace_id": {"status": "supported", "value": "ST-0000012345"}}]})
        assert q["starting_after"] == "po_1"
        return httpx.Response(200, json={"has_more": False, "data": [
            {"id": "po_2", "amount": 500, "currency": "eur", "created": 1785200000, "arrival_date": 1785300000,
             "trace_id": None}]})
    assert request.url.path == "/v1/balance_transactions" and q["expand[]"] == "data.source"
    if q["payout"] == "po_2":
        return httpx.Response(200, json={"has_more": False, "data": []})
    return httpx.Response(200, json={"has_more": False, "data": [
        {"id": "txn_1", "type": "charge", "amount": 20000, "fee": 610, "net": 19390, "currency": "usd",
         "created": 1784990000, "source": {"id": "ch_1", "billing_details": {"email": "ann@example.com"}}},
        {"id": "txn_2", "type": "refund", "amount": -2000, "fee": 0, "net": -2000, "currency": "usd",
         "created": 1784991000, "source": "re_1"},
        {"id": "txn_3", "type": "adjustment", "amount": -1512, "fee": 1500, "net": -3012, "currency": "usd",
         "created": 1784992000, "source": {"id": "dp_1", "customer": "cus_9"}},
        {"id": "txn_4", "type": "payout", "amount": -14378, "fee": 0, "net": -14378, "currency": "usd",
         "created": 1785000000, "source": "po_1"},
        {"id": "txn_5", "type": "stripe_fee", "amount": -200, "fee": 0, "net": -200, "currency": "usd",
         "created": 1784993000, "source": None}]})


def test_stripe_payouts_and_their_balance_transactions() -> None:
    txns, cursor = fetch_stripe(client(stripe_handler), "1784000000")
    assert cursor == "1785200000"
    by_id = {t.external_id: t for t in txns}
    assert sorted(by_id) == ["po_1", "po_2", "txn_1", "txn_2", "txn_3"]         # payout rows and unknown types left out
    assert (by_id["po_1"].kind, by_id["po_1"].amount_minor, by_id["po_1"].reference) == ("payout", 14378, "ST-0000012345")
    assert (by_id["po_2"].currency, by_id["po_2"].reference) == ("EUR", None)
    charge, refund, dispute = by_id["txn_1"], by_id["txn_2"], by_id["txn_3"]
    assert (charge.kind, charge.net_minor, charge.customer, charge.payout_id, charge.reference) == \
        ("charge", 19390, "ann@example.com", "po_1", "ch_1")
    assert (refund.kind, refund.amount_minor, refund.reference) == ("refund", -2000, "re_1")
    assert (dispute.kind, dispute.net_minor, dispute.customer) == ("chargeback", -3012, "cus_9")


def test_plaid_sync_keeps_settled_credits_only_and_follows_the_cursor() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["access_token"] == "access-x" and request.url.path == "/transactions/sync"
        if body["cursor"] == "":
            return httpx.Response(200, json={"has_more": True, "next_cursor": "c1", "added": [
                {"transaction_id": "t1", "amount": -143.78, "iso_currency_code": "USD", "date": "2026-08-03",
                 "name": "STRIPE TRANSFER ST-0000012345", "pending": False, "account_id": "acc_1"},
                {"transaction_id": "t2", "amount": 60.0, "iso_currency_code": "USD", "date": "2026-08-03",
                 "name": "OFFICE RENT", "pending": False, "account_id": "acc_1"},
                {"transaction_id": "t3", "amount": -10.0, "iso_currency_code": "USD", "date": "2026-08-03",
                 "name": "PENDING CREDIT", "pending": True, "account_id": "acc_1"}]})
        assert body["cursor"] == "c1"
        return httpx.Response(200, json={"has_more": False, "next_cursor": "c2", "added": [
            {"transaction_id": "t4", "amount": -0.1, "iso_currency_code": None, "date": "2026-08-04",
             "name": "INTEREST", "pending": False, "account_id": "acc_1"}]})

    txns, cursor = fetch_plaid(client(handler), "id", "secret", "access-x")
    assert cursor == "c2"
    assert [(t.external_id, t.amount_minor, t.currency, t.account) for t in txns] == [
        ("t1", 14378, "USD", "acc_1"), ("t4", 10, "USD", "acc_1")]


def test_quickbooks_payments() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/company/realm-1/query" and "FROM Payment" in request.url.params["query"]
        return httpx.Response(200, json={"QueryResponse": {"Payment": [
            {"Id": "88", "TotalAmt": 2500.5, "TxnDate": "2026-08-07", "CurrencyRef": {"value": "USD"},
             "PaymentRefNum": "INV-2026-001", "CustomerRef": {"name": "Client 1 Ltd"},
             "MetaData": {"LastUpdatedTime": "2026-08-07T10:00:00Z"}},
            {"Id": "89", "TotalAmt": 10, "TxnDate": "2026-08-08", "MetaData": {}}]}})

    txns, cursor = fetch_quickbooks(client(handler), "realm-1")
    assert cursor == "2026-08-07T10:00:00Z"
    assert [(t.kind, t.amount_minor, t.reference, t.customer) for t in txns] == [
        ("payment", 250050, "INV-2026-001", "Client 1 Ltd"), ("payment", 1000, None, None)]
    empty, same = fetch_quickbooks(client(lambda r: httpx.Response(200, json={"QueryResponse": {}})), "realm-1", "c")
    assert (empty, same) == ([], "c")
