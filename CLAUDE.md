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
- **db.py** — raw `sqlite3`, no ORM. When `SUPABASE_URL` + a key (`SUPABASE_SERVICE_ROLE_KEY`/`SUPABASE_KEY`/...) are set, every function is replaced by `db_supabase.py` (same names, PostgREST via `httpx`, tables `omnichat_*`: poll_state, processed_chats, poll_runs existed since 2026-10-17 (that Supabase version was never pushed to GitHub — a deploy of main on 2026-10-20 silently fell back to SQLite); cadence tables are in `supabase_schema.sql`). Production uses Supabase because EasyPanel volumes were not kept; SQLite is for local dev. Three tables: `poll_state` (single-row watermark),
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
- **This means the DB must persist** (Supabase in production; with SQLite, `data/` must be a persistent volume). Losing `data/leadsync.db` resets the
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
- New deal: name = lead name, phone, email, organization (found by
  name/CNPJ or created with Documento Fiscal digits). **No `deal_source`** — see below.
- **`whatsapp: true` on the phone makes `POST /deals` return 500** (HTML page). Reproduced
  2026-09-28 with a test deal: same body without the flag → 200 (owner Sabrina + email worked).
  Setting the flag afterwards via `PUT /contacts/{id}` is untested. The phone is sent without it.
- `create_deal` retries on any error in stages: full → without email/org → minimal body with the
  default owner. Each rejection is logged as `POST /deals (body '...') recusado`.
- Inactive RD users (`active: false`) are ignored when matching the owner — RD has
  duplicate names where the first entry is an inactive old account.
- `create_organization` body works: first org created 2026-09-30 ("59.208.128 LUISA DE PAULA
  SILVA MELLO RIBEIRO", Documento Fiscal digits filled, linked to deal "Jessyka de Paula").
  One earlier 422 ("JAIRO WALDOW - ME", 2026-09-28) was case-specific; cause unknown (logged
  before `_raise_for_status` existed).
- **Annotation text is mangled by RD**: `POST /activities` (v1) stores any non-ASCII char as
  UTF-8-read-as-Latin-1 ("Olá" → "OlÃ¡", emojis → garbage), regardless of how the body is sent
  (plain JSON, ASCII-escaped JSON, charset header, form-encoded — all tested 2026-09-30). Notes
  typed in the RD UI read back fine. Deal/contact names via `POST /deals` are fine.
- The RD itself flags new contact phones as WhatsApp; `deal_source` stays empty (team fills it).

### Field extraction from the conversation (extractor.py, added 2026-10-01)

The client wants company / CNPJ-CPF / email taken from the chat text when the Omni customer
record is empty (it usually is — of 18 cards on 2026-09-30 only 1 had businessName+CNPJ, 6 had
only a CPF). `poller._lead_data` merges: Omni record first, then `extractor.extract_lead_fields`
(OpenAI chat completions, `json_schema` structured output; model pinned to `gpt-4.1-mini` in config.py on purpose, not env-configurable) fills only empty fields. Tax docs
must pass CPF/CNPJ check digits, emails a format check, or they are dropped. Valid CPF/CNPJ but no
company name → company = full name from the conversation, else the Omni customer full name
(`omni_client.chat_customer_full_name`, strips numbers typed in the last name) — the sales team
does this even for CNPJ (e.g. "Kauã Rodrigues de Oliveira" + CNPJ). Eval on 2026-10-01 (44
real chats, history read back from RD, nothing written): tax doc matched the team's manual entry
in 22, differed in 1 (CPF vs the company CNPJ), found a valid doc in 11 still-empty cards. The
model sometimes returns labels/descriptions or the string "null" as the company — filtered by
`_EMPTY_VALUES` / `_COMPANY_LABELS`; single first names are dropped as `nome_completo`. Disabled when
`OPENAI_API_KEY` is empty; any extractor exception is logged and the card is created anyway.
The Omni customer record also has `salesPerson` (seller, with email) — unused so far, a
possible better owner source.

### WhatsApp cadence (cadence.py, added 2026-11-03)

The RD automation (set up by the client, live since ~2026-10-20) moves deals in "3.Comercial
Brasil" through stages T1º..T5º CONTATO. T1/T3/T4 are WhatsApp steps: `cadence.run_cadence()`
runs after each poll and, for every open deal (no win/closed_at/hold) in those stages, fires the
Omni campaign mapped in `CADENCE_STAGE_CAMPAIGNS` (stage_id:campaign_id) for the deal's phone.
- Campaigns "Cadencia RD T1/T3/T4 (automatica - nao apagar)" (old ids 1eAsZ7CFZ9 / j1qTmYhgS1 /
  3eXJsE119w) were created via the campaign API on 2026-11-04 with templates crEu3wTP9P /
  u1mUaPmSA1 / kYi0wpCK41, exit platform `AaLZ6QXSrcVH` (number 55 54 3022-5800, same one the
  "IS - ..." campaigns use), the header image as a fixed token and `consultor` as the T4 `{{1}}`.
  Their audience (c4GbsZpmcO) only holds Jorge's test number — never "start" these campaigns.
- **A campaign is bound to the Omni user of the API key that created it.** After the key swap
  (2026-11-08) the 2026-11-04 campaigns still accepted `/test` (200 + dispatch id) but every
  dispatch ended `OUTGOING_ERROR` / `CHANNEL_USER_NOT_FOUND` ("Object 6w30PhhUYQcA not found").
  Recreated with the new key as "Cadencia RD T1/T3/T4 v2" (hZ24VxW1wS / KPG4dpM3q3 / kKI1Z6sq3E,
  createdBy KVi0LlXUSFq2): `POST /campaigns {name,type:"audience-based",audience}` then
  `POST /campaigns/{id}/channels {platform,template:{id},tokens,forceSend:true}`. T1/T3 test
  → `OUTGOING_SENT`; T4 test got error 1004 (Meta usage/engagement policy — 3 marketing messages
  to the same number within a minute), retest later. **Swapping the key again = recreate them.**
- `dispatch_campaign` now polls `GET /dispatches/{id}` (~16s) after `/test`: `OUTGOING_ERROR` →
  `DispatchError` (deal marked `erro`), except `CHANNEL_USER_NOT_FOUND` → `OmniAuthError` (row
  deleted, run stops, retried later). Still pending after the wait → counted as sent.
- Sending = `POST campaign-api.omni.chat/v1/campaigns/{id}/test` with
  `{"contacts":[{"fullNumber":..., "consultor":...}]}`. Tested and delivered. `/dispatch` returns
  an id but sends nothing on a never-started campaign ("Dispatch not found"). The old
  `POST api.omni.chat/v1/messages` returns an opaque 400 for these templates (image header /
  buttons), with or without `integrationId` — don't use it.
- Dedup in `cadence_sends(deal_id, stage_id)`; the row is written as `enviando` before sending so
  a DB failure can never cause a double send. First run per stage records deals already there as
  `linha_de_base` without sending. Off unless `CADENCE_WHATSAPP_ENABLED=true`; capped by
  `CADENCE_MAX_SENDS_PER_RUN`. `POST /cadence/test?step=T1&phone=..` sends one message;
  `GET /cadence/sends` lists outcomes.
- Phones come from `deal.contacts` in `GET /deals` — **never `GET /contacts?deal_id=`: RD ignores
  that filter and returns unrelated contacts.** Only BR mobiles (55+DDD+9xxxxxxxx) are sent.
- EasyPanel replaces any 502 response with its "Service is not reachable" page — endpoints must
  not return 502 for app errors.
- **Revoked Omni key** (seen 2026-11-04 ~12:50 UTC on): `api.omni.chat` answers HTTP 200 with a text
  body `400 - {"code":209,"error":"Invalid session token"}`; campaign/whatsapp APIs answer 403
  "explicit deny". `omni_client._json` turns this into `OmniAuthError` (it used to surface as
  `Extra data: line 1 column 5`). In the cadence an auth refusal deletes the `enviando` row
  (nothing was sent) and stops the run, so the deal is retried once the key works again.
- **Wallet ("carteira") routing (added 2026-11-09, NOT yet verified in production):** the
  customer's reply to a cadence message used to go to the number's default bot ("Mobi" / "[PT] Bot
  LATAM – SALES E MKT") and be distributed at random; the client (Fran, audios 04 and 09/11) wants
  it with the RD deal owner. After each successful dispatch, `cadence.route_to_owner` takes the
  chat id from `GET /dispatches/{id}` (`chat.id`) and calls `POST api.omni.chat/v1/chats/{id}/route`
  `{"type":"USER","userId":...}` (documented in `api-docs.omni.chat/swagger-omnichat-api.yaml`).
  Owner → Omni user: same email (`GET /users?email=` is case-sensitive; we load all users, paged by
  `createdAt.gt`, skipping `disabled` and bot emails), else same name (first+last, unique). On
  2026-11-09, 12 of the 17 owners in T1/T3/T4 matched (11 by email, Aline Aldrighi by name); the
  other 5 (Pedro Venancio, Sandro Breda, Ricardo Glaeser, Mayara Santos, Anderson Cardoso) have
  no active Omni user, so their chats stay with the bot. Routing failures never fail the send;
  the outcome is appended to `cadence_sends.detail` ("<dispatch_id> | carteira: <name or reason>").
  Off with `CADENCE_ROUTE_TO_OWNER=false`. Test: `POST /cadence/test?...&owner_email=<omni email>`.
  Still to confirm: that the bot does not take the chat back when the customer replies.
- T1→T2 movement and the T2 call task are the client's RD automation, not this service. Clients
  reported (2026-11-05..09) deals not moving T1→T2 and T2 tasks not being created; the client's
  rule (Fran, 09/11): task created **on entering** T2, the seller has 3 days.
- 2026-11-09: production was still on the pre-`f32c6e4` code (old campaign 1eAsZ7CFZ9), so 22
  T1 rows that day are `enviado` but every dispatch was `CHANNEL_USER_NOT_FOUND` — nothing
  reached the customers. Delete those rows after deploying so they get T1.
- Deals already in T1/T3/T4 at activation are `linha_de_base` (193 T1, 49 T3, 76 T4) and never
  get the message; moving a deal out and back into the same stage does not resend either.

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
