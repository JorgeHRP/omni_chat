"""Cliente pra API publica de Chats do Omni (api.omni.chat/v1) - validada ao
vivo em 01/09/2026. Ver README.md deste servico pra contexto de como essa API
foi encontrada e o que ja foi confirmado/testado."""

from typing import Optional

import httpx

import config

HEADERS = {
    "x-api-key": config.OMNI_API_KEY,
    "x-api-secret": config.OMNI_API_SECRET,
}


async def fetch_chats_page(
    client: httpx.AsyncClient, updated_at_gt: str, limit: int = 100
) -> list[dict]:
    resp = await client.get(
        f"{config.OMNI_CHATS_API_BASE}/chats",
        headers=HEADERS,
        params={
            "updatedAt.gt": updated_at_gt,
            "limit": limit,
            "order.updatedAt": "ASC",
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


def chat_has_tag(chat: dict, tag_label_id: str) -> bool:
    return any(label.get("objectId") == tag_label_id for label in (chat.get("labels") or []))


def normalize_phone(chat: dict) -> Optional[str]:
    """O telefone vem como {countryCode, areaCode, phoneNumber} - juntamos
    tudo em uma string so de digitos."""
    phone = chat.get("phone") or {}
    parts = [phone.get("countryCode"), phone.get("areaCode"), phone.get("phoneNumber")]
    digits = "".join(str(p) for p in parts if p is not None)
    return digits or None


def chat_name(chat: dict, phone: Optional[str]) -> str:
    return chat.get("name") or (f"Contato Omni {phone}" if phone else "Contato Omni sem nome")
