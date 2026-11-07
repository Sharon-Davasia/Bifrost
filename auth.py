"""Authentication primitives for Bifrost.

Contents
--------
* :class:`Clock` / :class:`SystemClock` - injectable time source so tests can
  jump 60 seconds or 30 days without sleeping.
* :func:`generate_pin` / :func:`pin_matches` - six digit pairing PIN.
* :class:`PinRateLimiter` - per-client-IP lockout after N incorrect attempts.
* :class:`SessionManager` - opaque bearer tokens stored only as SHA-256 hashes.
* :func:`sanitize_device_name` - device label normalisation.

Security notes
--------------
The PIN and the raw session tokens never reach the logger, the database or the
public API. Only the SHA-256 hash of a session token is persisted.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from database import Database, Session

COOKIE_NAME = "bifrost_session"
DEFAULT_DEVICE_NAME = "Unknown device"

#: Zero-width joiner: kept so emoji sequences survive sanitisation.
_ZWJ = "\u200d"


class Clock(Protocol):
    """Injectable time source."""

    def now(self) -> datetime:
        """Timezone-aware current UTC time."""

    def monotonic(self) -> float:
        """Monotonic seconds, used for lockout maths."""


class SystemClock:
    """Production clock backed by the operating system."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()


# ---------------------------------------------------------------------- PIN
def generate_pin() -> str:
    """Return a fresh six-digit PIN, preserving leading zeros."""
    return f"{secrets.randbelow(1_000_000):06d}"


def pin_matches(submitted: str, expected: str) -> bool:
    """Constant-time PIN comparison."""
    return secrets.compare_digest(submitted, expected)


def is_six_digit_pin(value: str) -> bool:
    return len(value) == 6 and value.isdigit() and value.isascii()


# --------------------------------------------------------------- rate limiter
@dataclass
class _Attempts:
    failures: int = 0
    locked_until: float | None = None


class PinRateLimiter:
    """In-memory per-IP PIN failure tracker.

    Rules
    -----
    * ``max_attempts`` incorrect PINs per IP, then a ``lockout_seconds`` lockout.
    * The final incorrect attempt already reports a lockout.
    * Any request during the lockout is refused - even with the correct PIN.
    * A successful authentication clears the counter.
    """

    #: Entries untouched for this long are pruned so memory stays bounded.
    _STALE_SECONDS = 60 * 60

    def __init__(
        self,
        clock: Clock,
        *,
        max_attempts: int = 5,
        lockout_seconds: int = 60,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if lockout_seconds < 0:
            raise ValueError("lockout_seconds cannot be negative")
        self._clock = clock
        self._max_attempts = max_attempts
        self._lockout_seconds = float(lockout_seconds)
        self._entries: dict[str, _Attempts] = {}
        self._last_prune = clock.monotonic()
        # threading.Lock keeps the structure safe even if FastAPI runs a
        # synchronous endpoint in the worker threadpool.
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ api
    def lockout_remaining(self, client_ip: str) -> float:
        """Seconds left on an active lockout for ``client_ip`` (0 when free)."""
        with self._lock:
            entry = self._entries.get(client_ip)
            if entry is None or entry.locked_until is None:
                return 0.0
            remaining = entry.locked_until - self._clock.monotonic()
            if remaining <= 0:
                # Lockout expired: reset so the client may try again.
                self._entries.pop(client_ip, None)
                return 0.0
            return remaining

    def is_locked(self, client_ip: str) -> bool:
        return self.lockout_remaining(client_ip) > 0

    def record_failure(self, client_ip: str) -> bool:
        """Register an incorrect attempt. Returns True when now locked out."""
        now = self._clock.monotonic()
        with self._lock:
            self._prune(now)
            entry = self._entries.setdefault(client_ip, _Attempts())
            if entry.locked_until is not None:
                # Already locked out (defensive; callers check first).
                return entry.locked_until > now
            entry.failures += 1
            if entry.failures >= self._max_attempts:
                entry.locked_until = now + self._lockout_seconds
                return True
            return False

    def record_success(self, client_ip: str) -> None:
        with self._lock:
            self._entries.pop(client_ip, None)

    def reset(self) -> None:
        """Forget every tracked address (used by tests)."""
        with self._lock:
            self._entries.clear()

    @property
    def tracked_clients(self) -> int:
        with self._lock:
            return len(self._entries)

    # -------------------------------------------------------------- internal
    def _prune(self, now: float) -> None:
        if now - self._last_prune < self._STALE_SECONDS:
            return
        self._last_prune = now
        stale = [
            ip
            for ip, entry in self._entries.items()
            if entry.locked_until is None and entry.failures == 0
        ]
        for ip in stale:
            self._entries.pop(ip, None)


# ------------------------------------------------------------------- sessions
def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SessionManager:
    """Issues and validates opaque session tokens."""

    def __init__(self, db: Database, clock: Clock, *, session_days: int = 30) -> None:
        if session_days < 1:
            raise ValueError("session_days must be at least 1")
        self._db = db
        self._clock = clock
        self._session_days = session_days

    @property
    def lifetime(self) -> timedelta:
        return timedelta(days=self._session_days)

    def create(self, device: str) -> tuple[str, Session]:
        """Create a session, returning ``(raw_token, stored_session)``."""
        raw_token = secrets.token_urlsafe(32)
        now = self._clock.now()
        session = self._db.create_session(
            token_hash=hash_token(raw_token),
            device=device,
            created_at=now,
            expires_at=now + self.lifetime,
        )
        return raw_token, session

    def validate(self, raw_token: str | None) -> Session | None:
        """Return the session for a raw token, or ``None`` if invalid/expired."""
        if not raw_token:
            return None
        token_hash = hash_token(raw_token)
        # Constant-ish work regardless of existence: the lookup is a primary key
        # hit and the comparison happens inside SQLite.
        return self._db.get_session(token_hash, self._clock.now())

    def revoke(self, raw_token: str) -> None:
        self._db.delete_session(hash_token(raw_token))

    def prune_expired(self) -> int:
        return self._db.delete_expired_sessions(self._clock.now())


# -------------------------------------------------------------- device labels
def sanitize_device_name(
    raw: str | None,
    *,
    max_chars: int = 40,
    fallback: str = DEFAULT_DEVICE_NAME,
) -> str:
    """Normalise a user-supplied device name.

    Removes control/format characters (keeping the emoji zero-width joiner),
    collapses whitespace, trims, truncates and supplies a fallback if empty.
    """
    if not raw:
        return fallback
    text = unicodedata.normalize("NFC", raw)
    cleaned: list[str] = []
    for char in text:
        category = unicodedata.category(char)
        if char == _ZWJ:
            cleaned.append(char)
            continue
        if category in {"Cc", "Cf", "Cs", "Co", "Cn"}:
            cleaned.append(" ")
            continue
        cleaned.append(char)
    collapsed = " ".join("".join(cleaned).split())
    if not collapsed:
        return fallback
    if len(collapsed) > max_chars:
        collapsed = collapsed[:max_chars].rstrip()
    return collapsed or fallback
