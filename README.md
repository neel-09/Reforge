# Reforge

**Legacy code, re-forged.** Reforge is an autonomous code-migration and vulnerability-remediation agent. Give it a Python repository or a snippet and it upgrades the code across a breaking dependency release, patches known CVEs, verifies every change in an isolated sandbox, and proves nothing broke — with an audit report to back it up.

First migration target: **Pydantic v1 → v2**. The pipeline itself is library-agnostic.

- **Live demo:** `<deployed URL>`
- **Demo video:** `<YouTube URL>`
- **Track:** Coding and Agentic Engineering

---

## How it works

| # | Stage | What happens | Code |
|---|---|---|---|
| 1 | **Scan & baseline** | Detect the pinned dependency, look up CVEs on OSV, run the repo's tests under v1 for a baseline | `extract/scanner.py`, `extract/harness.py` |
| 2 | **Break & slice** | Install v2 in an isolated sandbox venv, record what breaks, AST-scan the code and order files schema → callers | `sandbox/sandbox.py`, `sandbox/slicer.py` |
| 3 | **Ground & plan** | Ground every breaking symbol in the official Pydantic migration docs (Tavily), then one **Nemotron 3 Ultra** call writes a validated, ordered refactor plan | `planner/grounder.py`, `planner/planner.py` |
| 4 | **Patch & self-heal** | **Nemotron 3.5 Lightning** applies SEARCH/REPLACE patches per file; misses escalate to **Super**, then **Ultra**; failing tests go through a Super → Ultra heal ladder | `route_patch/switchyard.py`, `route_patch/patcher.py` |
| 5 | **Prove parity** | Compare original vs refactored trees — test outcomes, return values, model schemas, entrypoint output — re-check CVEs, and compile the audit PDF | `audit/verifier.py`, `audit/report.py` |

Pasted snippets become their own test: Reforge records what the code prints under v1 and requires the migrated code to print exactly the same under v2.

**Outputs:** refactored `.py` files (view, diff, download), a git-apply-ready `reforge.patch`, and an audit PDF (vulnerabilities resolved, documentation each change was grounded in, v1-vs-v2 performance, AI cost ledger, side-by-side diffs).

## NVIDIA Nemotron models

Reforge routes work by difficulty across three NVIDIA open models instead of sending everything to the biggest one:

| Tier | Model (Token Factory id) | Role |
|---|---|---|
| Ultra | `nvidia/Nemotron-3-Ultra-550b-a55b` (550B MoE) | Writes the refactor plan once per run; last-resort patching and healing |
| Super | `nvidia/nemotron-3-super-120b-a12b` (120B MoE) | Escalation when Lightning misses a step; diagnoses and heals failing tests |
| Lightning | `nvidia/Nemotron-3_5-Lightning` (30B MoE) | Primary patcher — one call per file, carrying all of that file's steps |

Model replies are validated, never trusted: plan steps on unknown files or test files are dropped, every breaking finding must be covered, patches must match exactly once and pass an AST syntax gate, and a heal that makes things worse is rolled back.

## How Nebius Token Factory helped

- **All three tiers behind one OpenAI-compatible endpoint** — switching between Lightning, Super and Ultra is just a model id, which is what makes the cheap-first router practical.
- **Cost transparency:** every dispatch is priced with Token Factory rates and logged to a cost ledger against an all-Ultra counterfactual.
- **Reference run:** a full migration cost **$0.12 vs $0.32** on Ultra alone (**62% cheaper**); on its own, the patch stage saved **88.5%** ($0.0081 vs $0.0708).
- Plan and patch reply caches make identical reruns cost $0.

## Run locally

Requirements: Python 3.12+, git, and optionally [uv](https://github.com/astral-sh/uv) (faster sandbox venvs).

```bash
git clone <this repo> && cd Reforge
pip install -r requirements.txt
```

Create `.env` in the repo root:

```
nebius_token_factory=<your Nebius Token Factory API key>
tavily_key=<your Tavily API key>        # optional: falls back to the built-in docs knowledge base
```

Start the web app:

```bash
uvicorn src.backend.endpoints.web:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000. Paste a GitHub URL or Pydantic v1 code, attach a `.py` file, or use **Connect GitHub**. Without a reachable backend the UI plays a scripted demo run (`?demo=1` forces it).

API: `POST /api/run-stream` (NDJSON stage events, used by the UI) and `POST /api/run-pipeline` (single JSON result). See `src/frontend/README.md` for details.

## Deploy (Docker / Google Cloud Run)

```bash
gcloud run deploy reforge --source . --region us-central1 --allow-unauthenticated \
  --cpu 2 --memory 4Gi --timeout 3600 --max-instances 1 --execution-environment gen2 \
  --add-volume name=data,type=cloud-storage,bucket=<bucket> \
  --add-volume-mount volume=data,mount-path=/data \
  --set-env-vars nebius_token_factory=<key>,tavily_key=<key>,REFORGE_JUDGE_KEYS=<key>
```

Keep a single instance and a single worker: runs share one workspace and the usage counters.

**Usage limits** (so the hosted demo stays up through judging): 3 live runs per visitor per day, plus daily spend caps for public and judge runs. Configure with `REFORGE_PUBLIC_RUNS_PER_DAY`, `REFORGE_PUBLIC_DAILY_USD`, `REFORGE_JUDGE_DAILY_USD`, `REFORGE_JUDGE_KEYS`, `REFORGE_DEMO_VIDEO_URL`, `REFORGE_USAGE_FILE`.

## Privacy

Workspaces, audit output and LLM reply caches are deleted when the server starts and shuts down. Tests run in throw-away virtual environments with network access blocked. The telemetry file stores token counts only — never source code.

## Limitations & roadmap

- One migration today (Pydantic v1 → v2); SQLAlchemy 2, NumPy 2 and others need their own detection rules and docs.
- Python only; dependencies read from `requirements.txt`.
- Public GitHub repositories only; no pull requests yet (it returns files, a patch and a report).
- Proof is only as strong as the repo's tests.
- One run at a time per server.

Reforge is AI-based and can make mistakes — review every change before you use it.

## License

[MIT](LICENSE) © 2026 Abhroneel Chakraborty
