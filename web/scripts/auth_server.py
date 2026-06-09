#!/usr/bin/env python3
"""Auth server for Huawei Reports.

Listens on 127.0.0.1:28790. Nginx proxies /api/auth/ requests here.
Uses PBKDF2-SHA256 for password hashing (stdlib only, no extra deps).
"""
import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
import time
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DB_PATH = os.environ.get("DB_PATH", "/opt/db-data/usage.sqlite")
PORT = int(os.environ.get("AUTH_PORT", "28790"))
SESSION_TTL = 86400  # 24 hours
MAX_BODY = 8192
MAX_FAILED = 5
LOCK_MINUTES = 15

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return f"pbkdf2:sha256:200000:{salt}:{dk.hex()}"

def verify_password(password: str, stored: str) -> bool:
    try:
        algo, hash_name, iterations, salt, dk_hex = stored.split(":")
        dk = hashlib.pbkdf2_hmac(
            hash_name, password.encode(), salt.encode(), int(iterations)
        )
        return dk.hex() == dk_hex
    except (ValueError, AttributeError):
        return False

def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


class Handler(BaseHTTPRequestHandler):

    def do_POST(self):
        path = self.path.rstrip("/")
        if path == "/api/auth/login":
            return self._login()
        elif path == "/api/auth/logout":
            return self._logout()
        elif path == "/api/auth/register":
            return self._register()
        return self._json(404, {"error": "not found"})

    def do_GET(self):
        path = self.path.rstrip("/")
        if path == "/api/auth/check":
            return self._check()
        elif path == "/api/auth/profile":
            return self._profile()
        elif path == "/api/auth/health":
            return self._json(200, {"ok": True, "service": "auth"})
        return self._json(404, {"error": "not found"})

    # ── login ──────────────────────────────────────────────────────

    def _login(self):
        body = self._read_body()
        if body is None:
            return
        username = (body.get("username") or "").strip().lower()
        password = body.get("password") or ""

        if not username or not password:
            return self._json(400, {"error": "用户名和密码不能为空"})

        db = get_db()
        try:
            # Check rate limiting
            row = db.execute(
                "SELECT id, locked_until, failed_attempts FROM auth_user WHERE username = ?",
                (username,),
            ).fetchone()
            if row and row["locked_until"]:
                lock_exp = datetime.fromisoformat(row["locked_until"])
                if lock_exp > datetime.now(timezone.utc):
                    remaining = int((lock_exp - datetime.now(timezone.utc)).total_seconds() / 60) + 1
                    return self._json(429, {"error": f"账户已锁定，请 {remaining} 分钟后重试"})

            if not row:
                # Constant-time failure to avoid username enumeration
                hashlib.pbkdf2_hmac("sha256", b"dummy", os.urandom(16), 200_000)
                return self._json(401, {"error": "用户名或密码错误"})

            stored = db.execute(
                "SELECT password_hash FROM auth_user WHERE id = ?", (row["id"],)
            ).fetchone()["password_hash"]

            if not verify_password(password, stored):
                fails = (row["failed_attempts"] or 0) + 1
                lock = None
                if fails >= MAX_FAILED:
                    lock_dt = datetime.now(timezone.utc).timestamp() + LOCK_MINUTES * 60
                    lock = datetime.fromtimestamp(lock_dt, timezone.utc).isoformat(
                        timespec="seconds"
                    )
                db.execute(
                    "UPDATE auth_user SET failed_attempts = ?, locked_until = ? WHERE id = ?",
                    (fails, lock, row["id"]),
                )
                db.commit()
                return self._json(401, {"error": "用户名或密码错误"})

            # Success — reset rate limiting, create session
            db.execute(
                "UPDATE auth_user SET failed_attempts = 0, locked_until = NULL, "
                "last_login_at = ? WHERE id = ?",
                (now_iso(), row["id"]),
            )
            token = secrets.token_hex(32)
            expires = datetime.fromtimestamp(
                time.time() + SESSION_TTL, timezone.utc
            ).isoformat(timespec="seconds")
            db.execute(
                "INSERT INTO auth_session (token, person_id, created_at, expires_at, ip) "
                "VALUES (?, ?, ?, ?, ?)",
                (token, row["id"], now_iso(), expires,
                 self.headers.get("X-Real-IP") or self.client_address[0]),
            )
            # Audit
            db.execute(
                "INSERT INTO audit_log (ts, person_id, action, target, ip) "
                "VALUES (?, ?, 'login', ?, ?)",
                (now_iso(), row["id"], username,
                 self.headers.get("X-Real-IP") or self.client_address[0]),
            )
            db.commit()

            # Fetch person info
            person = db.execute(
                "SELECT id, name, email, role FROM person WHERE id = ?", (row["id"],)
            ).fetchone()
        finally:
            db.close()

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        cookie = (
            f"session={token}; Path=/; HttpOnly; Max-Age={SESSION_TTL}; SameSite=Lax"
        )
        self.send_header("Set-Cookie", cookie)
        body = json.dumps({
            "ok": True,
            "user": {
                "id": person["id"],
                "name": person["name"],
                "email": person["email"],
                "role": person["role"],
            },
        }, ensure_ascii=False).encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── logout ─────────────────────────────────────────────────────

    def _logout(self):
        cookie = self._get_session_cookie()
        if cookie:
            db = get_db()
            try:
                db.execute("DELETE FROM auth_session WHERE token = ?", (cookie,))
                db.commit()
            finally:
                db.close()
        self.send_response(200)
        self.send_header("Set-Cookie",
                         "session=; Path=/; HttpOnly; Max-Age=0; SameSite=Lax")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        body = b'{"ok": true}'
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── check ──────────────────────────────────────────────────────

    def _check(self):
        cookie = self._get_session_cookie()
        if not cookie:
            return self._json(401, {"error": "未登录"})

        db = get_db()
        try:
            row = db.execute(
                "SELECT s.person_id, s.expires_at, p.name, p.email, p.role "
                "FROM auth_session s JOIN person p ON p.id = s.person_id "
                "WHERE s.token = ?",
                (cookie,),
            ).fetchone()

            if not row:
                return self._json(401, {"error": "会话无效"})

            expires = datetime.fromisoformat(row["expires_at"])
            if expires < datetime.now(timezone.utc):
                db.execute("DELETE FROM auth_session WHERE token = ?", (cookie,))
                db.commit()
                return self._json(401, {"error": "会话已过期，请重新登录"})

            return self._json(200, {
                "ok": True,
                "user": {
                    "id": row["person_id"],
                    "name": row["name"],
                    "email": row["email"],
                    "role": row["role"],
                },
            })
        finally:
            db.close()

    # ── register ───────────────────────────────────────────────────

    def _register(self):
        body = self._read_body()
        if body is None:
            return

        name = (body.get("name") or "").strip()
        email = (body.get("email") or "").strip().lower()
        username = (body.get("username") or "").strip().lower()
        password = body.get("password") or ""
        request_hpc = bool(body.get("request_hpc"))
        request_npu = bool(body.get("request_npu"))
        request_llm = bool(body.get("request_llm"))
        hpc_account = (body.get("hpc_account") or "").strip()
        npu_account = (body.get("npu_account") or "").strip()
        llm_note = (body.get("llm_note") or "").strip()
        group_name = (body.get("group") or "").strip()

        # Validation
        if not name:
            return self._json(400, {"error": "姓名不能为空"})
        if not email or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            return self._json(400, {"error": "请输入有效的邮箱"})
        if not username or not re.match(r"^[a-z0-9_]{3,32}$", username):
            return self._json(400, {"error": "用户名需为 3-32 位小写字母、数字或下划线"})
        if len(password) < 8:
            return self._json(400, {"error": "密码至少 8 位"})

        # HPC account name validation
        if request_hpc and not hpc_account:
            return self._json(400, {"error": "申请 HPC 账号需填写希望使用的账号名"})
        if request_npu and not npu_account:
            return self._json(400, {"error": "申请 NPU 账号需填写希望使用的账号名"})

        db = get_db()
        try:
            # Check uniqueness
            if db.execute("SELECT 1 FROM auth_user WHERE username = ?", (username,)).fetchone():
                return self._json(409, {"error": "用户名已存在"})
            if db.execute("SELECT 1 FROM person WHERE email = ?", (email,)).fetchone():
                return self._json(409, {"error": "该邮箱已注册"})

            # Create individual group
            grp_name = group_name if group_name else f"个人:{name}"
            # Avoid group name collision
            suffix = ""
            while db.execute("SELECT 1 FROM groups WHERE name = ?", (grp_name + suffix,)).fetchone():
                suffix = f"_{secrets.token_hex(2)}" if suffix else "_2"
            grp_name = grp_name + suffix
            now = now_iso()
            db.execute(
                "INSERT INTO groups (name, is_individual, billing_contact_email, created_at) "
                "VALUES (?, 1, ?, ?)",
                (grp_name, email, now),
            )
            group_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

            # Create person
            db.execute(
                "INSERT INTO person (name, email, role, group_id, created_at) "
                "VALUES (?, ?, 'user', ?, ?)",
                (name, email, group_id, now),
            )
            person_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

            # Create auth_user
            pw_hash = hash_password(password)
            db.execute(
                "INSERT INTO auth_user (person_id, username, password_hash, created_at) "
                "VALUES (?, ?, ?, ?)",
                (person_id, username, pw_hash, now),
            )

            # Create accounts based on requests
            accounts_created = []
            if request_hpc:
                # Check if hpc_account already taken
                if db.execute(
                    "SELECT 1 FROM accounts WHERE system = 'hpc' AND account_name = ?",
                    (hpc_account,)
                ).fetchone():
                    return self._json(409, {"error": f"HPC 账号 '{hpc_account}' 已被占用"})
                db.execute(
                    "INSERT INTO accounts (person_id, system, account_name, created_at) "
                    "VALUES (?, 'hpc', ?, ?)",
                    (person_id, hpc_account, now),
                )
                accounts_created.append({"system": "hpc", "account_name": hpc_account})

            if request_npu:
                if db.execute(
                    "SELECT 1 FROM accounts WHERE system = 'npu' AND account_name = ?",
                    (npu_account,)
                ).fetchone():
                    return self._json(409, {"error": f"NPU 账号 '{npu_account}' 已被占用"})
                db.execute(
                    "INSERT INTO accounts (person_id, system, account_name, created_at) "
                    "VALUES (?, 'npu', ?, ?)",
                    (person_id, npu_account, now),
                )
                accounts_created.append({"system": "npu", "account_name": npu_account})

            if request_llm:
                # API key requires admin review — create a pending request
                api_key_name = f"llm:{username}:pending"
                db.execute(
                    "INSERT INTO accounts (person_id, system, account_name, created_at) "
                    "VALUES (?, 'llm', ?, ?)",
                    (person_id, api_key_name, now),
                )
                accounts_created.append({
                    "system": "llm",
                    "account_name": api_key_name,
                    "status": "pending_review",
                    "note": "等待管理员审核",
                })

            # Audit
            ip = self.headers.get("X-Real-IP") or self.client_address[0]
            db.execute(
                "INSERT INTO audit_log (ts, person_id, action, target, ip) "
                "VALUES (?, ?, 'register', ?, ?)",
                (now, person_id, username, ip),
            )
            db.commit()

        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        self._json(201, {
            "ok": True,
            "user": {
                "name": name,
                "email": email,
                "username": username,
            },
            "accounts": accounts_created,
        })

    # ── profile ────────────────────────────────────────────────────

    def _profile(self):
        cookie = self._get_session_cookie()
        if not cookie:
            return self._json(401, {"error": "未登录"})

        db = get_db()
        try:
            # Verify session
            row = db.execute(
                "SELECT s.person_id, s.expires_at "
                "FROM auth_session s WHERE s.token = ?",
                (cookie,),
            ).fetchone()
            if not row:
                return self._json(401, {"error": "会话无效"})
            expires = datetime.fromisoformat(row["expires_at"])
            if expires < datetime.now(timezone.utc):
                return self._json(401, {"error": "会话已过期"})

            person_id = row["person_id"]

            # Person info
            person = db.execute(
                "SELECT id, name, email, role FROM person WHERE id = ?",
                (person_id,),
            ).fetchone()
            if not person:
                return self._json(404, {"error": "用户不存在"})

            # Accounts
            accts = db.execute(
                "SELECT system, account_name, created_at FROM accounts "
                "WHERE person_id = ? ORDER BY system, account_name",
                (person_id,),
            ).fetchall()
            accounts = []
            for a in accts:
                entry = {"system": a["system"], "account_name": a["account_name"], "created_at": a["created_at"]}
                # LLM pending marker
                if a["system"] == "llm" and a["account_name"].endswith(":pending"):
                    entry["status"] = "pending_review"
                accounts.append(entry)

            # HPC usage summary (last 30 days)
            hpc_acct_names = [a["account_name"] for a in accts if a["system"] == "hpc"]
            hpc_usage = []
            if hpc_acct_names:
                placeholders = ",".join("?" * len(hpc_acct_names))
                usage_rows = db.execute(
                    f"SELECT date, account_name, core_hours, job_count "
                    f"FROM hpc_daily WHERE account_name IN ({placeholders}) "
                    f"AND date >= date('now', '-30 days') "
                    f"ORDER BY date DESC",
                    hpc_acct_names,
                ).fetchall()
                for u in usage_rows:
                    hpc_usage.append({
                        "date": u["date"],
                        "account_name": u["account_name"],
                        "core_hours": u["core_hours"],
                        "job_count": u["job_count"],
                    })

            # Group info
            group_row = db.execute(
                "SELECT g.name, g.is_individual FROM groups g "
                "JOIN person p ON p.group_id = g.id WHERE p.id = ?",
                (person_id,),
            ).fetchone()

            return self._json(200, {
                "ok": True,
                "user": {
                    "id": person["id"],
                    "name": person["name"],
                    "email": person["email"],
                    "role": person["role"],
                    "group": group_row["name"] if group_row else None,
                },
                "accounts": accounts,
                "hpc_usage": hpc_usage,
            })
        finally:
            db.close()

    # ── helpers ────────────────────────────────────────────────────

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > MAX_BODY:
            self._json(413, {"error": "请求体过大或为空"})
            return None
        try:
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json(400, {"error": "无效的 JSON"})
            return None

    def _get_session_cookie(self):
        raw = self.headers.get("Cookie", "")
        try:
            cookies = SimpleCookie(raw)
            session = cookies.get("session")
            return session.value if session else None
        except Exception:
            return None

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write(
            f"[auth] {self.address_string()} {self.log_date_time_string()} "
            f"{fmt % args}\n")
        sys.stderr.flush()


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    sys.stderr.write(f"[auth] listening on 127.0.0.1:{PORT}, db={DB_PATH}\n")
    sys.stderr.flush()
    server.serve_forever()
