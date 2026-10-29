"""
endpoints/web.py — Reforge web app: the UI + a live, streamed version of /api/run-pipeline.

Run (from the Reforge root):
    uvicorn src.backend.endpoints.web:app --reload --reload-dir src/backend/endpoints --reload-dir src/frontend ...

Serves:
    GET  /                       the frontend (src/frontend, static, no build step)
    GET  /api/health             liveness probe (the UI falls back to demo mode when it fails)
    POST /api/run-stream         same form fields as /api/run-pipeline, streams NDJSON events:
                                   {"type":"stage","id":..,"status":"running"|"done","elapsed":s,"facts":[[label,value],..]}
                                   {"type":"log","line":..}   {"type":"result","data":{..}}   {"type":"error","detail":..}
    POST /api/run-pipeline       unchanged (endpoint.py)
    GET  /api/report/{name}      unchanged (endpoint.py)
    GET  /api/patch/{name}       combined git-style patch of the last run
    GET  /api/limits             caller's daily allowance (runs left, judge mode, demo video URL)
    POST /api/judge/verify       check a judge key (header X-Reforge-Judge-Key or form judge_key)
    Daily limits (limits.py) guard /api/run-stream and /api/run-pipeline: 429 + pop-up payload when over.

Env:
    REFORGE_CORS   comma-separated allowed origins when the UI is hosted elsewhere (default "*")
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from src.backend.endpoints.endpoint import (DATA_ROOT, GIT_TIMEOUT, WORKSPACE_ROOT, app, check_python, sync_repo,
                                            write_snippet_workspace)
from src.backend.extract.normalise import normalize_github_url, normalize_pasted_code
from src.backend.extract.scanner import scan_environment
from src.backend.extract.harness import run_harness
from src.backend.sandbox.slicer import run_stage2
from src.backend.planner.planner import run_stage3
from src.backend.route_patch.switchyard import run_stage4
from src.backend.audit.report import file_diffs, run_stage5
from src.backend.endpoints import limits
from src.backend.planner.grounder import env_key

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"      # src/frontend
_RUN_LOCK = threading.Lock()                                           # one pipeline at a time (shared workspace)

app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in os.environ.get("REFORGE_CORS", "*").split(",")],
                   allow_methods=["*"], allow_headers=["*"])


# --------------------------------------------------------------------------- #
# stdout -> event queue (so the UI can show the backend's own [DEBUG]/[TIMING] lines live)
# --------------------------------------------------------------------------- #
class _Tee:
    def __init__(self, orig, q: "queue.Queue[dict]"):
        self.orig, self.q, self.buf = orig, q, ""

    def write(self, s: str) -> int:
        try:
            self.orig.write(s)
        except Exception:
            pass
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            if line.strip():
                self.q.put({"type": "log", "line": line[:400]})
        return len(s)

    def flush(self) -> None:
        try:
            self.orig.flush()
        except Exception:
            pass

    def __getattr__(self, name):          # isatty, encoding, fileno ...
        return getattr(self.orig, name)


# --------------------------------------------------------------------------- #
# Compact, display-ready facts per stage (defensive: never break the stream)
# --------------------------------------------------------------------------- #
def _g(d: Any, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k)
    return default if d is None else d


def _usd(x) -> str:
    try:
        return f"${float(x):.4f}"
    except (TypeError, ValueError):
        return "—"


def stage_facts(stage: str, r: dict) -> list[list[str]]:
    try:
        if stage == "ingest":
            return [["Input", "GitHub repository" if r["input_type"] == "github" else "Code snippet"],
                    ["Workspace", f"{r['ws']} ({r['workspace']})"]]
        if stage == "scan":
            s, b = r["scan"], r["baseline"]
            return [["Package", f"{s.get('package')} {s.get('current_version')} → {s.get('latest_version')}"],
                    ["Known CVEs", str(s.get("cve_count", 0))],
                    ["Baseline tests (v1)", f"{b.get('passed', 0)}/{b.get('collected', 0)} passing"]]
        if stage == "break":
            s2 = r["stage2"]
            syms = s2.get("breakage_symbols") or []
            return [["Break run (v2)", str(s2.get("test_status", "—"))],
                    ["Newly broken tests", str(len(_g(s2, "details", "newly_broken", default=[])))],
                    ["Affected files", str(len(s2.get("affected_files") or []))],
                    ["Breaking symbols", ", ".join(map(str, syms[:6])) + (" …" if len(syms) > 6 else "") or "—"]]
        if stage == "plan":
            s3 = r["stage3"]
            p = s3.get("planner") or {}
            docs = _g(s3, "grounding", "stats", default={}) or {}
            return [["Refactor steps", str(len(s3.get("refactor_plan") or []))],
                    ["Planner", f"Nemotron 3 Ultra · {p.get('mode', '—')}" + (" · cache hit" if _g(p, "cache", "hit") else "")],
                    ["Grounding", ", ".join(f"{k}: {v}" for k, v in list(docs.items())[:3]) or "official docs"]]
        if stage == "patch":
            s4 = r["stage4"]
            c = s4.get("cost") or {}
            tiers: dict[str, int] = {}
            for st in s4.get("steps") or []:
                tiers[str(st.get("tier") or "plan")] = tiers.get(str(st.get("tier") or "plan"), 0) + 1
            return [["Steps applied", str(s4.get("steps_applied", "—"))],
                    ["Status", str(s4.get("status", "—")) + (" · escalated" if s4.get("escalated") else "")],
                    ["Tier mix", ", ".join(f"{k} ×{v}" for k, v in tiers.items()) or "—"],
                    ["Spend vs Ultra-only", f"{_usd(c.get('actual_usd'))} vs {_usd(c.get('counterfactual_ultra_usd'))}"]]
        if stage == "report":
            s5 = r["stage5"]
            sm = s5.get("summary") or {}
            return [["Parity verdict", str(s5.get("verdict", "—"))],
                    ["Files refactored", str(sm.get("files_refactored", 0))],
                    ["Saved vs Ultra-only", _usd(sm.get("saved_usd"))]]
    except Exception as e:     # facts are cosmetic
        return [["note", f"{type(e).__name__}"]]
    return []


def ui_result(r: dict, ws_path: Path, timings: dict) -> dict:
    """Everything the results page needs, small enough to ship in one event."""
    scan, base, s2, s3, s4, s5 = (r.get(k) or {} for k in ("scan", "baseline", "stage2", "stage3", "stage4", "stage5"))
    try:
        files = [{k: d[k] for k in ("file", "before", "after", "unified", "added", "removed")} for d in file_diffs(ws_path)]
    except Exception:
        files = []
    ver = s4.get("verification") or {}
    tiers: dict[str, int] = {}
    for st in s4.get("steps") or []:
        tiers[str(st.get("tier") or "plan")] = tiers.get(str(st.get("tier") or "plan"), 0) + 1
    return {
        "ws": ws_path.name, "input_type": r.get("input_type"),
        "package": scan.get("package"), "from_version": scan.get("current_version"),
        "to_version": _g(s2, "details", "pydantic_version") or scan.get("latest_version"),
        "verdict": s5.get("verdict"), "reasons": s5.get("reasons") or [], "summary": s5.get("summary") or {},
        "vulnerabilities": (scan.get("vulnerabilities") or [])[:10], "cve_count": scan.get("cve_count", 0),
        "tests": {"before": {"passed": base.get("passed"), "collected": base.get("collected")},
                  "after": {"passed": ver.get("passed_count", ver.get("n_passed")), "collected": ver.get("collected"),
                            "ok": ver.get("passed")},
                  "newly_broken": len(_g(s2, "details", "newly_broken", default=[]))},
        "plan": {"steps": [{"step": s.get("step"), "file": s.get("file"), "action": s.get("action"),
                            "title": s.get("title") or s.get("description") or s.get("rationale")}
                           for s in (s3.get("refactor_plan") or [])][:40],
                 "notes": s3.get("notes") or [], "mode": _g(s3, "planner", "mode")},
        "patch": {"status": s4.get("status"), "steps_applied": s4.get("steps_applied"),
                  "escalated": s4.get("escalated"), "tiers": tiers, "cost": s4.get("cost") or {}},
        "breakage_symbols": (s2.get("breakage_symbols") or [])[:20],
        "timings_s": timings,
        "files": files,
        "report_url": f"/api/report/{ws_path.name}",
        "report_view_url": f"/api/report/{ws_path.name}/view",
        "patch_url": f"/api/patch/{ws_path.name}",
    }



# --------------------------------------------------------------------------- #
# Pre-flight: is there Pydantic v1 code to migrate at all? (cheap, no credits spent)
# --------------------------------------------------------------------------- #
import ast as _ast
import re as _re

_SKIP_DIRS = {"site-packages", "node_modules", "__pycache__", "venv", "env", "build", "dist"}
_V1_IMPORTS = {"validator", "root_validator", "parse_obj_as", "parse_raw_as", "parse_file_as", "GenericModel",
               "BaseSettings", "validate_arguments"}
_V1_ATTRS = {"parse_obj", "parse_raw", "parse_file", "from_orm", "schema_json", "__fields__", "update_forward_refs"}
_V1_KWARGS = {"regex", "min_items", "max_items", "unique_items", "allow_mutation", "const"}
_V2_IMPORTS = {"field_validator", "model_validator", "ConfigDict", "RootModel", "TypeAdapter", "computed_field",
               "field_serializer", "model_serializer"}
_V2_ATTRS = {"model_dump", "model_dump_json", "model_validate", "model_validate_json", "model_json_schema",
             "model_fields", "model_copy", "model_config"}
_PIN_RE = _re.compile(r"""(?<![\w-])pydantic(?![\w-])\s*(?:\[[^\]]*\])?\s*(==|~=|>=|<=|!=|<|>|\^|=)\s*v?(\d+(?:\.\d+)*)""", _re.I)
_MANIFESTS = ("requirements*.txt", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "environment.yml")


def _profile_source(src: str, prof: dict) -> None:
    try:
        tree = _ast.parse(src)
    except (SyntaxError, ValueError):
        return
    imports_pyd = False
    for n in _ast.walk(tree):
        if isinstance(n, _ast.Import) and any(a.name.split(".")[0] == "pydantic" for a in n.names):
            imports_pyd = True
            if any(a.name.startswith("pydantic.v1") for a in n.names):
                prof["v1"].add("pydantic.v1")
        elif isinstance(n, _ast.ImportFrom) and not n.level and (n.module or "").split(".")[0] == "pydantic":
            imports_pyd = True
            names = {a.name for a in n.names}
            if (n.module or "").startswith(("pydantic.v1", "pydantic.generics")):
                prof["v1"].add(n.module)
            prof["v1"].update(names & _V1_IMPORTS)
            prof["v2"].update(names & _V2_IMPORTS)
    if not imports_pyd:
        return
    prof["uses"] = True
    for n in _ast.walk(tree):
        if isinstance(n, _ast.ClassDef):
            if any(isinstance(b, _ast.ClassDef) and b.name == "Config" for b in n.body):
                prof["v1"].add("class Config")
            for b in n.body:
                tgt = [b.target] if isinstance(b, _ast.AnnAssign) else (b.targets if isinstance(b, _ast.Assign) else [])
                for t in tgt:
                    if isinstance(t, _ast.Name) and t.id == "__root__":
                        prof["v1"].add("__root__")
                    if isinstance(t, _ast.Name) and t.id == "model_config":
                        prof["v2"].add("model_config")
        elif isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            for d in n.decorator_list:
                f = d.func if isinstance(d, _ast.Call) else d
                name = f.id if isinstance(f, _ast.Name) else getattr(f, "attr", None)
                if name in ("validator", "root_validator"):
                    prof["v1"].add(f"@{name}")
        elif isinstance(n, _ast.Attribute):
            if n.attr in _V1_ATTRS:
                prof["v1"].add(f".{n.attr}")
            elif n.attr in _V2_ATTRS:
                prof["v2"].add(f".{n.attr}")
        elif isinstance(n, _ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", None)) == "Field":
            prof["v1"].update(f"Field({k.arg}=)" for k in n.keywords if k.arg in _V1_KWARGS)


def pydantic_profile(ws: Path | None = None, code: str | None = None) -> dict:
    prof: dict[str, Any] = {"uses": False, "v1": set(), "v2": set(), "pin_major": None, "pin": None}
    if code is not None:
        _profile_source(code, prof)
        return prof
    for py in ws.rglob("*.py"):
        parts = py.relative_to(ws).parts[:-1]
        if any(p.startswith(".") or p in _SKIP_DIRS for p in parts):
            continue
        try:
            _profile_source(py.read_text(encoding="utf-8", errors="replace"), prof)
        except OSError:
            continue
    for pat in _MANIFESTS:
        for mf in ws.glob(pat):
            try:
                m = _PIN_RE.search(mf.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            if m:
                op, ver = m.group(1), m.group(2)
                major = int(ver.split(".")[0])
                prof["pin"] = ver if op in ("==", "=") else f"{op}{ver}"
                prof["pin_major"] = major - 1 if op == "<" else major      # "<2" means v1
                break
        if prof["pin_major"] is not None:
            break
    return prof


def preflight(prof: dict, label: str) -> dict | None:
    """None -> go ahead. Otherwise a {code, title, message} notice for the UI."""
    if not prof["uses"]:
        extra = " Pydantic is listed as a dependency but never imported." if prof["pin"] else ""
        return {"code": "no_pydantic", "title": "No Pydantic code found",
                "message": f"{label} doesn't import Pydantic anywhere, so there's nothing to migrate.{extra} "
                           f"Reforge currently upgrades Pydantic v1 code to v2 — try a repository or snippet that uses Pydantic v1."}
    signs = ", ".join(sorted(prof["v2"])[:4])
    if not prof["v1"] and ((prof["pin_major"] or 0) >= 2 or (prof["pin_major"] is None and prof["v2"])):
        pin = f" (pinned pydantic {prof['pin']})" if prof["pin"] else ""
        return {"code": "up_to_date", "title": "Already on Pydantic v2",
                "message": f"{label} already uses Pydantic v2{pin}" + (f" — it uses {signs}" if signs else "") +
                           ". No v1 APIs were found, so there's nothing to upgrade."}
    return None

# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health():
    return {"ok": True, "busy": _RUN_LOCK.locked()}


@app.get("/api/limits")
def get_limits(request: Request):
    return limits.status(request)


@app.post("/api/judge/verify")
def judge_verify(request: Request, judge_key: Optional[str] = Form(None)):
    limits.verify_key(request, judge_key or request.headers.get("x-reforge-judge-key") or "")
    return limits.status(request, judge_key)


# /api/run-pipeline (endpoint.py) is the same pipeline without streaming: put it behind the same limits.
@app.middleware("http")
async def _guard_run_pipeline(request: Request, call_next):
    if request.url.path.rstrip("/") == "/api/run-pipeline" and request.method == "POST":
        from fastapi.responses import JSONResponse
        if not _RUN_LOCK.acquire(blocking=False):
            return JSONResponse({"detail": "A pipeline run is already in progress. Try again when it finishes."}, 409)
        try:
            ticket = limits.admit(request)
        except HTTPException as e:
            _RUN_LOCK.release()
            return JSONResponse({"detail": e.detail}, e.status_code)
        try:
            return await call_next(request)
        finally:
            ticket.finish()
            _RUN_LOCK.release()
    return await call_next(request)


@app.get("/api/report/{name}/view")
def view_report(name: str):
    """Same PDF as /api/report/{name}, but inline so the browser can render it in the viewer."""
    pdf = DATA_ROOT / "audit" / Path(name).name / "report.pdf"
    if not pdf.is_file():
        raise HTTPException(404, "no report yet")
    return FileResponse(pdf, media_type="application/pdf", filename=f"reforge_{name}.pdf",
                        content_disposition_type="inline")


@app.get("/api/patch/{name}")
def download_patch(name: str):
    p = DATA_ROOT / "audit" / Path(name).name / "reforge.patch"
    if not p.is_file():
        raise HTTPException(404, "no patch yet")
    return FileResponse(p, media_type="text/x-diff", filename=f"reforge_{name}.patch")


@app.post("/api/run-stream")
def run_stream(request: Request, content: Optional[str] = Form(None), repo_url: Optional[str] = Form(None),
               code_snippet: Optional[str] = Form(None), code_file: UploadFile = File(None),
               judge_key: Optional[str] = Form(None)):
    raw = (content or repo_url or code_snippet or "").strip()
    if code_file is not None and code_file.filename:
        raw = code_file.file.read().decode("utf-8-sig", errors="replace").strip()
    if not raw:
        raise HTTPException(400, "Missing input content.")
    url = normalize_github_url(raw.strip("\"'"))
    code = None
    if not url:                      # validate before streaming -> proper 422, no credits spent
        code = normalize_pasted_code(raw)
        check_python(code, raw)
        notice = preflight(pydantic_profile(code=code), "This code" if not (code_file and code_file.filename)
                           else f"{Path(code_file.filename).name}")
        if notice:
            raise HTTPException(422, notice)
    if not _RUN_LOCK.acquire(blocking=False):
        raise HTTPException(409, "A pipeline run is already in progress. Try again when it finishes.")
    try:
        ticket = limits.admit(request, judge_key)       # 429 (limit pop-up) / 403 (bad judge key)
    except HTTPException:
        _RUN_LOCK.release()
        raise

    q: "queue.Queue[dict]" = queue.Queue()
    DONE = object()

    def work() -> None:
        orig_out = sys.stdout
        sys.stdout = _Tee(orig_out, q)
        t0 = time.perf_counter()
        timings: dict[str, float] = {}
        r: dict[str, Any] = {}
        halted = False
        cur = {"id": None, "t": t0}

        def begin(sid: str) -> None:
            cur["id"], cur["t"] = sid, time.perf_counter()
            q.put({"type": "stage", "id": sid, "status": "running"})

        def end(sid: str, key: str) -> None:
            el = round(time.perf_counter() - cur["t"], 2)
            timings[key] = el
            ev = {"type": "stage", "id": sid, "status": "done", "elapsed": el, "facts": stage_facts(sid, r)}
            if sid == "patch":     # drives the live model-router panel
                tiers: dict[str, int] = {}
                for st in r["stage4"].get("steps") or []:
                    tiers[str(st.get("tier") or "plan")] = tiers.get(str(st.get("tier") or "plan"), 0) + 1
                ev["data"] = {"tiers": tiers, "cost": r["stage4"].get("cost") or {}}
            q.put(ev)

        try:
            begin("ingest")
            if url:
                r["input_type"] = "github"
                ws = WORKSPACE_ROOT / url.rstrip("/").split("/")[-1].replace(".git", "")
                try:
                    r["workspace"] = sync_repo(url, ws)
                except subprocess.CalledProcessError as e:
                    raise RuntimeError(f"git {e.cmd[1]} failed: {(e.stderr or '').strip()[-400:]}")
                except subprocess.TimeoutExpired:
                    raise RuntimeError(f"git timed out after {GIT_TIMEOUT}s")
            else:
                r["input_type"] = "text"
                ws = WORKSPACE_ROOT / "text"
                write_snippet_workspace(ws, code)
                r["workspace"] = "snippet"
            r["ws"] = ws.name
            end("ingest", "input + clone")
            if url:
                notice = preflight(pydantic_profile(ws), ws.name)
                if notice:
                    print(f"[preflight] {notice['code']}: stopping before any tests or model calls")
                    q.put({"type": "halt", **notice})
                    halted = True
                    return

            begin("scan")
            r["scan"] = scan_environment(ws, input_type=r["input_type"])
            r["baseline"] = run_harness(ws)
            end("scan", "stage1_scan_baseline")

            begin("break")
            r["stage2"] = run_stage2(ws, baseline=r["baseline"], target_version=r["scan"]["latest_version"])
            end("break", "stage2_break_run")

            begin("plan")
            r["stage3"] = run_stage3(ws, r["stage2"])
            end("plan", "stage3_ground_plan")

            begin("patch")
            r["stage4"] = run_stage4(ws, r["stage3"], stage2=r["stage2"], baseline=r["baseline"],
                                     budget_usd=ticket.stage4_budget(float(env_key("REFORGE_STAGE4_BUDGET_USD") or 0.50)))
            end("patch", "stage4_patch_verify")

            begin("report")
            pipeline = {k: r[k] for k in ("input_type", "workspace", "scan", "baseline", "stage2", "stage3", "stage4")}
            r["stage5"] = run_stage5(ws, pipeline)
            end("report", "stage5_parity_report")

            timings["total"] = round(time.perf_counter() - t0, 2)
            print(f"[TIMING] {timings}")
            q.put({"type": "result", "data": ui_result(r, ws, timings)})
        except Exception as e:
            q.put({"type": "error", "stage": cur["id"], "detail": f"{type(e).__name__}: {str(e)[:600]}"})
        finally:
            sys.stdout = orig_out
            try:
                ticket.finish(halted=halted)
            except Exception as e:
                print(f"[limits] finish failed: {e}")
            _RUN_LOCK.release()
            q.put(DONE)        # type: ignore[arg-type]

    threading.Thread(target=work, name="reforge-run", daemon=True).start()

    def events():
        yield json.dumps({"type": "hello", "input_type": "github" if url else "text"}) + "\n"
        while True:
            try:
                ev = q.get(timeout=10)
            except queue.Empty:
                yield json.dumps({"type": "tick"}) + "\n"       # keep proxies from closing an idle stream
                continue
            if ev is DONE:
                return
            yield json.dumps(ev, default=str) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# Frontend last: the "/" mount must not shadow the /api routes above.
if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="ui")
