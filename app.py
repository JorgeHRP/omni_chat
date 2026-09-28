import logging
import logging.handlers
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI

import config
import db
from poller import run_poll

config.LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(
            config.LOG_DIR / "app.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        ),
    ],
)
# O httpx loga a URL completa em INFO, e o token do RD vai na query string.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("leadsync")

scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    scheduler.add_job(run_poll, "interval", minutes=config.POLL_INTERVAL_MINUTES, id="poll_job")
    scheduler.start()
    logger.info("Scheduler iniciado - rodando a cada %s minutos.", config.POLL_INTERVAL_MINUTES)
    yield
    scheduler.shutdown()


app = FastAPI(title="Omni -> RD CRM Lead Sync", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/poll/run")
async def trigger_poll():
    """Dispara uma execucao do polling na hora, sem esperar o agendador."""
    return await run_poll()


@app.get("/poll/status")
async def poll_status():
    return db.get_status()


@app.get("/poll/runs")
async def poll_runs(limit: int = 20):
    return db.get_recent_runs(limit)


@app.get("/poll/leads")
async def poll_leads(limit: int = 50):
    return db.get_recent_processed(limit)
