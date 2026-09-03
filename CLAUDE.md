# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

FastAPI service that replaces an n8n polling workflow. On a timer it pulls recently-updated
Omni chat conversations, keeps the ones carrying a specific tag ("Lead qualificado"), and
creates a deal in RD Station CRM for each new tagged contact that doesn't already have an
active deal. State lives in SQLite so conversations are never processed twice.

The repo root **is** the deploy root (Docker / EasyPanel). There is deliberately no `app/`
package — every Python module sits directly at the top level and is imported flat
(`import config`, `import db`, `from poller import run_poll`).

## Commands

```bash
# Local dev (needs .env in repo root — copy from .env.example)
python -m venv .venv && .venv/Scripts/activate    # Windows
pip install -r requirements.txt
uvicorn app:app --reload --port 8000

# Docker (mount data/ so the SQLite watermark survives restarts)
# Container listens on port 80 (gunicorn); map it to a local port.
docker build -t lead-sync-service .
docker run --rm -p 8000:80 --env-file .env -v $(pwd)/data:/app/data lead-sync-service

# Trigger a poll run immediately instead of waiting for the scheduler
curl -X POST localhost:8000/poll/run
```

There is no test suite, linter, or CI configured. Manual verification is done by hitting
`POST /poll/run` and inspecting `GET /poll/status`, `GET /poll/runs`, `GET /poll/leads`,
and `logs/app.log`.

## Architecture

- **app.py** — FastAPI app. An APScheduler `AsyncIOScheduler` is started in the `lifespan`
  context and runs `run_poll` every `POLL_INTERVAL_MINUTES`. All work happens in-process;
  there is no separate worker. The HTTP endpoints are read-only views over the DB plus a
  manual `/poll/run` trigger.
- **The service must run as a single process / single worker.** The `Dockerfile` uses
  `gunicorn -w 1 -k uvicorn.workers.UvicornWorker`. More than one worker = more than one
  scheduler = duplicated polling. The `asyncio.Lock` in `poller.py` only guards within one
  process. Never scale by adding workers; run a second instance only if it has a different
  config and DB.
- **poller.py** — the whole business flow in `run_poll()`. It is wrapped in a broad
  `try/except` on purpose: any failure is logged and recorded in `poll_runs.error` but must
  not kill the scheduler.
- **omni_client.py / rdcrm_client.py** — thin `httpx` wrappers. No shared client; `run_poll`
  opens one `httpx.AsyncClient` and passes it down.
- **db.py** — raw `sqlite3`, no ORM. Three tables: `poll_state` (single-row watermark),
  `processed_chats` (dedup + per-lead outcome), `poll_runs` (per-execution stats).
- **config.py** — all configuration via env vars, loaded from a repo-root `.env` in dev and
  from the platform environment in production (`load_dotenv` silently no-ops when `.env` is
  absent). Required vars raise `KeyError` at import time if missing.

### Polling / dedup model (the important part)

- The Omni Chats API has **no `?label=` filter**, so the poller fetches *all* chats updated
  since the watermark and filters by tag ID in `chat_has_tag`.
- `poll_state.last_checked_at` is the watermark. Each run pages `GET /chats` with
  `updatedAt.gt=<watermark>` ordered ascending, advancing an in-memory cursor to the max
  `updatedAt` seen, stopping when a page has < 100 items or `MAX_PAGES_PER_RUN` is hit
  (backlog carries to the next run). The cursor is persisted only after paging finishes.
- First-ever run has no watermark and starts `INITIAL_LOOKBACK_HOURS` in the past.
- Per-conversation dedup is `processed_chats.chat_id` (the chat's `objectId`). Every tagged
  chat is written there exactly once with an `action`: `negociacao_criada`,
  `ja_tem_negociacao`, `sem_telefone`, or `erro` (RD CRM lookup/create failed). An `erro`
  row is not retried automatically — delete the row to reprocess.
- Per-lead failures are caught inside the loop so one bad lead neither aborts the run nor
  blocks the watermark. The watermark (`set_last_checked_at`) is only advanced after the
  whole lead loop finishes; a failure earlier in the run leaves it untouched and the window
  is re-fetched next run (dedup prevents double deals).
- `run_poll` is guarded by an `asyncio.Lock` — a manual `POST /poll/run` while a run is in
  progress returns `{"skipped": "ja em execucao"}` instead of racing it.
- **This means `data/` must be a persistent volume.** Losing `data/leadsync.db` resets the
  watermark and the dedup history — risk of reprocessing / duplicate deals.

### Deploy notes

- `.env` is git- and docker-ignored. In EasyPanel, set the vars from `.env.example` in the
  platform UI and mount a persistent volume at `/app/data` (and optionally `/app/logs`).
- Endpoints have no authentication — add protection before exposing beyond an internal network.

## Known open issues (see README.md "O que ainda falta")

- `has_active_deal` in `rdcrm_client.py` uses a provisional rule (deal with no `win` and no
  `closed_at`); the real business rule is unconfirmed.
- `create_deal` can create a **duplicate contact** in RD CRM when a phone already has a
  contact but no active deal — RD CRM's `POST /deals` is not upserting by phone.
