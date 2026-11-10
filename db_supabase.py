"""Mesmas funcoes de db.py, gravando no Supabase (API REST/PostgREST) em vez
do SQLite. Ativado quando SUPABASE_URL e a chave estao no ambiente - ver
db.py. Tabelas: supabase_schema.sql (prefixo omnichat_)."""

import logging
from typing import Optional

import httpx

import config

logger = logging.getLogger("leadsync.db")

_BASE = f"{config.SUPABASE_URL.rstrip('/')}/rest/v1"
_HEADERS = {
    "apikey": config.SUPABASE_KEY,
    "Authorization": f"Bearer {config.SUPABASE_KEY}",
    "Content-Type": "application/json",
}
# O PostgREST devolve no maximo 1000 linhas por request.
_PAGE = 1000

_client = httpx.Client(headers=_HEADERS, timeout=30.0)


def _check(resp: httpx.Response) -> httpx.Response:
    if resp.is_error:
        raise httpx.HTTPStatusError(
            f"Supabase {resp.status_code} em {resp.request.method} {resp.url.path}: {resp.text[:300]}",
            request=resp.request,
            response=resp,
        )
    return resp


def _get(table: str, **params) -> list[dict]:
    return _check(_client.get(f"{_BASE}/{table}", params=params)).json()


def _upsert(table: str, row: dict, on_conflict: str, ignore: bool = False) -> None:
    prefer = "resolution=ignore-duplicates" if ignore else "resolution=merge-duplicates"
    _check(
        _client.post(
            f"{_BASE}/{table}",
            params={"on_conflict": on_conflict},
            json=row,
            headers={"Prefer": f"{prefer},return=minimal"},
        )
    )


def init_db() -> None:
    """Nao cria tabela (a API REST nao faz DDL) - so confere que existem.
    Falta das tabelas da cadencia so loga: o polling de leads segue, e a
    cadencia falha sozinha (sem enviar nada) ate rodarem o supabase_schema.sql."""
    for table in ("omnichat_poll_state", "omnichat_processed_chats", "omnichat_poll_runs"):
        _get(table, select="*", limit=1)
    for table in ("omnichat_cadence_sends", "omnichat_cadence_stages_seeded"):
        try:
            _get(table, select="*", limit=1)
        except httpx.HTTPError:
            logger.error("Tabela %s nao existe no Supabase - rode supabase_schema.sql (cadencia nao envia ate la).", table)
    logger.info("Persistencia: Supabase (%s)", config.SUPABASE_URL)


def get_last_checked_at() -> Optional[str]:
    rows = _get("omnichat_poll_state", select="last_checked_at", id="eq.1")
    return rows[0]["last_checked_at"] if rows else None


def set_last_checked_at(value: str) -> None:
    _upsert("omnichat_poll_state", {"id": 1, "last_checked_at": value}, "id")


def is_chat_processed(chat_id: str) -> bool:
    return bool(_get("omnichat_processed_chats", select="chat_id", chat_id=f"eq.{chat_id}"))


def mark_chat_processed(
    chat_id: str,
    phone: Optional[str],
    name: Optional[str],
    action: str,
    rd_crm_deal_id: Optional[str],
    processed_at: str,
) -> None:
    _upsert(
        "omnichat_processed_chats",
        {
            "chat_id": chat_id,
            "phone": phone,
            "name": name,
            "action": action,
            "rd_crm_deal_id": rd_crm_deal_id,
            "processed_at": processed_at,
        },
        "chat_id",
    )


def cadence_done_deal_ids(stage_id: str) -> set[str]:
    ids: set[str] = set()
    offset = 0
    while True:
        rows = _get(
            "omnichat_cadence_sends",
            select="deal_id",
            stage_id=f"eq.{stage_id}",
            order="deal_id",
            limit=_PAGE,
            offset=offset,
        )
        ids.update(r["deal_id"] for r in rows)
        if len(rows) < _PAGE:
            return ids
        offset += _PAGE


def mark_cadence(
    deal_id: str,
    stage_id: str,
    deal_name: Optional[str],
    phone: Optional[str],
    action: str,
    detail: Optional[str],
    created_at: str,
) -> None:
    _upsert(
        "omnichat_cadence_sends",
        {
            "deal_id": deal_id,
            "stage_id": stage_id,
            "deal_name": deal_name,
            "phone": phone,
            "action": action,
            "detail": detail,
            "created_at": created_at,
        },
        "deal_id,stage_id",
    )


def is_cadence_stage_seeded(stage_id: str) -> bool:
    return bool(_get("omnichat_cadence_stages_seeded", select="stage_id", stage_id=f"eq.{stage_id}"))


def pending_cadence_routes(since: str) -> list[dict]:
    return _get(
        "omnichat_cadence_sends",
        select="*",
        action="eq.enviado",
        detail="like.*carteira pendente*",
        created_at=f"gte.{since}",
        limit=_PAGE,
    )


def delete_cadence(deal_id: str, stage_id: str) -> None:
    _check(
        _client.delete(
            f"{_BASE}/omnichat_cadence_sends",
            params={"deal_id": f"eq.{deal_id}", "stage_id": f"eq.{stage_id}"},
        )
    )


def mark_cadence_stage_seeded(stage_id: str, seeded_at: str) -> None:
    _upsert(
        "omnichat_cadence_stages_seeded",
        {"stage_id": stage_id, "seeded_at": seeded_at},
        "stage_id",
        ignore=True,
    )


def get_recent_cadence(limit: int = 50, include_baseline: bool = False) -> list[dict]:
    params = {"select": "*", "order": "created_at.desc", "limit": limit}
    if not include_baseline:
        params["action"] = "neq.linha_de_base"
    return _get("omnichat_cadence_sends", **params)


def start_run(started_at: str) -> int:
    resp = _check(
        _client.post(
            f"{_BASE}/omnichat_poll_runs",
            json={"started_at": started_at},
            headers={"Prefer": "return=representation"},
        )
    )
    return resp.json()[0]["id"]


def finish_run(
    run_id: int,
    finished_at: str,
    pages_fetched: int,
    chats_scanned: int,
    tag_matches: int,
    deals_created: int,
    skipped_existing_deal: int,
    error: Optional[str],
) -> None:
    _check(
        _client.patch(
            f"{_BASE}/omnichat_poll_runs",
            params={"id": f"eq.{run_id}"},
            json={
                "finished_at": finished_at,
                "pages_fetched": pages_fetched,
                "chats_scanned": chats_scanned,
                "tag_matches": tag_matches,
                "deals_created": deals_created,
                "skipped_existing_deal": skipped_existing_deal,
                "error": error,
            },
            headers={"Prefer": "return=minimal"},
        )
    )


def get_status() -> dict:
    count_resp = _check(
        _client.get(
            f"{_BASE}/omnichat_processed_chats",
            params={"select": "chat_id", "limit": 1},
            headers={"Prefer": "count=exact"},
        )
    )
    total = int((count_resp.headers.get("content-range") or "*/0").split("/")[-1] or 0)
    runs = get_recent_runs(1)
    return {
        "last_checked_at": get_last_checked_at(),
        "total_leads_processed": total,
        "last_run": runs[0] if runs else None,
    }


def get_recent_processed(limit: int = 50) -> list[dict]:
    return _get("omnichat_processed_chats", select="*", order="processed_at.desc", limit=limit)


def get_recent_runs(limit: int = 20) -> list[dict]:
    return _get("omnichat_poll_runs", select="*", order="id.desc", limit=limit)
