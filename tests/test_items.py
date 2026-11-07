"""Inbox item tests: notes, listing, downloads, Range requests and deletion."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import device_header, login, upload, wait_for


# --------------------------------------------------------------------- notes
def test_note_creation(authed_client):
    response = authed_client.post(
        "/api/note",
        json={"text": "Shared from the laptop\nSecond line"},
        headers={"x-device-name": device_header("Windows PC")},
    )
    assert response.status_code == 201
    item = response.json()["item"]
    assert item["kind"] == "note"
    assert item["text"] == "Shared from the laptop\nSecond line"
    assert item["sender"] == "Windows PC"
    assert item["size"] == len("Shared from the laptop\nSecond line")
    assert item["download_url"] is None
    assert item["thumb_url"] is None


def test_note_requires_text(authed_client):
    for payload in ({}, {"text": ""}, {"text": "   \n  "}, {"text": 42}):
        response = authed_client.post("/api/note", json=payload)
        assert response.status_code == 400, payload
        assert "error" in response.json()


def test_note_length_limit_is_exactly_100000(authed_client):
    settings = authed_client.app.state.bifrost.settings
    assert settings.max_note_chars == 100_000

    allowed = "a" * 100_000
    response = authed_client.post("/api/note", json={"text": allowed})
    assert response.status_code == 201
    assert response.json()["item"]["size"] == 100_000

    too_long = "b" * 100_001
    response = authed_client.post("/api/note", json={"text": too_long})
    assert response.status_code == 400
    assert "100000" in response.json()["error"]


def test_note_preserves_line_breaks_and_unicode(authed_client):
    text = "पहली पंक्ति\n\n🎉 दूसरी पंक्ति\n"
    response = authed_client.post("/api/note", json={"text": text})
    assert response.status_code == 201
    assert response.json()["item"]["text"] == text
    # Round-trips through the list endpoint too.
    items = authed_client.get("/api/items").json()["items"]
    assert items[0]["text"] == text


def test_note_sender_comes_from_header(authed_client):
    response = authed_client.post(
        "/api/note",
        json={"text": "hi"},
        headers={"x-device-name": device_header("Rahul's iPhone")},
    )
    assert response.status_code == 201
    assert response.json()["item"]["sender"] == "Rahul's iPhone"


# ------------------------------------------------------------------- listing
def test_items_are_newest_first(authed_client, clock):
    notes = []
    for index in range(4):
        clock.advance(30)
        response = authed_client.post("/api/note", json={"text": f"note {index}"})
        notes.append(response.json()["item"])

    listed = authed_client.get("/api/items").json()["items"]
    assert [item["text"] for item in listed] == ["note 3", "note 2", "note 1", "note 0"]

    clock.advance(30)
    upload(authed_client, b"data", filename="newest.bin")
    listed = authed_client.get("/api/items").json()["items"]
    assert listed[0]["name"] == "newest.bin"


def test_items_persist_across_restart(settings_factory, clock, app_factory):
    settings = settings_factory()

    first_app = app_factory(settings)
    with TestClient(first_app, base_url="http://localhost:8000", client=("127.0.0.1", 5000)) as first:
        login(first, first_app, device="Phone")
        token = first.cookies.get("bifrost_session")
        upload(first, b"persisted payload", filename="keep.txt")
        first.post("/api/note", json={"text": "keep this note"})

    second_app = app_factory(settings)
    with TestClient(second_app, base_url="http://localhost:8000", client=("127.0.0.1", 5000)) as second:
        second.cookies.set("bifrost_session", token)
        items = second.get("/api/items").json()["items"]
        assert len(items) == 2
        note = next(item for item in items if item["kind"] == "note")
        file_item = next(item for item in items if item["kind"] == "file")
        assert note["text"] == "keep this note"
        assert second.get(f"/api/files/{file_item['id']}").content == b"persisted payload"


# ------------------------------------------------------------------ download
def test_download_sets_attachment_and_nosniff(authed_client):
    item = upload(authed_client, b"payload", filename="report.txt").json()["item"]
    response = authed_client.get(f"/api/files/{item['id']}")
    assert response.status_code == 200
    assert response.content == b"payload"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert 'filename="report.txt"' in disposition
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-length"] == "7"


def test_unicode_content_disposition(authed_client):
    item = upload(authed_client, b"data", filename="फ़ाइल 🎉.txt").json()["item"]
    response = authed_client.get(f"/api/files/{item['id']}")
    disposition = response.headers["content-disposition"]
    assert "filename*=UTF-8''" in disposition
    assert "%E0%A4%AB" in disposition  # UTF-8 encoded Hindi
    assert 'filename="' in disposition  # ASCII fallback kept


def test_range_request_returns_206(authed_client):
    payload = bytes(range(256)) * 40  # 10 240 bytes
    item = upload(authed_client, payload, filename="range.bin").json()["item"]

    response = authed_client.get(
        f"/api/files/{item['id']}", headers={"range": "bytes=100-199"}
    )
    assert response.status_code == 206
    assert response.headers["content-range"] == f"bytes 100-199/{len(payload)}"
    assert response.headers["content-length"] == "100"
    assert response.content == payload[100:200]


def test_range_request_suffix(authed_client):
    payload = b"0123456789"
    item = upload(authed_client, payload, filename="small.bin").json()["item"]
    response = authed_client.get(
        f"/api/files/{item['id']}", headers={"range": "bytes=-4"}
    )
    assert response.status_code == 206
    assert response.content == b"6789"


def test_unsatisfiable_range_returns_416(authed_client):
    item = upload(authed_client, b"tiny", filename="tiny.bin").json()["item"]
    response = authed_client.get(
        f"/api/files/{item['id']}", headers={"range": "bytes=100-200"}
    )
    assert response.status_code == 416


def test_missing_and_invalid_items_return_404(authed_client):
    assert authed_client.get("/api/files/" + "a" * 32).status_code == 404
    assert authed_client.get("/api/files/not-a-uuid").status_code == 404
    assert authed_client.get("/api/files/../../etc/passwd").status_code == 404
    note = authed_client.post("/api/note", json={"text": "x"}).json()["item"]
    assert authed_client.get(f"/api/files/{note['id']}").status_code == 404
    assert authed_client.get(f"/api/thumb/{note['id']}").status_code == 404


# ------------------------------------------------------------------ deletion
def test_file_deletion_removes_metadata_and_file(authed_client, settings):
    item = upload(authed_client, b"bye", filename="bye.txt").json()["item"]
    assert any(settings.files_dir.iterdir())

    response = authed_client.delete(f"/api/items/{item['id']}")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "id": item["id"]}

    assert authed_client.get(f"/api/files/{item['id']}").status_code == 404
    assert authed_client.get("/api/items").json()["items"] == []
    assert [p for p in settings.files_dir.iterdir()] == []


def test_note_deletion(authed_client):
    item = authed_client.post("/api/note", json={"text": "delete me"}).json()["item"]
    assert authed_client.delete(f"/api/items/{item['id']}").status_code == 200
    assert authed_client.get("/api/items").json()["items"] == []
    assert authed_client.delete(f"/api/items/{item['id']}").status_code == 404


def test_deleting_missing_item_returns_404(authed_client):
    assert authed_client.delete("/api/items/" + "b" * 32).status_code == 404
    assert authed_client.delete("/api/items/nonsense").status_code == 404


def test_delete_during_download_allows_download_to_finish(authed_client):
    """Metadata disappears immediately; an accepted download still completes."""
    payload = b"x" * 4096
    item = upload(authed_client, payload, filename="busy.bin").json()["item"]
    item_id = item["id"]

    # Simulate an in-flight download by holding a lease directly.
    files = authed_client.app.state.bifrost.files
    files.begin_download(item_id)

    response = authed_client.delete(f"/api/items/{item_id}")
    assert response.status_code == 200
    # New download requests are refused immediately (row already deleted).
    assert authed_client.get(f"/api/files/{item_id}").status_code == 404

    # The deferred physical delete runs as soon as the lease is released.
    files.end_download(item_id)
    files_dir = authed_client.app.state.bifrost.settings.files_dir
    assert wait_for(lambda: not list(files_dir.iterdir()))


def test_item_ids_are_validated_before_filesystem_access(authed_client):
    for bogus in ("..", "%2e%2e%2f", "0" * 31, "Z" * 32):
        assert authed_client.delete(f"/api/items/{bogus}").status_code == 404


def test_list_limit_is_bounded(authed_client):
    response = authed_client.get("/api/items", params={"limit": 10})
    assert response.status_code == 200
    assert "items" in response.json()
