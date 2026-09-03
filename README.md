# lead-sync-service

Servico FastAPI que substitui o fluxo n8n de polling por tag
(`n8n-omni-rdcrm-polling-tag-workflow.json`). Mesma logica, sem os problemas
de loop/paired-item do n8n, com estado real em SQLite e logs em arquivo.

Esta pasta e a raiz do que sobe pro deploy (Docker/EasyPanel) - nao tem
subpasta `app/`, os modulos Python ficam direto aqui.

## O que ele faz

A cada `POLL_INTERVAL_MINUTES` (padrao 10 min), roda automaticamente:

1. Busca no Omni (`GET /chats`) as conversas atualizadas desde a ultima
   execucao, paginando de verdade ate alcancar o presente (ou um limite de
   seguranca de `MAX_PAGES_PER_RUN` paginas).
2. Filtra as que tem a tag `OMNI_TAG_LABEL_ID` ("Lead qualificado").
3. Pra cada conversa nova (ainda nao processada): busca o telefone no RD CRM.
   - Se ja existe negociacao ativa -> nao faz nada, so registra.
   - Se nao existe -> cria a negociacao no RD CRM.
4. Guarda tudo no SQLite (`data/leadsync.db`): ate onde ja checou, quais
   conversas ja foram processadas (pra nunca duplicar), e um log de cada
   execucao.

## Contexto (por que essa API e nao outra)

- A API publica do CDP (`cdp-api.omni.chat` / Segments/Audiences) **nao
  funciona pra esse caso** - testado ao vivo, a base de clientes do CDP e
  separada da base de conversas reais.
- A API certa e `https://api.omni.chat/v1/chats`, documentada em
  `api-docs.omni.chat` (nao confundir com `developers.omni.chat`, que so
  cobre o modulo de marketing/CDP). Cada conversa ja vem com `labels`.
- Nao existe filtro `?label=` na listagem - por isso filtramos no proprio
  codigo, depois de buscar todas as conversas atualizadas na janela.
- ID real da tag "Lead qualificado" confirmado ao vivo: `3mrDpvo1iMed`.

## Rodar local (sem Docker)

```bash
cd lead-sync-service
python -m venv .venv
.venv/Scripts/activate   # Windows
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```

Copie `.env.example` pra `.env` e preencha (ou use o `.env` ja presente
nesta pasta, que ja vem com os valores reais usados nos testes).

## Rodar com Docker (local)

```bash
cd lead-sync-service
docker build -t lead-sync-service .
docker run --rm -p 8000:80 --env-file .env -v $(pwd)/data:/app/data lead-sync-service
# app fica em http://localhost:8000 (o container escuta na porta 80)
```

## Deploy no EasyPanel

1. Aponte o EasyPanel pra essa pasta (`lead-sync-service/`) como raiz do
   build - ela ja tem o `Dockerfile` pronto.
2. **Nao suba o `.env`** (esta no `.gitignore` e no `.dockerignore` de
   proposito - nao vai pro repositorio nem pra imagem). Em vez disso,
   configure as mesmas variaveis do `.env.example` direto na tela de
   variaveis de ambiente do EasyPanel.
3. **Monte um volume persistente** apontando pra `/app/data` dentro do
   container. Sem isso, o SQLite (`leadsync.db` - watermark do polling +
   historico de leads ja processados) some a cada redeploy e o servico
   recomeça do zero (janela de `INITIAL_LOOKBACK_HOURS` novamente, risco de
   reprocessar/duplicar).
4. Porta exposta: `80` (no `EXPOSE` do Dockerfile e no bind do gunicorn).
   Aponte o proxy do EasyPanel pra ela.
5. **Nao aumente o numero de workers.** O `Dockerfile` roda gunicorn com
   `-w 1` de proposito: o agendador (APScheduler) roda dentro do processo;
   com 2+ workers cada um sobe um scheduler e o polling roda em duplicidade.

## Endpoints

- `GET /health` - checagem simples.
- `POST /poll/run` - dispara uma execucao na hora (nao precisa esperar o
  agendador), util pra testar. Se ja tiver uma execucao em andamento (agendada
  ou manual), responde `{"skipped": "ja em execucao"}` e nao roda de novo.
- `GET /poll/status` - ultimo estado: ate onde ja checou, total de leads
  processados, resumo da ultima execucao.
- `GET /poll/runs?limit=20` - historico das execucoes (paginas buscadas,
  quantos chats escaneados, quantos com a tag, quantos criados/pulados,
  erro se algum).
- `GET /poll/leads?limit=50` - lista das conversas ja processadas e o que
  aconteceu com cada uma (`negociacao_criada`, `ja_tem_negociacao`,
  `sem_telefone`, `erro`). Uma conversa marcada como `erro` (falha ao consultar
  ou criar no RD CRM) nao e retentada automaticamente - fica registrada aqui e
  no log; pra reprocessar, apague a linha correspondente em `processed_chats`.

Logs tambem vao pra `logs/app.log` (rotaciona em 5MB, mantem 3 arquivos) -
mesma recomendacao de volume persistente se quiser manter historico entre
deploys.

## Ja validado ao vivo (01/09/2026)

- Contato sem negociacao ativa -> cria negociacao nova (testado com "Fran").
- Contato com negociacao ativa -> pula, nao duplica (testado com "Jorge
  Henrique").
- Paginacao real (sem os bugs de "paired item" do n8n) - confirmado
  buscando 300+ conversas em multiplas paginas na mesma execucao.

## O que ainda falta antes de considerar 100% pronto

1. `create_deal` cria negociacao REAL no RD CRM de producao - ja usado em
   teste, mas ainda vale revisar o criterio de "negociacao ativa"
   (`has_active_deal` em `rdcrm_client.py`, hoje: sem `win` e sem
   `closed_at`) com quem decide a regra de negocio real.
2. **Duplicidade de contato no RD CRM**: durante os testes, criar uma
   negociacao pra um telefone que ja tinha contato (mas sem negociacao
   ativa porque foi excluida manualmente) gerou um CONTATO duplicado no RD
   CRM (mesmo telefone, dois `_id` diferentes) em vez de reaproveitar o
   contato existente. Vale investigar se `POST /deals` do RD CRM tem algum
   parametro de "upsert por telefone" ou se a busca (`GET /contacts?phone=`)
   precisa de outro formato de telefone.
3. Sem autenticacao nos endpoints - se for exposto na internet (nao so
   localhost/rede interna do EasyPanel), adicionar alguma protecao antes.
