"""Pillow-based thumbnail generation with an on-disk cache.

* Thumbnails are cached in ``data/thumbs/<item-id>.jpg`` and reused until the
  source file is newer than the cache.
* Rendering happens off the event loop, guarded by a per-item lock so that
  concurrent requests for the same thumbnail do no duplicate work.
* Malformed, unsupported (for example HEIC) or absurdly large images raise
  :class:`ThumbnailUnavailable` and the UI falls back to a file icon.
"""

from __future__ import annotations

import contextlib
import os
import threading
import uuid
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from config import Settings

#: Guard against decompression bombs (Pillow default is ~89M pixels).
Image.MAX_IMAGE_PIXELS = 64_000_000


class ThumbnailUnavailable(Exception):
    """Raised when a thumbnail cannot be produced for an item."""

    status_code = 415
    default_message = "Thumbnail is not available for this file."

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.default_message)
        self.message = message or self.default_message


class ThumbnailManager:
    """Generates and caches JPEG thumbnails for image items."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.thumbs_dir = settings.thumbs_dir
        self.max_px = settings.thumb_max_px
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ------------------------------------------------------------------ paths
    def cache_path(self, item_id: str) -> Path:
        return self.thumbs_dir / f"{item_id}.jpg"

    # ------------------------------------------------------------------- api
    def get_or_create(self, item_id: str, source: Path) -> Path:
        """Return the cached thumbnail path, rendering it if necessary."""
        if not _is_uuid_hex(item_id):
            raise ThumbnailUnavailable("Unknown item.")
        destination = self.cache_path(item_id)
        with self._item_lock(item_id):
            if destination.is_file():
                try:
                    if destination.stat().st_size > 0:
                        return destination
                except OSError:  # pragma: no cover - race with deletion
                    pass

            if not source.is_file():
                raise ThumbnailUnavailable("Source file is no longer available.")

            self.thumbs_dir.mkdir(parents=True, exist_ok=True)
            temp = self.thumbs_dir / f"{item_id}.{uuid.uuid4().hex}.tmp"
            try:
                self._render(source, temp)
                os.replace(temp, destination)
            except ThumbnailUnavailable:
                _unlink_quietly(temp)
                raise
            except (UnidentifiedImageError, OSError, ValueError) as exc:
                _unlink_quietly(temp)
                raise ThumbnailUnavailable("Thumbnail is not available for this file.") from exc
            except Image.DecompressionBombError as exc:  # type: ignore[attr-defined]
                _unlink_quietly(temp)
                raise ThumbnailUnavailable("Image is too large to preview.") from exc
            except Exception as exc:  # pragma: no cover - defensive
                _unlink_quietly(temp)
                raise ThumbnailUnavailable("Thumbnail is not available for this file.") from exc
            return destination

    def remove(self, item_id: str) -> None:
        """Delete a cached thumbnail (called when an item is deleted)."""
        with contextlib.suppress(OSError, ValueError):
            _unlink_quietly(self.cache_path(item_id))

    def cleanup_orphans(self, valid_item_ids: set[str]) -> int:
        removed = 0
        if not self.thumbs_dir.exists():
            return removed
        for path in self.thumbs_dir.iterdir():
            if not path.is_file():
                continue
            stem = path.name.split(".", 1)[0]
            if stem in valid_item_ids:
                continue
            if _unlink_quietly(path):
                removed += 1
        return removed

    # -------------------------------------------------------------- internals
    def _render(self, source: Path, destination: Path) -> None:
        try:
            with Image.open(source) as image:
                image.load()
                oriented = ImageOps.exif_transpose(image)
                if oriented is None:  # pragma: no cover - very old Pillow
                    oriented = image
                # Animated / multi-frame images: preview the first frame.
                if getattr(oriented, "is_animated", False):
                    with contextlib.suppress(Exception):
                        oriented.seek(0)
                oriented.thumbnail((self.max_px, self.max_px), Image.Resampling.LANCZOS)
                prepared = _flatten(oriented)
                prepared.save(
                    destination,
                    format="JPEG",
                    quality=82,
                    optimize=True,
                )
        except Image.DecompressionBombError as exc:
            raise ThumbnailUnavailable("Image is too large to preview.") from exc

    def _item_lock(self, item_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(item_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[item_id] = lock
            return lock


def _flatten(image: Image.Image) -> Image.Image:
    """Convert any Pillow mode to RGB, compositing transparency on white."""
    if image.mode == "RGB":
        return image
    if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    return image.convert("RGB")


def _is_uuid_hex(value: str) -> bool:
    if len(value) != 32:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _unlink_quietly(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
