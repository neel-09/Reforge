"""
sandbox/sandbox.py — Reforge Stage 2 (dynamic half): the break run.

Runs the UNMODIFIED workspace repo against Pydantic v2 to force it to crash, then
mines the tracebacks for origin file, line, exception type, message and the v1
breakage symbols named in the message.

Mirrors extract/harness.py so Stage 1 and Stage 2 results line up:
  * own persistent venv `<ws>/.sandbox_venv` (uv, stdlib-venv fallback), repo deps
    installed but pydantic forced to v2 (`--override` with uv; pin stripped otherwise)
  * same test discovery, pytest flags and pytest-json-report parsing as the harness
  * extra: `--tb=native`, v2 deprecation warnings escalated to errors,
    `--continue-on-collection-errors`, and an import sweep for modules no test touches
  * same isolation as the harness (make_test_env + pytest-socket): allowlisted env,
    `.env` secrets blanked, outbound network blocked -> no paid API calls, deterministic results

The venv is reusable for Stage 5 (post-patch verification under v2).

Endpoint entry point (via sandbox.slicer.run_stage2) or directly:
    run_break(ws_path, baseline=None, target_version=None) -> SandboxReport
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from src.backend.extract.harness import NETWORK_FLAGS, TEST_TOOLING, _discover_test_files, _parse_report, compare_runs, make_test_env
from src.backend.sandbox.slicer import FIELD_V1_KWARGS, canonical_symbol, discover_modules, is_test_path

VENV_DIRNAME = ".sandbox_venv"
DEFAULT_SPEC = "pydantic>=2,<3"
SWEEP_MARK = "@@REFORGE_SWEEP@@"
IS_WIN = sys.platform == "win32"
# Windows delivers console Ctrl+C (e.g. a `uvicorn --reload` restart) to every process on the console;
# a separate process group keeps the sandbox's pytest from being interrupted (exit code 2).
NO_CONSOLE_SIGNALS = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WIN else 0
PYTEST_EXIT = {2: "interrupted", 3: "internal error", 4: "usage error"}


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
@dataclass
class Crash:
    test_id: str
    kind: str                 # failed | error | collection | import | pytest
    exc_type: str
    message: str
    file: str | None          # repo-relative origin of the crash
    line: int | None
    symbols: list[str]
    frames: list[dict]        # repo-local frames, outermost -> innermost


@dataclass
class SandboxReport:
    test_status: str
    counts: dict[str, Any] = field(default_factory=dict)
    breakage_symbols: list[str] = field(default_factory=list)
    affected_files: list[str] = field(default_factory=list)
    crashes: list[Crash] = field(default_factory=list)
    outcomes: dict[str, str] = field(default_factory=dict)
    newly_broken: list[str] = field(default_factory=list)   # passed in Stage-1 baseline, broken under v2
    exit_code: int | None = None
    setup_ok: bool = True
    pydantic_version: str | None = None
    python: str | None = None
    log_tail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SandboxSetupError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Process / env helpers
# --------------------------------------------------------------------------- #
def _run(cmd: list[str], cwd: Path, env: dict | None = None, timeout: float | None = None,
         stdin: str | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, input=stdin, capture_output=True, text=True,
                           creationflags=NO_CONSOLE_SIGNALS,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as e:
        out = "".join(x.decode("utf-8", "replace") if isinstance(x, bytes) else (x or "") for x in (e.stdout, e.stderr))
        return 124, f"{out}\n[reforge] timeout after {timeout}s"


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts" if IS_WIN else "bin") / ("python.exe" if IS_WIN else "python")


def _test_env(ws: Path, isolate: bool) -> dict[str, str]:
    """Harness isolation; isolate=False passes the host env through (debugging only)."""
    if isolate:
        return make_test_env(ws)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    env.update(PYTHONPATH=str(ws), PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    return env


# --------------------------------------------------------------------------- #
# Environment: repo deps + pydantic v2 (forced)
# --------------------------------------------------------------------------- #
_PYD_LINE = re.compile(r"^\s*pydantic(\[[^\]]*\])?\s*([<>=!~;].*)?$", re.I)


def build_env(ws: Path, spec: str, python: str | None = None, timeout: float = 300) -> tuple[Path, str, str]:
    """Create/refresh `<ws>/.sandbox_venv`. Returns (python, pydantic_version, log)."""
    manifest = ws / "requirements.txt"
    if not manifest.is_file():
        raise SandboxSetupError("requirements.txt missing. Run Stage 1 scanner first.")
    venv = ws / VENV_DIRNAME
    py = _venv_python(venv)
    uv = shutil.which("uv")
    log: list[str] = []

    reqs = [l for l in manifest.read_text(encoding="utf-8", errors="replace").splitlines()
            if not _PYD_LINE.match(l.split("#", 1)[0])]
    stamp = hashlib.sha256("\n".join([spec, python or "", *TEST_TOOLING, *reqs]).encode()).hexdigest()
    stamp_file = venv / ".reforge_stamp"

    if not (py.exists() and stamp_file.is_file() and stamp_file.read_text() == stamp):
        if not py.exists():
            if uv:
                code, out = _run([uv, "venv", str(venv), "--quiet", *(["--python", python] if python else [])], ws, timeout=120)
                if code:
                    raise SandboxSetupError(f"uv venv failed: {out[-1500:]}")
            else:
                import venv as _venv
                _venv.EnvBuilder(with_pip=True).create(venv)
        if not py.exists():
            raise SandboxSetupError(f"venv python not found at {py}")

        filtered = venv / "requirements.v2.txt"
        filtered.write_text("\n".join(reqs) + "\n", encoding="utf-8")
        overrides = venv / "overrides.txt"
        overrides.write_text(spec + "\n", encoding="utf-8")
        if uv:   # override beats transitive `pydantic<2` pins too
            base = [uv, "pip", "install", "--python", str(py), "--override", str(overrides)]
        else:
            base = [str(py), "-m", "pip", "install", "--quiet", "--disable-pip-version-check"]

        code, out = _run(base + ["-r", str(filtered), spec, *TEST_TOOLING], ws, timeout=timeout)
        log.append(f"$ install deps + {spec} -> {code}\n{out[-1500:]}")
        if code:  # one unresolvable pin shouldn't sink the run
            code, out = _run(base + [spec, *TEST_TOOLING], ws, timeout=timeout)
            if code:
                raise SandboxSetupError(f"installing {spec} failed: {out[-1500:]}")
            for spec_line in reqs:
                s = spec_line.split("#", 1)[0].strip()
                if s and not s.startswith("-"):
                    c, o = _run(base + [s], ws, timeout=timeout)
                    if c:
                        log.append(f"  skipped {s}: {o.strip()[-300:]}")
        stamp_file.write_text(stamp)

    code, out = _run([str(py), "-c", "import sys, pydantic; print(pydantic.VERSION, sys.version.split()[0])"],
                     ws, env=_test_env(ws, True), timeout=60)
    parts = out.strip().splitlines()[-1].split() if out.strip() else []
    if code or not parts or not parts[0].startswith("2"):
        stamp_file.unlink(missing_ok=True)
        raise SandboxSetupError(f"pydantic v2 not active in sandbox: {out.strip()[-500:]}")
    log.append(f"python {parts[1] if len(parts) > 1 else '?'}")
    return py, parts[0], "\n".join(log)


# --------------------------------------------------------------------------- #
# Traceback parsing
# --------------------------------------------------------------------------- #
NATIVE_FRAME = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>.+)$', re.M)
SYNTAX_LOC = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+)\s*$', re.M)   # SyntaxError location
NO_SYMBOL_EXC = ("SyntaxError", "IndentationError", "TabError")
PYTEST_FRAME = re.compile(r'^(?P<file>(?:[A-Za-z]:)?[^\s":][^\n:]*?\.py):(?P<line>\d+): in (?P<func>.+)$', re.M)
EXC_LINE = re.compile(r'^(?:E\s+)?(?P<type>(?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*(?:Error|Exception|Warning|Exit|Interrupt|Failed))'
                      r'(?::\s?(?P<msg>.*))?$')


def _field_kw(k: str) -> str:
    return f"Field({k}=...)" if k in FIELD_V1_KWARGS else k


# message pattern -> symbol (same vocabulary as slicer.py)
MSG_RULES: list[tuple[re.Pattern, Any]] = [
    (re.compile(r"cannot import name '(\w+)' from 'pydantic"), lambda m: canonical_symbol(m[1])),
    (re.compile(r"No module named '(pydantic\.[\w.]+)'"), lambda m: m[1]),
    (re.compile(r"`(\w+)` has been moved to"), lambda m: m[1]),
    (re.compile(r"'(\w+)' has been (?:renamed|removed)"), lambda m: m[1]),            # v1 config keys
    (re.compile(r"__root__"), lambda m: "__root__"),
    (re.compile(r"`@?(root_validator|validator)`"), lambda m: "@" + m[1]),
    (re.compile(r"The `(\w+)` method is deprecated"), lambda m: f".{m[1]}()"),
    (re.compile(r"The `(__\w+__)` attribute is deprecated"), lambda m: m[1]),
    (re.compile(r"`(\w+)` is (?:removed|deprecated)"), lambda m: _field_kw(m[1])),     # Field(regex=...), parse_obj_as
    (re.compile(r"to use from_orm"), lambda m: ".from_orm()"),
    (re.compile(r"'Config' and 'model_config'|class-based `config` is deprecated"), lambda m: "class Config"),
    (re.compile(r"model_dump_json\(\) got an unexpected keyword argument|dumps_kwargs"), lambda m: ".json()"),
]


def symbols_from_message(text: str) -> list[str]:
    out: list[str] = []
    for rx, fn in MSG_RULES:
        for m in rx.finditer(text or ""):
            s = fn(m)
            if s and s not in out:
                out.append(s)
    return out


def _to_repo_rel(f: str, ws: Path) -> str | None:
    if f.startswith("<"):            # <frozen importlib._bootstrap>, <string>, <stdin>
        return None
    p = Path(f)
    if not p.is_absolute():
        p = ws / p
    try:
        rp = p.resolve()
    except OSError:
        return None
    a, b = os.path.normcase(str(rp)), os.path.normcase(str(ws))
    if not a.startswith(b + os.sep):
        return None
    rel = Path(str(rp)[len(str(ws)) + 1:]).as_posix()
    return None if rel.split("/", 1)[0].startswith(".") else rel   # skip .sandbox_venv/.harness_venv


def parse_traceback(text: str, ws: Path) -> tuple[list[dict], str, str]:
    """Return (frames, exception_type, message). Handles native and pytest-style tracebacks."""
    hits = sorted([(m.start(), m["file"], int(m["line"]), m["func"].strip())
                   for rx in (NATIVE_FRAME, PYTEST_FRAME) for m in rx.finditer(text)]
                  + [(m.start(), m["file"], int(m["line"]), "<syntax>") for m in SYNTAX_LOC.finditer(text)])
    frames = []
    for _, f, ln, fn in hits:
        rel = _to_repo_rel(f, ws)
        frames.append({"file": rel or f, "line": ln, "func": fn, "in_repo": rel is not None})

    lines, exc_type, idx, first = text.splitlines(), "", None, ""
    for i, l in enumerate(lines):
        m = EXC_LINE.match(l.rstrip())
        if m and not l.lstrip().startswith("File "):
            exc_type, idx, first = m["type"], i, m["msg"] or ""
    msg_lines = [first]
    if idx is not None:
        for l in lines[idx + 1: idx + 25]:
            l = re.sub(r"^E\s{1,3}", "", l).rstrip()
            if not l or NATIVE_FRAME.match(l) or PYTEST_FRAME.match(l) or l.startswith(("---", "During handling")):
                break
            msg_lines.append(l)
    return frames, exc_type, "\n".join(msg_lines).strip()[:1500]


def _make_crash(test_id: str, kind: str, text: str, ws: Path) -> Crash:
    frames, exc_type, msg = parse_traceback(text, ws)
    repo_frames = [f for f in frames if f["in_repo"]]
    non_test = [f for f in repo_frames if not is_test_path(f["file"])]
    origin = (non_test or repo_frames or [None])[-1]   # deepest app frame, else deepest test frame
    if not msg.strip():   # bare `raise X` / unparsed output: keep something readable for later stages
        tail = [ln.strip() for ln in text.splitlines() if ln.strip()]
        msg = (tail[-1] if tail else exc_type or "unknown error")[:500]
    if exc_type.split(".")[-1] in NO_SYMBOL_EXC:   # the traceback echoes source code, not a v2 message
        syms = []
    else:
        syms = symbols_from_message(msg) or symbols_from_message(text)
    return Crash(test_id, kind, exc_type or "Unknown", msg,
                 origin["file"] if origin else None, origin["line"] if origin else None,
                 syms, [{k: f[k] for k in ("file", "line", "func")} for f in repo_frames])


# --------------------------------------------------------------------------- #
# Test run (harness-compatible) + import sweep
# --------------------------------------------------------------------------- #
def run_pytest(py: Path, ws: Path, test_files: list[Path], report_file: Path, *, timeout: float,
               strict_deprecations: bool, isolate_secrets: bool) -> tuple[int, str]:
    report_file.unlink(missing_ok=True)
    cmd = [
        str(py), "-B", "-m", "pytest", *[str(p) for p in test_files],
        "--json-report", f"--json-report-file={report_file}",
        "-q", "--disable-warnings", "--no-header",
        "-o", "python_files=test_*.py *_test.py tests.py test.py",
        "-o", "python_functions=test_* check_* it_*",
        "-o", "python_classes=Test* Describe*",
        "--rootdir", str(ws), "-p", "no:cacheprovider", "--import-mode=importlib",
        "--tb=native", "--continue-on-collection-errors",
    ]
    if isolate_secrets:
        cmd += NETWORK_FLAGS
    if strict_deprecations:
        cmd += ["-W", "error::pydantic.warnings.PydanticDeprecatedSince20"]
    return _run(cmd, ws, env=_test_env(ws, isolate_secrets), timeout=timeout)


def _report_cases(report_file: Path) -> list[tuple[str, str, str]]:
    """(id, kind, traceback text) for every failed collector / test in the json report."""
    try:
        data = json.loads(report_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    cases = [(c.get("nodeid") or "<collect>", "collection", c.get("longrepr") or "")
             for c in data.get("collectors", []) if c.get("outcome") == "failed"]
    for t in data.get("tests", []):
        if t.get("outcome") in ("failed", "error"):
            text = next(((t.get(ph) or {}).get("longrepr") for ph in ("setup", "call", "teardown")
                         if (t.get(ph) or {}).get("longrepr")), "")
            cases.append((t.get("nodeid", "?"), t["outcome"], text))
    return cases


_SWEEP_SCRIPT = r"""
import importlib, json, os, socket, sys, traceback, warnings
if os.environ.get("REFORGE_BLOCK_NET") == "1":   # same policy as pytest-socket --allow-hosts
    _connect = socket.socket.connect
    def _guard(self, addr):
        if isinstance(addr, tuple) and addr[0] not in ("127.0.0.1", "::1", "localhost"):
            raise ConnectionRefusedError("[reforge] network blocked: %s" % (addr[0],))
        return _connect(self, addr)
    socket.socket.connect = _guard
try:
    from pydantic.warnings import PydanticDeprecatedSince20
    warnings.filterwarnings("error", category=PydanticDeprecatedSince20)
except Exception:
    pass
for mod in json.load(sys.stdin):
    try:
        importlib.import_module(mod)
    except BaseException:
        print("%s%s" % (MARK, json.dumps({"module": mod, "tb": traceback.format_exc()})), flush=True)
"""


def run_import_sweep(py: Path, ws: Path, timeout: float, isolate_secrets: bool) -> list[Crash]:
    mods = [m for rel, m in discover_modules(ws).items()
            if not is_test_path(rel) and Path(rel).name not in ("setup.py", "__main__.py")
            and all(p.isidentifier() for p in m.split("."))]
    if not mods:
        return []
    script = _SWEEP_SCRIPT.replace("MARK", repr(SWEEP_MARK))
    env = _test_env(ws, isolate_secrets)
    env["REFORGE_BLOCK_NET"] = "1" if isolate_secrets else "0"
    _, out = _run([str(py), "-B", "-c", script], ws, env=env,
                  timeout=timeout, stdin=json.dumps(mods))
    crashes = []
    for line in out.splitlines():
        if line.startswith(SWEEP_MARK):
            try:
                rec = json.loads(line[len(SWEEP_MARK):])
            except json.JSONDecodeError:
                continue
            crashes.append(_make_crash(f"import:{rec['module']}", "import", rec["tb"], ws))
    return crashes


def _status(metrics: dict, collection_errors: list[str], code: int) -> str:
    parts = [f"{metrics.get(k, 0)} {label}" for k, label in (("failed", "failed"), ("passed", "passed"))
             if metrics.get(k) or (k == "passed" and (metrics.get("failed") or metrics.get("errors")))]
    errs = metrics.get("errors", 0) or 0
    if errs:
        parts.append(f"{errs} error{'s' if errs != 1 else ''}")
    if metrics.get("skipped"):
        parts.append(f"{metrics['skipped']} skipped")
    status = ", ".join(parts)
    if collection_errors:
        status = (status + "; " if status else "") + f"collection failed: {', '.join(collection_errors)}"
    if status:
        return status
    if code == 124:
        return "timeout"
    if code == 5:
        return "no tests collected"
    return f"pytest {PYTEST_EXIT.get(code, 'exited')} (exit {code})"


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def run_break(ws_path: str | Path, *, baseline: dict | None = None, target_version: str | None = None,
              python: str | None = None, timeout: float = 300, sweep_timeout: float = 120,
              strict_deprecations: bool = True, import_sweep: bool = True,
              isolate_secrets: bool = True) -> SandboxReport:
    ws = Path(ws_path).resolve()
    if not ws.is_dir():
        return SandboxReport(test_status="sandbox_error: workspace path does not exist", setup_ok=False)
    spec = f"pydantic=={target_version}" if target_version else DEFAULT_SPEC

    try:
        py, version, setup_log = build_env(ws, spec, python, timeout)
    except SandboxSetupError as e:
        return SandboxReport(test_status=f"sandbox_error: {e}", setup_ok=False, log_tail=str(e)[-2000:])

    crashes: list[Crash] = []
    metrics: dict[str, Any] = {}
    code, out = 0, ""
    test_files = _discover_test_files(ws)
    if test_files:
        report_file = ws / VENV_DIRNAME / "pytest_report_v2.json"
        code, out = run_pytest(py, ws, test_files, report_file, timeout=timeout,
                               strict_deprecations=strict_deprecations, isolate_secrets=isolate_secrets)
        metrics = _parse_report(report_file)
        crashes = [_make_crash(tid, kind, text, ws) for tid, kind, text in _report_cases(report_file)]
        if not crashes and code not in (0, 5):        # pytest itself blew up (usage/internal/timeout)
            crashes.append(_make_crash("<pytest>", "pytest", out, ws))

    if import_sweep:
        seen = {(c.file, c.line, c.exc_type) for c in crashes}
        for c in run_import_sweep(py, ws, sweep_timeout, isolate_secrets):
            if (c.file, c.line, c.exc_type) not in seen:
                seen.add((c.file, c.line, c.exc_type))
                crashes.append(c)

    collection_errors = sorted({c.test_id for c in crashes if c.kind == "collection"})
    newly_broken = compare_runs(baseline, metrics)["regressions"] if baseline and baseline.get("outcomes") else []
    sym_counts = Counter(s for c in crashes for s in c.symbols)
    file_counts = Counter(c.file for c in crashes if c.file)
    if test_files:
        status = _status(metrics, collection_errors, code)
    else:   # snippets / test-less repos: the import check is the only dynamic signal
        n = sum(c.kind == "import" for c in crashes)
        status = ("no test files; import check: " + (f"{n} module(s) crashed" if n else "passed")) if import_sweep \
            else "no test files discovered"
    blocked = (baseline or {}).get("collected", 0) - metrics.get("collected", 0)
    if collection_errors and blocked > 0:
        status += f" ({blocked} test{'s' if blocked != 1 else ''} blocked)"

    return SandboxReport(
        test_status=status,
        counts={k: metrics.get(k, 0) for k in ("collected", "passed", "failed", "errors", "skipped", "pass_rate")},
        breakage_symbols=[s for s, _ in sym_counts.most_common()],
        affected_files=[f for f, _ in file_counts.most_common()],
        crashes=crashes,
        outcomes=metrics.get("outcomes", {}),
        newly_broken=newly_broken,
        exit_code=code,
        pydantic_version=version,
        python=setup_log.rsplit("python ", 1)[-1] if "python " in setup_log else None,
        log_tail=(setup_log + "\n" + out)[-4000:],
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Stage 2: dynamic break run under Pydantic v2")
    ap.add_argument("ws_path")
    ap.add_argument("--target-version", help="exact pydantic v2 version, e.g. 2.12.3")
    ap.add_argument("--python", help="interpreter for uv, e.g. 3.12")
    ap.add_argument("--timeout", type=float, default=300)
    ap.add_argument("--no-strict", action="store_true", help="don't escalate v2 deprecation warnings")
    ap.add_argument("--no-import-sweep", action="store_true")
    ap.add_argument("--no-isolation", action="store_true", help="pass host env/secrets + network through (debug only)")
    a = ap.parse_args(argv)
    rep = run_break(a.ws_path, target_version=a.target_version, python=a.python, timeout=a.timeout,
                    strict_deprecations=not a.no_strict, import_sweep=not a.no_import_sweep,
                    isolate_secrets=not a.no_isolation)
    print(json.dumps(rep.to_dict(), indent=2))
    return 0


if __name__ == "__main__":   # run from the Reforge root: python -m src.backend.sandbox.sandbox workspace/<repo>
    raise SystemExit(main())
