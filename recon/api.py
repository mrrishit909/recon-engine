"""HTTP API for the dashboard. Every request needs the bearer token; writes record who made them."""
from __future__ import annotations

import hmac
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import files, queue, service
from .db import connect, serializable

app = FastAPI(title="Reconciliation engine")


def current_actor(authorization: Annotated[str, Header()] = "", x_actor: Annotated[str, Header()] = "") -> str:
    expected = f"Bearer {os.environ['RECON_API_TOKEN']}"
    if not hmac.compare_digest(authorization.encode(), expected.encode()):
        raise HTTPException(401, "missing or wrong API token")
    if not x_actor.strip():
        raise HTTPException(400, "X-Actor header (who is making this request) is required")
    return f"user:{x_actor.strip()[:80]}"


Actor = Annotated[str, Depends(current_actor)]


@app.get("/api/reconciliation")
def reconciliation(_: Actor) -> dict[str, Any]:
    with connect() as conn:
        return service.reconciliation_view(conn)


@app.get("/api/analytics")
def analytics(_: Actor) -> dict[str, Any]:
    with connect() as conn:
        return service.analytics(conn)


@app.get("/api/exceptions")
def exceptions(_: Actor) -> list[dict[str, Any]]:
    with connect() as conn:
        return service.exceptions_view(conn)


@app.get("/api/audit")
def audit(_: Actor) -> list[dict[str, Any]]:
    with connect() as conn:
        return service.audit_view(conn)


@app.post("/api/files")
async def upload(file: UploadFile, actor: Actor) -> dict[str, Any]:
    """Validate a CSV/XLSX upload; if every row passes, store it and queue a matching run."""
    content = await file.read()
    try:
        txns = files.parse(file.filename or "", content)
    except files.FileRejected as exc:
        raise HTTPException(422, {"message": str(exc), "errors": exc.errors[:200]}) from exc

    def work(conn: Any) -> int:
        stored = service.store(conn, txns, actor)
        queue.enqueue(conn, "reconcile")
        return stored
    return {"rows": len(txns), "stored": serializable(work)}


@app.post("/api/reconcile")
def reconcile(actor: Actor) -> dict[str, int]:
    return serializable(lambda conn: service.reconcile(conn, datetime.now(UTC), actor))


class Resolution(BaseModel):
    resolution: str = Field(min_length=3, max_length=500)      # the supervisor's reason, kept in the audit log


class ManualMatch(BaseModel):
    ledger_txn: int
    bank_txn: int
    reason: str = Field(min_length=3, max_length=500)


@app.post("/api/exceptions/{exception_id}/resolve")
def resolve(exception_id: int, body: Resolution, actor: Actor) -> dict[str, str]:
    try:
        serializable(lambda conn: service.resolve(conn, exception_id, actor, body.resolution))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"status": "resolved"}


@app.post("/api/matches")
def manual_match(body: ManualMatch, actor: Actor) -> dict[str, int]:
    try:
        return {"match_id": serializable(lambda conn: service.manual_match(conn, actor, body.ledger_txn, body.bank_txn, body.reason))}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


_dist = Path(__file__).resolve().parent.parent / "web" / "dist"
if _dist.exists():  # pragma: no cover - only in the built image
    app.mount("/", StaticFiles(directory=_dist, html=True), name="web")
