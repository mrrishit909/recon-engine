-- Double-entry ledger. Money is stored as integer minor units (cents); never floats.
CREATE TABLE accounts (
    id    serial PRIMARY KEY,
    code  text NOT NULL UNIQUE,
    name  text NOT NULL,
    type  text NOT NULL CHECK (type IN ('asset', 'liability', 'equity', 'income', 'expense'))
);

INSERT INTO accounts (code, name, type) VALUES
    ('bank',               'Bank account',                         'asset'),
    ('processor_clearing', 'Processor balance in transit',         'asset'),
    ('undeposited_funds',  'Payments received, not yet banked',    'asset'),
    ('accounts_receivable','Accounts receivable',                  'asset'),
    ('fx_conversion',      'Currency conversion clearing',         'asset'),
    ('revenue',            'Sales revenue',                        'income'),
    ('refunds',            'Refunds',                              'expense'),
    ('chargebacks',        'Chargebacks',                          'expense'),
    ('processing_fees',    'Payment processing fees',              'expense'),
    ('recon_differences',  'Reconciliation differences',           'expense');

CREATE TABLE journal_entries (
    id          bigserial PRIMARY KEY,
    occurred_at timestamptz NOT NULL,
    description text NOT NULL,
    source_ref  text NOT NULL UNIQUE,          -- 'txn:<id>' or 'match:<id>': makes posting idempotent
    created_by  text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE postings (
    id           bigserial PRIMARY KEY,
    entry_id     bigint NOT NULL REFERENCES journal_entries (id),
    account_id   int    NOT NULL REFERENCES accounts (id),
    direction    char(1) NOT NULL CHECK (direction IN ('D', 'C')),
    amount_minor bigint NOT NULL CHECK (amount_minor > 0),
    currency     char(3) NOT NULL
);
CREATE INDEX postings_entry_idx ON postings (entry_id);
CREATE INDEX postings_account_idx ON postings (account_id);

-- Every entry must balance (debits = credits) in each currency, checked when the transaction commits.
CREATE FUNCTION check_entry_balanced() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    eid bigint;
BEGIN
    IF TG_TABLE_NAME = 'journal_entries' THEN eid := NEW.id; ELSE eid := NEW.entry_id; END IF;
    IF (SELECT count(*) FROM postings WHERE entry_id = eid) < 2 THEN
        RAISE EXCEPTION 'journal entry % needs at least two postings', eid;
    END IF;
    IF EXISTS (
        SELECT 1 FROM postings WHERE entry_id = eid
        GROUP BY currency
        HAVING sum(CASE direction WHEN 'D' THEN amount_minor ELSE -amount_minor END) <> 0
    ) THEN
        RAISE EXCEPTION 'journal entry % does not balance', eid;
    END IF;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER entry_balanced_on_entry AFTER INSERT ON journal_entries
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_entry_balanced();
CREATE CONSTRAINT TRIGGER entry_balanced_on_posting AFTER INSERT ON postings
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_entry_balanced();

-- The ledger and the audit log are append-only: corrections are new reversing entries.
CREATE FUNCTION forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only (% not allowed)', TG_TABLE_NAME, TG_OP;
END $$;

CREATE TRIGGER journal_entries_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON journal_entries
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_change();
CREATE TRIGGER postings_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON postings
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_change();

CREATE TABLE audit_log (
    id        bigserial PRIMARY KEY,
    at        timestamptz NOT NULL DEFAULT now(),
    actor     text NOT NULL,                   -- 'user:<name>' or 'process:<name>'
    action    text NOT NULL,                   -- ingested | matched | exception_raised | exception_resolved | manual_match
    entity    text NOT NULL,
    entity_id text NOT NULL,
    detail    jsonb NOT NULL DEFAULT '{}'
);
CREATE TRIGGER audit_log_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_change();
