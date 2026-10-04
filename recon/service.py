"""Everything that touches the database: storing records, posting the ledger, running the matcher, human review."""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any, cast

from psycopg.types.json import Jsonb

from . import crypto
from .db import Conn
from .matching import Config, convert, reconcile as run_matcher
from .models import COMPONENTS, LEDGER_SIDE, Match, Txn

INCOME_SIDE = {"charge": "revenue", "refund": "refunds", "chargeback": "chargebacks"}
CLEARING = {"payout": "processor_clearing", "payment": "undeposited_funds"}
Line = tuple[str, int, str]   # (account code, signed minor amount: + debit / - credit, currency)


def audit(conn: Conn, actor: str, action: str, entity: str, entity_id: object, detail: dict[str, Any] | None = None) -> None:
    conn.execute("INSERT INTO audit_log (actor, action, entity, entity_id, detail) VALUES (%s, %s, %s, %s, %s)",
                 (actor, action, entity, str(entity_id), Jsonb(detail or {})))


def post(conn: Conn, occurred_at: datetime, description: str, source_ref: str, actor: str, lines: list[Line]) -> None:
    """Write one balanced journal entry. The database re-checks the balance at commit (see 001_ledger.sql)."""
    lines = [line for line in lines if line[1] != 0]
    if not lines:
        return
    row = conn.execute("INSERT INTO journal_entries (occurred_at, description, source_ref, created_by) "
                       "VALUES (%s, %s, %s, %s) RETURNING id", (occurred_at, description, source_ref, actor)).fetchone()
    assert row is not None
    for account, signed, currency in lines:
        conn.execute("INSERT INTO postings (entry_id, account_id, direction, amount_minor, currency) "
                     "VALUES (%s, (SELECT id FROM accounts WHERE code = %s), %s, %s, %s)",
                     (row["id"], account, "D" if signed > 0 else "C", abs(signed), currency))


def store(conn: Conn, txns: Iterable[Txn], actor: str) -> int:
    """Insert new records (already-seen ones are skipped), post their ledger entries, and log the batch."""
    new_ids: list[int] = []
    sources: set[str] = set()
    for t in txns:
        row = conn.execute(
            "INSERT INTO source_transactions (source, external_id, kind, amount_minor, fee_minor, currency, occurred_at,"
            " reference, payout_id, customer_enc, account_enc) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT (source, external_id) DO NOTHING RETURNING id",
            (t.source, t.external_id, t.kind, t.amount_minor, t.fee_minor, t.currency, t.occurred_at, t.reference,
             t.payout_id, crypto.encrypt(t.customer), crypto.encrypt(t.account))).fetchone()
        if row is None:
            continue
        txn_id = cast(int, row["id"])
        new_ids.append(txn_id)
        sources.add(t.source)
        if t.kind in COMPONENTS:      # gross splits into what the processor owes us (net) and what it kept (fee)
            post(conn, t.occurred_at, f"{t.kind} {t.external_id}", f"txn:{txn_id}", actor, [
                ("processor_clearing", t.net_minor, t.currency), ("processing_fees", t.fee_minor, t.currency),
                (INCOME_SIDE[t.kind], -t.amount_minor, t.currency)])
        elif t.kind == "payment":
            post(conn, t.occurred_at, f"payment {t.external_id}", f"txn:{txn_id}", actor, [
                ("undeposited_funds", t.amount_minor, t.currency), ("accounts_receivable", -t.amount_minor, t.currency)])
    if new_ids:
        audit(conn, actor, "ingested", "source_transactions", f"{min(new_ids)}-{max(new_ids)}",
              {"count": len(new_ids), "sources": sorted(sources)})
    return len(new_ids)


def _load(conn: Conn, where: str, params: tuple[object, ...] = ()) -> list[Txn]:
    rows = conn.execute(f"SELECT * FROM source_transactions t WHERE {where} ORDER BY id", params).fetchall()
    return [Txn(id=cast(int, r["id"]), source=cast(str, r["source"]), external_id=cast(str, r["external_id"]),
                kind=cast(Any, r["kind"]), amount_minor=cast(int, r["amount_minor"]), fee_minor=cast(int, r["fee_minor"]),
                currency=cast(str, r["currency"]), occurred_at=cast(datetime, r["occurred_at"]),
                reference=cast("str | None", r["reference"]), payout_id=cast("str | None", r["payout_id"]),
                customer=crypto.decrypt(cast("bytes | None", r["customer_enc"])),
                account=crypto.decrypt(cast("bytes | None", r["account_enc"]))) for r in rows]


def _record_match(conn: Conn, m: Match, left: Txn, bank: Txn, actor: str, cfg: Config) -> int:
    row = conn.execute("INSERT INTO matches (pass, confidence, diff_minor, ledger_txn, bank_txn, created_by) "
                       "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                       (m.pass_name, m.confidence, m.diff_minor, left.id, bank.id, actor)).fetchone()
    assert row is not None
    match_id = cast(int, row["id"])
    clearing = CLEARING[left.kind]
    if left.currency == bank.currency:      # any small difference is booked, not hidden
        lines: list[Line] = [("bank", bank.amount_minor, bank.currency), (clearing, -left.amount_minor, left.currency),
                             ("recon_differences", left.amount_minor - bank.amount_minor, bank.currency)]
    else:                                   # each currency balances by itself through the conversion account
        lines = [("bank", bank.amount_minor, bank.currency), ("fx_conversion", -bank.amount_minor, bank.currency),
                 ("fx_conversion", left.amount_minor, left.currency), (clearing, -left.amount_minor, left.currency)]
    post(conn, bank.occurred_at, f"{left.kind} {left.external_id} received at bank", f"match:{match_id}", actor, lines)
    audit(conn, actor, "manual_match" if m.pass_name == "manual" else "matched", "matches", match_id,
          {"pass": m.pass_name, "confidence": m.confidence, "ledger_txn": left.id, "bank_txn": bank.id,
           "diff_minor": m.diff_minor})
    return match_id


UNMATCHED = "NOT EXISTS (SELECT 1 FROM matches m WHERE t.id IN (m.ledger_txn, m.bank_txn))"


def reconcile(conn: Conn, as_of: datetime, actor: str = "process:matcher", cfg: Config = Config()) -> dict[str, int]:
    """One matching run. Call it through db.serializable so concurrent runs cannot both claim the same record."""
    ledger = _load(conn, f"kind = ANY(%s) AND {UNMATCHED}", (list(LEDGER_SIDE),))
    bank = _load(conn, f"kind = 'deposit' AND {UNMATCHED}")
    components = _load(conn, "kind = ANY(%s)", (list(COMPONENTS),))
    result = run_matcher(ledger, bank, components, as_of, cfg)
    by_id = {t.id: t for t in [*ledger, *bank]}
    for m in result.matches:
        _record_match(conn, m, by_id[m.ledger_id], by_id[m.bank_id], f"{actor}:{m.pass_name}", cfg)
    raised = 0
    for a in result.anomalies:
        row = conn.execute("INSERT INTO exceptions (txn_id, kind, detail) VALUES (%s, %s, %s) "
                           "ON CONFLICT (txn_id, kind) DO NOTHING RETURNING id", (a.txn_id, a.kind, Jsonb(a.detail))).fetchone()
        if row is not None:
            raised += 1
            audit(conn, actor, "exception_raised", "exceptions", row["id"], {"kind": a.kind, "txn": a.txn_id})
    return {"matched": len(result.matches), "exceptions_raised": raised}


def manual_match(conn: Conn, actor: str, ledger_txn: int, bank_txn: int, reason: str, cfg: Config = Config()) -> int:
    """A supervisor pairs two records by hand. The difference, if any, is booked and the reason is audited."""
    left = _load(conn, f"id = %s AND kind = ANY(%s) AND {UNMATCHED}", (ledger_txn, list(LEDGER_SIDE)))
    bank = _load(conn, f"id = %s AND kind = 'deposit' AND {UNMATCHED}", (bank_txn,))
    if not left or not bank:
        raise ValueError("a manual match needs one unmatched payout/payment and one unmatched deposit")
    expected: int | None = left[0].amount_minor
    if left[0].currency != bank[0].currency:
        expected = convert(left[0].amount_minor, left[0].currency, bank[0].currency, cfg)
    diff = bank[0].amount_minor - expected if expected is not None else 0
    match_id = _record_match(conn, Match(left[0].id, bank[0].id, "manual", 1.0, diff), left[0], bank[0], actor, cfg)
    closed = conn.execute(     # the pair is settled, so open exceptions about either record are closed with it
        "UPDATE exceptions SET status = 'resolved', resolved_by = %s, resolution = %s WHERE status = 'open' "
        "AND (txn_id = ANY(%s) OR (detail->>'ledger_txn')::bigint = %s) RETURNING id",
        (actor, f"manual match {match_id}: {reason}", [ledger_txn, bank_txn], ledger_txn)).fetchall()
    for row in closed:
        audit(conn, actor, "exception_resolved", "exceptions", row["id"], {"resolution": reason, "match": match_id})
    return match_id


def resolve(conn: Conn, exception_id: int, actor: str, resolution: str) -> None:
    """A supervisor closes an exception with a written reason. Always audited."""
    done = conn.execute("UPDATE exceptions SET status = 'resolved', resolved_by = %s, resolution = %s "
                        "WHERE id = %s AND status = 'open' RETURNING id", (actor, resolution, exception_id)).fetchone()
    if done is None:
        raise ValueError(f"exception {exception_id} is not open")
    audit(conn, actor, "exception_resolved", "exceptions", exception_id, {"resolution": resolution})


# ---- read models for the dashboard -----------------------------------------------------------------------

def reconciliation_view(conn: Conn) -> dict[str, Any]:
    rows = conn.execute("""
        SELECT t.id, t.source, t.external_id, t.kind, t.amount_minor, t.currency, t.occurred_at, t.reference,
               m.id AS match_id, m.pass, m.confidence::float AS confidence, m.diff_minor,
               (SELECT string_agg(e.kind, ', ' ORDER BY e.kind) FROM exceptions e
                 WHERE e.status = 'open' AND (e.txn_id = t.id OR (e.detail->>'ledger_txn')::bigint = t.id)) AS exception
        FROM source_transactions t LEFT JOIN matches m ON t.id IN (m.ledger_txn, m.bank_txn)
        WHERE t.kind IN ('payout', 'payment', 'deposit') ORDER BY t.occurred_at, t.id""").fetchall()
    for r in rows:
        r["status"] = "matched" if r["match_id"] else "exception" if r["exception"] else "pending"
    return {"ledger": [r for r in rows if r["kind"] != "deposit"], "bank": [r for r in rows if r["kind"] == "deposit"]}


def analytics(conn: Conn) -> dict[str, Any]:
    one = conn.execute("""
        SELECT count(*) FILTER (WHERE kind IN ('payout', 'payment')) AS ledger_total,
               count(*) FILTER (WHERE kind IN ('payout', 'payment') AND EXISTS
                     (SELECT 1 FROM matches m WHERE m.ledger_txn = t.id)) AS ledger_matched,
               count(*) FILTER (WHERE kind = 'deposit') AS bank_total,
               count(*) FILTER (WHERE kind = 'deposit' AND EXISTS
                     (SELECT 1 FROM matches m WHERE m.bank_txn = t.id)) AS bank_matched
        FROM source_transactions t""").fetchone()
    assert one is not None
    total, matched = cast(int, one["ledger_total"]), cast(int, one["ledger_matched"])
    return {
        **one,
        "match_rate": round(matched / total, 4) if total else None,
        "matches_by_pass": conn.execute("SELECT pass, count(*) AS n FROM matches GROUP BY pass ORDER BY n DESC").fetchall(),
        "fees": conn.execute("SELECT source, currency, sum(fee_minor)::bigint AS fee_minor, "
                             "sum(amount_minor) FILTER (WHERE kind = 'charge')::bigint AS gross_minor "
                             "FROM source_transactions WHERE kind IN ('charge', 'refund', 'chargeback') "
                             "GROUP BY source, currency ORDER BY source, currency").fetchall(),
        "dispute_exposure": conn.execute(
            "SELECT t.currency, count(*) AS n, sum(t.fee_minor - t.amount_minor)::bigint AS exposure_minor "
            "FROM exceptions e JOIN source_transactions t ON t.id = e.txn_id "
            "WHERE e.kind = 'chargeback' AND e.status = 'open' GROUP BY t.currency ORDER BY t.currency").fetchall(),
        "open_exceptions": conn.execute("SELECT kind, count(*) AS n FROM exceptions WHERE status = 'open' "
                                        "GROUP BY kind ORDER BY n DESC, kind").fetchall(),
        "trial_balance": conn.execute(
            "SELECT a.code, a.type, p.currency, "
            "sum(CASE p.direction WHEN 'D' THEN p.amount_minor ELSE -p.amount_minor END)::bigint AS balance_minor "
            "FROM postings p JOIN accounts a ON a.id = p.account_id GROUP BY a.code, a.type, p.currency "
            "ORDER BY a.code, p.currency").fetchall(),
    }


def exceptions_view(conn: Conn) -> list[dict[str, Any]]:
    return conn.execute("""
        SELECT e.id, e.kind, e.status, e.detail, e.created_at, e.resolved_by, e.resolution,
               t.id AS txn_id, t.source, t.external_id, t.kind AS txn_kind, t.amount_minor, t.currency, t.occurred_at
        FROM exceptions e LEFT JOIN source_transactions t ON t.id = e.txn_id ORDER BY e.status, e.id""").fetchall()


def audit_view(conn: Conn, limit: int = 500) -> list[dict[str, Any]]:
    return conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT %s", (limit,)).fetchall()
