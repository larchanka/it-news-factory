from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock

import httplib2
import pytest
from googleapiclient.errors import HttpError

from youtube_uploader import (
    YouTubeAuthenticationError,
    YouTubeError,
    YouTubeQuotaExceededError,
    YouTubeUploader,
    YouTubeUploadError,
    YouTubeUploadResult,
    format_shorts_title,
    format_tags,
    upload_video,
)


class TestFormatting:
    def test_format_shorts_title_appends_shorts_tag(self) -> None:
        title = "OpenAI Releases Revolutionary Model"
        formatted = format_shorts_title(title)
        assert formatted == "OpenAI Releases Revolutionary Model #Shorts"

    def test_format_shorts_title_preserves_existing_shorts_tag(self) -> None:
        title1 = "OpenAI Releases Model #Shorts"
        assert format_shorts_title(title1) == "OpenAI Releases Model #Shorts"

        title2 = "OpenAI Releases Model #shorts"
        assert format_shorts_title(title2) == "OpenAI Releases Model #shorts"

    def test_format_shorts_title_bounds_to_100_characters(self) -> None:
        long_title = "A" * 120
        formatted = format_shorts_title(long_title)
        assert len(formatted) <= 100
        assert formatted.endswith("#Shorts")

    def test_format_tags_ensures_shorts_and_deduplicates(self) -> None:
        tags = format_tags(["AI", "Python", "ai", "Shorts"])
        assert "Shorts" in tags
        assert "AI" in tags
        assert "Python" in tags
        # Deduplication check (case-insensitive deduplication)
        assert len([t for t in tags if t.lower() == "ai"]) == 1

    def test_format_tags_default_fallback(self) -> None:
        tags = format_tags(None)
        assert "Shorts" in tags
        assert "TechNews" in tags


class TestUploadResult:
    def test_result_fields(self) -> None:
        res = YouTubeUploadResult(
            video_id="abc123xyz",
            video_url="https://youtube.com/shorts/abc123xyz",
            privacy_status="public",
        )
        assert res.video_id == "abc123xyz"
        assert res.video_url == "https://youtube.com/shorts/abc123xyz"
        assert res.privacy_status == "public"


class TestExceptions:
    def test_exception_inheritance(self) -> None:
        assert issubclass(YouTubeAuthenticationError, YouTubeError)
        assert issubclass(YouTubeQuotaExceededError, YouTubeError)
        assert issubclass(YouTubeUploadError, YouTubeError)


class TestCredentialsFlow:
    def test_loads_cached_valid_credentials(self, tmp_path: Path) -> None:
        token_path = tmp_path / "token.json"
        token_path.write_text('{"token": "fake_token", "refresh_token": "fake_refresh"}')

        mock_creds = Mock()
        mock_creds.valid = True
        mock_creds.expired = False

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "google.oauth2.credentials.Credentials.from_authorized_user_file",
                lambda path, scopes: mock_creds,
            )
            uploader = YouTubeUploader(token_file=str(token_path))
            creds = uploader.get_credentials()
            assert creds == mock_creds

    def test_refreshes_expired_cached_credentials(self, tmp_path: Path) -> None:
        token_path = tmp_path / "token.json"
        token_path.write_text('{"token": "old_token", "refresh_token": "fake_refresh"}')

        mock_creds = Mock()
        mock_creds.valid = False
        mock_creds.expired = True
        mock_creds.refresh_token = "fake_refresh"
        mock_creds.to_json.return_value = '{"token": "new_token", "refresh_token": "fake_refresh"}'

        def do_refresh(request: Any) -> None:
            mock_creds.valid = True
            mock_creds.expired = False

        mock_creds.refresh.side_effect = do_refresh

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "google.oauth2.credentials.Credentials.from_authorized_user_file",
                lambda path, scopes: mock_creds,
            )
            uploader = YouTubeUploader(token_file=str(token_path))
            creds = uploader.get_credentials()
            mock_creds.refresh.assert_called_once()
            assert creds == mock_creds
            assert "new_token" in token_path.read_text()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "google.oauth2.credentials.Credentials.from_authorized_user_file",
                lambda path, scopes: mock_creds,
            )
            uploader = YouTubeUploader(token_file=str(token_path))
            creds = uploader.get_credentials()
            mock_creds.refresh.assert_called_once()
            assert creds == mock_creds
            assert "new_token" in token_path.read_text()

    def test_missing_credentials_raises_authentication_error(self, tmp_path: Path) -> None:
        uploader = YouTubeUploader(
            client_secrets_file=str(tmp_path / "nonexistent_secret.json"),
            token_file=str(tmp_path / "nonexistent_token.json"),
        )
        with pytest.raises(YouTubeAuthenticationError, match="Missing OAuth credentials"):
            uploader.get_credentials()

    def test_authenticate_via_flow_success(self, tmp_path: Path) -> None:
        secrets_file = tmp_path / "client_secret.json"
        secrets_file.write_text('{"installed": {"client_id": "fake"}}')
        token_file = tmp_path / "token.json"

        mock_creds = Mock()
        mock_creds.valid = True
        mock_creds.to_json.return_value = '{"token": "flow_token"}'

        mock_flow = Mock()
        mock_flow.run_local_server.return_value = mock_creds

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file",
                lambda path, scopes: mock_flow,
            )
            uploader = YouTubeUploader(client_secrets_file=secrets_file, token_file=token_file)
            creds = uploader.get_credentials()
            assert creds == mock_creds
            assert "flow_token" in token_file.read_text()

    def test_authenticate_via_flow_failure(self, tmp_path: Path) -> None:
        secrets_file = tmp_path / "client_secret.json"
        secrets_file.write_text('{"installed": {"client_id": "fake"}}')
        token_file = tmp_path / "token.json"

        mock_flow = Mock()
        mock_flow.run_local_server.side_effect = RuntimeError("Port already in use")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file",
                lambda path, scopes: mock_flow,
            )
            uploader = YouTubeUploader(client_secrets_file=secrets_file, token_file=token_file)
            with pytest.raises(
                YouTubeAuthenticationError, match="OAuth authentication flow failed"
            ):
                uploader.get_credentials()


class TestUploadFlow:
    @pytest.fixture
    def sample_video(self, tmp_path: Path) -> Path:
        vpath = tmp_path / "short.mp4"
        vpath.write_bytes(b"dummy mp4 data")
        return vpath

    def test_upload_resumable_success(self, sample_video: Path) -> None:
        mock_service = MagicMock()
        mock_videos = MagicMock()
        mock_service.videos.return_value = mock_videos
        mock_insert = MagicMock()
        mock_videos.insert.return_value = mock_insert

        # Simulate 2 chunks
        mock_status = Mock()
        mock_status.progress.return_value = 0.5
        mock_insert.next_chunk.side_effect = [
            (mock_status, None),
            (None, {"id": "vid123", "status": {"privacyStatus": "public"}}),
        ]

        uploader = YouTubeUploader(credentials=Mock())
        uploader._service = mock_service

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("youtube_uploader.MediaFileUpload", MagicMock())
            res = uploader.upload_video(
                video_path=sample_video,
                title="Super New AI Feature",
                description="Check this out: https://example.com/article",
                tags=["AI", "Tech"],
                privacy_status="public",
                category_id="28",
            )

        assert res.video_id == "vid123"
        assert res.video_url == "https://youtube.com/shorts/vid123"
        assert res.privacy_status == "public"

        # Check call arguments
        mock_videos.insert.assert_called_once()
        call_kwargs = mock_videos.insert.call_args[1]
        assert call_kwargs["part"] == "snippet,status"
        snippet = call_kwargs["body"]["snippet"]
        assert snippet["title"] == "Super New AI Feature #Shorts"
        assert snippet["description"] == "Check this out: https://example.com/article"
        assert snippet["categoryId"] == "28"
        assert "AI" in snippet["tags"]
        assert "Shorts" in snippet["tags"]
        assert call_kwargs["body"]["status"]["privacyStatus"] == "public"
        assert call_kwargs["body"]["status"]["selfDeclaredMadeForKids"] is False

    def test_upload_transient_network_error_retries_and_succeeds(self, sample_video: Path) -> None:
        mock_service = MagicMock()
        mock_videos = MagicMock()
        mock_service.videos.return_value = mock_videos
        mock_insert = MagicMock()
        mock_videos.insert.return_value = mock_insert

        mock_insert.next_chunk.side_effect = [
            OSError("Connection reset by peer"),
            (None, {"id": "vid456", "status": {"privacyStatus": "public"}}),
        ]

        uploader = YouTubeUploader(credentials=Mock())
        uploader._service = mock_service

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("youtube_uploader.MediaFileUpload", MagicMock())
            mp.setattr("time.sleep", lambda s: None)
            res = uploader.upload_video(
                video_path=sample_video,
                title="Resilient Upload",
                description="Retrying test",
                retry_delay=0.01,
            )

        assert res.video_id == "vid456"
        assert mock_insert.next_chunk.call_count == 2

    def test_upload_http_503_retry_and_succeeds(self, sample_video: Path) -> None:
        mock_service = MagicMock()
        mock_videos = MagicMock()
        mock_service.videos.return_value = mock_videos
        mock_insert = MagicMock()
        mock_videos.insert.return_value = mock_insert

        resp_503 = httplib2.Response({"status": 503, "reason": "Service Unavailable"})
        mock_insert.next_chunk.side_effect = [
            HttpError(resp_503, b"Service Unavailable"),
            (None, {"id": "vid503", "status": {"privacyStatus": "public"}}),
        ]

        uploader = YouTubeUploader(credentials=Mock())
        uploader._service = mock_service

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("youtube_uploader.MediaFileUpload", MagicMock())
            mp.setattr("time.sleep", lambda s: None)
            res = uploader.upload_video(
                video_path=sample_video,
                title="503 Retry",
                description="desc",
            )
        assert res.video_id == "vid503"

    def test_upload_network_error_exhaustion_raises_youtube_upload_error(
        self, sample_video: Path
    ) -> None:
        mock_service = MagicMock()
        mock_videos = MagicMock()
        mock_service.videos.return_value = mock_videos
        mock_insert = MagicMock()
        mock_videos.insert.return_value = mock_insert

        mock_insert.next_chunk.side_effect = OSError("Persistent connection failure")

        uploader = YouTubeUploader(credentials=Mock())
        uploader._service = mock_service

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("youtube_uploader.MediaFileUpload", MagicMock())
            mp.setattr("time.sleep", lambda s: None)
            with pytest.raises(YouTubeUploadError, match="Upload failed after 2 retries"):
                uploader.upload_video(
                    video_path=sample_video,
                    title="Exhaustion",
                    description="desc",
                    max_retries=2,
                )

    def test_upload_non_retryable_http_error_raises_youtube_upload_error(
        self, sample_video: Path
    ) -> None:
        mock_service = MagicMock()
        mock_videos = MagicMock()
        mock_service.videos.return_value = mock_videos
        mock_insert = MagicMock()
        mock_videos.insert.return_value = mock_insert

        resp_404 = httplib2.Response({"status": 404, "reason": "Not Found"})
        mock_insert.next_chunk.side_effect = HttpError(resp_404, b"Not Found")

        uploader = YouTubeUploader(credentials=Mock())
        uploader._service = mock_service

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("youtube_uploader.MediaFileUpload", MagicMock())
            with pytest.raises(YouTubeUploadError, match="YouTube API upload failed"):
                uploader.upload_video(
                    video_path=sample_video,
                    title="404 Error",
                    description="desc",
                )

    def test_get_service_initialization(self) -> None:
        mock_creds = Mock()
        uploader = YouTubeUploader(credentials=mock_creds)
        with pytest.MonkeyPatch.context() as mp:
            mock_build = MagicMock()
            mp.setattr("youtube_uploader.build", mock_build)
            service = uploader.get_service()
            assert service == mock_build.return_value
            mock_build.assert_called_once_with("youtube", "v3", credentials=mock_creds)
            # Second call should return cached service
            assert uploader.get_service() == service
            assert mock_build.call_count == 1

    def test_upload_quota_exceeded_raises_youtube_quota_exceeded_error(
        self, sample_video: Path
    ) -> None:
        mock_service = MagicMock()
        mock_videos = MagicMock()
        mock_service.videos.return_value = mock_videos
        mock_insert = MagicMock()
        mock_videos.insert.return_value = mock_insert

        resp = httplib2.Response({"status": 403, "reason": "Forbidden"})
        content = b'{"error": {"errors": [{"reason": "quotaExceeded"}], "message": "The request cannot be completed because you have exceeded your quota."}}'
        mock_insert.next_chunk.side_effect = HttpError(resp, content)

        uploader = YouTubeUploader(credentials=Mock())
        uploader._service = mock_service

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("youtube_uploader.MediaFileUpload", MagicMock())
            with pytest.raises(YouTubeQuotaExceededError, match="quota exceeded"):
                uploader.upload_video(
                    video_path=sample_video,
                    title="Quota Test",
                    description="Testing quota limits",
                )

        # File must remain intact on disk
        assert sample_video.is_file()

    def test_upload_unverified_app_public_fallback_to_private(self, sample_video: Path) -> None:
        mock_service = MagicMock()
        mock_videos = MagicMock()
        mock_service.videos.return_value = mock_videos

        resp_400 = httplib2.Response({"status": 400, "reason": "Bad Request"})
        content_400 = b'{"error": {"errors": [{"reason": "uploadNotPermitted"}], "message": "The user has not verified their account for public uploads."}}'

        mock_insert_first = MagicMock()
        mock_insert_first.next_chunk.side_effect = HttpError(resp_400, content_400)

        mock_insert_second = MagicMock()
        mock_insert_second.next_chunk.return_value = (
            None,
            {"id": "fallback_id", "status": {"privacyStatus": "private"}},
        )

        mock_videos.insert.side_effect = [mock_insert_first, mock_insert_second]

        uploader = YouTubeUploader(credentials=Mock())
        uploader._service = mock_service

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("youtube_uploader.MediaFileUpload", MagicMock())
            res = uploader.upload_video(
                video_path=sample_video,
                title="Unverified App Test",
                description="Testing privacy fallback",
                privacy_status="public",
            )

        assert res.video_id == "fallback_id"
        assert res.privacy_status == "private"
        assert mock_videos.insert.call_count == 2
        # Verify second call used private status
        second_call_body = mock_videos.insert.call_args_list[1][1]["body"]
        assert second_call_body["status"]["privacyStatus"] == "private"

    def test_upload_video_file_not_found(self) -> None:
        uploader = YouTubeUploader(credentials=Mock())
        with pytest.raises(FileNotFoundError, match="Video file not found"):
            uploader.upload_video(
                video_path="/nonexistent/short.mp4",
                title="Missing Video",
                description="desc",
            )

    def test_upload_dry_run_mode(self, sample_video: Path) -> None:
        uploader = YouTubeUploader(mock=True)
        res = uploader.upload_video(
            video_path=sample_video,
            title="Dry Run Test",
            description="No API calls",
            dry_run=True,
        )
        assert res.video_id.startswith("mock_")
        assert "youtube.com/shorts/" in res.video_url
        assert res.privacy_status == "public"

    def test_upload_video_convenience_function(self, sample_video: Path) -> None:
        mock_uploader = MagicMock()
        mock_uploader.upload_video.return_value = YouTubeUploadResult(
            video_id="conv123",
            video_url="https://youtube.com/shorts/conv123",
            privacy_status="public",
        )
        res = upload_video(
            video_path=sample_video,
            title="Convenience Function",
            description="desc",
            client=mock_uploader,
        )
        assert res.video_id == "conv123"
        mock_uploader.upload_video.assert_called_once()
