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
- After a deal is created (or matched to an existing active one), `_anexar_anotacoes_omni`
  posts **two annotations** to that deal via `POST /activities`: (1) the direct link to the
  Omni conversation (`OMNI_CHAT_URL_TEMPLATE`), (2) the conversation history pulled from
  `GET /chats/{id}/messages` (system/routing/summary messages filtered out, capped at
  `RD_CRM_ANNOTATION_MAX_CHARS`, oldest lines dropped first). Annotation failures are logged
  and swallowed — they never mark the lead `erro`, since the deal already exists.
  `RD_CRM_USER_ID` is the author. `OMNI_CHAT_URL_TEMPLATE` default
  (`https://app.omni.chat/#/home/chat/{chat_id}`) was confirmed against the live panel.
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

### Card rules (client doc "Dados obrigatórios para criar um card.docx", 2026-09-26)

- Owner: RD user matching the Omni attendant — `chat.user` (usually null) then the most recent
  operator name in the messages (`rdcrm_client.match_user_by_name`: exact, else first+last name,
  unique). Fallback `RD_CRM_USER_ID`. Annotations are authored by the owner.
- Existing deal = open deal in `RD_CRM_DEAL_PIPELINE_ID` ("3.Comercial Brasil") linked to a
  contact with the same phone (DDD + last 8 digits), or to the same organization with the same
  owner. RD's `/organizations?q=` does NOT search by CNPJ, only by name; many orgs have the
  Documento Fiscal field empty.
- New deal: name = lead name, phone flagged WhatsApp, email, organization (found by
  name/CNPJ or created with Documento Fiscal digits). **No `deal_source`** — see below.
- If RD rejects the full `POST /deals` body (4xx), `create_deal` retries once with the
  minimal body that worked in prod until 2026-09-25 (default owner, name + phone only).
- Inactive RD users (`active: false`) are ignored when matching the owner — RD has
  duplicate names where the first entry is an inactive old account.
- `create_organization` returns **422** (seen 2026-09-28, "JAIRO WALDOW - ME"); body shape
  still wrong. The error body is now logged via `_raise_for_status` — read it and fix.
- NOT yet verified live: the `whatsapp: true` phone flag.

## Known open issues (see README.md "O que ainda falta")

- "Active" deal = no `win` and no `closed_at` (provisional rule).
- `create_deal` can create a **duplicate contact** in RD CRM when a phone already has a
  contact but no active deal — RD CRM's `POST /deals` is not upserting by phone.

## Deploy in production

Live at `https://jorge-omnichat.qbguwf.easypanel.host/` (EasyPanel, deployed from this repo's
`main` branch). Endpoints have no auth, so `GET /poll/leads?limit=N` / `GET /poll/runs` /
`GET /health` are reachable directly for debugging.

### `deal_source` in `POST /deals` → bare 404 (seen 2026-09-09 and again 2026-09-28)

2026-09-28: commit d44e028 re-added `deal_source: {_id: 608b18cdf59636001b59280e}`
("Marketing - Whatsapp Omni"). That ID **exists** (`GET /deal_sources/{id}` → 200), yet every
`POST /deals` returned a bare 404 and 7 leads were marked `erro`. Deals created without
`deal_source` (old body) worked through 2026-09-25. So the 2026-09-09 "dead ID" diagnosis below
was probably wrong: RD rejects `deal_source` in this shape regardless of the ID. Removed again
(and `RD_CRM_DEAL_SOURCE_ID` removed from config). To get the "origem" back, test another shape
(e.g. `deal.deal_source_id`) on one throwaway deal first.

Leads stuck as `erro` on 2026-09-28 (delete their `processed_chats` rows to reprocess):
tesMIp4lCVtr, PC1rFm32d6Is, sA4vFlEAAL3i, 0wsN8n4uIsgl, wiSfDKgy57aX, 6lrFQVrN7MEp, s2OFmkhU67UV.

The production DB only had rows from 2026-09-28 on that day — check that `/app/data` is really a
persistent volume (`docker inspect <container> --format '{{json .Mounts}}'`).

### Earlier note: bug found 2026-09-09 — `RD_CRM_DEAL_SOURCE_ID` was a dead ID

The `deal_source` object `create_deal` used to send in `POST /deals` referenced
`RD_CRM_DEAL_SOURCE_ID=6a8d0f8b47ba12002b63035b` (from `.env` / EasyPanel env vars). That ID
**did not exist** in the account's `deal_sources` (confirmed by paging through all 181 via
`GET /deal_sources?token=...&page=N&limit=20` — not present in any page). RD CRM's API returned
a bare `404 Not Found` on `POST /deals` when this happened (no error body), which surfaced in
`logs/app.log` as an `httpx.HTTPStatusError` and marked the lead `action: "erro"` in
`processed_chats` (see poller.py's per-lead try/except).

Verified NOT the cause: `RD_CRM_USER_ID` (active user, "Francieli Mignoni") and
`RD_CRM_DEAL_STAGE_ID_LEAD` (stage "LEAD" in pipeline "3.Comercial Brasil") both still exist.

**Fix applied (option 1):** `deal_source` was dropped from `create_deal`'s body entirely and
`RD_CRM_DEAL_SOURCE_ID` removed from `config.py` / `.env` / `.env.example`. New deals no longer
carry an "origem" tag. If the "origem" is wanted later, create a real `deal_source` in RD CRM
and re-add the block.

**Leads still stuck as `erro` from this bug (not yet reprocessed)** — found via
`GET /poll/leads?limit=10` on 2026-09-09:
- OCM PLANEJADOS — 5519994827313
- Prof. Fabio Gama — 5571988573944
- Rosy Paulo Duda Pedro — 5551991470722
- Vitor Luis — 5519996314800

`erro` rows are not retried automatically (per the dedup model above) — now that the source ID
is fixed, delete these rows from `processed_chats` in `data/leadsync.db` (in production, the
EasyPanel `/app/data` volume) or manually create the deals in RD CRM so they get picked up
again.

### Stale duplicate — ignore `G:\Meu Drive\repositorios\cliente sampa\lead-sync-service`

An earlier copy of this service was built and tested in that Google Drive folder before this
repo existed. It has since drifted and is **not** the source of truth: e.g. its
`requirements.txt` got overwritten with unrelated deps (`supabase`, `redis`, `bcrypt`, `zeep`,
etc. from a different project) and its `poller.py`/`Dockerfile` are missing the fixes already
in this repo (per-lead error isolation, `asyncio.Lock`, gunicorn single-worker, port 80). Don't
pull code from there — this repo (`omni_chat`) is ahead and is what's actually deployed.
