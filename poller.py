"""Logica central: busca conversas atualizadas no Omni (paginando de verdade,
sem os problemas de loop do n8n), filtra pela tag, e cria negociacao no RD
CRM so pra quem ainda nao tem uma."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

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
    client: httpx.AsyncClient, deal_id: str, chat: dict, messages: list[dict], user_id: str
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
            await rdcrm_client.create_annotation(client, deal_id, f"Conversa no Omni: {url}", user_id)

        history = omni_client.format_history(messages)
        if history:
            if len(history) > config.RD_CRM_ANNOTATION_MAX_CHARS:
                # mantem as mensagens mais recentes; descarta a primeira linha
                # que ficou pela metade no corte.
                history = history[-config.RD_CRM_ANNOTATION_MAX_CHARS:].split("\n", 1)[-1]
                history = "[historico truncado - mensagens mais antigas omitidas]\n" + history
            await rdcrm_client.create_annotation(
                client, deal_id, f"Historico da conversa (Omni)\n\n{history}", user_id
            )
        logger.info(
            "Anotacoes (link + historico) anexadas a deal %s (chat %s).", deal_id, chat_id
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Falha anexando anotacoes do Omni a deal %s (chat %s).", deal_id, chat_id
        )


async def _fetch_messages(client: httpx.AsyncClient, chat_id: str) -> list[dict]:
    """Mensagens da conversa - usadas pra achar o dono e pra anotacao de
    historico. Falha aqui nao impede criar o card."""
    try:
        return await omni_client.fetch_chat_messages(
            client, chat_id, config.OMNI_HISTORY_MESSAGE_LIMIT
        )
    except httpx.HTTPError:
        logger.exception("Falha buscando mensagens do chat %s.", chat_id)
        return []


def _resolve_owner(
    chat: dict, messages: list[dict], users_by_email: dict, users_by_name: dict
) -> str:
    """Dono da negociacao = usuario do RD correspondente a quem atendeu o
    lead no Omni. Ordem: atendente do chat (`chat.user`, por email/nome) ->
    ultimo operador humano que escreveu na conversa (por nome; o `chat.user`
    costuma vir vazio). Sem correspondencia -> dono padrao."""
    email, name = omni_client.chat_owner(chat)
    if email and email in users_by_email:
        return users_by_email[email]
    candidates = ([name] if name else []) + omni_client.message_operators(messages)
    for candidate in candidates:
        user_id = rdcrm_client.match_user_by_name(candidate, users_by_name)
        if user_id:
            return user_id
    logger.warning(
        "Chat %s: nenhum atendente (%s) com usuario no RD CRM - usando dono padrao.",
        chat.get("objectId"), ", ".join(filter(None, [email] + candidates)) or "nenhum",
    )
    return config.RD_CRM_USER_ID


async def _resolve_organization(
    client: httpx.AsyncClient, chat: dict, owner_id: str
) -> tuple[Optional[dict], Optional[str]]:
    """(empresa ja existente no RD ou None, id da empresa pro card novo).
    Cria a empresa so quando o Omni traz razao social E CNPJ/CPF - o
    Documento Fiscal e obrigatorio na empresa do RD."""
    company = omni_client.chat_company(chat)
    tax_document = omni_client.chat_tax_document(chat)
    if not company:
        return None, None

    org = await rdcrm_client.find_organization(client, company, tax_document)
    if org:
        return org, org["_id"]
    if not tax_document:
        logger.info("Empresa '%s' sem CNPJ/CPF no Omni - card sem empresa.", company)
        return None, None
    try:
        created = await rdcrm_client.create_organization(client, company, tax_document, owner_id)
        org_id = created.get("_id") or (created.get("organization") or {}).get("_id")
        logger.info("Empresa '%s' criada no RD CRM: %s", company, org_id)
        return None, org_id
    except httpx.HTTPError:
        # Sem empresa o card ainda serve - nao vale perder o lead por isso.
        logger.exception("Falha criando empresa '%s' - card sera criado sem empresa.", company)
        return None, None


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

            users_by_email: dict[str, str] = {}
            users_by_name: dict[str, str] = {}
            if leads:
                try:
                    users_by_email, users_by_name = await rdcrm_client.fetch_users(client)
                except httpx.HTTPError:
                    logger.exception("Falha ao listar usuarios do RD CRM - usando dono padrao nesta execucao.")

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
                    messages = await _fetch_messages(client, chat_id)
                    owner_id = _resolve_owner(chat, messages, users_by_email, users_by_name)
                    contacts = await rdcrm_client.find_contacts_by_phone(client, phone)
                    org, org_id = await _resolve_organization(client, chat, owner_id)

                    existing = await rdcrm_client.find_existing_deal(client, contacts, org, owner_id)
                    if existing:
                        existing_deal_id = existing.get("_id") or existing.get("id")
                        skipped_existing_deal += 1
                        db.mark_chat_processed(chat_id, phone, name, "ja_tem_negociacao", existing_deal_id, processed_at)
                        logger.info("Chat %s (%s) ja tem negociacao ativa (%s) no funil - anexando anotacoes.", chat_id, name, existing_deal_id)
                        await _anexar_anotacoes_omni(client, existing_deal_id, chat, messages, owner_id)
                        continue

                    deal = await rdcrm_client.create_deal(
                        client, name, phone, omni_client.chat_email(chat), owner_id, org_id
                    )
                    deal_id = (deal.get("deal") or {}).get("_id") or deal.get("_id")
                    deals_created += 1
                    db.mark_chat_processed(chat_id, phone, name, "negociacao_criada", deal_id, processed_at)
                    logger.info(
                        "Negociacao criada no RD CRM pra %s (chat %s, dono %s, empresa %s): deal_id=%s",
                        name, chat_id, owner_id, org_id, deal_id,
                    )
                    await _anexar_anotacoes_omni(client, deal_id, chat, messages, owner_id)
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
