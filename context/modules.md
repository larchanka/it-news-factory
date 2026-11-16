# Module Catalog

This document specifies the responsibilities, public interfaces, and data models of all core modules in **IT News Factory**.

---

## 1. `database.py` & `bot.py` — Telegram Listener & SQLite Debouncer

### Overview
`database.py` manages SQLite tables, atomic state transitions (`pending` -> `processing` -> `completed` / `failed`), and debounce expiration queries.
`bot.py` handles Telegram webhook/polling events, persists messages and media attachments via `database.py`, manages a 15-second debounce window across grouped media (`media_group_id`), and dispatches completed batches to `pipeline.py`.

### Key Functions & Classes in `database.py`
- `class Database`:
  - `init_db() -> None`: Prepares SQLite tables (`batches`, `batch_media`) and indexing.
  - `upsert_message(chat_id: int, article_url: str | None = None, media_group_id: str | None = None, file_type: str | None = None, file_path: str | None = None) -> int`: Upserts incoming text/media and resets 15-second debounce timer.
  - `get_expired_batches(debounce_seconds: int = 15) -> list[Batch]`: Retrieves pending batches whose inactivity exceeds debounce seconds.
  - `claim_batch(batch_id: int) -> bool`: Atomically acquires processing lock (`pending` -> `processing`).
  - `mark_completed(batch_id: int, youtube_url: str | None = None) -> bool`: Updates batch status to `completed` and records YouTube URL.
  - `mark_failed(batch_id: int, error_message: str | None = None) -> bool`: Updates batch status to `failed` and records error reason.
  - `get_batch(batch_id: int) -> Batch | None`: Retrieves batch and its media attachments.
  - `get_batch_media(batch_id: int) -> list[BatchMedia]`: Retrieves media files attached to a batch.
- Standalone module-level helpers: `init_db`, `upsert_message`, `record_message`, `get_expired_batches`, `claim_batch`, `mark_completed`, `mark_failed`, `get_batch`, `get_batch_media`.

### Key Functions & Classes in `bot.py`
- `class TelegramBotService`:
  - `__init__(config: TelegramConfig, database: Database, staging_dir: str | Path = "temp") -> None`: Manages message ingestion, media caching, and debounce coordination.
  - `async handle_message(message: Message, bot: Bot) -> int | None`: Ingests messages/media, downloads media attachments to staging directory, records updates in SQLite, and sends a single acknowledgment per batch/group.
  - `MAX_ACK_CACHE: int = 1000`: Bounded FIFO cache threshold for tracking acknowledged groups and batches.
- `class DebounceDispatcher`:
  - `__init__(bot: Bot, database: Database, config: TelegramConfig, pipeline_runner: Callable[[Batch], Awaitable[str | None]] | None = None, poll_interval: float = 1.0) -> None`: Debounce monitor and pipeline dispatcher.
  - `async process_expired_batches() -> list[int]`: Scans expired pending batches, atomically claims them, dispatches to `pipeline_runner`, and sends status notifications.
  - `async run_loop(stop_event: asyncio.Event | None = None) -> None`: Polling loop checking for expired batches every `poll_interval` seconds.
- Standalone module-level helpers:
  - `extract_article_url(text: str | None) -> str | None`: Extracts first HTTP/HTTPS URL from text/caption, stripping trailing sentence punctuation.
  - `is_chat_allowed(chat_id: int, allowed_chat_ids: list[int]) -> bool`: Validates chat authorization against whitelist.
  - `extract_media(message: Message) -> tuple[Any, str | None, str | None]`: Extracts photo/video object, type, and sanitized filename (mitigating path traversal).
  - `create_dispatcher(service: TelegramBotService) -> Dispatcher`: Factory creating and configuring the aiogram `Dispatcher` and `Router`.
  - `async run_bot(config: AppConfig | None = None) -> None`: Application entrypoint running Telegram polling and the background debounce dispatcher.

### SQLite Schema (`queue.db`)
```sql
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    media_group_id TEXT,
    article_url TEXT,
    status TEXT DEFAULT 'pending', -- 'pending', 'processing', 'completed', 'failed'
    youtube_url TEXT,
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS batch_media (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    file_type TEXT NOT NULL, -- 'photo', 'video'
    file_path TEXT NOT NULL
);
```

---

## 2. `scraper.py` — Content & Media Scraper

### Overview
Extracts clean readable article text and OpenGraph images using `trafilatura`. If insufficient media exists, launches a headless Playwright browser to capture full-page or article-level screenshots as visual assets.

### Key Functions
- `scrape_article(url: str) -> ScrapedContent`:
  - Main entry point. Fetches article text and image links.
  - Returns `ScrapedContent` model.
- `extract_og_images(html: str) -> List[str]`:
  - Extracts `og:image`, `twitter:image`, and high-resolution article images.
- `async capture_page_screenshot(url: str, output_path: str) -> str`:
  - Headless Playwright capture when no images or media items are provided by the user.

### Data Model
```python
@dataclass
class ScrapedContent:
    url: str
    title: str
    text: str
    image_paths: List[str]
```

---

## 3. `writer.py` — Local LLM Scriptwriter & Phonetic Sanitizer

### Overview
Communicates with Ollama (`format="json"`, default endpoint `http://192.168.1.124:11434` running `qwen2.5-coder:14b`) to generate an ironic, tech-savvy YouTube Shorts script conforming to word count limits and phonetic speech synthesis constraints.

### Key Functions
- `generate_script(article_text: Any, title: str | None = None, config: AppConfig | None = None, model: str | None = None, client: Any = None, max_retries: int = 3) -> ScriptPayload`:
  - Primary entrypoint. Accepts `ScrapedContent` or raw article text.
  - Prompts Ollama with system prompt (cynical tone, Shorts hook/body/punchline structure, 85–115 word budget).
  - Normalizes JSON response into `ScriptPayload` and applies `sanitize_for_tts`.
  - Retries up to `max_retries` with length feedback if script falls outside bounds.
  - Fallback cleanly trims over-budget scripts if retries are exhausted.
- `async async_generate_script(article_text: Any, title: str | None = None, config: AppConfig | None = None, model: str | None = None, client: Any = None, max_retries: int = 3) -> ScriptPayload`:
  - Asynchronous equivalent using `ollama.AsyncClient`.
- `sanitize_for_tts(text: str) -> str`:
  - Strips markdown formatting (`*`, `#`, `_`, `` ` ``, `~`, `[link](https://example.com)`).
  - Strips emojis and emoticon shrugs.
  - Expands currencies (`$42M` $\rightarrow$ "forty-two million dollars", `€20M` $\rightarrow$ "twenty million euros", `£5B` $\rightarrow$ "five billion pounds").
  - Expands percentages (`50%` $\rightarrow$ "fifty percent", `-5%` $\rightarrow$ "negative five percent").
  - Expands ordinals (`1st` $\rightarrow$ "first", `2nd` $\rightarrow$ "second", `21st` $\rightarrow$ "twenty-first").
  - Expands years and decimals (`2024` $\rightarrow$ "twenty twenty-four", `3.5` $\rightarrow$ "three point five").
  - Expands common tech acronyms (`AI` $\rightarrow$ "A I", `API` $\rightarrow$ "A P I", `GPU` $\rightarrow$ "G P U", `LLM` $\rightarrow$ "L L M").
- `count_words(text: str) -> int`:
  - Computes spoken word count in script text.
- `parse_script_payload(raw_data: str | dict[str, Any], default_source_url: str = "") -> ScriptPayload`:
  - Parses JSON strings or dictionaries across alternate key naming (`yt_title`/`title`, `script`/`full_script`).
- `validate_script_payload(payload: ScriptPayload, min_words: int = 85, max_words: int = 115) -> tuple[bool, str]`:
  - Verifies word count against the 85–115 word YouTube Shorts constraint.

### Data Model (`ScriptPayload`)
```python
@dataclass
class ScriptPayload:
    hook: str
    script: str
    yt_title: str
    yt_desc: str
    body: str = ""
    punchline: str = ""

    @property
    def title(self) -> str:
        return self.yt_title

    @property
    def full_script(self) -> str:
        return self.script

    @property
    def word_count(self) -> int:
        return count_words(self.script)
```

---

## 4. `tts.py` — Local TTS Synthesizer

### Overview
Synthesizes `narration.wav` using `kokoro-onnx` and measures exact audio duration $T$, serving as the master timing clock for video assembly. Generates 24kHz 16-bit mono PCM WAV conforming to AGENTS.md Constraints 1 & 3.

### Key Functions & Interfaces
- `class LocalTTS`:
  - `__init__(model_path: str | Path | None = None, voices_path: str | Path | None = None, voice_name: str = "af_bella", speed: float = 1.08, fallback_mock: bool = True)`:
    - Configures speech synthesis parameters and lazily loads the ONNX runtime session for Kokoro speech model.
  - `synthesize(text: str, output_wav_path: str | Path, voice: str | None = None, speed: float | None = None) -> AudioMeta`:
    - Synthesizes 24kHz 16-bit mono WAV.
    - Probes duration using `wave` / `soundfile`.
    - Returns `AudioMeta(wav_path=..., duration_seconds=...)`.
    - Automatically falls back to clean mock/tone synthesis when model weights are not present if `fallback_mock=True`.
- `synthesize_speech(text: str, output_path: str | Path, voice: str | None = None, speed: float | None = None, config: AppConfig | None = None, fallback_mock: bool = True) -> float`:
  - Convenience function taking plain text and writing 24kHz mono WAV, returning exact duration $T$ in seconds.
- `async async_synthesize_speech(text: str, output_path: str | Path, voice: str | None = None, speed: float | None = None, config: AppConfig | None = None, fallback_mock: bool = True) -> float`:
  - Asynchronous non-blocking wrapper running synthesis via worker thread executor.
- `get_audio_duration(file_path: str | Path) -> float`:
  - Reads exact duration $T$ from WAV file with millisecond precision via `wave`.
- `is_kokoro_model_available(models_dir: str | Path = "models", model_filename: str = "kokoro-v0_19.onnx", voices_filename: str = "voices.bin") -> bool`:
  - Non-raising checker verifying if model and voices files exist in target directory.
- `ensure_kokoro_model(models_dir: str | Path = "models", download_if_missing: bool = False, ...) -> tuple[Path, Path]`:
  - Checks for required Kokoro model assets; downloads missing files from GitHub releases if `download_if_missing=True`, with friendly diagnostic errors on offline/network failure.

### Data Model
```python
@dataclass
class AudioMeta:
    wav_path: str
    duration_seconds: float
```


---

## 5. `concat_client.py` — Concat Desktop JSON-RPC Client

### Overview
Manages IPC with Concat Desktop in `window` mode. Dynamically discovers connection credentials (`port`, `token`) from `~/.config/concat/automation.json`, establishes JSON-RPC over TCP / HTTP with `Authorization: Bearer <token>` authentication, builds tracks, triggers captions, and controls rendering. Includes in-process `MockConcatServer` and offline mock mode for testing without a live Concat GUI.

### Key Data Models & Helper Functions
- `load_automation_config(config_path: str | Path = "~/.config/concat/automation.json") -> AutomationConfig`:
  - Reads, parses, and validates `port`, `token`, and optional `host` from JSON file.
- `class AutomationConfig`:
  - `port: int`: Dynamic socket port.
  - `token: str`: Bearer authorization token.
  - `host: str = "127.0.0.1"`: Localhost or automation host.
- `class MockConcatServer`:
  - Built-in multi-threaded HTTP/TCP JSON-RPC server for automated tests and offline simulation.
  - Generates `automation.json` with ephemeral port and token.
  - Simulates RPC methods, authentication checks, errors, and rendering poll lifecycles.

### Key Client Methods (`class ConcatClient`)
- `__init__(config_path=None, host=None, port=None, token=None, timeout=30.0, mock=False)`:
  - Initializes client instance; supports offline `mock=True` mode.
- `connect(config_path: str | Path | None = None) -> None`:
  - Resolves dynamic credentials from `automation.json` (or configured path) and performs connection verification.
- `disconnect() -> None`:
  - Closes active connection session.
- `rpc_call(method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> Any`:
  - Formats JSON-RPC 2.0 payload, injects `Authorization: Bearer <token>`, and handles responses and error structures.
- `ping() -> dict[str, Any]`:
  - Verifies connection and retrieves application status/version handshake.
- `project_create(name: str = "short", path: str | None = None, **kwargs) -> str`:
  - Creates an isolated project in non-destructive window mode (Constraint 5); returns `project_id`.
- `project_set_video(width: int = 1080, height: int = 1920, fps: int = 30, timeline_background: str = "blur", **kwargs) -> dict[str, Any]`:
  - Sets project resolution (default 1080x1920 9:16 vertical), frame rate, and canvas background styling.
- `media_import(file_path: str | Path, **kwargs) -> str`:
  - Imports media asset into project library; returns `media_id`.
- `clip_add(first: str, second: str | None = None, start_time: float = 0.0, duration: float = 0.0, in_point: float = 0.0, *, track=None, track_type=None, media_id=None, **kwargs) -> str`:
  - Adds clip to video or audio track; supports both `(track, media_id, ...)` and `(media_id, track_type, ...)` calling conventions. Returns `clip_id`.
- `keyframe_set(clip_id: str, property: str, time: float, value: float, **kwargs) -> dict[str, Any]`:
  - Sets property keyframe (e.g. Ken Burns zoom `scale`).
- `captions_add(clip_id: str, language: str = "en", **kwargs) -> dict[str, Any]`:
  - Invokes built-in `whisper.cpp` to create word-synced subtitles.
- `create_shorts_project(audio_path: str | Path, image_paths: Sequence[str | Path], video_paths: Sequence[str | Path] | None = None, project_name: str = "short", output_resolution: tuple[int, int] | list[int] = (1080, 1920), **kwargs) -> TimelineResult`:
  - High-level coordinator delegating to `build_timeline` to build project, set vertical dimensions, place audio, trigger subtitles, sequence user video clips, loop image assets with Ken Burns keyframes, and save project.
- `project_save(**kwargs) -> dict[str, Any]`:
  - Persists project state.
- `export_start(output_path: str | Path, preset: str = "youtube_shorts", **kwargs) -> str`:
  - Initiates video rendering pipeline; returns `export_id`.
- `export_wait(export_id: str, timeout: float = 300.0, poll_interval: float = 1.0) -> dict[str, Any]`:
  - Polls export status until render completes, raising `ConcatExportTimeoutError` or `ConcatRPCError` on failure.

### CLI Usage
```bash
# Verify connection to running Concat instance
python concat_client.py --ping

# Test custom automation config path
python concat_client.py --ping --config ~/.config/concat/automation.json
```

---

## 5.1 `timeline.py` — Ken Burns Visual Timeline Builder & Subtitle Automation

### Overview
Implements Constraint 3 (Master Clock $T$): bounds the visual timeline strictly to narration audio duration $T$, places user videos first (ducking/trimming when $T_{\text{videos}} \ge T$), fills remaining time $T_{\text{rem}} = T - T_{\text{videos}}$ with 8.0-second image slots alternating Ken Burns zoom directions, pro-rates the terminal slot zoom scale, and automates whisper.cpp subtitle stamping via `captions_add`.

### Key Data Models
- `class VideoSlot`:
  - `video_index: int`, `path: Path`, `start_time: float`, `duration: float`
- `class ImageSlot`:
  - `slot_index: int`, `image_index: int`, `path: Path`, `start_time: float`, `duration: float`, `start_scale: float`, `end_scale: float`
- `class TimelinePlan`:
  - `total_duration: float`, `videos: list[VideoSlot]`, `images: list[ImageSlot]`
- `class TimelineResult`:
  - `project_id: str`, `audio_clip_id: str`, `captions_id: str | None`, `video_clip_ids: list[str]`, `image_clip_ids: list[str]`, `total_duration: float`, `plan: TimelinePlan`

### Key Functions & Interfaces
- `calculate_timeline_plan(total_duration: float, video_durations: Sequence[float], num_images: int, image_paths: Sequence[str | Path] | None = None, video_paths: Sequence[str | Path] | None = None, slot_duration: float = 8.0) -> TimelinePlan`:
  - Pure mathematical planning of visual slot layout and keyframe scale values.
- `calculate_ken_burns_slots(remaining_duration: float, num_images: int, slot_duration: float = 8.0, start_time_offset: float = 0.0, image_paths: Sequence[str | Path] | None = None) -> list[ImageSlot]`:
  - Convenience helper for calculating Ken Burns slots across a duration.
- `probe_video_duration(file_path: str | Path) -> float`:
  - Probes exact container duration via `ffprobe`.
- `build_timeline(client: ConcatClient, audio_path: str | Path, image_paths: Sequence[str | Path], video_paths: Sequence[str | Path] | None = None, project_name: str = "short", output_resolution: tuple[int, int] | list[int] = (1080, 1920), fps: int = 30, timeline_background: str = "blur", audio_duration: float | None = None, video_durations: dict[str | Path, float] | Sequence[float] | None = None, language: str = "en", slot_duration: float = 8.0, **kwargs: Any) -> TimelineResult`:
  - Orchestrates project creation, canvas setup, audio import, subtitle generation, video placement, Ken Burns image looping with keyframes, and project persistence.

---

## 6. `youtube_uploader.py` — YouTube Data API Publisher

### Overview
Handles OAuth2 authentication flow, token caching and automatic refresh, Shorts metadata formatting, resumable chunked upload loop with exponential backoff for transient network errors, unverified app privacy fallback, and 403 quota exhaustion detection.

### Data Models & Exceptions
- `class YouTubeUploadResult`:
  - `video_id: str`: Published YouTube video identifier.
  - `video_url: str`: Shorts direct URL (`https://youtube.com/shorts/<video_id>`).
  - `privacy_status: str`: Published privacy status (`"public"`, `"unlisted"`, or `"private"`).
- `class YouTubeError(Exception)`: Base error for YouTube publisher operations.
- `class YouTubeAuthenticationError(YouTubeError)`: Raised when OAuth credentials or token files are missing/invalid.
- `class YouTubeQuotaExceededError(YouTubeError)`: Raised on 403 `quotaExceeded` / `dailyLimitExceeded` to halt pipeline while keeping local MP4 intact.
- `class YouTubeUploadError(YouTubeError)`: Raised on persistent network or HTTP upload failures.

### Key Formatting Helpers
- `format_shorts_title(title: str, max_length: int = 100) -> str`:
  - Appends `#Shorts` if not present (case-insensitive) and ensures the total length does not exceed YouTube's 100-character title limit while preserving `#Shorts`.
- `format_tags(tags: Sequence[str] | None, default_tags: Sequence[str] | None = None) -> list[str]`:
  - Ensures `Shorts` is in the tag pool and deduplicates tags case-insensitively while preserving ordering.

### Client Class (`class YouTubeUploader`)
- `__init__(client_secrets_file: str | Path = "credentials/client_secret.json", token_file: str | Path = "credentials/token.json", credentials: Any | None = None, scopes: Sequence[str] | None = None, mock: bool = False)`:
  - Initializes client instance; supports offline dry-run simulation (`mock=True`).
- `get_credentials() -> Any`:
  - Resolves and refreshes OAuth2 credentials from `token.json` using `Request()`, falling back to `InstalledAppFlow` via `client_secret.json`.
- `get_service() -> Any`:
  - Builds and caches the googleapiclient `youtube.v3` Resource instance.
- `upload_video(video_path: str | Path, title: str, description: str, tags: Sequence[str] | None = None, privacy_status: str = "public", category_id: str = "28", chunk_size: int = DEFAULT_CHUNK_SIZE, max_retries: int = 5, retry_delay: float = 1.0, dry_run: bool = False, **kwargs: Any) -> YouTubeUploadResult`:
  - Validates `video_path` existence.
  - Formats title with `#Shorts`, category ID `28` (Science & Technology), and tags.
  - Initiates resumable chunked upload (`MediaFileUpload(resumable=True)`).
  - Handles transient network errors with exponential backoff.
  - Catches 403 quota exhaustion and raises `YouTubeQuotaExceededError`.
  - Catches unverified app public upload blocks and automatically retries with `"private"`.
  - Returns `YouTubeUploadResult`.

### Convenience Function
- `upload_video(video_path: str | Path, title: str, description: str, tags: Sequence[str] | None = None, privacy_status: str | None = None, category_id: str | None = None, client: YouTubeUploader | None = None, config: Any | None = None, dry_run: bool = False, **kwargs: Any) -> YouTubeUploadResult`:
  - Functional entrypoint accepting custom config or an existing `YouTubeUploader` instance.

---

## 7. `pipeline.py` — Orchestrator & Job Worker

### Overview
Coordinates the execution of all six stages from batch handoff to YouTube publication and Telegram reply. Provides workspace setup, structured timing metrics, error handling with automatic database status transitions, synchronous and asynchronous Python entrypoints, and a standalone CLI runner.

### Data Models & Exceptions
- `class PipelineResult(str)`:
  - Subclasses `str` to return the published YouTube Shorts URL while providing rich structured metadata.
  - Attributes: `batch_id: int | str`, `status: str`, `youtube_url: str | None`, `video_path: Path | None`, `duration_seconds: float`, `script: ScriptPayload | None`, `temp_dir: Path | None`, `stage_timings: dict[str, float]`, `error_message: str | None`.
- `class PipelineError(Exception)`:
  - Base exception raised on stage failures with descriptive context.

### Key Functions
- `init_workspace(base_dir: str | Path | None = None) -> dict[str, Path]`:
  - Ensures standard directory hierarchy exists (`output/`, `temp/`, `credentials/`, `models/`).
- `async run_pipeline(batch: int | Batch | None = None, article_url: str | None = None, config: AppConfig | None = None, dry_run: bool = False, keep_temp: bool = False, database: Database | None = None, base_dir: str | Path | None = None) -> PipelineResult | None`:
  - Primary async orchestrator executing Stages 1 through 6.
  - Stage 1: Ingestion & Scraping (`scraper.scrape_article(url, output_dir=temp_dir)`)
  - Stage 2: Script Generation (`writer.generate_script(article_text, title=...)`)
  - Stage 3: Voice Synthesis (`tts.synthesize_speech(script, output_path=...)`) -> duration $T$
  - Stage 4: Concat Desktop Assembly & Export (`client.create_shorts_project(...)` + `export_start` + `export_wait`) -> `output/{batch_id}.mp4`
  - Stage 5: YouTube Publishing (`youtube_uploader.upload_video(...)`) -> `YouTubeUploadResult`
  - Stage 6: Status Update & Temp Cleanup (`database.mark_completed`, cleanup `temp_dir` unless `keep_temp=True`)
- `process_batch(batch: int | Batch, config: AppConfig | None = None, dry_run: bool = False, keep_temp: bool = False, database: Database | None = None, base_dir: str | Path | None = None) -> PipelineResult | None`:
  - Synchronous wrapper for `run_pipeline` for blocking workers and job dispatchers.
- `main(argv: list[str] | None = None) -> int`:
  - CLI runner supporting `--test-url`, `--dry-run`, `--config`, `--keep-temp`, and `--base-dir`.

### CLI Usage
```bash
# Standalone dry-run test
python pipeline.py --test-url "https://example.com/article" --dry-run

# Production run with custom config and temporary file retention
python pipeline.py --test-url "https://example.com/article" --config config.yaml --keep-temp
```

---

## 8. `config.py` — Configuration Management & Validation

### Overview
Defines Pydantic models for type-safe validation of all pipeline settings across Telegram, scraping, Ollama, TTS, Concat, and YouTube. Loads from YAML (`config.yaml` or `config.example.yaml`), falls back to defaults, and supports environment variable overrides.

### Models & Key Functions
- `TelegramConfig`: Bot token, authorized chat IDs, debounce window, SQLite database path.
- `ScraperConfig`: User-Agent header and Playwright screenshot timeout.
- `OllamaConfig`: Host URL (default `http://192.168.1.124:11434`), model (default `qwen2.5-coder:14b`), temperature.
- `TTSConfig`: Kokoro ONNX model and voices paths, voice name, speech speed.
- `ConcatConfig`: Automation config path, resolution, FPS, background blur, export timeout.
- `YouTubeConfig`: OAuth client secrets, token cache, privacy status, category ID, default tags.
- `AppConfig`: Root configuration container holding all sub-configs.
- `load_config(config_path: str | Path | None = None) -> AppConfig`: Safe loader merging YAML and environment variables (`SECTION_KEY` or `SECTION__KEY`).

