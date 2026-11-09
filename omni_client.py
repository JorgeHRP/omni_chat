"""Cliente pra API publica de Chats do Omni (api.omni.chat/v1) - validada ao
vivo em 01/09/2026. Ver README.md deste servico pra contexto de como essa API
foi encontrada e o que ja foi confirmado/testado."""

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

import config

HEADERS = {
    "x-api-key": config.OMNI_API_KEY,
    "x-api-secret": config.OMNI_API_SECRET,
}


class OmniAuthError(RuntimeError):
    """O Omni recusou a chave (x-api-key/x-api-secret revogada ou trocada)."""


# Com chave invalida a api.omni.chat responde HTTP 200 com o corpo em texto
# '400 - {"code":209,"error":"Invalid session token"}' (visto 04-05/11/2026);
# o resp.json() quebrava com "Extra data: line 1 column 5".
_EMBEDDED_ERROR = re.compile(r"^\s*(\d{3}) - (.*)$", re.S)


def _json(resp: httpx.Response, what: str):
    resp.raise_for_status()
    m = _EMBEDDED_ERROR.match(resp.text)
    if m:
        body = m.group(2)[:300]
        if "Invalid session token" in body or '"code":209' in body:
            raise OmniAuthError(f"Omni recusou a chave em {what}: {body}")
        raise RuntimeError(f"Omni respondeu erro {m.group(1)} em {what}: {body}")
    return resp.json()


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
    return _json(resp, "GET /chats")


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
    return _json(resp, f"GET /chats/{chat_id}/messages")


CAMPAIGN_API_BASE = "https://campaign-api.omni.chat/v1"


async def dispatch_campaign(
    client: httpx.AsyncClient, campaign_id: str, phone: str, consultor: str
) -> str:
    """Dispara a campanha pra um contato agora. Usa POST /campaigns/{id}/test:
    o /dispatch aceita o pedido mas nao envia nada numa campanha que nunca foi
    iniciada (testado 04/11/2026 - "Dispatch not found"); o /test envia na hora
    e o Omni registra normal (status em GET /dispatches/{id}). `phone` so
    digitos com DDI."""
    resp = await client.post(
        f"{CAMPAIGN_API_BASE}/campaigns/{campaign_id}/test",
        headers=HEADERS,
        json={"contacts": [{"fullNumber": phone, "consultor": consultor}]},
        timeout=30.0,
    )
    if resp.status_code in (401, 403):
        raise OmniAuthError(f"Omni recusou a chave em POST /campaigns/{campaign_id}/test: {resp.text[:300]}")
    if resp.is_error:
        raise httpx.HTTPStatusError(
            f"{resp.status_code} em POST /campaigns/{campaign_id}/test: {resp.text[:500]}",
            request=resp.request,
            response=resp,
        )
    dispatches = resp.json().get("dispatches") or []
    if not dispatches:
        raise DispatchError(f"POST /campaigns/{campaign_id}/test nao devolveu dispatch: {resp.text[:300]}")
    dispatch_id = dispatches[0]["id"]
    await _wait_dispatch(client, dispatch_id)
    return dispatch_id


class DispatchError(RuntimeError):
    """O Omni aceitou o disparo mas a mensagem nao saiu (OUTGOING_ERROR)."""


# Statuses que ainda nao sao resultado final do disparo.
_DISPATCH_PENDING = {"CREATED", "QUEUED", "OUTGOING_CREATED"}


async def _wait_dispatch(client: httpx.AsyncClient, dispatch_id: str, tries: int = 8) -> None:
    """O /test responde 200 mesmo quando o envio falha logo depois (ex.:
    campanha criada por um usuario/chave que nao existe mais ->
    CHANNEL_USER_NOT_FOUND, visto 08/11/2026). Confere o status por alguns
    segundos; se continuar pendente, considera enviado."""
    for _ in range(tries):
        await asyncio.sleep(2)
        resp = await client.get(f"{CAMPAIGN_API_BASE}/dispatches/{dispatch_id}", headers=HEADERS, timeout=30.0)
        if resp.status_code in (401, 403):
            raise OmniAuthError(f"Omni recusou a chave em GET /dispatches/{dispatch_id}: {resp.text[:300]}")
        if resp.is_error:
            continue
        body = resp.json()
        status = body.get("status") or ""
        if status in _DISPATCH_PENDING:
            continue
        if "ERROR" not in status:
            return
        err = body.get("error") or {}
        msg = f"disparo {dispatch_id} {status}: {err.get('reason') or err.get('code')} - {err.get('message')}"
        # Problema da campanha, nao do contato: nada vai sair ate recriar a
        # campanha com a chave atual. Tratado como chave recusada (a cadencia
        # para e a negociacao e tentada de novo depois).
        if err.get("reason") == "CHANNEL_USER_NOT_FOUND":
            raise OmniAuthError(f"campanha presa a um usuario do Omni que nao existe mais, recrie com a chave atual ({msg})")
        raise DispatchError(msg)


def chat_conversation_url(chat: dict) -> Optional[str]:
    chat_id = chat.get("objectId")
    if not chat_id:
        return None
    return config.OMNI_CHAT_URL_TEMPLATE.format(chat_id=chat_id)


# Tipos de mensagem que sao "ruido de sistema" e nao fazem parte do historico
# da conversa em si (roteamento entre times, resumo automatico do bot, etc).
SKIP_MESSAGE_TYPES = {"ROUTING", "SUMMARY", "GROUPED", "INTERACTIVE"}


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
        if msg.get("type") in SKIP_MESSAGE_TYPES:
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
        if (msg.get("status") or "").startswith("INCOMING") or msg.get("type") in SKIP_MESSAGE_TYPES:
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


def chat_customer_full_name(chat: dict) -> Optional[str]:
    """Nome + sobrenome do cadastro do cliente no Omni, sem numeros soltos -
    tem cadastro com o CPF digitado no sobrenome ("Tine - 025.027.524-42")."""
    customer = chat.get("customer") or {}
    full = " ".join(filter(None, [customer.get("name"), customer.get("lastName")]))
    full = re.sub(r"[\d.\-/]{5,}", " ", full)
    full = " ".join(full.replace(" - ", " ").split()).strip(" -")
    return full or None


def chat_tax_document(chat: dict) -> Optional[str]:
    """CNPJ (businessTaxId) ou CPF (taxDocumentNumber) do cliente no Omni, so
    digitos - o RD exige sem pontuacao."""
    customer = chat.get("customer") or {}
    for raw in (customer.get("businessTaxId"), customer.get("taxDocumentNumber")):
        digits = re.sub(r"\D", "", str(raw or ""))
        if digits:
            return digits
    return None
