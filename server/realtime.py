from __future__ import annotations

import asyncio
from typing import Any

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self) -> None:
        self.rooms: dict[str, dict[int, set[WebSocket]]] = {}
        self.lock = asyncio.Lock()

    async def connect(self, campaign_id: str, user_id: int, ws: WebSocket,
                      subprotocol: str | None = None) -> None:
        await ws.accept(subprotocol=subprotocol)
        async with self.lock:
            self.rooms.setdefault(campaign_id, {}).setdefault(user_id, set()).add(ws)

    async def disconnect(self, campaign_id: str, user_id: int, ws: WebSocket) -> None:
        async with self.lock:
            users = self.rooms.get(campaign_id, {})
            sockets = users.get(user_id, set())
            sockets.discard(ws)
            if not sockets:
                users.pop(user_id, None)
            if not users:
                self.rooms.pop(campaign_id, None)

    async def send(self, campaign_id: str, user_id: int, event: str, payload: dict[str, Any]) -> None:
        async with self.lock:
            sockets = list(self.rooms.get(campaign_id, {}).get(user_id, set()))
        message = {"event": event, "payload": payload}
        for ws in sockets:
            try:
                await ws.send_json(message)
            except Exception:
                pass

    async def broadcast(self, campaign_id: str, event: str, payload: dict[str, Any]) -> None:
        async with self.lock:
            users = list(self.rooms.get(campaign_id, {}).items())
        for user_id, sockets in users:
            for ws in list(sockets):
                try:
                    await ws.send_json({"event": event, "payload": payload})
                except Exception:
                    pass


manager = ConnectionManager()
