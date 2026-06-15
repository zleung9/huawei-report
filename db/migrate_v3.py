#!/usr/bin/env python3
"""Migrate schema v2 → v3: add credential column to accounts table.

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
        print(f"[migrate-v3] current schema version: {version}")

        if version >= 3:
            print("[migrate-v3] already at v3, nothing to do")
            return

        # Add credential column
        try:
            db.execute("ALTER TABLE accounts ADD COLUMN credential TEXT")
            print("[migrate-v3] added 'credential' column to accounts")
        except sqlite3.OperationalError as e:
            if "duplicate column" in str(e).lower():
                print("[migrate-v3] 'credential' column already exists, skipping")
            else:
                raise

        # Bump schema version
        db.execute("INSERT INTO schema_version (version, applied_at) VALUES (3, datetime('now'))")
        db.commit()
        print("[migrate-v3] migration complete, schema version → 3")
    except Exception as e:
        print(f"[migrate-v3] ERROR: {e}", file=sys.stderr)
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
