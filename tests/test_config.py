import pytest
from pydantic import ValidationError

from config import (
    AppConfig,
    ConcatConfig,
    OllamaConfig,
    ScraperConfig,
    TelegramConfig,
    TTSConfig,
    YouTubeConfig,
    load_config,
)


def test_default_config():
    cfg = AppConfig()
    # Sections exist
    assert isinstance(cfg.telegram, TelegramConfig)
    assert isinstance(cfg.scraper, ScraperConfig)
    assert isinstance(cfg.ollama, OllamaConfig)
    assert isinstance(cfg.tts, TTSConfig)
    assert isinstance(cfg.concat, ConcatConfig)
    assert isinstance(cfg.youtube, YouTubeConfig)

    # Required Ollama defaults
    assert cfg.ollama.host == "http://192.168.1.124:11434"
    assert cfg.ollama.model == "qwen2.5-coder:14b"
    assert cfg.ollama.temperature == 0.7

    # Telegram defaults
    assert cfg.telegram.debounce_seconds == 15
    assert cfg.telegram.database_path == "queue.db"

    # Concat defaults
    assert cfg.concat.resolution == [1080, 1920] or cfg.concat.resolution == (1080, 1920)
    assert cfg.concat.fps == 30

    # YouTube defaults
    assert "Shorts" in cfg.youtube.default_tags


def test_load_from_yaml(tmp_path):
    yaml_content = """
telegram:
  bot_token: "test_bot_token"
  allowed_chat_ids: [111, 222]
  debounce_seconds: 20
ollama:
  host: "http://10.0.0.5:11434"
  model: "llama3.2:3b"
concat:
  fps: 60
"""
    yaml_file = tmp_path / "custom_config.yaml"
    yaml_file.write_text(yaml_content)

    cfg = load_config(yaml_file)
    assert cfg.telegram.bot_token == "test_bot_token"
    assert cfg.telegram.allowed_chat_ids == [111, 222]
    assert cfg.telegram.debounce_seconds == 20
    assert cfg.ollama.host == "http://10.0.0.5:11434"
    assert cfg.ollama.model == "llama3.2:3b"
    assert cfg.concat.fps == 60
    # Other sections remain default
    assert cfg.scraper.screenshot_timeout_ms == 15000
    assert cfg.tts.voice == "am_adam"


def test_load_nonexistent_file():
    with pytest.raises(FileNotFoundError):
        load_config("nonexistent_file_path_12345.yaml")


def test_load_config_env_overrides(tmp_path, monkeypatch):
    yaml_content = """
ollama:
  host: "http://initial:11434"
  model: "initial-model"
telegram:
  bot_token: "yaml_token"
"""
    yaml_file = tmp_path / "config.yaml"
    yaml_file.write_text(yaml_content)

    monkeypatch.setenv("OLLAMA_HOST", "http://overridden:11434")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env_secret_token")
    monkeypatch.setenv("TELEGRAM_DEBOUNCE_SECONDS", "45")

    cfg = load_config(yaml_file)
    assert cfg.ollama.host == "http://overridden:11434"
    assert cfg.telegram.bot_token == "env_secret_token"
    assert cfg.telegram.debounce_seconds == 45
    # Non-overridden remains from yaml
    assert cfg.ollama.model == "initial-model"


def test_load_config_nested_env_overrides(tmp_path, monkeypatch):
    yaml_file = tmp_path / "empty.yaml"
    yaml_file.write_text("{}")

    monkeypatch.setenv("SCRAPER__SCREENSHOT_TIMEOUT_MS", "30000")
    monkeypatch.setenv("CONCAT__FPS", "24")

    cfg = load_config(yaml_file)
    assert cfg.scraper.screenshot_timeout_ms == 30000
    assert cfg.concat.fps == 24


def test_load_config_fallback_to_example(tmp_path, monkeypatch):
    example_yaml = tmp_path / "config.example.yaml"
    example_yaml.write_text("""
telegram:
  bot_token: "from_example"
ollama:
  host: "http://192.168.1.124:11434"
  model: "qwen2.5-coder:14b"
""")
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    assert cfg.telegram.bot_token == "from_example"
    assert cfg.ollama.host == "http://192.168.1.124:11434"
    assert cfg.ollama.model == "qwen2.5-coder:14b"


def test_load_config_prefers_config_yaml_over_example(tmp_path, monkeypatch):
    example_yaml = tmp_path / "config.example.yaml"
    example_yaml.write_text("telegram:\n  bot_token: 'from_example'\n")
    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text("telegram:\n  bot_token: 'from_config'\n")

    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    assert cfg.telegram.bot_token == "from_config"


def test_validation_error():
    with pytest.raises(ValidationError):
        TelegramConfig(debounce_seconds="not_a_number")  # type: ignore[arg-type]


def test_load_config_invalid_yaml_type(tmp_path):
    invalid_file = tmp_path / "invalid.yaml"
    invalid_file.write_text("- item1\n- item2\n")
    with pytest.raises(ValueError, match="YAML mapping"):
        load_config(invalid_file)
