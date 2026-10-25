import shutil,subprocess
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, HTTPException, Form
from extract.normalise import normalize_github_url, normalize_pasted_code
from extract.scanner import scan_environment
from extract.harness import run_harness

WORKSPACE_ROOT = Path("workspace").resolve()
WORKSPACE_ROOT.mkdir(exist_ok=True)
app = FastAPI()
@app.post("/api/run-pipeline")
def run_pipeline(
    content: Optional[str] = Form(None),
    repo_url: Optional[str] = Form(None),
    code_snippet: Optional[str] = Form(None)):

    # Aggressively strip whitespace and stray literal quotes
    raw_input = (content or repo_url or code_snippet or "").strip().strip("\"'")
    if not raw_input:
        raise HTTPException(status_code=400, detail="Missing input content.")

    print(f"[DEBUG] Cleaned input received: {repr(raw_input)}")

    valid_url = normalize_github_url(raw_input)
    print(f"[DEBUG] Normalized URL result: {valid_url}")

    if valid_url:
        input_type = "github"
        repo_name = valid_url.rstrip("/").split("/")[-1].replace(".git", "")
        ws_path = WORKSPACE_ROOT / repo_name

        if ws_path.exists():
            shutil.rmtree(ws_path)
        ws_path.mkdir(parents=True, exist_ok=True)

        subprocess.run(["git", "clone", "--depth", "1", valid_url, str(ws_path)],check=True,capture_output=True,timeout=20)
    else:
        input_type = "text"
        ws_path = WORKSPACE_ROOT / "text"
        ws_path.mkdir(parents=True, exist_ok=True)
        (ws_path / "snippet.py").write_text(normalize_pasted_code(raw_input), encoding="utf-8")

    # Stage 1A: Scan Environment
    scan_result = scan_environment(ws_path, input_type=input_type)
    baseline_metrics = run_harness(ws_path)
    return {"scan": scan_result, "baseline": baseline_metrics}
