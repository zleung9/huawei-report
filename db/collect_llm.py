#!/usr/bin/env python3
"""Fetch daily LLM API token usage per key per model from Huawei AI Platform
and upsert into the local SQLite database.

Usage:
    python3 collect_llm.py                          # fetch yesterday only (default)
    python3 collect_llm.py --days 7                  # fetch last N days
    python3 collect_llm.py --date 2026-06-21         # fetch a specific date
    python3 collect_llm.py --start 2026-06-21 --end 2026-06-23  # fetch a range

Requires: curl on PATH, network access to 10.26.15.52
Env:      LLM_API_AUTH — Base64-encoded admin credentials (required)
"""

import json, subprocess, os, sys, base64, argparse
from datetime import datetime, timezone, timedelta, date
import sqlite3

# ── Config ──────────────────────────────────────────────────────────
BASE_URL = "https://10.26.15.52"
CST      = timezone(timedelta(hours=8))
ENV      = {**os.environ, "NO_PROXY": "10.26.15.52,10.0.0.0/8"}

# ── Helpers ─────────────────────────────────────────────────────────
def ts(dt):
    """datetime → unix timestamp (seconds)"""
    return int(dt.timestamp())

def day_range(d: date):
    """Return (startTs, endTs) for a full day in CST."""
    start = datetime(d.year, d.month, d.day, tzinfo=CST)
    end   = start + timedelta(days=1)
    return ts(start), ts(end)

def curl_json(url, body=None, timeout=20):
    auth = os.environ.get("LLM_API_AUTH", "")
    cmd = ["curl", "-sk", "--location",
           "-H", f"Authorization: Basic {auth}"]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json",
                "-d", json.dumps(body)]
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True, text=True, env=ENV, timeout=timeout)
    return json.loads(r.stdout)

def get_all_keys():
    """Fetch list of all API keys from the platform."""
    data = curl_json(
        f"{BASE_URL}/ai/api/v1/serving/apikeys"
        f"?clusterId=fleet-local/local&all=true&pageSize=500"
    )
    return data.get("data", {}).get("result", [])

def get_monitor(apikey_id: str, start_ts: int, end_ts: int):
    """Fetch monitor data for one key in a time range."""
    body = {
        "apikeyIds": [str(apikey_id)],
        "startTs": start_ts,
        "endTs": end_ts,
        "inferenceNames": [],
        "pageSize": 500,
        "pageNum": 1,
        "clusterId": "fleet-local/local",
    }
    data = curl_json(f"{BASE_URL}/ai/api/v1/serving/apikeys/apiKeyMonitor/form", body=body)
    return data.get("data", {}).get("result", [])

# ── DB ──────────────────────────────────────────────────────────────
def upsert_rows(db, rows):
    """Upsert list of (date, api_key_id, model, input_tokens, output_tokens,
                      input_rate, output_rate, request_count, success_count,
                      failed_count, source, updated_at)"""
    db.executemany("""
        INSERT INTO llm_daily (date, api_key_id, model,
                               input_tokens, output_tokens,
                               input_rate, output_rate,
                               request_count, success_count, failed_count,
                               source, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date, api_key_id, model) DO UPDATE SET
            input_tokens  = excluded.input_tokens,
            output_tokens = excluded.output_tokens,
            input_rate    = excluded.input_rate,
            output_rate   = excluded.output_rate,
            request_count = excluded.request_count,
            success_count = excluded.success_count,
            failed_count  = excluded.failed_count,
            source        = excluded.source,
            updated_at    = excluded.updated_at
    """, rows)

def sync_key_mappings(db, keys):
    """Auto-map platform keys to existing accounts by matching token → accounts.api_key
    or token → accounts.credential (for llm accounts where api_key was backfilled from credential)."""
    mapped = 0
    for k in keys:
        kid = k.get("id")
        token = k.get("token", "")
        if not token:
            continue
        # Try matching api_key first, then credential
        row = db.execute(
            "SELECT id FROM accounts "
            "WHERE system = 'llm' AND (api_key = ? OR credential = ?) AND platform_key_id IS NULL "
            "LIMIT 1",
            (token, token),
        ).fetchone()
        if row:
            db.execute(
                "UPDATE accounts SET platform_key_id = ?, api_key = ? WHERE id = ?",
                (kid, token, row[0]),
            )
            mapped += 1
    return mapped

# ── Main ────────────────────────────────────────────────────────────
def fetch_day(db, d: date):
    """Fetch and store usage for a single day."""
    start_ts, end_ts = day_range(d)
    day_str = d.isoformat()
    now_str = datetime.now(CST).isoformat()

    keys = get_all_keys()
    print(f"[{day_str}] Found {len(keys)} keys, fetching per-key monitor …")

    all_rows = []
    for k in keys:
        kid = str(k["id"])
        rows = get_monitor(kid, start_ts, end_ts)
        for r in rows:
            model = r.get("inferenceName", "")
            inp = r.get("inferenceInputTokenNum", 0)
            out = r.get("inferenceOutputTokenNum", 0)
            req = r.get("inferenceTotalRequests", 0)
            # Skip noise rows: 0 tokens and ≤1 request
            if inp == 0 and out == 0 and req <= 1:
                continue
            all_rows.append((
                day_str, int(kid), model,
                inp, out,
                None, None,  # input_rate, output_rate — not available from platform
                req,
                r.get("inferenceSuccessRequests", 0),
                r.get("inferenceFailedRequests", 0),
                "gateway", now_str,
            ))
        print(f"  key {kid}: {len(rows)} model rows", flush=True)

    if all_rows:
        upsert_rows(db, all_rows)
    print(f"[{day_str}] Stored {len(all_rows)} rows ✓")

    # Auto-map platform keys to accounts
    mapped = sync_key_mappings(db, keys)
    if mapped:
        db.commit()
        print(f"[{day_str}] Auto-mapped {mapped} platform keys to accounts")

def main():
    parser = argparse.ArgumentParser(description="Collect LLM API usage from Huawei AI Platform")
    parser.add_argument("--days", type=int, default=1, help="Number of past days to fetch (default: 1 = yesterday)")
    parser.add_argument("--date", type=str, help="Specific date YYYY-MM-DD")
    parser.add_argument("--start", type=str, help="Start date YYYY-MM-DD (inclusive)")
    parser.add_argument("--end", type=str, help="End date YYYY-MM-DD (inclusive)")
    parser.add_argument("--db", type=str, default="/opt/db-data/usage.sqlite", help="Path to SQLite database")
    args = parser.parse_args()

    if not os.environ.get("LLM_API_AUTH"):
        print("ERROR: LLM_API_AUTH env var not set (Base64 of admin credentials)", file=sys.stderr)
        sys.exit(1)

    # Resolve dates
    if args.date:
        dates = [datetime.strptime(args.date, "%Y-%m-%d").date()]
    elif args.start and args.end:
        start = datetime.strptime(args.start, "%Y-%m-%d").date()
        end   = datetime.strptime(args.end, "%Y-%m-%d").date()
        dates = []
        cur = start
        while cur <= end:
            dates.append(cur)
            cur += timedelta(days=1)
    else:
        # Default: last N days (starting from yesterday)
        dates = [date.today() - timedelta(days=i) for i in range(args.days, 0, -1)]

    db = sqlite3.connect(args.db)
    try:
        for d in dates:
            try:
                fetch_day(db, d)
                db.commit()
            except Exception as e:
                print(f"[{d}] ERROR: {e}")
                db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    main()
