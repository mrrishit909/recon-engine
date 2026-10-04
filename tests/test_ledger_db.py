"""What only a real database can prove: balanced entries, append-only tables, encryption at rest, SERIALIZABLE."""
from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from recon import crypto, seed, service
from recon.db import connect, migrate, serializable
from recon.models import Txn

T0 = datetime(2026, 8, 10, tzinfo=UTC)


def load_seed() -> dict[str, int]:
    serializable(lambda c: service.store(c, seed.scenario(), "process:seed"))
    return serializable(lambda c: service.reconcile(c, seed.AS_OF, cfg=seed.CFG))


def test_migrations_apply_once(db: str) -> None:
    assert migrate() == []


def test_unbalanced_entry_cannot_commit(db: str) -> None:
    with pytest.raises(psycopg.errors.RaiseException, match="does not balance"), connect() as conn:
        conn.execute("INSERT INTO journal_entries (occurred_at, description, source_ref, created_by) "
                     "VALUES (now(), 'bad', 'test:1', 'test')")
        conn.execute("INSERT INTO postings (entry_id, account_id, direction, amount_minor, currency) "
                     "VALUES (1, 1, 'D', 100, 'USD'), (1, 2, 'C', 99, 'USD')")
    with pytest.raises(psycopg.errors.RaiseException, match="does not balance"), connect() as conn:   # per currency
        conn.execute("INSERT INTO journal_entries (occurred_at, description, source_ref, created_by) "
                     "VALUES (now(), 'bad', 'test:2', 'test')")
        conn.execute("INSERT INTO postings (entry_id, account_id, direction, amount_minor, currency) "
                     "SELECT id, 1, 'D', 100, 'USD' FROM journal_entries WHERE source_ref = 'test:2' "
                     "UNION ALL SELECT id, 2, 'C', 100, 'EUR' FROM journal_entries WHERE source_ref = 'test:2'")
    with pytest.raises(psycopg.errors.RaiseException, match="at least two postings"), connect() as conn:
        conn.execute("INSERT INTO journal_entries (occurred_at, description, source_ref, created_by) "
                     "VALUES (now(), 'empty', 'test:3', 'test')")
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM journal_entries").fetchone() == {"n": 0}


@pytest.mark.parametrize("statement", [
    "UPDATE postings SET amount_minor = 1", "DELETE FROM postings", "TRUNCATE postings, journal_entries, audit_log",
    "UPDATE journal_entries SET description = 'x'", "DELETE FROM journal_entries",
    "UPDATE audit_log SET actor = 'someone else'", "DELETE FROM audit_log"])
def test_ledger_and_audit_log_are_append_only(db: str, statement: str) -> None:
    load_seed()
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"), connect() as conn:
        conn.execute(statement)  # type: ignore[arg-type]


def test_sensitive_columns_are_encrypted_at_rest(db: str) -> None:
    t = Txn(source="stripe", external_id="c1", kind="charge", amount_minor=1000, fee_minor=59, currency="USD",
            occurred_at=T0, customer="ann@example.com", account="acct-4821")
    serializable(lambda c: service.store(c, [t], "process:test"))
    with connect() as conn:
        row = conn.execute("SELECT customer_enc, account_enc FROM source_transactions").fetchone()
        assert row is not None
        raw = bytes(row["customer_enc"]) + bytes(row["account_enc"])  # type: ignore[arg-type]
        assert b"ann@" not in raw and b"4821" not in raw
        assert crypto.decrypt(row["customer_enc"]) == "ann@example.com"  # type: ignore[arg-type]
        conn.execute("INSERT INTO credentials VALUES ('stripe', %s)", (crypto.encrypt("sk_test_abc"),))
        stored = conn.execute("SELECT secret_enc FROM credentials").fetchone()
        assert stored is not None and b"sk_test" not in bytes(stored["secret_enc"])  # type: ignore[arg-type]
    assert crypto.encrypt(None) is None and crypto.decrypt(None) is None


def test_missing_encryption_key_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RECON_ENCRYPTION_KEY")
    with pytest.raises(RuntimeError, match="RECON_ENCRYPTION_KEY"):
        crypto.encrypt("x")


def test_storing_the_same_records_twice_is_a_no_op(db: str) -> None:
    first = serializable(lambda c: service.store(c, seed.scenario(), "process:seed"))
    again = serializable(lambda c: service.store(c, seed.scenario(), "process:seed"))
    assert first > 500 and again == 0
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM audit_log WHERE action = 'ingested'").fetchone() == {"n": 1}


def test_seed_month_end_to_end(db: str) -> None:
    """The planted problems are each found, everything else matches, and the books balance."""
    summary = load_seed()
    assert serializable(lambda c: service.reconcile(c, seed.AS_OF, cfg=seed.CFG)) == {"matched": 0, "exceptions_raised": 0}
    with connect() as conn:
        a = service.analytics(conn)
        open_kinds = {r["kind"]: r["n"] for r in a["open_exceptions"]}
        assert open_kinds == {"double_billing": 1, "chargeback": 2, "unmatched_payout": 1, "payout_composition_mismatch": 1,
                              "amount_mismatch": 1, "currency_mismatch": 1, "unmatched_deposit": 1}
        passes = {r["pass"]: r["n"] for r in a["matches_by_pass"]}
        assert passes["tolerance"] == 1 and passes["fx"] == 1 and passes["reference"] > 10 and passes["exact"] > 5
        assert summary["matched"] == sum(passes.values()) == a["ledger_matched"] == a["bank_matched"]
        statuses = [r["status"] for r in service.reconciliation_view(conn)["ledger"]]
        # in review: the missing deposit, the short deposit, the wrong-currency deposit (the withheld one still matched)
        assert (a["ledger_total"], statuses.count("exception"), statuses.count("matched")) == (38, 3, a["ledger_matched"])
        # every currency's ledger sums to zero, and what is left in clearing is exactly the unmatched payouts
        for cur in ("USD", "EUR", "GBP"):
            assert sum(r["balance_minor"] for r in a["trial_balance"] if r["currency"] == cur) == 0
        clearing = {r["currency"]: r["balance_minor"] for r in a["trial_balance"] if r["code"] == "processor_clearing"}
        unmatched = conn.execute(
            "SELECT currency, sum(amount_minor)::bigint AS s FROM source_transactions t WHERE kind = 'payout' AND NOT EXISTS "
            "(SELECT 1 FROM matches m WHERE m.ledger_txn = t.id) GROUP BY currency").fetchall()
        withheld = 500      # the payout that came 5.00 short of its charges: still owed by the processor
        assert clearing["USD"] == sum(r["s"] for r in unmatched if r["currency"] == "USD") + withheld  # type: ignore[misc]
        assert clearing.get("EUR", 0) == 0
        fees = {(r["source"], r["currency"]): r["fee_minor"] for r in a["fees"]}
        assert fees[("stripe", "USD")] > 0 and a["dispute_exposure"][0]["n"] == 2
        assert len(service.audit_view(conn)) == 1 + summary["matched"] + summary["exceptions_raised"]


def test_supervisor_manual_match_books_the_difference_and_is_audited(db: str) -> None:
    load_seed()
    with connect() as conn:
        exc = conn.execute("SELECT e.id, e.txn_id, e.detail FROM exceptions e WHERE kind = 'amount_mismatch'").fetchone()
        other = conn.execute("SELECT id FROM exceptions WHERE kind = 'unmatched_deposit'").fetchone()
        assert exc is not None and other is not None
    bank_txn, ledger_txn = int(exc["txn_id"]), int(exc["detail"]["ledger_txn"])  # type: ignore[call-overload, index]
    match_id = serializable(lambda c: service.manual_match(c, "user:dana", ledger_txn, bank_txn, "bank short-paid; written off"))
    with connect() as conn:
        m = conn.execute("SELECT * FROM matches WHERE id = %s", (match_id,)).fetchone()
        assert m is not None and (m["pass"], m["created_by"], m["diff_minor"]) == ("manual", "user:dana", -10_000)
        diff = conn.execute("SELECT sum(CASE direction WHEN 'D' THEN amount_minor ELSE -amount_minor END)::bigint AS s "
                            "FROM postings p JOIN accounts a ON a.id = p.account_id WHERE a.code = 'recon_differences' "
                            "AND p.entry_id = (SELECT id FROM journal_entries WHERE source_ref = %s)", (f"match:{match_id}",)).fetchone()
        assert diff == {"s": 10_000}
        actions = [r["action"] for r in conn.execute("SELECT action FROM audit_log WHERE actor = 'user:dana' ORDER BY id")]
        assert actions == ["manual_match", "exception_resolved"]
        assert conn.execute("SELECT status, resolved_by FROM exceptions WHERE id = %s", (exc["id"],)).fetchone() == {
            "status": "resolved", "resolved_by": "user:dana"}
    with pytest.raises(ValueError, match="manual match needs"):          # both records are matched now
        serializable(lambda c: service.manual_match(c, "user:dana", ledger_txn, bank_txn, "again"))
    other_id = int(other["id"])  # type: ignore[call-overload]
    serializable(lambda c: service.resolve(c, other_id, "user:dana", "customer overpayment, refunded by wire"))
    with pytest.raises(ValueError, match="not open"):
        serializable(lambda c: service.resolve(c, other_id, "user:dana", "again"))


def test_manual_match_across_currencies_with_no_rate(db: str) -> None:
    load_seed()
    with connect() as conn:
        exc = conn.execute("SELECT txn_id, detail FROM exceptions WHERE kind = 'currency_mismatch'").fetchone()
        assert exc is not None
    bank_txn, ledger_txn = int(exc["txn_id"]), int(exc["detail"]["ledger_txn"])  # type: ignore[call-overload, index]
    serializable(lambda c: service.manual_match(c, "user:dana", ledger_txn, bank_txn, "bank confirmed GBP conversion"))
    with connect() as conn:
        for cur in ("USD", "GBP"):
            total = conn.execute("SELECT sum(CASE direction WHEN 'D' THEN amount_minor ELSE -amount_minor END)::bigint AS s "
                                 "FROM postings WHERE currency = %s", (cur,)).fetchone()
            assert total == {"s": 0}


def test_concurrent_matching_runs_never_double_match(db: str) -> None:
    """Eight matchers start together on the same unmatched records. SERIALIZABLE lets one win each conflict and the
    rest retry, so every payout is matched once and posted once."""
    txns = []
    for i in range(60):
        txns.append(Txn(source="stripe", external_id=f"po_{i:06d}", kind="payout", amount_minor=10_000 + i, currency="USD",
                        occurred_at=T0 + timedelta(hours=i)))
        txns.append(Txn(source="plaid", external_id=f"bk_{i:06d}", kind="deposit", amount_minor=10_000 + i, currency="USD",
                        occurred_at=T0 + timedelta(hours=i + 20)))
    serializable(lambda c: service.store(c, txns, "process:test"))
    barrier, errors = threading.Barrier(8), []

    def run() -> None:
        try:
            barrier.wait()
            serializable(lambda c: service.reconcile(c, T0 + timedelta(days=30)), retries=30)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n, count(DISTINCT ledger_txn) AS l, count(DISTINCT bank_txn) AS b "
                            "FROM matches").fetchone() == {"n": 60, "l": 60, "b": 60}
        assert conn.execute("SELECT count(*) AS n FROM journal_entries").fetchone() == {"n": 60}
        bank = conn.execute("SELECT sum(amount_minor)::bigint AS s FROM postings p JOIN accounts a ON a.id = p.account_id "
                            "WHERE a.code = 'bank' AND direction = 'D'").fetchone()
        assert bank == {"s": sum(10_000 + i for i in range(60))}
