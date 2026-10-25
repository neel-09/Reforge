import json, shutil, subprocess, sys
from pathlib import Path


def run_harness(ws_path: Path | str, timeout: int = 300) -> dict:
    """
    Creates an isolated venv inside the target workspace using `uv` (fast)
    with a stdlib `venv` fallback, installs its requirements.txt, runs pytest
    with a JSON report, and returns metrics.
    """
    ws_path = Path(ws_path).resolve()
    if not ws_path.is_dir():
        return _error("Workspace path does not exist.", ws_path)

    manifest = ws_path / "requirements.txt"
    if not manifest.is_file():
        return _error("requirements.txt missing. Run scanner first.", ws_path)

    venv_dir = ws_path / ".harness_venv"
    report_file = ws_path / ".pytest_report.json"
    is_win = sys.platform == "win32"
    py_bin = venv_dir / ("Scripts" if is_win else "bin") / ("python.exe" if is_win else "python")

    uv = shutil.which("uv")

    # 1. Create isolated venv (idempotent) — prefer uv
    if not py_bin.exists():
        try:
            if uv:
                subprocess.run([uv, "venv", str(venv_dir), "--seed"],
                               capture_output=True, text=True, timeout=60, check=True)
            else:
                import venv as _venv
                _venv.EnvBuilder(with_pip=True, clear=False).create(venv_dir)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            err = getattr(e, "stderr", "") or str(e)
            return _error(f"venv creation failed: {err}", ws_path)
        except Exception as e:
            return _error(f"venv creation failed: {e}", ws_path)

    if not py_bin.exists():
        return _error(f"venv python binary not found at {py_bin}", ws_path)

    # 2. Install dependencies + pytest tooling
    if uv:
        install_cmd = [uv, "pip", "install", "--python", str(py_bin),
                       "-r", str(manifest), "pytest", "pytest-json-report"]
    else:
        install_cmd = [str(py_bin), "-m", "pip", "install", "--quiet",
                       "--disable-pip-version-check",
                       "-r", str(manifest), "pytest", "pytest-json-report"]
    try:
        install = subprocess.run(install_cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return _error("Dependency install timed out.", ws_path)

    if install.returncode != 0:
        return _error(f"pip install failed:\n{install.stderr.strip()[-800:]}", ws_path)

    # 3. Pre-discover test files (avoids pytest crawling into venv/caches)
    test_files = _discover_test_files(ws_path)
    if not test_files:
        return _error("No test files discovered in workspace.", ws_path) | {
            "discovered_candidates": [],
        }

    pytest_cmd = [
        str(py_bin), "-B", "-m", "pytest",
        *[str(p) for p in test_files],
        "--json-report", f"--json-report-file={report_file}",
        "-q", "--disable-warnings", "--no-header",
        "-o", "python_files=test_*.py *_test.py tests.py test.py",
        "-o", "python_functions=test_* check_* it_*",
        "-o", "python_classes=Test* Describe*",
        "--rootdir", str(ws_path),
        "-p", "no:cacheprovider",
        "--import-mode=importlib",
        "--full-trace",
    ]
    env = {**__import__("os").environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        proc = subprocess.run(pytest_cmd, capture_output=True, text=True,
                              timeout=timeout, cwd=ws_path, env=env)
    except subprocess.TimeoutExpired:
        return _error("Test execution timed out.", ws_path)

    exit_code = proc.returncode
    metrics = _parse_report(report_file)

    return {
        "status": "ok" if exit_code in (0, 1, 5) else "error",
        "exit_code": exit_code,
        "workspace": str(ws_path),
        "runner": "uv" if uv else "venv",
        "stdout_tail": proc.stdout.strip()[-500:],
        "stderr_tail": proc.stderr.strip()[-500:],
        "test_files": [str(p.relative_to(ws_path)) for p in test_files],
        **metrics,
    }


def _discover_test_files(ws_path: Path) -> list[Path]:
    """Find python files that look like tests, skipping venvs and caches."""
    skip_parts = {".harness_venv", ".venv", "venv", ".git", "__pycache__",
                  "node_modules", ".pytest_cache", "site-packages", ".tox"}
    found = []
    for p in ws_path.rglob("*.py"):
        if any(part in skip_parts for part in p.parts):
            continue
        name = p.name.lower()
        if (name.startswith("test") or name.endswith("_test.py")
                or name == "tests.py" or "spec" in name):
            found.append(p)
    return found


def _parse_report(report_file: Path) -> dict:
    if not report_file.is_file():
        return {
            "collected": 0, "passed": 0, "failed": 0, "errors": 0,
            "skipped": 0, "xfailed": 0, "xpassed": 0, "duration": 0.0,
            "pass_rate": 0.0, "failures": [], "report_available": False,
        }

    try:
        data = json.loads(report_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"report_available": False, "collected": 0, "passed": 0, "failed": 0}

    summary = data.get("summary", {})
    collected = summary.get("collected", 0) or summary.get("total", 0)
    passed = summary.get("passed", 0)
    failed = summary.get("failed", 0)
    errors = summary.get("error", 0)

    failures = [
        {
            "nodeid": t.get("nodeid"),
            "outcome": t.get("outcome"),
            "message": (t.get("call", {}).get("longrepr") or "")[:600],
        }
        for t in data.get("tests", [])
        if t.get("outcome") in ("failed", "error")
    ]

    pass_rate = (passed / collected * 100) if collected else 0.0

    return {
        "collected": collected,
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "skipped": summary.get("skipped", 0),
        "xfailed": summary.get("xfailed", 0),
        "xpassed": summary.get("xpassed", 0),
        "duration": round(data.get("duration", 0.0), 3),
        "pass_rate": round(pass_rate, 2),
        "failures": failures,
        "report_available": True,
    }


def _error(msg: str, ws_path: Path) -> dict:
    return {
        "status": "error",
        "message": msg,
        "workspace": str(ws_path),
        "collected": 0, "passed": 0, "failed": 0, "errors": 0,
        "pass_rate": 0.0, "failures": [],
    }