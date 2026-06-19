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
import string
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

def generate_password(length: int = 12) -> str:
    """Generate a random password: letters + digits, guaranteed 1 of each."""
    alpha = string.ascii_letters
    digits = string.digits
    pool = alpha + digits
    while True:
        pw = "".join(secrets.choice(pool) for _ in range(length))
        if any(c in alpha for c in pw) and any(c in digits for c in pw):
            return pw

def generate_api_key(prefix: str = "sk") -> str:
    """Generate an API key like sk-xxxxxxxxxxxxxxxxxxxxxxxx."""
    return f"{prefix}-{secrets.token_hex(24)}"

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
        elif path == "/api/auth/apply-account":
            return self._apply_account()
        elif path == "/api/auth/suspend-account":
            return self._suspend_account()
        elif path == "/api/auth/activate-account":
            return self._activate_account()
        # Admin endpoints
        elif path == "/api/auth/admin/users":
            return self._admin_require(self._admin_list_users)
        elif re.match(r"^/api/auth/admin/users/\d+/reset-password$", path):
            return self._admin_require(self._admin_reset_password)
        elif re.match(r"^/api/auth/admin/users/\d+/set-role$", path):
            return self._admin_require(self._admin_set_role)
        elif re.match(r"^/api/auth/admin/accounts/\d+/suspend$", path):
            return self._admin_require(self._admin_suspend_account)
        elif re.match(r"^/api/auth/admin/accounts/\d+/activate$", path):
            return self._admin_require(self._admin_activate_account)
        elif re.match(r"^/api/auth/admin/accounts/\d+/reset-credential$", path):
            return self._admin_require(self._admin_reset_credential)
        return self._json(404, {"error": "not found"})

    def do_GET(self):
        path = self.path.rstrip("/")
        if path == "/api/auth/check":
            return self._check()
        elif path == "/api/auth/profile":
            return self._profile()
        elif path == "/api/auth/health":
            return self._json(200, {"ok": True, "service": "auth"})
        elif re.match(r"^/api/auth/admin/users/\d+$", path):
            return self._admin_require(self._admin_get_user)
        elif path == "/api/auth/admin/users":
            return self._admin_require(self._admin_list_users)
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
                "SELECT id, person_id, locked_until, failed_attempts FROM auth_user WHERE username = ?",
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
                (token, row["person_id"], now_iso(), expires,
                 self.headers.get("X-Real-IP") or self.client_address[0]),
            )
            # Audit
            db.execute(
                "INSERT INTO audit_log (ts, person_id, action, target, ip) "
                "VALUES (?, ?, 'login', ?, ?)",
                (now_iso(), row["person_id"], username,
                 self.headers.get("X-Real-IP") or self.client_address[0]),
            )
            db.commit()

            # Fetch person info
            person = db.execute(
                "SELECT id, name, email, role FROM person WHERE id = ?", (row["person_id"],)
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

            # Accounts (include credential for the user's own view)
            accts = db.execute(
                "SELECT id, system, account_name, credential, status, created_at FROM accounts "
                "WHERE person_id = ? ORDER BY system, account_name",
                (person_id,),
            ).fetchall()
            accounts = []
            for a in accts:
                entry = {
                    "id": a["id"],
                    "system": a["system"],
                    "account_name": a["account_name"],
                    "created_at": a["created_at"],
                    "credential": a["credential"],
                    "status": a["status"] if a["status"] else ("pending_review" if a["system"] == "llm" and not a["credential"] else "active"),
                }
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

    # ── apply account ──────────────────────────────────────────────

    def _apply_account(self):
        """User applies for a new HPC/NPU/LLM account from their profile."""
        cookie = self._get_session_cookie()
        if not cookie:
            return self._json(401, {"error": "未登录"})

        body = self._read_body()
        if body is None:
            return

        system = (body.get("system") or "").strip().lower()
        account_name = (body.get("account_name") or "").strip()
        note = (body.get("note") or "").strip()

        if system not in ("hpc", "npu", "llm"):
            return self._json(400, {"error": "无效的系统类型"})

        if system in ("hpc", "npu") and not account_name:
            return self._json(400, {"error": f"{system.upper()} 账号需填写账号名"})

        # Validate account_name format for hpc/npu
        if system in ("hpc", "npu") and not re.match(r"^[a-z0-9_]{2,32}$", account_name):
            return self._json(400, {"error": "账号名需为 2-32 位小写字母、数字或下划线"})

        db = get_db()
        try:
            person_id = self._verify_session(db, cookie)
            if person_id is None:
                return self._json(401, {"error": "会话无效或已过期"})

            # Check uniqueness
            if system in ("hpc", "npu"):
                if db.execute(
                    "SELECT 1 FROM accounts WHERE system = ? AND account_name = ?",
                    (system, account_name)
                ).fetchone():
                    return self._json(409, {"error": f"{system.upper()} 账号 '{account_name}' 已被占用"})

            # Check if user already has an account of this system type
            existing = db.execute(
                "SELECT id FROM accounts WHERE person_id = ? AND system = ?",
                (person_id, system)
            ).fetchone()
            if existing:
                return self._json(409, {"error": f"您已有 {system.upper()} 账号，不能重复申请"})

            now = now_iso()
            if system == "llm":
                # Generate API key
                credential = generate_api_key()
                account_name = f"llm:{note}" if note else f"llm:auto-{secrets.token_hex(4)}"
            else:
                credential = generate_password()

            db.execute(
                "INSERT INTO accounts (person_id, system, account_name, credential, status, created_at) "
                "VALUES (?, ?, ?, ?, 'active', ?)",
                (person_id, system, account_name, credential, now),
            )
            acct_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

            # Audit
            ip = self.headers.get("X-Real-IP") or self.client_address[0]
            db.execute(
                "INSERT INTO audit_log (ts, person_id, action, target, detail_json, ip) "
                "VALUES (?, ?, 'apply_account', ?, ?, ?)",
                (now, person_id, f"{system}:{account_name}",
                 json.dumps({"system": system, "account_name": account_name}), ip),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        self._json(201, {
            "ok": True,
            "account": {
                "id": acct_id,
                "system": system,
                "account_name": account_name,
                "credential": credential,
                "status": "active",
            },
        })

    # ── suspend account ────────────────────────────────────────────

    def _suspend_account(self):
        """User suspends their own account."""
        cookie = self._get_session_cookie()
        if not cookie:
            return self._json(401, {"error": "未登录"})

        body = self._read_body()
        if body is None:
            return

        account_id = body.get("account_id")
        if account_id is None:
            return self._json(400, {"error": "缺少 account_id"})

        db = get_db()
        try:
            person_id = self._verify_session(db, cookie)
            if person_id is None:
                return self._json(401, {"error": "会话无效或已过期"})

            acct = db.execute(
                "SELECT id, system, account_name, status FROM accounts WHERE id = ? AND person_id = ?",
                (account_id, person_id),
            ).fetchone()
            if not acct:
                return self._json(404, {"error": "账号不存在或不属于您"})
            if acct["status"] == "suspended":
                return self._json(400, {"error": "账号已处于停用状态"})

            db.execute(
                "UPDATE accounts SET status = 'suspended' WHERE id = ?",
                (account_id,),
            )
            # Audit
            ip = self.headers.get("X-Real-IP") or self.client_address[0]
            db.execute(
                "INSERT INTO audit_log (ts, person_id, action, target, ip) "
                "VALUES (?, ?, 'suspend_account', ?, ?)",
                (now_iso(), person_id, f"{acct['system']}:{acct['account_name']}", ip),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        self._json(200, {"ok": True, "status": "suspended"})

    # ── activate account ───────────────────────────────────────────

    def _activate_account(self):
        """User re-activates their own suspended account."""
        cookie = self._get_session_cookie()
        if not cookie:
            return self._json(401, {"error": "未登录"})

        body = self._read_body()
        if body is None:
            return

        account_id = body.get("account_id")
        if account_id is None:
            return self._json(400, {"error": "缺少 account_id"})

        db = get_db()
        try:
            person_id = self._verify_session(db, cookie)
            if person_id is None:
                return self._json(401, {"error": "会话无效或已过期"})

            acct = db.execute(
                "SELECT id, system, account_name, status FROM accounts WHERE id = ? AND person_id = ?",
                (account_id, person_id),
            ).fetchone()
            if not acct:
                return self._json(404, {"error": "账号不存在或不属于您"})
            if acct["status"] == "active":
                return self._json(400, {"error": "账号已处于启用状态"})

            db.execute(
                "UPDATE accounts SET status = 'active' WHERE id = ?",
                (account_id,),
            )
            # Audit
            ip = self.headers.get("X-Real-IP") or self.client_address[0]
            db.execute(
                "INSERT INTO audit_log (ts, person_id, action, target, ip) "
                "VALUES (?, ?, 'activate_account', ?, ?)",
                (now_iso(), person_id, f"{acct['system']}:{acct['account_name']}", ip),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        self._json(200, {"ok": True, "status": "active"})

    # ── admin guard ────────────────────────────────────────────────

    def _admin_require(self, handler):
        """Wrapper: verify session + admin role before calling handler."""
        cookie = self._get_session_cookie()
        if not cookie:
            return self._json(401, {"error": "未登录"})

        db = get_db()
        try:
            row = db.execute(
                "SELECT s.person_id, s.expires_at, p.role "
                "FROM auth_session s JOIN person p ON p.id = s.person_id "
                "WHERE s.token = ?",
                (cookie,),
            ).fetchone()
            if not row:
                return self._json(401, {"error": "会话无效"})
            expires = datetime.fromisoformat(row["expires_at"])
            if expires < datetime.now(timezone.utc):
                return self._json(401, {"error": "会话已过期"})
            if row["role"] != "admin":
                return self._json(403, {"error": "需要管理员权限"})
        finally:
            db.close()

        return handler(cookie)

    # ── admin: list users ──────────────────────────────────────────

    def _admin_list_users(self, cookie):
        db = get_db()
        try:
            rows = db.execute(
                "SELECT p.id, p.name, p.email, p.role, p.created_at, "
                "u.username, g.name AS group_name, "
                "(SELECT COUNT(*) FROM accounts a WHERE a.person_id = p.id) AS account_count "
                "FROM person p "
                "LEFT JOIN auth_user u ON u.person_id = p.id "
                "LEFT JOIN groups g ON g.id = p.group_id "
                "ORDER BY p.created_at DESC"
            ).fetchall()
            users = []
            for r in rows:
                users.append({
                    "id": r["id"],
                    "name": r["name"],
                    "email": r["email"],
                    "role": r["role"],
                    "username": r["username"],
                    "group": r["group_name"],
                    "account_count": r["account_count"],
                    "created_at": r["created_at"],
                })
        finally:
            db.close()
        return self._json(200, {"ok": True, "users": users})

    # ── admin: get user detail ─────────────────────────────────────

    def _admin_get_user(self, cookie):
        # Extract user id from path: /api/auth/admin/users/:id
        parts = self.path.rstrip("/").split("/")
        try:
            user_id = int(parts[-1])
        except (ValueError, IndexError):
            return self._json(400, {"error": "无效的用户 ID"})

        db = get_db()
        try:
            person = db.execute(
                "SELECT p.id, p.name, p.email, p.role, p.created_at, p.notes, "
                "u.username, u.must_change_password, u.last_login_at, "
                "g.name AS group_name "
                "FROM person p "
                "LEFT JOIN auth_user u ON u.person_id = p.id "
                "LEFT JOIN groups g ON g.id = p.group_id "
                "WHERE p.id = ?",
                (user_id,),
            ).fetchone()
            if not person:
                return self._json(404, {"error": "用户不存在"})

            accts = db.execute(
                "SELECT id, system, account_name, credential, status, created_at "
                "FROM accounts WHERE person_id = ? ORDER BY system, account_name",
                (user_id,),
            ).fetchall()
            accounts = [dict(a) for a in accts]

            # HPC usage summary (last 30 days)
            hpc_names = [a["account_name"] for a in accts if a["system"] == "hpc"]
            hpc_usage = []
            if hpc_names:
                ph = ",".join("?" * len(hpc_names))
                usage_rows = db.execute(
                    f"SELECT date, account_name, core_hours, job_count "
                    f"FROM hpc_daily WHERE account_name IN ({ph}) "
                    f"AND date >= date('now', '-30 days') ORDER BY date DESC",
                    hpc_names,
                ).fetchall()
                hpc_usage = [dict(u) for u in usage_rows]

        finally:
            db.close()

        return self._json(200, {
            "ok": True,
            "user": {
                "id": person["id"],
                "name": person["name"],
                "email": person["email"],
                "role": person["role"],
                "username": person["username"],
                "group": person["group_name"],
                "must_change_password": person["must_change_password"],
                "last_login_at": person["last_login_at"],
                "created_at": person["created_at"],
                "notes": person["notes"],
            },
            "accounts": accounts,
            "hpc_usage": hpc_usage,
        })

    # ── admin: reset password ──────────────────────────────────────

    def _admin_reset_password(self, cookie):
        parts = self.path.rstrip("/").split("/")
        try:
            user_id = int(parts[-2])
        except (ValueError, IndexError):
            return self._json(400, {"error": "无效的用户 ID"})

        db = get_db()
        try:
            auth = db.execute(
                "SELECT id, username FROM auth_user WHERE person_id = ?",
                (user_id,),
            ).fetchone()
            if not auth:
                return self._json(404, {"error": "用户不存在"})

            new_pw = generate_password()
            pw_hash = hash_password(new_pw)
            db.execute(
                "UPDATE auth_user SET password_hash = ?, must_change_password = 1, "
                "failed_attempts = 0, locked_until = NULL WHERE id = ?",
                (pw_hash, auth["id"]),
            )
            # Audit
            admin_pid = self._session_person_id(db, cookie)
            ip = self.headers.get("X-Real-IP") or self.client_address[0]
            db.execute(
                "INSERT INTO audit_log (ts, person_id, action, target, ip) "
                "VALUES (?, ?, 'admin_reset_password', ?, ?)",
                (now_iso(), admin_pid, auth["username"], ip),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        return self._json(200, {"ok": True, "new_password": new_pw})

    # ── admin: set role ────────────────────────────────────────────

    def _admin_set_role(self, cookie):
        parts = self.path.rstrip("/").split("/")
        try:
            user_id = int(parts[-2])
        except (ValueError, IndexError):
            return self._json(400, {"error": "无效的用户 ID"})

        body = self._read_body()
        if body is None:
            return

        new_role = (body.get("role") or "").strip()
        if new_role not in ("user", "admin"):
            return self._json(400, {"error": "无效的角色"})

        db = get_db()
        try:
            person = db.execute(
                "SELECT id, name, role FROM person WHERE id = ?", (user_id,)
            ).fetchone()
            if not person:
                return self._json(404, {"error": "用户不存在"})

            db.execute("UPDATE person SET role = ? WHERE id = ?", (new_role, user_id))

            # Audit
            admin_pid = self._session_person_id(db, cookie)
            ip = self.headers.get("X-Real-IP") or self.client_address[0]
            db.execute(
                "INSERT INTO audit_log (ts, person_id, action, target, detail_json, ip) "
                "VALUES (?, ?, 'admin_set_role', ?, ?, ?)",
                (now_iso(), admin_pid, person["name"],
                 json.dumps({"from": person["role"], "to": new_role}), ip),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        return self._json(200, {"ok": True, "role": new_role})

    # ── admin: suspend account ─────────────────────────────────────

    def _admin_suspend_account(self, cookie):
        parts = self.path.rstrip("/").split("/")
        try:
            acct_id = int(parts[-2])
        except (ValueError, IndexError):
            return self._json(400, {"error": "无效的账号 ID"})

        db = get_db()
        try:
            acct = db.execute(
                "SELECT id, system, account_name, status FROM accounts WHERE id = ?",
                (acct_id,),
            ).fetchone()
            if not acct:
                return self._json(404, {"error": "账号不存在"})
            if acct["status"] == "suspended":
                return self._json(400, {"error": "账号已处于停用状态"})

            db.execute("UPDATE accounts SET status = 'suspended' WHERE id = ?", (acct_id,))

            admin_pid = self._session_person_id(db, cookie)
            ip = self.headers.get("X-Real-IP") or self.client_address[0]
            db.execute(
                "INSERT INTO audit_log (ts, person_id, action, target, ip) "
                "VALUES (?, ?, 'admin_suspend_account', ?, ?)",
                (now_iso(), admin_pid, f"{acct['system']}:{acct['account_name']}", ip),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        return self._json(200, {"ok": True, "status": "suspended"})

    # ── admin: activate account ────────────────────────────────────

    def _admin_activate_account(self, cookie):
        parts = self.path.rstrip("/").split("/")
        try:
            acct_id = int(parts[-2])
        except (ValueError, IndexError):
            return self._json(400, {"error": "无效的账号 ID"})

        db = get_db()
        try:
            acct = db.execute(
                "SELECT id, system, account_name, status FROM accounts WHERE id = ?",
                (acct_id,),
            ).fetchone()
            if not acct:
                return self._json(404, {"error": "账号不存在"})
            if acct["status"] == "active":
                return self._json(400, {"error": "账号已处于启用状态"})

            db.execute("UPDATE accounts SET status = 'active' WHERE id = ?", (acct_id,))

            admin_pid = self._session_person_id(db, cookie)
            ip = self.headers.get("X-Real-IP") or self.client_address[0]
            db.execute(
                "INSERT INTO audit_log (ts, person_id, action, target, ip) "
                "VALUES (?, ?, 'admin_activate_account', ?, ?)",
                (now_iso(), admin_pid, f"{acct['system']}:{acct['account_name']}", ip),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        return self._json(200, {"ok": True, "status": "active"})

    # ── admin: reset credential ────────────────────────────────────

    def _admin_reset_credential(self, cookie):
        parts = self.path.rstrip("/").split("/")
        try:
            acct_id = int(parts[-2])
        except (ValueError, IndexError):
            return self._json(400, {"error": "无效的账号 ID"})

        db = get_db()
        try:
            acct = db.execute(
                "SELECT id, system, account_name FROM accounts WHERE id = ?",
                (acct_id,),
            ).fetchone()
            if not acct:
                return self._json(404, {"error": "账号不存在"})

            if acct["system"] == "llm":
                new_cred = generate_api_key()
            else:
                new_cred = generate_password()

            db.execute(
                "UPDATE accounts SET credential = ? WHERE id = ?",
                (new_cred, acct_id),
            )

            admin_pid = self._session_person_id(db, cookie)
            ip = self.headers.get("X-Real-IP") or self.client_address[0]
            db.execute(
                "INSERT INTO audit_log (ts, person_id, action, target, ip) "
                "VALUES (?, ?, 'admin_reset_credential', ?, ?)",
                (now_iso(), admin_pid, f"{acct['system']}:{acct['account_name']}", ip),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        return self._json(200, {"ok": True, "credential": new_cred})

    # ── session helpers ────────────────────────────────────────────

    def _verify_session(self, db, cookie):
        """Return person_id if session valid, else None."""
        row = db.execute(
            "SELECT person_id, expires_at FROM auth_session WHERE token = ?",
            (cookie,),
        ).fetchone()
        if not row:
            return None
        expires = datetime.fromisoformat(row["expires_at"])
        if expires < datetime.now(timezone.utc):
            return None
        return row["person_id"]

    def _session_person_id(self, db, cookie):
        """Get person_id from session cookie (for audit logging)."""
        row = db.execute(
            "SELECT person_id FROM auth_session WHERE token = ?",
            (cookie,),
        ).fetchone()
        return row["person_id"] if row else None

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
