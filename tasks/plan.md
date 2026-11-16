# Implementation Plan: IT News Factory

## Overview
IT News Factory is an autonomous pipeline that transforms tech news links and media dropped into Telegram into complete, high-retention vertical YouTube Shorts (1080x1920, 30fps) with synchronized subtitles, Ken Burns visual looping, and voiceover, rendered via Concat Desktop and published to YouTube with zero manual intervention.

This plan details the step-by-step implementation from scratch based on the architectural specifications in `context/` and operational constraints in `AGENTS.md`.

---

## Architecture Decisions & Constraints

1. **Local Ollama Endpoint Integration**:
   - Primary LLM server: `http://192.168.1.124:11434`.
   - Default model: `qwen2.5-coder:14b` (verified available; fast, high quality structured JSON generation) with fallback support for `qwen3.5:latest` or `llama3.2:3b`.
   - Temperature: `0.7`.
   - Output format: Enforced JSON object (`format="json"`).

2. **Master Clock Audio Anchoring ($T$)**:
   - The audio duration $T$ produced by `tts.py` (via `kokoro-onnx`) is the absolute master clock for visual assembly and export bounds.
   - Any user video clips occupy the initial $T_{\text{videos}}$ seconds.
   - Remaining time $T_{\text{rem}} = T - T_{\text{videos}}$ is covered using 8.0s image slots with alternating Ken Burns zoom keyframes (`1.0` $\rightarrow$ `1.10` and `1.10` $\rightarrow$ `1.0`), with the final slot trimmed and pro-rated to match $T$ exactly.

3. **Concat Desktop Window Mode & Issue #707 Workaround**:
   - In accordance with Concat Issue #707, internal Concat TTS (`concat-speech`) is restricted to the GUI. We synthesize voice locally with `kokoro-onnx` and import `narration.wav` via `media_import`.
   - Dynamic port & token discovery reads `~/.config/concat/automation.json` on each connection attempt.
   - Concat automation runs strictly in `window` mode without touching active user projects.
   - Concat client supports a mock/dry-run mode for environments without a running Concat GUI.

4. **15-Second Telegram SQLite Debounce**:
   - Incoming messages, photos, and videos sharing a `media_group_id` or sent in rapid succession are recorded in `queue.db`.
   - Inactivity of 15 seconds triggers an atomic transition from `pending` to `processing` and dispatches the job to `pipeline.py`.

5. **Phonetic Speech Sanitization**:
   - `writer.py` strictly sanitizes the script for TTS: no markdown symbols, no emojis, numbers/currency/symbols expanded to spoken words ("forty-two million dollars", "three point five"), abbreviations expanded ("A I", "A P I").

---

## Task List

### Phase 1: Foundation & Data Management

## Task 1: Configuration Management & Validation
**Description:** Implement `config.py` using Pydantic to load, validate, and provide type-safe access to application configuration from `config.yaml` (falling back to `config.example.yaml` defaults). Configure the Ollama host default to `http://192.168.1.124:11434` with model `qwen2.5-coder:14b`.

**Acceptance criteria:**
- [x] `AppConfig` Pydantic model covers all sections: `telegram`, `scraper`, `ollama`, `tts`, `concat`, and `youtube`.
- [x] Loads YAML configuration file safely with environment variable overrides or fallback defaults.
- [x] Ollama host defaults to `http://192.168.1.124:11434` with model `qwen2.5-coder:14b`.

**Verification:**
- [x] Tests pass: `pytest tests/test_config.py -v`
- [x] Typing check passes: `mypy config.py`

**Dependencies:** None
**Files likely touched:**
- `config.py`
- `config.example.yaml`
- `tests/test_config.py`
**Estimated scope:** Small (2-3 files)

---

## Task 2: SQLite Debounce State Machine & Queue Manager
**Description:** Implement `database.py` (or SQLite manager in `bot.py`) providing schema initialization, atomic message ingestion, 15-second debounce expiration querying, and state transitions (`pending` -> `processing` -> `completed` / `failed`) for Telegram drops.

**Acceptance criteria:**
- [x] SQLite tables `batches` and `batch_media` are initialized with proper foreign keys and timestamps.
- [x] Incoming messages update `last_updated_at` to reset the debounce timer.
- [x] `get_expired_batches(debounce_seconds=15)` retrieves batches with elapsed inactivity.
- [x] `mark_batch_processing(batch_id)` atomically acquires lock (only 1 worker processes a batch).

**Verification:**
- [x] Tests pass: `pytest tests/test_database.py -v`
- [x] Concurrency/race condition test confirms single-worker lock acquisition.

**Dependencies:** Task 1
**Files likely touched:**
- `database.py`
- `tests/test_database.py`
**Estimated scope:** Small (2 files)

---

### Checkpoint: Foundation
- [x] Configuration and Database unit tests pass: `pytest tests/test_config.py tests/test_database.py -v`
- [x] Clean type annotations and zero linter warnings on Foundation code.

---

### Phase 2: Ingestion & Web Scraping

## Task 3: Content & Media Scraper with Playwright Fallback
**Description:** Implement `scraper.py` using `trafilatura` to extract clean article text, title, and OpenGraph/Twitter image URLs. Include an async Playwright headless Chromium fallback (`capture_page_screenshot`) when the article contains no media and the user did not drop images.

**Acceptance criteria:**
- [x] `scrape_article(url: str) -> ScrapedContent` extracts title, text, and image URLs.
- [x] Filters out low-resolution thumbnails and icons from extracted image URLs.
- [x] `capture_page_screenshot(url: str, output_path: str)` captures a crisp viewport screenshot using Playwright when no images exist.
- [x] Downloads images into a local temporary workspace directory.

**Verification:**
- [x] Tests pass: `pytest tests/test_scraper.py -v`
- [x] Test covers both standard trafilatura extraction and headless screenshot fallback.

**Dependencies:** Task 1
**Files likely touched:**
- `scraper.py`
- `tests/test_scraper.py`
**Estimated scope:** Small (2 files)

---

## Task 4: Telegram Ingestion Bot & Debounce Dispatcher
**Description:** Implement `bot.py` using `aiogram` 3.x to receive text, links, photos, and videos from authorized Telegram chats, aggregate grouped media (`media_group_id`), and run an async background worker checking for expired batches to dispatch to the pipeline.

**Acceptance criteria:**
- [x] Authenticates incoming updates against `allowed_chat_ids`.
- [x] Downloads incoming Telegram photos and videos to local staging directories.
- [x] Debounce background task queries `database.py` every second and dispatches expired batches.
- [x] Sends feedback messages (processing start, progress, and final YouTube Shorts link) back to Telegram.

**Verification:**
- [x] Tests pass: `pytest tests/test_bot.py -v`
- [x] Simulated media group messages reset the debounce timer and yield a single batch.

**Dependencies:** Task 1, Task 2
**Files likely touched:**
- `bot.py`
- `tests/test_bot.py`
**Estimated scope:** Medium (2-3 files)

---

### Checkpoint: Ingestion & Scraping
- [x] Tests pass for config, database, scraper, and bot: `pytest tests/test_config.py tests/test_database.py tests/test_scraper.py tests/test_bot.py -v`
- [x] Mock Telegram media group produces a populated `BatchPayload` with scraped text and cached images.

---

### Phase 3: Scriptwriting & Voice Synthesis

## Task 5: Ollama Scriptwriter & Phonetic Sanitizer
**Description:** Implement `writer.py` to communicate with the local Ollama instance (`http://192.168.1.124:11434`), requesting structured JSON containing `hook`, `script`, `yt_title`, and `yt_desc`. Enforce strict 85–115 word count limits and phonetic speech sanitization (stripping markdown/emojis, expanding numbers, currency, and abbreviations).

**Acceptance criteria:**
- [x] Prompts Ollama with `format="json"` and parses the response into `ScriptPayload`.
- [x] Validates word count (85–115 words); retries or trims cleanly if outside target.
- [x] Strips markdown (`*`, `#`, `_`, `` ` ``) and emojis completely.
- [x] Phonetically expands numbers (`$42M` -> "forty-two million dollars", `3.5` -> "three point five", `AI` -> "A I", `API` -> "A P I").

**Verification:**
- [x] Tests pass: `pytest tests/test_writer.py -v`
- [x] Unit tests verify regex phonetic expansions and Ollama mock/live responses.

**Dependencies:** Task 1
**Files likely touched:**
- `writer.py`
- `tests/test_writer.py`
**Estimated scope:** Small (2 files)

---

## Task 6: Kokoro Local TTS Synthesizer & Audio Master Clock
**Description:** Implement `tts.py` utilizing `kokoro-onnx` to synthesize 24kHz 16-bit mono `narration.wav` from the sanitized script. Measure exact duration $T$ using `soundfile` / `wave` to serve as the master timing anchor for video generation.

**Acceptance criteria:**
- [x] `LocalTTS` loads ONNX weights (`kokoro-v0_19.onnx`, `voices.bin`) and synthesizes speech with voice `am_adam`.
- [x] Outputs valid 24kHz mono WAV to destination path.
- [x] Measures duration in seconds with millisecond precision ($T$).
- [x] Gracefully handles missing model weights with clear download instructions or automated download helper.

**Verification:**
- [x] Tests pass: `pytest tests/test_tts.py -v`
- [x] Generated audio file verified for sampling rate, channels, and duration metadata.

**Dependencies:** Task 1, Task 5
**Files likely touched:**
- `tts.py`
- `tests/test_tts.py`
**Estimated scope:** Small (2 files)

---

### Checkpoint: Scriptwriting & Audio Synthesis
- [x] Tests pass across Phases 1-3: `pytest tests/test_writer.py tests/test_tts.py -v`
- [x] Sample scraped article successfully produces validated `ScriptPayload` and measurable `narration.wav`.

---

### Phase 4: Concat Desktop Window Automation & Visual Timeline

## Task 7: Concat JSON-RPC Client & Connection Protocol
**Description:** Implement the core TCP JSON-RPC client in `concat_client.py`. Dynamically read connection parameters (`port`, `token`) from `~/.config/concat/automation.json`, establish socket connection, and implement RPC primitives: `project_create`, `project_set_video`, `media_import`, `clip_add`, `keyframe_set`, `captions_add`, `project_save`, `export_start`, and `export_wait`.

**Acceptance criteria:**
- [x] Dynamically parses `port` and `token` from `~/.config/concat/automation.json` on each connection.
- [x] Implements raw JSON-RPC 2.0 over TCP with proper authentication headers / tokens.
- [x] Implements ping / status verification (`concat_client.py --ping`).
- [x] Provides clean timeout and retry handling for Concat export polling (`export_wait`).
- [x] Supports mock mode for unit tests and headless environments.

**Verification:**
- [x] Tests pass: `pytest tests/test_concat_rpc.py -v`
- [x] Mock TCP server verifies correct JSON-RPC method payloads and token passing.

**Dependencies:** Task 1
**Files likely touched:**
- `concat_client.py`
- `tests/test_concat_rpc.py`
**Estimated scope:** Medium (2-3 files)

---

## Task 8: Ken Burns Visual Track Loop Builder & Subtitle Automation
**Description:** Implement timeline construction in `concat_client.py`: layout user videos first (capped at $T$), fill remaining duration $T_{\text{rem}} = T - T_{\text{videos}}$ with 8.0s image slots looping available images, calculate alternating Ken Burns keyframes (`1.0` $\rightarrow$ `1.10`, `1.10` $\rightarrow$ `1.0`), pro-rate the final trimmed slot, and call `captions_add` on the master audio track.

**Acceptance criteria:**
- [x] Timeline conforms strictly to master audio duration $T$.
- [x] Even slots zoom from `1.0` to `1.10`; odd slots zoom from `1.10` to `1.0`.
- [x] Terminal trimmed slot ($i = N-1$) pro-rates ending zoom scale $\text{scale}_{\text{end}} = 1.0 + (0.10 \times d_i / 8.0)$.
- [x] Calls `captions_add` with built-in `whisper.cpp` to stamp animated subtitles.

**Verification:**
- [x] Tests pass: `pytest tests/test_concat_timeline.py -v`
- [x] Mathematical validation tests verify slot count $N$, slice durations, and zoom scale bounds.

**Dependencies:** Task 6, Task 7
**Files likely touched:**
- `concat_client.py`
- `tests/test_concat_timeline.py`
**Estimated scope:** Small (2 files)

---

### Checkpoint: Concat Desktop Automation
- [x] Tests pass for Concat RPC and timeline logic: `pytest tests/test_concat_rpc.py tests/test_concat_timeline.py -v`
- [x] Simulated assembly creates accurate project structure with exact duration matching $T$.

---

### Phase 5: Distribution & Pipeline Orchestration

## Task 9: YouTube Data API v3 Resumable Publisher
**Description:** Implement `youtube_uploader.py` using `google-api-python-client` with resumable file upload. Handle OAuth2 credential caching (`token.json`), set Shorts metadata (`#Shorts` in title, category `28`, `privacyStatus: "public"` with private fallback), and return the published YouTube Shorts URL.

**Acceptance criteria:**
- [x] Loads or refreshes OAuth tokens from `token.json` or desktop client secret.
- [x] Uploads video via chunked resumable upload (`MediaFileUpload`).
- [x] Tags, description, and title contain `#Shorts` and source attribution.
- [x] Handles 403 quota exhaustion gracefully by alerting user and keeping the local MP4 intact.
- [x] Provides dry-run / mock mode for development and testing without GCP credentials.

**Verification:**
- [x] Tests pass: `pytest tests/test_youtube_uploader.py -v`
- [x] Mock YouTube API service validates request payload, metadata, and URL generation.

**Dependencies:** Task 1
**Files likely touched:**
- `youtube_uploader.py`
- `tests/test_youtube_uploader.py`
**Estimated scope:** Small (2 files)

---

## Task 10: End-to-End Pipeline Orchestrator & CLI Runner
**Description:** Implement `pipeline.py` connecting all stages: scrape article, write script via Ollama, synthesize narration audio, build Concat visual project, export vertical MP4, and upload to YouTube. Implement CLI arguments (`--test-url`, `--dry-run`, `--config`, `--keep-temp`) for standalone testing and manual runs.

**Acceptance criteria:**
- [x] `process_batch(batch: BatchPayload) -> str` executes Stages 1 through 6 seamlessly.
- [x] CLI flags: `python pipeline.py --test-url <url> --dry-run` runs end-to-end without real external publishing.
- [x] Cleans up intermediate temporary files unless `--keep-temp` is specified.
- [x] Emits detailed structured logs for each stage execution time and status.

**Verification:**
- [x] Tests pass: `pytest tests/test_pipeline.py -v`
- [x] CLI dry-run executes cleanly on sample URL: `python pipeline.py --test-url "https://example.com" --dry-run`

**Dependencies:** Tasks 1, 2, 3, 5, 6, 7, 8, 9
**Files likely touched:**
- `pipeline.py`
- `tests/test_pipeline.py`
**Estimated scope:** Medium (2-3 files)

---

### Checkpoint: Pipeline & Distribution
- [x] All unit and component tests pass: `pytest tests/ -v`
- [x] End-to-end dry-run executes without error from command line.

---

### Phase 6: System Integration, Test Suite & Documentation Verification

## Task 11: End-to-End Integration Test Suite & Mock Fixtures
**Description:** Build comprehensive integration fixtures in `tests/` testing the entire chain with mock external servers (mock Ollama HTTP server, mock Concat TCP server, mock YouTube API, mock Telegram updates) to ensure 100% test pass rate in CI/CD and offline developer environments.

**Acceptance criteria:**
- [x] Complete mock test suite covers happy paths, edge cases, and failure recoveries.
- [x] Tests verify recovery from scraper failure, LLM retries on bad JSON, and export timeout handling.
- [x] All tests run and pass without requiring external live hardware or network connections.

**Verification:**
- [x] Full test run passes: `pytest tests/ -v`

**Dependencies:** Tasks 1-10
**Files likely touched:**
- `tests/conftest.py`
- `tests/test_e2e.py`
**Estimated scope:** Small (2 files)

---

## Task 12: Code Quality, Typing, Linting & Documentation Synchronization
**Description:** Run static type checking with `mypy`, format and lint with `ruff` and `black`, ensure full adherence to project guidelines in `AGENTS.md`, and verify documentation consistency in `context/`.

**Acceptance criteria:**
- [x] `ruff check .` reports zero errors.
- [x] `black --check .` passes cleanly.
- [x] `mypy .` passes without type errors.
- [x] `context/modules.md` and `README.md` match final function signatures and module interfaces.

**Verification:**
- [x] `ruff check .`
- [x] `mypy .`
- [x] `pytest tests/ -v`

**Dependencies:** Tasks 1-11
**Files likely touched:**
- `context/modules.md`
- `README.md`
- Various source files for type refinements
**Estimated scope:** Small to Medium (3-4 files)

---

### Final Checkpoint: Complete Verification
- [x] All unit, integration, and CLI tests pass (`pytest tests/ -v`).
- [x] Code is formatted and passes `ruff` and `mypy` strict checks.
- [x] Ready for deployment and live testing with local Ollama (`http://192.168.1.124:11434`) and Concat Desktop.

---

## Risks and Mitigations

| Risk | Impact | Mitigation |
| :--- | :--- | :--- |
| **Concat Desktop not running or GUI automation disabled** | High | Dynamic port detector returns informative error; CLI dry-run and mock mode allow offline verification. |
| **Local Ollama endpoint network connectivity** | Medium | Configurable timeout, automatic retry with lower temperature, and clear logging for endpoint unreachable. |
| **Kokoro ONNX model weights missing on first run** | Medium | Check model file existence on startup; provide download utility script or clear instructions pointing to official HuggingFace weights. |
| **Playwright browser dependencies on headless Linux** | Low | Headless screenshot is wrapped in try/except fallback to trafilatura OpenGraph image extraction. |
| **YouTube API Quota exhaustion (403)** | Low | Catch quota error, retain rendered MP4 in `output/`, and report direct download file path in Telegram. |

---

## Open Questions & Decisions
- **LLM Model**: Configured default is `qwen2.5-coder:14b` at `http://192.168.1.124:11434` (fast and reliable JSON compliance). Easily overridden to `qwen3.5:latest` or `llama3.2:3b` in `config.yaml`.
- **Kokoro Model**: Weights placed in `models/kokoro-v0_19.onnx` and `models/voices.bin`. If absent, mock or download helper will ensure graceful operation.
