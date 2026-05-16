#!/usr/bin/env python3
"""API key request submissions endpoint.

Listens on 127.0.0.1:18789. Nginx proxies /api/ requests here.
POSTed records are validated and appended to a JSONL file.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

OUT = Path(os.environ.get(
    "APIKEY_OUT_FILE", "/usr/share/nginx/html/data/apikey-requests.jsonl"))
PORT = int(os.environ.get("APIKEY_PORT", "28789"))
MAX_BODY = 16 * 1024

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
VALID_PURPOSES = {
    "long-doc", "code", "rag", "writing",
    "reasoning", "tutoring", "throughput", "other",
}
VALID_MODELS = {"glm5.1", "deepseek-v4", "qwen3.6"}
VALID_VOLUME = {"<10k", "10k-100k", "100k-1M", "1M-10M", ">10M"}
VALID_AFFIL = {"internal", "external"}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path.rstrip("/") != "/api/apikey-request":
            return self._json(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > MAX_BODY:
            return self._json(413, {"error": "body too large or empty"})
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._json(400, {"error": "invalid JSON"})

        name = (payload.get("name") or "").strip()[:100]
        email = (payload.get("email") or "").strip().lower()[:200]
        affil = payload.get("affiliation_type")
        group = (payload.get("group") or "").strip()[:200]
        purposes = payload.get("purposes") or []
        volume = payload.get("volume")
        models = payload.get("models") or []
        notes = (payload.get("notes") or "").strip()[:2000]

        errors = []
        if not name:
            errors.append("姓名不能为空")
        if not EMAIL_RE.match(email):
            errors.append("邮箱格式无效")
        if affil not in VALID_AFFIL:
            errors.append("请选择校内或校外")
        if not group:
            errors.append("请填写研究组/单位")
        if not isinstance(purposes, list) or not purposes:
            errors.append("请至少选择一项用途")
        elif not all(p in VALID_PURPOSES for p in purposes):
            errors.append("用途选项无效")
        if volume not in VALID_VOLUME:
            errors.append("请选择月调用量区间")
        if not isinstance(models, list) or not models:
            errors.append("请至少选择一个模型")
        elif not all(m in VALID_MODELS for m in models):
            errors.append("模型选项无效")

        if errors:
            return self._json(400, {"error": "; ".join(errors)})

        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ip": (self.headers.get("X-Real-IP")
                   or self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                   or ""),
            "name": name,
            "email": email,
            "affiliation_type": affil,
            "group": group,
            "purposes": purposes,
            "volume": volume,
            "models": models,
            "notes": notes,
        }
        OUT.parent.mkdir(parents=True, exist_ok=True)
        with OUT.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return self._json(200, {"ok": True})

    def do_GET(self):
        if self.path.rstrip("/") == "/api/health":
            return self._json(200, {"ok": True, "service": "apikey"})
        return self._json(404, {"error": "not found"})

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write(
            f"[apikey] {self.address_string()} {self.log_date_time_string()} "
            f"{fmt % args}\n")
        sys.stderr.flush()


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    sys.stderr.write(
        f"[apikey] listening on 127.0.0.1:{PORT}, appending to {OUT}\n")
    sys.stderr.flush()
    server.serve_forever()
