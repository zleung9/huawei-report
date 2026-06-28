#!/usr/bin/env python3
"""Migrate schema v4 → v5: expand accounts table + add llm_daily.

Changes:
  - accounts: add platform_key_id, api_key, hpc_account, npu_account columns
  - Backfill hpc_account/npu_account from account_name for existing rows
  - Backfill api_key from credential for existing llm rows
  - Create llm_daily table (token usage per API key per model per day)

Idempotent: safe to re-run; uses ALTER TABLE … ADD COLUMN which is a no-op
if the column already exists (SQLite raises OperationalError which we catch).
"""
import sqlite3
import sys


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else "/opt/db-data/usage.sqlite"
    db = sqlite3.connect(db_path)
    db.execute("PRAGMA foreign_keys = ON")

    try:
        version = db.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_version"
        ).fetchone()[0]
        print(f"[migrate-v5] current schema version: {version}")

        if version >= 5:
            print("[migrate-v5] already at v5, nothing to do")
            return

        # ── Expand accounts table ──────────────────────────────────
        new_columns = [
            ("platform_key_id", "INTEGER"),
            ("api_key",         "TEXT"),
            ("hpc_account",     "TEXT"),
            ("npu_account",     "TEXT"),
        ]
        for col_name, col_type in new_columns:
            try:
                db.execute(f"ALTER TABLE accounts ADD COLUMN {col_name} {col_type}")
                print(f"[migrate-v5] added '{col_name}' column to accounts")
            except sqlite3.OperationalError as e:
                if "duplicate column" in str(e).lower():
                    print(f"[migrate-v5] '{col_name}' column already exists, skipping")
                else:
                    raise

        # Backfill: hpc_account from account_name for hpc rows
        db.execute(
            "UPDATE accounts SET hpc_account = account_name "
            "WHERE system = 'hpc' AND hpc_account IS NULL"
        )
        # Backfill: npu_account from account_name for npu rows
        db.execute(
            "UPDATE accounts SET npu_account = account_name "
            "WHERE system = 'npu' AND npu_account IS NULL"
        )
        # Backfill: api_key from credential for llm rows
        db.execute(
            "UPDATE accounts SET api_key = credential "
            "WHERE system = 'llm' AND api_key IS NULL AND credential IS NOT NULL"
        )
        print("[migrate-v5] backfilled hpc_account, npu_account, api_key")

        # ── Create llm_daily table ─────────────────────────────────
        db.execute("""
            CREATE TABLE IF NOT EXISTS llm_daily (
                date TEXT NOT NULL,
                api_key_id INTEGER NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                input_rate REAL,
                output_rate REAL,
                request_count INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'gateway',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (date, api_key_id, model)
            )
        """)
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_llm_daily_date ON llm_daily(date)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_llm_daily_key ON llm_daily(api_key_id)"
        )
        print("[migrate-v5] created llm_daily table + indexes")

        # ── Bump schema version ────────────────────────────────────
        db.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (5, datetime('now'))"
        )
        db.commit()
        print("[migrate-v5] migration complete, schema version → 5")
    except Exception as e:
        print(f"[migrate-v5] ERROR: {e}", file=sys.stderr)
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
