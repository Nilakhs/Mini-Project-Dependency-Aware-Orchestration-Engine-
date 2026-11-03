"""
Event Bus

Provides a thread-safe and async-safe publish/subscribe event distribution bus.
Allows multiple consumers (SQLite logger, live WebSocket broadcasters, metrics collector)
to receive TelemetryEvents simultaneously without coupling to the scheduler.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional, Set
from telemetry.events import TelemetryEvent

logger = logging.getLogger(__name__)


class EventBus:
    """
    Central Pub/Sub bus for build engine telemetry events.
    
    Supports:
    - Synchronous listener callbacks: Callable[[TelemetryEvent], None]
    - Asynchronous listener queues: asyncio.Queue[TelemetryEvent] for WebSocket broadcasting
    """

    def __init__(self):
        self._sync_listeners: Set[Callable[[TelemetryEvent], None]] = set()
        self._async_queues: Set[asyncio.Queue[TelemetryEvent]] = set()

    def subscribe(self, callback: Callable[[TelemetryEvent], None]) -> None:
        """Register a synchronous event consumer callback."""
        self._sync_listeners.add(callback)

    def unsubscribe(self, callback: Callable[[TelemetryEvent], None]) -> None:
        """Unregister a synchronous event consumer callback."""
        self._sync_listeners.discard(callback)

    def register_async_queue(self, maxsize: int = 1000) -> asyncio.Queue[TelemetryEvent]:
        """
        Create and register an async Queue for streaming events to WebSockets.
        """
        queue: asyncio.Queue[TelemetryEvent] = asyncio.Queue(maxsize=maxsize)
        self._async_queues.add(queue)
        return queue

    def unregister_async_queue(self, queue: asyncio.Queue[TelemetryEvent]) -> None:
        """Unregister an async queue when a client disconnects."""
        self._async_queues.discard(queue)

    def publish(self, event: TelemetryEvent) -> None:
        """
        Publish an event to all registered synchronous listeners and async queues.
        Exceptions raised by individual consumers are caught and logged so other
        consumers are never disrupted.
        """
        # 1. Notify synchronous subscribers
        for listener in list(self._sync_listeners):
            try:
                listener(event)
            except Exception as e:
                logger.error(f"Error in sync telemetry subscriber {listener}: {e}", exc_info=True)

        # 2. Push to asynchronous queues (e.g. WebSockets)
        for queue in list(self._async_queues):
            try:
                if queue.full():
                    try:
                        # Drop oldest event if queue is congested
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                queue.put_nowait(event)
            except Exception as e:
                logger.error(f"Error putting event to async queue: {e}", exc_info=True)

    def clear(self) -> None:
        """Clear all registered subscribers and queues."""
        self._sync_listeners.clear()
        self._async_queues.clear()
