import os
from pathlib import Path

from dotenv import load_dotenv

# Le o .env que fica ao lado deste arquivo (raiz do servico). Em producao no
# EasyPanel as variaveis normalmente sao injetadas direto no container (nao
# tem .env no build) - load_dotenv nao encontra o arquivo e nao faz nada, e
# os.environ ja tem tudo vindo da configuracao da plataforma.
load_dotenv(Path(__file__).resolve().parent / ".env")

OMNI_API_KEY = os.environ["OMNI_API_KEY"]
OMNI_API_SECRET = os.environ["OMNI_API_SECRET"]
OMNI_CHATS_API_BASE = os.environ.get("OMNI_CHATS_API_BASE", "https://api.omni.chat/v1")
OMNI_TAG_LABEL_ID = os.environ["OMNI_TAG_LABEL_ID"]
OMNI_TAG_LABEL_NAME = os.environ.get("OMNI_TAG_LABEL_NAME", "Lead qualificado")
# Template do link direto pra conversa no painel do Omni - o `{chat_id}` e
# substituido pelo objectId do chat (formato confirmado no painel em 09/2026).
OMNI_CHAT_URL_TEMPLATE = os.environ.get(
    "OMNI_CHAT_URL_TEMPLATE", "https://app.omni.chat/#/home/chat/{chat_id}"
)
# Quantas mensagens da conversa puxar pra montar a anotacao de historico.
OMNI_HISTORY_MESSAGE_LIMIT = int(os.environ.get("OMNI_HISTORY_MESSAGE_LIMIT", "300"))
# Corta o texto da anotacao de historico neste tamanho (protege contra
# conversas gigantes / limite de tamanho do campo no RD CRM).
RD_CRM_ANNOTATION_MAX_CHARS = int(os.environ.get("RD_CRM_ANNOTATION_MAX_CHARS", "15000"))

RD_CRM_API_BASE = os.environ.get("RD_CRM_API_BASE", "https://crm.rdstation.com/api/v1")
RD_CRM_TOKEN = os.environ["RD_CRM_TOKEN"]
# Dono padrao da negociacao - usado so quando o atendente do chat no Omni nao
# tem usuario correspondente (por email ou nome) no RD CRM.
RD_CRM_USER_ID = os.environ["RD_CRM_USER_ID"]
RD_CRM_DEAL_STAGE_ID_LEAD = os.environ["RD_CRM_DEAL_STAGE_ID_LEAD"]
# Funil onde a negociacao e criada e onde checamos se ja existe uma ativa
# (padrao: "3.Comercial Brasil"). Negociacoes em outros funis sao ignoradas.
RD_CRM_DEAL_PIPELINE_ID = os.environ.get("RD_CRM_DEAL_PIPELINE_ID", "692f81682d85a9001e8b504e")
# Campo personalizado da EMPRESA "Documento Fiscal (CNPJ, CPF, ...)" -
# obrigatorio no RD, so digitos.
RD_CRM_ORG_TAX_DOC_FIELD_ID = os.environ.get(
    "RD_CRM_ORG_TAX_DOC_FIELD_ID", "5fd26d3e5cc1db00123f7dcd"
)

POLL_INTERVAL_MINUTES = int(os.environ.get("POLL_INTERVAL_MINUTES", "10"))
INITIAL_LOOKBACK_HOURS = int(os.environ.get("INITIAL_LOOKBACK_HOURS", "2"))
MAX_PAGES_PER_RUN = int(os.environ.get("MAX_PAGES_PER_RUN", "20"))

DATA_DIR = Path(__file__).resolve().parent / "data"
LOG_DIR = Path(__file__).resolve().parent / "logs"
DB_PATH = DATA_DIR / "leadsync.db"
