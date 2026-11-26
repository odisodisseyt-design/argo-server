"""Small ordered migration runner.

Each migration is idempotent, so the same list can be applied to a fresh
database and to an existing one. The SQL below is deliberately written in the
same shape Alembic operations would produce, so it can be transferred to
PostgreSQL later without changing business logic.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from . import db

Migration = tuple[str, Callable[[], Awaitable[None]]]


async def _turn_idempotency() -> None:
    await db.execute(
        """CREATE TABLE IF NOT EXISTS turn_idempotency (
               key TEXT PRIMARY KEY,
               campaign_id TEXT NOT NULL,
               user_id INTEGER NOT NULL,
               response_json TEXT NOT NULL,
               created_at TEXT NOT NULL DEFAULT (datetime('now'))
           )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_idem_created ON turn_idempotency(created_at)"
    )


async def _rate_limits() -> None:
    await db.execute(
        """CREATE TABLE IF NOT EXISTS rate_limits (
               user_id INTEGER NOT NULL,
               bucket TEXT NOT NULL,
               window_start INTEGER NOT NULL,
               count INTEGER NOT NULL DEFAULT 0,
               PRIMARY KEY (user_id, bucket, window_start)
           )"""
    )


async def _model_version() -> None:
    await db.ensure_column("llm_logs", "model_version", "TEXT NOT NULL DEFAULT ''")


async def _campaign_model() -> None:
    await db.ensure_column(
        "campaigns", "model_id", "TEXT NOT NULL DEFAULT 'fast'"
    )


async def _campaign_events() -> None:
    await db.execute(
        """CREATE TABLE IF NOT EXISTS campaign_events (
               seq INTEGER PRIMARY KEY AUTOINCREMENT,
               campaign_id TEXT NOT NULL,
               user_id INTEGER,
               event TEXT NOT NULL,
               payload_json TEXT NOT NULL,
               created_at TEXT NOT NULL DEFAULT (datetime('now'))
           )"""
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_campaign ON campaign_events(campaign_id, seq)"
    )


async def _p1_campaign_fields() -> None:
    await db.ensure_column(
        "campaigns", "pack_version", "TEXT NOT NULL DEFAULT ''"
    )
    await db.ensure_column(
        "campaigns", "language", "TEXT NOT NULL DEFAULT 'ru'"
    )
    await db.ensure_column(
        "campaigns", "order_deadline_ts", "TEXT"
    )


async def _resolution_fencing() -> None:
    """A claim carries a unique token; only its owner may commit the step."""
    await db.ensure_column(
        "campaigns", "resolution_id", "TEXT NOT NULL DEFAULT ''"
    )
    await db.ensure_column(
        "campaigns", "resolution_expires_at", "TEXT"
    )


async def _order_clarifications() -> None:
    """A pending clarification survives restarts and keeps the original text."""
    await db.execute(
        """CREATE TABLE IF NOT EXISTS order_clarifications (
               id TEXT PRIMARY KEY,
               campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
               user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
               step_no INTEGER NOT NULL,
               original_text TEXT NOT NULL,
               question TEXT NOT NULL,
               status TEXT NOT NULL DEFAULT 'pending',
               answer_response_json TEXT,
               created_at TEXT NOT NULL DEFAULT (datetime('now')),
               expires_at TEXT NOT NULL
           )"""
    )
    await db.execute(
        """CREATE INDEX IF NOT EXISTS idx_clarification_owner
           ON order_clarifications(campaign_id, user_id, step_no, status)"""
    )


MIGRATIONS: list[Migration] = [
    ("0001_turn_idempotency", _turn_idempotency),
    ("0002_rate_limits", _rate_limits),
    ("0003_llm_model_version", _model_version),
    ("0004_campaign_model", _campaign_model),
    ("0005_campaign_events", _campaign_events),
    ("0006_p1_campaign_fields", _p1_campaign_fields),
    ("0007_resolution_fencing", _resolution_fencing),
    ("0008_order_clarifications", _order_clarifications),
]


async def apply_migrations() -> None:
    await db.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version TEXT PRIMARY KEY,
               applied_at TEXT NOT NULL DEFAULT (datetime('now'))
           )"""
    )
    applied = {
        row["version"]
        for row in await db.fetchall("SELECT version FROM schema_migrations")
    }
    for version, migrate in MIGRATIONS:
        if version in applied:
            continue
        await migrate()
        await db.execute(
            "INSERT INTO schema_migrations(version) VALUES(?)", (version,)
        )
