from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class TelegramConfig(BaseModel):
    bot_token: str = ""
    allowed_chat_ids: list[int] = Field(default_factory=list)
    debounce_seconds: int = 15
    database_path: str = "queue.db"


class ScraperConfig(BaseModel):
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    )
    screenshot_timeout_ms: int = 15000


class OllamaConfig(BaseModel):
    host: str = "http://192.168.1.124:11434"
    model: str = "qwen2.5-coder:14b"
    temperature: float = 0.7


class TTSConfig(BaseModel):
    model_path: str = "models/kokoro-v0_19.onnx"
    voices_path: str = "models/voices.bin"
    voice: str = "am_adam"
    speed: float = 1.08


class ConcatConfig(BaseModel):
    config_path: str = "~/.config/concat/automation.json"
    resolution: list[int] = Field(default_factory=lambda: [1080, 1920])
    fps: int = 30
    background: str = "blur"
    export_timeout_seconds: int = 180


class YouTubeConfig(BaseModel):
    client_secrets_file: str = "credentials/client_secret.json"
    token_file: str = "credentials/token.json"
    privacy_status: str = "public"
    category_id: str = "28"
    default_tags: list[str] = Field(
        default_factory=lambda: ["Shorts", "TechNews", "Programming", "AI"]
    )


class AppConfig(BaseModel):
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    scraper: ScraperConfig = Field(default_factory=ScraperConfig)
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    concat: ConcatConfig = Field(default_factory=ConcatConfig)
    youtube: YouTubeConfig = Field(default_factory=YouTubeConfig)


_SECTION_MODELS: dict[str, type[BaseModel]] = {
    "telegram": TelegramConfig,
    "scraper": ScraperConfig,
    "ollama": OllamaConfig,
    "tts": TTSConfig,
    "concat": ConcatConfig,
    "youtube": YouTubeConfig,
}


def _parse_env_val(val: str) -> Any:
    val_stripped = val.strip()
    if (val_stripped.startswith("[") and val_stripped.endswith("]")) or (
        val_stripped.startswith("{") and val_stripped.endswith("}")
    ):
        try:
            return json.loads(val_stripped)
        except (json.JSONDecodeError, ValueError):
            return val
    return val


def _apply_env_overrides(raw_data: dict[str, Any]) -> dict[str, Any]:
    result = dict(raw_data)
    for env_key, env_val in os.environ.items():
        key_lower = env_key.lower()

        # Check double underscore: e.g. OLLAMA__HOST
        if "__" in key_lower:
            parts = key_lower.split("__", 1)
            section, field = parts[0], parts[1]
            if section in _SECTION_MODELS:
                model_cls = _SECTION_MODELS[section]
                if field in model_cls.model_fields:
                    sec_dict = result.setdefault(section, {})
                    if isinstance(sec_dict, dict):
                        sec_dict[field] = _parse_env_val(env_val)
            continue

        # Check single underscore: e.g. OLLAMA_HOST, TELEGRAM_BOT_TOKEN
        for section, model_cls in _SECTION_MODELS.items():
            prefix = f"{section}_"
            if key_lower.startswith(prefix):
                field = key_lower[len(prefix) :]
                if field in model_cls.model_fields:
                    sec_dict = result.setdefault(section, {})
                    if isinstance(sec_dict, dict):
                        sec_dict[field] = _parse_env_val(env_val)
                break

    return result


def load_config(config_path: str | Path | None = None) -> AppConfig:
    """
    Load application configuration from a YAML file with environment variable overrides.

    If config_path is specified:
      - Reads the file; raises FileNotFoundError if it doesn't exist.
    If config_path is None:
      - Tries loading 'config.yaml' first.
      - If 'config.yaml' is absent, tries 'config.example.yaml'.
      - If neither exists, falls back to default settings.
    """
    target_path: Path | None = None

    if config_path is not None:
        target_path = Path(config_path)
        if not target_path.is_file():
            raise FileNotFoundError(f"Config file not found: {config_path}")
    else:
        if Path("config.yaml").is_file():
            target_path = Path("config.yaml")
        elif Path("config.example.yaml").is_file():
            target_path = Path("config.example.yaml")

    raw_data: dict[str, Any] = {}
    if target_path is not None and target_path.is_file():
        with open(target_path, "r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f)
            if loaded is not None and not isinstance(loaded, dict):
                raise ValueError(
                    f"Config file must contain a YAML mapping/dictionary, got {type(loaded).__name__}"
                )
            if isinstance(loaded, dict):
                raw_data = loaded

    data_with_env = _apply_env_overrides(raw_data)
    return AppConfig.model_validate(data_with_env)
