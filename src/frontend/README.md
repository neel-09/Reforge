# Reforge — web app

Zero-build frontend (HTML/CSS/JS). FastAPI serves it from the same origin as the API, so there is one process to deploy.

## Run

From the Reforge root (with `nebius_token_factory` in `.env`):

```bash
uvicorn src.backend.endpoints.web:app --host 0.0.0.0 --port 8000 \
  --reload --reload-dir src/backend/endpoints --reload-dir src/backend/extract \
  --reload-dir src/backend/sandbox --reload-dir src/backend/planner \
  --reload-dir src/backend/route_patch --reload-dir src/backend/audit --reload-dir src/frontend
```

Open http://localhost:8000. `/docs` and `POST /api/run-pipeline` still work as before.

## Files

| File | Purpose |
| --- | --- |
| `index.html` | the three views (landing → execution trace → results), FAQ drawer, GitHub picker, file viewer |
| `styles.css` | design system: black, ember accent, Instrument Serif + Newsreader, JetBrains Mono for code |
| `app.js` | composer, NDJSON stream reader, trace animation, results chat + outputs panel, demo run |
| `config.js` | `API_BASE` (blank = same origin) and `DEMO` (`auto` / `on` / `off`) |
| `assets/reforge-overview.pdf` | the **About** PDF |
| `assets/demo-report.pdf` | report shown in demo mode |

## Backend glue — `src/backend/endpoints/web.py`

Imports the existing `app` from `endpoint.py` (left unchanged) and adds:

- `POST /api/run-stream` — same form fields as `/api/run-pipeline` (`content`, `code_file`, …); streams NDJSON events per stage (`stage` running/done with facts, `log` lines from the backend's stdout, `result`, `error`). Only one run at a time (409 otherwise).
- `GET /api/report/{name}/view` — the report PDF inline, for the in-page viewer.
- `GET /api/patch/{name}` — the combined `reforge.patch`.
- `GET /api/health` — the UI falls back to a scripted **demo mode** when this fails (or with `?demo=1`).
- Static mount of `src/frontend` at `/`.

## Hosting the UI separately (optional)

Set `API_BASE` in `config.js` to the backend URL and start the backend with `REFORGE_CORS=https://your-ui.example.com`.
The streaming endpoint sends `X-Accel-Buffering: no`; behind nginx also set `proxy_buffering off` for `/api/run-stream`.

## Daily limits (deployment)

`src/backend/endpoints/limits.py` guards `/api/run-stream` and `/api/run-pipeline`. Counters reset at 00:00 UTC.

| Env var | Default | Meaning |
| --- | --- | --- |
| `REFORGE_JUDGE_KEYS` | — | comma-separated judge keys (share only in the private Devpost testing instructions) |
| `REFORGE_PUBLIC_RUNS_PER_DAY` | `3` | live runs per visitor (IP) per day |
| `REFORGE_PUBLIC_DAILY_USD` | `0.75` | Token Factory spend per day, all public visitors combined |
| `REFORGE_JUDGE_DAILY_USD` | `2.00` | spend per day for judge-key runs |
| `REFORGE_DEMO_VIDEO_URL` | — | linked from the limit pop-up (fallback: `DEMO_VIDEO_URL` in `config.js`) |
| `REFORGE_USAGE_FILE` | `src/backend/data/usage.json` | put on a persistent volume so restarts don't reset counters |
| `REFORGE_TRUST_PROXY` | `1` | client IP = right-most `X-Forwarded-For` entry |

Spend is read from `data/telemetry.jsonl`. A single run can overshoot a cap by at most one run's cost; Stage 4's budget is clamped to what's left.
