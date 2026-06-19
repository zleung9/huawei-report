#!/usr/bin/env python3
"""Migrate schema v3 → v4: add status column to accounts table.

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
        # Check current schema version
        version = db.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_version"
        ).fetchone()[0]
        print(f"[migrate-v4] current schema version: {version}")

        if version >= 4:
            print("[migrate-v4] already at v4, nothing to do")
            return

        # Add status column
        try:
            db.execute(
                "ALTER TABLE accounts ADD COLUMN status TEXT NOT NULL DEFAULT 'active' "
                "CHECK(status IN ('active','suspended','pending_review'))"
            )
            print("[migrate-v4] added 'status' column to accounts")
        except sqlite3.OperationalError as e:
            if "duplicate column" in str(e).lower():
                print("[migrate-v4] 'status' column already exists, skipping")
            else:
                raise

        # Bump schema version
        db.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (4, datetime('now'))"
        )
        db.commit()
        print("[migrate-v4] migration complete, schema version → 4")
    except Exception as e:
        print(f"[migrate-v4] ERROR: {e}", file=sys.stderr)
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
