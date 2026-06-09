#!/usr/bin/env python3
"""Auth server for Huawei Reports.

Listens on 127.0.0.1:28790. Nginx proxies /api/auth/ requests here.
Uses PBKDF2-SHA256 for password hashing (stdlib only, no extra deps).
"""
import hashlib
import json
import os
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
MAX_BODY = 4096
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
        return self._json(404, {"error": "not found"})

    def do_GET(self):
        path = self.path.rstrip("/")
        if path == "/api/auth/check":
            return self._check()
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
                    lock = (datetime.now(timezone.utc).isoformat(timespec="seconds")
                            .replace("T", " "))
                    # Extend lock time
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
