"""Cadencia de WhatsApp: a automacao do RD CRM move as negociacoes do funil
"3.Comercial Brasil" pelas etapas T1º..T5º CONTATO. Nas etapas T1, T3 e T4 o
contato e por WhatsApp - aqui, quando uma negociacao aberta aparece numa
dessas etapas, mandamos o template correspondente pelo Omni
(`CADENCE_STAGE_TEMPLATES`).

Cada (negociacao, etapa) e gravado uma unica vez em `cadence_sends`, com
`action`: `enviado`, `sem_telefone`, `erro` (nao e reenviado sozinho - apague a
linha pra tentar de novo) ou `linha_de_base`.

Linha de base: na primeira execucao com o envio ligado, as negociacoes que JA
estavam em cada etapa sao gravadas como `linha_de_base` sem mandar nada - so
quem entrar na etapa depois recebe a mensagem. Sem isso, ligar o envio
disparava de uma vez pra centenas de negociacoes antigas."""

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

import config
import db
import omni_client
import rdcrm_client

logger = logging.getLogger("leadsync.cadence")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _owner_name(deal: dict) -> str:
    """Nome do consultor pro {{1}} do template: primeiro + ultimo nome do dono."""
    parts = ((deal.get("user") or {}).get("name") or "").split()
    if not parts:
        return "consultor"
    return parts[0] if len(parts) == 1 else f"{parts[0]} {parts[-1]}"


def _tokens(template: dict, deal: dict) -> list[str]:
    count = omni_client.template_token_count(template)
    if count == 0:
        return []
    if count == 1:
        return [_owner_name(deal)]
    raise ValueError(f"template {template.get('title')!r} tem {count} variaveis - so sei preencher {{{{1}}}}")


def _is_active(deal: dict) -> bool:
    return deal.get("win") is None and not deal.get("closed_at") and not deal.get("hold")


async def send_one(
    client: httpx.AsyncClient, template_id: str, phone: str, deal: Optional[dict] = None
) -> dict:
    """Envia um template pra um telefone (usado tambem no teste manual)."""
    templates = await omni_client.fetch_message_templates(client)
    template = templates.get(template_id)
    if not template:
        raise ValueError(f"template {template_id} nao existe no Omni")
    tokens = _tokens(template, deal or {})
    return await omni_client.send_template(client, template_id, phone, tokens)


async def run_cadence() -> dict:
    stats = {"enviados": 0, "linha_de_base": 0, "sem_telefone": 0, "erros": 0}
    if not config.CADENCE_WHATSAPP_ENABLED:
        return {"desligado": True}

    try:
        async with httpx.AsyncClient() as client:
            templates = await omni_client.fetch_message_templates(client)
            sends_left = config.CADENCE_MAX_SENDS_PER_RUN

            for stage_id, template_id in config.CADENCE_STAGE_TEMPLATES.items():
                template = templates.get(template_id)
                if not template or template.get("status") != "READY":
                    logger.error("Template %s da etapa %s nao existe ou nao esta READY no Omni - pulando etapa.", template_id, stage_id)
                    stats["erros"] += 1
                    continue

                deals = [d for d in await rdcrm_client.fetch_deals_in_stage(client, stage_id) if _is_active(d)]
                done = db.cadence_done_deal_ids(stage_id)
                new_deals = [d for d in deals if d["_id"] not in done]

                if not db.is_cadence_stage_seeded(stage_id):
                    for d in new_deals:
                        db.mark_cadence(d["_id"], stage_id, d.get("name"), None, "linha_de_base", None, _now())
                    db.mark_cadence_stage_seeded(stage_id, _now())
                    stats["linha_de_base"] += len(new_deals)
                    logger.info("Etapa %s: linha de base com %s negociacoes (nada enviado).", stage_id, len(new_deals))
                    continue

                for deal in new_deals:
                    if sends_left <= 0:
                        logger.warning("Limite de %s envios por execucao atingido - resto fica pra proxima.", config.CADENCE_MAX_SENDS_PER_RUN)
                        return stats
                    deal_id, name = deal["_id"], deal.get("name")
                    phone = rdcrm_client.whatsapp_phone(deal)
                    if not phone:
                        db.mark_cadence(deal_id, stage_id, name, None, "sem_telefone", None, _now())
                        stats["sem_telefone"] += 1
                        logger.warning("Cadencia: negociacao %s (%s) sem celular - nao enviado.", deal_id, name)
                        continue
                    sends_left -= 1
                    try:
                        result = await omni_client.send_template(client, template_id, phone, _tokens(template, deal))
                        db.mark_cadence(deal_id, stage_id, name, phone, "enviado", str(result.get("objectId") or ""), _now())
                        stats["enviados"] += 1
                        logger.info("Cadencia: template %r enviado pra %s (%s, negociacao %s).", template.get("title"), name, phone, deal_id)
                    except Exception as exc:  # noqa: BLE001 - um envio ruim nao para os outros
                        db.mark_cadence(deal_id, stage_id, name, phone, "erro", str(exc)[:500], _now())
                        stats["erros"] += 1
                        logger.exception("Cadencia: erro enviando pra %s (%s, negociacao %s).", name, phone, deal_id)
    except Exception as exc:  # noqa: BLE001 - nao pode derrubar o scheduler
        stats["erro_geral"] = str(exc)
        logger.exception("Erro na cadencia de WhatsApp")
    return stats
