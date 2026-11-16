from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

logger = logging.getLogger(__name__)

DEFAULT_SHORTS_TAG = "Shorts"
DEFAULT_TAGS = ["Shorts", "TechNews", "Programming", "AI"]
MAX_YOUTUBE_TITLE_LENGTH = 100
YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
DEFAULT_CHUNK_SIZE = 1024 * 1024 * 4  # 4MB chunks (must be multiple of 256KB)
DEFAULT_RETRYABLE_STATUS_CODES = {500, 502, 503, 504, 408, 429}


class YouTubeError(Exception):
    """Base exception for all YouTube publisher errors."""


class YouTubeAuthenticationError(YouTubeError):
    """Raised when authentication credentials are missing or invalid."""


class YouTubeQuotaExceededError(YouTubeError):
    """Raised when YouTube API upload quota (403 quotaExceeded) is exhausted."""


class YouTubeUploadError(YouTubeError):
    """Raised when an unrecoverable error occurs during video upload."""


@dataclass(frozen=True)
class YouTubeUploadResult:
    """Published YouTube video metadata and URL."""

    video_id: str
    video_url: str
    privacy_status: str


def format_shorts_title(title: str, max_length: int = MAX_YOUTUBE_TITLE_LENGTH) -> str:
    """Ensure #Shorts is present in the title and length does not exceed max_length (100)."""
    cleaned = title.strip()
    if "#shorts" in cleaned.lower():
        formatted = cleaned
    else:
        formatted = f"{cleaned} #Shorts"

    if len(formatted) > max_length:
        lower = formatted.lower()
        shorts_idx = lower.rfind("#shorts")
        if shorts_idx != -1:
            tag_part = formatted[shorts_idx:]
            available_for_title = max_length - len(tag_part) - 1
            if available_for_title > 0:
                title_part = formatted[:available_for_title].rstrip()
                formatted = f"{title_part} {tag_part}"
            else:
                formatted = tag_part[:max_length]
        else:
            formatted = formatted[:max_length]

    return formatted


def format_tags(
    tags: Sequence[str] | None,
    default_tags: Sequence[str] | None = None,
) -> list[str]:
    """Ensure Shorts tag is present and deduplicate tags case-insensitively while preserving order."""
    pool: list[str] = []
    if tags:
        pool.extend(tags)
    elif default_tags:
        pool.extend(default_tags)
    else:
        pool.extend(DEFAULT_TAGS)

    if not any(t.lower() == "shorts" for t in pool):
        pool.append(DEFAULT_SHORTS_TAG)

    seen: set[str] = set()
    result: list[str] = []
    for tag in pool:
        clean = tag.strip().lstrip("#")
        if not clean:
            continue
        lower = clean.lower()
        if lower not in seen:
            seen.add(lower)
            result.append(clean)

    return result


def _is_quota_exceeded(error: HttpError) -> bool:
    """Detect if an HttpError corresponds to 403 quotaExceeded or dailyLimitExceeded."""
    if error.resp.status != 403:
        return False
    error_str = str(error)
    if "quotaExceeded" in error_str or "dailyLimitExceeded" in error_str:
        return True
    try:
        content = json.loads(error.content.decode("utf-8"))
        errors = content.get("error", {}).get("errors", [])
        return any(err.get("reason") in {"quotaExceeded", "dailyLimitExceeded"} for err in errors)
    except (json.JSONDecodeError, UnicodeDecodeError, KeyError, AttributeError):
        return False


def _is_privacy_or_unverified_error(error: HttpError) -> bool:
    """Detect if an HttpError indicates unverified app or blocked public upload."""
    if error.resp.status not in (400, 403):
        return False
    error_str = str(error).lower()
    keywords = ("uploadnotpermitted", "invalidprivacystatus", "unverified", "privacystatus")
    if any(k in error_str for k in keywords):
        return True
    try:
        content = json.loads(error.content.decode("utf-8"))
        errors = content.get("error", {}).get("errors", [])
        return any(
            any(
                k in str(err.get("reason", "")).lower() or k in str(err.get("message", "")).lower()
                for k in keywords
            )
            for err in errors
        )
    except (json.JSONDecodeError, UnicodeDecodeError, KeyError, AttributeError):
        return False


def _perform_resumable_upload(
    insert_request: Any,
    max_retries: int,
    retry_delay: float,
) -> dict[str, Any]:
    """Execute the resumable chunk upload loop with exponential backoff for transient errors."""
    response: dict[str, Any] | None = None
    retries = 0

    while response is None:
        try:
            status, response = insert_request.next_chunk()
            if status:
                pct = int(status.progress() * 100)
                logger.debug(f"YouTube upload progress: {pct}%")
        except HttpError as ex:
            if _is_quota_exceeded(ex):
                raise YouTubeQuotaExceededError(
                    f"YouTube API upload quota exceeded (403): {ex}"
                ) from ex
            if ex.resp.status in DEFAULT_RETRYABLE_STATUS_CODES:
                retries += 1
                if retries > max_retries:
                    raise YouTubeUploadError(
                        f"Upload failed after {max_retries} retries on HTTP {ex.resp.status}: {ex}"
                    ) from ex
                sleep_time = retry_delay * (2 ** (retries - 1))
                logger.warning(
                    f"HTTP {ex.resp.status} during upload; retrying in {sleep_time:.2f}s..."
                )
                time.sleep(sleep_time)
                continue
            raise
        except OSError as ex:
            retries += 1
            if retries > max_retries:
                raise YouTubeUploadError(
                    f"Upload failed after {max_retries} retries on network error: {ex}"
                ) from ex
            sleep_time = retry_delay * (2 ** (retries - 1))
            logger.warning(f"Network error '{ex}' during upload; retrying in {sleep_time:.2f}s...")
            time.sleep(sleep_time)

    return response


class YouTubeUploader:
    """Manages YouTube Data API v3 authentication, metadata formatting, and resumable uploads."""

    def __init__(
        self,
        client_secrets_file: str | Path = "credentials/client_secret.json",
        token_file: str | Path = "credentials/token.json",
        credentials: Any | None = None,
        scopes: Sequence[str] | None = None,
        mock: bool = False,
    ) -> None:
        self.client_secrets_file = Path(client_secrets_file)
        self.token_file = Path(token_file)
        self._credentials = credentials
        self.scopes = list(scopes) if scopes else [YOUTUBE_UPLOAD_SCOPE]
        self.mock = mock
        self._service: Any = None

    def _load_cached_credentials(self) -> Credentials | None:
        """Load credentials from token_file if present."""
        if not self.token_file.is_file():
            return None
        try:
            creds = Credentials.from_authorized_user_file(str(self.token_file), self.scopes)
            return cast(Credentials, creds)
        except (ValueError, OSError, KeyError, GoogleAuthError) as e:
            logger.warning(f"Failed to load cached credentials from {self.token_file}: {e}")
            return None

    def _refresh_credentials(self, creds: Credentials) -> Credentials | None:
        """Refresh expired credentials and persist to token_file."""
        if not (creds.expired and creds.refresh_token):
            return creds
        try:
            creds.refresh(Request())
            self.token_file.parent.mkdir(parents=True, exist_ok=True)
            self.token_file.write_text(creds.to_json(), encoding="utf-8")
            logger.info(f"Refreshed expired OAuth credentials saved to {self.token_file}")
            return creds
        except (GoogleAuthError, OSError) as e:
            logger.warning(f"Failed to refresh OAuth token: {e}")
            return None

    def _authenticate_via_flow(self) -> Credentials:
        """Run InstalledAppFlow from client_secrets_file and persist credentials."""
        try:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.client_secrets_file), self.scopes
            )
            creds = flow.run_local_server(port=0)
            if creds:
                self.token_file.parent.mkdir(parents=True, exist_ok=True)
                self.token_file.write_text(creds.to_json(), encoding="utf-8")
                return cast(Credentials, creds)
            raise YouTubeAuthenticationError(
                "Local server authentication flow returned no credentials"
            )
        except Exception as e:
            if isinstance(e, YouTubeAuthenticationError):
                raise
            raise YouTubeAuthenticationError(
                f"OAuth authentication flow failed using {self.client_secrets_file}: {e}"
            ) from e

    def get_credentials(self) -> Any:
        """Resolve and refresh OAuth2 user credentials."""
        if self._credentials is not None:
            return self._credentials

        if self.mock:
            return None

        creds = self._load_cached_credentials()
        if creds:
            creds = self._refresh_credentials(creds)

        if creds and creds.valid:
            self._credentials = creds
            return creds

        if self.client_secrets_file.is_file():
            self._credentials = self._authenticate_via_flow()
            return self._credentials

        raise YouTubeAuthenticationError(
            f"Missing OAuth credentials: {self.client_secrets_file} and {self.token_file} do not exist or are invalid"
        )

    def get_service(self) -> Any:
        """Build or return existing googleapiclient YouTube v3 Resource service."""
        if self._service is not None:
            return self._service

        if self.mock:
            return None

        credentials = self.get_credentials()
        self._service = build("youtube", "v3", credentials=credentials)
        return self._service

    def upload_video(
        self,
        video_path: str | Path,
        title: str,
        description: str,
        tags: Sequence[str] | None = None,
        privacy_status: str = "public",
        category_id: str = "28",
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        max_retries: int = 5,
        retry_delay: float = 1.0,
        dry_run: bool = False,
        **kwargs: Any,
    ) -> YouTubeUploadResult:
        """Execute resumable chunked upload of a vertical video to YouTube Shorts."""
        vpath = Path(video_path).resolve()
        if not vpath.is_file():
            raise FileNotFoundError(f"Video file not found: {vpath}")

        if self.mock or dry_run:
            mock_id = f"mock_{uuid.uuid4().hex[:8]}"
            logger.info(f"[Dry Run] Skipped upload for {vpath}. Generated ID: {mock_id}")
            return YouTubeUploadResult(
                video_id=mock_id,
                video_url=f"https://youtube.com/shorts/{mock_id}",
                privacy_status=privacy_status,
            )

        formatted_title = format_shorts_title(title)
        formatted_tags = format_tags(tags)
        service = self.get_service()

        def _execute_upload_with_privacy(target_privacy: str) -> dict[str, Any]:
            body = {
                "snippet": {
                    "title": formatted_title,
                    "description": description,
                    "tags": formatted_tags,
                    "categoryId": str(category_id),
                },
                "status": {
                    "privacyStatus": target_privacy,
                    "selfDeclaredMadeForKids": False,
                },
            }

            media = MediaFileUpload(
                str(vpath),
                mimetype="video/mp4",
                resumable=True,
                chunksize=chunk_size,
            )

            insert_request = service.videos().insert(
                part="snippet,status",
                body=body,
                media_body=media,
            )

            return _perform_resumable_upload(
                insert_request=insert_request,
                max_retries=max_retries,
                retry_delay=retry_delay,
            )

        current_privacy = privacy_status
        try:
            response = _execute_upload_with_privacy(current_privacy)
        except HttpError as ex:
            if current_privacy == "public" and _is_privacy_or_unverified_error(ex):
                logger.warning(
                    f"Public upload rejected (unverified app restriction). "
                    f"Falling back to private upload: {ex}"
                )
                current_privacy = "private"
                response = _execute_upload_with_privacy("private")
            else:
                raise YouTubeUploadError(f"YouTube API upload failed: {ex}") from ex

        video_id = str(response.get("id", ""))
        actual_privacy = str(response.get("status", {}).get("privacyStatus", current_privacy))
        video_url = f"https://youtube.com/shorts/{video_id}"
        logger.info(f"Successfully published video {video_id} to YouTube Shorts ({video_url})")

        return YouTubeUploadResult(
            video_id=video_id,
            video_url=video_url,
            privacy_status=actual_privacy,
        )


def upload_video(
    video_path: str | Path,
    title: str,
    description: str,
    tags: Sequence[str] | None = None,
    privacy_status: str | None = None,
    category_id: str | None = None,
    client: YouTubeUploader | None = None,
    config: Any | None = None,
    dry_run: bool = False,
    **kwargs: Any,
) -> YouTubeUploadResult:
    """Publish video to YouTube Shorts using YouTubeUploader."""
    if client is None:
        cfg = config
        if cfg is None:
            try:
                from config import load_config

                cfg = load_config().youtube
            except (FileNotFoundError, ValueError, OSError):
                cfg = None

        secrets_file = getattr(cfg, "client_secrets_file", "credentials/client_secret.json")
        token_file = getattr(cfg, "token_file", "credentials/token.json")
        default_privacy = getattr(cfg, "privacy_status", "public")
        default_category = getattr(cfg, "category_id", "28")
        default_tags = getattr(cfg, "default_tags", None)

        uploader = YouTubeUploader(
            client_secrets_file=secrets_file,
            token_file=token_file,
            mock=dry_run,
        )
    else:
        uploader = client
        default_privacy = "public"
        default_category = "28"
        default_tags = None

    chosen_privacy = privacy_status if privacy_status is not None else default_privacy
    chosen_category = category_id if category_id is not None else default_category
    chosen_tags = tags if tags is not None else default_tags

    return uploader.upload_video(
        video_path=video_path,
        title=title,
        description=description,
        tags=chosen_tags,
        privacy_status=chosen_privacy,
        category_id=chosen_category,
        dry_run=dry_run,
        **kwargs,
    )
