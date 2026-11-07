"""Raw streaming uploads, downloads and safe storage on disk.

The public surface is intentionally small:

* :func:`sanitize_filename` - Windows-safe, traversal-proof display names.
* :func:`safe_extension` - extension whitelist for physical storage names.
* :class:`FileManager` - stream to a ``.part`` file, atomically promote, persist,
  resolve storage paths, and delete files without failing on Windows locks.

Uploaded bytes are written with ``aiofiles`` in roughly 1 MB writes; the request
body is consumed with ``Request.stream()`` so no multipart parser and no
framework-level spooling is involved.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import unicodedata
import uuid
from pathlib import Path
from typing import AsyncIterator, Callable

import aiofiles
from starlette.concurrency import run_in_threadpool

from config import Settings
from database import Database, Item

#: Characters Windows forbids in file names.
_INVALID_CHARS = set('<>:"/\\|?*')

#: Windows reserved device names (reserved with or without an extension).
_RESERVED_NAMES = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

_EXTENSION_RE = re.compile(r"^\.[A-Za-z0-9]{1,10}$")
_MIME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*$")
_ITEM_ID_RE = re.compile(r"^[0-9a-f]{32}$")

#: How often the free-space reserve is re-checked during an upload.
_DISK_CHECK_INTERVAL_BYTES = 64 * 1024 * 1024


# --------------------------------------------------------------------- errors
class UploadError(Exception):
    """Base class for upload failures that map onto HTTP status codes."""

    status_code = 400
    default_message = "Upload rejected."

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.default_message)
        self.message = message or self.default_message


class InvalidUpload(UploadError):
    status_code = 400
    default_message = "Invalid upload request."


class UploadTooLarge(UploadError):
    status_code = 413
    default_message = "File exceeds the maximum allowed size."


class InsufficientStorage(UploadError):
    status_code = 507
    default_message = "Not enough free storage to accept this file."


# ------------------------------------------------------------------ filenames
def split_extension(name: str) -> tuple[str, str]:
    """Split ``name`` into ``(stem, extension)``; extension includes the dot."""
    dot = name.rfind(".")
    if dot <= 0 or dot == len(name) - 1:
        return name, ""
    extension = name[dot:]
    if not _EXTENSION_RE.match(extension):
        return name, ""
    return name[:dot], extension


def _truncate(name: str, max_chars: int) -> str:
    if len(name) <= max_chars:
        return name
    stem, extension = split_extension(name)
    if extension and len(extension) + 2 < max_chars:
        keep = max_chars - len(extension) - 2
        stem = stem[:keep].rstrip(" .")
        return f"{stem}{extension}"
    return name[:max_chars].rstrip(" .")


def sanitize_filename(
    raw: str | None,
    *,
    max_chars: int = 150,
    default: str = "file",
) -> str:
    """Return a safe display/download filename.

    Handles Windows path separators, control characters, invalid characters,
    reserved device names, trailing dots/spaces and length limits while
    preserving non-ASCII text (Hindi, emoji, ...).
    """
    if raw is None:
        return default
    text = unicodedata.normalize("NFC", str(raw))
    if not text:
        return default

    # 1. Normalise both separators, then take the basename. This strips every
    #    path component, including '../..' traversal attempts.
    text = text.replace("\\", "/")
    text = text.split("/")[-1]

    # 2. Drop unassigned / surrogate / control characters; keep the emoji ZWJ.
    kept: list[str] = []
    for char in text:
        category = unicodedata.category(char)
        if char == "\u200d":
            kept.append(char)
        elif category in {"Cc", "Cs", "Co", "Cn", "Cf"}:
            kept.append(" ")
        else:
            kept.append(char)
    text = "".join(kept)

    # 3. Replace Windows-invalid characters instead of silently deleting them.
    text = "".join("_" if char in _INVALID_CHARS else char for char in text)

    # 4. Trim whitespace and Windows-hostile trailing dots/spaces.
    text = text.strip()
    while text and text[-1] in {" ", "."}:
        text = text[:-1]
    text = text.lstrip(" ")

    if not text or text in {".", "..", "/", "\\"}:
        return default

    # 5. Windows reserved device names, with or without an extension.
    first_segment = text.split(".")[0].strip().upper()
    if first_segment in _RESERVED_NAMES:
        text = f"_{text}"

    # 6. Length limit in Unicode code points, keeping the extension intact.
    text = _truncate(text, max_chars)
    text = unicodedata.normalize("NFC", text)
    if not text:
        return default
    return text


def safe_extension(display_name: str) -> str:
    """Return a lowercase, whitelisted extension (including the dot) or ''."""
    _, extension = split_extension(display_name)
    if not extension:
        return ""
    extension = extension.lower()
    if not _EXTENSION_RE.match(extension):
        return ""
    return extension


def guess_mime(display_name: str, declared: str | None = None) -> str:
    """Best-effort MIME type from the display name, falling back to a header."""
    import mimetypes

    guessed, _ = mimetypes.guess_type(display_name)
    if guessed:
        return guessed
    if declared:
        candidate = declared.split(";", 1)[0].strip()
        if _MIME_RE.match(candidate):
            return candidate.lower()
    return "application/octet-stream"


def is_valid_item_id(value: str | None) -> bool:
    return bool(value) and bool(_ITEM_ID_RE.match(value or ""))


class FileManager:
    """Owns everything that touches the files directory."""

    def __init__(
        self,
        settings: Settings,
        db: Database,
        *,
        free_space_provider: Callable[[Path], int] | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.files_dir = settings.files_dir
        self._free_space_provider = free_space_provider or (
            lambda path: shutil.disk_usage(path).free
        )
        self._downloads: dict[str, int] = {}
        self._background_tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------- utilities
    @property
    def max_upload_bytes(self) -> int:
        return self.settings.max_upload_bytes

    def free_bytes(self) -> int:
        return int(self._free_space_provider(self.files_dir))

    def _ensure_free(self, extra_bytes: int = 0) -> None:
        free = self.free_bytes()
        if free < extra_bytes + self.settings.min_free_space_bytes:
            raise InsufficientStorage(
                "Not enough free disk space to accept this upload."
            )

    def resolve_stored_path(self, stored_name: str | None) -> Path:
        """Resolve a physical file path, guaranteeing it stays inside files/."""
        if not stored_name:
            raise ValueError("Item has no stored file.")
        name = Path(stored_name).name
        base = self.files_dir.resolve()
        candidate = (base / name).resolve()
        if candidate.parent != base:
            raise ValueError("Resolved path escapes the files directory.")
        return candidate

    # --------------------------------------------------------------- uploads
    async def receive_upload(
        self,
        stream: AsyncIterator[bytes],
        *,
        filename: str | None,
        sender: str,
        declared_length: int | None = None,
        declared_mime: str | None = None,
    ) -> Item:
        """Stream a raw request body to disk and persist its metadata.

        Raises :class:`UploadError` subclasses for client-facing failures. Any
        failure removes the temporary ``.part`` file; no orphaned bytes remain.
        """
        display_name = sanitize_filename(
            filename, max_chars=self.settings.max_filename_chars
        )
        if not display_name:
            raise InvalidUpload("A filename is required.")

        if declared_length is not None:
            if declared_length < 0:
                raise InvalidUpload("Invalid Content-Length header.")
            if declared_length > self.max_upload_bytes:
                raise UploadTooLarge(
                    "File exceeds the maximum allowed size of"
                    f" {_human_bytes(self.max_upload_bytes)}."
                )
            self._ensure_free(declared_length)
        else:
            self._ensure_free(0)

        token = uuid.uuid4().hex
        part_path = self.files_dir / f"{token}.part"
        extension = safe_extension(display_name)
        stored_name = f"{token}{extension}"
        final_path = self.files_dir / stored_name
        moved = False
        received = 0
        next_disk_check = _DISK_CHECK_INTERVAL_BYTES

        try:
            async with aiofiles.open(part_path, "wb") as handle:
                async for chunk in stream:
                    if not chunk:
                        continue
                    received += len(chunk)
                    if received > self.max_upload_bytes:
                        raise UploadTooLarge(
                            "File exceeds the maximum allowed size of"
                            f" {_human_bytes(self.max_upload_bytes)}."
                        )
                    await handle.write(chunk)
                    if received >= next_disk_check:
                        next_disk_check = received + _DISK_CHECK_INTERVAL_BYTES
                        self._ensure_free(0)
                await handle.flush()
                self._fsync(handle)

            if declared_length is not None and received != declared_length:
                raise InvalidUpload("Upload was incomplete. Please retry.")

            # Promote the finished upload atomically; the UUID name is unique so
            # existing files are never overwritten.
            await run_in_threadpool(os.replace, part_path, final_path)
            moved = True

            mime = guess_mime(display_name, declared_mime)
            item = await run_in_threadpool(
                lambda: self.db.insert_file(
                    item_id=token,
                    name=display_name,
                    stored_name=stored_name,
                    size=received,
                    mime=mime,
                    sender=sender,
                )
            )
            return item
        except BaseException:
            # Client disconnect, cancellation, size/space failure or a failing
            # database insert: leave no partial artefacts behind.
            with contextlib.suppress(OSError):
                await run_in_threadpool(_unlink_quietly, part_path)
            if moved:
                with contextlib.suppress(OSError):
                    await run_in_threadpool(_unlink_quietly, final_path)
            raise

    @staticmethod
    def _fsync(handle: object) -> None:
        """Best-effort fsync without letting platform quirks break an upload."""
        try:
            fileno_value = handle.fileno()  # type: ignore[attr-defined]
            if asyncio.iscoroutine(fileno_value):  # pragma: no cover - aiofiles
                return
            os.fsync(fileno_value)
        except (OSError, ValueError, AttributeError):  # pragma: no cover - platform
            pass

    # -------------------------------------------------------------- downloads
    def begin_download(self, item_id: str) -> None:
        self._downloads[item_id] = self._downloads.get(item_id, 0) + 1

    def end_download(self, item_id: str) -> None:
        remaining = self._downloads.get(item_id, 0) - 1
        if remaining > 0:
            self._downloads[item_id] = remaining
        else:
            self._downloads.pop(item_id, None)

    def active_downloads(self, item_id: str) -> int:
        return self._downloads.get(item_id, 0)

    # --------------------------------------------------------------- deletion
    async def delete_item(
        self,
        item_id: str,
        *,
        on_deleted: Callable[[Item], None] | None = None,
        thumbnail_remover: Callable[[str], None] | None = None,
    ) -> Item | None:
        """Delete metadata first, then the physical file.

        New downloads immediately 404 because the row is gone, and ``on_deleted``
        fires (for the SSE broadcast and thumbnail cleanup) before the file is
        touched. Physical removal is retried a few times; if Windows still holds
        a handle the file is left for startup orphan cleanup instead of failing
        the request.
        """
        item = await run_in_threadpool(self.db.delete_item, item_id)
        if item is None:
            return None
        if on_deleted is not None:
            with contextlib.suppress(Exception):
                on_deleted(item)
        if thumbnail_remover is not None:
            with contextlib.suppress(Exception):
                await run_in_threadpool(thumbnail_remover, item.id)

        if not item.stored_name:
            return item

        if self.active_downloads(item.id) > 0:
            # Let the accepted download release its handle, then delete.
            task = asyncio.create_task(self._delete_when_idle(item))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        else:
            await self.delete_stored_file(item.stored_name)
        return item

    async def delete_stored_file(self, stored_name: str | None, *, attempts: int = 3) -> bool:
        if not stored_name:
            return True
        try:
            path = self.resolve_stored_path(stored_name)
        except ValueError:
            return False
        for attempt in range(attempts):
            try:
                await run_in_threadpool(_unlink_quietly, path)
                if not await run_in_threadpool(path.exists):
                    return True
            except OSError:  # pragma: no cover - Windows lock behaviour
                pass
            if attempt < attempts - 1:
                await asyncio.sleep(0.1 * (attempt + 1))
        return False

    async def _delete_when_idle(self, item: Item, *, timeout: float = 30.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while self.active_downloads(item.id) > 0:
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(0.2)
        await self.delete_stored_file(item.stored_name)

    async def wait_for_background_tasks(self) -> None:
        """Await in-flight physical deletions (called on shutdown / in tests)."""
        if not self._background_tasks:
            return
        tasks = list(self._background_tasks)
        await asyncio.gather(*tasks, return_exceptions=True)

    # ---------------------------------------------------------------- cleanup
    def cleanup_partials(self) -> int:
        """Remove leftover ``.part`` files from interrupted uploads."""
        removed = 0
        if not self.files_dir.exists():
            return removed
        for path in self.files_dir.glob("*.part"):
            if path.is_file() and _unlink_quietly(path):
                removed += 1
        return removed

    def cleanup_orphans(self) -> int:
        """Delete storage files that SQLite no longer references."""
        referenced = self.db.stored_names()
        removed = 0
        if not self.files_dir.exists():
            return removed
        for path in self.files_dir.iterdir():
            if not path.is_file():
                continue
            if path.name.endswith(".part"):
                continue
            if path.name not in referenced and _unlink_quietly(path):
                removed += 1
        return removed

    def cleanup(self) -> tuple[int, int]:
        """Startup hygiene: partial uploads first, then unreferenced files."""
        return self.cleanup_partials(), self.cleanup_orphans()

def _unlink_quietly(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def _human_bytes(value: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.0f} {unit}" if size >= 10 else f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"
