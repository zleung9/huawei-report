#!/usr/bin/env python3
"""Bootstrap admin account from ADMIN_EMAIL + ADMIN_PASSWORD env vars.

Idempotent: creates if missing, updates password if already exists.
"""
import hashlib
import os
import secrets
import sqlite3
import sys
from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return f"pbkdf2:sha256:200000:{salt}:{dk.hex()}"


def main():
    db_path = os.environ.get("DB_PATH", "/opt/db-data/usage.sqlite")
    email = os.environ.get("ADMIN_EMAIL", "").strip()
    password = os.environ.get("ADMIN_PASSWORD", "")

    if not email or not password:
        print("[admin-bootstrap] ADMIN_EMAIL or ADMIN_PASSWORD not set, skipping")
        return

    username = email.split("@")[0]
    db = sqlite3.connect(db_path)
    db.execute("PRAGMA foreign_keys = ON")

    try:
        ts = now_iso()

        # Check if person exists
        person = db.execute(
            "SELECT id, name, role FROM person WHERE email = ?", (email,)
        ).fetchone()

        if person is None:
            # Create group + person + auth_user
            group_name = f"管理:{username}"
            db.execute(
                "INSERT OR IGNORE INTO groups (name, is_individual, created_at, notes) "
                "VALUES (?, 0, ?, 'admin group')",
                (group_name, ts),
            )
            group_id = db.execute(
                "SELECT id FROM groups WHERE name = ?", (group_name,)
            ).fetchone()[0]

            db.execute(
                "INSERT OR IGNORE INTO person (name, email, role, group_id, created_at, notes) "
                "VALUES (?, ?, 'admin', ?, ?, 'auto-bootstrapped from env')",
                (username, email, group_id, ts),
            )
            person_id = db.execute(
                "SELECT id FROM person WHERE email = ?", (email,)
            ).fetchone()[0]
            print(f"[admin-bootstrap] created person id={person_id}, role=admin")

            pwd_hash = hash_password(password)
            db.execute(
                "INSERT INTO auth_user (person_id, username, password_hash, created_at) "
                "VALUES (?, ?, ?, ?)",
                (person_id, username, pwd_hash, ts),
            )
            print(f"[admin-bootstrap] created auth_user '{username}'")

        else:
            person_id = person[0]
            if person[2] != "admin":
                db.execute(
                    "UPDATE person SET role = 'admin' WHERE id = ?", (person_id,)
                )
                print(f"[admin-bootstrap] upgraded person id={person_id} to admin")

            # Upsert auth_user
            auth = db.execute(
                "SELECT id, password_hash FROM auth_user WHERE person_id = ?",
                (person_id,),
            ).fetchone()

            pwd_hash = hash_password(password)
            if auth is None:
                db.execute(
                    "INSERT INTO auth_user (person_id, username, password_hash, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (person_id, username, pwd_hash, ts),
                )
                print(f"[admin-bootstrap] created auth_user for existing person id={person_id}")
            else:
                db.execute(
                    "UPDATE auth_user SET password_hash = ?, username = ? WHERE person_id = ?",
                    (pwd_hash, username, person_id),
                )
                print(f"[admin-bootstrap] updated auth_user password for '{username}'")

        db.commit()
        print("[admin-bootstrap] done")
    except Exception as e:
        print(f"[admin-bootstrap] ERROR: {e}", file=sys.stderr)
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
