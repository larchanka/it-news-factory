# Codebase Context & Navigation

This directory contains technical documentation for **IT News Factory**, covering architecture, module specifications, dependencies, and engineering conventions.

---

## Navigation Map

1. [**System Architecture**](./architecture.md)
   - High-level pipeline overview and lifecycle flow.
   - Concat desktop window mode JSON-RPC protocol.
   - Bypassing Concat issue #707 via local Kokoro TTS.
   - Telegram 15s SQLite debounce state machine.
   - Ken Burns visual loop timing calculations.

2. [**Module Catalog**](./modules.md)
   - Detailed breakdown of all modules:
     - `config.py`: Central configuration manager and Pydantic validation schemas.
     - `database.py`: SQLite state machine, message ingestion, and 15-second debounce queue.
     - `bot.py`: Telegram event listener and background debounce dispatcher.
     - `scraper.py`: Trafilatura article parser & Playwright headless fallback.
     - `writer.py`: Ollama structured JSON prompt generator and phonetic speech sanitizer.
     - `tts.py`: Local Kokoro ONNX synthesis & duration probe ($T$ master clock).
     - `concat_client.py`: Window-attached JSON-RPC client.
     - `timeline.py`: Ken Burns visual timeline builder and whisper subtitles.
     - `youtube_uploader.py`: Resumable YouTube Data API v3 uploader.
     - `pipeline.py`: Orchestrator connecting all six stages.

3. [**Dependencies & Environment**](./dependencies.md)
   - Host platform requirements (Debian Linux, Ryzen AI 9, RTX 3060).
   - Python third-party packages and pinned versions.
   - Concat Desktop installation and automation setup.
   - Ollama local model serving.
   - YouTube Data API v3 credentials and scopes.

4. [**Engineering Conventions**](./conventions.md)
   - Code style, type annotations, and linting.
   - Master clock audio anchoring ($T$).
   - Alternating Ken Burns zoom formula.
   - Robust error handling, retries, and failure recovery.

---

## Quick Facts

- **Target Output Format**: YouTube Shorts (1080x1920 vertical, 30fps, H.264/AAC).
- **Target Video Duration**: 35–45 seconds (~85–115 spoken words).
- **Primary Orchestrator**: [`pipeline.py`](../pipeline.py)
- **Primary Service Entrypoint**: [`bot.py`](../bot.py)
- **Primary Automation Channel**: Local JSON-RPC over TCP via `~/.config/concat/automation.json`.
