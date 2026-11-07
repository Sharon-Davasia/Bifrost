"""Central configuration for Bifrost.

Every tunable value is parsed from the environment once, validated, and exposed
through a frozen :class:`Settings` instance.  Invalid configuration fails loudly
at startup instead of mid-transfer.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, replace
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent

DEFAULT_DATA_DIR = PROJECT_DIR / "data"
DEFAULT_MAX_UPLOAD_BYTES = 5 * 1024 * 1024 * 1024  # 5 GB
DEFAULT_UPLOAD_CHUNK_BYTES = 1024 * 1024  # 1 MB
DEFAULT_MIN_FREE_SPACE_BYTES = 256 * 1024 * 1024  # 256 MB
DEFAULT_SESSION_DAYS = 30
DEFAULT_PIN_ATTEMPTS = 5
DEFAULT_LOCKOUT_SECONDS = 60
DEFAULT_MAX_NOTE_CHARS = 100_000
DEFAULT_MAX_DEVICE_NAME_CHARS = 40
DEFAULT_MAX_FILENAME_CHARS = 150
DEFAULT_THUMB_MAX_PX = 320


class ConfigError(RuntimeError):
    """Raised when an environment variable cannot be turned into a valid setting."""


def _env_str(name: str) -> str | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    raw = raw.strip()
    return raw or None


def _env_int(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw = _env_str(name)
    if raw is None:
        return default
    try:
        value = int(raw, 10)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}.") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}.")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} must be <= {maximum}, got {value}.")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_str(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in {"1", "true", "yes", "on", "y"}:
        return True
    if lowered in {"0", "false", "no", "off", "n"}:
        return False
    raise ConfigError(f"{name} must be a boolean flag (1/0, true/false), got {raw!r}.")


def _env_path(name: str, default: Path) -> Path:
    raw = _env_str(name)
    if raw is None:
        return default
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = PROJECT_DIR / candidate
    return candidate


@dataclass(frozen=True)
class Settings:
    """Validated, immutable application configuration."""

    port: int = 8000
    host_ip: str | None = None
    data_dir: Path = DEFAULT_DATA_DIR
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    upload_chunk_bytes: int = DEFAULT_UPLOAD_CHUNK_BYTES
    min_free_space_bytes: int = DEFAULT_MIN_FREE_SPACE_BYTES
    session_days: int = DEFAULT_SESSION_DAYS
    pin_attempts: int = DEFAULT_PIN_ATTEMPTS
    lockout_seconds: int = DEFAULT_LOCKOUT_SECONDS
    max_note_chars: int = DEFAULT_MAX_NOTE_CHARS
    max_device_name_chars: int = DEFAULT_MAX_DEVICE_NAME_CHARS
    max_filename_chars: int = DEFAULT_MAX_FILENAME_CHARS
    cookie_secure: bool = False
    thumb_max_px: int = DEFAULT_THUMB_MAX_PX
    #: Suppress the startup banner (used by the automated test-suite).
    quiet: bool = False

    # ------------------------------------------------------------------ paths
    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / "thumbs"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bifrost.db"

    @property
    def session_seconds(self) -> int:
        return self.session_days * 24 * 60 * 60

    @property
    def cookie_max_age(self) -> int:
        return self.session_seconds

    # -------------------------------------------------------------- factories
    @classmethod
    def from_env(cls) -> "Settings":
        settings = cls(
            port=_env_int("BIFROST_PORT", 8000, minimum=1, maximum=65535),
            host_ip=_env_str("BIFROST_HOST_IP"),
            data_dir=_env_path("BIFROST_DATA_DIR", DEFAULT_DATA_DIR),
            max_upload_bytes=_env_int(
                "BIFROST_MAX_UPLOAD_BYTES", DEFAULT_MAX_UPLOAD_BYTES, minimum=1
            ),
            upload_chunk_bytes=_env_int(
                "BIFROST_UPLOAD_CHUNK_BYTES",
                DEFAULT_UPLOAD_CHUNK_BYTES,
                minimum=4096,
                maximum=64 * 1024 * 1024,
            ),
            min_free_space_bytes=_env_int(
                "BIFROST_MIN_FREE_SPACE_BYTES", DEFAULT_MIN_FREE_SPACE_BYTES, minimum=0
            ),
            session_days=_env_int("BIFROST_SESSION_DAYS", DEFAULT_SESSION_DAYS, minimum=1),
            pin_attempts=_env_int("BIFROST_PIN_ATTEMPTS", DEFAULT_PIN_ATTEMPTS, minimum=1),
            lockout_seconds=_env_int(
                "BIFROST_LOCKOUT_SECONDS", DEFAULT_LOCKOUT_SECONDS, minimum=0
            ),
            max_note_chars=_env_int("BIFROST_MAX_NOTE_CHARS", DEFAULT_MAX_NOTE_CHARS, minimum=1),
            max_device_name_chars=_env_int(
                "BIFROST_MAX_DEVICE_NAME_CHARS", DEFAULT_MAX_DEVICE_NAME_CHARS, minimum=1
            ),
            cookie_secure=_env_bool("BIFROST_COOKIE_SECURE", False),
            quiet=_env_bool("BIFROST_QUIET", False),
        )
        settings.validate()
        return settings

    # ------------------------------------------------------------- validation
    def validate(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ConfigError(f"BIFROST_PORT must be between 1 and 65535, got {self.port}.")
        if self.host_ip:
            try:
                ipaddress.ip_address(self.host_ip)
            except ValueError as exc:
                raise ConfigError(
                    f"BIFROST_HOST_IP must be a valid IP address, got {self.host_ip!r}."
                ) from exc
        if self.max_upload_bytes < 1:
            raise ConfigError("BIFROST_MAX_UPLOAD_BYTES must be a positive integer.")
        if not 4096 <= self.upload_chunk_bytes <= 64 * 1024 * 1024:
            raise ConfigError(
                "BIFROST_UPLOAD_CHUNK_BYTES must be between 4096 and 67108864, "
                f"got {self.upload_chunk_bytes}."
            )
        if self.min_free_space_bytes < 0:
            raise ConfigError("BIFROST_MIN_FREE_SPACE_BYTES cannot be negative.")
        if self.session_days < 1:
            raise ConfigError("BIFROST_SESSION_DAYS must be at least 1.")
        if self.pin_attempts < 1:
            raise ConfigError("BIFROST_PIN_ATTEMPTS must be at least 1.")
        if self.lockout_seconds < 0:
            raise ConfigError("BIFROST_LOCKOUT_SECONDS cannot be negative.")
        if self.max_note_chars < 1:
            raise ConfigError("BIFROST_MAX_NOTE_CHARS must be at least 1.")
        if not 1 <= self.max_device_name_chars <= 200:
            raise ConfigError("BIFROST_MAX_DEVICE_NAME_CHARS must be between 1 and 200.")
        if self.thumb_max_px < 16:
            raise ConfigError("Thumbnail size must be at least 16 pixels.")

    # ------------------------------------------------------------- utilities
    def with_overrides(self, **kwargs: object) -> "Settings":
        """Return a copy with overrides applied and re-validated."""
        updated = replace(self, **kwargs)
        updated.validate()
        return updated

    def ensure_directories(self) -> None:
        """Create the data, files and thumbnail directories if missing."""
        for path in (self.data_dir, self.files_dir, self.thumbs_dir):
            path.mkdir(parents=True, exist_ok=True)
