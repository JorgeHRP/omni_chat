"""Cliente pra API publica de Chats do Omni (api.omni.chat/v1) - validada ao
vivo em 01/09/2026. Ver README.md deste servico pra contexto de como essa API
foi encontrada e o que ja foi confirmado/testado."""

import re
from datetime import datetime, timedelta, timezone
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


# Horario de Brasilia (sem horario de verao desde 2019). Offset fixo em vez de
# zoneinfo porque a imagem python:slim nao traz tzdata.
_BRT = timezone(timedelta(hours=-3))


def _format_ts(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso[:16]
    return dt.astimezone(_BRT).strftime("%d/%m/%Y %H:%M")


def format_history(messages: list[dict]) -> str:
    """Monta o historico da conversa no modelo de anotacao pedido pelo
    cliente (doc "Dados obrigatorios para criar um card"):

        [13/08/2026 13:17] 👤 Operador: *Sabrina Vargas Dainhaia:*
        Boa tarde Victoria, tudo certo?
        [13/08/2026 13:25] 💬 Cliente: Oi, boa tarde.

    So as mensagens da conversa (cliente/atendente); descarta roteamento e
    resumo automatico."""
    lines: list[str] = []
    for msg in messages:
        if msg.get("type") in _SKIP_MESSAGE_TYPES:
            continue
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        ts = _format_ts(msg.get("createdAt") or "")
        if (msg.get("status") or "").startswith("INCOMING"):
            lines.append(f"[{ts}] 💬 Cliente: {text}")
        else:
            operator = ((msg.get("user") or {}).get("name") or "").strip()
            if operator:
                lines.append(f"[{ts}] 👤 Operador: *{operator}:*\n{text}")
            else:
                lines.append(f"[{ts}] 👤 Operador: {text}")
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


def chat_owner(chat: dict) -> tuple[Optional[str], Optional[str]]:
    """(email, nome) do atendente responsavel pelo chat no Omni (`chat.user`)
    - quem assumiu a conversa e marcou a tag. A API nao expoe quem aplicou
    cada label, entao esse e o melhor indicador disponivel. Pode vir vazio."""
    user = chat.get("user") or {}
    email = (user.get("email") or "").strip().lower() or None
    name = " ".join(filter(None, [user.get("name"), user.get("lastName")])).strip() or None
    return email, name


def message_operators(messages: list[dict]) -> list[str]:
    """Nomes dos operadores que escreveram na conversa, do mais recente pro
    mais antigo, sem repetir. Inclui bots (ex.: "Mobi") - quem chama descarta
    os que nao tem usuario no RD."""
    names: list[str] = []
    for msg in reversed(messages):
        if (msg.get("status") or "").startswith("INCOMING") or msg.get("type") in _SKIP_MESSAGE_TYPES:
            continue
        user = msg.get("user") or {}
        name = " ".join(filter(None, [user.get("name"), user.get("lastName")])).strip()
        if name and name not in names:
            names.append(name)
    return names


def chat_email(chat: dict) -> Optional[str]:
    customer = chat.get("customer") or {}
    return (chat.get("email") or customer.get("email") or "").strip() or None


def chat_company(chat: dict) -> Optional[str]:
    return ((chat.get("customer") or {}).get("businessName") or "").strip() or None


def chat_tax_document(chat: dict) -> Optional[str]:
    """CNPJ (businessTaxId) ou CPF (taxDocumentNumber) do cliente no Omni, so
    digitos - o RD exige sem pontuacao."""
    customer = chat.get("customer") or {}
    for raw in (customer.get("businessTaxId"), customer.get("taxDocumentNumber")):
        digits = re.sub(r"\D", "", str(raw or ""))
        if digits:
            return digits
    return None
