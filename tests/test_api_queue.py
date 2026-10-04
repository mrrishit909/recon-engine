from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from recon import crypto, queue, seed, service
from recon.api import app
from recon.db import connect, serializable

AUTH = {"Authorization": "Bearer test-token", "X-Actor": "dana"}
client = TestClient(app)


def test_api_needs_token_and_actor(db: str) -> None:
    assert client.get("/api/analytics").status_code == 401
    assert client.get("/api/analytics", headers={"Authorization": "Bearer nope", "X-Actor": "x"}).status_code == 401
    assert client.get("/api/analytics", headers={"Authorization": "Bearer test-token"}).status_code == 400


def test_dashboard_endpoints_and_manual_resolution(db: str) -> None:
    serializable(lambda c: service.store(c, seed.scenario(), "process:seed"))
    assert client.post("/api/reconcile", headers=AUTH).json()["matched"] > 20
    view = client.get("/api/reconciliation", headers=AUTH).json()
    assert {r["status"] for r in view["ledger"]} == {"matched", "exception"} and len(view["bank"]) > 30
    assert 0.8 < client.get("/api/analytics", headers=AUTH).json()["match_rate"] < 1
    exceptions = client.get("/api/exceptions", headers=AUTH).json()
    target = next(e for e in exceptions if e["kind"] == "chargeback")
    url = f"/api/exceptions/{target['id']}/resolve"
    assert client.post(url, headers=AUTH, json={"resolution": "x"}).status_code == 422          # a reason is required
    assert client.post(url, headers=AUTH, json={"resolution": "dispute lost, accepted"}).json() == {"status": "resolved"}
    assert client.post(url, headers=AUTH, json={"resolution": "dispute lost, accepted"}).status_code == 409
    newest = client.get("/api/audit", headers=AUTH).json()[0]
    assert (newest["actor"], newest["action"]) == ("user:dana", "exception_resolved")
    pair = next(e for e in exceptions if e["kind"] == "amount_mismatch")
    body = {"ledger_txn": pair["detail"]["ledger_txn"], "bank_txn": pair["txn_id"], "reason": "short-paid by bank"}
    assert client.post("/api/matches", headers=AUTH, json=body).json()["match_id"] > 0
    assert client.post("/api/matches", headers=AUTH, json=body).status_code == 409


def test_file_upload_validates_then_stores_and_queues_matching(db: str) -> None:
    good = b"external_id,kind,amount,currency,occurred_at\nf1,payout,10.00,USD,2026-08-01\nf2,deposit,10.00,USD,2026-08-02\n"
    bad = good + b"f3,deposit,ten,USD,2026-08-02\n"
    r = client.post("/api/files", headers=AUTH, files={"file": ("bank.csv", bad)})
    assert r.status_code == 422 and r.json()["detail"]["errors"][0]["row"] == 4
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM source_transactions").fetchone() == {"n": 0}
    assert client.post("/api/files", headers=AUTH, files={"file": ("bank.csv", good)}).json() == {"rows": 2, "stored": 2}
    assert queue.work_one() is True and queue.work_one() is False
    with connect() as conn:
        assert conn.execute("SELECT pass FROM matches").fetchone() == {"pass": "exact"}
        assert conn.execute("SELECT status FROM jobs").fetchone() == {"status": "done"}


def fake_transport(source: str) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if source == "stripe":
            assert request.headers["Authorization"] == "Bearer sk_test_123"
            if request.url.path == "/v1/payouts":
                return httpx.Response(200, json={"has_more": False, "data": [
                    {"id": "po_LIVE0001", "amount": 9680, "currency": "usd", "created": 1785000000, "arrival_date": 1785100000}]})
            return httpx.Response(200, json={"has_more": False, "data": [
                {"id": "txn_1", "type": "charge", "amount": 10000, "fee": 320, "currency": "usd", "created": 1784990000, "source": "ch_1"}]})
        if source == "plaid":
            return httpx.Response(200, json={"has_more": False, "next_cursor": "c9", "added": [
                {"transaction_id": "t1", "amount": -96.8, "iso_currency_code": "USD", "date": "2026-07-27",
                 "name": "STRIPE PO_LIVE0001", "pending": False, "account_id": "acc_1"}]})
        return httpx.Response(200, json={"QueryResponse": {}})
    return httpx.MockTransport(handler)


def test_poll_jobs_ingest_from_each_provider_then_reconcile(db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(queue, "transport_for", fake_transport)
    secrets = {"stripe": {"token": "sk_test_123"}, "plaid": {"client_id": "id", "secret": "s", "access_token": "a"},
               "quickbooks": {"token": "qb", "realm_id": "r1"}}
    with connect() as conn:
        for source, secret in secrets.items():
            conn.execute("INSERT INTO credentials VALUES (%s, %s)", (source, crypto.encrypt(json.dumps(secret))))
            queue.enqueue(conn, "poll", {"source": source, "every_seconds": 900 if source == "stripe" else 0})
    while queue.work_one():
        pass
    with connect() as conn:
        assert conn.execute("SELECT pass, created_by FROM matches").fetchone() == {"pass": "reference", "created_by": "process:matcher:reference"}
        assert {r["source"]: r["cursor"] for r in conn.execute("SELECT * FROM sync_state")} == {
            "stripe": "1785000000", "plaid": "c9", "quickbooks": "1970-01-01T00:00:00Z"}
        again = conn.execute("SELECT count(*) AS n FROM jobs WHERE kind = 'poll' AND status = 'queued' AND run_at > now()").fetchone()
        assert again == {"n": 1}                                                   # stripe re-scheduled itself


def test_failing_job_backs_off_then_dead_letters(db: str) -> None:
    with connect() as conn:
        queue.enqueue(conn, "poll", {"source": "stripe"})                          # no credentials stored -> fails
    assert queue.work_one() is True
    with connect() as conn:
        job = conn.execute("SELECT status, attempts, last_error, run_at > now() AS later FROM jobs").fetchone()
        assert job == {"status": "queued", "attempts": 1, "last_error": "RuntimeError: no credentials stored for stripe", "later": True}
        conn.execute("UPDATE jobs SET attempts = 4, run_at = now()")
    assert queue.work_one() is True
    with connect() as conn:
        assert conn.execute("SELECT status, attempts FROM jobs").fetchone() == {"status": "failed", "attempts": 5}
    assert queue.work_one() is False
