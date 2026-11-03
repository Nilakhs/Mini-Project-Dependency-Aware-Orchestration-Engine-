"""
WebSocket Connection Manager & Telemetry Broadcaster

Manages client connections to /ws/telemetry and relays live engine events from the EventBus.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Set

from fastapi import WebSocket, WebSocketDisconnect

from telemetry.event_bus import EventBus
from telemetry.events import TelemetryEvent

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manages active WebSockets and streams events."""

    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self.active_connections: Set[WebSocket] = set()
        self._queue: asyncio.Queue[TelemetryEvent] = self.event_bus.register_async_queue()
        self._broadcast_task: asyncio.Task | None = None

    async def connect(self, websocket: WebSocket) -> None:
        """Accept new WebSocket connection."""
        await websocket.accept()
        self.active_connections.add(websocket)
        logger.info(f"WebSocket client connected. Total clients: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket) -> None:
        """Remove disconnected WebSocket client."""
        self.active_connections.discard(websocket)
        logger.info(f"WebSocket client disconnected. Total clients: {len(self.active_connections)}")

    async def broadcast_json(self, data: dict) -> None:
        """Send JSON payload to all active clients."""
        text = json.dumps(data)
        dead = []
        for ws in self.active_connections:
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)

        for ws in dead:
            self.active_connections.discard(ws)

    def start_background_broadcaster(self) -> None:
        """Start async task to consume from EventBus and stream to WebSockets."""
        if self._broadcast_task is None or self._broadcast_task.done():
            self._broadcast_task = asyncio.create_task(self._broadcast_loop())

    async def stop_background_broadcaster(self) -> None:
        """Stop broadcast loop."""
        if self._broadcast_task and not self._broadcast_task.done():
            self._broadcast_task.cancel()
            try:
                await self._broadcast_task
            except asyncio.CancelledError:
                pass

    async def _broadcast_loop(self) -> None:
        """Continuously pulls events from event bus queue and pushes to clients."""
        while True:
            try:
                event = await self._queue.get()
                if self.active_connections:
                    await self.broadcast_json({"type": "EVENT", "data": event.to_dict()})
                self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in broadcast loop: {e}", exc_info=True)
