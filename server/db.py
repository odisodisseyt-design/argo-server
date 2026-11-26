from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager
from typing import Any, Iterable

import aiosqlite

from .config import DB_PATH, settings

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'player',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS campaigns (
    id TEXT PRIMARY KEY,
    pack_id TEXT NOT NULL,
    name TEXT NOT NULL,
    state_json TEXT NOT NULL,
    step_no INTEGER NOT NULL DEFAULT 1,
    phase TEXT NOT NULL DEFAULT 'orders',
    pending_json TEXT NOT NULL DEFAULT '[]',
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS campaign_members (
    campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    character_json TEXT NOT NULL,
    is_founder INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active',
    joined_at TEXT NOT NULL DEFAULT (datetime('now')),
    left_at TEXT,
    PRIMARY KEY (campaign_id, user_id)
);

CREATE TABLE IF NOT EXISTS orders (
    campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    step_no INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    action_json TEXT NOT NULL,
    preview_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (campaign_id, step_no, user_id)
);

CREATE TABLE IF NOT EXISTS steps (
    campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    step_no INTEGER NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (campaign_id, step_no)
);

CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'canon',
    step_no INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS llm_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    campaign_id TEXT,
    step_no INTEGER,
    purpose TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt TEXT NOT NULL,
    response TEXT NOT NULL DEFAULT '',
    latency_ms INTEGER NOT NULL DEFAULT 0,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    action TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS client_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS friends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_a INTEGER NOT NULL,
    user_b INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_a, user_b)
);

CREATE TABLE IF NOT EXISTS invites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT NOT NULL,
    inviter INTEGER NOT NULL,
    invitee INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    payload TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS saves (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    state_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_ledger_campaign ON ledger(campaign_id);
CREATE INDEX IF NOT EXISTS idx_llm_campaign ON llm_logs(campaign_id, step_no);
CREATE INDEX IF NOT EXISTS idx_steps_campaign ON steps(campaign_id, step_no);
"""


async def _connect() -> aiosqlite.Connection:
    db = await aiosqlite.connect(settings.db_path, timeout=1.0)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys=ON")
    await db.execute("PRAGMA busy_timeout=1000")
    return db


async def init_db() -> None:
    settings.ensure_dirs()
    async with aiosqlite.connect(settings.db_path) as db:
        await db.executescript(SCHEMA)
        await db.commit()
    from .migrations import apply_migrations

    await apply_migrations()


def _row(row: aiosqlite.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


async def fetchone(sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
    db = await _connect()
    try:
        async with db.execute(sql, tuple(params)) as cur:
            return _row(await cur.fetchone())
    finally:
        await db.close()


async def fetchall(sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    db = await _connect()
    try:
        async with db.execute(sql, tuple(params)) as cur:
            return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def execute(sql: str, params: Iterable[Any] = ()) -> int:
    db = await _connect()
    try:
        cur = await db.execute(sql, tuple(params))
        await db.commit()
        return cur.lastrowid or 0
    finally:
        await db.close()


@asynccontextmanager
async def transaction(immediate: bool = False):
    db = await _connect()
    try:
        if immediate:
            await db.execute("BEGIN IMMEDIATE")
        yield db
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


def execute_sync(sql: str, params: Iterable[Any] = ()) -> None:
    """For synchronous audit writes from helpers that cannot await."""
    conn = sqlite3.connect(settings.db_path, timeout=1.0)
    try:
        conn.execute(sql, tuple(params))
        conn.commit()
    finally:
        conn.close()


async def ensure_column(table: str, column: str, declaration: str) -> None:
    rows = await fetchall(f"PRAGMA table_info({table})")
    if any(row["name"] == column for row in rows):
        return
    await execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default
