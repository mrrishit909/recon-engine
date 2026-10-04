CREATE TABLE source_transactions (
    id            bigserial PRIMARY KEY,
    source        text NOT NULL,               -- stripe | plaid | quickbooks | file
    external_id   text NOT NULL,
    kind          text NOT NULL CHECK (kind IN ('charge', 'refund', 'chargeback', 'payout', 'payment', 'deposit')),
    amount_minor  bigint NOT NULL,             -- signed: refunds and chargebacks are negative
    fee_minor     bigint NOT NULL DEFAULT 0,   -- processor's cut; net = amount - fee
    currency      char(3) NOT NULL,
    occurred_at   timestamptz NOT NULL,
    reference     text,
    payout_id     text,                        -- the payout a charge / refund / chargeback settled in
    customer_enc  bytea,                       -- encrypted (Fernet): customer name / e-mail
    account_enc   bytea,                       -- encrypted (Fernet): bank account metadata
    ingested_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source, external_id)               -- re-polling the same record is a no-op
);
CREATE INDEX source_transactions_payout_idx ON source_transactions (payout_id);

CREATE TABLE matches (
    id          bigserial PRIMARY KEY,
    pass        text NOT NULL,                 -- reference | exact | tolerance | fx | manual
    confidence  numeric(3, 2) NOT NULL,
    diff_minor  bigint NOT NULL DEFAULT 0,     -- bank amount minus expected amount (bank currency)
    ledger_txn  bigint NOT NULL UNIQUE REFERENCES source_transactions (id),   -- UNIQUE: a record can be matched once
    bank_txn    bigint NOT NULL UNIQUE REFERENCES source_transactions (id),
    created_by  text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Dead-letter table: everything the engine will not decide by itself waits here for a person.
CREATE TABLE exceptions (
    id          bigserial PRIMARY KEY,
    txn_id      bigint REFERENCES source_transactions (id),
    kind        text NOT NULL,
    detail      jsonb NOT NULL DEFAULT '{}',
    status      text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    resolved_by text,
    resolution  text,
    UNIQUE (txn_id, kind)                      -- re-running the matcher does not raise the same exception twice
);

CREATE TABLE credentials (
    provider   text PRIMARY KEY,
    secret_enc bytea NOT NULL                  -- encrypted (Fernet): API keys / access tokens
);

CREATE TABLE sync_state (
    source text PRIMARY KEY,
    cursor text NOT NULL
);

-- Job queue. Workers claim rows with FOR UPDATE SKIP LOCKED, so a job and its ledger writes commit together.
CREATE TABLE jobs (
    id        bigserial PRIMARY KEY,
    kind      text NOT NULL,
    payload   jsonb NOT NULL DEFAULT '{}',
    status    text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'done', 'failed')),
    run_at    timestamptz NOT NULL DEFAULT now(),
    attempts  int NOT NULL DEFAULT 0,
    last_error text
);
CREATE INDEX jobs_ready_idx ON jobs (run_at) WHERE status = 'queued';
