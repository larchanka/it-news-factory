from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from database import Database
from scraper import ScrapedContent
from writer import ScriptPayload
from youtube_uploader import YouTubeUploadResult


def test_init_workspace(tmp_path: Path) -> None:
    from pipeline import init_workspace

    dirs = init_workspace(base_dir=tmp_path)
    assert dirs["output"].is_dir()
    assert dirs["temp"].is_dir()
    assert dirs["credentials"].is_dir()
    assert dirs["models"].is_dir()


def test_pipeline_result_str_and_attributes() -> None:
    from pipeline import PipelineResult

    res = PipelineResult(
        youtube_url="https://youtube.com/shorts/xyz123",
        batch_id=42,
        status="completed",
        video_path=Path("output/42.mp4"),
        duration_seconds=35.5,
        stage_timings={"stage_1_scrape": 0.5, "total": 1.2},
    )
    assert isinstance(res, str)
    assert str(res) == "https://youtube.com/shorts/xyz123"
    assert res.youtube_url == "https://youtube.com/shorts/xyz123"
    assert res.batch_id == 42
    assert res.status == "completed"
    assert res.video_path == Path("output/42.mp4")
    assert res.duration_seconds == 35.5
    assert res.stage_timings["stage_1_scrape"] == 0.5


@pytest.fixture
def mock_scraped_content(tmp_path: Path) -> ScrapedContent:
    img = tmp_path / "article_og.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0mock_image_bytes")
    return ScrapedContent(
        url="https://example.com/news/ai-breakthrough",
        title="AI Breakthrough 2026",
        text="A new breakthrough in artificial intelligence was announced today.",
        image_paths=[str(img)],
    )


@pytest.fixture
def mock_script_payload() -> ScriptPayload:
    return ScriptPayload(
        hook="Engineers thought AI was slowing down, but they were wrong.",
        script="Engineers thought AI was slowing down, but they were wrong. Here is the meat of the story.",
        yt_title="AI Breakthrough Announced #Shorts",
        yt_desc="Major breakthrough in AI. #Shorts #TechNews",
    )


@pytest.mark.asyncio
async def test_run_pipeline_happy_path(
    tmp_path: Path,
    mock_scraped_content: ScrapedContent,
    mock_script_payload: ScriptPayload,
) -> None:
    from pipeline import run_pipeline

    db = Database(db_path=tmp_path / "test_queue.db")
    batch_id = db.upsert_message(
        chat_id=12345,
        article_url="https://example.com/news/ai-breakthrough",
    )
    db.claim_batch(batch_id)

    with (
        patch("pipeline.scrape_article", return_value=mock_scraped_content) as mock_scrape,
        patch("pipeline.generate_script", return_value=mock_script_payload) as mock_gen,
        patch("pipeline.synthesize_speech", return_value=32.4) as mock_synth,
        patch("pipeline.ConcatClient") as mock_concat_cls,
        patch("pipeline.upload_video") as mock_upload,
    ):
        mock_concat = MagicMock()
        mock_concat_cls.return_value = mock_concat
        mock_concat.export_start.return_value = "exp_123"
        mock_concat.export_wait.return_value = {"status": "completed"}

        mock_upload.return_value = YouTubeUploadResult(
            video_id="vid_abc123",
            video_url="https://youtube.com/shorts/vid_abc123",
            privacy_status="public",
        )

        result = await run_pipeline(
            batch=batch_id,
            database=db,
            base_dir=tmp_path,
        )

        assert result is not None
        assert result.status == "completed"
        assert result.youtube_url == "https://youtube.com/shorts/vid_abc123"
        assert str(result) == "https://youtube.com/shorts/vid_abc123"
        assert result.duration_seconds == 32.4
        assert "stage_1_scrape" in result.stage_timings
        assert "stage_2_script" in result.stage_timings
        assert "stage_3_tts" in result.stage_timings
        assert "stage_4_concat" in result.stage_timings
        assert "stage_5_youtube" in result.stage_timings
        assert "total" in result.stage_timings

        # Verify DB updated
        batch_record = db.get_batch(batch_id)
        assert batch_record is not None
        assert batch_record.status == "completed"
        assert batch_record.youtube_url == "https://youtube.com/shorts/vid_abc123"

        mock_scrape.assert_called_once()
        mock_gen.assert_called_once()
        mock_synth.assert_called_once()
        mock_concat.create_shorts_project.assert_called_once()
        mock_concat.export_start.assert_called_once()
        mock_concat.export_wait.assert_called_once()
        mock_upload.assert_called_once()


@pytest.mark.asyncio
async def test_run_pipeline_dry_run_standalone(tmp_path: Path) -> None:
    from pipeline import run_pipeline

    with (
        patch(
            "pipeline.scrape_article",
            return_value=ScrapedContent(
                url="https://example.com/item",
                title="Dry Run Item",
                text="Testing dry run behavior standalone.",
                image_paths=[],
            ),
        ),
        patch(
            "pipeline.generate_script",
            return_value=ScriptPayload(
                hook="Dry run hook.",
                script="Dry run voiceover content for testing.",
                yt_title="Dry Run #Shorts",
                yt_desc="Dry run description #Shorts",
            ),
        ),
        patch("pipeline.synthesize_speech", return_value=15.0),
    ):
        result = await run_pipeline(
            article_url="https://example.com/item",
            dry_run=True,
            base_dir=tmp_path,
        )

        assert result is not None
        assert result.status == "completed"
        assert result.youtube_url is not None
        assert "mock" in result.youtube_url or "shorts" in result.youtube_url
        assert result.video_path is not None
        assert result.video_path.is_file()


@pytest.mark.asyncio
async def test_run_pipeline_cleanup_behavior(
    tmp_path: Path,
    mock_scraped_content: ScrapedContent,
    mock_script_payload: ScriptPayload,
) -> None:
    from pipeline import run_pipeline

    # Test 1: keep_temp=False removes batch temp folder
    with (
        patch("pipeline.scrape_article", return_value=mock_scraped_content),
        patch("pipeline.generate_script", return_value=mock_script_payload),
        patch("pipeline.synthesize_speech", return_value=10.0),
    ):
        res1 = await run_pipeline(
            article_url="https://example.com/news1",
            dry_run=True,
            keep_temp=False,
            base_dir=tmp_path,
        )
        assert res1 is not None
        assert res1.temp_dir is not None
        assert not res1.temp_dir.exists()

    # Test 2: keep_temp=True preserves batch temp folder
    with (
        patch("pipeline.scrape_article", return_value=mock_scraped_content),
        patch("pipeline.generate_script", return_value=mock_script_payload),
        patch("pipeline.synthesize_speech", return_value=10.0),
    ):
        res2 = await run_pipeline(
            article_url="https://example.com/news2",
            dry_run=True,
            keep_temp=True,
            base_dir=tmp_path,
        )
        assert res2 is not None
        assert res2.temp_dir is not None
        assert res2.temp_dir.exists()


@pytest.mark.asyncio
async def test_run_pipeline_scraper_failure_marks_database(tmp_path: Path) -> None:
    from pipeline import PipelineError, run_pipeline

    db = Database(db_path=tmp_path / "test_queue.db")
    batch_id = db.upsert_message(chat_id=123, article_url="https://broken.invalid/article")
    db.claim_batch(batch_id)

    with patch("pipeline.scrape_article", side_effect=RuntimeError("Scraper connection timeout")):
        with pytest.raises(PipelineError) as exc_info:
            await run_pipeline(
                batch=batch_id,
                database=db,
                base_dir=tmp_path,
            )

        assert "Scraper connection timeout" in str(exc_info.value)
        batch_record = db.get_batch(batch_id)
        assert batch_record is not None
        assert batch_record.status == "failed"
        assert "Scraper connection timeout" in (batch_record.error_message or "")


@pytest.mark.asyncio
async def test_run_pipeline_concat_export_failure(
    tmp_path: Path,
    mock_scraped_content: ScrapedContent,
    mock_script_payload: ScriptPayload,
) -> None:
    from concat_client import ConcatRPCError
    from pipeline import PipelineError, run_pipeline

    db = Database(db_path=tmp_path / "test_queue.db")
    batch_id = db.upsert_message(chat_id=123, article_url="https://example.com/test")
    db.claim_batch(batch_id)

    with (
        patch("pipeline.scrape_article", return_value=mock_scraped_content),
        patch("pipeline.generate_script", return_value=mock_script_payload),
        patch("pipeline.synthesize_speech", return_value=20.0),
        patch("pipeline.ConcatClient") as mock_concat_cls,
    ):
        mock_concat = MagicMock()
        mock_concat_cls.return_value = mock_concat
        mock_concat.export_wait.side_effect = ConcatRPCError(
            code=-32001, message="GPU NVENC out of memory"
        )

        with pytest.raises(PipelineError) as exc_info:
            await run_pipeline(
                batch=batch_id,
                database=db,
                base_dir=tmp_path,
            )

        assert "GPU NVENC out of memory" in str(exc_info.value)
        batch_record = db.get_batch(batch_id)
        assert batch_record is not None
        assert batch_record.status == "failed"


@pytest.mark.asyncio
async def test_run_pipeline_youtube_failure_preserves_video(
    tmp_path: Path,
    mock_scraped_content: ScrapedContent,
    mock_script_payload: ScriptPayload,
) -> None:
    from pipeline import PipelineError, run_pipeline
    from youtube_uploader import YouTubeQuotaExceededError

    db = Database(db_path=tmp_path / "test_queue.db")
    batch_id = db.upsert_message(chat_id=123, article_url="https://example.com/test")
    db.claim_batch(batch_id)

    with (
        patch("pipeline.scrape_article", return_value=mock_scraped_content),
        patch("pipeline.generate_script", return_value=mock_script_payload),
        patch("pipeline.synthesize_speech", return_value=20.0),
        patch("pipeline.ConcatClient") as mock_concat_cls,
        patch(
            "pipeline.upload_video",
            side_effect=YouTubeQuotaExceededError("Daily quota 403 exceeded"),
        ),
    ):
        mock_concat = MagicMock()
        mock_concat_cls.return_value = mock_concat
        mock_concat.export_wait.return_value = {"status": "completed"}

        with pytest.raises(PipelineError) as exc_info:
            await run_pipeline(
                batch=batch_id,
                database=db,
                base_dir=tmp_path,
            )

        assert "Daily quota 403 exceeded" in str(exc_info.value)
        batch_record = db.get_batch(batch_id)
        assert batch_record is not None
        assert batch_record.status == "failed"


def test_cli_main_dry_run(tmp_path: Path) -> None:
    from pipeline import main

    with (
        patch(
            "pipeline.scrape_article",
            return_value=ScrapedContent(
                url="https://example.com/cli",
                title="CLI Test",
                text="CLI test article text.",
                image_paths=[],
            ),
        ),
        patch(
            "pipeline.generate_script",
            return_value=ScriptPayload(
                hook="CLI Hook",
                script="CLI Script voiceover.",
                yt_title="CLI Title #Shorts",
                yt_desc="CLI Description",
            ),
        ),
        patch("pipeline.synthesize_speech", return_value=12.0),
    ):
        exit_code = main(
            ["--test-url", "https://example.com/cli", "--dry-run", "--base-dir", str(tmp_path)]
        )
        assert exit_code == 0
