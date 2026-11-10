"""Pushes monitoring updates to connected WebSocket clients."""

import asyncio
import logging
import json
from typing import Set
from fastapi import WebSocket

logger = logging.getLogger(__name__)


class MonitoringBroadcaster:
    """Monitoring sockets; the metrics collector calls broadcast() after each cycle."""

    def __init__(self):
        self._clients: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)
        logger.debug(f"Monitoring WS client connected ({len(self._clients)} total)")

    async def disconnect(self, ws: WebSocket):
        async with self._lock:
            self._clients.discard(ws)
        logger.debug(f"Monitoring WS client disconnected ({len(self._clients)} total)")

    async def broadcast(self, data: dict):
        """Send `data` as JSON to every client, dropping clients the send fails on."""
        async with self._lock:
            clients = list(self._clients)
        if not clients:
            return

        payload = json.dumps(data, default=str)
        stale: list[WebSocket] = []
        for ws in clients:
            try:
                await ws.send_text(payload)
            except Exception:
                stale.append(ws)

        if stale:
            async with self._lock:
                for ws in stale:
                    self._clients.discard(ws)
            logger.debug(f"Removed {len(stale)} stale monitoring WS clients")

    @property
    def client_count(self) -> int:
        return len(self._clients)


_broadcaster = MonitoringBroadcaster()


def get_monitoring_broadcaster() -> MonitoringBroadcaster:
    return _broadcaster
