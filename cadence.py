"""Cadencia de WhatsApp: a automacao do RD CRM move as negociacoes do funil
"3.Comercial Brasil" pelas etapas T1º..T5º CONTATO. Nas etapas T1, T3 e T4 o
contato e por WhatsApp - aqui, quando uma negociacao aberta aparece numa
dessas etapas, disparamos a campanha do Omni da etapa
(`CADENCE_STAGE_CAMPAIGNS`). Cada campanha ("Cadencia RD T1/T3/T4 (automatica -
nao apagar)", criadas em 04/11/2026) ja tem o template, a imagem do cabecalho e o
numero de saida 55 54 3022-5800; o `{{1}}` do T4 vem do campo `consultor`.
POST /v1/messages (API antiga) responde 400 pra esses templates - nao usar.

Cada (negociacao, etapa) e gravado uma unica vez em `cadence_sends`, com
`action`: `enviado`, `enviando` (envio saiu mas nao confirmou), `sem_telefone`, `erro` (nao e reenviado sozinho - apague a
linha pra tentar de novo; recusa de chave do Omni nao vira `erro`, a linha e
apagada e a negociacao volta a ser tentada) ou `linha_de_base`.

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


def _is_active(deal: dict) -> bool:
    return deal.get("win") is None and not deal.get("closed_at") and not deal.get("hold")


async def send_one(
    client: httpx.AsyncClient, campaign_id: str, phone: str, consultor: str
) -> tuple[str, Optional[str]]:
    """Dispara a campanha pra um telefone (usado tambem no teste manual).
    Devolve (dispatch_id, chat_id)."""
    return await omni_client.dispatch_campaign(client, campaign_id, phone, consultor)


def find_omni_owner(owner: dict, omni_users: list[dict]) -> Optional[dict]:
    """Atendente do Omni que corresponde ao dono da negociacao no RD: mesmo
    email, senao mesmo nome (exato ou primeiro + ultimo, so se for unico) -
    alguns emails sao diferentes nos dois sistemas."""
    email = (owner.get("email") or "").strip().lower()
    if email:
        for u in omni_users:
            if (u.get("email") or "").strip().lower() == email:
                return u
    by_name: dict[str, str] = {}
    for u in omni_users:
        full = " ".join(filter(None, [u.get("name"), u.get("lastName")]))
        if full:
            by_name.setdefault(rdcrm_client.norm_text(full), u["objectId"])
    uid = rdcrm_client.match_user_by_name(owner.get("name") or "", by_name)
    return next((u for u in omni_users if u["objectId"] == uid), None) if uid else None


async def route_to_owner(
    client: httpx.AsyncClient, chat_id: Optional[str], owner: dict, omni_users: list[dict]
) -> str:
    """Passa a conversa pra carteira do dono. Nunca levanta erro: a mensagem
    ja saiu, falhar aqui so e registrado (texto devolvido vai pro detail)."""
    who = owner.get("name") or owner.get("email") or "?"
    if not chat_id:
        logger.warning("Cadencia: Omni nao informou a conversa - nao deu pra passar pra carteira de %s.", who)
        return "carteira: conversa desconhecida"
    target = find_omni_owner(owner, omni_users)
    if not target:
        logger.warning("Cadencia: dono %s (%s) nao encontrado no Omni - conversa %s fica sem carteira.", who, owner.get("email"), chat_id)
        return f"carteira: {who} nao encontrado no Omni"
    try:
        await omni_client.route_chat_to_user(client, chat_id, target["objectId"])
    except Exception as exc:  # noqa: BLE001 - a mensagem ja foi; so registra
        logger.exception("Cadencia: erro passando a conversa %s pra %s.", chat_id, target.get("name"))
        return f"carteira: erro ({str(exc)[:150]})"
    logger.info("Cadencia: conversa %s passada pra carteira de %s.", chat_id, target.get("name"))
    return f"carteira: {target.get('name')}"


async def run_cadence() -> dict:
    stats = {"enviados": 0, "linha_de_base": 0, "sem_telefone": 0, "erros": 0}
    if not config.CADENCE_WHATSAPP_ENABLED:
        return {"desligado": True}

    try:
        async with httpx.AsyncClient() as client:
            sends_left = config.CADENCE_MAX_SENDS_PER_RUN
            omni_users: Optional[list[dict]] = None  # carregado no 1o envio

            for stage_id, campaign_id in config.CADENCE_STAGE_CAMPAIGNS.items():
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
                    # Grava ANTES de enviar: se o banco falhar, nao envia; se o
                    # envio sair mas a atualizacao falhar, a linha fica
                    # "enviando" e nunca reenvia (melhor perder do que duplicar).
                    db.mark_cadence(deal_id, stage_id, name, phone, "enviando", None, _now())
                    try:
                        dispatch_id, chat_id = await omni_client.dispatch_campaign(client, campaign_id, phone, _owner_name(deal))
                        detail = dispatch_id
                        if config.CADENCE_ROUTE_TO_OWNER:
                            if omni_users is None:
                                omni_users = await omni_client.fetch_users(client)
                            detail = f"{dispatch_id} | {await route_to_owner(client, chat_id, deal.get('user') or {}, omni_users)}"
                        db.mark_cadence(deal_id, stage_id, name, phone, "enviado", detail, _now())
                        stats["enviados"] += 1
                        logger.info("Cadencia: campanha %s disparada pra %s (%s, negociacao %s, dispatch %s).", campaign_id, name, phone, deal_id, dispatch_id)
                    except omni_client.OmniAuthError as exc:
                        # Recusado na autenticacao = nada saiu. Apaga a linha
                        # pra negociacao ser tentada de novo quando a chave
                        # voltar, e para: os proximos envios falhariam igual.
                        db.delete_cadence(deal_id, stage_id)
                        stats["erro_geral"] = str(exc)[:300]
                        logger.error("Cadencia parada: %s", exc)
                        return stats
                    except Exception as exc:  # noqa: BLE001 - um envio ruim nao para os outros
                        db.mark_cadence(deal_id, stage_id, name, phone, "erro", str(exc)[:500], _now())
                        stats["erros"] += 1
                        logger.exception("Cadencia: erro enviando pra %s (%s, negociacao %s).", name, phone, deal_id)
    except Exception as exc:  # noqa: BLE001 - nao pode derrubar o scheduler
        stats["erro_geral"] = str(exc)
        logger.exception("Erro na cadencia de WhatsApp")
    return stats
