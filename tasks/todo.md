# Todo Checklist: IT News Factory Implementation

## Phase 1: Foundation & Data Management
- [x] **Task 1: Configuration Management & Validation**
  - [x] Implement `config.py` with Pydantic `AppConfig` models for telegram, scraper, ollama, tts, concat, youtube
  - [x] Support loading from YAML, environment overrides, and fallback defaults
  - [x] Set Ollama default host to `http://192.168.1.124:11434` and model to `qwen2.5-coder:14b`
  - [x] Create `tests/test_config.py` and verify test pass
- [x] **Task 2: SQLite Debounce State Machine & Queue Manager**
  - [x] Implement `database.py` with `batches` and `batch_media` schema
  - [x] Implement message upsert and 15-second inactivity query
  - [x] Implement atomic lock acquisition (`pending` -> `processing`)
  - [x] Create `tests/test_database.py` and verify test pass

### Checkpoint: Foundation
- [x] All tests pass: `pytest tests/test_config.py tests/test_database.py -v`
- [x] Clean type annotations and zero linter warnings

---

## Phase 2: Ingestion & Web Scraping
- [x] **Task 3: Content & Media Scraper with Playwright Fallback**
  - [x] Implement `scraper.py` using `trafilatura` for body text, title, and OpenGraph images
  - [x] Add async Playwright headless Chromium screenshot fallback (`capture_page_screenshot`)
  - [x] Implement image downloading and validation into local temp directory
  - [x] Create `tests/test_scraper.py` and verify test pass
- [x] **Task 4: Telegram Ingestion Bot & Debounce Dispatcher**
  - [x] Implement `bot.py` using `aiogram` 3.x with message and media group handlers
  - [x] Implement 15-second debounce background polling loop checking for expired batches
  - [x] Implement Telegram status feedback notifications (received, processing, published)
  - [x] Create `tests/test_bot.py` with mock Telegram events and verify test pass

### Checkpoint: Ingestion & Scraping
- [x] All tests pass: `pytest tests/test_config.py tests/test_database.py tests/test_scraper.py tests/test_bot.py -v`
- [x] Ingestion flow verified with mock media group drop

---

## Phase 3: Scriptwriting & Voice Synthesis
- [x] **Task 5: Ollama Scriptwriter & Phonetic Sanitizer**
  - [x] Implement `writer.py` communicating with Ollama JSON endpoint (`format="json"`)
  - [x] Implement cynical prompt template for hook (0-3s), meat (3-30s), punchline (30-40s)
  - [x] Implement phonetic speech sanitizer (strip markdown/emojis, expand numbers, currency, acronyms)
  - [x] Enforce 85–115 word count constraint with validation and retry
  - [x] Create `tests/test_writer.py` and verify test pass
- [x] **Task 6: Kokoro Local TTS Synthesizer & Audio Master Clock**
  - [x] Implement `tts.py` using `kokoro-onnx` for 24kHz 16-bit mono WAV generation
  - [x] Implement duration probe returning exact seconds $T$
  - [x] Add model weights checker / downloader helper for Kokoro ONNX files
  - [x] Create `tests/test_tts.py` and verify test pass

### Checkpoint: Scriptwriting & Audio Synthesis
- [x] All tests pass: `pytest tests/test_writer.py tests/test_tts.py -v`
- [x] Pipeline produces valid `ScriptPayload` and measurable `narration.wav`


---

## Phase 4: Concat Desktop Window Automation & Visual Timeline
- [x] **Task 7: Concat JSON-RPC Client & Connection Protocol**
  - [x] Implement `concat_client.py` reading dynamic port & token from `~/.config/concat/automation.json`
  - [x] Implement TCP JSON-RPC client: `project_create`, `project_set_video`, `media_import`, `clip_add`, `keyframe_set`, `captions_add`, `project_save`, `export_start`, `export_wait`
  - [x] Implement `--ping` handshake CLI command
  - [x] Add mock socket mode for offline testing
  - [x] Create `tests/test_concat_rpc.py` and verify test pass
- [x] **Task 8: Ken Burns Visual Track Loop Builder & Subtitle Automation**
  - [x] Implement visual timeline math: place user videos first, fill remaining duration $T_{\text{rem}} = T - T_{\text{videos}}$
  - [x] Implement 8.0s image slot looping with alternating Ken Burns zoom (`1.0` $\rightarrow$ `1.10`, `1.10` $\rightarrow$ `1.0`)
  - [x] Implement terminal slot pro-rated zoom calculation: $\text{scale}_{\text{end}} = 1.0 + (0.10 \times d_i / 8.0)$
  - [x] Connect `captions_add` on audio clip for whisper.cpp subtitle stamping
  - [x] Create `tests/test_concat_timeline.py` and verify test pass

### Checkpoint: Concat Desktop Automation
- [x] All tests pass: `pytest tests/test_concat_rpc.py tests/test_concat_timeline.py -v`
- [x] Verified timeline construction and Ken Burns keyframe math

---

## Phase 5: Distribution & Pipeline Orchestration
- [x] **Task 9: YouTube Data API v3 Resumable Publisher**
  - [x] Implement `youtube_uploader.py` using `google-api-python-client` with resumable chunked upload
  - [x] Support OAuth2 token caching in `token.json`
  - [x] Format title with `#Shorts`, description, tags, and category ID 28
  - [x] Handle unverified app privacy fallback and 403 quota exhaustion
  - [x] Create `tests/test_youtube_uploader.py` and verify test pass
- [x] **Task 10: End-to-End Pipeline Orchestrator & CLI Runner**
  - [x] Implement `pipeline.py` connecting Stages 1 through 6
  - [x] Support CLI flags: `--test-url`, `--dry-run`, `--config`, `--keep-temp`
  - [x] Add workspace directory setup (`output/`, `temp/`, `credentials/`, `models/`)
  - [x] Implement structured logging and stage timing diagnostics
  - [x] Create `tests/test_pipeline.py` and verify test pass

### Checkpoint: Pipeline & Distribution
- [x] All tests pass: `pytest tests/ -v`
- [x] Standalone CLI dry-run executes cleanly: `python pipeline.py --test-url "https://example.com" --dry-run`

---

## Phase 6: System Integration, Test Suite & Documentation Verification
- [x] **Task 11: End-to-End Integration Test Suite & Mock Fixtures**
  - [x] Create mock fixtures in `tests/conftest.py` for Ollama, Concat, YouTube, and Telegram
  - [x] Implement full integration test `tests/test_e2e.py` covering happy paths and failure recovery
  - [x] Ensure test suite runs offline with 100% pass rate
- [x] **Task 12: Code Quality, Typing, Linting & Documentation Synchronization**
  - [x] Run `ruff check .` and fix any issues
  - [x] Run `black --check .` (or `ruff format --check .`)
  - [x] Run `mypy .` and resolve type discrepancies
  - [x] Synchronize `context/modules.md` and `README.md` if any signatures evolved

### Final Checkpoint: Complete Verification
- [x] Full test suite green: `pytest tests/ -v`
- [x] Ruff, Black, and Mypy checks clean
- [x] Documentation up to date
