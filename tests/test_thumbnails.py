"""Thumbnail generation, caching and failure-mode tests."""

from __future__ import annotations

import io
import threading

import pytest
from PIL import Image

from database import Database
from tests.conftest import make_image_bytes, upload
from thumbnail_manager import ThumbnailManager, ThumbnailUnavailable


def _jpeg_with_orientation(size=(800, 400), orientation=6) -> bytes:
    """JPEG tagged EXIF orientation 6 (rotate 90 degrees clockwise to view)."""
    image = Image.new("RGB", size, (10, 120, 200))
    exif = Image.Exif()
    exif[0x0112] = orientation
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", exif=exif)
    return buffer.getvalue()


def test_thumbnail_is_generated_and_bounded(authed_client, settings):
    data = make_image_bytes((1200, 900))
    item = upload(authed_client, data, filename="photo.png", mime="image/png").json()["item"]

    response = authed_client.get(f"/api/thumb/{item['id']}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "private, max-age=604800"

    with Image.open(io.BytesIO(response.content)) as thumb:
        assert thumb.format == "JPEG"
        assert thumb.width <= 320 and thumb.height <= 320
        assert max(thumb.width, thumb.height) == 320

    cache_file = settings.thumbs_dir / f"{item['id']}.jpg"
    assert cache_file.is_file()


def test_thumbnail_is_cached_not_regenerated(authed_client, settings):
    data = make_image_bytes((600, 600))
    item = upload(authed_client, data, filename="cached.png", mime="image/png").json()["item"]
    cache_file = settings.thumbs_dir / f"{item['id']}.jpg"

    first = authed_client.get(f"/api/thumb/{item['id']}")
    assert first.status_code == 200
    first_mtime = cache_file.stat().st_mtime_ns
    first_bytes = cache_file.read_bytes()

    second = authed_client.get(f"/api/thumb/{item['id']}")
    assert second.status_code == 200
    assert cache_file.stat().st_mtime_ns == first_mtime
    assert cache_file.read_bytes() == first_bytes
    assert second.content == first.content

    # The cache directory only ever holds one file for one item.
    assert len(list(settings.thumbs_dir.iterdir())) == 1


def test_thumbnail_cache_removed_on_delete(authed_client, settings):
    item = upload(
        authed_client, make_image_bytes((400, 300)), filename="bye.png", mime="image/png"
    ).json()["item"]
    assert authed_client.get(f"/api/thumb/{item['id']}").status_code == 200
    cache_file = settings.thumbs_dir / f"{item['id']}.jpg"
    assert cache_file.exists()

    assert authed_client.delete(f"/api/items/{item['id']}").status_code == 200
    assert not cache_file.exists()
    assert authed_client.get(f"/api/thumb/{item['id']}").status_code == 404


def test_corrupt_image_returns_415(authed_client):
    item = upload(
        authed_client, b"\x89PNG\r\n\x1a\nnot really an image", filename="broken.png", mime="image/png"
    ).json()["item"]
    response = authed_client.get(f"/api/thumb/{item['id']}")
    assert response.status_code == 415
    assert response.json() == {"error": "Thumbnail is not available for this file."}


def test_unsupported_heic_returns_415(authed_client):
    item = upload(
        authed_client, b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64, filename="photo.heic", mime="image/heic"
    ).json()["item"]
    response = authed_client.get(f"/api/thumb/{item['id']}")
    assert response.status_code == 415
    assert "error" in response.json()


def test_oversized_image_is_refused(authed_client, monkeypatch):
    """A low decompression-bomb threshold stands in for an enormous image."""
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    item = upload(
        authed_client, make_image_bytes((200, 200)), filename="bomb.png", mime="image/png"
    ).json()["item"]
    response = authed_client.get(f"/api/thumb/{item['id']}")
    assert response.status_code == 415
    assert "error" in response.json()


def test_exif_orientation_is_applied(authed_client):
    item = upload(
        authed_client, _jpeg_with_orientation((800, 400)), filename="rotated.jpg", mime="image/jpeg"
    ).json()["item"]
    response = authed_client.get(f"/api/thumb/{item['id']}")
    assert response.status_code == 200
    with Image.open(io.BytesIO(response.content)) as thumb:
        # Source is landscape but tagged to display as portrait.
        assert thumb.height > thumb.width


def test_thumbnail_requires_authentication(client):
    response = client.get("/api/thumb/" + "a" * 32)
    assert response.status_code == 401


def test_thumbnail_for_missing_file_returns_404(authed_client):
    item = upload(authed_client, make_image_bytes((50, 50)), filename="gone.png", mime="image/png").json()["item"]
    stored = next(p for p in authed_client.app.state.bifrost.settings.files_dir.iterdir())
    stored.unlink()
    assert authed_client.get(f"/api/thumb/{item['id']}").status_code == 404


def test_thumbnail_generation_is_concurrency_safe(settings_factory):
    settings = settings_factory()
    settings.ensure_directories()
    db = Database(settings.db_path)
    db.init_schema()
    thumbs = ThumbnailManager(settings)

    source = settings.files_dir / ("a" * 32 + ".png")
    source.write_bytes(make_image_bytes((900, 700)))
    item_id = "a" * 32

    results: list[str] = []
    errors: list[Exception] = []

    def worker() -> None:
        try:
            path = thumbs.get_or_create(item_id, source)
            results.append(path.read_bytes().hex())
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(results) == 8
    assert len(set(results)) == 1
    assert len([p for p in settings.thumbs_dir.iterdir()]) == 1


def test_thumbnail_manager_rejects_bad_item_ids(settings_factory):
    settings = settings_factory()
    settings.ensure_directories()
    manager = ThumbnailManager(settings)
    with pytest.raises(ThumbnailUnavailable):
        manager.get_or_create("../../etc/passwd", settings.files_dir / "whatever")


def test_thumbnail_cleanup_orphans(settings_factory):
    settings = settings_factory()
    settings.ensure_directories()
    manager = ThumbnailManager(settings)
    (settings.thumbs_dir / f"{'b' * 32}.jpg").write_bytes(b"stale")
    valid = {"c" * 32}
    (settings.thumbs_dir / f"{'c' * 32}.jpg").write_bytes(b"keep")

    removed = manager.cleanup_orphans(valid)
    assert removed == 1
    assert (settings.thumbs_dir / f"{'c' * 32}.jpg").exists()
    assert not (settings.thumbs_dir / f"{'b' * 32}.jpg").exists()


def test_transparent_png_is_flattened(authed_client):
    image = Image.new("RGBA", (400, 400), (255, 0, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    item = upload(authed_client, buffer.getvalue(), filename="alpha.png", mime="image/png").json()["item"]
    response = authed_client.get(f"/api/thumb/{item['id']}")
    assert response.status_code == 200
    with Image.open(io.BytesIO(response.content)) as thumb:
        assert thumb.mode == "RGB"
