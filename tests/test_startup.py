"""Startup, shutdown, cleanup, network detection and configuration tests."""

from __future__ import annotations

import io
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

import network
from config import PROJECT_DIR, ConfigError, Settings
from main import create_app
from network import build_urls, detect_lan_ip, is_usable_remote_ip, print_banner, render_qr
from tests.conftest import login, upload


# ---------------------------------------------------------------- lifecycle
def test_startup_creates_directories_and_database(settings_factory, clock):
    settings = settings_factory()
    assert not settings.data_dir.exists()

    app = create_app(settings, clock=clock, show_banner=False)
    with TestClient(app):
        assert settings.data_dir.is_dir()
        assert settings.files_dir.is_dir()
        assert settings.thumbs_dir.is_dir()
        assert settings.db_path.is_file()
        pin = app.state.bifrost.pin
        assert re.fullmatch(r"\d{6}", pin)
        # WAL mode is enabled for the database.
        connection = sqlite3.connect(str(settings.db_path))
        try:
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
        finally:
            connection.close()
        assert mode.lower() == "wal"
        assert timeout >= 5000


def test_pin_is_generated_during_lifespan_not_at_import(settings_factory, clock):
    app = create_app(settings_factory(), clock=clock, show_banner=False)
    assert app.state.bifrost.pin == ""
    with TestClient(app):
        assert re.fullmatch(r"\d{6}", app.state.bifrost.pin)


def test_startup_removes_part_files_and_orphans(settings_factory, clock):
    settings = settings_factory()
    settings.ensure_directories()
    (settings.files_dir / "leftover.part").write_bytes(b"partial")
    (settings.files_dir / "orphan.bin").write_bytes(b"orphan")
    (settings.files_dir / ("d" * 32 + ".txt")).write_bytes(b"also orphan")
    (settings.thumbs_dir / ("e" * 32 + ".jpg")).write_bytes(b"stale thumb")

    app = create_app(settings, clock=clock, show_banner=False, free_space_provider=lambda _p: 10**12)
    with TestClient(app):
        assert not list(settings.files_dir.iterdir())
        assert not list(settings.thumbs_dir.iterdir())


def test_startup_keeps_files_that_are_referenced(settings_factory, clock):
    settings = settings_factory()
    first_app = create_app(settings, clock=clock, show_banner=False, free_space_provider=lambda _p: 10**12)
    with TestClient(first_app, base_url="http://localhost:8000") as client:
        login(client, first_app)
        upload(client, b"keep me", filename="keep.txt")
        stored = [p for p in settings.files_dir.iterdir()]
        assert len(stored) == 1
        stored_name = stored[0].name

    # A .part file appears between runs and must be removed, not the real file.
    (settings.files_dir / "interrupted.part").write_bytes(b"junk")

    second_app = create_app(settings, clock=clock, show_banner=False, free_space_provider=lambda _p: 10**12)
    with TestClient(second_app):
        assert (settings.files_dir / stored_name).is_file()
        assert not (settings.files_dir / "interrupted.part").exists()


def test_lifespan_shutdown_stops_sse(settings_factory, clock):
    app = create_app(settings_factory(), clock=clock, show_banner=False, free_space_provider=lambda _p: 10**12)
    with TestClient(app) as client:
        login(client, app)
        assert app.state.bifrost.events.accepting is True
    assert app.state.bifrost.events.accepting is False
    assert app.state.bifrost.events.client_count == 0


# ------------------------------------------------------------------- network
def test_manual_override_wins(monkeypatch):
    monkeypatch.setattr(network, "_udp_detect", lambda: "10.0.0.99")
    ip, warnings = detect_lan_ip("192.168.1.44")
    assert ip == "192.168.1.44"
    assert warnings == []


def test_override_to_loopback_warns():
    ip, warnings = detect_lan_ip("127.0.0.1")
    assert ip == "127.0.0.1"
    assert warnings and "not a routable address" in warnings[0]


def test_udp_detection_used(monkeypatch):
    monkeypatch.setattr(network, "_udp_detect", lambda: "192.168.0.12")
    monkeypatch.setattr(network, "_hostname_detect", lambda: "10.0.0.5")
    ip, warnings = detect_lan_ip()
    assert ip == "192.168.0.12"
    assert warnings == []


def test_hostname_fallback(monkeypatch):
    monkeypatch.setattr(network, "_udp_detect", lambda: None)
    monkeypatch.setattr(network, "_hostname_detect", lambda: "192.168.1.9")
    ip, warnings = detect_lan_ip()
    assert ip == "192.168.1.9"
    assert warnings == []


def test_localhost_fallback(monkeypatch):
    monkeypatch.setattr(network, "_udp_detect", lambda: None)
    monkeypatch.setattr(network, "_hostname_detect", lambda: None)
    ip, warnings = detect_lan_ip()
    assert ip == "127.0.0.1"
    assert warnings and "No LAN address" in warnings[0]


@pytest.mark.parametrize(
    "value, usable",
    [
        ("192.168.1.10", True),
        ("10.1.2.3", True),
        ("127.0.0.1", False),
        ("169.254.1.1", False),
        ("0.0.0.0", False),
        ("::1", False),
        ("not-an-ip", False),
    ],
)
def test_is_usable_remote_ip(value, usable):
    assert is_usable_remote_ip(value) is usable


def test_pairing_url_uses_fragment_not_query():
    urls = build_urls("192.168.1.23", 8000, "004281")
    assert urls["pairing"] == "http://192.168.1.23:8000/#pin=004281"
    assert "?pin=" not in urls["pairing"]


def test_ipv6_host_is_bracketed():
    urls = build_urls("fd00::5", 8000, "123456")
    assert urls["pairing"].startswith("http://[fd00::5]:8000/#pin=")


def test_render_qr_produces_block_glyphs():
    art = render_qr("http://192.168.1.23:8000/#pin=004281")
    assert art
    assert any(char in art for char in "\u2588\u2580\u2584")
    assert len(art.splitlines()) > 5


def test_banner_contains_required_information(settings_factory):
    settings = settings_factory(port=8123)
    buffer = io.StringIO()
    print_banner(settings, pin="004281", ip="192.168.1.23", stream=buffer)
    output = buffer.getvalue()
    assert "BIFROST" in output
    assert "004281" in output
    assert "http://localhost:8123/" in output
    assert "http://192.168.1.23:8123/" in output
    assert "#pin=004281" in output
    assert "?pin=" not in output
    assert str(settings.data_dir) in output
    assert "5.0 GB" in output


def test_banner_falls_back_to_plain_text_on_encoding_error(settings_factory):
    class AsciiConsole:
        """Mimics a Windows console that cannot render Unicode block glyphs."""

        def __init__(self) -> None:
            self.writes: list[str] = []

        def write(self, text: str) -> None:
            try:
                text.encode("ascii")
            except UnicodeEncodeError as exc:
                raise UnicodeEncodeError("ascii", text, 0, 1, "ordinal not in range") from exc
            self.writes.append(text)

        def flush(self) -> None:
            return None

    console = AsciiConsole()
    settings = settings_factory()
    print_banner(settings, pin="004281", ip="192.168.1.23", stream=console)  # must not raise
    output = "".join(console.writes)
    assert "BIFROST is running." in output
    assert "004281" in output
    assert "#pin=004281" in output


# --------------------------------------------------------------- configuration
def test_config_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("BIFROST_PORT", "9001")
    monkeypatch.setenv("BIFROST_MAX_UPLOAD_BYTES", "10485760")
    monkeypatch.setenv("BIFROST_COOKIE_SECURE", "1")
    monkeypatch.setenv("BIFROST_DATA_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("BIFROST_HOST_IP", "192.168.1.77")

    settings = Settings.from_env()
    assert settings.port == 9001
    assert settings.max_upload_bytes == 10485760
    assert settings.cookie_secure is True
    assert settings.data_dir == tmp_path / "storage"
    assert settings.host_ip == "192.168.1.77"


def test_relative_data_dir_resolves_against_project(monkeypatch):
    monkeypatch.setenv("BIFROST_DATA_DIR", "data-test-tmp")
    settings = Settings.from_env()
    assert settings.data_dir == PROJECT_DIR / "data-test-tmp"


def test_default_data_dir_is_project_relative(monkeypatch):
    for name in (
        "BIFROST_DATA_DIR",
        "BIFROST_PORT",
        "BIFROST_MAX_UPLOAD_BYTES",
        "BIFROST_COOKIE_SECURE",
        "BIFROST_HOST_IP",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings.from_env()
    assert settings.data_dir == PROJECT_DIR / "data"
    assert settings.port == 8000
    assert settings.max_upload_bytes == 5 * 1024**3
    assert settings.min_free_space_bytes == 256 * 1024 * 1024
    assert settings.session_days == 30
    assert settings.pin_attempts == 5
    assert settings.lockout_seconds == 60
    assert settings.max_note_chars == 100_000
    assert settings.max_device_name_chars == 40


@pytest.mark.parametrize(
    "name, value",
    [
        ("BIFROST_PORT", "not-a-number"),
        ("BIFROST_PORT", "70000"),
        ("BIFROST_PORT", "0"),
        ("BIFROST_MAX_UPLOAD_BYTES", "0"),
        ("BIFROST_UPLOAD_CHUNK_BYTES", "12"),
        ("BIFROST_HOST_IP", "not-an-ip"),
        ("BIFROST_COOKIE_SECURE", "maybe"),
        ("BIFROST_SESSION_DAYS", "0"),
    ],
)
def test_invalid_configuration_fails_fast(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError):
        Settings.from_env()


def test_validation_rejects_bad_values():
    with pytest.raises(ConfigError):
        Settings(port=0).validate()
    with pytest.raises(ConfigError):
        Settings(upload_chunk_bytes=1).validate()  # below the 4 KiB floor
    with pytest.raises(ConfigError):
        Settings(max_note_chars=0).validate()
    with pytest.raises(ConfigError):
        Settings(min_free_space_bytes=-1).validate()
    with pytest.raises(ConfigError):
        Settings(host_ip="999.1.1.1").validate()


def test_data_dir_can_be_overridden_outside_project(tmp_path):
    settings = Settings(data_dir=tmp_path / "elsewhere")
    settings.ensure_directories()
    assert settings.files_dir.parent == tmp_path / "elsewhere"


def test_no_access_log_requirements_are_documented():
    """Guardrail: the start script must not enable --reload or multiple workers."""
    script = (PROJECT_DIR / "start.bat").read_text(encoding="utf-8", errors="replace")
    assert "--reload" not in script
    assert "--workers 1" in script
    assert "--timeout-graceful-shutdown 10" in script
    assert "--no-access-log" in script
    assert "PYTHONUTF8=1" in script
    assert "chcp 65001" in script
