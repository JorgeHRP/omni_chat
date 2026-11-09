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

# Extracao dos dados do card (empresa, CNPJ/CPF, email) de dentro da conversa
# via OpenAI, pra quando o atendente nao preencheu o cadastro no Omni. Vazio =
# desligado.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
# Fixo de proposito (decisao do cliente) - nao vem do ambiente.
OPENAI_MODEL = "gpt-4.1-mini"
OPENAI_API_BASE = os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1")
# Quanto da conversa (fim dela, em caracteres) vai pro modelo.
OPENAI_MAX_INPUT_CHARS = int(os.environ.get("OPENAI_MAX_INPUT_CHARS", "30000"))

# Cadencia de WhatsApp: quando uma negociacao do funil entra numa etapa da
# cadencia (T1/T3/T4 CONTATO), manda o template correspondente pelo Omni.
# Desligado por padrao - mensagem pra cliente nao tem volta.
CADENCE_WHATSAPP_ENABLED = os.environ.get("CADENCE_WHATSAPP_ENABLED", "false").lower() == "true"
# "etapa_id:campanha_id" separados por virgula. Padrao: T1º/T3º/T4º CONTATO do
# "3.Comercial Brasil" -> campanhas "Cadencia RD T1/T3/T4 (automatica - nao
# apagar)" do Omni (template + imagem + numero 55 54 3022-5800). As "v2" foram
# recriadas em 08/11/2026 com a chave nova: a campanha fica presa ao usuario da
# chave que a criou, e as de 04/11 pararam de enviar quando a chave foi trocada.
CADENCE_STAGE_CAMPAIGNS = dict(
    pair.strip().split(":", 1)
    for pair in os.environ.get(
        "CADENCE_STAGE_CAMPAIGNS",
        "6a16c675ada558002202465b:hZ24VxW1wS,"
        "6a16c69715fbf90021c23be0:KPG4dpM3q3,"
        "6a1f5bfd5c7da5001d843a5c:kKI1Z6sq3E",
    ).split(",")
    if pair.strip()
)
# Trava de seguranca: no maximo N envios por execucao.
CADENCE_MAX_SENDS_PER_RUN = int(os.environ.get("CADENCE_MAX_SENDS_PER_RUN", "50"))

POLL_INTERVAL_MINUTES = int(os.environ.get("POLL_INTERVAL_MINUTES", "10"))
INITIAL_LOOKBACK_HOURS = int(os.environ.get("INITIAL_LOOKBACK_HOURS", "2"))
MAX_PAGES_PER_RUN = int(os.environ.get("MAX_PAGES_PER_RUN", "20"))

# Persistencia no Supabase (tabelas leadsync_* - ver supabase_schema.sql).
# Sem SUPABASE_URL cai no SQLite local em data/ (bom pra dev; em producao o
# arquivo some a cada deploy). Use a chave service_role (a anon esbarra em RLS).
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = next(
    (
        os.environ[name]
        for name in ("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_KEY", "SUPABASE_SERVICE_KEY", "SUPABASE_ANON_KEY")
        if os.environ.get(name)
    ),
    "",
)

DATA_DIR = Path(__file__).resolve().parent / "data"
LOG_DIR = Path(__file__).resolve().parent / "logs"
DB_PATH = DATA_DIR / "leadsync.db"
