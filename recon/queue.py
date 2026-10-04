"""Asynchronous pipeline workers on a Postgres job queue.

Why Postgres and not Celery/Redis: a job and the ledger rows it writes commit in the same transaction, so a crash
can never leave "job done, ledger half-written". Workers claim jobs with FOR UPDATE SKIP LOCKED, so any number of
`python -m recon.queue` processes can run side by side.
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
from psycopg.types.json import Jsonb

from . import connectors, crypto, service
from .db import Conn, serializable

MAX_ATTEMPTS = 5
BASE_URLS = {"stripe": "https://api.stripe.com", "plaid": "https://production.plaid.com",
             "quickbooks": "https://quickbooks.api.intuit.com"}
Transport = Callable[[str], httpx.BaseTransport | None]
transport_for: Transport = lambda source: None   # tests swap this for a fake transport  # noqa: E731


def enqueue(conn: Conn, kind: str, payload: dict[str, Any] | None = None, delay_seconds: int = 0) -> None:
    conn.execute("INSERT INTO jobs (kind, payload, run_at) VALUES (%s, %s, now() + make_interval(secs => %s))",
                 (kind, Jsonb(payload or {}), delay_seconds))


def _poll(conn: Conn, payload: dict[str, Any]) -> None:
    """Fetch new records from one provider, store them, schedule the next poll and a matching run."""
    source = cast(str, payload["source"])
    cred = conn.execute("SELECT secret_enc FROM credentials WHERE provider = %s", (source,)).fetchone()
    if cred is None:
        raise RuntimeError(f"no credentials stored for {source}")
    secret = json.loads(crypto.decrypt(cast(bytes, cred["secret_enc"])) or "{}")
    state = conn.execute("SELECT cursor FROM sync_state WHERE source = %s", (source,)).fetchone()
    cursor = cast("str | None", state["cursor"]) if state else None
    headers = {"Authorization": f"Bearer {secret['token']}"} if "token" in secret else {}
    # ponytail: the HTTP call runs inside the job's transaction; split fetch and store if polls become slow
    with httpx.Client(base_url=BASE_URLS[source], headers=headers, timeout=30, transport=transport_for(source)) as client:
        if source == "stripe":
            txns, new_cursor = connectors.fetch_stripe(client, cursor or "0")
        elif source == "plaid":
            txns, new_cursor = connectors.fetch_plaid(client, secret["client_id"], secret["secret"],
                                                      secret["access_token"], cursor or "")
        else:
            txns, new_cursor = connectors.fetch_quickbooks(client, secret["realm_id"], cursor or "1970-01-01T00:00:00Z")
    service.store(conn, txns, f"process:poll:{source}")
    conn.execute("INSERT INTO sync_state (source, cursor) VALUES (%s, %s) "
                 "ON CONFLICT (source) DO UPDATE SET cursor = EXCLUDED.cursor", (source, new_cursor))
    if payload.get("every_seconds"):
        enqueue(conn, "poll", payload, int(payload["every_seconds"]))
    enqueue(conn, "reconcile")


def _reconcile(conn: Conn, payload: dict[str, Any]) -> None:
    as_of = datetime.fromisoformat(payload["as_of"]) if payload.get("as_of") else datetime.now(UTC)
    service.reconcile(conn, as_of)


HANDLERS: dict[str, Callable[[Conn, dict[str, Any]], None]] = {"poll": _poll, "reconcile": _reconcile}


def work_one(url: str | None = None) -> bool:
    """Claim and run one due job. Returns False when the queue is empty."""
    def run(conn: Conn) -> bool:
        job = conn.execute("SELECT id, kind, payload, attempts FROM jobs WHERE status = 'queued' AND run_at <= now() "
                           "ORDER BY run_at, id FOR UPDATE SKIP LOCKED LIMIT 1").fetchone()
        if job is None:
            return False
        try:
            with conn.transaction():         # savepoint: a failed handler leaves no partial writes behind
                HANDLERS[cast(str, job["kind"])](conn, cast("dict[str, Any]", job["payload"]))
            conn.execute("UPDATE jobs SET status = 'done', attempts = attempts + 1 WHERE id = %s", (job["id"],))
        except Exception as exc:
            from psycopg import errors
            if isinstance(exc, (errors.SerializationFailure, errors.DeadlockDetected)):
                raise                        # let db.serializable retry the whole job
            attempts = cast(int, job["attempts"]) + 1
            conn.execute("UPDATE jobs SET attempts = %s, last_error = %s, status = %s, run_at = now() + %s WHERE id = %s",
                         (attempts, f"{type(exc).__name__}: {exc}"[:500],
                          "failed" if attempts >= MAX_ATTEMPTS else "queued",
                          timedelta(seconds=30 * 2 ** attempts), job["id"]))   # dead-lettered after MAX_ATTEMPTS
        return True
    return serializable(run, url)


def worker(poll_seconds: float = 2.0) -> None:  # pragma: no cover - the loop itself; work_one is what is tested
    while True:
        if not work_one():
            time.sleep(poll_seconds)


if __name__ == "__main__":  # pragma: no cover
    print("worker started, database:", os.environ["DATABASE_URL"].rsplit("@", 1)[-1])
    worker()
