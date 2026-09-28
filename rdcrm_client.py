"""Cliente pro RD Station CRM - busca contato/empresa, confere negociacao
ativa no funil configurado, cria empresa/negociacao e anotacoes.

Campos do card seguem o doc do cliente "Dados obrigatorios para criar um
card": nome da negociacao (nome do lead), empresa (com Documento
Fiscal so em digitos), contato (nome, telefone com WhatsApp, email)."""

import logging
import re
import unicodedata
from typing import Optional

import httpx

import config

logger = logging.getLogger("leadsync.rdcrm")


def _params(**extra) -> dict:
    return {"token": config.RD_CRM_TOKEN, **extra}


def _raise_for_status(resp: httpx.Response) -> None:
    """Como `raise_for_status`, mas inclui o corpo da resposta na mensagem (o
    RD explica o 422 no corpo) e nao expoe o token da URL."""
    if resp.is_error:
        raise httpx.HTTPStatusError(
            f"{resp.status_code} em {resp.request.method} {resp.url.path}: {resp.text[:500]}",
            request=resp.request,
            response=resp,
        )


def norm_text(value: Optional[str]) -> str:
    """Pra comparar nomes: sem acento, minusculo, espacos colapsados."""
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(c for c in value if not unicodedata.combining(c))
    return " ".join(value.lower().split())


def _phone_key(raw: Optional[str]) -> Optional[str]:
    """Chave de comparacao de telefone BR: DDD + ultimos 8 digitos. Ignora o
    55 e o nono digito, que o RD guarda de jeitos diferentes
    ("(62) 98150-8319", "5562981508319", "6281508319"...)."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) >= 12 and digits.startswith("55"):
        digits = digits[2:]
    if len(digits) < 10:
        return digits or None
    return digits[:2] + digits[-8:]


def _phone_search_variants(phone: str) -> list[str]:
    digits = re.sub(r"\D", "", phone)
    national = digits[2:] if len(digits) >= 12 and digits.startswith("55") else digits
    variants = [digits, national]
    if len(national) == 11:
        variants.append(f"({national[:2]}) {national[2:7]}-{national[7:]}")
    return list(dict.fromkeys(variants))


# --- usuarios -----------------------------------------------------------------


async def fetch_users(client: httpx.AsyncClient) -> tuple[dict[str, str], dict[str, str]]:
    """(email -> user_id, nome normalizado -> user_id) dos usuarios do RD."""
    resp = await client.get(f"{config.RD_CRM_API_BASE}/users", params=_params(), timeout=30.0)
    resp.raise_for_status()
    data = resp.json()
    users = data.get("users", []) if isinstance(data, dict) else data
    by_email: dict[str, str] = {}
    by_name: dict[str, str] = {}
    for u in users:
        # Ha nomes repetidos (usuario antigo inativo + novo ativo, ex. "Iuri
        # Lobato") - inativo nao pode virar dono.
        if u.get("active") is False:
            continue
        uid = u.get("_id") or u.get("id")
        if u.get("email"):
            by_email[u["email"].strip().lower()] = uid
        if u.get("name"):
            by_name.setdefault(norm_text(u["name"]), uid)
    return by_email, by_name


def match_user_by_name(name: str, users_by_name: dict[str, str]) -> Optional[str]:
    """Nome exato (normalizado) ou, se nao achar, primeiro + ultimo nome - o
    Omni costuma ter o nome curto ("Sabrina Dainhaia") e o RD o completo
    ("Sabrina Vargas Dainhaia"). So aceita se a correspondencia for unica."""
    wanted = norm_text(name)
    if wanted in users_by_name:
        return users_by_name[wanted]
    parts = wanted.split()
    if len(parts) < 2:
        return None
    matches = {
        uid
        for full, uid in users_by_name.items()
        if full.split() and full.split()[0] == parts[0] and full.split()[-1] == parts[-1]
    }
    return matches.pop() if len(matches) == 1 else None


# --- contatos / empresas / negociacoes ---------------------------------------


async def find_contacts_by_phone(client: httpx.AsyncClient, phone: str) -> list[dict]:
    """Todos os contatos com esse telefone. O RD tem contatos duplicados e
    telefones gravados em formatos diferentes, entao busca por algumas
    variacoes e confirma comparando a chave normalizada."""
    target = _phone_key(phone)
    found: dict[str, dict] = {}
    for variant in _phone_search_variants(phone):
        resp = await client.get(
            f"{config.RD_CRM_API_BASE}/contacts",
            params=_params(phone=variant, limit=20),
            timeout=30.0,
        )
        resp.raise_for_status()
        for contact in resp.json().get("contacts") or []:
            phones = [p.get("phone") for p in contact.get("phones") or []]
            if any(_phone_key(p) == target for p in phones):
                found[contact["_id"]] = contact
    return list(found.values())


def _org_tax_document(org: dict) -> Optional[str]:
    for field in org.get("custom_fields") or org.get("organization_custom_fields") or []:
        field_id = field.get("custom_field_id") or (field.get("custom_field") or {}).get("_id")
        if field_id == config.RD_CRM_ORG_TAX_DOC_FIELD_ID:
            return re.sub(r"\D", "", str(field.get("value") or "")) or None
    return None


async def find_organization(
    client: httpx.AsyncClient, name: str, tax_document: Optional[str]
) -> Optional[dict]:
    """Empresa pelo nome (a API nao busca pelo CNPJ). Se a empresa encontrada
    tiver Documento Fiscal preenchido, ele precisa bater com o do lead -
    senao e outra empresa com o mesmo nome."""
    resp = await client.get(
        f"{config.RD_CRM_API_BASE}/organizations",
        params=_params(q=name, limit=20),
        timeout=30.0,
    )
    resp.raise_for_status()
    orgs = resp.json().get("organizations") or []

    if tax_document:
        for org in orgs:
            if _org_tax_document(org) == tax_document:
                return org

    wanted = norm_text(name)
    for org in orgs:
        if norm_text(org.get("name")) != wanted:
            continue
        org_doc = _org_tax_document(org)
        if tax_document and org_doc and org_doc != tax_document:
            continue
        return org
    return None


async def create_organization(
    client: httpx.AsyncClient, name: str, tax_document: str, user_id: str
) -> dict:
    body = {
        "organization": {
            "name": name,
            "user_id": user_id,
            "organization_custom_fields": [
                {"custom_field_id": config.RD_CRM_ORG_TAX_DOC_FIELD_ID, "value": tax_document}
            ],
        }
    }
    resp = await client.post(
        f"{config.RD_CRM_API_BASE}/organizations", params=_params(), json=body, timeout=30.0
    )
    _raise_for_status(resp)
    return resp.json()


async def get_deal(client: httpx.AsyncClient, deal_id: str) -> dict:
    resp = await client.get(
        f"{config.RD_CRM_API_BASE}/deals/{deal_id}", params=_params(), timeout=30.0
    )
    resp.raise_for_status()
    return resp.json()


def _is_open(deal: dict) -> bool:
    """Criterio provisorio de "ativa": sem win e sem closed_at."""
    return deal.get("win") is None and not deal.get("closed_at")


async def _open_deal_in_pipeline(client: httpx.AsyncClient, deal_id: str) -> Optional[dict]:
    detail = await get_deal(client, deal_id)
    if _is_open(detail) and (detail.get("deal_pipeline") or {}).get("id") == config.RD_CRM_DEAL_PIPELINE_ID:
        return detail
    return None


async def find_existing_deal(
    client: httpx.AsyncClient,
    contacts: list[dict],
    organization: Optional[dict],
    owner_user_id: str,
) -> Optional[dict]:
    """Oportunidade ja existente no funil configurado ("3.Comercial Brasil").
    Regra do cliente: validar telefone, CNPJ/CPF e o dono da oportunidade -
    a mesma empresa costuma ter varias oportunidades com pessoas diferentes.

    1. Negociacao aberta de um contato com o MESMO TELEFONE -> e o mesmo lead.
    2. Negociacao aberta da MESMA EMPRESA com o MESMO RESPONSAVEL -> e o mesmo
       lead. Mesma empresa com outro responsavel NAO conta (cria card novo).
    Negociacoes em outros funis sao ignoradas."""
    for contact in contacts:
        for deal in contact.get("deals") or []:
            if _is_open(deal):
                found = await _open_deal_in_pipeline(client, deal["_id"])
                if found:
                    return found

    if organization:
        resp = await client.get(
            f"{config.RD_CRM_API_BASE}/deals",
            params=_params(organization=organization["_id"], limit=200),
            timeout=30.0,
        )
        resp.raise_for_status()
        for deal in resp.json().get("deals") or []:
            if not _is_open(deal):
                continue
            if ((deal.get("user") or {}).get("_id") or (deal.get("user") or {}).get("id")) != owner_user_id:
                continue
            found = await _open_deal_in_pipeline(client, deal["_id"])
            if found:
                return found
    return None


async def create_deal(
    client: httpx.AsyncClient,
    name: str,
    phone: str,
    email: Optional[str],
    user_id: str,
    organization_id: Optional[str],
) -> dict:
    def body(owner: str, full_contact: bool, with_org: bool) -> dict:
        contact: dict = {"name": name, "phones": [{"phone": phone, "type": "cellphone"}]}
        if full_contact:
            contact["phones"][0]["whatsapp"] = True
            if email:
                contact["emails"] = [{"email": email}]
        b: dict = {
            "deal": {
                "name": name,
                "deal_stage_id": config.RD_CRM_DEAL_STAGE_ID_LEAD,
                "user_id": owner,
                "deal_custom_fields": [],
            },
            "contacts": [contact],
        }
        if with_org and organization_id:
            b["organization"] = {"_id": organization_id}
        return b

    # Sem `deal_source`: com ele o RD responde 404 sem corpo no POST /deals,
    # mesmo com um ID valido ("Marketing - Whatsapp Omni", 608b18cd...,
    # confirmado via GET /deal_sources/{id} em 28/09/2026).
    #
    # O body completo (dono do Omni + whatsapp + email) deu 500 em 28/09. Pra
    # nao perder lead, tenta em etapas ate o body minimo que ja funcionava em
    # producao (dono padrao, so nome + telefone); cada recusa e logada, o que
    # mostra qual campo o RD nao aceita.
    attempts = [
        ("completo", body(user_id, True, True)),
        ("sem whatsapp/email", body(user_id, False, True)),
        ("minimo, dono padrao", body(config.RD_CRM_USER_ID, False, False)),
    ]
    for i, (label, payload) in enumerate(attempts):
        if i and payload == attempts[i - 1][1]:
            continue
        resp = await client.post(
            f"{config.RD_CRM_API_BASE}/deals", params=_params(), json=payload, timeout=30.0
        )
        if not resp.is_error:
            if i:
                logger.warning("Negociacao '%s' criada com o body '%s'.", name, label)
            return resp.json()
        logger.warning(
            "POST /deals (body '%s') recusado pra '%s': %s %s",
            label, name, resp.status_code, resp.text[:300],
        )
    _raise_for_status(resp)
    return resp.json()


async def create_annotation(
    client: httpx.AsyncClient, deal_id: str, text: str, user_id: str
) -> dict:
    """Cria uma anotacao (aba Historico da negociacao) via POST /activities."""
    body = {"activity": {"deal_id": deal_id, "text": text, "user_id": user_id}}
    resp = await client.post(
        f"{config.RD_CRM_API_BASE}/activities", params=_params(), json=body, timeout=30.0
    )
    resp.raise_for_status()
    return resp.json()
