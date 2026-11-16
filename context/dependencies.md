# Dependencies & Ecosystem

This document catalogues system prerequisites, hardware runtime requirements, Python dependencies, and external service integrations.

---

## 1. Hardware & Operating System

- **Operating System**: Debian Linux (tested on Debian 12 / Bookworm) or Ubuntu 22.04+ LTS.
- **CPU**: AMD Ryzen AI 9 HX 370 (12 cores / 24 threads, AVX-512 support).
- **GPU**: NVIDIA GeForce RTX 3060 (12GB VRAM, CUDA 12.x / cuDNN enabled).
- **Audio Output**: ALSA / PulseAudio / PipeWire (for local audio driver verification).

---

## 2. System Software & Local Daemons

### Concat Desktop
- **Version**: $\ge$ 0.2.9
- **Settings Required**: **Settings > Automation > Scripts and apps** enabled.
- **IPC File**: Concat writes its active port and token to:
  ```text
  ~/.config/concat/automation.json
  ```
- **Socket Protocol**: Newline-delimited JSON-RPC over TCP (or through MCP helper `concat --mcp window`).
- **Features Used**: `project_create`, `project_set_video`, `media_import`, `clip_add`, `keyframe_set`, `captions_add` (whisper.cpp), `export_start`, `export_wait`.

### Ollama (Local LLM Server)
- **Version**: $\ge$ 0.3.0
- **Listen Address**: `http://192.168.1.124:11434` (configurable via `OLLAMA_HOST` or `config.yaml`)
- **Configured Default Model**: `qwen2.5-coder:14b` (fast, high-accuracy structured JSON scriptwriting)
- **Alternative Models**: `qwen3.5:latest`, `llama3.1:8b`
- **Feature Required**: Native JSON mode (`format="json"`).

### Playwright Headless Browser
- **Engine**: Chromium (headless)
- **Usage**: Fallback screenshot generation when user drops bare links without photos or videos.

---

## 3. Python Ecosystem & Requirements (`requirements.txt`)

| Package | Version | Purpose |
| :--- | :--- | :--- |
| `aiogram` | `^3.14.0` | Telegram Bot framework (async event handling) |
| `trafilatura` | `^1.12.0` | High-accuracy article body & metadata extraction |
| `playwright` | `^1.48.0` | Headless page rendering and screenshot capture |
| `ollama` | `^0.3.3` | Python client for local Ollama LLM execution |
| `kokoro-onnx` | `^0.3.1` | Local ONNX-based speech synthesis (Kokoro voice) |
| `soundfile` | `^0.12.1` | Audio WAV file validation and duration probing |
| `numpy` | `^1.26.0` | Numerical array handling for audio signals |
| `google-api-python-client` | `^2.150.0` | YouTube Data API v3 client |
| `google-auth-oauthlib` | `^1.2.1` | OAuth 2.0 desktop token generation |
| `google-auth-httplib2` | `^0.2.0` | HTTP transport for Google OAuth |
| `pyyaml` | `^6.0.2` | Configuration file parser |
| `pydantic` | `^2.9.2` | Data validation and structured schemas |
| `pytest` | `^8.3.3` | Test suite execution |
| `pytest-asyncio` | `^0.24.0` | Async test fixtures for aiogram / scraper |
| `ruff` | `^0.7.0` | Fast Python linter and formatter |
| `mypy` | `^1.13.0` | Static type analysis |

---

## 4. External APIs & Cloud Services

### Telegram Bot API
- **Token**: Provisioned via `@BotFather`.
- **Bot Permissions**: Access to read messages, download photos, download videos, send replies.
- **Debouncing**: Handled locally via SQLite across `media_group_id`.

### Google Cloud & YouTube Data API v3
- **API**: YouTube Data API v3 (`videos.insert`).
- **OAuth Scope**: `https://www.googleapis.com/auth/youtube.upload`.
- **Authentication Credentials**:
  - `client_secret.json`: Downloaded from GCP Console (Desktop Client OAuth App).
  - `token.json`: Generated on first run; automatically refreshed by Google Auth library.
- **Publishing Status & GCP Verification Guard**:
  - `privacyStatus: "public"` requires either an "Internal" app status (Google Workspace) or completed Google OAuth App Verification.
  - If using an unverified "External" GCP app, YouTube enforces `privacyStatus: "private"` by default on API uploads.
