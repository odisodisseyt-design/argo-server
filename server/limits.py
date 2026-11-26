"""Per-player rate limits and campaign slot accounting."""
from __future__ import annotations

import time

from . import db
from .config import settings


class RateLimited(Exception):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__("rate limit exceeded")


class CampaignLimit(Exception):
    pass


async def check_turn_rate(user_id: int) -> None:
    now = int(time.time())
    minute = now - (now % 60)
    hour = now - (now % 3600)
    windows = (
        ("turn_minute", minute, settings.orders_per_minute, 60),
        ("turn_hour", hour, settings.orders_per_hour, 3600),
    )
    async with db.transaction(immediate=True) as conn:
        for bucket, start, limit, span in windows:
            await conn.execute(
                """INSERT OR IGNORE INTO rate_limits(user_id,bucket,window_start,count)
                   VALUES(?,?,?,0)""",
                (user_id, bucket, start),
            )
            await conn.execute(
                """UPDATE rate_limits SET count=count+1
                   WHERE user_id=? AND bucket=? AND window_start=?""",
                (user_id, bucket, start),
            )
            cur = await conn.execute(
                "SELECT count FROM rate_limits WHERE user_id=? AND bucket=? AND window_start=?",
                (user_id, bucket, start),
            )
            row = await cur.fetchone()
            count = int(row["count"]) if row else 0
            if count > limit:
                raise RateLimited(max(1, start + span - now))
        # lazy cleanup of old windows
        await conn.execute(
            "DELETE FROM rate_limits WHERE window_start < ?",
            (hour - 7200,),
        )


async def assert_campaign_slot(user_id: int) -> None:
    row = await db.fetchone(
        """SELECT COUNT(*) AS n FROM campaign_members
           WHERE user_id=? AND status='active'""",
        (user_id,),
    )
    if row and int(row["n"]) >= settings.max_active_campaigns:
        raise CampaignLimit()


async def assert_campaign_slot_conn(conn, user_id: int) -> None:
    cur = await conn.execute(
        """SELECT COUNT(*) AS n FROM campaign_members
           WHERE user_id=? AND status='active'""",
        (user_id,),
    )
    row = await cur.fetchone()
    if row and int(row["n"]) >= settings.max_active_campaigns:
        raise CampaignLimit()
