# Reconciliation engine

Takes payment-processor records (Stripe), accounting records (QuickBooks) and bank deposits (Plaid or CSV/XLSX uploads),
pairs them automatically, books every movement in a double-entry ledger, and sends anything it will not decide by itself
to a review queue for a person.

Built from the "Automated Financial Compliance & Reconciliation Engine" blueprint as an MVP.
**Live demo (recorded run on synthetic data):** https://mrrishit909.github.io/projects/recon-engine/demo/

## Run it

```bash
# 1. secrets (never committed)
printf 'RECON_ENCRYPTION_KEY=%s\nRECON_API_TOKEN=%s\n' \
  "$(python3 -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())')" \
  "$(openssl rand -hex 24)" > .env

# 2. Postgres + API + dashboard + worker
docker compose up --build -d
docker compose run --rm api python -m recon.seed     # optional: load the synthetic month and reconcile it
open http://localhost:8000                            # sign in with any name + the RECON_API_TOKEN from .env

# 3. tests (55 tests, coverage gate at 90%; currently 98%)
docker compose run --rm test
```

Without Docker for the Python side: `python3 -m venv venv && venv/bin/pip install -e ".[dev]"`, keep `docker compose up -d db`
running, then `venv/bin/pytest` and `venv/bin/mypy` (strict).

## How it works

| Part | File | What it does |
|---|---|---|
| Ledger schema | `migrations/001_ledger.sql` | `journal_entries` + `postings`. A deferred constraint trigger refuses to commit an entry whose debits and credits differ in any currency. Ledger and audit log reject UPDATE / DELETE / TRUNCATE. |
| Reconciliation schema | `migrations/002_reconciliation.sql` | Source records, matches (a record can be matched once: `UNIQUE`), the `exceptions` dead-letter table, encrypted credentials, job queue. |
| Matcher | `recon/matching.py` | Pure functions, no database. Passes in order: **reference** (bank memo names the payout / trace id / invoice) → **exact** amount inside a 3-day window → **tolerance** (2 cents or 1 basis point) → **fx** (other currency, within 1% of the known rate). Then checks: payout = Σ(gross − fee), double billing, chargebacks, stale unmatched records. |
| Ledger posting | `recon/service.py` | Charge: Dr processor clearing (net), Dr fees, Cr revenue. Bank match: Dr bank, Cr clearing, difference to `recon_differences`. Cross-currency matches balance per currency through `fx_conversion`. |
| Concurrency | `recon/db.py` | Every write runs in a `SERIALIZABLE` transaction and is retried on a serialization failure. |
| Ingestion | `recon/connectors.py`, `recon/files.py` | Stripe payouts + balance transactions, Plaid `/transactions/sync`, QuickBooks payments; CSV/XLSX with a strict pydantic schema (one bad row rejects the file and names the row). |
| Workers | `recon/queue.py` | Postgres job queue (`FOR UPDATE SKIP LOCKED`), retries with backoff, dead-letters after 5 attempts. Poll jobs re-schedule themselves. |
| Encryption | `recon/crypto.py` | Fernet on customer identifiers, bank account metadata and API credentials, before they reach the database. |
| API | `recon/api.py` | FastAPI. Bearer token on every route; `X-Actor` names the person, and lands in the audit log. |
| Dashboard | `web/` | React + TypeScript + TanStack Table: split-screen control center with linked pairs, analytics, exception queue, audit log. |

## What the synthetic month shows

`recon/seed.py` generates one month of invented activity (531 records: 38 payouts and payments, 37 bank deposits, 456 charges,
refunds and chargebacks) with ten irregularities planted in it: eight that need a person and two that are legitimate
(a deposit one cent over, a euro payout landing in dollars). One run of the matcher:

- **34 of 38** payouts and payments matched to a bank deposit (89.5%): 21 by reference, 11 by exact amount, 1 by tolerance
  (bank paid one cent more; booked to `recon_differences`), 1 by FX (EUR payout landed as USD).
- **8 exceptions** sent to review: 2 chargebacks ($478.00 exposure including dispute fees), 1 double billing, 1 deposit $100 short,
  1 deposit in a currency with no rate on file, 1 payout that was $5.00 less than its charges, 1 payout that never arrived,
  1 deposit from an unknown sender.
- The ledger balances to zero in every currency, and the processor-clearing balance equals exactly the payouts not yet matched
  plus the $5.00 the processor withheld (asserted in `tests/test_ledger_db.py`).

These are properties of invented data and show that the engine finds what was planted; they are not a benchmark on real books.

## Not done, and caveats

- **The Stripe, Plaid and QuickBooks connectors have never called the live services.** They are written against the documented
  response shapes and tested with fake HTTP transports; real credentials will surface differences. Xero is not implemented.
  Only Plaid `added` transactions are read (not `modified` / `removed`).
- The task queue is Postgres, not Celery/Redis: one less service, and a job commits atomically with its ledger writes.
- One FX rate per currency pair (no dated rate table). Six zero-decimal currencies are known; others are treated as two-decimal.
- One shared API token and a self-declared actor name: no user accounts, roles or SSO. Encryption keys come from an environment
  variable: no KMS, no key rotation.
- The audit log is append-only by trigger. A database superuser can still drop the trigger; hash-chaining or shipping the log
  to write-once storage is the next step.
- No pagination or row virtualisation in the dashboard; not load-tested ("horizontally scalable" is a design intent:
  stateless API and any number of workers, not a measured result). One in-memory test matches 20,000 pairs in under 10 s.
