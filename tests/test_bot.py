from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot import (
    DebounceDispatcher,
    TelegramBotService,
    create_dispatcher,
    extract_article_url,
    extract_media,
    is_chat_allowed,
    run_bot,
)
from config import AppConfig, TelegramConfig
from database import Batch, Database


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "test_bot_queue.db")


@pytest.fixture
def bot_config() -> TelegramConfig:
    return TelegramConfig(
        bot_token="test_token",
        allowed_chat_ids=[12345],
        debounce_seconds=15,
    )


def test_extract_article_url_valid() -> None:
    text = "Check this breaking story: https://techcrunch.com/2026/01/ai-news?ref=rss it is crazy!"
    assert extract_article_url(text) == "https://techcrunch.com/2026/01/ai-news?ref=rss"


def test_extract_article_url_http() -> None:
    text = "http://example.com/news"
    assert extract_article_url(text) == "http://example.com/news"


def test_extract_article_url_none() -> None:
    assert extract_article_url(None) is None
    assert extract_article_url("No link in this message") is None


def test_extract_article_url_trailing_punctuation() -> None:
    assert extract_article_url("Read this: https://example.com/news.") == "https://example.com/news"
    assert extract_article_url("See (https://example.com/news)!") == "https://example.com/news"
    assert (
        extract_article_url("Check https://example.com/article, it is great")
        == "https://example.com/article"
    )
    assert (
        extract_article_url("Look at https://example.com/story?q=1; amazing")
        == "https://example.com/story?q=1"
    )


def test_extract_media_sanitizes_path_traversal() -> None:
    # Safe filename
    msg_safe = MagicMock(photo=None, video=MagicMock(file_name="clip.mp4", file_unique_id="v1"))
    _, _, name_safe = extract_media(msg_safe)
    assert name_safe == "clip.mp4"

    # Path traversal attempt
    msg_traversal = MagicMock(
        photo=None, video=MagicMock(file_name="../../../etc/passwd", file_unique_id="v2")
    )
    _, _, name_traversal = extract_media(msg_traversal)
    assert name_traversal == "passwd"

    # Path traversal attempt with absolute path
    msg_abs = MagicMock(
        photo=None, video=MagicMock(file_name="/var/log/attack.mp4", file_unique_id="v3")
    )
    _, _, name_abs = extract_media(msg_abs)
    assert name_abs == "attack.mp4"

    # Path traversal with empty/dot name falling back to file_unique_id
    msg_dot = MagicMock(photo=None, video=MagicMock(file_name="../../../", file_unique_id="v4"))
    _, _, name_dot = extract_media(msg_dot)
    assert name_dot == "v4.mp4"


def test_is_chat_allowed() -> None:
    assert is_chat_allowed(12345, []) is True
    assert is_chat_allowed(12345, [12345, 67890]) is True
    assert is_chat_allowed(99999, [12345, 67890]) is False


@pytest.mark.asyncio
async def test_handle_message_unauthorized(
    db: Database, bot_config: TelegramConfig, tmp_path: Path
) -> None:
    service = TelegramBotService(config=bot_config, database=db, staging_dir=tmp_path / "staging")
    mock_bot = MagicMock()
    mock_message = AsyncMock()
    mock_message.chat.id = 99999
    mock_message.text = "https://example.com/news"
    mock_message.caption = None
    mock_message.photo = None
    mock_message.video = None
    mock_message.media_group_id = None

    batch_id = await service.handle_message(mock_message, mock_bot)
    assert batch_id is None
    assert len(db.get_expired_batches(debounce_seconds=0)) == 0
    mock_message.answer.assert_called_once()
    assert "Unauthorized" in mock_message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_handle_message_text_url(
    db: Database, bot_config: TelegramConfig, tmp_path: Path
) -> None:
    service = TelegramBotService(config=bot_config, database=db, staging_dir=tmp_path / "staging")
    mock_bot = MagicMock()
    mock_message = AsyncMock()
    mock_message.chat.id = 12345
    mock_message.text = "Check this: https://example.com/tech-article"
    mock_message.caption = None
    mock_message.photo = None
    mock_message.video = None
    mock_message.media_group_id = None

    batch_id = await service.handle_message(mock_message, mock_bot)
    assert isinstance(batch_id, int)

    batch = db.get_batch(batch_id)
    assert batch is not None
    assert batch.article_url == "https://example.com/tech-article"
    assert batch.chat_id == 12345
    mock_message.answer.assert_called_once()
    assert "Batch" in mock_message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_handle_message_photo_and_caption(
    db: Database, bot_config: TelegramConfig, tmp_path: Path
) -> None:
    staging_dir = tmp_path / "staging"
    service = TelegramBotService(config=bot_config, database=db, staging_dir=staging_dir)

    async def fake_download(file_obj: MagicMock, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"fake photo bytes")
        return destination

    mock_bot = MagicMock()
    mock_bot.download = AsyncMock(side_effect=fake_download)

    mock_photo = MagicMock()
    mock_photo.file_unique_id = "photo_abc"

    mock_message = AsyncMock()
    mock_message.chat.id = 12345
    mock_message.text = None
    mock_message.caption = "https://example.com/gpu-shortage"
    mock_message.photo = [mock_photo]
    mock_message.video = None
    mock_message.media_group_id = None

    batch_id = await service.handle_message(mock_message, mock_bot)
    assert isinstance(batch_id, int)

    batch = db.get_batch(batch_id)
    assert batch is not None
    assert batch.article_url == "https://example.com/gpu-shortage"
    assert len(batch.media) == 1
    assert batch.media[0].file_type == "photo"
    assert Path(batch.media[0].file_path).exists()
    assert Path(batch.media[0].file_path).read_bytes() == b"fake photo bytes"


@pytest.mark.asyncio
async def test_handle_message_video(
    db: Database, bot_config: TelegramConfig, tmp_path: Path
) -> None:
    staging_dir = tmp_path / "staging"
    service = TelegramBotService(config=bot_config, database=db, staging_dir=staging_dir)

    async def fake_download(file_obj: MagicMock, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"fake video bytes")
        return destination

    mock_bot = MagicMock()
    mock_bot.download = AsyncMock(side_effect=fake_download)

    mock_video = MagicMock()
    mock_video.file_unique_id = "vid_xyz"
    mock_video.file_name = "test_clip.mp4"

    mock_message = AsyncMock()
    mock_message.chat.id = 12345
    mock_message.text = None
    mock_message.caption = "Here is video clip https://example.com/robotics"
    mock_message.photo = None
    mock_message.video = mock_video
    mock_message.media_group_id = None

    batch_id = await service.handle_message(mock_message, mock_bot)
    assert isinstance(batch_id, int)

    batch = db.get_batch(batch_id)
    assert batch is not None
    assert batch.article_url == "https://example.com/robotics"
    assert len(batch.media) == 1
    assert batch.media[0].file_type == "video"
    assert Path(batch.media[0].file_path).exists()


@pytest.mark.asyncio
async def test_handle_media_group_aggregation(
    db: Database, bot_config: TelegramConfig, tmp_path: Path
) -> None:
    staging_dir = tmp_path / "staging"
    service = TelegramBotService(config=bot_config, database=db, staging_dir=staging_dir)

    async def fake_download(file_obj: MagicMock, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"file content")
        return destination

    mock_bot = MagicMock()
    mock_bot.download = AsyncMock(side_effect=fake_download)

    # First message: has link and photo 1
    msg1 = AsyncMock()
    msg1.chat.id = 12345
    msg1.text = None
    msg1.caption = "Breaking tech https://example.com/deepseek"
    photo1 = MagicMock(file_unique_id="p1")
    msg1.photo = [photo1]
    msg1.video = None
    msg1.media_group_id = "group_999"

    batch_id_1 = await service.handle_message(msg1, mock_bot)

    # Second message: only photo 2, same media_group_id
    msg2 = AsyncMock()
    msg2.chat.id = 12345
    msg2.text = None
    msg2.caption = None
    photo2 = MagicMock(file_unique_id="p2")
    msg2.photo = [photo2]
    msg2.video = None
    msg2.media_group_id = "group_999"

    batch_id_2 = await service.handle_message(msg2, mock_bot)

    assert batch_id_1 == batch_id_2
    assert batch_id_1 is not None
    batch = db.get_batch(batch_id_1)
    assert batch is not None
    assert batch.article_url == "https://example.com/deepseek"
    assert len(batch.media) == 2

    # Acknowledgment should only be sent once per media group
    assert msg1.answer.call_count == 1
    assert msg2.answer.call_count == 0


@pytest.mark.asyncio
async def test_handle_message_no_url_no_media(
    db: Database, bot_config: TelegramConfig, tmp_path: Path
) -> None:
    service = TelegramBotService(config=bot_config, database=db, staging_dir=tmp_path / "staging")
    mock_bot = MagicMock()
    mock_message = AsyncMock()
    mock_message.chat.id = 12345
    mock_message.text = "Hello just saying hi!"
    mock_message.caption = None
    mock_message.photo = None
    mock_message.video = None
    mock_message.media_group_id = None

    batch_id = await service.handle_message(mock_message, mock_bot)
    assert batch_id is None
    mock_message.answer.assert_called_once()
    assert "attach media" in mock_message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_dispatcher_processes_expired_batch_success(
    db: Database, bot_config: TelegramConfig
) -> None:
    batch_id = db.upsert_message(chat_id=12345, article_url="https://example.com/ai-chips")
    # Backdate batch to 20 seconds ago
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE batches SET last_updated_at = datetime('now', '-20 seconds') WHERE id = ?",
            (batch_id,),
        )

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    pipeline_runner = AsyncMock(return_value="https://youtube.com/shorts/sample123")

    dispatcher = DebounceDispatcher(
        bot=mock_bot,
        database=db,
        config=bot_config,
        pipeline_runner=pipeline_runner,
        poll_interval=0.01,
    )

    claimed = await dispatcher.process_expired_batches()
    assert claimed == [batch_id]

    pipeline_runner.assert_called_once()
    assert pipeline_runner.call_args[0][0].id == batch_id

    batch = db.get_batch(batch_id)
    assert batch is not None
    assert batch.status == "completed"
    assert batch.youtube_url == "https://youtube.com/shorts/sample123"

    # Status updates sent to chat
    assert mock_bot.send_message.call_count == 2
    msg_start = mock_bot.send_message.call_args_list[0][1]["text"]
    msg_end = mock_bot.send_message.call_args_list[1][1]["text"]
    assert "Starting" in msg_start or "Processing" in msg_start
    assert "https://youtube.com/shorts/sample123" in msg_end


@pytest.mark.asyncio
async def test_dispatcher_processes_expired_batch_failure(
    db: Database, bot_config: TelegramConfig
) -> None:
    batch_id = db.upsert_message(chat_id=12345, article_url="https://example.com/error-article")
    # Backdate batch to 20 seconds ago
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE batches SET last_updated_at = datetime('now', '-20 seconds') WHERE id = ?",
            (batch_id,),
        )

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    pipeline_runner = AsyncMock(side_effect=RuntimeError("Ollama model crashed"))

    dispatcher = DebounceDispatcher(
        bot=mock_bot,
        database=db,
        config=bot_config,
        pipeline_runner=pipeline_runner,
        poll_interval=0.01,
    )

    claimed = await dispatcher.process_expired_batches()
    assert claimed == [batch_id]

    batch = db.get_batch(batch_id)
    assert batch is not None
    assert batch.status == "failed"
    assert "Ollama model crashed" in (batch.error_message or "")

    # Check alert was sent to chat
    assert mock_bot.send_message.call_count == 2
    failure_msg = mock_bot.send_message.call_args_list[1][1]["text"]
    assert "failed" in failure_msg.lower()
    assert "Ollama model crashed" in failure_msg


@pytest.mark.asyncio
async def test_dispatcher_skips_unexpired_batch(db: Database, bot_config: TelegramConfig) -> None:
    batch_id = db.upsert_message(chat_id=12345, article_url="https://example.com/fresh")

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()
    pipeline_runner = AsyncMock()

    dispatcher = DebounceDispatcher(
        bot=mock_bot,
        database=db,
        config=bot_config,
        pipeline_runner=pipeline_runner,
        poll_interval=0.01,
    )

    claimed = await dispatcher.process_expired_batches()
    assert claimed == []
    pipeline_runner.assert_not_called()

    batch = db.get_batch(batch_id)
    assert batch is not None
    assert batch.status == "pending"


@pytest.mark.asyncio
async def test_dispatcher_run_loop_cancellation(db: Database, bot_config: TelegramConfig) -> None:
    mock_bot = MagicMock()
    pipeline_runner = AsyncMock()

    dispatcher = DebounceDispatcher(
        bot=mock_bot,
        database=db,
        config=bot_config,
        pipeline_runner=pipeline_runner,
        poll_interval=0.01,
    )

    stop_event = asyncio.Event()
    loop_task = asyncio.create_task(dispatcher.run_loop(stop_event=stop_event))

    await asyncio.sleep(0.03)
    stop_event.set()
    await loop_task
    assert loop_task.done()


@pytest.mark.asyncio
async def test_dispatcher_default_pipeline_runner_fallback(
    db: Database, bot_config: TelegramConfig
) -> None:
    mock_bot = MagicMock()
    dispatcher = DebounceDispatcher(
        bot=mock_bot,
        database=db,
        config=bot_config,
    )
    fake_batch = Batch(id=99, chat_id=12345)
    res = await dispatcher.pipeline_runner(fake_batch)
    assert res is None


def test_create_dispatcher_registers_router(
    db: Database, bot_config: TelegramConfig, tmp_path: Path
) -> None:
    service = TelegramBotService(config=bot_config, database=db, staging_dir=tmp_path / "staging")
    dp = create_dispatcher(service)
    assert dp is not None
    assert len(dp.sub_routers) == 1


@pytest.mark.asyncio
async def test_run_bot_missing_token_raises() -> None:
    bad_config = AppConfig(telegram=TelegramConfig(bot_token=""))
    with pytest.raises(ValueError, match="TELEGRAM__BOT_TOKEN is not configured"):
        await run_bot(bad_config)


@pytest.mark.asyncio
async def test_service_bounded_cache(
    db: Database, bot_config: TelegramConfig, tmp_path: Path
) -> None:
    service = TelegramBotService(config=bot_config, database=db, staging_dir=tmp_path / "staging")
    service.MAX_ACK_CACHE = 10  # Set small limit for testing

    mock_bot = MagicMock()
    for i in range(25):
        msg = AsyncMock()
        msg.chat.id = 12345
        msg.text = f"https://example.com/article-{i}"
        msg.caption = None
        msg.photo = None
        msg.video = None
        msg.media_group_id = f"group_{i}"
        await service.handle_message(msg, mock_bot)

    assert len(service._acknowledged_groups) <= 10
    assert len(service._acknowledged_batches) <= 10
