import concurrent.futures
from pathlib import Path

import pytest

from database import Database


@pytest.fixture
def db(tmp_path: Path) -> Database:
    db_file = tmp_path / "test_queue.db"
    return Database(db_file)


def test_init_db(tmp_path: Path):
    db_file = tmp_path / "init_check.db"
    db = Database(db_file)
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        assert "batches" in tables
        assert "batch_media" in tables


def test_upsert_single_message_new_batch(db: Database):
    batch_id = db.upsert_message(
        chat_id=12345,
        article_url="https://example.com/tech-news",
    )
    assert isinstance(batch_id, int)
    batch = db.get_batch(batch_id)
    assert batch is not None
    assert batch.id == batch_id
    assert batch.chat_id == 12345
    assert batch.article_url == "https://example.com/tech-news"
    assert batch.status == "pending"
    assert batch.media_group_id is None
    assert len(batch.media) == 0


def test_upsert_grouped_media_same_batch(db: Database):
    # First message in media group with link
    batch_id_1 = db.upsert_message(
        chat_id=100,
        media_group_id="group_abc",
        article_url="https://news.ycombinator.com/item?id=1",
        file_type="photo",
        file_path="/tmp/photo1.jpg",
    )

    # Second message in media group with another photo
    batch_id_2 = db.upsert_message(
        chat_id=100,
        media_group_id="group_abc",
        file_type="photo",
        file_path="/tmp/photo2.jpg",
    )

    # Third message in media group with a video
    batch_id_3 = db.upsert_message(
        chat_id=100,
        media_group_id="group_abc",
        file_type="video",
        file_path="/tmp/video1.mp4",
    )

    assert batch_id_1 == batch_id_2 == batch_id_3

    batch = db.get_batch(batch_id_1)
    assert batch is not None
    assert batch.article_url == "https://news.ycombinator.com/item?id=1"
    assert batch.media_group_id == "group_abc"

    media_items = db.get_batch_media(batch_id_1)
    assert len(media_items) == 3
    assert media_items[0].file_type == "photo"
    assert media_items[0].file_path == "/tmp/photo1.jpg"
    assert media_items[1].file_type == "photo"
    assert media_items[1].file_path == "/tmp/photo2.jpg"
    assert media_items[2].file_type == "video"
    assert media_items[2].file_path == "/tmp/video1.mp4"


def test_upsert_resets_debounce_timer(db: Database):
    batch_id = db.upsert_message(chat_id=200, article_url="https://test.com")
    batch_before = db.get_batch(batch_id)
    assert batch_before is not None

    # Manually backdate the batch's last_updated_at to 30 seconds ago
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE batches SET last_updated_at = datetime('now', '-30 seconds') WHERE id = ?",
            (batch_id,),
        )

    # Verify backdated
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT (strftime('%s', 'now') - strftime('%s', last_updated_at)) FROM batches WHERE id = ?",
            (batch_id,),
        ).fetchone()
        assert row[0] >= 30

    # Upsert a new message into the same chat using record_message alias
    db.record_message(chat_id=200, file_type="photo", file_path="/tmp/p.jpg")

    # Inactivity timer should now be reset (~0 seconds)
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT (strftime('%s', 'now') - strftime('%s', last_updated_at)) FROM batches WHERE id = ?",
            (batch_id,),
        ).fetchone()
        assert row[0] < 5


def test_get_expired_batches(db: Database):
    # Batch 1: Active, fresh (0 seconds elapsed)
    b1 = db.upsert_message(chat_id=1, article_url="https://active.com")

    # Batch 2: Expired (simulate 20s elapsed)
    b2 = db.upsert_message(chat_id=2, article_url="https://expired.com")
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE batches SET last_updated_at = datetime('now', '-20 seconds') WHERE id = ?",
            (b2,),
        )

    # Query with default debounce_seconds=15
    expired = db.get_expired_batches(debounce_seconds=15)
    expired_ids = [b.id for b in expired]

    assert b2 in expired_ids
    assert b1 not in expired_ids


def test_claim_batch_atomic_lock(db: Database):
    batch_id = db.upsert_message(chat_id=300, article_url="https://claim-test.com")

    # First claim must succeed
    claimed_first = db.claim_batch(batch_id)
    assert claimed_first is True

    batch = db.get_batch(batch_id)
    assert batch is not None
    assert batch.status == "processing"

    # Second claim must fail (already claimed / not pending)
    claimed_second = db.claim_batch(batch_id)
    assert claimed_second is False


def test_claim_batch_nonexistent(db: Database):
    assert db.claim_batch(999999) is False
    assert db.mark_batch_processing(999999) is False
    assert db.mark_completed(999999) is False
    assert db.mark_batch_completed(999999) is False
    assert db.mark_failed(999999) is False
    assert db.mark_batch_failed(999999) is False
    assert db.get_batch(999999) is None


def test_concurrent_claim_batch(db: Database):
    batch_id = db.upsert_message(chat_id=400, article_url="https://race-condition.com")

    # 10 threads trying to claim the same batch at the same time
    results: list[bool] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(db.claim_batch, batch_id) for _ in range(10)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    # Exactly one thread must have acquired the lock
    assert results.count(True) == 1
    assert results.count(False) == 9

    batch = db.get_batch(batch_id)
    assert batch is not None
    assert batch.status == "processing"


def test_batch_status_transitions(db: Database):
    batch_id = db.upsert_message(chat_id=500, article_url="https://transitions.com")

    # Pending -> Processing
    assert db.claim_batch(batch_id) is True

    # Processing -> Completed
    yt_url = "https://youtube.com/shorts/abc123xyz"
    assert db.mark_completed(batch_id, youtube_url=yt_url) is True

    batch = db.get_batch(batch_id)
    assert batch is not None
    assert batch.status == "completed"
    assert batch.youtube_url == yt_url

    # Another batch for failure transition
    fail_id = db.upsert_message(chat_id=501, article_url="https://fail.com")
    assert db.claim_batch(fail_id) is True

    err_msg = "Render timeout after 180s"
    assert db.mark_failed(fail_id, error_message=err_msg) is True

    fail_batch = db.get_batch(fail_id)
    assert fail_batch is not None
    assert fail_batch.status == "failed"
    assert fail_batch.error_message == err_msg


def test_module_level_helpers(tmp_path: Path):
    db_file = tmp_path / "helper_test.db"
    from database import (
        claim_batch,
        get_batch,
        get_batch_media,
        get_expired_batches,
        init_db,
        mark_completed,
        mark_failed,
        upsert_message,
    )

    init_db(db_file)
    b_id = upsert_message(
        chat_id=600,
        article_url="https://helper.com",
        file_type="photo",
        file_path="/tmp/pic.jpg",
        db_path=db_file,
    )
    assert b_id > 0

    batch = get_batch(b_id, db_path=db_file)
    assert batch is not None
    assert batch.id == b_id

    media = get_batch_media(b_id, db_path=db_file)
    assert len(media) == 1

    # Should not be expired yet
    assert len(get_expired_batches(debounce_seconds=15, db_path=db_file)) == 0

    assert claim_batch(b_id, db_path=db_file) is True
    assert mark_completed(b_id, youtube_url="https://yt.com", db_path=db_file) is True
    assert mark_failed(b_id, error_message="err", db_path=db_file) is True
