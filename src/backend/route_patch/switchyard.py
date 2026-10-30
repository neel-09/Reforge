"""
route_patch/switchyard.py — Reforge Stage 4 (smart router): cheap first, escalate on failure.

Input: the Stage-3 refactor_plan. Output: a patched workspace verified under Pydantic v2.

Phase A — patch (per FILE, files in parallel; tuned for a ~1 min end-to-end run)
    0. steps whose change is already in the file are marked done ($0, no call)
    1. ONE Nemotron 3.5 Lightning call per file carries all of that file's pending steps; each returned
       SEARCH/REPLACE block is applied on its own (bad blocks are dropped, good ones kept) and linted
       (no duplicate imports, no "Step N" comments) — reply capped at LIGHTNING_MAX_TOKENS
    2. steps still open -> the architect's own before/after ($0)
    3. steps still open -> per-step escalation: Nemotron 3.5 Super VL, then Nemotron 3 Ultra
    requirements.txt (manifest) steps are applied straight from the plan ($0).

Phase B — verify + heal
    The repo's tests run in the Stage-2 sandbox venv (Pydantic v2, deprecations as errors, network
    blocked). Pass -> done. Fail -> the failing tracebacks + current sources go to Super; still failing
    -> Ultra (last resort). A heal reply that makes things worse is rolled back.

Cost: every dispatch is priced with src/model_catalog.md, appended to data/telemetry.jsonl, and
compared with the counterfactual of the same tokens on Ultra. A per-run budget (REFORGE_STAGE4_BUDGET_USD,
default $0.50) stops further dispatches. Identical prompts are served from data/cache/patches ($0;
disable with REFORGE_PATCH_CACHE=0). No Nebius key / use_llm=False -> the plan's own before/after are
applied deterministically (tier "plan"), so the pipeline never stalls.

Endpoint entry point:
    run_stage4(ws_path, stage3, *, stage2=None, baseline=None, use_llm=True) -> dict
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.backend.extract.harness import _discover_test_files, _parse_report, compare_runs
from src.backend.planner.grounder import ROOT, env_key
from src.backend.route_patch.patcher import (CLEAN, NO_BLOCKS, make_diff, PatchResult, apply_blocks, apply_reply, Block,
                                 parse_blocks, rollback)
from src.backend.sandbox.sandbox import (DEFAULT_SPEC, VENV_DIRNAME, SandboxSetupError, _report_cases, build_env,
                             run_import_sweep, run_pytest)
from src.backend.sandbox.slicer import is_test_path

BASE_URL = env_key("NEBIUS_BASE_URL") or "https://api.tokenfactory.us-central1.nebius.com/v1/"
TELEMETRY_FILE = ROOT / "data" / "telemetry.jsonl"
PATCH_CACHE_DIR = ROOT / "data" / "cache" / "patches"


@dataclass(frozen=True)
class Tier:
    key: str
    model: str
    rate_in: float           # $ / 1M tokens
    rate_out: float
    max_tokens: int
    temperature: float
    thinking: bool = False   # Nemotron reasoning mode; off = direct answer (fewer tokens, no length cut-offs)
    base_url: str = ""       # tier-specific Token Factory endpoint; "" = BASE_URL


# src/model_catalog.md — model ids overridable via env (Token Factory naming)
TIERS: dict[str, Tier] = {
    "lightning": Tier("lightning", env_key("REFORGE_LIGHTNING_MODEL") or "nvidia/Nemotron-3_5-Lightning",
                      0.06, 0.24, 8192, 0.2),
    # Nemotron 3.5 Super VL (120B MoE) replaces nemotron-3-super-120b-a12b (retired on Token Factory 2026-11-09).
    # Its public endpoint is us-north1; override with REFORGE_SUPER_BASE_URL.
    "super": Tier("super", env_key("REFORGE_SUPER_MODEL") or "nvidia/Nemotron-3.5-Super-VL",
                  0.30, 0.90, 8192, 0.4,
                  base_url=env_key("REFORGE_SUPER_BASE_URL") or "https://api.tokenfactory.us-north1.nebius.com/v1/"),
    "ultra": Tier("ultra", env_key("REFORGE_ULTRA_MODEL") or "nvidia/Nemotron-3-Ultra-550b-a55b",
                  1.00, 3.00, 16384, 0.6, True),
}
PATCH_LADDER = ("lightning", "super", "ultra")
HEAL_LADDER = ("super", "ultra")
ULTRA_RATES = (TIERS["ultra"].rate_in, TIERS["ultra"].rate_out)
MAX_FULL_FILE_LINES = 300
LIGHTNING_MAX_TOKENS = int(os.environ.get("REFORGE_LIGHTNING_MAX_TOKENS", "2048"))   # one file batch
PATCH_TIMEOUT = {"lightning": 60.0, "super": 90.0, "ultra": 120.0}                    # fail fast, then escalate
ESCALATION_LADDER = ("super", "ultra")
WINDOW = 40


# Built at runtime so the source never contains git-conflict-looking lines.
BLOCK_FORMAT = "\n".join(["app/example.py", "<" * 7 + " SEARCH",
                          "    total = old_api(a, b)", "=" * 7,
                          "    total = new_api(a, b)", ">" * 7 + " REPLACE",
                          "(example only: use the real file path and lines copied from the CURRENT file)"])

PATCH_SYSTEM = """/no_think
You are the code patcher of Reforge, a Pydantic v1 -> v2 migration agent.
Apply ONE refactor step to ONE file. Reply ONLY with SEARCH/REPLACE blocks, no prose:

{block_format}

Rules:
1. SEARCH must match the current file character for character (indentation included) and occur
   exactly once; add a neighbouring line if needed to make it unique. Never include line numbers.
2. Keep SEARCH minimal: only the lines that change plus the context needed for uniqueness.
3. REPLACE must be complete, runnable code with correct indentation. No placeholders, no "...".
4. Only edit the file named in the task. Several blocks are allowed for one step.
5. Preserve behaviour: same output, error messages and exception types under Pydantic v2.""".replace(
    "{block_format}", BLOCK_FORMAT)

FILE_SYSTEM = """/no_think
You are the code patcher of Reforge, a Pydantic v1 -> v2 migration agent.
Apply ALL the listed refactor steps to ONE file. Reply ONLY with SEARCH/REPLACE blocks (one per changed
region), no prose, no explanations:

{block_format}

Rules:
1. SEARCH must match the CURRENT file character for character (indentation included) and occur exactly once.
2. Keep each SEARCH minimal: only the lines that change plus context needed for uniqueness.
3. Change ONLY what the steps describe. Do not add comments, do not re-add or duplicate imports,
   do not touch other lines.
4. REPLACE must be complete, runnable code with correct indentation. No placeholders, no "...".
5. Preserve behaviour: same output, error messages and exception types under Pydantic v2.""".replace(
    "{block_format}", BLOCK_FORMAT)

HEAL_SYSTEM = """/no_think
You are the self-healing engineer of Reforge, a Pydantic v1 -> v2 migration agent.
The repo has been patched but its test suite still fails under Pydantic v2. Fix the SOURCE files so the
tests pass unchanged. Test files are READ-ONLY. Reply ONLY with SEARCH/REPLACE blocks, no prose:

{block_format}

Rules: SEARCH must match the current file exactly and occur once; never include line numbers; edit only
files listed under editable_files; complete runnable code only; keep error messages, exception types and
serialized output identical to what the tests assert; use documented Pydantic v2 APIs (no deprecated ones).""".replace(
    "{block_format}", BLOCK_FORMAT)


# --------------------------------------------------------------------------- #
# Cost ledger
# --------------------------------------------------------------------------- #
class Ledger:
    def __init__(self, budget_usd: float):
        self.budget = budget_usd
        self.entries: list[dict] = []
        self.dead: dict[str, str] = {}     # tier -> reason (404 / auth): skipped for the rest of the run
        self._lock = threading.Lock()      # files are patched in parallel

    @property
    def spent(self) -> float:
        return sum(e["actual_cost_usd"] for e in self.entries if not e.get("cached"))

    def can_spend(self) -> bool:
        return self.spent < self.budget

    def record(self, stage: str, tier: Tier, call: dict, success: bool, step: int | None = None,
               steps: list | None = None) -> dict:
        tin, tout = call.get("prompt_tokens", 0), call.get("completion_tokens", 0)
        cached = bool(call.get("cached"))
        cost = tin / 1e6 * tier.rate_in + tout / 1e6 * tier.rate_out
        ultra = tin / 1e6 * ULTRA_RATES[0] + tout / 1e6 * ULTRA_RATES[1]
        entry = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "stage": stage, "tier": tier.key, "model": tier.model,
            "prompt_tokens": tin, "completion_tokens": tout, "latency_ms": call.get("latency_ms", 0.0),
            "actual_cost_usd": round(0.0 if cached else cost, 7),
            "counterfactual_ultra_cost_usd": round(0.0 if cached else ultra, 7),
            "savings_usd": round(0.0 if cached else ultra - cost, 7),
            "first_pass_success": success,
        }
        if step is not None:
            entry["step"] = step
        if steps:
            entry["steps"] = steps
        if cached:
            entry.update(cached=True, original_cost_usd=round(cost, 7))
        with self._lock:
            self.entries.append(entry)
            if not cached:
                try:
                    TELEMETRY_FILE.parent.mkdir(parents=True, exist_ok=True)
                    with TELEMETRY_FILE.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(entry) + "\n")
                except OSError:
                    pass
        return entry

    def mark_last(self, success: bool) -> None:
        """Success is only known after patch/test — fix up the entry and the jsonl line."""
        if not self.entries:
            return
        e = self.entries[-1]
        if e["first_pass_success"] == success:
            return
        e["first_pass_success"] = success
        if e.get("cached"):
            return
        try:
            lines = TELEMETRY_FILE.read_text(encoding="utf-8").splitlines()
            if lines:
                last = json.loads(lines[-1])
                if last.get("timestamp") == e["timestamp"] and last.get("model") == e["model"]:
                    lines[-1] = json.dumps(e)
                    TELEMETRY_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            pass

    def summary(self) -> dict:
        live = [e for e in self.entries if not e.get("cached")]
        actual = sum(e["actual_cost_usd"] for e in live)
        ultra = sum(e["counterfactual_ultra_cost_usd"] for e in live)
        by_tier: dict[str, dict] = {}
        for e in self.entries:
            t = by_tier.setdefault(e["tier"], {"dispatches": 0, "cached": 0, "prompt_tokens": 0,
                                               "completion_tokens": 0, "cost_usd": 0.0})
            t["dispatches"] += 1
            t["cached"] += int(bool(e.get("cached")))
            t["prompt_tokens"] += e["prompt_tokens"]
            t["completion_tokens"] += e["completion_tokens"]
            t["cost_usd"] = round(t["cost_usd"] + e["actual_cost_usd"], 7)
        return {
            "actual_usd": round(actual, 6),
            "counterfactual_ultra_usd": round(ultra, 6),
            "savings_usd": round(ultra - actual, 6),
            "savings_pct": round((ultra - actual) / ultra * 100, 1) if ultra else 0.0,
            "dispatches": len(self.entries),
            "budget_usd": self.budget,
            "budget_exhausted": not self.can_spend(),
            "by_tier": by_tier,
        }


# --------------------------------------------------------------------------- #
# Nebius dispatch (+ reply cache)
# --------------------------------------------------------------------------- #
_MODEL_HINTS = {"lightning": ("lightning",),  # exact ids above win; hints only if renamed
                "super": ("nemotron-3.5-super", "nemotron-3-5-super", "super-vl"),
                "ultra": ("nemotron-3-ultra", "nemotron3-ultra")}
_ENV_MODEL = {"lightning": "REFORGE_LIGHTNING_MODEL", "super": "REFORGE_SUPER_MODEL", "ultra": "REFORGE_ULTRA_MODEL"}
_RESOLVED: dict[str, Any] = {}


def resolve_models(api_key: str) -> dict:
    """Map each tier to a model id the key can actually call (GET /v1/models, once per process).
    Env overrides win; unmatched tiers keep their default id."""
    if _RESOLVED.get("key") == api_key:
        return _RESOLVED["info"]
    info: dict[str, Any] = {"source": {}, "error": None}
    try:
        from openai import OpenAI
        ids = [m.id for m in OpenAI(base_url=BASE_URL, api_key=api_key, timeout=30, max_retries=1).models.list().data]
    except Exception as e:
        ids, info["error"] = [], f"{type(e).__name__}: {str(e)[:200]}"
    low = {i.lower(): i for i in ids}
    for k, hints in _MODEL_HINTS.items():
        if env_key(_ENV_MODEL[k]):
            info["source"][k] = "env"
            continue
        if TIERS[k].base_url and TIERS[k].base_url.rstrip("/") != BASE_URL.rstrip("/"):
            info["source"][k] = f"own endpoint ({TIERS[k].base_url})"     # not in this region's /v1/models
            continue
        cur = TIERS[k].model
        if cur.lower() in low:
            TIERS[k] = replace(TIERS[k], model=low[cur.lower()])
            info["source"][k] = "catalog"
            continue
        match = sorted((i for l, i in low.items() if any(h in l for h in hints) and "nemotron" in l),
                       key=lambda i: ("instruct" not in i.lower(), len(i)))
        if match:
            TIERS[k] = replace(TIERS[k], model=match[0])
            info["source"][k] = "catalog-match"
        else:
            info["source"][k] = "default (not in /v1/models)" if ids else "default"
    info["models"] = {k: t.model for k, t in TIERS.items()}
    _RESOLVED.update(key=api_key, info=info)
    return info


def _cache_enabled() -> bool:
    return os.environ.get("REFORGE_PATCH_CACHE", "1") != "0"


_NO_THINK_UNSUPPORTED: set[str] = set()


def _thinking(tier: Tier) -> bool:
    return tier.thinking or os.environ.get("REFORGE_THINKING", "0") == "1"


def _cache_key(tier: Tier, messages: list[dict]) -> str:
    blob = json.dumps({"m": tier.model, "t": tier.temperature, "x": tier.max_tokens, "k": _thinking(tier),
                       "msg": messages},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def dispatch(tier: Tier, messages: list[dict], *, api_key: str, timeout: float = 180.0) -> dict:
    key = _cache_key(tier, messages)
    cache_file = PATCH_CACHE_DIR / f"{key}.json"
    if _cache_enabled() and cache_file.is_file():
        try:
            return {**json.loads(cache_file.read_text(encoding="utf-8")), "cached": True}
        except (OSError, json.JSONDecodeError):
            pass
    from openai import OpenAI
    client = OpenAI(base_url=tier.base_url or BASE_URL, api_key=api_key, timeout=timeout, max_retries=1)
    wire = [m if m["role"] != "user" else {"role": "user", "content": [{"type": "text", "text": m["content"]}]}
            for m in messages]
    kwargs = dict(model=tier.model, messages=wire, temperature=tier.temperature, top_p=0.95,
                  max_tokens=tier.max_tokens)
    no_think = not _thinking(tier) and tier.model not in _NO_THINK_UNSUPPORTED
    t0 = time.perf_counter()
    try:
        res = client.chat.completions.create(
            **kwargs, **({"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}} if no_think else {}))
    except Exception as e:      # endpoint rejects the template flag -> retry plain, remember
        if not no_think or getattr(e, "status_code", None) not in (400, 422):
            raise
        _NO_THINK_UNSUPPORTED.add(tier.model)
        t0 = time.perf_counter()
        res = client.chat.completions.create(**kwargs)
    msg, usage = res.choices[0].message, res.usage
    call = {
        "content": msg.content or "",
        "reasoning": getattr(msg, "reasoning_content", None) or "",
        "finish_reason": res.choices[0].finish_reason,
        "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
        "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
        "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
        "model": tier.model,
    }
    if _cache_enabled() and parse_blocks(call["content"] or call["reasoning"]):
        try:
            PATCH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps({**call, "cached_at": datetime.now(timezone.utc).isoformat()}),
                                  encoding="utf-8")
        except OSError:
            pass
    return call


def _mark_dead(ledger: "Ledger", key: str, e: Exception) -> None:
    """Model missing / key rejected -> retrying it on every step only burns time."""
    code = getattr(e, "status_code", None)
    if code in (401, 403, 404):
        ledger.dead[key] = f"{TIERS[key].model} unavailable (HTTP {code})"


def _reply_text(call: dict) -> str:
    """Prefer content; fall back to reasoning when the model put its blocks there."""
    return call["content"] if parse_blocks(call.get("content", "")) else (call.get("reasoning") or call.get("content", ""))


# --------------------------------------------------------------------------- #
# Prompt building
# --------------------------------------------------------------------------- #
def _read(ws: Path, rel: str) -> str:
    try:
        return (ws / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _file_view(text: str, anchor: str | None, lines_hint: list[int]) -> tuple[str, bool]:
    """Whole file when short; otherwise a raw (un-numbered) window around the step's code."""
    lines = text.splitlines()
    if len(lines) <= MAX_FULL_FILE_LINES:
        return text, True
    centre = None
    if anchor and anchor.strip():
        idx = text.find(anchor.strip().splitlines()[0])
        if idx != -1:
            centre = text.count("\n", 0, idx)
    if centre is None and lines_hint:
        centre = max(0, min(lines_hint) - 1)
    centre = centre or 0
    lo, hi = max(0, centre - WINDOW), min(len(lines), centre + WINDOW)
    head = lines[:15] if lo > 15 else []
    body = (head + (["# ... (lines omitted) ..."] if head else []) + lines[lo:hi])
    return "\n".join(body), False


def patch_messages(ws: Path, step: dict, feedback: list[str]) -> list[dict]:
    rel = step["file"]
    text = _read(ws, rel)
    view, full = _file_view(text, step.get("before"), step.get("lines") or [])
    task = {
        "file": rel,
        "action": step.get("action"),
        "symbols": step.get("symbols"),
        "instructions": step.get("instructions"),
        "planned_before": step.get("before"),
        "planned_after": step.get("after"),
        "note": "planned_before/after come from the architect and may be stale if earlier steps already "
                "edited nearby lines; the CURRENT file below is the source of truth for SEARCH.",
    }
    user = (f"TASK\n{json.dumps(task, indent=1, ensure_ascii=False)}\n\n"
            f"CURRENT {'FILE' if full else 'EXCERPT OF'} {rel}\n```python\n{view}\n```")
    user += f"\n\nStart every block with the line `{rel}` (not a placeholder path)."
    if feedback:
        user += "\n\nPREVIOUS ATTEMPTS FAILED — fix this:\n" + "\n".join(f"- {f}" for f in feedback[-3:])
    return [{"role": "system", "content": PATCH_SYSTEM}, {"role": "user", "content": user}]


def heal_messages(ws: Path, editable: list[str], failures: list[dict], feedback: list[str]) -> list[dict]:
    files = {rel: _read(ws, rel) for rel in editable}
    tests = {p.relative_to(ws).as_posix(): p.read_text(encoding="utf-8", errors="replace")[:6000]
             for p in _discover_test_files(ws)[:3]}
    user = {"editable_files": list(files), "failing": failures[:8]}
    parts = [json.dumps(user, indent=1, ensure_ascii=False)]
    parts += [f"EDITABLE {rel}\n```python\n{src}\n```" for rel, src in files.items()]
    parts += [f"READ-ONLY TEST {rel}\n```python\n{src}\n```" for rel, src in tests.items()]
    if feedback:
        parts.append("PREVIOUS HEAL ATTEMPTS FAILED:\n" + "\n".join(f"- {f}" for f in feedback[-3:]))
    return [{"role": "system", "content": HEAL_SYSTEM}, {"role": "user", "content": "\n\n".join(parts)}]


# --------------------------------------------------------------------------- #
# Verification (Stage-2 sandbox venv, Pydantic v2)
# --------------------------------------------------------------------------- #
def verify(ws: Path, spec: str, *, baseline: dict | None, timeout: float = 300) -> dict:
    try:
        py, version, _ = build_env(ws, spec, None, timeout)
    except SandboxSetupError as e:
        return {"passed": False, "status": f"sandbox_error: {e}"[:500], "failures": [], "counts": {}}
    test_files = _discover_test_files(ws)
    if not test_files:     # test-less repo: importing every module under v2 is the signal
        crashes = run_import_sweep(py, ws, 120, True)
        fails = [{"test": c.test_id, "file": c.file, "line": c.line, "error": f"{c.exc_type}: {c.message}"[:600]}
                 for c in crashes]
        return {"passed": not crashes, "status": "import check " + ("passed" if not crashes else "failed"),
                "failures": fails, "counts": {}, "pydantic": version}
    report = ws / VENV_DIRNAME / "pytest_report_stage4.json"
    code, out = run_pytest(py, ws, test_files, report, timeout=timeout, strict_deprecations=True,
                           isolate_secrets=True)
    metrics = _parse_report(report)
    fails = [{"test": tid, "kind": kind, "traceback": text[-1500:]} for tid, kind, text in _report_cases(report)]
    if not metrics.get("report_available"):
        fails = fails or [{"test": "<pytest>", "kind": "pytest", "traceback": out[-1500:]}]
    cmp = compare_runs(baseline, metrics) if baseline and baseline.get("outcomes") else None
    passed = bool(metrics.get("collected")) and not fails and code == 0
    counts = {k: metrics.get(k, 0) for k in ("collected", "passed", "failed", "errors", "pass_rate")}
    return {"passed": passed, "status": f"{counts.get('passed', 0)}/{counts.get('collected', 0)} passed",
            "failures": fails, "counts": counts, "pydantic": version,
            "regressions": (cmp or {}).get("regressions", []), "preserved": (cmp or {}).get("preserved")}


# --------------------------------------------------------------------------- #
# Backup (so a bad run can be undone)
# --------------------------------------------------------------------------- #
def _backup_dir(ws: Path) -> Path:
    return ws.parent / f"{ws.name}_stage4_orig"


def snapshot(ws: Path, files: list[str]) -> Path:
    bk = _backup_dir(ws)
    if bk.exists():
        shutil.rmtree(bk, ignore_errors=True)
    for rel in files:
        src = ws / rel
        if src.is_file():
            dst = bk / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    return bk


def restore(ws: Path) -> list[str]:
    bk, done = _backup_dir(ws), []
    for p in bk.rglob("*") if bk.exists() else []:
        if p.is_file():
            rel = p.relative_to(bk).as_posix()
            shutil.copy2(p, ws / rel)
            done.append(rel)
    return done


# --------------------------------------------------------------------------- #
# Phase A: per-step patching with escalation
# --------------------------------------------------------------------------- #
def _plan_direct(ws: Path, step: dict) -> PatchResult:
    """Deterministic path: the architect's exact before/after as one SEARCH/REPLACE block."""
    before, after = step.get("before") or "", step.get("after") or ""
    return apply_blocks(ws, [Block(step["file"], before, after)], allowed={step["file"]})


def patch_step(ws: Path, step: dict, ledger: Ledger, api_key: str | None, use_llm: bool,
               ladder: tuple[str, ...] = PATCH_LADDER, out: dict | None = None) -> dict:
    rel = step["file"]
    out = out or {"step": step.get("step"), "file": rel, "action": step.get("action"),
                  "status": None, "diff": "", "escalated": False, "tier": None, "attempts": []}

    if step.get("origin") == "manifest" or not rel.endswith(".py") or not (use_llm and api_key):
        res = _plan_direct(ws, step)
        out.update(status=res.status, diff=res.diff, tier="plan", applied=res.applied)
        out["attempts"].append({"tier": "plan", "status": res.status, "cost_usd": 0.0})
        return out

    feedback: list[str] = [f"{a['tier']}: {a.get('error') or a['status']}" for a in out["attempts"]
                           if a.get("status") and not a.get("ok")]
    for i, key in enumerate(ladder):
        tier = TIERS[key]
        if not ledger.can_spend():
            out["attempts"].append({"tier": key, "status": "skipped: stage-4 budget exhausted"})
            break
        if key in ledger.dead:
            out["attempts"].append({"tier": key, "status": f"skipped: {ledger.dead[key]}"})
            continue
        msgs = patch_messages(ws, step, feedback)
        try:
            call = dispatch(tier, msgs, api_key=api_key, timeout=PATCH_TIMEOUT.get(key, 120.0))
        except Exception as e:     # network / auth / quota / timeout
            _mark_dead(ledger, key, e)
            out["attempts"].append({"tier": key, "status": f"dispatch error: {type(e).__name__}: {str(e)[:400]}"})
            feedback.append(f"{key} dispatch failed")
            continue
        res = apply_reply(ws, _reply_text(call), default_file=rel, allowed={rel})
        if res.applied:
            lint = _lint(_read(ws, rel))
            if lint:
                rollback(ws, res)
                res = replace(res, applied=False, status=f"Rejected by lint: {lint}", error=lint)
        ok = res.applied
        entry = ledger.record("patch", tier, call, success=ok and i == 0 and not out["attempts"],
                              step=step.get("step"))
        out["attempts"].append({"tier": key, "status": res.status, "error": res.error,
                                "cost_usd": entry["actual_cost_usd"], "cached": bool(call.get("cached")),
                                "finish_reason": call.get("finish_reason"),
                                "completion_tokens": call.get("completion_tokens")})
        if ok:
            out.update(status=res.status, diff=res.diff, tier=key, escalated=True if out["attempts"][:-1] else i > 0,
                       applied=True)
            return out
        feedback.append(f"{key}: {res.error or res.status}"
                        + (f" (finish_reason={call.get('finish_reason')})" if res.status == NO_BLOCKS else ""))

    # every model failed -> last resort: the architect's own before/after ($0)
    res = _plan_direct(ws, step)
    out["attempts"].append({"tier": "plan", "status": res.status, "cost_usd": 0.0})
    out.update(status=res.status if res.applied else f"FAILED: {feedback[-1] if feedback else res.status}",
               diff=res.diff, tier="plan" if res.applied else None, escalated=True, applied=res.applied)
    return out


def _norm(text: str) -> str:
    return "\n".join(l.strip() for l in (text or "").splitlines() if l.strip())


def _has(text: str, snippet: str) -> bool:
    return bool(_norm(snippet)) and _norm(snippet) in _norm(text)


def _step_done(text: str, step: dict) -> bool:
    """The step's change is in the file: old code gone and new code present."""
    before, after = step.get("before") or "", step.get("after") or ""
    return bool(after.strip()) and _has(text, after) and not (before.strip() and _has(text, before))


_STEP_COMMENT = re.compile(r"^\s*#\s*step\s*\d+\b", re.I)


def _lint(text: str) -> str | None:
    """Cheap guards against model noise the tests would not catch."""
    seen: set[str] = set()
    for line in text.splitlines():
        s = line.strip()
        if s.startswith(("import ", "from ")) and not line[:1].isspace():
            if s in seen:
                return f"duplicate import: {s[:80]}"
            seen.add(s)
        if _STEP_COMMENT.match(line):
            return f"planning comment left in code: {s[:80]}"
    return None


def file_messages(ws: Path, rel: str, steps: list[dict]) -> list[dict]:
    text = _read(ws, rel)
    view, full = _file_view(text, steps[0].get("before"), sorted({l for s in steps for l in (s.get("lines") or [])}))
    task = [{"step": s.get("step"), "action": s.get("action"), "instructions": s.get("instructions"),
             "planned_before": s.get("before"), "planned_after": s.get("after")} for s in steps]
    user = (f"FILE {rel}\nSTEPS (planned_before/after may be stale; the CURRENT file is the source of truth)\n"
            f"{json.dumps(task, indent=1, ensure_ascii=False)}\n\n"
            f"CURRENT {'FILE' if full else 'EXCERPT OF'} {rel}\n```python\n{view}\n```\n\n"
            f"Start every block with the line `{rel}`.")
    return [{"role": "system", "content": FILE_SYSTEM}, {"role": "user", "content": user}]


def _new_out(step: dict) -> dict:
    return {"step": step.get("step"), "file": step["file"], "action": step.get("action"), "status": None,
            "diff": "", "escalated": False, "tier": None, "attempts": []}


def patch_file(ws: Path, rel: str, steps: list[dict], ledger: Ledger, api_key: str) -> list[dict]:
    """All plan steps of one file: skip done -> one Lightning batch -> plan direct -> Super/Ultra per step."""
    outs = {id(s): _new_out(s) for s in steps}
    text = _read(ws, rel)
    pending = []
    for s in steps:
        if _step_done(text, s):
            outs[id(s)].update(status="Already applied", tier="none", applied=True)
        else:
            pending.append(s)

    # 1. one Lightning call for the whole file
    key = "lightning"
    if pending and ledger.can_spend() and key not in ledger.dead:
        tier = replace(TIERS[key], max_tokens=min(TIERS[key].max_tokens, LIGHTNING_MAX_TOKENS))
        before_present = {id(s): _has(text, s.get("before") or "") for s in pending}
        try:
            call = dispatch(tier, file_messages(ws, rel, pending), api_key=api_key, timeout=PATCH_TIMEOUT[key])
        except Exception as e:
            _mark_dead(ledger, key, e)
            call, err = None, f"dispatch error: {type(e).__name__}: {str(e)[:300]}"
        if call is not None:
            kept, errors = 0, []
            for b in parse_blocks(_reply_text(call), default_file=rel):
                res = apply_blocks(ws, [b], allowed={rel}, default_file=rel)
                if res.applied:
                    lint = _lint(_read(ws, rel))
                    if lint:
                        rollback(ws, res)
                        errors.append(lint)
                        continue
                    kept += 1
                else:
                    errors.append(res.error or res.status)
            text = _read(ws, rel)
            done = [s for s in pending if _step_done(text, s)
                    or (before_present[id(s)] and not _has(text, s.get("before") or ""))]
            ledger.record("patch", tier, call, success=len(done) == len(pending),
                          steps=[s.get("step") for s in pending])
            share = round(ledger.entries[-1]["actual_cost_usd"] / max(len(pending), 1), 7)
            err = "; ".join(dict.fromkeys(errors))[:300] or (
                "no SEARCH/REPLACE block in reply" if not kept else "step not covered by the reply")
            for s in pending:
                ok = s in done
                outs[id(s)]["attempts"].append({
                    "tier": key, "batch": True, "ok": ok, "cost_usd": share, "cached": bool(call.get("cached")),
                    "status": "applied (file batch)" if ok else "not applied (file batch)",
                    "error": None if ok else err, "finish_reason": call.get("finish_reason"),
                    "completion_tokens": call.get("completion_tokens")})
                if ok:
                    outs[id(s)].update(status="Applied by Lightning (file batch)", tier=key, applied=True,
                                       diff=make_diff(s.get("before") or "", s.get("after") or ""))
            pending = [s for s in pending if s not in done]
        else:
            for s in pending:
                outs[id(s)]["attempts"].append({"tier": key, "batch": True, "status": err})

    # 2. architect's own before/after ($0), 3. per-step escalation
    for s in pending:
        o = outs[id(s)]
        if _step_done(_read(ws, rel), s):      # a sibling step's edit already covered it
            o.update(status="Already applied", tier="none", applied=True)
            continue
        res = _plan_direct(ws, s)
        if res.applied and _lint(_read(ws, rel)):
            rollback(ws, res)
            res = replace(res, applied=False)
        o["attempts"].append({"tier": "plan", "status": res.status, "ok": res.applied, "cost_usd": 0.0})
        if res.applied:
            o.update(status=res.status, diff=res.diff, tier="plan", applied=True)
            continue
        patch_step(ws, s, ledger, api_key, True, ladder=ESCALATION_LADDER, out=o)
        if not o.get("applied") and _step_done(_read(ws, rel), s):
            o.update(status="Already applied", tier="none", applied=True)
    return [outs[id(s)] for s in steps]


# --------------------------------------------------------------------------- #
# Phase B: verify + heal ladder
# --------------------------------------------------------------------------- #
def _score(v: dict) -> tuple[int, int]:
    c = v.get("counts", {})
    return (c.get("passed", 0), -len(v.get("failures", [])))


def heal(ws: Path, editable: list[str], spec: str, baseline: dict | None, first: dict, ledger: Ledger,
         api_key: str | None, use_llm: bool, max_rounds_per_tier: int = 1) -> tuple[dict, list[dict]]:
    current, rounds, feedback = first, [], []
    if current["passed"] or not (use_llm and api_key) or not editable:
        return current, rounds
    for key in HEAL_LADDER:
        tier = TIERS[key]
        for _ in range(max_rounds_per_tier):
            if not ledger.can_spend():
                rounds.append({"tier": key, "status": "skipped: stage-4 budget exhausted"})
                return current, rounds
            if key in ledger.dead:
                rounds.append({"tier": key, "status": f"skipped: {ledger.dead[key]}"})
                break
            msgs = heal_messages(ws, editable, current["failures"], feedback)
            try:
                call = dispatch(tier, msgs, api_key=api_key)
            except Exception as e:
                _mark_dead(ledger, key, e)
                rounds.append({"tier": key, "status": f"dispatch error: {type(e).__name__}: {str(e)[:400]}"})
                continue
            entry = ledger.record("heal", tier, call, success=False)
            res = apply_reply(ws, _reply_text(call), allowed=set(editable))
            rnd = {"tier": key, "status": res.status, "diff": res.diff, "cost_usd": entry["actual_cost_usd"],
                   "cached": bool(call.get("cached"))}
            if not res.applied:
                rnd["error"] = res.error
                feedback.append(f"{key}: {res.error or res.status}")
                rounds.append(rnd)
                continue
            after = verify(ws, spec, baseline=baseline)
            rnd["tests"] = after["status"]
            if after["passed"]:
                ledger.mark_last(True)
                rounds.append(rnd)
                return after, rounds
            if _score(after) < _score(current):           # made it worse -> undo
                rollback(ws, res)
                rnd["rolled_back"] = True
                feedback.append(f"{key}: patch reduced passing tests ({after['status']}); it was reverted")
            else:
                current = after
                feedback.append(f"{key}: tests still failing ({after['status']})")
            rounds.append(rnd)
    return current, rounds


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def run_stage4(ws_path: str | Path, stage3: dict, *, stage2: dict | None = None, baseline: dict | None = None,
               use_llm: bool = True, api_key: str | None = None, budget_usd: float | None = None,
               verify_tests: bool = True) -> dict[str, Any]:
    ws = Path(ws_path).resolve()
    plan = [s for s in stage3.get("refactor_plan", []) if s.get("file") and not is_test_path(s["file"])]
    if not plan:
        return {"status": "nothing_to_patch", "escalated": False, "steps": [], "verification": None,
                "cost": Ledger(0).summary()}

    key = api_key or env_key("nebius_token_factory", "NEBIUS_API_KEY")
    budget = budget_usd if budget_usd is not None else float(env_key("REFORGE_STAGE4_BUDGET_USD") or 0.50)
    ledger = Ledger(budget)
    models = resolve_models(key) if (use_llm and key) else None
    files = list(dict.fromkeys(s["file"] for s in plan))
    snapshot(ws, files)

    # Phase A — patch each step (manifest steps first so the venv reinstalls once)
    plan.sort(key=lambda s: (s.get("origin") != "manifest",))
    direct = [s for s in plan if s.get("origin") == "manifest" or not s["file"].endswith(".py") or not (use_llm and key)]
    steps = [patch_step(ws, s, ledger, key, use_llm) for s in direct]
    by_file: dict[str, list[dict]] = {}
    for s in plan:
        if s not in direct:
            by_file.setdefault(s["file"], []).append(s)
    if by_file:     # files are independent -> patch them concurrently
        with ThreadPoolExecutor(max_workers=min(8, len(by_file))) as ex:
            for res in ex.map(lambda kv: patch_file(ws, kv[0], kv[1], ledger, key), by_file.items()):
                steps += res
    steps.sort(key=lambda s: (s.get("step") is None, s.get("step") or 0))

    # Phase B — verify under v2, heal on failure
    target = (stage2 or {}).get("details", {}).get("pydantic_version")
    spec = f"pydantic=={target}" if target else DEFAULT_SPEC
    verification, heal_rounds = None, []
    if verify_tests:
        first = verify(ws, spec, baseline=baseline)
        editable = [f for f in files if f.endswith(".py")]
        verification, heal_rounds = heal(ws, editable, spec, baseline, first, ledger, key, use_llm)
        verification = {**verification, "first_run": first["status"], "heal_rounds": heal_rounds}
        verification["failures"] = verification["failures"][:5]

    applied = sum(bool(s.get("applied")) for s in steps)
    escalated = any(s["escalated"] for s in steps) or bool(heal_rounds)
    if verification is None:
        status = "patched_unverified" if applied == len(steps) else "partially_patched"
    else:
        status = "verified" if verification["passed"] else "tests_failing"

    for s in steps:
        s.pop("applied", None)
    diff = "\n".join([s["diff"] for s in steps if s["diff"]]
                     + [r["diff"] for r in heal_rounds if r.get("diff") and not r.get("rolled_back")])
    return {
        "status": status,
        "escalated": escalated,
        "steps_applied": f"{applied}/{len(steps)}",
        "diff": diff,
        "steps": steps,
        "verification": verification,
        "cost": ledger.summary(),
        "telemetry": ledger.entries,
        "mode": "llm" if (use_llm and key) else "plan-direct",
        "models": models,
        "unavailable_tiers": ledger.dead,
        "backup": str(_backup_dir(ws)),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Stage 4: patch + verify with tiered escalation")
    ap.add_argument("ws_path")
    ap.add_argument("pipeline_json", help="endpoint response (needs stage3; stage2/baseline used if present)")
    ap.add_argument("--no-llm", action="store_true", help="apply the plan's before/after directly ($0)")
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--budget", type=float, default=None)
    ap.add_argument("--restore", action="store_true", help="undo the last Stage-4 run and exit")
    a = ap.parse_args(argv)
    ws = Path(a.ws_path)
    if a.restore:
        print(json.dumps({"restored": restore(ws.resolve())}, indent=2))
        return 0
    data = json.loads(Path(a.pipeline_json).read_text(encoding="utf-8"))
    out = run_stage4(ws, data.get("stage3", data), stage2=data.get("stage2"), baseline=data.get("baseline"),
                     use_llm=not a.no_llm, budget_usd=a.budget, verify_tests=not a.no_verify)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":   # from the Reforge root: python -m src.backend.route_patch.switchyard workspace/<repo> out.json
    raise SystemExit(main())
