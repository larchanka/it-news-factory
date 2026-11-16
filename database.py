from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class BatchMedia:
    id: int
    batch_id: int
    file_type: str  # 'photo', 'video'
    file_path: str


@dataclass
class Batch:
    id: int
    chat_id: int
    media_group_id: str | None = None
    article_url: str | None = None
    status: str = "pending"  # 'pending', 'processing', 'completed', 'failed'
    youtube_url: str | None = None
    error_message: str | None = None
    created_at: str | None = None
    last_updated_at: str | None = None
    media: list[BatchMedia] = field(default_factory=list)


class Database:
    """SQLite manager for Telegram media debounce queuing and state transitions."""

    def __init__(self, db_path: str | Path = "queue.db") -> None:
        self.db_path = str(db_path)
        self.init_db()

    @contextmanager
    def get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_db(self) -> None:
        with self.get_connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    media_group_id TEXT,
                    article_url TEXT,
                    status TEXT DEFAULT 'pending',
                    youtube_url TEXT,
                    error_message TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS batch_media (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                    file_type TEXT NOT NULL,
                    file_path TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_batches_debounce
                    ON batches(status, last_updated_at);

                CREATE INDEX IF NOT EXISTS idx_batches_chat_group
                    ON batches(chat_id, media_group_id, status);
                """
            )

    def _find_pending_batch_id(
        self,
        conn: sqlite3.Connection,
        chat_id: int,
        media_group_id: str | None,
    ) -> int | None:
        cursor = conn.cursor()
        if media_group_id is not None:
            # 1. Match active pending batch with same media_group_id
            cursor.execute(
                """
                SELECT id FROM batches
                WHERE chat_id = ? AND media_group_id = ? AND status = 'pending'
                ORDER BY last_updated_at DESC LIMIT 1
                """,
                (chat_id, media_group_id),
            )
            row = cursor.fetchone()
            if row:
                return int(row["id"])

            # 2. Check if a pending batch without media_group_id exists in this chat
            cursor.execute(
                """
                SELECT id FROM batches
                WHERE chat_id = ? AND media_group_id IS NULL AND status = 'pending'
                ORDER BY last_updated_at DESC LIMIT 1
                """,
                (chat_id,),
            )
            row = cursor.fetchone()
            if row:
                batch_id = int(row["id"])
                cursor.execute(
                    "UPDATE batches SET media_group_id = ? WHERE id = ?",
                    (media_group_id, batch_id),
                )
                return batch_id
            return None

        # media_group_id is None: find any active pending batch for this chat
        cursor.execute(
            """
            SELECT id FROM batches
            WHERE chat_id = ? AND status = 'pending'
            ORDER BY last_updated_at DESC LIMIT 1
            """,
            (chat_id,),
        )
        row = cursor.fetchone()
        return int(row["id"]) if row else None

    def upsert_message(
        self,
        chat_id: int,
        article_url: str | None = None,
        media_group_id: str | None = None,
        file_type: str | None = None,
        file_path: str | None = None,
    ) -> int:
        """
        Record an incoming message or media attachment, resetting the 15-second debounce timer.
        Returns the batch_id.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            batch_id = self._find_pending_batch_id(conn, chat_id, media_group_id)

            if batch_id is None:
                cursor.execute(
                    """
                    INSERT INTO batches (chat_id, media_group_id, article_url, status)
                    VALUES (?, ?, ?, 'pending')
                    """,
                    (chat_id, media_group_id, article_url),
                )
                batch_id = int(cursor.lastrowid)  # type: ignore[arg-type]
            else:
                cursor.execute(
                    """
                    UPDATE batches
                    SET last_updated_at = CURRENT_TIMESTAMP,
                        article_url = COALESCE(?, article_url)
                    WHERE id = ?
                    """,
                    (article_url, batch_id),
                )

            if file_type and file_path:
                cursor.execute(
                    """
                    INSERT INTO batch_media (batch_id, file_type, file_path)
                    VALUES (?, ?, ?)
                    """,
                    (batch_id, file_type, file_path),
                )

            return batch_id

    def record_message(
        self,
        chat_id: int,
        article_url: str | None = None,
        media_group_id: str | None = None,
        file_type: str | None = None,
        file_path: str | None = None,
    ) -> int:
        """Alias for upsert_message."""
        return self.upsert_message(
            chat_id=chat_id,
            article_url=article_url,
            media_group_id=media_group_id,
            file_type=file_type,
            file_path=file_path,
        )

    def get_expired_batches(self, debounce_seconds: int = 15) -> list[Batch]:
        """
        Retrieve all pending batches whose last_updated_at inactivity exceeds debounce_seconds.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, chat_id, media_group_id, article_url, status,
                       youtube_url, error_message, created_at, last_updated_at
                FROM batches
                WHERE status = 'pending'
                  AND (strftime('%s', 'now') - strftime('%s', last_updated_at)) >= ?
                ORDER BY last_updated_at ASC
                """,
                (debounce_seconds,),
            )
            rows = cursor.fetchall()
            batches = [self._row_to_batch(row) for row in rows]

        for batch in batches:
            batch.media = self.get_batch_media(batch.id)
        return batches

    def claim_batch(self, batch_id: int) -> bool:
        """
        Atomically lock a pending batch by transitioning it to 'processing'.
        Returns True if the lock was acquired, False otherwise.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE batches
                SET status = 'processing',
                    last_updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'pending'
                """,
                (batch_id,),
            )
            return cursor.rowcount == 1

    def mark_batch_processing(self, batch_id: int) -> bool:
        """Alias for claim_batch."""
        return self.claim_batch(batch_id)

    def mark_completed(self, batch_id: int, youtube_url: str | None = None) -> bool:
        """
        Mark batch as completed, optionally storing the uploaded YouTube Shorts URL.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE batches
                SET status = 'completed',
                    youtube_url = COALESCE(?, youtube_url),
                    last_updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (youtube_url, batch_id),
            )
            return cursor.rowcount > 0

    def mark_batch_completed(self, batch_id: int, youtube_url: str | None = None) -> bool:
        """Alias for mark_completed."""
        return self.mark_completed(batch_id, youtube_url)

    def mark_failed(self, batch_id: int, error_message: str | None = None) -> bool:
        """
        Mark batch as failed with an error description.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE batches
                SET status = 'failed',
                    error_message = COALESCE(?, error_message),
                    last_updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (error_message, batch_id),
            )
            return cursor.rowcount > 0

    def mark_batch_failed(self, batch_id: int, error_message: str | None = None) -> bool:
        """Alias for mark_failed."""
        return self.mark_failed(batch_id, error_message)

    def get_batch(self, batch_id: int) -> Batch | None:
        """Fetch a batch by ID, including its associated media items."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, chat_id, media_group_id, article_url, status,
                       youtube_url, error_message, created_at, last_updated_at
                FROM batches
                WHERE id = ?
                """,
                (batch_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            batch = self._row_to_batch(row)

        batch.media = self.get_batch_media(batch_id)
        return batch

    def get_batch_media(self, batch_id: int) -> list[BatchMedia]:
        """Fetch all media files attached to a batch."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, batch_id, file_type, file_path
                FROM batch_media
                WHERE batch_id = ?
                ORDER BY id ASC
                """,
                (batch_id,),
            )
            rows = cursor.fetchall()
            return [
                BatchMedia(
                    id=row["id"],
                    batch_id=row["batch_id"],
                    file_type=row["file_type"],
                    file_path=row["file_path"],
                )
                for row in rows
            ]

    @staticmethod
    def _row_to_batch(row: sqlite3.Row) -> Batch:
        return Batch(
            id=row["id"],
            chat_id=row["chat_id"],
            media_group_id=row["media_group_id"],
            article_url=row["article_url"],
            status=row["status"],
            youtube_url=row["youtube_url"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            last_updated_at=row["last_updated_at"],
        )


# Module-level convenience functions
_default_db: Database | None = None


def _get_db(db_path: str | Path | None = None) -> Database:
    global _default_db
    if db_path is not None:
        return Database(db_path)
    if _default_db is None:
        _default_db = Database()
    return _default_db


def init_db(db_path: str | Path | None = None) -> None:
    _get_db(db_path).init_db()


def upsert_message(
    chat_id: int,
    article_url: str | None = None,
    media_group_id: str | None = None,
    file_type: str | None = None,
    file_path: str | None = None,
    db_path: str | Path | None = None,
) -> int:
    return _get_db(db_path).upsert_message(
        chat_id=chat_id,
        article_url=article_url,
        media_group_id=media_group_id,
        file_type=file_type,
        file_path=file_path,
    )


def record_message(
    chat_id: int,
    article_url: str | None = None,
    media_group_id: str | None = None,
    file_type: str | None = None,
    file_path: str | None = None,
    db_path: str | Path | None = None,
) -> int:
    return _get_db(db_path).record_message(
        chat_id=chat_id,
        article_url=article_url,
        media_group_id=media_group_id,
        file_type=file_type,
        file_path=file_path,
    )


def get_expired_batches(
    debounce_seconds: int = 15,
    db_path: str | Path | None = None,
) -> list[Batch]:
    return _get_db(db_path).get_expired_batches(debounce_seconds=debounce_seconds)


def claim_batch(batch_id: int, db_path: str | Path | None = None) -> bool:
    return _get_db(db_path).claim_batch(batch_id=batch_id)


def mark_batch_processing(batch_id: int, db_path: str | Path | None = None) -> bool:
    return _get_db(db_path).mark_batch_processing(batch_id=batch_id)


def mark_completed(
    batch_id: int,
    youtube_url: str | None = None,
    db_path: str | Path | None = None,
) -> bool:
    return _get_db(db_path).mark_completed(batch_id=batch_id, youtube_url=youtube_url)


def mark_batch_completed(
    batch_id: int,
    youtube_url: str | None = None,
    db_path: str | Path | None = None,
) -> bool:
    return _get_db(db_path).mark_batch_completed(batch_id=batch_id, youtube_url=youtube_url)


def mark_failed(
    batch_id: int,
    error_message: str | None = None,
    db_path: str | Path | None = None,
) -> bool:
    return _get_db(db_path).mark_failed(batch_id=batch_id, error_message=error_message)


def mark_batch_failed(
    batch_id: int,
    error_message: str | None = None,
    db_path: str | Path | None = None,
) -> bool:
    return _get_db(db_path).mark_batch_failed(batch_id=batch_id, error_message=error_message)


def get_batch(batch_id: int, db_path: str | Path | None = None) -> Batch | None:
    return _get_db(db_path).get_batch(batch_id=batch_id)


def get_batch_media(batch_id: int, db_path: str | Path | None = None) -> list[BatchMedia]:
    return _get_db(db_path).get_batch_media(batch_id=batch_id)
