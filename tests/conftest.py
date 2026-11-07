"""Shared pytest fixtures for the Bifrost backend test-suite.

Every test runs against a throwaway data directory; the real ``data/`` folder is
never touched and no user files are ever deleted by the suite.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterator
from urllib.parse import quote

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from auth import SystemClock  # noqa: E402
from config import Settings  # noqa: E402
from main import create_app  # noqa: E402

DEFAULT_DEVICE = "Test Device"
PLENTY_OF_SPACE = 1_000_000_000_000  # 1 TB


class FakeClock:
    """Deterministic clock: jumps forward instantly instead of sleeping."""

    def __init__(
        self,
        now: datetime | None = None,
        monotonic_start: float = 5_000.0,
    ) -> None:
        self._now = now or datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        self._monotonic = monotonic_start

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float = 0, *, days: float = 0) -> None:
        delta = timedelta(seconds=seconds, days=days)
        self._now = self._now + delta
        self._monotonic += delta.total_seconds()

    # Convenience aliases used by some tests.
    def advance_seconds(self, seconds: float) -> None:
        self.advance(seconds)

    def advance_days(self, days: float) -> None:
        self.advance(days=days)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def settings_factory(tmp_path: Path) -> Callable[..., Settings]:
    """Build isolated settings whose data dir lives inside ``tmp_path``."""

    def _make(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "data_dir": tmp_path / "data",
            "port": 8000,
            "quiet": True,
            # Keep the reserve small so tests can simulate full disks easily
            # while still exercising the real reserve logic.
            "min_free_space_bytes": 1024,
        }
        values.update(overrides)
        settings = Settings(**values)
        settings.validate()
        return settings

    return _make


@pytest.fixture
def settings(settings_factory: Callable[..., Settings]) -> Settings:
    return settings_factory()


@pytest.fixture
def app_factory(
    settings_factory: Callable[..., Settings], clock: FakeClock
) -> Callable[..., Any]:
    """Create applications with a controllable free-space provider."""

    def _make(
        settings: Settings | None = None,
        *,
        free_bytes: int = PLENTY_OF_SPACE,
        clock_override: FakeClock | None = None,
        **kwargs: Any,
    ):
        resolved = settings or settings_factory()
        return create_app(
            resolved,
            clock=clock_override or clock,
            show_banner=False,
            free_space_provider=lambda _path: free_bytes,
            **kwargs,
        )

    return _make


@pytest.fixture
def app(app_factory: Callable[..., Any]):
    return app_factory()


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    """Authenticated-capable client that looks like a host-local browser."""
    with TestClient(
        app,
        base_url="http://localhost:8000",
        client=("127.0.0.1", 50000),
    ) as test_client:
        yield test_client


@pytest.fixture
def remote_client(app) -> Iterator[TestClient]:
    """Client whose peer address is not loopback (simulates a phone on the LAN)."""
    with TestClient(
        app,
        base_url="http://192.168.1.50:8000",
        client=("192.168.1.50", 51000),
    ) as test_client:
        yield test_client


def login(
    client: TestClient,
    app=None,
    *,
    device: str = DEFAULT_DEVICE,
    pin: str | None = None,
):
    """Authenticate ``client`` against the running app."""
    resolved_pin = pin or app.state.bifrost.pin
    return client.post("/api/auth", json={"pin": resolved_pin, "device": device})


@pytest.fixture
def authed_client(client: TestClient, app) -> TestClient:
    response = login(client, app)
    assert response.status_code == 200, response.text
    return client


@pytest.fixture
def authed_remote_client(remote_client: TestClient, app) -> TestClient:
    response = login(remote_client, app, device="Android Phone")
    assert response.status_code == 200, response.text
    return remote_client


# --------------------------------------------------------------------- helpers
def device_header(name: str) -> str:
    return quote(name, safe="")


def upload(
    client: TestClient,
    data: bytes,
    *,
    filename: str = "note.txt",
    device: str = DEFAULT_DEVICE,
    mime: str = "application/octet-stream",
    extra_headers: dict[str, str] | None = None,
):
    headers = {
        "x-filename": quote(filename, safe=""),
        "x-device-name": device_header(device),
        "content-type": mime,
    }
    if extra_headers:
        headers.update(extra_headers)
    return client.post("/api/upload", content=data, headers=headers)


def make_image_bytes(
    size: tuple[int, int] = (640, 480),
    *,
    fmt: str = "PNG",
    color: tuple[int, int, int] = (200, 40, 60),
) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, fmt)
    return buffer.getvalue()


def run_async(coroutine):
    """Run a coroutine in a fresh event loop (no pytest-asyncio needed)."""
    return asyncio.run(coroutine)


async def collect(iterator: AsyncIterator[str], limit: int) -> list[str]:
    frames: list[str] = []
    async for frame in iterator:
        frames.append(frame)
        if len(frames) >= limit:
            break
    return frames


@pytest.fixture
def isolated_client(settings_factory, app_factory):
    """Factory for a logged-in client on its own app/settings/data dir.

    Yields ``(client, app, settings)`` and closes every started app on teardown.
    """
    stack = contextlib.ExitStack()

    def _make(
        *,
        free_bytes: int = PLENTY_OF_SPACE,
        device: str = DEFAULT_DEVICE,
        **overrides: Any,
    ):
        resolved = settings_factory(**overrides)
        app = app_factory(resolved, free_bytes=free_bytes)
        client = stack.enter_context(
            TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 50000))
        )
        response = login(client, app, device=device)
        assert response.status_code == 200, response.text
        return client, app, resolved

    try:
        yield _make
    finally:
        stack.close()


@pytest.fixture
def system_clock() -> SystemClock:
    return SystemClock()


def wait_for(condition, timeout: float = 5.0, interval: float = 0.05) -> bool:
    """Block until ``condition()`` is truthy or ``timeout`` expires."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return bool(condition())


def raw_call(
    app,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    client: tuple[str, int] = ("127.0.0.1", 50000),
) -> tuple[int, dict[str, str], bytes]:
    """Drive the ASGI app directly so tests control header values exactly.

    Used for cases httpx refuses to construct, such as a Content-Length that
    does not match the body (which is how the 5 GB rejection is verified).
    """
    header_list = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method.upper(),
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": header_list,
        "client": client,
        "server": ("localhost", 8000),
        "scheme": "http",
    }

    state = {"sent": False}
    start: dict[str, Any] = {}
    chunks: list[bytes] = []

    async def receive() -> dict[str, Any]:
        if not state["sent"]:
            state["sent"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            start["status"] = message["status"]
            start["headers"] = {
                key.decode("latin-1"): value.decode("latin-1")
                for key, value in message.get("headers", [])
            }
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    asyncio.run(app(scope, receive, send))
    return start.get("status", 0), start.get("headers", {}), b"".join(chunks)
