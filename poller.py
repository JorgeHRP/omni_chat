"""Logica central: busca conversas atualizadas no Omni (paginando de verdade,
sem os problemas de loop do n8n), filtra pela tag, e cria negociacao no RD
CRM so pra quem ainda nao tem uma."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

import config
import db
import omni_client
import rdcrm_client

logger = logging.getLogger("leadsync.poller")

# Impede que a execucao agendada e o trigger manual (POST /poll/run) rodem em
# paralelo - sem isso os dois leem o mesmo watermark, passam pelo dedup ao
# mesmo tempo e criam negociacao duplicada no RD CRM.
_run_lock = asyncio.Lock()


async def _anexar_anotacoes_omni(
    client: httpx.AsyncClient, deal_id: str, chat: dict
) -> None:
    """Cria 2 anotacoes na negociacao (aba Historico): (1) link direto pra
    conversa no Omni, (2) historico da conversa. Erro aqui e logado mas nao
    propaga - a negociacao ja existe, nao faz sentido marcar o lead como
    'erro' so porque a anotacao falhou."""
    chat_id = chat.get("objectId")
    if not deal_id:
        logger.warning("Sem deal_id pra anexar anotacoes (chat %s) - pulando.", chat_id)
        return

    try:
        url = omni_client.chat_conversation_url(chat)
        if url:
            await rdcrm_client.create_annotation(client, deal_id, f"Conversa no Omni: {url}")

        messages = await omni_client.fetch_chat_messages(
            client, chat_id, config.OMNI_HISTORY_MESSAGE_LIMIT
        )
        history = omni_client.format_history(messages)
        if history:
            if len(history) > config.RD_CRM_ANNOTATION_MAX_CHARS:
                # mantem as mensagens mais recentes; descarta a primeira linha
                # que ficou pela metade no corte.
                history = history[-config.RD_CRM_ANNOTATION_MAX_CHARS:].split("\n", 1)[-1]
                history = "[historico truncado - mensagens mais antigas omitidas]\n" + history
            await rdcrm_client.create_annotation(
                client, deal_id, f"Historico da conversa (Omni)\n\n{history}"
            )
        logger.info(
            "Anotacoes (link + historico) anexadas a deal %s (chat %s).", deal_id, chat_id
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Falha anexando anotacoes do Omni a deal %s (chat %s).", deal_id, chat_id
        )


async def run_poll() -> dict:
    if _run_lock.locked():
        logger.info("Poll ja em execucao - ignorando este disparo.")
        return {"skipped": "ja em execucao"}

    async with _run_lock:
        return await _run_poll()


async def _run_poll() -> dict:
    started_at = datetime.now(timezone.utc).isoformat()
    run_id = db.start_run(started_at)

    pages_fetched = 0
    chats_scanned = 0
    tag_matches = 0
    deals_created = 0
    skipped_existing_deal = 0
    lead_errors: list[str] = []
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

                try:
                    contact = await rdcrm_client.find_contact_by_phone(client, phone)
                    existing_deal_id = rdcrm_client.active_deal_id(contact) if contact else None
                    if existing_deal_id:
                        skipped_existing_deal += 1
                        db.mark_chat_processed(chat_id, phone, name, "ja_tem_negociacao", existing_deal_id, processed_at)
                        logger.info("Chat %s (%s) ja tem negociacao ativa (%s) no RD CRM - anexando anotacoes.", chat_id, name, existing_deal_id)
                        await _anexar_anotacoes_omni(client, existing_deal_id, chat)
                        continue

                    deal = await rdcrm_client.create_deal(client, name, phone)
                    deal_id = (deal.get("deal") or {}).get("_id") or deal.get("_id")
                    deals_created += 1
                    db.mark_chat_processed(chat_id, phone, name, "negociacao_criada", deal_id, processed_at)
                    logger.info("Negociacao criada no RD CRM pra %s (chat %s): deal_id=%s", name, chat_id, deal_id)
                    await _anexar_anotacoes_omni(client, deal_id, chat)
                except Exception as exc:  # noqa: BLE001
                    # Falha num lead nao pode abortar a run inteira nem impedir o
                    # avanco do watermark (senao os leads seguintes nunca sao
                    # reprocessados). Registra como "erro" - o dedup passa a
                    # pular esse chat; a falha fica visivel no log e na run.
                    lead_errors.append(f"{chat_id} ({name}): {exc}")
                    db.mark_chat_processed(chat_id, phone, name, "erro", None, processed_at)
                    logger.exception("Erro processando chat %s (%s) - marcado como 'erro'.", chat_id, name)

            # So avanca o watermark depois que todos os leads da janela foram
            # tratados (criados, pulados ou marcados como erro).
            db.set_last_checked_at(cursor)

        if lead_errors:
            error_message = f"{len(lead_errors)} lead(s) com erro: " + "; ".join(lead_errors)

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
