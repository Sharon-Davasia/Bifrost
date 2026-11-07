"""SQLite persistence layer for Bifrost.

Design notes
------------
* One connection per operation, always closed in a ``finally`` block.
* WAL journal mode, ``busy_timeout`` of five seconds and foreign keys enabled on
  every connection.
* Writes use explicit transactions (``BEGIN IMMEDIATE``) so that concurrent
  uploads can never claim the same display filename.
* Every statement is parameterised.

The module is fully synchronous: callers on the event loop must wrap calls in
``starlette.concurrency.run_in_threadpool``.
"""

from __future__ import annotations

import contextlib
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

ITEM_COLUMNS = "id, kind, name, stored_name, size, mime, text, sender, created_at"

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('file', 'note')),
    name        TEXT NOT NULL,
    stored_name TEXT,
    size        INTEGER NOT NULL DEFAULT 0,
    mime        TEXT,
    text        TEXT,
    sender      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_items_created_at
    ON items (created_at DESC);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    device      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_expires_at
    ON sessions (expires_at);
"""


def utc_now_iso() -> str:
    """Current UTC time as a sortable ISO-8601 string (microsecond precision).

    Microseconds matter: uploads and notes can be created in the same second and
    the inbox must still order them deterministically.
    """
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def to_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass(frozen=True)
class Item:
    id: str
    kind: str
    name: str
    stored_name: str | None
    size: int
    mime: str | None
    text: str | None
    sender: str
    created_at: str

    def to_public(self) -> dict[str, Any]:
        mime = self.mime or "application/octet-stream"
        is_image = self.kind == "file" and mime.startswith("image/")
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "size": self.size,
            "mime": self.mime,
            "text": self.text,
            "sender": self.sender,
            "created_at": self.created_at,
            "is_image": is_image,
            "download_url": f"/api/files/{self.id}" if self.kind == "file" else None,
            "thumb_url": f"/api/thumb/{self.id}" if is_image else None,
        }


@dataclass(frozen=True)
class Session:
    token_hash: str
    device: str
    created_at: str
    expires_at: str

    @property
    def expires_at_dt(self) -> datetime:
        return parse_iso(self.expires_at)


class DuplicateItemError(RuntimeError):
    """Raised when an item id already exists (practically impossible)."""


class Database:
    """Small, explicit data-access object for the Bifrost SQLite database."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ----------------------------------------------------------- connections
    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path),
            timeout=5.0,
            isolation_level=None,  # explicit transaction control
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            connection.close()
            raise
        return connection

    @contextlib.contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextlib.contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """Run a write transaction; commit on success, roll back on any error."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
                raise
            else:
                connection.execute("COMMIT")

    # --------------------------------------------------------------- schema
    def init_schema(self) -> None:
        with self._connection() as connection:
            connection.executescript(SCHEMA)

    # ---------------------------------------------------------------- items
    def insert_file(
        self,
        *,
        name: str,
        stored_name: str,
        size: int,
        mime: str | None,
        sender: str,
        created_at: str | None = None,
        item_id: str | None = None,
    ) -> Item:
        """Insert a file item, resolving display-name collisions atomically."""
        item_id = item_id or uuid.uuid4().hex
        created_at = created_at or utc_now_iso()
        with self._write() as connection:
            display_name = self._unique_display_name(connection, name)
            connection.execute(
                "INSERT INTO items (id, kind, name, stored_name, size, mime, text, sender,"
                " created_at) VALUES (?, 'file', ?, ?, ?, ?, NULL, ?, ?)",
                (item_id, display_name, stored_name, size, mime, sender, created_at),
            )
        return Item(
            id=item_id,
            kind="file",
            name=display_name,
            stored_name=stored_name,
            size=size,
            mime=mime,
            text=None,
            sender=sender,
            created_at=created_at,
        )

    def insert_note(
        self,
        *,
        text: str,
        name: str,
        sender: str,
        created_at: str | None = None,
        item_id: str | None = None,
    ) -> Item:
        item_id = item_id or uuid.uuid4().hex
        created_at = created_at or utc_now_iso()
        with self._write() as connection:
            connection.execute(
                "INSERT INTO items (id, kind, name, stored_name, size, mime, text, sender,"
                " created_at) VALUES (?, 'note', ?, NULL, ?, NULL, ?, ?, ?)",
                (item_id, name, len(text), text, sender, created_at),
            )
        return Item(
            id=item_id,
            kind="note",
            name=name,
            stored_name=None,
            size=len(text),
            mime=None,
            text=text,
            sender=sender,
            created_at=created_at,
        )

    @staticmethod
    def _unique_display_name(connection: sqlite3.Connection, desired: str) -> str:
        """Return ``desired`` or ``name (n).ext`` so display names stay unique.

        Must be called from inside a ``BEGIN IMMEDIATE`` transaction so that two
        concurrent uploads cannot be handed the same name.
        """
        stem, extension = _split_extension(desired)
        candidate = desired
        counter = 0
        while True:
            row = connection.execute(
                "SELECT 1 FROM items WHERE name = ? LIMIT 1", (candidate,)
            ).fetchone()
            if row is None:
                return candidate
            counter += 1
            candidate = f"{stem} ({counter}){extension}"
            if counter > 10_000:  # pragma: no cover - defensive bound
                return f"{stem} ({uuid.uuid4().hex[:8]}){extension}"

    def list_items(self, limit: int = 1000) -> list[Item]:
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT {ITEM_COLUMNS} FROM items ORDER BY created_at DESC, rowid DESC"
                " LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_item(row) for row in rows]

    def get_item(self, item_id: str) -> Item | None:
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT {ITEM_COLUMNS} FROM items WHERE id = ?", (item_id,)
            ).fetchone()
        return _row_to_item(row) if row else None

    def delete_item(self, item_id: str) -> Item | None:
        """Delete item metadata inside a transaction and return the deleted row."""
        with self._write() as connection:
            row = connection.execute(
                f"SELECT {ITEM_COLUMNS} FROM items WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                return None
            connection.execute("DELETE FROM items WHERE id = ?", (item_id,))
        return _row_to_item(row)

    def stored_names(self) -> set[str]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT stored_name FROM items WHERE stored_name IS NOT NULL"
            ).fetchall()
        return {row["stored_name"] for row in rows}

    def item_ids(self) -> set[str]:
        with self._connection() as connection:
            rows = connection.execute("SELECT id FROM items").fetchall()
        return {row["id"] for row in rows}

    # ------------------------------------------------------------- sessions
    def create_session(
        self, *, token_hash: str, device: str, created_at: datetime, expires_at: datetime
    ) -> Session:
        created_iso = to_iso(created_at)
        expires_iso = to_iso(expires_at)
        with self._write() as connection:
            connection.execute(
                "INSERT INTO sessions (token_hash, device, created_at, expires_at)"
                " VALUES (?, ?, ?, ?)",
                (token_hash, device, created_iso, expires_iso),
            )
        return Session(
            token_hash=token_hash,
            device=device,
            created_at=created_iso,
            expires_at=expires_iso,
        )

    def get_session(self, token_hash: str, now: datetime) -> Session | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT token_hash, device, created_at, expires_at FROM sessions"
                " WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
        if row is None:
            return None
        session = Session(
            token_hash=row["token_hash"],
            device=row["device"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )
        if session.expires_at_dt <= _aware(now):
            return None
        return session

    def delete_session(self, token_hash: str) -> None:
        with self._write() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def delete_expired_sessions(self, now: datetime) -> int:
        with self._write() as connection:
            cursor = connection.execute(
                "DELETE FROM sessions WHERE expires_at <= ?", (to_iso(_aware(now)),)
            )
            return cursor.rowcount or 0


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _split_extension(name: str) -> tuple[str, str]:
    dot = name.rfind(".")
    if dot <= 0 or dot == len(name) - 1:
        return name, ""
    extension = name[dot:]
    if len(extension) > 24:
        return name, ""
    return name[:dot], extension


def _row_to_item(row: sqlite3.Row) -> Item:
    return Item(
        id=row["id"],
        kind=row["kind"],
        name=row["name"],
        stored_name=row["stored_name"],
        size=int(row["size"]),
        mime=row["mime"],
        text=row["text"],
        sender=row["sender"],
        created_at=row["created_at"],
    )
