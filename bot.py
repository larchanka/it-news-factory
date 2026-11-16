from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

from config import AppConfig, TelegramConfig, load_config
from database import Batch, Database

logger = logging.getLogger(__name__)

URL_REGEX = re.compile(r"https?://[^\s<>\"']+")


def extract_article_url(text: str | None) -> str | None:
    """Extract first http or https URL found in text, if any, stripping trailing punctuation."""
    if not text:
        return None
    match = URL_REGEX.search(text)
    if not match:
        return None
    url = match.group(0).rstrip(".,!?:;)]\"'")
    return url if url else None


def is_chat_allowed(chat_id: int, allowed_chat_ids: list[int]) -> bool:
    """Check if chat_id is allowed. If allowed_chat_ids is empty, all chats are allowed."""
    if not allowed_chat_ids:
        return True
    return chat_id in allowed_chat_ids


def extract_media(message: Message) -> tuple[Any, str | None, str | None]:
    """Extract media object, media type ('photo'|'video'), and filename from message."""
    if message.photo:
        photo = message.photo[-1]
        return photo, "photo", f"{photo.file_unique_id}.jpg"
    if message.video:
        video = message.video
        raw_name = getattr(video, "file_name", None)
        safe_name = Path(raw_name).name if raw_name else ""
        name = (
            safe_name
            if safe_name and safe_name not in (".", "..")
            else f"{video.file_unique_id}.mp4"
        )
        return video, "video", name
    return None, None, None


class TelegramBotService:
    """Handles Telegram ingestion, media caching, and debounce coordination."""

    MAX_ACK_CACHE: int = 1000

    def __init__(
        self,
        config: TelegramConfig,
        database: Database,
        staging_dir: str | Path = "temp",
    ) -> None:
        self.config = config
        self.db = database
        self.staging_dir = Path(staging_dir)
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self._acknowledged_groups: OrderedDict[str, None] = OrderedDict()
        self._acknowledged_batches: OrderedDict[int, None] = OrderedDict()

    async def handle_message(self, message: Message, bot: Bot) -> int | None:
        """Process incoming Telegram message, persist link & media, and return batch_id."""
        chat_id = message.chat.id
        if not is_chat_allowed(chat_id, self.config.allowed_chat_ids):
            logger.warning("Rejected message from unauthorized chat_id=%s", chat_id)
            await message.answer("⚠️ Unauthorized chat ID. Access denied.")
            return None

        text = message.text or message.caption or ""
        article_url = extract_article_url(text)
        media_group_id = message.media_group_id
        file_obj, file_type, file_name = extract_media(message)

        if not article_url and not file_type:
            logger.info("Received text without URL or media from chat_id=%s", chat_id)
            await message.answer("ℹ️ Please provide a news article link (URL) or attach media.")
            return None

        batch_id = self.db.upsert_message(
            chat_id=chat_id,
            article_url=article_url,
            media_group_id=media_group_id,
        )

        if file_obj and file_type and file_name:
            media_dir = self.staging_dir / str(batch_id) / "user_media"
            media_dir.mkdir(parents=True, exist_ok=True)
            target_path = media_dir / file_name

            await bot.download(file_obj, destination=target_path)

            self.db.upsert_message(
                chat_id=chat_id,
                media_group_id=media_group_id,
                file_type=file_type,
                file_path=str(target_path),
            )

        await self._send_acknowledgment_once(message, batch_id, media_group_id)
        return batch_id

    async def _send_acknowledgment_once(
        self,
        message: Message,
        batch_id: int,
        media_group_id: str | None,
    ) -> None:
        if media_group_id:
            if media_group_id in self._acknowledged_groups:
                return
            self._acknowledged_groups[media_group_id] = None
            if len(self._acknowledged_groups) > self.MAX_ACK_CACHE:
                self._acknowledged_groups.popitem(last=False)
        elif batch_id in self._acknowledged_batches:
            return
        else:
            self._acknowledged_batches[batch_id] = None
            if len(self._acknowledged_batches) > self.MAX_ACK_CACHE:
                self._acknowledged_batches.popitem(last=False)

        try:
            await message.answer(
                f"📥 Received news link/media (Batch #{batch_id}). "
                f"Debouncing for {self.config.debounce_seconds}s for any further media..."
            )
        except TelegramAPIError as err:
            logger.error("Failed sending acknowledgment for batch #%s: %s", batch_id, err)


class DebounceDispatcher:
    """Monitors expired batches and dispatches them atomically to the pipeline."""

    def __init__(
        self,
        bot: Bot,
        database: Database,
        config: TelegramConfig,
        pipeline_runner: Callable[[Batch], Awaitable[str | None]] | None = None,
        poll_interval: float = 1.0,
    ) -> None:
        self.bot = bot
        self.db = database
        self.config = config
        self.pipeline_runner = pipeline_runner or self._default_pipeline_runner
        self.poll_interval = poll_interval

    async def _default_pipeline_runner(self, batch: Batch) -> str | None:
        try:
            from pipeline import run_pipeline

            return await run_pipeline(batch)  # type: ignore[no-any-return]
        except ImportError:
            logger.warning(
                "pipeline.py not yet implemented; claimed batch #%s in mock mode",
                batch.id,
            )
            return None

    async def _notify_chat(self, chat_id: int, text: str) -> None:
        try:
            await self.bot.send_message(chat_id=chat_id, text=text)
        except TelegramAPIError as err:
            logger.error("Failed sending Telegram status to chat_id=%s: %s", chat_id, err)

    async def process_expired_batches(self) -> list[int]:
        """Scan and process batches whose debounce inactivity has expired."""
        expired = self.db.get_expired_batches(debounce_seconds=self.config.debounce_seconds)
        claimed_ids: list[int] = []

        for batch in expired:
            if not self.db.claim_batch(batch.id):
                continue
            claimed_ids.append(batch.id)

            await self._notify_chat(
                batch.chat_id,
                f"🎬 Debounce complete. Starting pipeline processing for Batch #{batch.id}...",
            )

            try:
                youtube_url = await self.pipeline_runner(batch)
                self.db.mark_completed(batch.id, youtube_url=youtube_url)
                await self._notify_chat(
                    batch.chat_id,
                    (
                        f"🚀 YouTube Short published for Batch #{batch.id}!\n"
                        f"{youtube_url or 'Render finished successfully.'}"
                    ),
                )
            except Exception as exc:
                logger.exception("Pipeline execution failed for batch #%s", batch.id)
                self.db.mark_failed(batch.id, error_message=str(exc))
                await self._notify_chat(
                    batch.chat_id,
                    f"❌ Processing failed for Batch #{batch.id}:\n{exc}",
                )

        return claimed_ids

    async def run_loop(self, stop_event: asyncio.Event | None = None) -> None:
        """Continuous polling loop dispatching expired batches."""
        logger.info("Debounce dispatcher loop started (interval=%ss)", self.poll_interval)
        while stop_event is None or not stop_event.is_set():
            try:
                await self.process_expired_batches()
            except Exception:
                logger.exception("Error during debounce batch check")

            try:
                if stop_event is not None:
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=self.poll_interval)
                        break
                    except TimeoutError:
                        pass
                else:
                    await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                logger.info("Debounce dispatcher loop cancelled.")
                break


def create_dispatcher(service: TelegramBotService) -> Dispatcher:
    """Create and configure aiogram Dispatcher with service handlers."""
    dp = Dispatcher()
    router = Router()

    @router.message()
    async def on_message(message: Message, bot: Bot) -> None:
        await service.handle_message(message, bot)

    dp.include_router(router)
    return dp


async def run_bot(config: AppConfig | None = None) -> None:
    """Application entrypoint for running the Telegram bot and debounce dispatcher."""
    if config is None:
        config = load_config()

    if not config.telegram.bot_token:
        raise ValueError("TELEGRAM__BOT_TOKEN is not configured.")

    bot = Bot(token=config.telegram.bot_token)
    db = Database(config.telegram.database_path)
    service = TelegramBotService(config=config.telegram, database=db)
    dp = create_dispatcher(service)

    stop_event = asyncio.Event()
    dispatcher = DebounceDispatcher(
        bot=bot,
        database=db,
        config=config.telegram,
        poll_interval=1.0,
    )
    dispatcher_task = asyncio.create_task(dispatcher.run_loop(stop_event=stop_event))

    logger.info("Starting Telegram Bot polling...")
    try:
        await dp.start_polling(bot)
    finally:
        stop_event.set()
        dispatcher_task.cancel()
        await asyncio.gather(dispatcher_task, return_exceptions=True)
        await bot.session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_bot())
