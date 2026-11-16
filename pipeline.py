from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Self

from concat_client import ConcatClient
from config import AppConfig, load_config
from database import Batch, BatchMedia, Database
from scraper import ScrapedContent, scrape_article
from tts import synthesize_speech
from writer import ScriptPayload, generate_script
from youtube_uploader import YouTubeUploadResult, upload_video

logger = logging.getLogger(__name__)

MOCK_MP4_HEADER = b"\x00\x00\x00 ftypisom\x00\x00\x02\x00isomiso2mp41"
MINIMAL_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\rIDATx\x9cc`\x00\x00"
    b"\x00\x02\x00\x01H\xaf\xa4q\x00\x00\x00\x00IEND\xaeB`\x82"
)


MINIMAL_WAV_HEADER = (
    b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
    b"\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
)


class PipelineError(Exception):
    """Base exception for pipeline execution errors."""


class PipelineResult(str):
    """Result of an end-to-end pipeline run, behaving as a str URL while retaining metadata."""

    batch_id: int | str
    status: str
    youtube_url: str | None
    video_path: Path | None
    duration_seconds: float
    script: ScriptPayload | None
    temp_dir: Path | None
    stage_timings: dict[str, float]
    error_message: str | None

    def __new__(
        cls,
        youtube_url: str | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> Self:
        value = youtube_url or ""
        return super().__new__(cls, value)

    def __init__(
        self,
        youtube_url: str | None = None,
        *,
        batch_id: int | str = 0,
        status: str = "completed",
        video_path: Path | None = None,
        duration_seconds: float = 0.0,
        script: ScriptPayload | None = None,
        temp_dir: Path | None = None,
        stage_timings: dict[str, float] | None = None,
        error_message: str | None = None,
    ) -> None:
        self.youtube_url = youtube_url
        self.batch_id = batch_id
        self.status = status
        self.video_path = video_path
        self.duration_seconds = duration_seconds
        self.script = script
        self.temp_dir = temp_dir
        self.stage_timings = stage_timings or {}
        self.error_message = error_message


def init_workspace(base_dir: str | Path | None = None) -> dict[str, Path]:
    """Ensure standard workspace folders exist and return their paths."""
    root = Path(base_dir).resolve() if base_dir else Path.cwd().resolve()
    dirs = {
        "output": root / "output",
        "temp": root / "temp",
        "credentials": root / "credentials",
        "models": root / "models",
    }
    for folder in dirs.values():
        folder.mkdir(parents=True, exist_ok=True)
    return dirs


def _resolve_batch_and_url(
    batch: int | Batch | None,
    article_url: str | None,
    database: Database | None,
) -> tuple[int | str, str | None, list[BatchMedia]]:
    """Resolve batch identifier, article URL, and attached media items."""
    if isinstance(batch, Batch):
        return batch.id, (batch.article_url or article_url), batch.media

    if isinstance(batch, int):
        batch_id = batch
        media: list[BatchMedia] = []
        resolved_url = article_url
        if database is not None:
            batch_record = database.get_batch(batch_id)
            if batch_record is not None:
                resolved_url = batch_record.article_url or resolved_url
                media = batch_record.media
        return batch_id, resolved_url, media

    # Virtual batch run (e.g. standalone CLI)
    timestamp_id = int(time.time())
    return timestamp_id, article_url, []


def _collect_visual_assets(
    batch_media: list[BatchMedia],
    scraped_content: ScrapedContent | None,
    fallback_dir: Path | None = None,
) -> tuple[list[str], list[str]]:
    """Separate and collect valid photos and videos for timeline composition."""
    user_photos = [
        m.file_path for m in batch_media if m.file_type == "photo" and Path(m.file_path).is_file()
    ]
    user_videos = [
        m.file_path for m in batch_media if m.file_type == "video" and Path(m.file_path).is_file()
    ]

    scraped_images: list[str] = []
    if scraped_content and scraped_content.image_paths:
        scraped_images = [p for p in scraped_content.image_paths if Path(p).is_file()]

    image_pool = user_photos + scraped_images
    if not image_pool and not user_videos and fallback_dir is not None:
        placeholder = fallback_dir / "placeholder.png"
        placeholder.parent.mkdir(parents=True, exist_ok=True)
        if not placeholder.is_file():
            placeholder.write_bytes(MINIMAL_PNG_BYTES)
        image_pool.append(str(placeholder))

    return image_pool, user_videos


def _execute_stage_1_scrape(
    article_url: str | None,
    temp_dir: Path,
    config: AppConfig,
) -> ScrapedContent:
    """Stage 1: Extract article text, title, and media assets."""
    if not article_url:
        raise PipelineError("Stage 1 failed: No article URL provided.")
    scrape_dir = temp_dir / "scraped"
    scrape_dir.mkdir(parents=True, exist_ok=True)
    try:
        return scrape_article(
            url=article_url,
            output_dir=scrape_dir,
            config=config.scraper,
        )
    except Exception as exc:
        raise PipelineError(f"Stage 1 Ingestion & Scraping failed: {exc}") from exc


def _execute_stage_2_script(
    scraped: ScrapedContent,
    config: AppConfig,
) -> ScriptPayload:
    """Stage 2: Script generation via Ollama LLM."""
    try:
        return generate_script(
            article_text=scraped,
            title=scraped.title,
            config=config,
        )
    except Exception as exc:
        raise PipelineError(f"Stage 2 Script Generation failed: {exc}") from exc


def _execute_stage_3_tts(
    script: ScriptPayload,
    temp_dir: Path,
    batch_id: int | str,
    config: AppConfig,
) -> tuple[Path, float]:
    """Stage 3: Voice synthesis via local Kokoro TTS."""
    audio_path = temp_dir / f"narration_{batch_id}.wav"
    try:
        duration_t = synthesize_speech(
            text=script.script,
            output_path=audio_path,
            config=config,
            fallback_mock=True,
        )
        return audio_path, duration_t
    except Exception as exc:
        raise PipelineError(f"Stage 3 Voice Synthesis failed: {exc}") from exc


def _execute_stage_4_concat(
    audio_path: Path,
    audio_duration: float,
    image_paths: list[str],
    video_paths: list[str],
    output_path: Path,
    config: AppConfig,
    dry_run: bool,
) -> Path:
    """Stage 4: Assemble Concat visual timeline and export vertical MP4."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not audio_path.is_file():
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(MINIMAL_WAV_HEADER)
    try:
        concat_cfg = config.concat
        cfg_path = getattr(concat_cfg, "config_path", None)
        timeout_sec = getattr(concat_cfg, "export_timeout_seconds", 180)
        res = getattr(concat_cfg, "resolution", [1080, 1920])
        res_tuple = (res[0], res[1]) if len(res) >= 2 else (1080, 1920)
        fps = getattr(concat_cfg, "fps", 30)
        bg = getattr(concat_cfg, "background", "blur")

        client = ConcatClient(
            config_path=cfg_path,
            timeout=timeout_sec,
            mock=dry_run,
        )
        client.connect()

        # Build timeline & captions
        client.create_shorts_project(
            audio_path=audio_path,
            image_paths=image_paths,
            video_paths=video_paths if video_paths else None,
            project_name=output_path.stem,
            output_resolution=res_tuple,
            fps=fps,
            timeline_background=bg,
            audio_duration=audio_duration,
        )

        client.project_save()
        export_id = client.export_start(output_path=output_path)
        client.export_wait(
            export_id=export_id,
            timeout=timeout_sec,
        )

        if dry_run and not output_path.is_file():
            output_path.write_bytes(MOCK_MP4_HEADER)

        return output_path
    except Exception as exc:
        raise PipelineError(f"Stage 4 Concat Desktop Assembly failed: {exc}") from exc


def _execute_stage_5_youtube(
    video_path: Path,
    script: ScriptPayload,
    config: AppConfig,
    dry_run: bool,
) -> YouTubeUploadResult:
    """Stage 5: YouTube Shorts publishing."""
    try:
        return upload_video(
            video_path=video_path,
            title=script.yt_title,
            description=script.yt_desc,
            config=config.youtube,
            dry_run=dry_run,
        )
    except Exception as exc:
        raise PipelineError(f"Stage 5 YouTube Publishing failed: {exc}") from exc


def _execute_stage_6_finalize(
    batch_id: int | str,
    youtube_url: str | None,
    database: Database | None,
    temp_dir: Path,
    keep_temp: bool,
) -> None:
    """Stage 6: Status update and temp cleanup."""
    if database is not None and isinstance(batch_id, int):
        database.mark_completed(batch_id, youtube_url=youtube_url)

    if not keep_temp and temp_dir.exists():
        shutil.rmtree(temp_dir, ignore_errors=True)


async def run_pipeline(
    batch: int | Batch | None = None,
    article_url: str | None = None,
    config: AppConfig | None = None,
    dry_run: bool = False,
    keep_temp: bool = False,
    database: Database | None = None,
    base_dir: str | Path | None = None,
) -> PipelineResult | None:
    """
    Execute all 6 stages of the IT News Factory pipeline.

    Returns PipelineResult containing published YouTube URL and execution metrics.
    """
    start_total = time.time()
    cfg = config or load_config()
    dirs = init_workspace(base_dir=base_dir)

    batch_id, resolved_url, batch_media = _resolve_batch_and_url(batch, article_url, database)
    if not resolved_url:
        logger.warning("No article URL provided for batch %s; skipping pipeline", batch_id)
        return None

    temp_dir = dirs["temp"] / f"batch_{batch_id}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    output_path = dirs["output"] / f"{batch_id}.mp4"

    timings: dict[str, float] = {}

    try:
        # Stage 1: Ingestion & Scraping
        t0 = time.time()
        scraped = _execute_stage_1_scrape(resolved_url, temp_dir, cfg)
        timings["stage_1_scrape"] = round(time.time() - t0, 3)

        image_pool, user_videos = _collect_visual_assets(
            batch_media, scraped, fallback_dir=temp_dir
        )

        # Stage 2: Script Generation
        t0 = time.time()
        script = _execute_stage_2_script(scraped, cfg)
        timings["stage_2_script"] = round(time.time() - t0, 3)

        # Stage 3: Voice Synthesis
        t0 = time.time()
        audio_path, duration_t = _execute_stage_3_tts(script, temp_dir, batch_id, cfg)
        timings["stage_3_tts"] = round(time.time() - t0, 3)

        # Stage 4: Concat Desktop Assembly & Export
        t0 = time.time()
        video_path = _execute_stage_4_concat(
            audio_path=audio_path,
            audio_duration=duration_t,
            image_paths=image_pool,
            video_paths=user_videos,
            output_path=output_path,
            config=cfg,
            dry_run=dry_run,
        )
        timings["stage_4_concat"] = round(time.time() - t0, 3)

        # Stage 5: YouTube Publishing
        t0 = time.time()
        yt_result = _execute_stage_5_youtube(video_path, script, cfg, dry_run=dry_run)
        timings["stage_5_youtube"] = round(time.time() - t0, 3)

        # Stage 6: Status Update & Temp Cleanup
        _execute_stage_6_finalize(batch_id, yt_result.video_url, database, temp_dir, keep_temp)

        timings["total"] = round(time.time() - start_total, 3)

        return PipelineResult(
            youtube_url=yt_result.video_url,
            batch_id=batch_id,
            status="completed",
            video_path=video_path,
            duration_seconds=duration_t,
            script=script,
            temp_dir=temp_dir,
            stage_timings=timings,
        )

    except Exception as exc:
        if database is not None and isinstance(batch_id, int):
            database.mark_failed(batch_id, error_message=str(exc))
        if not keep_temp and temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        logger.exception("Pipeline execution failed for batch %s", batch_id)
        raise PipelineError(str(exc)) from exc


def process_batch(
    batch: int | Batch,
    config: AppConfig | None = None,
    dry_run: bool = False,
    keep_temp: bool = False,
    database: Database | None = None,
    base_dir: str | Path | None = None,
) -> PipelineResult | None:
    """Synchronous entry point wrapping run_pipeline for task runners and background jobs."""
    return asyncio.run(
        run_pipeline(
            batch=batch,
            config=config,
            dry_run=dry_run,
            keep_temp=keep_temp,
            database=database,
            base_dir=base_dir,
        )
    )


def main(argv: list[str] | None = None) -> int:
    """CLI runner entry point."""
    parser = argparse.ArgumentParser(
        description="IT News Factory End-to-End Pipeline & CLI Runner."
    )
    parser.add_argument(
        "--test-url",
        type=str,
        required=True,
        help="Article URL to process through the pipeline.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run in mock/dry-run mode without live Concat GUI or YouTube quota.",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to custom config YAML file.",
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Retain intermediate temporary files and directories.",
    )
    parser.add_argument(
        "--base-dir",
        type=str,
        default=None,
        help="Base directory for output, temp, and models folders.",
    )

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        cfg = load_config(args.config) if args.config else load_config()
        print(f"🚀 Starting pipeline run for URL: {args.test_url} (dry_run={args.dry_run})")

        result = asyncio.run(
            run_pipeline(
                article_url=args.test_url,
                config=cfg,
                dry_run=args.dry_run,
                keep_temp=args.keep_temp,
                base_dir=args.base_dir,
            )
        )
        if result is None:
            raise PipelineError(f"No valid article URL provided in '{args.test_url}'.")

        print("\n✅ Pipeline completed successfully!")
        print(f"🎬 Video: {result.video_path}")
        print(f"⏱️ Duration: {result.duration_seconds:.2f}s")
        print(f"🔗 YouTube Shorts: {result.youtube_url}")
        print("\n⏱️ Stage execution timings:")
        for stage, duration in result.stage_timings.items():
            print(f"  - {stage}: {duration:.3f}s")

        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"\n❌ Pipeline failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
