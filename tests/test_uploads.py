"""Raw-body streaming upload tests."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from urllib.parse import quote

import pytest

from auth import COOKIE_NAME
from database import Database
from file_manager import FileManager, InvalidUpload, UploadTooLarge
from tests.conftest import PLENTY_OF_SPACE, device_header, raw_call, upload


def _auth_headers(client, app, *, filename="file.txt", device="Test Device", mime="text/plain"):
    token = client.cookies.get(COOKIE_NAME)
    extra = {"cookie": f"{COOKIE_NAME}={token}"}
    return {
        "host": "localhost:8000",
        "x-filename": quote(filename, safe=""),
        "x-device-name": device_header(device),
        "content-type": mime,
        **extra,
    }


# ------------------------------------------------------------------- happy path
def test_raw_body_upload(authed_client):
    response = upload(authed_client, b"hello bifrost", filename="hello.txt")
    assert response.status_code == 201
    item = response.json()["item"]
    assert item["kind"] == "file"
    assert item["name"] == "hello.txt"
    assert item["size"] == len(b"hello bifrost")
    assert item["sender"] == "Test Device"
    assert item["download_url"] == f"/api/files/{item['id']}"


def test_multiple_uploads_are_all_stored(authed_client, settings):
    payloads = {f"file-{index}.bin": bytes([index]) * (index + 1) for index in range(5)}
    ids = []
    for name, data in payloads.items():
        response = upload(authed_client, data, filename=name)
        assert response.status_code == 201
        ids.append(response.json()["item"]["id"])

    assert len(set(ids)) == len(ids)
    stored = list(settings.files_dir.glob("*"))
    assert len([path for path in stored if not path.name.endswith(".part")]) == 5


def test_received_content_is_byte_exact(authed_client):
    blob = bytes(range(256)) * 512  # 128 KiB of every byte value
    response = upload(authed_client, blob, filename="binary.dat")
    assert response.status_code == 201
    item_id = response.json()["item"]["id"]
    download = authed_client.get(f"/api/files/{item_id}")
    assert download.status_code == 200
    assert download.content == blob


def test_upload_requires_authentication(client):
    response = upload(client, b"data")  # no session cookie
    assert response.status_code == 401


def test_upload_requires_x_filename_header(authed_client):
    response = authed_client.post(
        "/api/upload", content=b"data", headers={"content-type": "text/plain"}
    )
    assert response.status_code == 400
    assert response.json()["error"] == "An X-Filename header is required."


def test_upload_without_device_header_uses_session_device(authed_client):
    response = authed_client.post(
        "/api/upload",
        content=b"data",
        headers={"x-filename": "x.txt", "content-type": "text/plain"},
    )
    assert response.status_code == 201
    assert response.json()["item"]["sender"] == "Test Device"


def test_upload_stores_uuid_physical_filename(authed_client, settings):
    response = upload(authed_client, b"content", filename="holiday photo.jpg")
    item = response.json()["item"]
    stored_files = [p for p in settings.files_dir.iterdir() if p.is_file()]
    assert len(stored_files) == 1
    stored = stored_files[0]
    assert stored.suffix == ".jpg"
    assert stored.stem == item["id"]
    assert len(stored.stem) == 32
    assert stored.name != "holiday photo.jpg"


def test_duplicate_display_names_are_differentiated(authed_client, settings):
    first = upload(authed_client, b"first", filename="photo.jpg")
    second = upload(authed_client, b"second", filename="photo.jpg")
    third = upload(authed_client, b"third", filename="photo.jpg")

    names = [r.json()["item"]["name"] for r in (first, second, third)]
    assert names == ["photo.jpg", "photo (1).jpg", "photo (2).jpg"]

    # Every physical file is unique and both payloads are intact.
    files = sorted(p for p in settings.files_dir.iterdir() if p.is_file())
    assert len(files) == 3
    assert len({p.name for p in files}) == 3

    for response, expected in ((first, b"first"), (second, b"second"), (third, b"third")):
        item_id = response.json()["item"]["id"]
        assert authed_client.get(f"/api/files/{item_id}").content == expected


def test_empty_file_upload_is_allowed(authed_client):
    response = upload(authed_client, b"", filename="empty.txt")
    assert response.status_code == 201
    assert response.json()["item"]["size"] == 0


def test_traversal_filename_is_sanitized(authed_client, settings):
    response = upload(authed_client, b"evil", filename="../../evil.txt")
    assert response.status_code == 201
    assert response.json()["item"]["name"] == "evil.txt"
    # Nothing was written outside the files directory.
    assert not (settings.data_dir.parent / "evil.txt").exists()
    assert len([p for p in settings.files_dir.iterdir() if p.is_file()]) == 1


# ------------------------------------------------------------------- limits
def test_declared_content_length_over_limit_is_rejected(authed_client, settings):
    headers = _auth_headers(authed_client, None, filename="huge.bin")
    headers["content-length"] = str(settings.max_upload_bytes + 1)

    status, _, body = raw_call(
        authed_client.app,
        "POST",
        "/api/upload",
        headers=headers,
        body=b"only a few bytes",
    )
    assert status == 413
    assert b"maximum allowed size" in body
    assert not list(settings.files_dir.glob("*.part"))


def test_five_gigabyte_boundary_uses_default_limit(isolated_client):
    """The 5 GB rejection is verified without allocating a 5 GB file."""
    client, app, settings = isolated_client()
    assert settings.max_upload_bytes == 5 * 1024**3

    headers = _auth_headers(client, app, filename="huge.bin")
    headers["content-length"] = str(5 * 1024**3 + 1)
    status, _, body = raw_call(app, "POST", "/api/upload", headers=headers, body=b"x")
    assert status == 413
    assert b"maximum allowed size" in body
    assert not list(settings.files_dir.glob("*.part"))


def test_streamed_size_enforcement_without_content_length(isolated_client):
    client, app, settings = isolated_client(max_upload_bytes=2000)

    def chunks():
        yield b"a" * 800
        yield b"b" * 800
        yield b"c" * 800  # 2400 > 2000, and no Content-Length is sent

    response = client.post(
        "/api/upload",
        content=chunks(),
        headers={
            "x-filename": "big.bin",
            "x-device-name": device_header("Test Device"),
            "content-type": "application/octet-stream",
        },
    )
    assert response.status_code == 413
    assert not list(settings.files_dir.glob("*.part"))
    assert client.get("/api/items").json()["items"] == []


def test_insufficient_disk_space_is_rejected(isolated_client):
    client, app, settings = isolated_client(free_bytes=10)

    response = upload(client, b"data", filename="thing.txt")
    assert response.status_code == 507
    assert "disk space" in response.json()["error"].lower()
    assert not list(settings.files_dir.glob("*.part"))


def test_disk_space_checked_during_long_upload(settings_factory):
    """Free space is re-checked periodically; losing it mid-stream aborts."""
    settings = settings_factory(max_upload_bytes=200 * 1024 * 1024)
    settings.ensure_directories()
    db = Database(settings.db_path)
    db.init_schema()

    calls = {"n": 0}

    def provider(_path: Path) -> int:
        calls["n"] += 1
        # Ample at first, then the disk "fills up" during the upload.
        return PLENTY_OF_SPACE if calls["n"] == 1 else 0

    manager = FileManager(settings, db, free_space_provider=provider)
    manager.settings = settings.with_overrides(
        min_free_space_bytes=1024, max_upload_bytes=200 * 1024 * 1024
    )
    manager.settings.ensure_directories()

    async def stream():
        for _ in range(80):  # 80 MB > the 64 MB re-check interval
            yield b"x" * (1024 * 1024)

    async def run():
        with pytest.raises(Exception) as excinfo:
            await manager.receive_upload(
                stream(), filename="long.bin", sender="Tester", declared_length=None
            )
        return excinfo

    excinfo = asyncio.run(run())
    from file_manager import InsufficientStorage

    assert isinstance(excinfo.value, InsufficientStorage)
    assert not list(settings.files_dir.glob("*.part"))


# ------------------------------------------------------------------ cleanup
def test_failed_upload_leaves_no_part_file(settings_factory):
    settings = settings_factory()
    settings.ensure_directories()
    db = Database(settings.db_path)
    db.init_schema()
    manager = FileManager(settings, db, free_space_provider=lambda _p: PLENTY_OF_SPACE)

    async def broken_stream():
        yield b"partial data"
        raise RuntimeError("client vanished")

    async def run():
        with pytest.raises(RuntimeError):
            await manager.receive_upload(broken_stream(), filename="x.txt", sender="Tester")

    asyncio.run(run())
    leftovers = [p for p in settings.files_dir.iterdir()]
    assert leftovers == []
    assert db.list_items() == []


def test_cancelled_upload_cleans_up_part_file(settings_factory):
    settings = settings_factory()
    settings.ensure_directories()
    db = Database(settings.db_path)
    db.init_schema()
    manager = FileManager(settings, db, free_space_provider=lambda _p: PLENTY_OF_SPACE)

    seen_part_file = {"value": False}

    async def cancelled_stream():
        yield b"chunk"
        seen_part_file["value"] = any(settings.files_dir.glob("*.part"))
        raise asyncio.CancelledError()

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await manager.receive_upload(cancelled_stream(), filename="c.txt", sender="Tester")

    asyncio.run(run())
    assert seen_part_file["value"] is True
    assert not list(settings.files_dir.glob("*.part"))


def test_incomplete_upload_is_rejected(settings_factory):
    settings = settings_factory()
    settings.ensure_directories()
    db = Database(settings.db_path)
    db.init_schema()
    manager = FileManager(settings, db, free_space_provider=lambda _p: PLENTY_OF_SPACE)

    async def short_stream():
        yield b"12345"

    async def run():
        with pytest.raises(InvalidUpload):
            await manager.receive_upload(
                short_stream(), filename="short.txt", sender="Tester", declared_length=10
            )

    asyncio.run(run())
    assert not list(settings.files_dir.glob("*.part"))
    assert db.list_items() == []


def test_database_failure_removes_stored_file(settings_factory, monkeypatch):
    settings = settings_factory()
    settings.ensure_directories()
    db = Database(settings.db_path)
    db.init_schema()
    manager = FileManager(settings, db, free_space_provider=lambda _p: PLENTY_OF_SPACE)

    def boom(**_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "insert_file", boom)

    async def stream():
        yield b"payload"

    async def run():
        with pytest.raises(sqlite3.OperationalError):
            await manager.receive_upload(stream(), filename="fail.txt", sender="Tester")

    asyncio.run(run())
    assert [p for p in settings.files_dir.iterdir()] == []


def test_concurrent_same_name_uploads_get_unique_display_names(settings_factory):
    settings = settings_factory()
    settings.ensure_directories()
    db = Database(settings.db_path)
    db.init_schema()
    manager = FileManager(settings, db, free_space_provider=lambda _p: PLENTY_OF_SPACE)

    def make_stream(payload: bytes):
        async def stream():
            yield payload

        return stream()

    async def run():
        return await asyncio.gather(
            *[
                manager.receive_upload(
                    make_stream(f"payload-{index}".encode()),
                    filename="photo.jpg",
                    sender=f"Dev {index}",
                )
                for index in range(6)
            ]
        )

    items = asyncio.run(run())
    names = sorted(item.name for item in items)
    assert names == sorted(
        ["photo.jpg", "photo (1).jpg", "photo (2).jpg", "photo (3).jpg", "photo (4).jpg", "photo (5).jpg"]
    )
    stored = {item.stored_name for item in items}
    assert len(stored) == 6
    assert all(name.endswith(".jpg") for name in stored)


def test_upload_rejects_oversized_declared_length_via_manager(settings_factory):
    settings = settings_factory(max_upload_bytes=100)
    settings.ensure_directories()
    db = Database(settings.db_path)
    db.init_schema()
    manager = FileManager(settings, db, free_space_provider=lambda _p: PLENTY_OF_SPACE)

    async def stream():
        yield b"x"

    async def run():
        with pytest.raises(UploadTooLarge):
            await manager.receive_upload(
                stream(), filename="big.bin", sender="Tester", declared_length=101
            )

    asyncio.run(run())


def test_unicode_filename_is_preserved(authed_client, settings):
    response = upload(authed_client, b"data", filename="नमस्ते दुनिया 🎉.txt")
    assert response.status_code == 201
    assert response.json()["item"]["name"] == "नमस्ते दुनिया 🎉.txt"
    item_id = response.json()["item"]["id"]
    download = authed_client.get(f"/api/files/{item_id}")
    assert download.status_code == 200
    assert "filename*=UTF-8''" in download.headers["content-disposition"]
