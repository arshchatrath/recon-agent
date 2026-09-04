-- recon-agent storage. Every money column is INTEGER paise. No REALs for money.
-- REAL is used only for confidences, costs and rates, which are not money.

PRAGMA foreign_keys = ON;

-- ------------------------------------------------------------ source data
CREATE TABLE IF NOT EXISTS orders (
    order_id            TEXT PRIMARY KEY,
    batch_id            TEXT NOT NULL,
    customer_name       TEXT,
    order_datetime      TEXT NOT NULL,
    gross_amount_paise  INTEGER NOT NULL,
    instrument          TEXT NOT NULL,
    status              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settlements (
    settlement_txn_id    TEXT PRIMARY KEY,
    batch_id             TEXT NOT NULL,
    -- deliberately NOT a foreign key: orphan settlements claim orders that
    -- do not exist, and losing them would erase a case we must detect.
    order_id_claimed     TEXT,
    settled_datetime     TEXT NOT NULL,
    gross_amount_paise   INTEGER NOT NULL,
    mdr_paise            INTEGER NOT NULL,
    gst_on_mdr_paise     INTEGER NOT NULL,
    net_amount_paise     INTEGER NOT NULL,
    settlement_batch_id  TEXT,
    instrument           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bank_credits (
    utr                  TEXT PRIMARY KEY,
    batch_id             TEXT NOT NULL,
    credit_datetime      TEXT NOT NULL,
    credit_amount_paise  INTEGER NOT NULL,   -- may be negative (a reversal debit)
    narration            TEXT
);

-- ---------------------------------------------------------------- results
CREATE TABLE IF NOT EXISTS matches (
    match_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id     TEXT NOT NULL,
    left_type    TEXT NOT NULL,
    left_id      TEXT NOT NULL,
    right_type   TEXT NOT NULL,
    right_id     TEXT NOT NULL,
    match_kind   TEXT NOT NULL
        CHECK (match_kind IN ('exact','rule','assignment','subset_sum','llm_resolved')),
    resolved_by  TEXT NOT NULL
        CHECK (resolved_by IN ('deterministic','hungarian','mincostflow','subset_sum','llm')),
    rule_id      INTEGER REFERENCES rules(rule_id),
    confidence   REAL,
    cost         REAL,
    explanation  TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_matches_batch ON matches(batch_id);
CREATE INDEX IF NOT EXISTS idx_matches_left  ON matches(left_type, left_id);
CREATE INDEX IF NOT EXISTS idx_matches_right ON matches(right_type, right_id);

CREATE TABLE IF NOT EXISTS exceptions (
    exception_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id            TEXT NOT NULL,
    record_type         TEXT NOT NULL,
    record_id           TEXT NOT NULL,
    reason_code         TEXT NOT NULL,
    reason_text         TEXT,
    money_at_risk_paise INTEGER NOT NULL DEFAULT 0,
    candidates_json     TEXT,
    status              TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','resolved','escalated')),
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_exc_batch  ON exceptions(batch_id, status);
CREATE INDEX IF NOT EXISTS idx_exc_record ON exceptions(record_type, record_id);

-- ----------------------------------------------------- the learned library
CREATE TABLE IF NOT EXISTS rules (
    rule_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_type        TEXT NOT NULL
        CHECK (rule_type IN ('exact_id','fee_formula','timing_window',
                             'refund_pattern','narration_pattern')),
    scope_instrument TEXT NOT NULL DEFAULT 'ALL',
    predicate_json   TEXT NOT NULL,
    priority         INTEGER NOT NULL DEFAULT 100,
    status           TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','rejected','retired')),
    promoted_at                TEXT,
    promoted_from_proposal_id  INTEGER REFERENCES rule_proposals(proposal_id),
    times_applied    INTEGER NOT NULL DEFAULT 0,
    times_correct    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS rule_proposals (
    proposal_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id             TEXT,
    rule_type            TEXT NOT NULL,
    scope_instrument     TEXT NOT NULL DEFAULT 'ALL',
    predicate_json       TEXT NOT NULL,
    fingerprint          TEXT NOT NULL UNIQUE,   -- canonicalised predicate
    proposed_by_case_id  TEXT,
    llm_confidence       REAL,
    occurrence_count     INTEGER NOT NULL DEFAULT 1,
    status               TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','promoted','rejected')),
    rejection_reason     TEXT,
    created_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS rule_audit (
    audit_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    event        TEXT NOT NULL
        CHECK (event IN ('proposed','promoted','rejected','retired','conflict')),
    rule_id      INTEGER REFERENCES rules(rule_id),
    proposal_id  INTEGER REFERENCES rule_proposals(proposal_id),
    batch_id     TEXT,
    detail_json  TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------- metrics
CREATE TABLE IF NOT EXISTS run_metrics (
    run_id               INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id             TEXT NOT NULL,
    run_datetime         TEXT NOT NULL DEFAULT (datetime('now')),
    total_records        INTEGER,
    exact_matches        INTEGER,
    rule_matches         INTEGER,
    assignment_matches   INTEGER,
    subset_sum_matches   INTEGER,
    llm_resolved         INTEGER,
    exceptions_count     INTEGER,
    llm_calls            INTEGER,
    llm_calls_avoided    INTEGER,
    llm_tokens_in        INTEGER,
    llm_tokens_out       INTEGER,
    match_rate           REAL,
    precision_score      REAL,
    recall_score         REAL,
    false_positive_count INTEGER,
    money_at_risk_paise  INTEGER,
    cost_weighted_error  REAL,
    wall_clock_seconds   REAL,
    active_rules_count   INTEGER,
    component_sizes_json TEXT,
    -- contract compliance: what was taken beyond what was agreed
    fee_leakage_paise    INTEGER,
    transactions_overcharged INTEGER,
    contract_deviations  INTEGER
);
