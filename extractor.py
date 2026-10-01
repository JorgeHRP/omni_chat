"""Extrai da conversa do Omni os dados do card que o atendente nao preencheu
no cadastro do cliente (empresa, CNPJ/CPF, email, nome completo), via API da
OpenAI. Desligado se OPENAI_API_KEY estiver vazio.

Nada que o modelo devolve e usado sem validar: CPF/CNPJ passam pelo digito
verificador e email pelo formato - numero inventado ou digitado errado e
descartado. Os campos do cadastro do Omni sempre tem prioridade (quem chama
so usa o extraido pra preencher o que estiver vazio)."""

import json
import logging
import re
from typing import Optional

import httpx

import config
import omni_client

logger = logging.getLogger("leadsync.extractor")

_PROMPT = """Voce le conversas de WhatsApp entre atendentes de uma empresa de \
software (Promob) e clientes, e extrai dados cadastrais do CLIENTE para \
criar um card no CRM.

Regras:
- Extraia somente o que o cliente (ou o atendente, repetindo o dado do \
cliente) escreveu na conversa. Nunca invente nem complete dados.
- empresa: somente o NOME da empresa do cliente (razao social ou nome \
fantasia), como escrito na conversa. Se o cliente so descreve o negocio (ex.: \
"loja de moveis em Goiania") ou se for um rotulo/titulo (ex.: "Dados da \
empresa"), use null. null se nao aparecer.
- documento: CNPJ ou CPF do cliente, como aparece na conversa. Se houver os \
dois, prefira o CNPJ. null se nao aparecer.
- email: email do cliente. null se nao aparecer.
- nome_completo: nome completo da pessoa (nome e sobrenome), se aparecer. \
null se so houver o primeiro nome.
- Ignore dados da propria empresa do atendente (Promob, Cyncly) e de \
terceiros que nao sejam o cliente."""

_SCHEMA = {
    "name": "dados_cliente",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "empresa": {"type": ["string", "null"]},
            "documento": {"type": ["string", "null"]},
            "email": {"type": ["string", "null"]},
            "nome_completo": {"type": ["string", "null"]},
        },
        "required": ["empresa", "documento", "email", "nome_completo"],
        "additionalProperties": False,
    },
}

_EMPTY_VALUES = {"", "null", "none", "n/a", "na", "-", "nao informado", "não informado"}
# Rotulos/descricoes que o modelo as vezes devolve no lugar do nome.
_COMPANY_LABELS = ("dados da empresa", "loja de ", "marcenaria de ", "fabrica de ", "fábrica de ")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


def _cpf_ok(d: str) -> bool:
    if len(d) != 11 or d == d[0] * 11:
        return False
    for n in (9, 10):
        s = sum(int(d[i]) * (n + 1 - i) for i in range(n))
        if (s * 10 % 11) % 10 != int(d[n]):
            return False
    return True


def _cnpj_ok(d: str) -> bool:
    if len(d) != 14 or d == d[0] * 14:
        return False
    for n in (12, 13):
        weights = list(range(n - 7, 1, -1)) + list(range(9, 1, -1))
        s = sum(int(d[i]) * weights[i] for i in range(n))
        r = s % 11
        if (0 if r < 2 else 11 - r) != int(d[n]):
            return False
    return True


def valid_tax_document(raw: Optional[str]) -> Optional[str]:
    """So digitos, e so se for CPF ou CNPJ com digito verificador valido."""
    digits = re.sub(r"\D", "", raw or "")
    return digits if (_cpf_ok(digits) or _cnpj_ok(digits)) else None


def _conversation_text(messages: list[dict]) -> str:
    """Texto simples da conversa pro modelo - mesma selecao de mensagens do
    historico, sem emoji, cortado nas mensagens mais recentes."""
    lines = []
    for msg in messages:
        if msg.get("type") in omni_client.SKIP_MESSAGE_TYPES:
            continue
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        if (msg.get("status") or "").startswith("INCOMING"):
            lines.append(f"Cliente: {text}")
        else:
            operator = ((msg.get("user") or {}).get("name") or "").strip()
            lines.append(f"Atendente{f' ({operator})' if operator else ''}: {text}")
    text = "\n".join(lines)
    return text[-config.OPENAI_MAX_INPUT_CHARS:]


async def extract_lead_fields(client: httpx.AsyncClient, messages: list[dict]) -> dict:
    """{empresa, documento, email, nome_completo} validados (None no que nao
    achou ou nao passou na validacao). Retorna {} se desligado ou sem conversa."""
    if not config.OPENAI_API_KEY:
        return {}
    conversation = _conversation_text(messages)
    if not conversation:
        return {}

    resp = await client.post(
        f"{config.OPENAI_API_BASE}/chat/completions",
        headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
        json={
            "model": config.OPENAI_MODEL,
            "messages": [
                {"role": "system", "content": _PROMPT},
                {"role": "user", "content": conversation},
            ],
            "response_format": {"type": "json_schema", "json_schema": _SCHEMA},
        },
        timeout=60.0,
    )
    resp.raise_for_status()
    raw = json.loads(resp.json()["choices"][0]["message"]["content"])

    def clean(value: Optional[str]) -> Optional[str]:
        value = " ".join((value or "").split())
        # O modelo as vezes devolve o texto "null" em vez do null do JSON.
        return None if value.lower() in _EMPTY_VALUES else value or None

    email = clean(raw.get("email"))
    fields = {
        "empresa": clean(raw.get("empresa")),
        "documento": valid_tax_document(raw.get("documento")),
        "email": email.lower() if email and _EMAIL_RE.match(email) else None,
        "nome_completo": clean(raw.get("nome_completo")),
    }
    if fields["empresa"] and fields["empresa"].lower().startswith(_COMPANY_LABELS):
        fields["empresa"] = None
    # So o primeiro nome nao serve como nome de empresa.
    if fields["nome_completo"] and len(fields["nome_completo"].split()) < 2:
        fields["nome_completo"] = None
    if raw.get("documento") and not fields["documento"]:
        logger.info("Documento extraido '%s' invalido (digito verificador) - descartado.", raw["documento"])
    return fields
