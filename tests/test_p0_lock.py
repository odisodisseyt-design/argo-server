import asyncio

import pytest

from server.locks import LockBusy, campaign_lock


async def test_second_lock_is_busy():
    async with campaign_lock("camp-1"):
        with pytest.raises(LockBusy):
            async with campaign_lock("camp-1"):
                pass
        await asyncio.sleep(0)


async def test_lock_released_after_exception():
    with pytest.raises(RuntimeError):
        async with campaign_lock("camp-2"):
            raise RuntimeError("boom")
    async with campaign_lock("camp-2"):
        pass


async def test_different_campaigns_do_not_block():
    async with campaign_lock("a"):
        async with campaign_lock("b"):
            pass
