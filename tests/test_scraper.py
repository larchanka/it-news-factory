from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# The module under test will be scraper
from scraper import (
    ScrapedArticle,
    ScrapedContent,
    capture_page_screenshot,
    download_and_validate_image,
    download_images,
    extract_og_images,
    scrape_article,
)


def test_scraped_content_data_model():
    """Verify ScrapedContent dataclass and ScrapedArticle alias."""
    content = ScrapedContent(
        url="https://example.com/news/1",
        title="Breaking Tech News",
        text="A new AI breakthrough was announced today.",
        image_paths=["/tmp/img1.jpg"],
        image_urls=["https://example.com/img1.jpg"],
    )
    assert content.url == "https://example.com/news/1"
    assert content.title == "Breaking Tech News"
    assert content.text == "A new AI breakthrough was announced today."
    assert content.image_paths == ["/tmp/img1.jpg"]
    assert content.image_urls == ["https://example.com/img1.jpg"]

    # ScrapedArticle must be an alias or subclass of ScrapedContent
    assert issubclass(ScrapedArticle, ScrapedContent) or ScrapedArticle is ScrapedContent


def test_extract_og_images_parses_meta_and_resolves_relative_urls():
    """Verify OpenGraph and Twitter image meta extraction and URL resolution."""
    sample_html = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta property="og:title" content="Quantum Leap in Computing" />
        <meta property="og:image" content="https://example.com/images/hero.jpg" />
        <meta name="twitter:image" content="/images/twitter_card.png" />
        <link rel="image_src" href="https://example.com/images/preview.webp" />
    </head>
    <body>
        <article>
            <p>Some text</p>
            <img src="/assets/diagram.jpg" alt="Architecture Diagram" />
        </article>
    </body>
    </html>
    """
    images = extract_og_images(sample_html, base_url="https://example.com/articles/news1")
    assert "https://example.com/images/hero.jpg" in images
    assert "https://example.com/images/twitter_card.png" in images
    assert "https://example.com/images/preview.webp" in images
    assert "https://example.com/assets/diagram.jpg" in images


def test_extract_og_images_filters_icons_and_svgs():
    """Verify filters reject low-res thumbnails, favicons, logos, and tracking pixels."""
    sample_html = """
    <html>
    <head>
        <link rel="icon" href="/favicon.ico" />
        <meta property="og:image" content="https://example.com/static/icon-small.png" />
        <meta property="og:image" content="https://example.com/pixel.gif" />
        <meta property="og:image" content="https://example.com/logo.svg" />
        <meta property="og:image" content="https://example.com/user_avatar_32x32.jpg" />
        <meta property="og:image" content="https://example.com/images/real_photo.jpg" />
    </head>
    <body>
        <img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=" />
    </body>
    </html>
    """
    images = extract_og_images(sample_html, base_url="https://example.com")
    assert images == ["https://example.com/images/real_photo.jpg"]


def test_download_and_validate_image_valid_formats(tmp_path):
    """Verify downloading and validating valid JPEG, PNG, and WebP images."""
    jpeg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 600
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 600
    webp_bytes = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 600

    dest_jpg = tmp_path / "img1.jpg"
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.read.return_value = jpeg_bytes
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        ok = download_and_validate_image("https://example.com/img1.jpg", dest_jpg)
        assert ok is True
        assert dest_jpg.is_file()
        assert dest_jpg.read_bytes() == jpeg_bytes

    dest_png = tmp_path / "img2.png"
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.read.return_value = png_bytes
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        ok = download_and_validate_image("https://example.com/img2.png", dest_png)
        assert ok is True
        assert dest_png.is_file()

    dest_webp = tmp_path / "img3.webp"
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.read.return_value = webp_bytes
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        ok = download_and_validate_image("https://example.com/img3.webp", dest_webp)
        assert ok is True
        assert dest_webp.is_file()


def test_download_and_validate_image_rejects_invalid(tmp_path):
    """Verify rejection of HTML error pages, empty files, or corrupt non-image bytes."""
    html_bytes = b"<!DOCTYPE html><html><body>404 Not Found</body></html>"
    empty_bytes = b""
    corrupt_bytes = b"Random binary gibberish that is definitely not an image format"

    dest = tmp_path / "invalid.jpg"

    for bad_content in (html_bytes, empty_bytes, corrupt_bytes):
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = bad_content
            mock_resp.__enter__.return_value = mock_resp
            mock_urlopen.return_value = mock_resp

            ok = download_and_validate_image("https://example.com/bad.jpg", dest)
            assert ok is False
            assert not dest.exists()


def test_download_images_batch(tmp_path):
    """Verify download_images saves multiple valid assets and ignores invalid ones."""
    jpeg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 600

    def fake_urlopen(req, timeout=10):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        mock_resp = MagicMock()
        if "good" in url:
            mock_resp.read.return_value = jpeg_bytes
        else:
            mock_resp.read.return_value = b"<html>404</html>"
        mock_resp.__enter__.return_value = mock_resp
        return mock_resp

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        urls = [
            "https://example.com/good1.jpg",
            "https://example.com/bad.jpg",
            "https://example.com/good2.jpg",
        ]
        saved_paths = download_images(urls, tmp_path, max_images=2)
        assert len(saved_paths) == 2
        for p in saved_paths:
            assert Path(p).is_file()


@pytest.mark.asyncio
async def test_capture_page_screenshot(tmp_path):
    """Verify capture_page_screenshot launches headless browser and configures 1080x1920 viewport."""
    screenshot_file = tmp_path / "screenshot.png"

    mock_playwright = MagicMock()
    mock_browser = AsyncMock()
    mock_context = AsyncMock()
    mock_page = AsyncMock()

    mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)
    mock_browser.new_context = AsyncMock(return_value=mock_context)
    mock_context.new_page = AsyncMock(return_value=mock_page)

    # When screenshot is called, create the mock file so the function returns verified path
    async def mock_screenshot(path, full_page=False):
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 500)

    mock_page.screenshot = AsyncMock(side_effect=mock_screenshot)

    with patch("scraper.async_playwright") as mock_pw_context:
        mock_pw_cm = MagicMock()
        mock_pw_cm.__aenter__ = AsyncMock(return_value=mock_playwright)
        mock_pw_cm.__aexit__ = AsyncMock(return_value=None)
        mock_pw_context.return_value = mock_pw_cm

        out_path = await capture_page_screenshot(
            "https://example.com/article",
            str(screenshot_file),
            viewport={"width": 1080, "height": 1920},
        )

        assert out_path == str(screenshot_file)
        assert screenshot_file.is_file()
        mock_browser.new_context.assert_called_once_with(
            viewport={"width": 1080, "height": 1920},
            user_agent=mock_browser.new_context.call_args[1].get("user_agent", None)
            or mock_browser.new_context.call_args[0][0]
            if mock_browser.new_context.call_args[0]
            else mock_browser.new_context.call_args[1].get("user_agent"),
        )
        mock_page.goto.assert_called_once()
        mock_page.screenshot.assert_called_once()
        mock_browser.close.assert_called_once()


def test_scrape_article_with_extracted_images(tmp_path):
    """Verify scrape_article extracts text and downloads images when available."""
    html_content = """
    <html>
    <head>
        <title>AI Revolution in 2026</title>
        <meta property="og:title" content="AI Revolution in 2026" />
        <meta property="og:image" content="https://example.com/ai_hero.jpg" />
    </head>
    <body>
        <h1>AI Revolution in 2026</h1>
        <p>Artificial intelligence continues to evolve at unprecedented speed.</p>
    </body>
    </html>
    """
    jpeg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 600

    with (
        patch("trafilatura.fetch_url", return_value=html_content),
        patch(
            "trafilatura.extract",
            return_value="Artificial intelligence continues to evolve at unprecedented speed.",
        ),
        patch("urllib.request.urlopen") as mock_urlopen,
    ):
        mock_resp = MagicMock()
        mock_resp.read.return_value = jpeg_bytes
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        article = scrape_article("https://example.com/ai-revolution", output_dir=tmp_path)

        assert article.url == "https://example.com/ai-revolution"
        assert article.title == "AI Revolution in 2026"
        assert "Artificial intelligence continues to evolve" in article.text
        assert len(article.image_paths) >= 1
        assert Path(article.image_paths[0]).exists()


def test_scrape_article_fallback_guarantees_visual_asset(tmp_path):
    """Verify scrape_article guarantees at least 1 visual asset by taking a screenshot if no images exist."""
    html_content = """
    <html>
    <head>
        <title>Text Only Article</title>
    </head>
    <body>
        <p>This article contains only pure text without any images.</p>
    </body>
    </html>
    """
    screenshot_file = tmp_path / "fallback_screenshot.png"
    screenshot_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 500)

    with (
        patch("trafilatura.fetch_url", return_value=html_content),
        patch(
            "trafilatura.extract",
            return_value="This article contains only pure text without any images.",
        ),
        patch(
            "scraper.capture_page_screenshot_sync",
            return_value=str(screenshot_file),
        ) as mock_screenshot,
    ):
        article = scrape_article("https://example.com/text-only", output_dir=tmp_path)

        assert article.url == "https://example.com/text-only"
        assert article.title == "Text Only Article"
        assert "This article contains only pure text" in article.text
        assert len(article.image_paths) == 1
        assert article.image_paths[0] == str(screenshot_file)
        mock_screenshot.assert_called_once()


@pytest.mark.asyncio
async def test_async_scrape_article(tmp_path):
    """Verify async_scrape_article executes cleanly in async runtime."""
    html_content = """
    <html>
    <head>
        <title>Async Scraper Test</title>
        <meta property="og:image" content="https://example.com/pic.jpg" />
    </head>
    <body>
        <p>Async scraping works flawlessly.</p>
    </body>
    </html>
    """
    jpeg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 600

    from scraper import async_scrape_article

    with (
        patch("trafilatura.fetch_url", return_value=html_content),
        patch("trafilatura.extract", return_value="Async scraping works flawlessly."),
        patch("urllib.request.urlopen") as mock_urlopen,
    ):
        mock_resp = MagicMock()
        mock_resp.read.return_value = jpeg_bytes
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        article = await async_scrape_article(
            "https://example.com/async-test",
            output_dir=tmp_path,
        )

        assert article.title == "Async Scraper Test"
        assert article.text == "Async scraping works flawlessly."
        assert len(article.image_paths) == 1


def test_scrape_article_network_failure_fallback_to_screenshot(tmp_path):
    """Verify scrape_article recovers when HTML fetch fails entirely."""
    screenshot_file = tmp_path / "fallback_screenshot.png"
    screenshot_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 500)

    with (
        patch("trafilatura.fetch_url", return_value=None),
        patch("urllib.request.urlopen", side_effect=Exception("Connection timed out")),
        patch(
            "scraper.capture_page_screenshot_sync",
            return_value=str(screenshot_file),
        ) as mock_screenshot,
    ):
        article = scrape_article(
            "https://example.com/network-failure-slug",
            output_dir=tmp_path,
        )

        assert article.url == "https://example.com/network-failure-slug"
        assert article.title == "Network Failure Slug"
        assert article.text == ""
        assert len(article.image_paths) == 1
        mock_screenshot.assert_called_once()


@pytest.mark.asyncio
async def test_capture_page_screenshot_sync_with_running_loop(tmp_path):
    """Verify capture_page_screenshot_sync delegates to thread pool if event loop is running."""
    from scraper import capture_page_screenshot_sync

    dest = tmp_path / "sync_in_async.png"

    with patch("scraper.capture_page_screenshot", new_callable=AsyncMock) as mock_async_capture:
        mock_async_capture.return_value = str(dest)
        result = capture_page_screenshot_sync("https://example.com", dest)
        assert result == str(dest)


def test_is_valid_image_bytes():
    """Verify image bytes validation for various formats, lengths, and edge cases."""
    from scraper import is_valid_image_bytes

    # Under minimum size (< 512 bytes)
    assert is_valid_image_bytes(b"\xff\xd8\xff" + b"\x00" * 100) is False
    assert is_valid_image_bytes(b"") is False

    # Valid sizes
    assert is_valid_image_bytes(b"\xff\xd8\xff" + b"\x00" * 600) is True
    assert is_valid_image_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 600) is True
    assert is_valid_image_bytes(b"GIF87a" + b"\x00" * 600) is True
    assert is_valid_image_bytes(b"GIF89a" + b"\x00" * 600) is True
    assert is_valid_image_bytes(b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 600) is True

    # Invalid header of sufficient length
    assert is_valid_image_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 600) is False
    assert is_valid_image_bytes(b"HELLO WORLD" + b"\x00" * 600) is False


def test_meta_image_relative_resolution_and_filtering():
    """Verify trafilatura metadata image resolves relative URLs and respects icon filtering."""
    from scraper import _extract_article_data

    # 1. Valid relative image in metadata resolves to full URL
    mock_meta_valid = MagicMock()
    mock_meta_valid.title = "Valid Story"
    mock_meta_valid.image = "/assets/cover_photo.jpg"

    with (
        patch("trafilatura.extract_metadata", return_value=mock_meta_valid),
        patch("trafilatura.extract", return_value="Some story text"),
    ):
        title, _, candidate_urls = _extract_article_data(
            "<html></html>",
            "https://example.com/news/article",
        )
        assert title == "Valid Story"
        assert "https://example.com/assets/cover_photo.jpg" in candidate_urls

    # 2. Icon / favicon in metadata is filtered out
    mock_meta_icon = MagicMock()
    mock_meta_icon.title = "Icon Story"
    mock_meta_icon.image = "/static/favicon.ico"

    with (
        patch("trafilatura.extract_metadata", return_value=mock_meta_icon),
        patch("trafilatura.extract", return_value="Some story text"),
    ):
        _, _, candidate_urls = _extract_article_data(
            "<html></html>",
            "https://example.com/news/article",
        )
        assert "https://example.com/static/favicon.ico" not in candidate_urls
        assert len(candidate_urls) == 0


def test_malformed_url_and_unsupported_scheme_handling(tmp_path):
    """Verify malformed and unsupported schemes return safely without unhandled exceptions."""
    from scraper import _fetch_html, download_and_validate_image

    # Unsupported schemes should gracefully return False / empty string
    for bad_url in (
        "ftp://files.example.com/img.jpg",
        "javascript:void(0)",
        "file:///etc/passwd",
        "",
    ):
        assert download_and_validate_image(bad_url, tmp_path / "test.jpg") is False
        assert _fetch_html(bad_url, user_agent="TestUA") == ""


def test_extract_title_unescapes_html_entities():
    """Verify _extract_title unescapes HTML entities in title tag and og:title."""
    from scraper import _extract_title

    html_entities = """
    <html>
    <head>
        <meta property="og:title" content="AT&amp;T &lt;AI&gt; Breakthrough &quot;2026&quot;" />
    </head>
    </html>
    """
    with patch("trafilatura.extract_metadata", return_value=None):
        title = _extract_title(html_entities, "https://example.com")
        assert title == 'AT&T <AI> Breakthrough "2026"'
