"""SSE event manager tests plus endpoint-level security checks.

The manager is tested directly with finite streams so the suite can never hang
on an endless SSE response.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from event_manager import EventManager


def _data_of(frame: str) -> dict:
    for line in frame.splitlines():
        if line.startswith("data: "):
            return json.loads(line[len("data: "):])
    raise AssertionError(f"no data line in frame: {frame!r}")


def test_register_and_added_event_delivery():
    async def scenario():
        manager = EventManager(queue_size=10, heartbeat_seconds=0.05)
        manager.start()
        client = manager.register()
        assert manager.client_count == 1

        delivered = manager.broadcast("added", {"id": "abc", "kind": "file", "name": "a.txt"})
        assert delivered == 1

        frames = []
        async for frame in manager.stream_until_closed(client, max_events=1):
            frames.append(frame)
        return frames, manager

    frames, manager = asyncio.run(scenario())
    events = [frame for frame in frames if "\nevent: " in frame]
    assert len(events) == 1
    assert "event: added" in events[0]
    assert _data_of(events[0])["name"] == "a.txt"
    # The stream ends and unregisters itself.
    assert manager.client_count == 0


def test_deleted_event_delivery():
    async def scenario():
        manager = EventManager(queue_size=10, heartbeat_seconds=0.05)
        manager.start()
        client = manager.register()
        manager.broadcast("deleted", {"id": "x1", "kind": "note"})
        frames = []
        async for frame in manager.stream_until_closed(client, max_events=1):
            frames.append(frame)
        return frames

    frames = asyncio.run(scenario())
    event = next(frame for frame in frames if "\nevent: " in frame)
    assert "event: deleted" in event
    assert _data_of(event) == {"id": "x1", "kind": "note"}


def test_disconnected_client_is_removed():
    async def scenario():
        manager = EventManager(queue_size=4, heartbeat_seconds=0.05)
        manager.start()
        first = manager.register()
        second = manager.register()
        manager.unregister(first)
        return manager, second

    manager, second = asyncio.run(scenario())
    assert manager.client_count == 1
    assert manager.broadcast("added", {"id": "1"}) == 1
    manager.unregister(second)
    assert manager.client_count == 0


def test_bounded_queue_drops_slow_client():
    async def scenario():
        manager = EventManager(queue_size=2, heartbeat_seconds=0.05)
        manager.start()
        slow = manager.register()
        # Three events with nobody consuming the queue.
        results = [manager.broadcast("added", {"id": str(i)}) for i in range(3)]
        return manager, slow, results

    manager, slow, results = asyncio.run(scenario())
    assert manager.client_count == 0
    assert slow.closed.is_set()
    # Overflowing client is evicted instead of silently losing events.
    assert results[-1] == 0


def test_slow_client_does_not_block_others():
    async def scenario():
        manager = EventManager(queue_size=2, heartbeat_seconds=0.05)
        manager.start()
        slow = manager.register()
        healthy = manager.register()
        # Simulate a browser that stopped reading its stream.
        slow.queue.put_nowait(object())
        slow.queue.put_nowait(object())
        delivered = manager.broadcast("added", {"id": "x"})
        return manager, slow, healthy, delivered

    manager, slow, healthy, delivered = asyncio.run(scenario())
    assert delivered == 1
    assert slow.closed.is_set()
    assert not healthy.closed.is_set()
    assert healthy.queue.qsize() == 1
    manager.unregister(healthy)


def test_stream_is_closed_when_client_is_dropped():
    """An overflowing client's stream must end promptly (browser reconnects)."""

    async def scenario():
        manager = EventManager(queue_size=1, heartbeat_seconds=5.0)
        manager.start()
        client = manager.register()

        async def consume():
            frames = []
            async for frame in manager.stream(client):
                frames.append(frame)
            return frames

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.02)  # let the generator start
        manager.broadcast("added", {"id": "1"})
        manager.broadcast("added", {"id": "2"})  # overflows the 1-slot queue
        frames = await asyncio.wait_for(task, timeout=2)
        return frames, manager, client

    frames, manager, client = asyncio.run(scenario())
    assert frames  # initial retry/comment frames were emitted
    assert client.closed.is_set()
    assert manager.client_count == 0


def test_shutdown_closes_active_clients():
    async def scenario():
        manager = EventManager(queue_size=10, heartbeat_seconds=0.05)
        manager.start()
        client = manager.register()
        received: list[str] = []

        async def consume():
            async for frame in manager.stream(client):
                received.append(frame)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.02)
        await manager.shutdown()
        await asyncio.wait_for(task, timeout=2)
        return manager, received

    manager, received = asyncio.run(scenario())
    assert manager.accepting is False
    assert manager.client_count == 0
    assert received  # the stream produced at least the initial comment


def test_register_after_shutdown_is_refused():
    async def scenario():
        manager = EventManager()
        manager.start()
        await manager.shutdown()
        with pytest.raises(RuntimeError):
            manager.register()

    asyncio.run(scenario())


def test_heartbeats_are_emitted_when_idle():
    async def scenario():
        manager = EventManager(queue_size=10, heartbeat_seconds=0.01)
        manager.start()
        client = manager.register()

        async def consume():
            frames = []
            async for frame in manager.stream(client):
                frames.append(frame)
                if any("heartbeat" in frame for frame in frames):
                    break
            return frames

        frames = await asyncio.wait_for(consume(), timeout=2)
        manager.unregister(client)
        return frames

    frames = asyncio.run(scenario())
    assert any(frame.startswith(": heartbeat") for frame in frames)


# --------------------------------------------------------------- endpoint level
def test_sse_endpoint_requires_authentication(client):
    response = client.get("/api/events")
    assert response.status_code == 401
    assert response.json() == {"error": "Authentication required."}


def test_sse_endpoint_refuses_connections_while_shutting_down(authed_client, app):
    app.state.bifrost.events.stop_accepting()
    response = authed_client.get("/api/events")
    assert response.status_code == 503
    assert app.state.bifrost.events.client_count == 0
    app.state.bifrost.events.start()


def test_upload_broadcasts_added_event(authed_client, app):
    from tests.conftest import upload

    manager = app.state.bifrost.events
    client = manager.register()
    try:
        upload(authed_client, b"event payload", filename="event.txt")
        assert client.queue.qsize() == 1
        queued = client.queue.get_nowait()
        assert queued.type == "added"
        assert queued.data["name"] == "event.txt"
    finally:
        manager.unregister(client)


def test_delete_broadcasts_deleted_event(authed_client, app):
    from tests.conftest import upload

    item = upload(authed_client, b"bye", filename="bye.txt").json()["item"]
    manager = app.state.bifrost.events
    client = manager.register()
    try:
        assert authed_client.delete(f"/api/items/{item['id']}").status_code == 200
        events = []
        while not client.queue.empty():
            events.append(client.queue.get_nowait())
        assert [event.type for event in events] == ["deleted"]
        assert events[0].data["id"] == item["id"]
    finally:
        manager.unregister(client)
