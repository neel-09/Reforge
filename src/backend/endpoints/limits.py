"""
endpoints/limits.py — daily usage limits so the Token Factory credits last through judging.

Two pools, reset every day at 00:00 UTC (05:30 IST):
    public   REFORGE_PUBLIC_RUNS_PER_DAY runs per visitor (IP) per day, and REFORGE_PUBLIC_DAILY_USD spend per day
    judge    callers with a valid judge key: no per-IP run cap, REFORGE_JUDGE_DAILY_USD spend per day

Spend is measured from data/telemetry.jsonl (every live Token Factory call appends one line with
actual_cost_usd), so a run that fails midway is still charged for what it really spent.

Env:
    REFORGE_JUDGE_KEYS            comma-separated judge keys (empty -> judge mode disabled)
    REFORGE_PUBLIC_RUNS_PER_DAY   default 3
    REFORGE_PUBLIC_DAILY_USD      default 0.75
    REFORGE_JUDGE_DAILY_USD       default 2.00
    REFORGE_DEMO_VIDEO_URL        shown in the limit pop-up
    REFORGE_USAGE_FILE            where counters live (default src/backend/data/usage.json; point at a
                                  persistent volume in production so restarts don't reset the counters)
    REFORGE_TRUST_PROXY           "1" (default): client IP = right-most X-Forwarded-For entry (set by the host's proxy)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request

from src.backend.endpoints.endpoint import DATA_ROOT

TELEMETRY_FILE = DATA_ROOT / "telemetry.jsonl"
USAGE_FILE = Path(os.environ.get("REFORGE_USAGE_FILE") or DATA_ROOT / "usage.json")
JUDGE_KEYS = [k.strip() for k in os.environ.get("REFORGE_JUDGE_KEYS", "").split(",") if k.strip()]
PUBLIC_RUNS = int(os.environ.get("REFORGE_PUBLIC_RUNS_PER_DAY", "3"))
PUBLIC_USD = float(os.environ.get("REFORGE_PUBLIC_DAILY_USD", "0.75"))
JUDGE_USD = float(os.environ.get("REFORGE_JUDGE_DAILY_USD", "2.00"))
VIDEO_URL = os.environ.get("REFORGE_DEMO_VIDEO_URL", "")
TRUST_PROXY = os.environ.get("REFORGE_TRUST_PROXY", "1") == "1"
MAX_KEY_FAILS = 10                      # wrong judge-key guesses per IP per day
_SALT = os.environ.get("REFORGE_IP_SALT", "reforge")   # IPs are stored hashed, never raw

_lock = threading.Lock()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load() -> dict:
    day = _today()
    try:
        d = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    if d.get("day") != day:              # new day -> fresh counters (old days are dropped)
        d = {"day": day, "runs": {}, "fails": {}, "usd": {"public": 0.0, "judge": 0.0}}
    return d


def _save(d: dict) -> None:
    try:
        USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = USAGE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d), encoding="utf-8")
        tmp.replace(USAGE_FILE)
    except OSError as e:
        print(f"[limits] could not save usage: {e}")


def client_id(request: Request) -> str:
    ip = request.client.host if request.client else "unknown"
    if TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for", "")
        if xff.strip():
            ip = xff.split(",")[-1].strip()
    return hashlib.sha256(f"{_SALT}:{ip}".encode()).hexdigest()[:24]


def _key_from(request: Request, form_key: Optional[str] = None) -> str:
    return (request.headers.get("x-reforge-judge-key") or form_key or "").strip()


def is_judge(key: str) -> bool:
    return bool(key) and any(hmac.compare_digest(key.encode(), k.encode()) for k in JUDGE_KEYS)


def _limit(code: str, title: str, message: str) -> HTTPException:
    return HTTPException(429, {"code": code, "title": title, "message": message, "video_url": VIDEO_URL})


def _pool_msg(who: str) -> str:
    return (f"Today's {who} AI budget for live runs has been used up, so new runs are paused until "
            f"00:00 UTC (05:30 IST). Meanwhile, watch the demo video to see a full Reforge run.")


# --------------------------------------------------------------------------- #
def status(request: Request, form_key: Optional[str] = None) -> dict:
    judge = is_judge(_key_from(request, form_key))
    with _lock:
        d = _load()
    pool = "judge" if judge else "public"
    cap = JUDGE_USD if judge else PUBLIC_USD
    used = d["runs"].get(client_id(request), 0)
    return {"judge": judge, "judge_enabled": bool(JUDGE_KEYS),
            "runs_left": None if judge else max(0, PUBLIC_RUNS - used), "runs_per_day": PUBLIC_RUNS,
            "budget_left": d["usd"][pool] < cap,       # dollar figures are kept server-side
            "resets": "00:00 UTC", "video_url": VIDEO_URL}


def verify_key(request: Request, key: str) -> dict:
    cid = client_id(request)
    with _lock:
        d = _load()
        if d["fails"].get(cid, 0) >= MAX_KEY_FAILS:
            raise HTTPException(429, "Too many wrong judge keys today.")
        if not is_judge(key.strip()):
            d["fails"][cid] = d["fails"].get(cid, 0) + 1
            _save(d)
            raise HTTPException(403, "That judge key isn't valid.")
    return {"ok": True}


class Ticket:
    """One admitted run. Call .finish(halted=...) exactly once when the run ends."""

    def __init__(self, pool: str, cid: str, start_line: int, budget_left: float):
        self.pool, self.cid, self.start_line, self.budget_left = pool, cid, start_line, budget_left
        self._done = False

    def stage4_budget(self, default: float = 0.50) -> float:
        """Never let Stage 4 spend past what's left in today's pool."""
        return max(0.01, min(default, self.budget_left))

    def finish(self, halted: bool = False) -> float:
        if self._done:
            return 0.0
        self._done = True
        spent = _spent_since(self.start_line)
        with _lock:
            d = _load()
            d["usd"][self.pool] = round(d["usd"][self.pool] + spent, 6)
            if halted and spent == 0 and self.pool == "public":   # pre-flight stop: nothing ran, give the run back
                d["runs"][self.cid] = max(0, d["runs"].get(self.cid, 1) - 1)
            _save(d)
        print(f"[limits] {self.pool} run spent ${spent:.4f}; today {self.pool}: ${d['usd'][self.pool]:.4f}")
        return spent


def _line_count() -> int:
    try:
        with TELEMETRY_FILE.open("rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def _spent_since(start_line: int) -> float:
    """Runs are serialised (_RUN_LOCK), so every telemetry line after start_line belongs to this run."""
    try:
        lines = TELEMETRY_FILE.read_text(encoding="utf-8").splitlines()[start_line:]
    except OSError:
        return 0.0
    total = 0.0
    for l in lines:
        try:
            e = json.loads(l)
        except ValueError:
            continue
        if not e.get("cached"):
            total += float(e.get("actual_cost_usd") or 0.0)
    return total


def admit(request: Request, form_key: Optional[str] = None) -> Ticket:
    """Check the caller's limits and reserve one run. Raises 429 with a pop-up payload when over."""
    key = _key_from(request, form_key)
    judge = is_judge(key)
    if key and not judge:
        raise HTTPException(403, "That judge key isn't valid. Remove it or enter the correct one.")
    cid = client_id(request)
    with _lock:
        d = _load()
        if judge:
            if d["usd"]["judge"] >= JUDGE_USD:
                raise _limit("judge_budget", "Judge budget reached for today", _pool_msg("judge"))
            left = JUDGE_USD - d["usd"]["judge"]
            pool = "judge"
        else:
            if d["runs"].get(cid, 0) >= PUBLIC_RUNS:
                raise _limit("public_limit", "Daily run limit reached",
                             f"You've used all {PUBLIC_RUNS} free live runs for today. They reset at "
                             f"00:00 UTC (05:30 IST). Meanwhile, watch the demo video to see a full Reforge run.")
            if d["usd"]["public"] >= PUBLIC_USD:
                raise _limit("public_budget", "Daily budget reached", _pool_msg("public"))
            d["runs"][cid] = d["runs"].get(cid, 0) + 1
            left = PUBLIC_USD - d["usd"]["public"]
            pool = "public"
        _save(d)
    return Ticket(pool, cid, _line_count(), left)
