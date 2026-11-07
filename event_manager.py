"""Server-Sent Events fan-out.

One bounded ``asyncio.Queue`` per connected client. Slow clients are dropped
(and their stream ended) instead of blocking the broadcaster or growing memory
without limit; the browser's EventSource reconnects and the frontend resyncs on
``open`` via ``GET /api/items``.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

logger = logging.getLogger("bifrost.events")

#: Heartbeat cadence; keeps proxies and idle connections alive.
HEARTBEAT_SECONDS = 15.0
#: How long a stream waits for a queued event before emitting a heartbeat.
QUEUE_POLL_TIMEOUT = HEARTBEAT_SECONDS


class _CloseSignal:
    """Sentinel pushed into a queue to wake up a stream that must end."""

    __slots__ = ()


CLOSE = _CloseSignal()


@dataclass
class Client:
    """A single SSE subscriber."""

    id: int
    queue: "asyncio.Queue[Any]" = field(default_factory=lambda: asyncio.Queue(maxsize=100))
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    disconnected: bool = False


@dataclass(frozen=True)
class Event:
    id: int
    type: str
    data: dict[str, Any]

    def encode(self) -> str:
        payload = json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))
        return f"id: {self.id}\nevent: {self.type}\ndata: {payload}\n\n"


class EventManager:
    """Tracks SSE clients and broadcasts item events to all of them."""

    def __init__(self, *, queue_size: int = 100, heartbeat_seconds: float = HEARTBEAT_SECONDS) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be at least 1")
        self.queue_size = queue_size
        self.heartbeat_seconds = heartbeat_seconds
        self._clients: dict[int, Client] = {}
        self._ids = itertools.count(1)
        self._event_ids = itertools.count(1)
        self._accepting = True
        self._lock = asyncio.Lock()

    # -------------------------------------------------------------- lifecycle
    @property
    def accepting(self) -> bool:
        return self._accepting

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def start(self) -> None:
        """Allow new registrations (called from lifespan startup)."""
        self._accepting = True

    def stop_accepting(self) -> None:
        """Refuse new SSE registrations without touching active streams."""
        self._accepting = False

    async def shutdown(self) -> None:
        """Stop accepting clients and close every active stream."""
        self._accepting = False
        async with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            self._close_client(client)

    # -------------------------------------------------------------- registry
    def register(self) -> Client:
        """Create a client and add it to the fan-out set."""
        if not self._accepting:
            raise RuntimeError("Event manager is shutting down.")
        client = Client(id=next(self._ids))
        client.queue = asyncio.Queue(maxsize=self.queue_size)
        self._clients[client.id] = client
        return client

    def unregister(self, client: Client) -> None:
        self._clients.pop(client.id, None)
        client.disconnected = True
        client.closed.set()

    def _close_client(self, client: Client) -> None:
        self._clients.pop(client.id, None)
        client.closed.set()
        with contextlib.suppress(asyncio.QueueFull):
            client.queue.put_nowait(CLOSE)

    # ------------------------------------------------------------ broadcasting
    def broadcast(self, event_type: str, data: dict[str, Any]) -> int:
        """Queue an event for every client. Returns the number of recipients."""
        event = Event(id=next(self._event_ids), type=event_type, data=data)
        delivered = 0
        for client in list(self._clients.values()):
            if client.closed.is_set():
                self._clients.pop(client.id, None)
                continue
            try:
                client.queue.put_nowait(event)
                delivered += 1
            except asyncio.QueueFull:
                # Never silently drop: evict the client so its browser
                # reconnects and resyncs from /api/items.
                logger.warning(
                    "SSE client %s fell behind (%s queued events); closing stream.",
                    client.id,
                    self.queue_size,
                )
                self._close_client(client)
        return delivered

    # ---------------------------------------------------------------- streams
    async def stream(self, client: Client) -> AsyncIterator[str]:
        """Yield encoded SSE frames until the client is closed or evicted."""
        try:
            # Tell browsers to retry quickly and send an initial comment so
            # proxies flush the headers straight away.
            yield "retry: 3000\n\n"
            yield ": connected\n\n"
            while True:
                if client.closed.is_set():
                    return
                try:
                    payload = await asyncio.wait_for(
                        client.queue.get(), timeout=self.heartbeat_seconds
                    )
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                except asyncio.CancelledError:
                    raise
                if payload is CLOSE or client.closed.is_set():
                    return
                yield payload.encode()  # type: ignore[union-attr]
        finally:
            self.unregister(client)

    async def stream_until_closed(self, client: Client, max_events: int | None = None) -> AsyncIterator[str]:
        """Test helper: like :meth:`stream` but stops after ``max_events``."""
        seen = 0
        async for frame in self.stream(client):
            yield frame
            if "\nevent: " in frame:
                seen += 1
                if max_events is not None and seen >= max_events:
                    return
