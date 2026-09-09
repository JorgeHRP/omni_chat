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


def active_deal_id(contact: dict) -> Optional[str]:
    """Retorna o _id da primeira negociacao "ativa" do contato (criterio
    provisorio: sem win e sem closed_at), ou None se nao houver. Se o criterio
    real for "qualquer negociacao", afrouxar a condicao aqui."""
    for deal in contact.get("deals") or []:
        if deal.get("win") is None and not deal.get("closed_at"):
            return deal.get("_id") or deal.get("id")
    return None


def has_active_deal(contact: dict) -> bool:
    return active_deal_id(contact) is not None


async def create_deal(client: httpx.AsyncClient, name: str, phone: str) -> dict:
    # Sem `deal_source`: o unico ID que tinhamos (RD_CRM_DEAL_SOURCE_ID) nao
    # existe mais na conta e fazia o RD CRM responder 404 no POST /deals. O
    # campo e opcional; novos deals ficam sem a "origem" marcada.
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

    resp = await client.post(
        f"{config.RD_CRM_API_BASE}/deals",
        params={"token": config.RD_CRM_TOKEN},
        json=body,
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


async def create_annotation(client: httpx.AsyncClient, deal_id: str, text: str) -> dict:
    """Cria uma anotacao (aba Historico da negociacao) via POST /activities."""
    body = {
        "activity": {
            "deal_id": deal_id,
            "text": text,
            "user_id": config.RD_CRM_USER_ID,
        }
    }
    resp = await client.post(
        f"{config.RD_CRM_API_BASE}/activities",
        params={"token": config.RD_CRM_TOKEN},
        json=body,
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()
