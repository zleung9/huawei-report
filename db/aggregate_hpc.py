#!/usr/bin/env python3
"""Aggregate sacct output into hpc_daily.

Same per-job per-day overlap math as web/scripts/collect_slurm.py, but
UPSERTs into the SQLite hpc_daily table instead of writing JSON.

Usage:
    python3 aggregate_hpc.py <sacct.tsv> <days> <db_path>

Behaviour: rows for any (date, account_name) present in the input range
are REPLACEd. Days with no jobs are not touched (no zero-row insertion).
"""
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def parse_dt(s: str):
    if not s or s in ("Unknown", "None"):
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def aggregate(raw_path: Path, days: int):
    """Return {(date_str, account): {core_hours, job_count}}."""
    now = datetime.now()
    end_day = now.date()
    start_day = end_day - timedelta(days=days - 1)
    win_start = datetime.combine(start_day, datetime.min.time())
    win_end = datetime.combine(end_day, datetime.max.time())

    buckets: dict[tuple[str, str], dict[str, float]] = {}

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
            if not user:
                continue
            try:
                alloc = int(row.get("AllocCPUS") or 0)
            except ValueError:
                alloc = 0
            if alloc <= 0:
                continue
            start = parse_dt(row.get("Start", ""))
            end = parse_dt(row.get("End", ""))
            if start is None:
                continue
            if end is None:
                end = now
            s = max(start, win_start)
            e = min(end, win_end)
            if e <= s:
                continue

            # Track job_count per job's *start* day (within window).
            start_key = (s.date().isoformat(), user)
            buckets.setdefault(start_key, {"core_seconds": 0.0, "job_count": 0})
            buckets[start_key]["job_count"] += 1

            cur = s
            while cur < e:
                day_key = (cur.date().isoformat(), user)
                buckets.setdefault(day_key, {"core_seconds": 0.0, "job_count": 0})
                day_end = datetime.combine(cur.date(), datetime.max.time())
                seg_end = min(e, day_end + timedelta(microseconds=1))
                seconds = (seg_end - cur).total_seconds()
                if seconds > 0:
                    buckets[day_key]["core_seconds"] += seconds * alloc
                cur = datetime.combine(
                    cur.date() + timedelta(days=1), datetime.min.time()
                )

    return buckets


def upsert(db_path: Path, buckets: dict):
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA foreign_keys = ON")
        db.executemany(
            """INSERT INTO hpc_daily
                   (date, account_name, core_hours, job_count, source, updated_at)
               VALUES (?, ?, ?, ?, 'sacct', ?)
               ON CONFLICT(date, account_name) DO UPDATE SET
                   core_hours = excluded.core_hours,
                   job_count  = excluded.job_count,
                   source     = excluded.source,
                   updated_at = excluded.updated_at""",
            [
                (date, account, round(v["core_seconds"] / 3600.0, 3),
                 v["job_count"], ts)
                for (date, account), v in buckets.items()
            ],
        )
        db.commit()


def main() -> None:
    if len(sys.argv) < 4:
        print("usage: aggregate_hpc.py <sacct.tsv> <days> <db_path>",
              file=sys.stderr)
        sys.exit(1)
    raw_path = Path(sys.argv[1])
    days = int(sys.argv[2])
    db_path = Path(sys.argv[3])

    buckets = aggregate(raw_path, days)
    upsert(db_path, buckets)
    total_h = sum(v["core_seconds"] for v in buckets.values()) / 3600.0
    total_j = sum(v["job_count"] for v in buckets.values())
    print(f"[aggregate_hpc] upserted {len(buckets)} (date, account) rows; "
          f"total {total_h:.1f} core-hours, {total_j} jobs (window {days}d)")


if __name__ == "__main__":
    main()
