"""Crash-safe inter-process lock for a campaign step.

SQLite has no advisory lock, so we use a per-campaign file lock. It is released
by the OS when the process dies, which makes it safe for systemd restarts and
for a separate jobs process. PostgreSQL replacement: keep the same public API
and call pg_try_advisory_xact_lock(hashtextextended(campaign_id, 0)) inside the
resolution transaction.
"""
from __future__ import annotations

import fcntl
import hashlib
from contextlib import asynccontextmanager
from typing import AsyncIterator

from .config import settings


class LockBusy(Exception):
    def __init__(self, retry_after: int | None = None) -> None:
        self.retry_after = retry_after or settings.lock_retry_after
        super().__init__("campaign is busy")


def _path(campaign_id: str):
    digest = hashlib.sha256(campaign_id.encode("utf-8")).hexdigest()[:32]
    settings.locks_dir.mkdir(parents=True, exist_ok=True)
    return settings.locks_dir / f"{digest}.lock"


@asynccontextmanager
async def campaign_lock(campaign_id: str) -> AsyncIterator[None]:
    """Serialize step resolution across processes. Never hold it during LLM calls."""
    path = _path(campaign_id)
    handle = path.open("a+b")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise LockBusy() from exc
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
