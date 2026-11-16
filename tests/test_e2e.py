from __future__ import annotations

import socket
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from bot import DebounceDispatcher, TelegramBotService
from concat_client import MockConcatServer
from config import (
    AppConfig,
    ConcatConfig,
    OllamaConfig,
    ScraperConfig,
    TelegramConfig,
    TTSConfig,
    YouTubeConfig,
)
from database import Database
from pipeline import run_pipeline
from scraper import ScrapedContent
from youtube_uploader import YouTubeQuotaExceededError, YouTubeUploadResult


def create_test_config(
    workspace_dirs: dict[str, Path],
    concat_config_path: Path,
) -> AppConfig:
    """Helper to assemble a clean test AppConfig pointing to isolated test directories."""
    return AppConfig(
        telegram=TelegramConfig(
            bot_token="test_bot_token",
            allowed_chat_ids=[12345],
            debounce_seconds=15,
            database_path=str(workspace_dirs["root"] / "queue.db"),
        ),
        scraper=ScraperConfig(
            user_agent="TestUserAgent/1.0",
            screenshot_timeout_ms=5000,
        ),
        ollama=OllamaConfig(
            host="http://127.0.0.1:11434",
            model="qwen2.5-coder:14b",
            temperature=0.7,
        ),
        tts=TTSConfig(
            model_path=str(workspace_dirs["models"] / "kokoro.onnx"),
            voices_path=str(workspace_dirs["models"] / "voices.bin"),
            voice="am_adam",
            speed=1.08,
        ),
        concat=ConcatConfig(
            config_path=str(concat_config_path),
            resolution=[1080, 1920],
            fps=30,
            background="blur",
            export_timeout_seconds=30,
        ),
        youtube=YouTubeConfig(
            client_secrets_file=str(workspace_dirs["credentials"] / "client_secret.json"),
            token_file=str(workspace_dirs["credentials"] / "token.json"),
            privacy_status="public",
            category_id="28",
            default_tags=["Shorts", "TechNews", "AI"],
        ),
    )


@pytest.mark.asyncio
async def test_full_e2e_pipeline_telegram_to_youtube(
    e2e_workspace: dict[str, Any],
    mock_concat_server: tuple[MockConcatServer, Path],
    mock_telegram_bot: AsyncMock,
    telegram_message_factory: Any,
    mock_scraped_content_factory: Any,
    mock_ollama_client: Any,
    mock_youtube_upload_result: YouTubeUploadResult,
) -> None:
    """
    Full end-to-end integration test:
    1. User drops article link + photo into Telegram bot.
    2. User drops video in the same media group.
    3. 15-second debounce groups them into a single batch.
    4. Debounce dispatcher claims batch and runs pipeline.
    5. Stages 1-6 execute:
       - Scraper extracts text and images
       - Ollama generates structured script
       - Kokoro synthesizes 24kHz audio and measures duration T
       - Concat builds Ken Burns timeline and exports MP4
       - YouTube uploader publishes the video
       - Status notifications sent back to Telegram chat
    6. Batch marked completed in SQLite.
    """
    server, concat_cfg_path = mock_concat_server
    dirs = e2e_workspace["dirs"]
    db: Database = e2e_workspace["db"]
    cfg = create_test_config(dirs, concat_cfg_path)

    chat_id = 12345
    article_url = "https://techcrunch.com/2026/01/breakthrough-ai-cluster"
    scraped_data = mock_scraped_content_factory(
        url=article_url,
        title="Next-Gen AI Cluster Breakthrough",
        num_images=2,
    )

    # Initialize bot service
    bot_service = TelegramBotService(
        config=cfg.telegram,
        database=db,
        staging_dir=dirs["staging"],
    )

    # 1. First Telegram message: link + photo in a media group
    msg1 = telegram_message_factory(
        chat_id=chat_id,
        caption=f"Check out this breaking story: {article_url}",
        photo_ids=["photo_og_101"],
        media_group_id="group_breakthrough_2026",
        message_id=101,
    )
    batch_id_1 = await bot_service.handle_message(msg1, mock_telegram_bot)
    assert batch_id_1 is not None

    # 2. Second Telegram message: video clip in the same media group
    msg2 = telegram_message_factory(
        chat_id=chat_id,
        video_id="video_clip_202",
        video_name="datacenter_broll.mp4",
        media_group_id="group_breakthrough_2026",
        message_id=102,
    )
    batch_id_2 = await bot_service.handle_message(msg2, mock_telegram_bot)
    assert batch_id_2 == batch_id_1

    # Verify SQLite aggregation
    batch_record = db.get_batch(batch_id_1)
    assert batch_record is not None
    assert batch_record.status == "pending"
    assert batch_record.article_url == article_url
    assert len(batch_record.media) == 2

    # Verify debounce: immediately, batch is not expired (debounce active)
    expired_immediate = db.get_expired_batches(debounce_seconds=15)
    assert len(expired_immediate) == 0

    # 3. Simulate passage of 15 seconds by backdating last_updated_at in SQLite
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE batches SET last_updated_at = datetime('now', '-20 seconds') WHERE id = ?",
            (batch_id_1,),
        )

    # 4. Debounce dispatcher activates
    dispatcher = DebounceDispatcher(
        bot=mock_telegram_bot,
        database=db,
        config=cfg.telegram,
        pipeline_runner=lambda b: run_pipeline(
            batch=b,
            config=cfg,
            database=db,
            base_dir=dirs["root"],
            keep_temp=True,  # preserve to verify intermediate outputs
        ),
        poll_interval=0.01,
    )

    with (
        patch("pipeline.scrape_article", return_value=scraped_data) as mock_scrape,
        patch("writer._get_sync_client", return_value=mock_ollama_client),
        patch("timeline.probe_video_duration", return_value=5.0),
        patch("pipeline.upload_video", return_value=mock_youtube_upload_result) as mock_yt,
    ):
        claimed_ids = await dispatcher.process_expired_batches()

    # 5. Assertions on pipeline completion and status
    assert claimed_ids == [batch_id_1]
    mock_scrape.assert_called_once_with(
        url=article_url,
        output_dir=dirs["temp"] / f"batch_{batch_id_1}" / "scraped",
        config=cfg.scraper,
    )
    mock_yt.assert_called_once()

    # Verify database updated to completed
    final_batch = db.get_batch(batch_id_1)
    assert final_batch is not None
    assert final_batch.status == "completed"
    assert final_batch.youtube_url == mock_youtube_upload_result.video_url

    # Verify rendered video file exists in output directory
    rendered_video = dirs["output"] / f"{batch_id_1}.mp4"
    assert rendered_video.is_file()
    assert rendered_video.stat().st_size > 0

    # Verify Concat Desktop JSON-RPC server received all timeline & render methods
    methods_called = [req["body"]["method"] for req in server._requests]
    assert "project_create" in methods_called
    assert "project_set_video" in methods_called
    assert "media_import" in methods_called
    assert "clip_add" in methods_called
    assert "captions_add" in methods_called
    assert "keyframe_set" in methods_called
    assert "project_save" in methods_called
    assert "export_start" in methods_called
    assert "export_status" in methods_called

    # Verify Telegram status messages were sent
    # 1. Debounce complete / starting
    # 2. Published successfully with link
    sent_texts = [call[1]["text"] for call in mock_telegram_bot.send_message.call_args_list]
    assert any("Debounce complete" in t or "Starting" in t for t in sent_texts)
    assert any(mock_youtube_upload_result.video_url in t for t in sent_texts)


@pytest.mark.asyncio
async def test_e2e_ollama_transient_failure_and_retry(
    e2e_workspace: dict[str, Any],
    mock_concat_server: tuple[MockConcatServer, Path],
    mock_scraped_content_factory: Any,
    mock_ollama_client_factory: Any,
    mock_youtube_upload_result: YouTubeUploadResult,
) -> None:
    """
    Resilience test: Ollama fails on first call (simulating network timeout or model loading),
    then succeeds on second attempt. Verifies pipeline completes without failure.
    """
    _, concat_cfg_path = mock_concat_server
    dirs = e2e_workspace["dirs"]
    db: Database = e2e_workspace["db"]
    cfg = create_test_config(dirs, concat_cfg_path)

    batch_id = db.upsert_message(
        chat_id=12345,
        article_url="https://example.com/ai-robotics",
    )
    db.claim_batch(batch_id)

    scraped = mock_scraped_content_factory(
        url="https://example.com/ai-robotics",
        title="Robotics Milestone Reached",
        num_images=1,
    )

    # Mock Ollama client: fails 1 time with ConnectionError, then returns valid structured JSON
    retry_client = mock_ollama_client_factory(fail_count=1)

    with (
        patch("pipeline.scrape_article", return_value=scraped),
        patch("writer._get_sync_client", return_value=retry_client),
        patch("pipeline.upload_video", return_value=mock_youtube_upload_result),
    ):
        result = await run_pipeline(
            batch=batch_id,
            config=cfg,
            database=db,
            base_dir=dirs["root"],
        )

    assert result is not None
    assert result.status == "completed"
    assert retry_client.call_count == 2  # Proves retry occurred!
    assert db.get_batch(batch_id).status == "completed"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_e2e_missing_visual_assets_fallback(
    e2e_workspace: dict[str, Any],
    mock_concat_server: tuple[MockConcatServer, Path],
    mock_ollama_client: Any,
    mock_youtube_upload_result: YouTubeUploadResult,
) -> None:
    """
    Resilience test: User dropped no images/videos, and scraper returned zero images.
    Pipeline must activate fallback visual asset creation (placeholder image) and
    successfully complete Concat timeline assembly and publishing.
    """
    server, concat_cfg_path = mock_concat_server
    dirs = e2e_workspace["dirs"]
    db: Database = e2e_workspace["db"]
    cfg = create_test_config(dirs, concat_cfg_path)

    # Batch with no media attachments
    batch_id = db.upsert_message(
        chat_id=12345,
        article_url="https://example.com/text-only-article",
    )
    db.claim_batch(batch_id)

    # Scraper returns no images
    zero_media_scraped = ScrapedContent(
        url="https://example.com/text-only-article",
        title="Zero Media Tech News",
        text="A detailed technical report with zero photographs or illustrations.",
        image_paths=[],
    )

    with (
        patch("pipeline.scrape_article", return_value=zero_media_scraped),
        patch("writer._get_sync_client", return_value=mock_ollama_client),
        patch("pipeline.upload_video", return_value=mock_youtube_upload_result),
    ):
        result = await run_pipeline(
            batch=batch_id,
            config=cfg,
            database=db,
            base_dir=dirs["root"],
        )

    assert result is not None
    assert result.status == "completed"

    # Verify placeholder fallback image was used
    # Placeholder was imported
    import_calls = [
        req["body"]["params"]["file_path"]
        for req in server._requests
        if req["body"]["method"] == "media_import"
    ]
    assert any("placeholder.png" in p for p in import_calls)


@pytest.mark.asyncio
async def test_e2e_youtube_quota_exhaustion_recovery(
    e2e_workspace: dict[str, Any],
    mock_concat_server: tuple[MockConcatServer, Path],
    mock_telegram_bot: AsyncMock,
    mock_scraped_content_factory: Any,
    mock_ollama_client: Any,
) -> None:
    """
    Resilience test: YouTube Data API v3 returns 403 quotaExceeded.
    - Pipeline fails cleanly.
    - Database records batch as 'failed' with quota error message.
    - INVARIANT: The locally rendered video file (output/<batch_id>.mp4) is PRESERVED on disk!
    - Telegram chat receives error notification mentioning quota.
    """
    _, concat_cfg_path = mock_concat_server
    dirs = e2e_workspace["dirs"]
    db: Database = e2e_workspace["db"]
    cfg = create_test_config(dirs, concat_cfg_path)

    batch_id = db.upsert_message(
        chat_id=12345,
        article_url="https://example.com/quota-test-story",
    )

    # Backdate batch
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE batches SET last_updated_at = datetime('now', '-20 seconds') WHERE id = ?",
            (batch_id,),
        )

    scraped = mock_scraped_content_factory(
        url="https://example.com/quota-test-story",
        title="High Volume Tech Day",
        num_images=1,
    )

    dispatcher = DebounceDispatcher(
        bot=mock_telegram_bot,
        database=db,
        config=cfg.telegram,
        pipeline_runner=lambda b: run_pipeline(
            batch=b,
            config=cfg,
            database=db,
            base_dir=dirs["root"],
        ),
        poll_interval=0.01,
    )

    quota_error = YouTubeQuotaExceededError("YouTube Data API quota exceeded for today.")

    with (
        patch("pipeline.scrape_article", return_value=scraped),
        patch("writer._get_sync_client", return_value=mock_ollama_client),
        patch("pipeline.upload_video", side_effect=quota_error),
    ):
        claimed_ids = await dispatcher.process_expired_batches()

    assert claimed_ids == [batch_id]

    # Verify batch marked failed in database
    batch_record = db.get_batch(batch_id)
    assert batch_record is not None
    assert batch_record.status == "failed"
    assert "quota exceeded" in (batch_record.error_message or "").lower()

    # INVARIANT CHECK: The rendered output MP4 MUST exist and NOT be deleted!
    rendered_mp4 = dirs["output"] / f"{batch_id}.mp4"
    assert rendered_mp4.is_file(), "Rendered MP4 must be preserved when YouTube quota is exceeded!"
    assert rendered_mp4.stat().st_size > 0

    # Verify notification sent to Telegram informing chat of failure
    sent_texts = [call[1]["text"] for call in mock_telegram_bot.send_message.call_args_list]
    assert any("failed" in t.lower() and "quota" in t.lower() for t in sent_texts)


@pytest.mark.asyncio
async def test_e2e_scraper_failure_resilience(
    e2e_workspace: dict[str, Any],
    mock_concat_server: tuple[MockConcatServer, Path],
    mock_telegram_bot: AsyncMock,
) -> None:
    """
    Resilience test: Article scraper encounters unrecoverable connection timeout.
    Pipeline raises PipelineError, database is marked failed, and Telegram is alerted.
    """
    _, concat_cfg_path = mock_concat_server
    dirs = e2e_workspace["dirs"]
    db: Database = e2e_workspace["db"]
    cfg = create_test_config(dirs, concat_cfg_path)

    batch_id = db.upsert_message(
        chat_id=12345,
        article_url="https://unreachable.invalid/news",
    )
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE batches SET last_updated_at = datetime('now', '-20 seconds') WHERE id = ?",
            (batch_id,),
        )

    dispatcher = DebounceDispatcher(
        bot=mock_telegram_bot,
        database=db,
        config=cfg.telegram,
        pipeline_runner=lambda b: run_pipeline(
            batch=b,
            config=cfg,
            database=db,
            base_dir=dirs["root"],
        ),
        poll_interval=0.01,
    )

    with patch("pipeline.scrape_article", side_effect=RuntimeError("DNS resolution failed")):
        await dispatcher.process_expired_batches()

    batch_record = db.get_batch(batch_id)
    assert batch_record is not None
    assert batch_record.status == "failed"
    assert "DNS resolution failed" in (batch_record.error_message or "")

    sent_texts = [call[1]["text"] for call in mock_telegram_bot.send_message.call_args_list]
    assert any("failed" in t.lower() for t in sent_texts)


@pytest.mark.asyncio
async def test_e2e_guaranteed_offline_execution(
    e2e_workspace: dict[str, Any],
    mock_concat_server: tuple[MockConcatServer, Path],
    mock_scraped_content_factory: Any,
    mock_ollama_client: Any,
    mock_youtube_upload_result: YouTubeUploadResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Offline assurance test:
    Verify that the entire pipeline functions completely offline with zero external network access.
    Any attempt to connect outside localhost (127.0.0.1 / ::1) is blocked.
    """
    orig_connect = socket.socket.connect

    def guarded_connect(self: socket.socket, address: Any) -> None:
        host = address[0] if isinstance(address, tuple) and address else address
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ConnectionRefusedError(
                f"External network connection blocked during offline test: {address}"
            )
        orig_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)

    _, concat_cfg_path = mock_concat_server
    dirs = e2e_workspace["dirs"]
    db: Database = e2e_workspace["db"]
    cfg = create_test_config(dirs, concat_cfg_path)

    batch_id = db.upsert_message(
        chat_id=12345,
        article_url="https://example.com/offline-safe-news",
    )
    db.claim_batch(batch_id)

    scraped = mock_scraped_content_factory(
        url="https://example.com/offline-safe-news",
        title="Offline News Title",
        num_images=1,
    )

    with (
        patch("pipeline.scrape_article", return_value=scraped),
        patch("writer._get_sync_client", return_value=mock_ollama_client),
        patch("pipeline.upload_video", return_value=mock_youtube_upload_result),
    ):
        result = await run_pipeline(
            batch=batch_id,
            config=cfg,
            database=db,
            base_dir=dirs["root"],
        )

    assert result is not None
    assert result.status == "completed"
    assert result.video_path is not None
    assert result.video_path.is_file()
