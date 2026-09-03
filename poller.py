"""Logica central: busca conversas atualizadas no Omni (paginando de verdade,
sem os problemas de loop do n8n), filtra pela tag, e cria negociacao no RD
CRM so pra quem ainda nao tem uma."""

import logging
from datetime import datetime, timedelta, timezone

import httpx

import config
import db
import omni_client
import rdcrm_client

logger = logging.getLogger("leadsync.poller")


async def run_poll() -> dict:
    started_at = datetime.now(timezone.utc).isoformat()
    run_id = db.start_run(started_at)

    pages_fetched = 0
    chats_scanned = 0
    tag_matches = 0
    deals_created = 0
    skipped_existing_deal = 0
    error_message = None

    try:
        cursor = db.get_last_checked_at()
        if cursor is None:
            cursor = (
                datetime.now(timezone.utc) - timedelta(hours=config.INITIAL_LOOKBACK_HOURS)
            ).isoformat()
            logger.info("Primeira execucao - comecando a janela em %s", cursor)

        all_chats: list[dict] = []

        async with httpx.AsyncClient() as client:
            for page_num in range(1, config.MAX_PAGES_PER_RUN + 1):
                page = await omni_client.fetch_chats_page(client, cursor)
                pages_fetched = page_num
                chats_scanned += len(page)
                all_chats.extend(page)

                updated_ats = [c["updatedAt"] for c in page if c.get("updatedAt")]
                if updated_ats:
                    latest = max(updated_ats)
                    if latest > cursor:
                        cursor = latest

                logger.info("Pagina %s: %s chats (cursor agora em %s)", page_num, len(page), cursor)

                if len(page) < 100:
                    break
            else:
                logger.warning(
                    "Atingiu o limite de %s paginas nesta execucao - pode haver "
                    "backlog restante pra proxima rodada.",
                    config.MAX_PAGES_PER_RUN,
                )

            db.set_last_checked_at(cursor)

            leads = [c for c in all_chats if omni_client.chat_has_tag(c, config.OMNI_TAG_LABEL_ID)]
            tag_matches = len(leads)
            logger.info("%s chats escaneados, %s com a tag '%s'", chats_scanned, tag_matches, config.OMNI_TAG_LABEL_NAME)

            for chat in leads:
                chat_id = chat.get("objectId")
                if not chat_id or db.is_chat_processed(chat_id):
                    continue

                processed_at = datetime.now(timezone.utc).isoformat()
                phone = omni_client.normalize_phone(chat)
                name = omni_client.chat_name(chat, phone)

                if not phone:
                    db.mark_chat_processed(chat_id, phone, name, "sem_telefone", None, processed_at)
                    logger.warning("Chat %s (%s) com a tag mas sem telefone - pulando.", chat_id, name)
                    continue

                contact = await rdcrm_client.find_contact_by_phone(client, phone)
                if contact and rdcrm_client.has_active_deal(contact):
                    skipped_existing_deal += 1
                    db.mark_chat_processed(chat_id, phone, name, "ja_tem_negociacao", None, processed_at)
                    logger.info("Chat %s (%s) ja tem negociacao ativa no RD CRM - nada a fazer.", chat_id, name)
                    continue

                deal = await rdcrm_client.create_deal(client, name, phone)
                deal_id = (deal.get("deal") or {}).get("_id") or deal.get("_id")
                deals_created += 1
                db.mark_chat_processed(chat_id, phone, name, "negociacao_criada", deal_id, processed_at)
                logger.info("Negociacao criada no RD CRM pra %s (chat %s): deal_id=%s", name, chat_id, deal_id)

    except Exception as exc:  # noqa: BLE001 - queremos logar qualquer falha e nao derrubar o scheduler
        error_message = str(exc)
        logger.exception("Erro durante o polling")
    finally:
        finished_at = datetime.now(timezone.utc).isoformat()
        db.finish_run(
            run_id,
            finished_at,
            pages_fetched,
            chats_scanned,
            tag_matches,
            deals_created,
            skipped_existing_deal,
            error_message,
        )

    return {
        "pages_fetched": pages_fetched,
        "chats_scanned": chats_scanned,
        "tag_matches": tag_matches,
        "deals_created": deals_created,
        "skipped_existing_deal": skipped_existing_deal,
        "error": error_message,
    }
