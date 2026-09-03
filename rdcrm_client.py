"""Cliente pro RD Station CRM - mesma logica ja validada nos fluxos n8n
anteriores (busca contato por telefone, confere negociacao ativa, cria
negociacao)."""

from typing import Optional

import httpx

import config


async def find_contact_by_phone(client: httpx.AsyncClient, phone: str) -> Optional[dict]:
    resp = await client.get(
        f"{config.RD_CRM_API_BASE}/contacts",
        params={"token": config.RD_CRM_TOKEN, "phone": phone, "limit": 5},
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    contacts = data.get("contacts") or []
    return contacts[0] if contacts else None


def has_active_deal(contact: dict) -> bool:
    """Mesmo criterio provisorio dos fluxos anteriores: negociacao "ativa" =
    sem win e sem closed_at. Se o criterio real for "qualquer negociacao,
    mesmo ganha/perdida", trocar pra `bool(contact.get("deals"))`."""
    for deal in contact.get("deals") or []:
        if deal.get("win") is None and not deal.get("closed_at"):
            return True
    return False


async def create_deal(client: httpx.AsyncClient, name: str, phone: str) -> dict:
    body = {
        "deal": {
            "name": f"[Omni Tag: {config.OMNI_TAG_LABEL_NAME}] {name}",
            "deal_stage_id": config.RD_CRM_DEAL_STAGE_ID_LEAD,
            "user_id": config.RD_CRM_USER_ID,
            "deal_custom_fields": [],
        },
        "contacts": [
            {"name": name, "phones": [{"phone": phone, "type": "cellphone"}]},
        ],
    }
    if config.RD_CRM_DEAL_SOURCE_ID:
        body["deal_source"] = {"_id": config.RD_CRM_DEAL_SOURCE_ID}

    resp = await client.post(
        f"{config.RD_CRM_API_BASE}/deals",
        params={"token": config.RD_CRM_TOKEN},
        json=body,
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()
