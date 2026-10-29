"""
audit/verifier.py — Reforge Stage 5 (Parity Verifier): is the refactored repo the SAME program?

Runs the ORIGINAL code under Pydantic v1 and the REFACTORED code under Pydantic v2, side by side, and
compares what they do — not just whether tests pass.

    before  = workspace copy with the Stage-4 backup (<ws>_stage4_orig) laid back on top
              -> v1 venv (.harness_venv if it still holds pydantic 1.x, else <ws>/.parity_v1_venv)
    after   = workspace copy as patched by Stage 4
              -> Stage-2 sandbox venv (.sandbox_venv, pydantic 2.x)
    Both copies get the pristine tests from <ws>_tests_orig, so a patched test can't fake parity.

Probes (same isolation as the harness: allowlisted env, .env secrets blanked, network blocked)
  1. tests     per-test outcomes before vs after; regressions / newly fixed tests
  2. outputs   a pytest plugin records the return value of every top-level repo function called
               during each test (and each test's stdout). Values are compared at two levels:
                 identical   same value
                 equivalent  same structure/types, different values  (LLM text, timestamps, hashes)
                 similar     structure overlaps >= SIMILAR_AT           (an extra/missing key)
                 divergent   structure differs
               With capture_runs >= 2 (REFORGE_PARITY_CAPTURE_RUNS) a value that changes between two runs
               of the SAME code is flagged nondeterministic (LLM output) and judged on structure only.
  3. schemas   every BaseModel: fields, required-ness, aliases, normalised JSON-schema types and
               constraints (pattern, min_length, ...) — catches a dropped `regex=` the tests miss
  4. entry     scripts (snippet.py for pasted code): stdout compared exactly, then structurally
  5. perf      N timed runs per side (no probe attached): wall time, per-test latency, peak RSS,
               import footprint — the numbers behind the Pydantic v2 speedup

Verdict: FAIL on a regression, a divergent output/entrypoint or schema; WARN on `similar` /
missing outputs or constraint drift; PASS otherwise. Result -> data/audit/<ws>/parity.json

Entry points:
    run_verifier(ws_path, *, baseline=None, scan=None, stage2=None, ...) -> dict
    python -m src.backend.audit.verifier workspace/<repo> [pipeline.json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.backend.extract.harness import NETWORK_FLAGS, TEST_TOOLING, _discover_test_files, compare_runs, make_test_env
from src.backend.planner.grounder import ROOT
from src.backend.sandbox.sandbox import DEFAULT_SPEC, SandboxSetupError, build_env
from src.backend.sandbox.slicer import discover_modules, is_test_path

AUDIT_DIR = ROOT / "data" / "audit"
V1_VENV = ".parity_v1_venv"
VENV_DIRS = {".harness_venv", ".sandbox_venv", V1_VENV, ".venv", "venv"}
COPY_SKIP = VENV_DIRS | {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".tox", "node_modules"}
COPY_SKIP_FILES = {".pytest_report.json"}
SNIPPET_FILE = "snippet.py"
IS_WIN = sys.platform == "win32"
NO_CONSOLE_SIGNALS = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WIN else 0
PERF_MARK, SCHEMA_MARK, ENTRY_MARK = "@@REFORGE_PERF@@", "@@REFORGE_SCHEMA@@", "@@REFORGE_ENTRY@@"
SIMILAR_AT = 0.80          # structural overlap (Jaccard of path:type pairs) that still counts as "similar"
MAX_ITEMS_KEPT = 60        # non-identical comparisons kept in the report


# --------------------------------------------------------------------------- #
# Child-process scripts
# --------------------------------------------------------------------------- #
_NET_GUARD = r"""
import os, socket
if os.environ.get("REFORGE_BLOCK_NET") == "1":
    _connect = socket.socket.connect
    def _guard(self, addr):
        if isinstance(addr, tuple) and addr[0] not in ("127.0.0.1", "::1", "localhost"):
            raise ConnectionRefusedError("[reforge] network blocked: %s" % (addr[0],))
        return _connect(self, addr)
    socket.socket.connect = _guard
"""

_PEAK_RSS = r"""
def _peak_mb():
    import sys
    try:
        import resource
        r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return r / 1048576 if sys.platform == "darwin" else r / 1024
    except ImportError:
        pass
    try:
        import ctypes
        from ctypes import wintypes as w
        class PMC(ctypes.Structure):
            _fields_ = [("cb", w.DWORD), ("PageFaultCount", w.DWORD)] + [(n, ctypes.c_size_t) for n in (
                "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
        k32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
        k32.GetCurrentProcess.restype = w.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD]
        c = PMC(); c.cb = ctypes.sizeof(c)
        psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb)
        return c.PeakWorkingSetSize / 1048576
    except Exception:
        return None
"""

# pytest under a stopwatch + peak-memory reading (argv = pytest args)
_PYTEST_WRAPPER = _PEAK_RSS + r"""
import json, sys, time
t0 = time.perf_counter()
import pytest
code = int(pytest.main(sys.argv[1:]))
print("MARK" + json.dumps({"exit": code, "wall_s": time.perf_counter() - t0, "peak_rss_mb": _peak_mb()}), flush=True)
sys.exit(code)
""".replace("MARK", PERF_MARK)

# imports every repo module, describes each BaseModel (v1 or v2 API), reports import footprint (stdin = modules)
_SCHEMA_SCRIPT = _NET_GUARD + _PEAK_RSS + r"""
import importlib, inspect, io, json, sys, warnings, contextlib
warnings.simplefilter("ignore")
out, errors = {}, {}
for mod in json.load(sys.stdin):
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            m = importlib.import_module(mod)
    except BaseException as e:
        errors[mod] = "%s: %s" % (type(e).__name__, str(e)[:300]); continue
    try:
        from pydantic import BaseModel
    except Exception:
        break
    for name, cls in vars(m).items():
        if not (inspect.isclass(cls) and issubclass(cls, BaseModel) and cls is not BaseModel
                and cls.__module__ == m.__name__):
            continue
        rec = {"fields": {}}
        try:
            if hasattr(cls, "model_fields"):                     # v2
                for f, info in cls.model_fields.items():
                    rec["fields"][f] = {"required": bool(info.is_required()), "alias": info.alias or f}
                schema = cls.model_json_schema()
            else:                                                # v1
                for f, info in cls.__fields__.items():
                    rec["fields"][f] = {"required": bool(info.required), "alias": info.alias or f}
                schema = cls.schema()
            rec["schema"] = schema
        except BaseException as e:
            rec["error"] = "%s: %s" % (type(e).__name__, str(e)[:300])
        out["%s.%s" % (mod, name)] = rec
import pydantic
print("MARK" + json.dumps({"models": out, "import_errors": errors, "pydantic": pydantic.VERSION,
                           "python": sys.version.split()[0], "import_rss_mb": _peak_mb()}, default=str), flush=True)
""".replace("MARK", SCHEMA_MARK)

# runs one script as __main__, captures stdout (argv[1] = path)
_ENTRY_SCRIPT = _NET_GUARD + r"""
import io, json, runpy, sys, contextlib, traceback, warnings
warnings.simplefilter("ignore")
buf, err = io.StringIO(), None
sys.argv = [sys.argv[1]]
try:
    with contextlib.redirect_stdout(buf):
        runpy.run_path(sys.argv[0], run_name="__main__")
except SystemExit:
    pass
except BaseException as e:
    err = "%s: %s" % (type(e).__name__, str(e)[:500])
print("MARK" + json.dumps({"stdout": buf.getvalue(), "error": err}), flush=True)
""".replace("MARK", ENTRY_MARK)

# pytest plugin: return values of top-level repo functions, per test
_PROBE_PLUGIN = r'''
"""Generated by Reforge Stage 5. Records return values of the repo's top-level functions per test."""
import json, os, sys
import pytest

ROOT = os.path.normcase(os.path.realpath(os.environ["REFORGE_PARITY_ROOT"]))
OUT = os.environ["REFORGE_PARITY_OUT"]
MAX_PER_FN = int(os.environ.get("REFORGE_PARITY_MAX", "5"))
_records, _counts, _files = {}, {}, {}
_current = None


def _is_test(rel):
    parts = rel.split("/")
    name = parts[-1].lower()
    return (name.startswith("test") or name.endswith("_test.py") or "spec" in name or name == "conftest.py"
            or any(p in ("tests", "test", "testing") for p in parts[:-1]))


def _rel(filename):
    r = _files.get(filename)
    if r is None:
        r = ""
        if not filename.startswith("<"):
            p = os.path.normcase(os.path.realpath(filename))
            if p.startswith(ROOT + os.sep):
                rel = p[len(ROOT) + 1:].replace(os.sep, "/")
                if not any(s.startswith(".") for s in rel.split("/")[:-1]) and not _is_test(rel):
                    r = rel
        _files[filename] = r
    return r


def _jsonable(v, depth=0):
    if depth > 8:
        return {"__type__": type(v).__name__}
    if v is None or isinstance(v, (bool, int, str)):
        return v[:2000] if isinstance(v, str) else v
    if isinstance(v, float):
        return v if v == v and v not in (float("inf"), float("-inf")) else repr(v)
    if isinstance(v, dict):
        return {str(k): _jsonable(x, depth + 1) for k, x in list(v.items())[:200]}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x, depth + 1) for x in list(v)[:50]]
    if isinstance(v, (set, frozenset)):
        return [_jsonable(x, depth + 1) for x in sorted(v, key=repr)[:50]]
    if isinstance(v, bytes):
        return {"__bytes__": len(v)}
    try:
        if hasattr(v, "model_dump") and hasattr(type(v), "model_fields"):          # pydantic v2
            return {"__model__": type(v).__name__, **_jsonable(v.model_dump(), depth + 1)}
        if hasattr(v, "dict") and hasattr(type(v), "__fields__"):                   # pydantic v1
            return {"__model__": type(v).__name__, **_jsonable(v.dict(), depth + 1)}
    except Exception:
        pass
    return {"__type__": type(v).__name__}


def _record(code, value):
    if _current is None:
        return
    qual = getattr(code, "co_qualname", code.co_name)
    if "." in qual or "<" in qual:          # methods / validators / lambdas: internal, signature may change
        return
    key = "%s::%s" % (_rel(code.co_filename), qual)
    n = _counts.get(key, 0)
    if n >= MAX_PER_FN:
        return
    _counts[key] = n + 1
    try:
        val = _jsonable(value)
    except Exception as e:
        val = {"__unserialisable__": type(e).__name__}
    _records.setdefault(_current, []).append({"fn": key, "i": n, "value": val})


_TOOL = None


def _install():
    global _TOOL
    mon = getattr(sys, "monitoring", None)
    if mon is not None:                      # 3.12+: near-zero overhead, non-repo code disabled once
        for tid in (3, 4, 5, 1):
            try:
                mon.use_tool_id(tid, "reforge-parity")
                _TOOL = tid
                break
            except ValueError:
                continue
    if _TOOL is not None:
        def on_return(code, offset, retval):
            if not _rel(code.co_filename):
                return sys.monitoring.DISABLE
            _record(code, retval)
        sys.monitoring.register_callback(_TOOL, sys.monitoring.events.PY_RETURN, on_return)
        sys.monitoring.set_events(_TOOL, sys.monitoring.events.PY_RETURN)
    else:
        def prof(frame, event, arg):
            if event == "return" and _rel(frame.f_code.co_filename):
                _record(frame.f_code, arg)
        sys.setprofile(prof)


def _uninstall():
    if _TOOL is not None:
        sys.monitoring.set_events(_TOOL, 0)
        sys.monitoring.free_tool_id(_TOOL)
    else:
        sys.setprofile(None)


def pytest_sessionstart(session):
    _install()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    global _current
    _current = item.nodeid
    _counts.clear()
    _records.setdefault(item.nodeid, [])
    try:
        yield
    finally:
        _current = None


def pytest_sessionfinish(session, exitstatus):
    _uninstall()
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(_records, f, default=str)
'''


# --------------------------------------------------------------------------- #
# Process helpers
# --------------------------------------------------------------------------- #
def _run(cmd: list[str], cwd: Path, env: dict, timeout: float, stdin: str | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, input=stdin, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, creationflags=NO_CONSOLE_SIGNALS)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as e:
        out = "".join(x.decode("utf-8", "replace") if isinstance(x, bytes) else (x or "") for x in (e.stdout, e.stderr))
        return 124, f"{out}\n[reforge] timeout after {timeout}s"


def _marker(out: str, mark: str) -> dict | None:
    for line in reversed(out.splitlines()):
        if line.startswith(mark):
            try:
                return json.loads(line[len(mark):])
            except json.JSONDecodeError:
                return None
    return None


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts" if IS_WIN else "bin") / ("python.exe" if IS_WIN else "python")


def _env(root: Path, probe_dir: Path | None = None, **extra: str) -> dict:
    path = str(root) + (os.pathsep + str(probe_dir) if probe_dir else "")
    return make_test_env(root, {"PYTHONPATH": path, "REFORGE_BLOCK_NET": "1", **extra})


def _pydantic_version(py: Path, cwd: Path) -> str | None:
    code, out = _run([str(py), "-c", "import pydantic; print(pydantic.VERSION)"], cwd, _env(cwd), 60)
    lines = out.strip().splitlines()
    return lines[-1].strip() if code == 0 and lines else None


# --------------------------------------------------------------------------- #
# Environments
# --------------------------------------------------------------------------- #
_PYD_LINE = re.compile(r"^\s*pydantic(\[[^\]]*\])?\s*([<>=!~;].*)?$", re.I)


def _v1_spec(ws: Path, scan: dict | None) -> tuple[str, list[str]]:
    """Pydantic pin + other requirements of the ORIGINAL repo."""
    orig = _backup(ws) / "requirements.txt"
    manifest = orig if orig.is_file() else ws / "requirements.txt"
    lines = manifest.read_text(encoding="utf-8", errors="replace").splitlines() if manifest.is_file() else []
    spec = None
    for l in lines:
        s = l.split("#", 1)[0].strip()
        if _PYD_LINE.match(s) and re.search(r"==\s*1\.|<\s*2", s):
            spec = s
    cur = (scan or {}).get("current_version")
    if not spec:
        spec = f"pydantic=={cur}" if cur and str(cur).startswith("1") else "pydantic<2"
    reqs = [l for l in lines if l.strip() and not _PYD_LINE.match(l.split("#", 1)[0])]
    return spec, reqs


def build_v1_env(ws: Path, scan: dict | None = None, timeout: float = 300) -> tuple[Path, str]:
    """Reuse the Stage-1 harness venv when it still holds v1; otherwise build <ws>/.parity_v1_venv."""
    harness_py = _venv_python(ws / ".harness_venv")
    if harness_py.exists():
        v = _pydantic_version(harness_py, ws)
        if v and v.startswith("1."):
            return harness_py, v

    spec, reqs = _v1_spec(ws, scan)
    venv = ws / V1_VENV
    py = _venv_python(venv)
    stamp = hashlib.sha256("\n".join([spec, *TEST_TOOLING, *reqs]).encode()).hexdigest()
    stamp_file = venv / ".reforge_stamp"
    if not (py.exists() and stamp_file.is_file() and stamp_file.read_text() == stamp):
        uv = shutil.which("uv")
        if not py.exists():
            if uv:
                code, out = _run([uv, "venv", str(venv), "--quiet"], ws, dict(os.environ), 120)
                if code:
                    raise SandboxSetupError(f"uv venv failed: {out[-800:]}")
            else:
                import venv as _venv
                _venv.EnvBuilder(with_pip=True).create(venv)
        venv.mkdir(exist_ok=True)
        req_file = venv / "requirements.v1.txt"
        req_file.write_text("\n".join(reqs) + "\n", encoding="utf-8")
        overrides = venv / "overrides.txt"
        overrides.write_text(spec + "\n", encoding="utf-8")
        base = ([uv, "pip", "install", "--python", str(py), "--override", str(overrides)] if uv
                else [str(py), "-m", "pip", "install", "--quiet", "--disable-pip-version-check"])
        code, out = _run(base + ["-r", str(req_file), spec, *TEST_TOOLING], ws, dict(os.environ), timeout)
        if code:      # an unresolvable side dependency shouldn't block the v1 reference run
            code, out = _run(base + [spec, *TEST_TOOLING], ws, dict(os.environ), timeout)
            if code:
                raise SandboxSetupError(f"installing {spec} failed: {out[-800:]}")
            for r in reqs:
                _run(base + [r.split("#", 1)[0].strip()], ws, dict(os.environ), timeout)
        stamp_file.write_text(stamp)
    v = _pydantic_version(py, ws)
    if not v or not v.startswith("1."):
        stamp_file.unlink(missing_ok=True)
        raise SandboxSetupError(f"pydantic v1 not active in {venv.name} (got {v})")
    return py, v


# --------------------------------------------------------------------------- #
# Before / after trees
# --------------------------------------------------------------------------- #
def _backup(ws: Path) -> Path:
    return ws.parent / f"{ws.name}_stage4_orig"


def _tests_backup(ws: Path) -> Path:
    return ws.parent / f"{ws.name}_tests_orig"


def _copytree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)

    def ignore(d: str, names: list[str]) -> set[str]:
        return {n for n in names if n in COPY_SKIP or n in COPY_SKIP_FILES or n.endswith(".reforge")}
    shutil.copytree(src, dst, ignore=ignore)


def _overlay(src: Path, dst: Path) -> list[str]:
    done = []
    for p in src.rglob("*") if src.is_dir() else []:
        if p.is_file() and "__pycache__" not in p.parts:
            rel = p.relative_to(src).as_posix()
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst / rel)
            done.append(rel)
    return done


def refactored_files(ws: Path) -> list[str]:
    """Files Stage 4 changed (backup copy differs from the current workspace copy)."""
    bk, out = _backup(ws), []
    for p in sorted(bk.rglob("*")) if bk.is_dir() else []:
        if p.is_file():
            rel = p.relative_to(bk).as_posix()
            cur = ws / rel
            if not cur.is_file() or cur.read_bytes() != p.read_bytes():
                out.append(rel)
    return out


def prepare_trees(ws: Path, scratch: Path) -> dict[str, Any]:
    before, after = scratch / "before", scratch / "after"
    _copytree(ws, before)
    _copytree(ws, after)
    restored = _overlay(_backup(ws), before)
    tests_bk = _tests_backup(ws)
    tampered = [rel for rel in _overlay(tests_bk, scratch / "_tests_probe")
                if (ws / rel).is_file() and (ws / rel).read_bytes() != (tests_bk / rel).read_bytes()]
    _overlay(tests_bk, before)
    _overlay(tests_bk, after)
    return {"before": before, "after": after, "restored": restored, "tests_tampered": tampered}


# --------------------------------------------------------------------------- #
# Probes
# --------------------------------------------------------------------------- #
def _pytest_args(root: Path, report: Path, test_files: list[Path]) -> list[str]:
    return [*[str(p) for p in test_files], "--json-report", f"--json-report-file={report}",
            "-q", "--disable-warnings", "--no-header",
            "-o", "python_files=test_*.py *_test.py tests.py test.py",
            "-o", "python_functions=test_* check_* it_*",
            "-o", "python_classes=Test* Describe*",
            "--rootdir", str(root), "-p", "no:cacheprovider", "--import-mode=importlib", "--tb=short",
            *NETWORK_FLAGS]


def _read_report(report: Path) -> dict:
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {"report_available": False, "collected": 0, "passed": 0, "failed": 0, "errors": 0,
                "pass_rate": 0.0, "outcomes": {}, "durations": {}, "stdout": {}}
    s = data.get("summary", {})
    collected = s.get("collected", 0) or s.get("total", 0)
    tests = data.get("tests", [])
    dur = {t["nodeid"]: round(sum((t.get(ph) or {}).get("duration", 0.0) for ph in ("setup", "call", "teardown")), 6)
           for t in tests}
    return {
        "report_available": True, "collected": collected, "passed": s.get("passed", 0),
        "failed": s.get("failed", 0), "errors": s.get("error", 0), "skipped": s.get("skipped", 0),
        "pass_rate": round(s.get("passed", 0) / collected * 100, 2) if collected else 0.0,
        "duration": round(data.get("duration", 0.0), 4),
        "outcomes": {t["nodeid"]: t["outcome"] for t in tests},
        "durations": dur,
        "stdout": {t["nodeid"]: (t.get("call") or {}).get("stdout", "") for t in tests},
    }


def run_tests(py: Path, root: Path, work: Path, tag: str, *, probe_dir: Path | None = None,
              timeout: float = 300) -> dict:
    test_files = _discover_test_files(root)
    if not test_files:
        return {"report_available": False, "collected": 0, "outcomes": {}, "durations": {}, "stdout": {},
                "status": "no tests"}
    report = work / f"report_{tag}.json"
    report.unlink(missing_ok=True)
    args = _pytest_args(root, report, test_files)
    extra = {}
    if probe_dir:
        capture = work / f"capture_{tag}.json"
        capture.unlink(missing_ok=True)
        args += ["-p", "reforge_parity_probe"]
        extra = {"REFORGE_PARITY_ROOT": str(root), "REFORGE_PARITY_OUT": str(capture)}
    code, out = _run([str(py), "-B", "-c", _PYTEST_WRAPPER, *args], root, _env(root, probe_dir, **extra), timeout)
    res = _read_report(report)
    res.update(exit_code=code, perf=_marker(out, PERF_MARK) or {}, log_tail=out[-1500:])
    if probe_dir:
        try:
            res["capture"] = json.loads((work / f"capture_{tag}.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            res["capture"] = {}
    return res


def run_schema_probe(py: Path, root: Path, timeout: float = 120) -> dict:
    mods = [m for rel, m in discover_modules(root).items()
            if not is_test_path(rel) and Path(rel).name not in ("setup.py", "__main__.py", SNIPPET_FILE)
            and all(p.isidentifier() for p in m.split("."))]
    code, out = _run([str(py), "-B", "-c", _SCHEMA_SCRIPT], root, _env(root), timeout, stdin=json.dumps(mods))
    return _marker(out, SCHEMA_MARK) or {"models": {}, "import_errors": {"<probe>": out[-500:]}}


def run_entrypoint(py: Path, root: Path, rel: str, timeout: float = 120) -> dict:
    code, out = _run([str(py), "-B", "-c", _ENTRY_SCRIPT, str(root / rel)], root, _env(root), timeout)
    return _marker(out, ENTRY_MARK) or {"stdout": "", "error": out.strip()[-500:] or f"exit {code}"}


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #
def shape(v: Any) -> Any:
    """Structure without values: what an LLM-generated answer must keep even when its text changes."""
    if isinstance(v, dict):
        if "__model__" in v:   # model identity matters, its name does not change across v1/v2
            return {k: shape(x) for k, x in v.items() if k != "__model__"} | {"__model__": "model"}
        return {k: shape(x) for k, x in v.items()}
    if isinstance(v, list):
        uniq = {json.dumps(shape(x), sort_keys=True) for x in v}
        return {"__list__": sorted(uniq)}
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "str"
    return "null" if v is None else type(v).__name__


def _flatten(s: Any, prefix: str = "") -> set[str]:
    if isinstance(s, dict):
        if "__list__" in s:
            out = {f"{prefix}[]"}
            for item in s["__list__"]:
                out |= _flatten(json.loads(item), prefix + "[]")
            return out
        out = {f"{prefix}{{}}"}
        for k, x in s.items():
            out |= _flatten(x, f"{prefix}.{k}")
        return out
    return {f"{prefix}:{s}"}


def similarity(a: Any, b: Any) -> float:
    fa, fb = _flatten(shape(a)), _flatten(shape(b))
    return round(len(fa & fb) / len(fa | fb), 3) if fa | fb else 1.0


def _text_value(text: str) -> Any:
    """stdout -> list of per-line values (JSON / Python literal when parseable, else text)."""
    import ast
    vals = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            vals.append(json.loads(s))
            continue
        except (json.JSONDecodeError, ValueError):
            pass
        try:
            vals.append(ast.literal_eval(s))
            continue
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            pass
        vals.append(s)
    return vals



def classify(before_runs: list[Any], after_runs: list[Any]) -> dict:
    """Compare values captured on each side (one entry per run of that side)."""
    b, a = before_runs[0], after_runs[0]
    nondet = any(x != b for x in before_runs[1:]) or any(x != a for x in after_runs[1:])
    if a == b and not nondet:
        level, sim = "identical", 1.0
    elif shape(a) == shape(b):
        level, sim = "equivalent", 1.0
    else:
        sim = similarity(b, a)
        level = "similar" if sim >= SIMILAR_AT else "divergent"
    return {"level": level, "similarity": sim, "nondeterministic": nondet}


def _preview(v: Any, n: int = 240) -> str:
    s = json.dumps(v, default=str, sort_keys=True)
    return s if len(s) <= n else s[:n - 1] + "…"


def compare_outputs(before_caps: list[dict], after_caps: list[dict], before_stdout: list[dict],
                    after_stdout: list[dict], comparable_tests: set[str]) -> dict:
    def index(caps: dict) -> dict[tuple, Any]:
        return {(t, r["fn"], r["i"]): r["value"] for t, recs in caps.items() if t in comparable_tests for r in recs}

    bi, ai = [index(c) for c in before_caps], [index(c) for c in after_caps]
    counts = {k: 0 for k in ("identical", "equivalent", "similar", "divergent", "missing_after", "new_after")}
    nondet, items, fn_stats = 0, [], {}

    for key in sorted(set(bi[0]) | set(ai[0])):
        test, fn, i = key
        st = fn_stats.setdefault(fn, {"calls": 0, "identical": 0, "equivalent": 0, "similar": 0, "divergent": 0})
        if key not in ai[0]:
            counts["missing_after"] += 1
            items.append({"kind": "return", "test": test, "fn": fn, "call": i, "level": "missing_after",
                          "before": _preview(bi[0][key])})
            continue
        if key not in bi[0]:
            counts["new_after"] += 1
            continue
        res = classify([x.get(key) for x in bi if key in x], [x.get(key) for x in ai if key in x])
        counts[res["level"]] += 1
        nondet += res["nondeterministic"]
        st["calls"] += 1
        st[res["level"]] += 1
        if res["level"] != "identical":
            items.append({"kind": "return", "test": test, "fn": fn, "call": i, **res,
                          "before": _preview(bi[0][key]), "after": _preview(ai[0][key])})

    for test in sorted(comparable_tests):        # per-test stdout
        bo = [s.get(test, "") for s in before_stdout]
        ao = [s.get(test, "") for s in after_stdout]
        if not any(bo) and not any(ao):
            continue
        res = classify([_text_value(x) for x in bo], [_text_value(x) for x in ao])
        res["level"] = "identical" if bo[0] == ao[0] and not res["nondeterministic"] else res["level"]
        counts[res["level"]] += 1
        nondet += res["nondeterministic"]
        if res["level"] != "identical":
            items.append({"kind": "stdout", "test": test, **res, "before": bo[0][:240], "after": ao[0][:240]})

    order = {"divergent": 0, "missing_after": 1, "similar": 2, "equivalent": 3}
    items.sort(key=lambda x: (order.get(x["level"], 9), x.get("test", ""), x.get("fn", "")))
    compared = sum(counts[k] for k in ("identical", "equivalent", "similar", "divergent"))
    return {"compared": compared, **counts, "nondeterministic": nondet,
            "parity_pct": round((counts["identical"] + counts["equivalent"]) / compared * 100, 2) if compared else None,
            "functions": fn_stats, "items": items[:MAX_ITEMS_KEPT]}


_CONSTRAINTS = ("pattern", "minLength", "maxLength", "minItems", "maxItems", "minimum", "maximum",
                "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "enum", "const", "format", "uniqueItems")


def _norm_type(s: Any) -> Any:
    """JSON-schema fragment -> v1/v2-neutral form ($defs vs definitions, Optional as anyOf+null, titles)."""
    if not isinstance(s, dict):
        return s
    if "$ref" in s:
        return {"ref": s["$ref"].rsplit("/", 1)[-1]}
    for key in ("anyOf", "oneOf"):
        if key in s:
            opts = [o for o in s[key] if o != {"type": "null"}]
            if len(opts) == 1:
                return _norm_type({**{k: v for k, v in s.items() if k != key}, **opts[0]})
            return {key: sorted((_norm_type(o) for o in opts), key=lambda o: json.dumps(o, sort_keys=True))}
    if "allOf" in s and len(s["allOf"]) == 1:
        return _norm_type({**{k: v for k, v in s.items() if k != "allOf"}, **s["allOf"][0]})
    out: dict[str, Any] = {}
    t = s.get("type")
    if isinstance(t, list):
        t = [x for x in t if x != "null"]
        t = t[0] if len(t) == 1 else sorted(t)
    if t is not None:
        out["type"] = t
    if "items" in s:
        out["items"] = _norm_type(s["items"])
    if isinstance(s.get("additionalProperties"), dict):
        out["values"] = _norm_type(s["additionalProperties"])
    cons = {k: s[k] for k in _CONSTRAINTS if k in s}
    if cons:
        out["constraints"] = cons
    if s.get("default") is not None:
        out["default"] = s["default"]
    return out


def _model_signature(rec: dict) -> dict:
    fields = {("root" if k == "__root__" else k): v for k, v in rec.get("fields", {}).items()}
    schema = rec.get("schema") or {}
    props = schema.get("properties", {})
    if not props and fields.keys() == {"root"}:                     # __root__ / RootModel
        props = {"root": {k: v for k, v in schema.items() if k not in ("title", "$defs", "definitions", "description")}}
    sig = {}
    for name, f in fields.items():
        alias = f.get("alias") if name != "root" else "root"
        p = props.get(alias, props.get(name, {}))
        t = _norm_type(p)
        sig[name] = {"required": f.get("required"), "alias": alias,
                     "type": {k: v for k, v in t.items() if k != "constraints"},
                     "constraints": t.get("constraints", {})}
    return sig


def compare_schemas(before: dict, after: dict) -> dict:
    bm, am = before.get("models", {}), after.get("models", {})
    items, counts = [], {"identical": 0, "constraint_drift": 0, "divergent": 0, "missing_after": 0, "new_after": 0}
    for name in sorted(set(bm) | set(am)):
        if name not in am:
            counts["missing_after"] += 1
            items.append({"model": name, "level": "missing_after", "changes": ["model no longer defined"]})
            continue
        if name not in bm:
            counts["new_after"] += 1
            continue
        b, a = _model_signature(bm[name]), _model_signature(am[name])
        changes, level = [], "identical"
        for f in sorted(set(b) | set(a)):
            if f not in a or f not in b:
                changes.append(f"field `{f}` {'removed' if f not in a else 'added'}")
                level = "divergent"
                continue
            for attr in ("required", "alias", "type"):
                if b[f][attr] != a[f][attr]:
                    changes.append(f"`{f}`.{attr}: {_preview(b[f][attr], 80)} -> {_preview(a[f][attr], 80)}")
                    level = "divergent"
            if b[f]["constraints"] != a[f]["constraints"]:
                changes.append(f"`{f}` constraints: {_preview(b[f]['constraints'], 80)} -> "
                               f"{_preview(a[f]['constraints'], 80)}")
                level = level if level == "divergent" else "constraint_drift"
        if am[name].get("error"):
            changes.append(f"schema error after: {am[name]['error']}")
            level = "divergent"
        counts[level] += 1
        items.append({"model": name, "level": level, "fields": len(a), "changes": changes})
    return {"models": len(set(bm) & set(am)), **counts, "items": items,
            "import_errors": {"before": before.get("import_errors", {}), "after": after.get("import_errors", {})}}


# --------------------------------------------------------------------------- #
# Performance
# --------------------------------------------------------------------------- #
def _median(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 4) if xs else None


def summarize_perf(runs: list[dict], schema: dict) -> dict:
    per_test: dict[str, list[float]] = {}
    for r in runs:
        for t, d in r.get("durations", {}).items():
            if r.get("outcomes", {}).get(t) == "passed":
                per_test.setdefault(t, []).append(d)
    return {
        "runs": len(runs),
        "wall_s": _median([r.get("perf", {}).get("wall_s") for r in runs]),
        "test_time_s": _median([r.get("duration") for r in runs if r.get("report_available")]),
        "peak_rss_mb": _median([r.get("perf", {}).get("peak_rss_mb") for r in runs]),
        "import_rss_mb": round(schema["import_rss_mb"], 2) if schema.get("import_rss_mb") else None,
        "per_test_s": {t: _median(v) for t, v in per_test.items()},
    }


def _ratio(before: float | None, after: float | None) -> float | None:
    return round(before / after, 2) if before and after else None


def perf_delta(b: dict, a: dict) -> dict:
    tests = []
    for t in sorted(set(b["per_test_s"]) & set(a["per_test_s"])):
        tb, ta = b["per_test_s"][t], a["per_test_s"][t]
        tests.append({"test": t, "before_s": tb, "after_s": ta, "speedup_x": _ratio(tb, ta)})
    tests.sort(key=lambda x: -(x["before_s"] or 0))
    return {
        "test_time_speedup_x": _ratio(b["test_time_s"], a["test_time_s"]),
        "wall_speedup_x": _ratio(b["wall_s"], a["wall_s"]),
        "peak_rss_delta_mb": round(a["peak_rss_mb"] - b["peak_rss_mb"], 2) if a["peak_rss_mb"] and b["peak_rss_mb"] else None,
        "import_rss_delta_mb": round(a["import_rss_mb"] - b["import_rss_mb"], 2) if a["import_rss_mb"] and b["import_rss_mb"] else None,
        "per_test": tests,
        "hot_path": tests[0] if tests else None,      # slowest test before = the validation-heavy one
    }


# --------------------------------------------------------------------------- #
# Verdict
# --------------------------------------------------------------------------- #
def verdict(tests: dict, outputs: dict, schemas: dict, entry: list[dict], tampered: list[str]) -> tuple[str, list[str]]:
    fail, warn = [], []
    if tests["after"].get("collected") and not tests["after"].get("report_available"):
        fail.append("post-refactor test run produced no report")
    if tests["regressions"]:
        fail.append(f"{len(tests['regressions'])} test(s) passed before but not after")
    if outputs["divergent"]:
        fail.append(f"{outputs['divergent']} output(s) changed structure")
    if schemas["divergent"] or schemas["missing_after"]:
        fail.append(f"{schemas['divergent'] + schemas['missing_after']} model schema(s) changed shape")
    if any(e["level"] in ("divergent", "error_after") for e in entry):
        fail.append("entrypoint output diverged")
    if outputs["similar"]:
        warn.append(f"{outputs['similar']} output(s) only structurally similar")
    if outputs["missing_after"]:
        warn.append(f"{outputs['missing_after']} recorded call(s) no longer happen")
    if schemas["constraint_drift"]:
        warn.append(f"{schemas['constraint_drift']} model(s) with constraint drift")
    if tampered:
        warn.append(f"workspace tests differ from the originals ({', '.join(tampered)}); originals were used")
    if not tests["before"].get("collected") and not outputs["compared"] and not entry:
        warn.append("no tests and no entrypoints: parity rests on schemas only")
    return ("FAIL" if fail else "WARN" if warn else "PASS"), fail + warn


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def run_verifier(ws_path: str | Path, *, baseline: dict | None = None, scan: dict | None = None,
                 stage2: dict | None = None, entrypoints: list[str] | None = None,
                 capture_runs: int = int(os.environ.get("REFORGE_PARITY_CAPTURE_RUNS", "1")),
                 timing_runs: int = int(os.environ.get("REFORGE_PARITY_TIMING_RUNS", "2")), timeout: float = 300, keep_trees: bool = False,
                 out_dir: str | Path | None = None) -> dict[str, Any]:
    ws = Path(ws_path).resolve()
    out = Path(out_dir) if out_dir else AUDIT_DIR / ws.name
    out.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    if not ws.is_dir():
        return {"verdict": "ERROR", "reasons": ["workspace does not exist"], "workspace": str(ws)}
    if not _backup(ws).is_dir():
        return {"verdict": "ERROR", "reasons": [f"no Stage-4 backup at {_backup(ws).name}; run Stage 4 first"],
                "workspace": str(ws)}

    target = (stage2 or {}).get("details", {}).get("pydantic_version")
    try:
        py_b, ver_b = build_v1_env(ws, scan, timeout)
        py_a, ver_a, _ = build_env(ws, f"pydantic=={target}" if target else DEFAULT_SPEC, None, timeout)
    except SandboxSetupError as e:
        return {"verdict": "ERROR", "reasons": [f"environment setup failed: {e}"[:600]], "workspace": str(ws)}

    scratch = Path(tempfile.mkdtemp(prefix=f"reforge_parity_{ws.name}_"))
    try:
        trees = prepare_trees(ws, scratch)
        before, after = trees["before"], trees["after"]
        work = scratch / "_work"
        probe = scratch / "_probe"
        work.mkdir()
        probe.mkdir()
        (probe / "reforge_parity_probe.py").write_text(_PROBE_PLUGIN, encoding="utf-8")

        # 1+2. capture runs (probe attached) — outcomes, return values, stdout
        # before/after are independent processes -> run them side by side (timing runs stay sequential)
        pool = ThreadPoolExecutor(max_workers=2)
        side_b = pool.submit(lambda: [run_tests(py_b, before, work, f"cap_b{i}", probe_dir=probe, timeout=timeout)
                                      for i in range(capture_runs)])
        cap_a = [run_tests(py_a, after, work, f"cap_a{i}", probe_dir=probe, timeout=timeout) for i in range(capture_runs)]
        cap_b = side_b.result()
        tb, ta = cap_b[0], cap_a[0]
        cmp = compare_runs(tb, ta) if tb.get("outcomes") else {"regressions": [], "preserved": None}
        fixed = [t for t, o in ta.get("outcomes", {}).items() if o == "passed" and tb.get("outcomes", {}).get(t) != "passed"]
        both_passed = {t for t, o in tb.get("outcomes", {}).items() if o == "passed" and ta.get("outcomes", {}).get(t) == "passed"}
        tests = {
            "before": {k: tb.get(k) for k in ("collected", "passed", "failed", "errors", "skipped", "pass_rate",
                                              "report_available")},
            "after": {k: ta.get(k) for k in ("collected", "passed", "failed", "errors", "skipped", "pass_rate",
                                             "report_available")},
            "baseline_stage1": {k: (baseline or {}).get(k) for k in ("collected", "passed", "pass_rate")} if baseline else None,
            "stage2_unpatched_v2": (stage2 or {}).get("details", {}).get("test_counts") if stage2 else None,
            "regressions": cmp["regressions"], "fixed": fixed, "preserved": cmp.get("preserved"),
            "outcomes": {t: {"before": tb.get("outcomes", {}).get(t), "after": ta.get("outcomes", {}).get(t)}
                         for t in sorted(set(tb.get("outcomes", {})) | set(ta.get("outcomes", {})))},
            "log_tail_after": ta.get("log_tail", "")[-800:] if cmp["regressions"] else "",
        }
        outputs = compare_outputs([c.get("capture", {}) for c in cap_b], [c.get("capture", {}) for c in cap_a],
                                  [c.get("stdout", {}) for c in cap_b], [c.get("stdout", {}) for c in cap_a],
                                  both_passed)

        # 3. schemas + import footprint
        fut = pool.submit(run_schema_probe, py_b, before)
        sch_a = run_schema_probe(py_a, after)
        sch_b = fut.result()
        schemas = compare_schemas(sch_b, sch_a)

        # 4. entrypoints
        eps = entrypoints if entrypoints is not None else ([SNIPPET_FILE] if (ws / SNIPPET_FILE).is_file() else [])
        entry = []
        for rel in eps:
            fut = pool.submit(lambda: [run_entrypoint(py_b, before, rel) for _ in range(capture_runs)])
            ra = [run_entrypoint(py_a, after, rel) for _ in range(capture_runs)]
            rb = fut.result()
            if ra[0]["error"] and not rb[0]["error"]:
                entry.append({"script": rel, "level": "error_after", "error": ra[0]["error"],
                              "before": rb[0]["stdout"][:600], "after": ""})
                continue
            res = classify([_text_value(r["stdout"]) for r in rb], [_text_value(r["stdout"]) for r in ra])
            if rb[0]["stdout"] == ra[0]["stdout"] and not res["nondeterministic"]:
                res["level"] = "identical"
            entry.append({"script": rel, **res, "error_before": rb[0]["error"],
                          "before": rb[0]["stdout"][:600], "after": ra[0]["stdout"][:600]})

        # 5. performance (probe detached)
        pool.shutdown()
        tim_b, tim_a = [], []
        for i in range(timing_runs):     # interleaved + sequential: no CPU contention between the two sides
            tim_b.append(run_tests(py_b, before, work, f"tim_b{i}", timeout=timeout))
            tim_a.append(run_tests(py_a, after, work, f"tim_a{i}", timeout=timeout))
        pb, pa = summarize_perf(tim_b, sch_b), summarize_perf(tim_a, sch_a)
        performance = {"before": pb, "after": pa, "delta": perf_delta(pb, pa)}

        status, reasons = verdict(tests, outputs, schemas, entry, trees["tests_tampered"])
        result = {
            "verdict": status,
            "reasons": reasons,
            "workspace": str(ws),
            "generated_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_s": round((datetime.now(timezone.utc) - started).total_seconds(), 2),
            "envs": {"before": {"pydantic": ver_b, "python": sch_b.get("python"), "venv": py_b.parent.parent.name},
                     "after": {"pydantic": ver_a, "python": sch_a.get("python"), "venv": py_a.parent.parent.name}},
            "refactored_files": refactored_files(ws),
            "tests": tests,
            "outputs": outputs,
            "schemas": schemas,
            "entrypoints": entry,
            "performance": performance,
            "settings": {"capture_runs": capture_runs, "timing_runs": timing_runs, "similar_at": SIMILAR_AT},
        }
    finally:
        if keep_trees:
            print(f"[reforge] parity trees kept at {scratch}", file=sys.stderr)
        else:
            shutil.rmtree(scratch, ignore_errors=True)

    (out / "parity.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    result["path"] = str(out / "parity.json")
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Stage 5: before/after parity (v1 original vs v2 refactor)")
    ap.add_argument("ws_path")
    ap.add_argument("pipeline_json", nargs="?", help="endpoint response (scan/baseline/stage2 used if present)")
    ap.add_argument("--entry", action="append", help="script to run in both envs and compare stdout (repeatable)")
    ap.add_argument("--capture-runs", type=int, default=1, help="2+ flags nondeterministic (LLM) outputs")
    ap.add_argument("--timing-runs", type=int, default=2)
    ap.add_argument("--timeout", type=float, default=300)
    ap.add_argument("--keep-trees", action="store_true", help="keep the before/after copies for debugging")
    a = ap.parse_args(argv)
    data = json.loads(Path(a.pipeline_json).read_text(encoding="utf-8")) if a.pipeline_json else {}
    res = run_verifier(a.ws_path, baseline=data.get("baseline"), scan=data.get("scan"), stage2=data.get("stage2"),
                       entrypoints=a.entry, capture_runs=a.capture_runs, timing_runs=a.timing_runs,
                       timeout=a.timeout, keep_trees=a.keep_trees)
    print(json.dumps({k: res.get(k) for k in ("verdict", "reasons", "path")}, indent=2))
    return 0 if res.get("verdict") in ("PASS", "WARN") else 1


if __name__ == "__main__":   # from the Reforge root: python -m src.backend.audit.verifier workspace/<repo> [out.json]
    raise SystemExit(main())
