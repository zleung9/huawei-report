#!/usr/bin/env python3
"""Initialise usage.sqlite from schema.sql and seed person/accounts/groups
from the current sacct user list.

Idempotent: safe to re-run; uses INSERT OR IGNORE on uniqueness keys.

Usage:
    python3 init_db.py <db_path> <schema_path> [<sacct_raw.tsv>]

If sacct_raw.tsv is omitted, person/accounts/groups are not seeded.
"""
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_schema(db: sqlite3.Connection, schema_sql: str) -> None:
    db.executescript(schema_sql)


def sacct_users(raw_path: Path) -> set[str]:
    users: set[str] = set()
    with raw_path.open() as f:
        header = None
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            if header is None:
                header = line.split("|")
                continue
            cols = line.split("|")
            if len(cols) < len(header):
                continue
            row = dict(zip(header, cols))
            user = (row.get("User") or "").strip()
            if user:
                users.add(user)
    return users


def seed_users(db: sqlite3.Connection, users: set[str]) -> tuple[int, int, int]:
    """Insert any missing person + single-person group + hpc account.

    Returns (new_groups, new_persons, new_accounts).
    """
    ts = now_iso()
    new_groups = new_persons = new_accounts = 0
    for u in sorted(users):
        group_name = f"个人:{u}"
        cur = db.execute(
            "INSERT OR IGNORE INTO groups (name, is_individual, created_at, notes) "
            "VALUES (?, 1, ?, 'auto-seeded from sacct')",
            (group_name, ts),
        )
        new_groups += cur.rowcount
        group_id = db.execute(
            "SELECT id FROM groups WHERE name = ?", (group_name,)
        ).fetchone()[0]

        cur = db.execute(
            "INSERT OR IGNORE INTO person (name, email, group_id, created_at, notes) "
            "VALUES (?, NULL, ?, ?, 'auto-seeded from sacct; please fill email')",
            (u, group_id, ts),
        )
        new_persons += cur.rowcount
        person_id = db.execute(
            "SELECT id FROM person WHERE name = ? AND group_id = ?",
            (u, group_id),
        ).fetchone()[0]

        cur = db.execute(
            "INSERT OR IGNORE INTO accounts (person_id, system, account_name, created_at) "
            "VALUES (?, 'hpc', ?, ?)",
            (person_id, u, ts),
        )
        new_accounts += cur.rowcount

    db.commit()
    return new_groups, new_persons, new_accounts


def main() -> None:
    if len(sys.argv) < 3:
        print("usage: init_db.py <db_path> <schema_path> [<sacct_raw.tsv>]",
              file=sys.stderr)
        sys.exit(1)

    db_path = Path(sys.argv[1])
    schema_path = Path(sys.argv[2])
    sacct_path = Path(sys.argv[3]) if len(sys.argv) > 3 else None

    db_path.parent.mkdir(parents=True, exist_ok=True)
    schema_sql = schema_path.read_text()

    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA foreign_keys = ON")
        init_schema(db, schema_sql)
        version = db.execute(
            "SELECT max(version) FROM schema_version"
        ).fetchone()[0]
        print(f"[init_db] schema version: {version}")

        if sacct_path is not None and sacct_path.is_file():
            users = sacct_users(sacct_path)
            print(f"[init_db] sacct users to seed: {len(users)} ({sorted(users)})")
            g, p, a = seed_users(db, users)
            print(f"[init_db] inserted: {g} groups, {p} persons, {a} accounts")
        else:
            print("[init_db] no sacct file provided; skipping seed")

        # Counters
        cnts = {
            "groups":   db.execute("SELECT COUNT(*) FROM groups").fetchone()[0],
            "person":   db.execute("SELECT COUNT(*) FROM person").fetchone()[0],
            "accounts": db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0],
            "hpc_daily": db.execute("SELECT COUNT(*) FROM hpc_daily").fetchone()[0],
        }
        print(f"[init_db] table counts: {cnts}")


if __name__ == "__main__":
    main()
