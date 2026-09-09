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


async def fetch_chat_messages(
    client: httpx.AsyncClient, chat_id: str, limit: int = 300
) -> list[dict]:
    """Mensagens da conversa em ordem cronologica (mais antiga primeiro)."""
    resp = await client.get(
        f"{config.OMNI_CHATS_API_BASE}/chats/{chat_id}/messages",
        headers=HEADERS,
        params={"limit": limit, "order.createdAt": "ASC"},
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


def chat_conversation_url(chat: dict) -> Optional[str]:
    chat_id = chat.get("objectId")
    if not chat_id:
        return None
    return config.OMNI_CHAT_URL_TEMPLATE.format(chat_id=chat_id)


# Tipos de mensagem que sao "ruido de sistema" e nao fazem parte do historico
# da conversa em si (roteamento entre times, resumo automatico do bot, etc).
_SKIP_MESSAGE_TYPES = {"ROUTING", "SUMMARY", "GROUPED", "INTERACTIVE"}


def format_history(messages: list[dict]) -> str:
    """Monta o historico da conversa como texto simples pra virar anotacao no
    RD CRM. So as mensagens da conversa (cliente/atendente), marcando a
    direcao; descarta roteamento/resumo automatico."""
    lines: list[str] = []
    for msg in messages:
        if msg.get("type") in _SKIP_MESSAGE_TYPES:
            continue
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        if (msg.get("status") or "").startswith("INCOMING"):
            who = "Cliente"
        else:
            user = msg.get("user") or {}
            who = user.get("name") or "Atendimento"
        ts = (msg.get("createdAt") or "")[:19].replace("T", " ")
        lines.append(f"[{ts}] {who}: {text}")
    return "\n".join(lines)


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
