from __future__ import annotations

import asyncio
import hashlib
import html
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import trafilatura
from playwright.async_api import ViewportSize, async_playwright

from config import ScraperConfig, load_config

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

# Minimum bytes for a valid content image (filters out 1x1 tracking pixels)
MIN_IMAGE_BYTES = 512

# Common image magic byte headers
IMAGE_SIGNATURES: list[tuple[bytes, str]] = [
    (b"\xff\xd8\xff", "jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
]

# Patterns for images that should be excluded (icons, avatars, tracking pixels)
DISALLOWED_IMAGE_PATTERNS = re.compile(
    r"(favicon|\.ico|\.svg|pixel|tracker|tracking|avatar|badge|button|logo|icon|1x1|spacer)",
    re.IGNORECASE,
)


@dataclass
class ScrapedContent:
    """Extracted text, metadata, and local media assets for an article."""

    url: str
    title: str
    text: str
    image_paths: list[str] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)


# Alias for backward compatibility / specification naming
ScrapedArticle = ScrapedContent


def is_valid_image_bytes(data: bytes) -> bool:
    """Validate that raw bytes represent a supported, non-empty image format."""
    if len(data) < MIN_IMAGE_BYTES:
        return False

    # Check WebP signature (RIFF .... WEBP)
    if data.startswith(b"RIFF") and len(data) >= 16 and b"WEBP" in data[:16]:
        return True

    # Check standard headers
    for magic, _ in IMAGE_SIGNATURES:
        if data.startswith(magic):
            return True

    return False


def _is_allowed_image_candidate(resolved_url: str) -> bool:
    """Check if candidate URL has valid scheme and does not match disallowed icon patterns."""
    parsed = urllib.parse.urlparse(resolved_url)
    if parsed.scheme not in ("http", "https"):
        return False
    return not bool(DISALLOWED_IMAGE_PATTERNS.search(parsed.path))


def _collect_raw_image_candidates(html: str) -> list[str]:
    """Collect image URL candidates from meta tags and body img tags."""
    found: list[str] = []

    # 1. Meta tags: og:image, twitter:image, image_src, itemprop=image
    meta_pattern = re.compile(
        r"""<meta\s+[^>]*(?:property|name)=["'](?:og:image|og:image:url|twitter:image|twitter:image:src)["'][^>]*content=["']([^"']+)["']"""
        r"""|<meta\s+[^>]*content=["']([^"']+)["'][^>]*(?:property|name)=["'](?:og:image|og:image:url|twitter:image|twitter:image:src)["']"""
        r"""|<link\s+[^>]*rel=["']image_src["'][^>]*href=["']([^"']+)["']""",
        re.IGNORECASE,
    )
    for match in meta_pattern.finditer(html):
        for candidate in match.groups():
            if candidate:
                found.append(candidate.strip())

    # 2. Extract <img> tags inside <article> or general content if present
    img_pattern = re.compile(r"""<img\s+[^>]*src=["']([^"']+)["']""", re.IGNORECASE)
    article_block = re.search(r"<article[^>]*>([\s\S]*?)</article>", html, re.IGNORECASE)
    search_html = article_block.group(1) if article_block else html
    for match in img_pattern.finditer(search_html):
        found.append(match.group(1).strip())

    return found


def extract_og_images(html: str, base_url: str = "") -> list[str]:
    """
    Extract OpenGraph, Twitter, and high-resolution article image URLs from HTML.
    Resolves relative URLs against base_url and filters out low-res thumbnails, icons, and svgs.
    """
    if not html:
        return []

    filtered_urls: list[str] = []
    seen: set[str] = set()

    for raw_url in _collect_raw_image_candidates(html):
        if not raw_url or raw_url.startswith("data:"):
            continue

        resolved = urllib.parse.urljoin(base_url, raw_url)
        if resolved not in seen and _is_allowed_image_candidate(resolved):
            seen.add(resolved)
            filtered_urls.append(resolved)

    return filtered_urls


def download_and_validate_image(
    url: str,
    output_path: Path | str,
    timeout: int = 10,
    user_agent: str = DEFAULT_USER_AGENT,
) -> bool:
    """
    Download an image URL, validate its magic bytes and minimum size, and persist to output_path.
    Cleans up partial or invalid files on failure.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False

    dest = Path(output_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": user_agent},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()

        if not is_valid_image_bytes(data):
            if dest.exists():
                dest.unlink(missing_ok=True)
            return False

        dest.write_bytes(data)
        return True
    except Exception:  # noqa: BLE001
        if dest.exists():
            dest.unlink(missing_ok=True)
        return False


def download_images(
    image_urls: list[str],
    output_dir: Path | str,
    max_images: int = 5,
    user_agent: str = DEFAULT_USER_AGENT,
) -> list[str]:
    """Download and validate candidate images up to max_images, returning local file paths."""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    saved_paths: list[str] = []
    for idx, img_url in enumerate(image_urls):
        if len(saved_paths) >= max_images:
            break

        ext = Path(urllib.parse.urlparse(img_url).path).suffix.lower()
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            ext = ".jpg"

        file_hash = hashlib.sha256(img_url.encode("utf-8")).hexdigest()[:10]
        dest_file = out_dir / f"image_{idx:02d}_{file_hash}{ext}"

        if download_and_validate_image(img_url, dest_file, user_agent=user_agent):
            saved_paths.append(str(dest_file))

    return saved_paths


async def capture_page_screenshot(
    url: str,
    output_path: str | Path,
    viewport: ViewportSize | None = None,
    timeout_ms: int = 15000,
    user_agent: str | None = None,
) -> str:
    """
    Headless Playwright Chromium capture producing a vertical (1080x1920) screenshot
    as a guaranteed fallback visual asset.
    """
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    vp: ViewportSize = viewport or {"width": 1080, "height": 1920}
    ua = user_agent or DEFAULT_USER_AGENT

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport=vp, user_agent=ua)
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            await page.screenshot(path=str(out_path), full_page=False)
        finally:
            await browser.close()

    return str(out_path)


def capture_page_screenshot_sync(
    url: str,
    output_path: str | Path,
    viewport: ViewportSize | None = None,
    timeout_ms: int = 15000,
    user_agent: str | None = None,
) -> str:
    """Synchronous wrapper for capture_page_screenshot handling both running and idle loops."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                asyncio.run,
                capture_page_screenshot(
                    url=url,
                    output_path=output_path,
                    viewport=viewport,
                    timeout_ms=timeout_ms,
                    user_agent=user_agent,
                ),
            )
            return future.result()
    else:
        return asyncio.run(
            capture_page_screenshot(
                url=url,
                output_path=output_path,
                viewport=viewport,
                timeout_ms=timeout_ms,
                user_agent=user_agent,
            )
        )


def _extract_title(html_str: str, url: str) -> str:
    """Extract page title from metadata, title tags, or domain fallback."""
    if html_str:
        meta = trafilatura.extract_metadata(html_str)
        if meta and meta.title:
            return html.unescape(meta.title.strip())

        og_title = re.search(
            r"""<meta\s+[^>]*(?:property|name)=["'](?:og:title|twitter:title)["'][^>]*content=["']([^"']+)["']""",
            html_str,
            re.IGNORECASE,
        )
        if og_title:
            return html.unescape(og_title.group(1).strip())

        title_tag = re.search(r"<title[^>]*>([^<]+)</title>", html_str, re.IGNORECASE)
        if title_tag:
            return html.unescape(title_tag.group(1).strip())

    # Fallback to URL path or domain
    parsed = urllib.parse.urlparse(url)
    slug = parsed.path.rstrip("/").split("/")[-1]
    return slug.replace("-", " ").replace("_", " ").title() if slug else parsed.netloc


def _fetch_html(url: str, user_agent: str) -> str:
    """Fetch HTML content using trafilatura or urllib fallback."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return ""

    fetched = trafilatura.fetch_url(url)
    if fetched:
        return fetched

    try:
        req = urllib.request.Request(url, headers={"User-Agent": user_agent})
        with urllib.request.urlopen(req, timeout=10) as resp:
            content: bytes = resp.read()
            return content.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def _extract_article_data(html_str: str, url: str) -> tuple[str, str, list[str]]:
    """Extract (title, text, candidate_image_urls) from HTML and URL."""
    title = _extract_title(html_str, url)
    extracted_text = ""
    if html_str:
        extracted = trafilatura.extract(
            html_str,
            include_images=False,
            include_links=False,
            output_format="txt",
        )
        if extracted:
            extracted_text = extracted.strip()

    candidate_urls = extract_og_images(html_str, base_url=url)
    if html_str:
        meta = trafilatura.extract_metadata(html_str)
        if meta and meta.image:
            resolved_meta = urllib.parse.urljoin(url, meta.image)
            if resolved_meta not in candidate_urls and _is_allowed_image_candidate(resolved_meta):
                candidate_urls.insert(0, resolved_meta)

    return title, extracted_text, candidate_urls


def scrape_article(
    url: str,
    output_dir: str | Path | None = None,
    config: ScraperConfig | None = None,
) -> ScrapedContent:
    """
    Main synchronous entry point to extract readable article text, title, and media assets.
    Guarantees at least 1 visual asset by capturing a vertical Playwright screenshot if no images exist.
    """
    cfg = config or load_config().scraper
    user_agent = cfg.user_agent
    timeout_ms = cfg.screenshot_timeout_ms

    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    out_dir = Path(output_dir) if output_dir else Path("temp") / f"scrape_{url_hash}"
    out_dir.mkdir(parents=True, exist_ok=True)

    html = _fetch_html(url, user_agent)
    title, extracted_text, candidate_urls = _extract_article_data(html, url)

    # Download and validate images
    downloaded_paths = download_images(
        candidate_urls,
        output_dir=out_dir,
        max_images=5,
        user_agent=user_agent,
    )

    # Guarantee at least 1 visual asset
    if not downloaded_paths:
        screenshot_path = out_dir / "page_screenshot.png"
        captured = capture_page_screenshot_sync(
            url=url,
            output_path=screenshot_path,
            viewport={"width": 1080, "height": 1920},
            timeout_ms=timeout_ms,
            user_agent=user_agent,
        )
        downloaded_paths.append(captured)

    return ScrapedContent(
        url=url,
        title=title,
        text=extracted_text,
        image_paths=downloaded_paths,
        image_urls=candidate_urls,
    )


async def async_scrape_article(
    url: str,
    output_dir: str | Path | None = None,
    config: ScraperConfig | None = None,
) -> ScrapedContent:
    """Async variant of scrape_article for direct integration with async pipelines."""
    cfg = config or load_config().scraper
    user_agent = cfg.user_agent
    timeout_ms = cfg.screenshot_timeout_ms

    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    out_dir = Path(output_dir) if output_dir else Path("temp") / f"scrape_{url_hash}"
    out_dir.mkdir(parents=True, exist_ok=True)

    loop = asyncio.get_running_loop()
    html = await loop.run_in_executor(None, _fetch_html, url, user_agent)
    title, extracted_text, candidate_urls = _extract_article_data(html, url)

    downloaded_paths = await loop.run_in_executor(
        None, download_images, candidate_urls, out_dir, 5, user_agent
    )

    if not downloaded_paths:
        screenshot_path = out_dir / "page_screenshot.png"
        captured = await capture_page_screenshot(
            url=url,
            output_path=screenshot_path,
            viewport={"width": 1080, "height": 1920},
            timeout_ms=timeout_ms,
            user_agent=user_agent,
        )
        downloaded_paths.append(captured)

    return ScrapedContent(
        url=url,
        title=title,
        text=extracted_text,
        image_paths=downloaded_paths,
        image_urls=candidate_urls,
    )
