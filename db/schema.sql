-- usage.sqlite schema v4 (2026-06-19)
-- v4: added accounts.status column for suspend/activate lifecycle

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- Billing units. Individual users live in their own "single-person group"
-- (is_individual=1, name='个人:<account>') until reassigned.
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    is_individual INTEGER NOT NULL DEFAULT 0,
    billing_contact_email TEXT,
    created_at TEXT NOT NULL,
    notes TEXT
);

-- Natural persons. One person ↔ one group ↔ many accounts.
CREATE TABLE IF NOT EXISTS person (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT UNIQUE,
    role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('user','admin')),
    group_id INTEGER REFERENCES groups(id),
    created_at TEXT NOT NULL,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_person_group ON person(group_id);

-- A person's identity on each compute system.
-- credential stores: HPC/NPU login password, LLM API key (or NULL if pending).
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    system TEXT NOT NULL CHECK(system IN ('hpc','npu','llm')),
    account_name TEXT NOT NULL,
    credential TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','suspended','pending_review')),
    created_at TEXT NOT NULL,
    UNIQUE(system, account_name)
);

CREATE INDEX IF NOT EXISTS idx_accounts_person ON accounts(person_id);
CREATE INDEX IF NOT EXISTS idx_accounts_system_name ON accounts(system, account_name);

-- HPC daily usage. account_name is intentionally a free-form string (not a FK
-- to accounts) so the cron job can ingest new sacct usernames before an admin
-- maps them to a person. Reports JOIN via accounts.account_name when needed.
CREATE TABLE IF NOT EXISTS hpc_daily (
    date TEXT NOT NULL,                   -- YYYY-MM-DD
    account_name TEXT NOT NULL,           -- slurm User
    core_hours REAL NOT NULL DEFAULT 0,
    job_count INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'sacct',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (date, account_name)
);

CREATE INDEX IF NOT EXISTS idx_hpc_daily_date ON hpc_daily(date);
CREATE INDEX IF NOT EXISTS idx_hpc_daily_account ON hpc_daily(account_name);

-- Future tables (uncomment + ALTER schema_version when adding):
--
-- CREATE TABLE npu_daily (
--     date TEXT NOT NULL,
--     account_name TEXT NOT NULL,
--     card_hours REAL NOT NULL DEFAULT 0,
--     job_count INTEGER NOT NULL DEFAULT 0,
--     source TEXT NOT NULL DEFAULT 'mindcluster',
--     updated_at TEXT NOT NULL,
--     PRIMARY KEY (date, account_name)
-- );
--
-- CREATE TABLE llm_daily (
--     date TEXT NOT NULL,
--     account_name TEXT NOT NULL,        -- API key id or username
--     model TEXT NOT NULL,
--     tokens_in INTEGER NOT NULL DEFAULT 0,
--     tokens_out INTEGER NOT NULL DEFAULT 0,
--     request_count INTEGER NOT NULL DEFAULT 0,
--     p95_latency_ms REAL,
--     source TEXT NOT NULL DEFAULT 'gateway',
--     updated_at TEXT NOT NULL,
--     PRIMARY KEY (date, account_name, model)
-- );

-- Auth tables (v2)
CREATE TABLE IF NOT EXISTS auth_user (
    id INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES person(id),
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,            -- PBKDF2-SHA256
    must_change_password INTEGER NOT NULL DEFAULT 0,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    last_login_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_session (
    token TEXT PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES person(id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    ip TEXT,
    user_agent TEXT
);

CREATE INDEX IF NOT EXISTS idx_auth_session_person ON auth_session(person_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    person_id INTEGER REFERENCES person(id),
    action TEXT NOT NULL,
    target TEXT,
    detail_json TEXT,
    ip TEXT
);

INSERT OR IGNORE INTO schema_version (version, applied_at)
VALUES (2, datetime('now'));

INSERT OR IGNORE INTO schema_version (version, applied_at)
VALUES (3, datetime('now'));

INSERT OR IGNORE INTO schema_version (version, applied_at)
VALUES (4, datetime('now'));
