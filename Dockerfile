# Versao exata do Python usada no projeto
FROM python:3.12.7-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Copia so as dependencias primeiro (aproveita o cache de camada)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia o restante do codigo
COPY . .

# Pontos de montagem dos volumes persistentes (watermark/dedup + logs)
RUN mkdir -p /app/data /app/logs

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:80/health').status==200 else 1)"

# IMPORTANTE: -w 1 (um unico worker). O agendador (APScheduler) roda dentro do
# processo; com varios workers cada um sobe um scheduler e o polling roda em
# duplicidade/triplicidade. Nao aumente o numero de workers.
CMD ["gunicorn", "app:app", "-w", "1", "-k", "uvicorn.workers.UvicornWorker", "-b", "0.0.0.0:80", "--timeout", "120", "--access-logfile", "-", "--forwarded-allow-ips", "*"]
