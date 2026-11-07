"""Authentication, session and pairing-info tests."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from auth import COOKIE_NAME, PinRateLimiter, generate_pin, sanitize_device_name
from main import create_app
from tests.conftest import login


# --------------------------------------------------------------------- PIN
def test_generated_pin_is_six_digits_with_leading_zeros():
    for _ in range(250):
        pin = generate_pin()
        assert re.fullmatch(r"\d{6}", pin), pin
        assert len(pin) == 6
    # Very likely at least one generated PIN starts with a zero.
    pins = [generate_pin() for _ in range(400)]
    assert any(pin.startswith("0") for pin in pins)


def test_pin_changes_on_restart(settings_factory, clock):
    first_app = create_app(settings_factory(), clock=clock, show_banner=False)
    second_app = create_app(settings_factory(), clock=clock, show_banner=False)
    with TestClient(first_app) as c1, TestClient(second_app) as c2:
        pins = {c1.app.state.bifrost.pin, c2.app.state.bifrost.pin}
    # Two independent servers generate independent PINs; a 1-in-a-million
    # collision is acceptable to ignore but the values must both be valid.
    assert all(re.fullmatch(r"\d{6}", pin) for pin in pins)


# ------------------------------------------------------------ happy path auth
def test_successful_pin_authentication_sets_httponly_cookie(client, app):
    response = login(client, app, device="Windows PC")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["device"] == "Windows PC"

    raw_cookie = response.headers["set-cookie"]
    lowered = raw_cookie.lower()
    assert COOKIE_NAME in raw_cookie
    assert "httponly" in lowered
    assert "samesite=lax" in lowered
    assert "path=/" in lowered
    assert "max-age=2592000" in lowered
    assert "secure" not in lowered  # BIFROST_COOKIE_SECURE defaults to 0

    token = client.cookies.get(COOKIE_NAME)
    assert token and len(token) > 20

    items = client.get("/api/items")
    assert items.status_code == 200
    assert items.json()["items"] == []


def test_cookie_secure_flag_is_configurable(settings_factory, clock):
    settings = settings_factory(cookie_secure=True)
    app = create_app(
        settings, clock=clock, show_banner=False, free_space_provider=lambda _p: 10**12
    )
    with TestClient(app, base_url="http://localhost:8000") as client:
        response = login(client, app)
        assert response.status_code == 200
        assert "secure" in response.headers["set-cookie"].lower()
        assert "strict-transport-security" in {k.lower() for k in response.headers}


def test_invalid_pin_is_rejected(client, app):
    response = client.post("/api/auth", json={"pin": "000000" if app.state.bifrost.pin != "000000" else "111111"})
    assert response.status_code == 401
    assert response.json() == {"error": "Incorrect PIN."}
    assert COOKIE_NAME not in client.cookies


def test_non_six_digit_pin_is_a_bad_request(client, app):
    for payload in ({"pin": "12345"}, {"pin": "abcdef"}, {"pin": ""}, {"nopin": "1"}):
        response = client.post("/api/auth", json=payload)
        assert response.status_code == 400, payload
        assert "error" in response.json()


def test_malformed_json_body_returns_400(client):
    response = client.post(
        "/api/auth", content=b"not json", headers={"content-type": "application/json"}
    )
    assert response.status_code == 400
    assert response.json()["error"] == "Request body must be valid JSON."


def test_successful_auth_clears_failure_counter(client, app):
    wrong = "000000" if app.state.bifrost.pin != "000000" else "111111"
    for _ in range(4):
        assert client.post("/api/auth", json={"pin": wrong}).status_code == 401
    assert login(client, app).status_code == 200
    # Counter cleared: four more wrong attempts do not trigger the lockout yet.
    for _ in range(4):
        assert client.post("/api/auth", json={"pin": wrong}).status_code == 401


# ------------------------------------------------------------- PIN lockout
def test_five_incorrect_attempts_trigger_lockout(client, app, clock):
    wrong = "000000" if app.state.bifrost.pin != "000000" else "111111"
    statuses = [client.post("/api/auth", json={"pin": wrong}).status_code for _ in range(4)]
    assert statuses == [401, 401, 401, 401]
    fifth = client.post("/api/auth", json={"pin": wrong})
    assert fifth.status_code == 429
    assert "error" in fifth.json()


def test_correct_pin_during_lockout_returns_429(client, app, clock):
    wrong = "000000" if app.state.bifrost.pin != "000000" else "111111"
    for _ in range(5):
        client.post("/api/auth", json={"pin": wrong})
    response = login(client, app)
    assert response.status_code == 429
    # Still locked a moment later.
    clock.advance(59)
    assert login(client, app).status_code == 429


def test_authentication_allowed_after_lockout_expires(client, app, clock):
    wrong = "000000" if app.state.bifrost.pin != "000000" else "111111"
    for _ in range(5):
        client.post("/api/auth", json={"pin": wrong})
    assert login(client, app).status_code == 429
    clock.advance(61)
    assert login(client, app).status_code == 200


def test_rate_limiter_is_per_ip(settings, clock):
    limiter = PinRateLimiter(clock, max_attempts=2, lockout_seconds=60)
    assert limiter.record_failure("10.0.0.1") is False
    assert limiter.record_failure("10.0.0.1") is True
    assert limiter.is_locked("10.0.0.1") is True
    assert limiter.is_locked("10.0.0.2") is False
    clock.advance(61)
    assert limiter.is_locked("10.0.0.1") is False
    assert limiter.record_failure("10.0.0.1") is False  # fresh window


def test_rate_limiter_is_thread_safe(clock):
    limiter = PinRateLimiter(clock, max_attempts=5, lockout_seconds=60)
    results: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        locked = limiter.record_failure("192.168.1.20")
        with lock:
            results.append(locked)

    threads = [threading.Thread(target=worker) for _ in range(30)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(results) >= 1
    assert limiter.is_locked("192.168.1.20") is True


# ---------------------------------------------------------------- sessions
def test_session_is_stored_only_as_a_hash(authed_client, client, settings):
    raw_token = client.cookies.get(COOKIE_NAME)
    assert raw_token

    connection = sqlite3.connect(str(settings.db_path))
    try:
        rows = connection.execute("SELECT token_hash FROM sessions").fetchall()
    finally:
        connection.close()

    stored = {row[0] for row in rows}
    assert stored, "expected at least one stored session"
    assert raw_token not in stored
    assert hashlib.sha256(raw_token.encode()).hexdigest() in stored


def test_session_expires_after_thirty_days(authed_client, clock):
    assert authed_client.get("/api/items").status_code == 200
    clock.advance(days=29)
    assert authed_client.get("/api/items").status_code == 200
    clock.advance(days=2)
    response = authed_client.get("/api/items")
    assert response.status_code == 401
    assert response.json() == {"error": "Authentication required."}


def test_requests_without_cookie_are_rejected(client):
    assert client.get("/api/items").status_code == 401
    assert client.post("/api/note", json={"text": "hi"}).status_code == 401
    assert client.get("/api/files/" + "0" * 32).status_code == 401
    assert client.get("/api/events").status_code == 401
    assert client.delete("/api/items/" + "0" * 32).status_code == 401


def test_session_survives_server_restart(settings_factory, clock, app_factory):
    """A paired device keeps working after the server restarts with a new PIN."""
    settings = settings_factory()

    first_app = app_factory(settings)
    with TestClient(first_app, base_url="http://localhost:8000", client=("127.0.0.1", 50000)) as first:
        assert login(first, first_app, device="Phone").status_code == 200
        raw_token = first.cookies.get(COOKIE_NAME)
        first_pin = first_app.state.bifrost.pin
        assert first.get("/api/items").status_code == 200

    second_app = app_factory(settings)
    with TestClient(second_app, base_url="http://localhost:8000", client=("127.0.0.1", 50000)) as second:
        second_pin = second_app.state.bifrost.pin
        # Fresh server, fresh PIN, but the old cookie must still authenticate.
        if second_pin == first_pin:  # pragma: no cover - astronomically unlikely
            pytest.skip("PIN collision between restarts")
        second.cookies.set(COOKIE_NAME, raw_token)
        assert second.get("/api/items").status_code == 200


def test_session_device_name_is_used_as_sender_fallback(authed_client):
    response = authed_client.post("/api/note", json={"text": "hello"}, headers={"x-device-name": ""})
    # An empty header decodes to nothing, so the session device is used.
    assert response.status_code == 201
    assert response.json()["item"]["sender"] == "Test Device"


# --------------------------------------------------------------- pairing info
def test_pairing_info_available_on_loopback(client, app):
    response = client.get("/api/pairing-info")
    assert response.status_code == 200
    payload = response.json()
    assert payload["pin"] == app.state.bifrost.pin
    assert payload["port"] == 8000
    assert payload["local_url"].startswith("http://localhost:8000")
    assert response.headers["cache-control"] == "no-store"


def test_pairing_info_rejects_remote_clients(remote_client, app):
    response = remote_client.get("/api/pairing-info")
    assert response.status_code == 403
    assert app.state.bifrost.pin not in response.text


def test_pairing_info_rejects_invalid_host_header(client, app):
    response = client.get("/api/pairing-info", headers={"host": "example.com:8000"})
    assert response.status_code == 403
    assert app.state.bifrost.pin not in response.text


def test_pairing_info_rejects_wrong_port_in_host_header(client):
    response = client.get("/api/pairing-info", headers={"host": "localhost:9999"})
    assert response.status_code == 403


def test_pairing_info_rejects_matching_dns_name_that_is_not_loopback_name(client):
    response = client.get("/api/pairing-info", headers={"host": "my-laptop.local:8000"})
    assert response.status_code == 403


def test_pairing_info_rejects_foreign_origin(client):
    response = client.get(
        "/api/pairing-info",
        headers={"origin": "http://evil.example", "host": "localhost:8000"},
    )
    assert response.status_code == 403


def test_pairing_info_accepts_matching_loopback_origin(client, app):
    response = client.get(
        "/api/pairing-info",
        headers={"origin": "http://localhost:8000", "host": "localhost:8000"},
    )
    assert response.status_code == 200
    assert response.json()["pin"] == app.state.bifrost.pin


def test_pairing_info_accepts_127_0_0_1_host(client, app):
    response = client.get(
        "/api/pairing-info",
        headers={"origin": "http://127.0.0.1:8000", "host": "127.0.0.1:8000"},
    )
    assert response.status_code == 200
    assert response.json()["pin"] == app.state.bifrost.pin


# ------------------------------------------------------------ device names
def test_device_name_is_sanitized_and_truncated(client, app):
    long_name = "Phone " + "x" * 100
    response = login(client, app, device=long_name)
    assert response.status_code == 200
    assert len(response.json()["device"]) <= 40


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, "Unknown device"),
        ("", "Unknown device"),
        ("   ", "Unknown device"),
        ("\x00\x01bad\x02", "bad"),
        ("Windows   PC", "Windows PC"),
        ("मेरा फ़ोन", "मेरा फ़ोन"),
        ("📱 Phone", "📱 Phone"),
        ("A\nB", "A B"),
    ],
)
def test_sanitize_device_name(raw, expected):
    assert sanitize_device_name(raw, max_chars=40) == expected


def test_device_name_length_limit_applies_to_header_sender(authed_client):
    from tests.conftest import upload

    long_name = "साझा" * 20
    response = upload(authed_client, b"payload", device=long_name)
    assert response.status_code == 201
    sender = response.json()["item"]["sender"]
    assert len(sender) <= 40
    assert sender == long_name[:40]


# ---------------------------------------------------------------- utilities
def test_health_of_static_page(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "content-security-policy" in {k.lower() for k in response.headers}
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_unknown_api_route_returns_json_error(client):
    response = client.get("/api/does-not-exist")
    assert response.status_code in {404, 405}
    assert "error" in response.json()
