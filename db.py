"""Persistencia simples em SQLite - watermark do polling, dedup de conversas
ja processadas, e log de cada execucao. Sem ORM de proposito: e pouca coisa,
SQL direto e mais facil de inspecionar/depurar."""

import sqlite3
from contextlib import contextmanager
from typing import Iterator, Optional

import config

config.DATA_DIR.mkdir(parents=True, exist_ok=True)


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS poll_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_checked_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_chats (
                chat_id TEXT PRIMARY KEY,
                phone TEXT,
                name TEXT,
                action TEXT NOT NULL,
                rd_crm_deal_id TEXT,
                processed_at TEXT NOT NULL
            )
            """
        )
        # Um registro por (negociacao, etapa da cadencia): garante que cada
        # template e enviado no maximo uma vez por negociacao.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cadence_sends (
                deal_id TEXT NOT NULL,
                stage_id TEXT NOT NULL,
                deal_name TEXT,
                phone TEXT,
                action TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (deal_id, stage_id)
            )
            """
        )
        # Etapas que ja tiveram a "linha de base" gravada (ver cadence.py).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cadence_stages_seeded (
                stage_id TEXT PRIMARY KEY,
                seeded_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS poll_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                pages_fetched INTEGER DEFAULT 0,
                chats_scanned INTEGER DEFAULT 0,
                tag_matches INTEGER DEFAULT 0,
                deals_created INTEGER DEFAULT 0,
                skipped_existing_deal INTEGER DEFAULT 0,
                error TEXT
            )
            """
        )


def get_last_checked_at() -> Optional[str]:
    with get_connection() as conn:
        row = conn.execute("SELECT last_checked_at FROM poll_state WHERE id = 1").fetchone()
        return row["last_checked_at"] if row else None


def set_last_checked_at(value: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO poll_state (id, last_checked_at) VALUES (1, ?)
            ON CONFLICT(id) DO UPDATE SET last_checked_at = excluded.last_checked_at
            """,
            (value,),
        )


def is_chat_processed(chat_id: str) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_chats WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return row is not None


def mark_chat_processed(
    chat_id: str,
    phone: Optional[str],
    name: Optional[str],
    action: str,
    rd_crm_deal_id: Optional[str],
    processed_at: str,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO processed_chats
                (chat_id, phone, name, action, rd_crm_deal_id, processed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (chat_id, phone, name, action, rd_crm_deal_id, processed_at),
        )


def cadence_done_deal_ids(stage_id: str) -> set[str]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT deal_id FROM cadence_sends WHERE stage_id = ?", (stage_id,)
        ).fetchall()
        return {r["deal_id"] for r in rows}


def mark_cadence(
    deal_id: str,
    stage_id: str,
    deal_name: Optional[str],
    phone: Optional[str],
    action: str,
    detail: Optional[str],
    created_at: str,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO cadence_sends
                (deal_id, stage_id, deal_name, phone, action, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (deal_id, stage_id, deal_name, phone, action, detail, created_at),
        )


def is_cadence_stage_seeded(stage_id: str) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM cadence_stages_seeded WHERE stage_id = ?", (stage_id,)
        ).fetchone()
        return row is not None


def mark_cadence_stage_seeded(stage_id: str, seeded_at: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO cadence_stages_seeded (stage_id, seeded_at) VALUES (?, ?)",
            (stage_id, seeded_at),
        )


def get_recent_cadence(limit: int = 50, include_baseline: bool = False) -> list[dict]:
    where = "" if include_baseline else "WHERE action != 'linha_de_base'"
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM cadence_sends {where} ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def start_run(started_at: str) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO poll_runs (started_at) VALUES (?)", (started_at,)
        )
        return cur.lastrowid


def finish_run(
    run_id: int,
    finished_at: str,
    pages_fetched: int,
    chats_scanned: int,
    tag_matches: int,
    deals_created: int,
    skipped_existing_deal: int,
    error: Optional[str],
) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE poll_runs
            SET finished_at = ?, pages_fetched = ?, chats_scanned = ?,
                tag_matches = ?, deals_created = ?, skipped_existing_deal = ?, error = ?
            WHERE id = ?
            """,
            (
                finished_at,
                pages_fetched,
                chats_scanned,
                tag_matches,
                deals_created,
                skipped_existing_deal,
                error,
                run_id,
            ),
        )


def get_status() -> dict:
    with get_connection() as conn:
        state = conn.execute("SELECT last_checked_at FROM poll_state WHERE id = 1").fetchone()
        last_run = conn.execute(
            "SELECT * FROM poll_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        total_processed = conn.execute(
            "SELECT COUNT(*) AS n FROM processed_chats"
        ).fetchone()["n"]
        return {
            "last_checked_at": state["last_checked_at"] if state else None,
            "total_leads_processed": total_processed,
            "last_run": dict(last_run) if last_run else None,
        }


def get_recent_processed(limit: int = 50) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM processed_chats ORDER BY processed_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_recent_runs(limit: int = 20) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM poll_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
