"""Bifrost - self-hosted local network file and text sharing.

Run with a single worker:

    python -m uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1 \
        --timeout-graceful-shutdown 10 --no-access-log

Everything is process-local (PIN, rate limiter, SSE clients), which is why the
server must run with exactly one worker.
"""

from __future__ import annotations

import ipaddress
import logging
import math
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable
from urllib.parse import quote, unquote

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect

from auth import (
    COOKIE_NAME,
    DEFAULT_DEVICE_NAME,
    Clock,
    PinRateLimiter,
    SessionManager,
    SystemClock,
    generate_pin,
    is_six_digit_pin,
    pin_matches,
    sanitize_device_name,
)
from config import PROJECT_DIR, Settings
from database import Database, Item, Session, utc_now_iso
from event_manager import EventManager
from file_manager import FileManager, UploadError, is_valid_item_id
from network import LOOPBACK_HOSTNAMES, build_urls, detect_lan_ip, print_banner
from thumbnail_manager import ThumbnailManager, ThumbnailUnavailable

logger = logging.getLogger("bifrost")

STATIC_DIR = PROJECT_DIR / "static"

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "img-src 'self' data: blob:; "
    "media-src 'self'; "
    "style-src 'self'; "
    "script-src 'self'; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


class ApiError(Exception):
    """Client-facing error that renders as ``{"error": "..."}``."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass
class AppState:
    """Everything shared between route handlers."""

    settings: Settings
    clock: Clock
    db: Database
    sessions: SessionManager
    rate_limiter: PinRateLimiter
    files: FileManager
    thumbnails: ThumbnailManager
    events: EventManager
    show_banner: bool = True
    pin: str = ""
    lan_ip: str = "127.0.0.1"
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------- small helpers
def _state(request: Request) -> AppState:
    return request.app.state.bifrost  # type: ignore[no-any-return]


def _client_ip(request: Request) -> str:
    """Real peer address. Never trusts ``X-Forwarded-For``."""
    client = request.client
    if client is None:
        return "unknown"
    return client.host or "unknown"


def _is_loopback_host(host: str) -> bool:
    candidate = host.strip().lower()
    if candidate.startswith("::ffff:"):
        candidate = candidate[len("::ffff:") :]
    if candidate in LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def _split_host_header(value: str) -> tuple[str, int | None]:
    """Parse a ``Host`` header into ``(hostname, port)``."""
    value = value.strip().lower()
    if not value:
        raise ValueError("empty host")
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            raise ValueError("malformed IPv6 host")
        hostname = value[1:end]
        rest = value[end + 1 :]
        if rest.startswith(":"):
            port_text = rest[1:]
            if not port_text.isdigit():
                raise ValueError("malformed port")
            return hostname, int(port_text)
        if rest:
            raise ValueError("malformed host")
        return hostname, None
    if value.count(":") == 1:
        hostname, port_text = value.rsplit(":", 1)
        if not port_text.isdigit():
            raise ValueError("malformed port")
        return hostname, int(port_text)
    return value, None


def _parse_origin(value: str) -> tuple[str, str, int | None]:
    """Parse an Origin header into ``(scheme, hostname, port)``."""
    scheme, _, rest = value.partition("://")
    if not rest or scheme not in {"http", "https"}:
        raise ValueError("malformed origin")
    hostname, port = _split_host_header(rest)
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, hostname, port


def enforce_loopback(request: Request, settings: Settings) -> None:
    """Reject everything that is not a genuine host-local request."""
    if not _is_loopback_host(_client_ip(request)):
        raise ApiError(403, "This endpoint is only available on the host machine.")

    host_header = request.headers.get("host")
    if not host_header:
        raise ApiError(403, "This endpoint is only available on the host machine.")
    try:
        hostname, port = _split_host_header(host_header)
    except ValueError as exc:
        raise ApiError(403, "This endpoint is only available on the host machine.") from exc
    if hostname not in LOOPBACK_HOSTNAMES:
        raise ApiError(403, "This endpoint is only available on the host machine.")
    if port is None:
        port = 80
    if port != settings.port:
        raise ApiError(403, "This endpoint is only available on the host machine.")

    origin = request.headers.get("origin")
    if origin:
        try:
            scheme, origin_host, origin_port = _parse_origin(origin)
        except ValueError as exc:
            raise ApiError(403, "This endpoint is only available on the host machine.") from exc
        if (
            origin_host not in LOOPBACK_HOSTNAMES
            or origin_port != settings.port
            or scheme not in {"http", "https"}
        ):
            raise ApiError(403, "This endpoint is only available on the host machine.")


def _decode_header_value(raw: str | None) -> str | None:
    """Percent-decode a UTF-8 header value (headers themselves are latin-1)."""
    if raw is None:
        return None
    try:
        return unquote(raw, encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, ValueError):
        return raw


def _resolve_sender(request: Request, session: Session, settings: Settings) -> str:
    """Sender attribution from ``X-Device-Name``, falling back to

    the device stored with the session. Never used for authorisation.
    """
    header = _decode_header_value(request.headers.get("x-device-name"))
    if header is not None:
        cleaned = sanitize_device_name(
            header,
            max_chars=settings.max_device_name_chars,
            fallback=session.device or DEFAULT_DEVICE_NAME,
        )
        return cleaned
    return sanitize_device_name(
        session.device,
        max_chars=settings.max_device_name_chars,
        fallback=DEFAULT_DEVICE_NAME,
    )


def _content_disposition(filename: str) -> str:
    """RFC 6266/5987 attachment header with an ASCII fallback."""
    ascii_name = filename.encode("ascii", "replace").decode("ascii")
    ascii_name = ascii_name.replace("\\", "_").replace('"', "'")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


async def _read_json_object(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as exc:
        raise ApiError(400, "Request body must be valid JSON.") from exc
    if not isinstance(payload, dict):
        raise ApiError(400, "Request body must be a JSON object.")
    return payload


def _note_label(text: str) -> str:
    first_line = text.strip().splitlines()[0] if text.strip() else "Note"
    collapsed = " ".join(first_line.split())
    if not collapsed:
        return "Note"
    return collapsed[:80]


def _broadcast_added(state: AppState, item: Item) -> None:
    state.events.broadcast("added", item.to_public())


def create_app(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    show_banner: bool | None = None,
    free_space_provider: Callable[[Path], int] | None = None,
) -> FastAPI:
    """Application factory.

    ``settings``/``clock`` are injectable so the test-suite can use temporaries
    and a fake clock.
    """
    config = settings or Settings.from_env()
    config.validate()
    time_source = clock or SystemClock()
    banner_enabled = (not config.quiet) if show_banner is None else show_banner

    db = Database(config.db_path)
    state = AppState(
        settings=config,
        clock=time_source,
        db=db,
        sessions=SessionManager(db, time_source, session_days=config.session_days),
        rate_limiter=PinRateLimiter(
            time_source,
            max_attempts=config.pin_attempts,
            lockout_seconds=config.lockout_seconds,
        ),
        files=FileManager(config, db, free_space_provider=free_space_provider),
        thumbnails=ThumbnailManager(config),
        events=EventManager(queue_size=100),
        show_banner=banner_enabled,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        state.settings.ensure_directories()
        await run_in_threadpool(state.db.init_schema)
        state.pin = generate_pin()
        state.lan_ip, state.warnings = await run_in_threadpool(
            detect_lan_ip, config.host_ip
        )
        partials, orphans = await run_in_threadpool(state.files.cleanup)
        item_ids = await run_in_threadpool(state.db.item_ids)
        thumbs_removed = await run_in_threadpool(state.thumbnails.cleanup_orphans, item_ids)
        state.events.start()
        if state.show_banner:
            print_banner(config, pin=state.pin, ip=state.lan_ip, warnings=state.warnings)
        logger.info(
            "Bifrost started with %d item(s); removed %d partial, %d orphan and"
            " %d stale thumbnail file(s).",
            len(item_ids),
            partials,
            orphans,
            thumbs_removed,
        )
        try:
            yield
        finally:
            await state.events.shutdown()
            await state.files.wait_for_background_tasks()
            logger.info("Bifrost stopped.")

    app = FastAPI(
        title="Bifrost",
        description="Self-hosted local network file and text sharing.",
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.bifrost = state

    # ------------------------------------------------------------ middleware
    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        headers = response.headers
        headers.setdefault("x-content-type-options", "nosniff")
        headers.setdefault("referrer-policy", "no-referrer")
        headers.setdefault("x-frame-options", "DENY")
        headers.setdefault(
            "permissions-policy", "camera=(), microphone=(), geolocation=()"
        )
        headers.setdefault("content-security-policy", CONTENT_SECURITY_POLICY)
        if config.cookie_secure:
            headers.setdefault(
                "strict-transport-security", "max-age=31536000; includeSubDomains"
            )
        path = request.url.path
        if path.startswith("/api/"):
            if not path.startswith(("/api/thumb/", "/api/files/")):
                headers.setdefault("cache-control", "no-store")
        elif path == "/" or path.endswith(".html"):
            headers.setdefault("cache-control", "no-cache")
        return response

    # ------------------------------------------------------- error handling
    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            {"error": exc.message},
            status_code=exc.status_code,
            headers={"cache-control": "no-store"},
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed."
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                {"error": detail},
                status_code=exc.status_code,
                headers=exc.headers or None,
            )
        return JSONResponse({"error": detail}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse({"error": "Invalid request."}, status_code=400)

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error: %s", type(exc).__name__)
        return JSONResponse(
            {"error": "Unexpected server error."},
            status_code=500,
            headers={"cache-control": "no-store"},
        )

    # ----------------------------------------------------------- dependencies
    async def require_session(request: Request) -> Session:
        raw_token = request.cookies.get(COOKIE_NAME)
        session = await run_in_threadpool(state.sessions.validate, raw_token)
        if session is None:
            raise ApiError(401, "Authentication required.")
        return session

    # ------------------------------------------------------------------ pages
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "index.html",
            media_type="text/html; charset=utf-8",
            headers={"cache-control": "no-cache"},
        )

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> FileResponse:
        return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")

    # ------------------------------------------------------------------- auth
    @app.post("/api/auth")
    async def authenticate(request: Request) -> JSONResponse:
        payload = await _read_json_object(request)
        submitted = payload.get("pin")
        device_raw = payload.get("device")

        if not isinstance(submitted, str):
            raise ApiError(400, "A six-digit PIN is required.")
        pin = submitted.strip()
        if not is_six_digit_pin(pin):
            raise ApiError(400, "A six-digit PIN is required.")

        client_ip = _client_ip(request)
        remaining = state.rate_limiter.lockout_remaining(client_ip)
        if remaining > 0:
            seconds = max(1, math.ceil(remaining))
            raise ApiError(
                429, f"Too many incorrect attempts. Try again in {seconds} seconds."
            )

        if not pin_matches(pin, state.pin):
            locked_out = state.rate_limiter.record_failure(client_ip)
            if locked_out:
                seconds = max(1, math.ceil(state.rate_limiter.lockout_remaining(client_ip)))
                raise ApiError(
                    429,
                    "Too many incorrect attempts."
                    f" Try again in {seconds} seconds.",
                )
            raise ApiError(401, "Incorrect PIN.")

        state.rate_limiter.record_success(client_ip)
        device = sanitize_device_name(
            device_raw if isinstance(device_raw, str) else None,
            max_chars=config.max_device_name_chars,
            fallback=DEFAULT_DEVICE_NAME,
        )
        await run_in_threadpool(state.sessions.prune_expired)
        token, session = await run_in_threadpool(state.sessions.create, device)

        response = JSONResponse(
            {
                "ok": True,
                "device": session.device,
                "expires_at": session.expires_at,
            }
        )
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=config.cookie_max_age,
            httponly=True,
            samesite="lax",
            secure=config.cookie_secure,
            path="/",
        )
        return response

    @app.get("/api/pairing-info")
    async def pairing_info(request: Request) -> JSONResponse:
        enforce_loopback(request, config)
        urls = build_urls(state.lan_ip, config.port, state.pin)
        return JSONResponse(
            {
                "pin": state.pin,
                "port": config.port,
                "local_url": urls["local"],
                "lan_url": urls["lan"],
            },
            headers={"cache-control": "no-store"},
        )

    # ------------------------------------------------------------------ inbox
    @app.get("/api/items")
    async def list_items(session: Session = Depends(require_session)) -> JSONResponse:
        items = await run_in_threadpool(state.db.list_items)
        return JSONResponse(
            {"items": [item.to_public() for item in items], "server_time": utc_now_iso()},
            headers={"cache-control": "no-store"},
        )

    @app.post("/api/upload")
    async def upload(
        request: Request, session: Session = Depends(require_session)
    ) -> JSONResponse:
        raw_filename = _decode_header_value(request.headers.get("x-filename"))
        if not raw_filename:
            raise ApiError(400, "An X-Filename header is required.")

        sender = _resolve_sender(request, session, config)

        declared_length: int | None = None
        raw_length = request.headers.get("content-length")
        if raw_length is not None:
            try:
                declared_length = int(raw_length)
            except ValueError as exc:
                raise ApiError(400, "Invalid Content-Length header.") from exc
            if declared_length < 0:
                raise ApiError(400, "Invalid Content-Length header.")

        try:
            item = await state.files.receive_upload(
                request.stream(),
                filename=raw_filename,
                sender=sender,
                declared_length=declared_length,
                declared_mime=request.headers.get("content-type"),
            )
        except ClientDisconnect as exc:
            # The .part file has already been removed by receive_upload().
            raise ApiError(400, "Upload was cancelled.") from exc
        except UploadError as exc:
            raise ApiError(exc.status_code, exc.message) from exc

        _broadcast_added(state, item)
        return JSONResponse({"item": item.to_public()}, status_code=201)

    @app.post("/api/note")
    async def create_note(
        request: Request, session: Session = Depends(require_session)
    ) -> JSONResponse:
        payload = await _read_json_object(request)
        text = payload.get("text")
        if not isinstance(text, str):
            raise ApiError(400, "Note text is required.")
        if not text.strip():
            raise ApiError(400, "Note cannot be empty.")
        if len(text) > config.max_note_chars:
            raise ApiError(
                400,
                "Note exceeds the maximum of"
                f" {config.max_note_chars} characters.",
            )

        sender = _resolve_sender(request, session, config)
        item = await run_in_threadpool(
            lambda: state.db.insert_note(text=text, name=_note_label(text), sender=sender)
        )
        _broadcast_added(state, item)
        return JSONResponse({"item": item.to_public()}, status_code=201)

    # -------------------------------------------------------------- downloads
    @app.get("/api/files/{item_id}")
    async def download(
        item_id: str, session: Session = Depends(require_session)
    ) -> FileResponse:
        if not is_valid_item_id(item_id):
            raise ApiError(404, "File not found.")
        item = await run_in_threadpool(state.db.get_item, item_id)
        if item is None or item.kind != "file" or not item.stored_name:
            raise ApiError(404, "File not found.")
        try:
            path = state.files.resolve_stored_path(item.stored_name)
        except ValueError as exc:
            raise ApiError(404, "File not found.") from exc
        if not await run_in_threadpool(path.is_file):
            raise ApiError(404, "File not found.")

        state.files.begin_download(item.id)
        return FileResponse(
            path,
            media_type=item.mime or "application/octet-stream",
            headers={
                "content-disposition": _content_disposition(item.name),
                "cache-control": "private, no-store",
                "x-content-type-options": "nosniff",
            },
            background=BackgroundTask(state.files.end_download, item.id),
        )

    @app.get("/api/thumb/{item_id}")
    async def thumbnail(
        item_id: str, session: Session = Depends(require_session)
    ) -> FileResponse:
        if not is_valid_item_id(item_id):
            raise ApiError(404, "Thumbnail not found.")
        item = await run_in_threadpool(state.db.get_item, item_id)
        if item is None or item.kind != "file" or not item.stored_name:
            raise ApiError(404, "Thumbnail not found.")
        try:
            source = state.files.resolve_stored_path(item.stored_name)
        except ValueError as exc:
            raise ApiError(404, "Thumbnail not found.") from exc
        if not await run_in_threadpool(source.is_file):
            raise ApiError(404, "Thumbnail not found.")

        try:
            cached = await run_in_threadpool(
                state.thumbnails.get_or_create, item.id, source
            )
        except ThumbnailUnavailable as exc:
            raise ApiError(exc.status_code, exc.message) from exc

        return FileResponse(
            cached,
            media_type="image/jpeg",
            headers={
                "cache-control": "private, max-age=604800",
                "x-content-type-options": "nosniff",
            },
        )

    # --------------------------------------------------------------- deletion
    @app.delete("/api/items/{item_id}")
    async def delete_item(
        item_id: str, session: Session = Depends(require_session)
    ) -> JSONResponse:
        if not is_valid_item_id(item_id):
            raise ApiError(404, "Item not found.")

        def _broadcast(item: Item) -> None:
            state.events.broadcast(
                "deleted", {"id": item.id, "kind": item.kind, "name": item.name}
            )

        item = await state.files.delete_item(
            item_id,
            on_deleted=_broadcast,
            thumbnail_remover=state.thumbnails.remove,
        )
        if item is None:
            raise ApiError(404, "Item not found.")
        return JSONResponse({"ok": True, "id": item.id})

    # ------------------------------------------------------------------- SSE
    @app.get("/api/events")
    async def events(
        request: Request, session: Session = Depends(require_session)
    ) -> StreamingResponse:
        if not state.events.accepting:
            raise ApiError(503, "Server is shutting down.")
        try:
            client = state.events.register()
        except RuntimeError as exc:
            raise ApiError(503, "Server is shutting down.") from exc

        stream: AsyncIterator[str] = state.events.stream(client)
        return StreamingResponse(
            stream,
            media_type="text/event-stream",
            headers={
                "cache-control": "no-store",
                "connection": "keep-alive",
                "x-accel-buffering": "no",
            },
        )

    return app


app = create_app()
