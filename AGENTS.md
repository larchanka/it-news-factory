# Agent Instructions & Codebase Guidelines

Welcome to **IT News Factory**. This file defines operational rules, architectural foundations, verified developer commands, and operating constraints for AI agents and automated coding tools working within this repository.

---

## 1. Project Overview

- **Repository Purpose**: Autonomous pipeline that transforms tech news links and media dropped into Telegram into complete, high-retention vertical YouTube Shorts (1080x1920) rendered via Concat Desktop and published with zero manual intervention.
- **Core Stack**: Python 3.11+, `aiogram`, `trafilatura`, `playwright`, `ollama`, `kokoro-onnx` / `sherpa-onnx`, Concat Desktop v0.2.9+ (Window JSON-RPC automation), `google-api-python-client`, SQLite.
- **Runtime Environment**: Debian Linux on AMD Ryzen AI 9 HX 370 + NVIDIA RTX 3060 (CUDA / ONNX Runtime execution).

---

## 2. Essential Commands

> [!IMPORTANT]
> Run all commands from the repository root inside the active Python virtual environment (`.venv`).

### Environment & Setup
```bash
# Set up Python virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install Python dependencies
pip install -r requirements.txt

# Install Playwright browser engines (for fallback screenshot capture)
playwright install chromium
```

### Running & Service Operations
```bash
# Start Telegram listener & orchestrator
python bot.py

# Run standalone end-to-end test on a single article URL
python pipeline.py --test-url "https://example.com/tech-news-article" --dry-run

# Test Concat window connection and basic JSON-RPC handshake
python concat_client.py --ping
```

### Testing & Verification
```bash
# Run all unit tests
pytest tests/ -v

# Run a single test module
pytest tests/test_concat_client.py -v

# Linting & Style
ruff check .
ruff format --check .

# Static Type Checking
mypy .
```

---

## 3. Architecture & Context Navigation

Detailed architectural documentation, data schemas, module breakdowns, and domain invariants are maintained in [`context/`](./context/):

- [**context/index.md**](./context/index.md): Central navigation map and repository summary.
- [**context/architecture.md**](./context/architecture.md): Complete 6-stage lifecycle, Concat window JSON-RPC communication, and debounce state machine.
- [**context/modules.md**](./context/modules.md): Module-by-module breakdown of functions, schemas, and interfaces (`config.py`, `database.py`, `bot.py`, `scraper.py`, `writer.py`, `tts.py`, `concat_client.py`, `timeline.py`, `youtube_uploader.py`, `pipeline.py`).
- [**context/dependencies.md**](./context/dependencies.md): System dependencies, hardware accelerators, external APIs, and Python packages.
- [**context/conventions.md**](./context/conventions.md): Code style, Ken Burns math formulas, SQLite debouncing rules, and error recovery policies.

### Primary Entrypoints & Core Hubs

| File | Subsystem Role | Invariant / Boundary |
| :--- | :--- | :--- |
| `config.py` | Central configuration | Pydantic type-safe validation; Ollama defaults to `http://192.168.1.124:11434` with `qwen2.5-coder:14b` |
| `database.py` | SQLite state & debounce manager | Atomic state transitions (`pending` -> `processing` -> `completed`/`failed`) |
| `bot.py` | Telegram entrypoint & SQLite 15s debouncer | Aggregates `media_group_id` before firing pipeline |
| `pipeline.py` | Orchestration coordinator | Chops job across stages 1 through 6 |
| `scraper.py` | Ingestion & context extractor | Guarantees text + at least 1 image (trafilatura / Playwright) |
| `writer.py` | Ollama script generation | Connects to `http://192.168.1.124:11434`, enforces 85–115 words, strict JSON schema, TTS phonetic numbers |
| `tts.py` | Voice synthesis (`kokoro-onnx`) | Generates `narration.wav` and measures duration $T$ as master clock |
| `concat_client.py` | Concat Desktop Window JSON-RPC driver | Dynamic port detection from `~/.config/concat/automation.json` |
| `timeline.py` | Ken Burns timeline planner & whisper subtitles | Slices visual track conforming strictly to $T$; alternating zoom keyframes |
| `youtube_uploader.py` | YouTube Data API v3 publisher | Resumable chunked upload with `privacyStatus: "public"` |

---

## 4. Agent Rules & Operational Constraints

When modifying or extending this codebase, adhere strictly to these constraints:

### Constraint 1: Concat Issue #707 Workaround
Never attempt to call `concat-speech` or Concat's internal TTS over JSON-RPC. Concat v0.2.9 restricts TTS to GUI interaction ([Issue #707](https://github.com/concat-app/concat/issues/707)). All voice generation must be executed locally by `tts.py` using `kokoro-onnx`, producing `narration.wav` which is then fed into Concat via `media_import`.

### Constraint 2: Dynamic Port & Token Resolution
Never hardcode port numbers or tokens for Concat. Concat writes its active port and bearer token to `~/.config/concat/automation.json` on startup. `concat_client.py` must dynamically re-read this file on each execution or reconnection.

### Constraint 3: Audio Duration $T$ Is the Master Clock
The visual timeline must always conform to the audio duration $T$:
1. Place user video clips first.
2. Fill remaining time $(T - T_{\text{videos}})$ with 8.0-second image slots alternating Ken Burns zoom directions (`1.0` $\rightarrow$ `1.10`, `1.10` $\rightarrow$ `1.0`).
3. Trim the final image slot to match the exact terminal timestamp of $T$ so the video ends cleanly with the voiceover.

### Constraint 4: TTS Formatting Invariants
The local LLM in `writer.py` must return clean plain text for speech synthesis:
- **No markdown** (`*`, `#`, `_`).
- **No emojis**.
- **Spelled-out numbers and units** (e.g. "forty-two million dollars" instead of "$42M", "three point five" instead of "3.5").

### Constraint 5: Non-Destructive Window Mode
All Concat commands must run in `window` mode. Never manipulate or overwrite the user's active desktop project; agent jobs must create and close their own isolated projects.

### Constraint 6: Surgical Edits & Minimum Complexity
- Touch only lines and modules directly required for the task.
- Do not introduce speculative abstractions or unrequested configuration files.
- Preserve existing naming, typing, and formatting standards.

---

## 5. Automatic Context & Graph Maintenance

> [!IMPORTANT]
> If any code was added, refactored, or removed, you must keep the architectural documentation in [`context/`](./context/) accurate and up to date.

1. **Verify Interface Integrity**: If a module signature in `modules.md` changed, update the documentation immediately.
2. **Verify Cross-Links**: Ensure all markdown links across `README.md`, `AGENTS.md`, and `context/*.md` resolve properly.
3. **Run Validation**: Verify linting and tests before declaring any task complete.
