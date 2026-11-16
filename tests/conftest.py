from __future__ import annotations

import json
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from concat_client import MockConcatServer
from database import Database
from scraper import ScrapedContent
from youtube_uploader import YouTubeUploadResult

# Standard test MP4 header (ftyp box)
MOCK_MP4_BYTES = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00isommp42"
# Minimal 1x1 PNG bytes
MOCK_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00"
    b"\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

# 92 spoken words: perfectly satisfies 85-115 word constraint
VALID_SHORTS_SCRIPT = (
    "Engineers thought this major database bug was completely impossible until yesterday when "
    "it struck production without warning. An automated migration worker malfunctioned during "
    "peak traffic hours, accidentally isolating primary data clusters from the global application "
    "mesh. The entire engineering organization immediately initiated emergency disaster recovery "
    "protocols. Teams worked through the night to verify cryptographic ledger integrity and safely "
    "reroute active read traffic to secondary replicas across three continents. In the end, complete "
    "data consistency was successfully restored with zero customer data loss. Always test your migration "
    "jobs thoroughly before pushing to production."
)


@pytest.fixture
def mock_ollama_structured_data() -> dict[str, str]:
    """Return standard valid structured JSON dictionary for Ollama scriptwriter."""
    return {
        "hook": "Engineers thought this database bug was impossible until now.",
        "script": VALID_SHORTS_SCRIPT,
        "yt_title": "Database Disaster Averted #Shorts",
        "yt_desc": (
            "Full breakdown of the critical database migration incident. "
            "Source: https://example.com/db-incident #Shorts #TechNews"
        ),
    }


@pytest.fixture
def mock_ollama_json_str(mock_ollama_structured_data: dict[str, str]) -> str:
    """Return serialized JSON string of valid script payload."""
    return json.dumps(mock_ollama_structured_data)


class MockChatResponse:
    """Mock Ollama chat response matching library structure."""

    def __init__(self, content: str) -> None:
        self.message = {"role": "assistant", "content": content}


class MockOllamaClient:
    """Mock Ollama client supporting both sync and async chat invocations with transient failure support."""

    def __init__(
        self,
        responses: list[Any] | None = None,
        fail_count: int = 0,
        error_factory: Callable[[], Exception] | None = None,
    ) -> None:
        self.responses = responses or []
        self.fail_count = fail_count
        self.error_factory = error_factory or (
            lambda: ConnectionError("Ollama endpoint unreachable")
        )
        self.call_count = 0
        self.recorded_calls: list[dict[str, Any]] = []

    def _get_next_response(self, kwargs: dict[str, Any]) -> Any:
        self.recorded_calls.append(kwargs)
        self.call_count += 1
        if self.call_count <= self.fail_count:
            raise self.error_factory()

        if self.responses:
            idx = min(self.call_count - self.fail_count - 1, len(self.responses) - 1)
            resp = self.responses[idx]
            if isinstance(resp, Exception):
                raise resp
            if isinstance(resp, str):
                return MockChatResponse(resp)
            return resp

        return MockChatResponse(
            json.dumps(
                {
                    "hook": "Engineers thought this database bug was impossible until now.",
                    "script": VALID_SHORTS_SCRIPT,
                    "yt_title": "Database Incident #Shorts",
                    "yt_desc": "Incident analysis. #Shorts #TechNews",
                }
            )
        )

    def chat(self, **kwargs: Any) -> Any:
        return self._get_next_response(kwargs)

    async def chat_async(self, **kwargs: Any) -> Any:
        return self._get_next_response(kwargs)

    # In case AsyncClient.chat is called as a coroutine
    async def __call__(self, **kwargs: Any) -> Any:
        return self._get_next_response(kwargs)


@pytest.fixture
def mock_ollama_client_factory(
    mock_ollama_json_str: str,
) -> Callable[..., MockOllamaClient]:
    """Factory fixture to create customized mock Ollama clients."""

    def _factory(
        responses: list[Any] | None = None,
        fail_count: int = 0,
        error_factory: Callable[[], Exception] | None = None,
    ) -> MockOllamaClient:
        resolved_responses = responses if responses is not None else [mock_ollama_json_str]
        client = MockOllamaClient(
            responses=resolved_responses,
            fail_count=fail_count,
            error_factory=error_factory,
        )
        return client

    return _factory


@pytest.fixture
def mock_ollama_client(mock_ollama_json_str: str) -> MockOllamaClient:
    """Default mock Ollama client returning valid structured JSON."""
    return MockOllamaClient(responses=[mock_ollama_json_str])


@pytest.fixture
def mock_concat_server(tmp_path: Path) -> Generator[tuple[MockConcatServer, Path], None, None]:
    """In-process mock Concat Desktop JSON-RPC window server with automation config on disk."""
    config_path = tmp_path / "concat_automation.json"
    server = MockConcatServer(host="127.0.0.1", port=0)
    server.start()
    server.write_automation_json(config_path)

    try:
        yield server, config_path
    finally:
        server.stop()


@pytest.fixture
def mock_youtube_upload_result() -> YouTubeUploadResult:
    """Standard successful YouTube Shorts publication result."""
    return YouTubeUploadResult(
        video_id="mock_shorts_e2e_id",
        video_url="https://youtube.com/shorts/mock_shorts_e2e_id",
        privacy_status="public",
    )


@pytest.fixture
def mock_telegram_bot() -> AsyncMock:
    """Mock aiogram Bot instance that simulates media downloading and records chat notifications."""
    bot = AsyncMock()

    async def fake_download(file_obj: Any, destination: Path) -> Path:
        dest = Path(destination)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.is_file():
            # Write dummy image or video header depending on suffix
            if dest.suffix.lower() in (".mp4", ".mov"):
                dest.write_bytes(MOCK_MP4_BYTES)
            else:
                dest.write_bytes(MOCK_PNG_BYTES)
        return dest

    bot.download = AsyncMock(side_effect=fake_download)
    bot.send_message = AsyncMock()
    bot.get_file = AsyncMock(return_value=MagicMock(file_path="mock/remote/file.png"))
    return bot


@pytest.fixture
def telegram_message_factory() -> Callable[..., AsyncMock]:
    """Factory fixture to create realistic aiogram Message mock instances."""

    def _factory(
        chat_id: int = 12345,
        text: str | None = None,
        caption: str | None = None,
        photo_ids: list[str] | None = None,
        video_id: str | None = None,
        video_name: str | None = None,
        media_group_id: str | None = None,
        message_id: int = 1,
    ) -> AsyncMock:
        msg = AsyncMock()
        msg.message_id = message_id
        msg.chat = MagicMock()
        msg.chat.id = chat_id
        msg.text = text
        msg.caption = caption
        msg.media_group_id = media_group_id

        if photo_ids:
            photos = []
            for pid in photo_ids:
                p = MagicMock()
                p.file_id = f"fid_{pid}"
                p.file_unique_id = pid
                p.width = 1080
                p.height = 1920
                photos.append(p)
            msg.photo = photos
        else:
            msg.photo = None

        if video_id:
            vid = MagicMock()
            vid.file_id = f"vid_{video_id}"
            vid.file_unique_id = video_id
            vid.file_name = video_name or f"{video_id}.mp4"
            vid.duration = 5.0
            msg.video = vid
        else:
            msg.video = None

        msg.answer = AsyncMock()
        return msg

    return _factory


@pytest.fixture
def mock_scraped_content_factory(tmp_path: Path) -> Callable[..., ScrapedContent]:
    """Factory creating ScrapedContent instances with verified temporary images on disk."""

    def _factory(
        url: str = "https://example.com/news/breakthrough",
        title: str = "Breakthrough Tech News 2026",
        text: str = "A revolutionary technological breakthrough was announced today.",
        num_images: int = 2,
    ) -> ScrapedContent:
        image_paths: list[str] = []
        for i in range(num_images):
            img_file = tmp_path / f"scraped_img_{i}.png"
            if not img_file.exists():
                img_file.write_bytes(MOCK_PNG_BYTES)
            image_paths.append(str(img_file))

        return ScrapedContent(
            url=url,
            title=title,
            text=text,
            image_paths=image_paths,
        )

    return _factory


@pytest.fixture
def e2e_workspace(tmp_path: Path) -> Generator[dict[str, Any], None, None]:
    """Isolated complete workspace for end-to-end integration execution."""
    dirs = {
        "root": tmp_path,
        "output": tmp_path / "output",
        "temp": tmp_path / "temp",
        "credentials": tmp_path / "credentials",
        "models": tmp_path / "models",
        "staging": tmp_path / "staging",
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)

    db_path = tmp_path / "queue.db"
    db = Database(db_path=db_path)

    yield {
        "dirs": dirs,
        "db": db,
        "db_path": db_path,
    }
