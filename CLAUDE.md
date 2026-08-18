# CLAUDE.md — Job Search HQ

Job Search HQ (`jshq`) is a local-first, zero-tracking job-search dashboard. It
tracks companies you choose, pulls their openings straight from applicant-tracking
job boards, and scores each posting against fit criteria you write. Scoring is
driven by a user-authored criteria document, not by hard-coded rules.

## Setup

```
python3.12 -m venv .venv
.venv/bin/pip install -e . --group dev
```

## Test

```
.venv/bin/pytest
```

Run from the repo root. The suite is offline and keyless — it never calls a
network service or needs an API key.

## Run

- `jshq` — serve the app (frontend + API, one process) on `127.0.0.1:5747`.
- `jshq refresh` — run one ATS ingestion pass. Point a scheduler at it twice a day.
- `jshq backup` — take one verified backup into `backups/` in the data directory.

## Stack and conventions

- Python 3.12, FastAPI, SQLite; a framework-free ES-module frontend (no build step).
- Two themes: dark `:root` and light `html[data-theme="light"]` in
  `src/jshq/frontend/css/tokens.css`. Every color change lands in **both** blocks.
  Color encodes state or urgency only.
- The criteria document is the single source of truth for scoring config. Behavior
  changes go in the criteria doc's machine blocks, not in code constants. The
  shipped example is `src/jshq/defaults/fit_criteria.md`; the live copy the app
  reads and writes is `DATA_DIR/fit_criteria.md` (seeded on first run).

## Invariants — never violate

- **Localhost only.** The server binds `127.0.0.1`. There is no auth and no
  account system; do not add one.
- **Graceful without an API key.** Every AI feature degrades (skips or returns an
  actionable message), never crashes. The whole test suite runs keyless.
- **No phone-home.** No telemetry, crash reporting, update checks, or CDN assets.
  The only permitted network calls are ATS job boards (robots.txt-respecting,
  honest User-Agent, twice-daily max), `api.anthropic.com` with the user's own
  key, and a per-company logo lookup. `PRIVACY.md` is the complete outbound
  inventory and must be updated with any new call.
- **`Cache-Control: no-cache` on all static/frontend responses.** The ES-module
  graph is un-hashed; heuristic caching half-updates it.
