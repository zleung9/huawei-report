#!/usr/bin/env python3
"""Parse sacct pipe-delimited output and produce per-user per-day core-hours JSON.

Input file is the raw stdout of:
    sacct -aX --starttime=now-Ndays \
          --format=User,Account,JobID,State,CPUTimeRAW,Start,End,AllocCPUS,Elapsed -P

Output JSON shape:
{
  "generated_at": "2026-05-06T...",
  "range": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD", "days": N},
  "users": ["alice", "bob", ...],
  "dates": ["YYYY-MM-DD", ...],
  "core_hours": {"alice": [0.0, 1.2, ...], ...},
  "totals_per_day": [...],
  "totals_per_user": {"alice": 12.3, ...}
}
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

if len(sys.argv) < 4:
    print("usage: collect_slurm.py <sacct.tsv> <days> <out.json>", file=sys.stderr)
    sys.exit(1)

raw_path, days, out_path = Path(sys.argv[1]), int(sys.argv[2]), Path(sys.argv[3])

now = datetime.now()
end_day = now.date()
start_day = end_day - timedelta(days=days - 1)

dates = [start_day + timedelta(days=i) for i in range(days)]
date_strs = [d.isoformat() for d in dates]


def parse_dt(s: str):
    if not s or s in ("Unknown", "None"):
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


user_day_seconds: dict[str, list[float]] = {}

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
        user = row.get("User", "").strip()
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
        # Clip to window.
        win_start = datetime.combine(start_day, datetime.min.time())
        win_end = datetime.combine(end_day, datetime.max.time())
        s = max(start, win_start)
        e = min(end, win_end)
        if e <= s:
            continue
        # Walk per day.
        cur = s
        bucket = user_day_seconds.setdefault(user, [0.0] * days)
        while cur < e:
            day_idx = (cur.date() - start_day).days
            if 0 <= day_idx < days:
                day_end = datetime.combine(cur.date(), datetime.max.time())
                seg_end = min(e, day_end + timedelta(microseconds=1))
                seconds = (seg_end - cur).total_seconds()
                if seconds > 0:
                    bucket[day_idx] += seconds * alloc
            # Advance to next midnight.
            next_midnight = datetime.combine(
                cur.date() + timedelta(days=1), datetime.min.time()
            )
            cur = next_midnight

users = sorted(user_day_seconds.keys())
core_hours = {
    u: [round(s / 3600.0, 3) for s in user_day_seconds[u]] for u in users
}
totals_per_day = [
    round(sum(core_hours[u][i] for u in users), 3) for i in range(days)
]
totals_per_user = {u: round(sum(core_hours[u]), 3) for u in users}

out = {
    "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "range": {
        "start": date_strs[0],
        "end": date_strs[-1],
        "days": days,
    },
    "users": users,
    "dates": date_strs,
    "core_hours": core_hours,
    "totals_per_day": totals_per_day,
    "totals_per_user": totals_per_user,
}
out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2))
print(f"wrote {out_path} ({len(users)} users, {days} days, total {sum(totals_per_day):.1f} core-hours)")
