"""
media_downloader.py — Download photos, videos, and story screenshots.

File path convention:
    data/media/{platform}/{target_account}/{content_group}/{YYYY-MM-DD}/{item_id}/{filename}

Filenames:
    Single photo:   photo.jpg
    Carousel:       photo_01.jpg, photo_02.jpg, ...
    Video / Reel:   video.mp4
    Multi-video:    video_01.mp4, video_02.mp4, ...
    Story image:    story_{story_id}.jpg
    Story video:    story_{story_id}.mp4
    Thumbnail:      thumb.jpg / video_01_thumb.jpg
"""

from __future__ import annotations

import hashlib
import json
import re
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import requests
from loguru import logger
from playwright.async_api import Page
from requests.adapters import HTTPAdapter
from urllib3.util import connection as urllib3_connection
from urllib3.util.retry import Retry

from src.logging_utils import compact_url, log_event

if TYPE_CHECKING:
    from src.config import MediaConfig


_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_DIRECT_VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".mkv", ".webm"}
_DIRECT_VIDEO_HOST_HINTS = (
    "cdninstagram.com",
    "fbcdn.net",
    "fbsbx.com",
    "akamaihd.net",
    "tiktokcdn",
    "byteoversea.com",
    "ibytedtos.com",
    "muscdn.com",
)
_SOCIAL_WEB_PAGE_HOSTS = {
    "instagram.com",
    "www.instagram.com",
    "m.instagram.com",
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "tiktok.com",
    "www.tiktok.com",
    "m.tiktok.com",
}

_VIDEO_CONTENT_TYPE_EXTENSIONS = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
}

_HTTP_RETRY = Retry(
    total=2,
    connect=2,
    read=2,
    status=2,
    backoff_factor=0.8,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset({"GET", "HEAD"}),
    respect_retry_after_header=True,
)
_IPV4_REQUEST_LOCK = Lock()


def _build_request_headers(referer: str | None = None) -> dict[str, str]:
    """Build consistent browser-like headers for direct media downloads."""
    headers = {"User-Agent": _DEFAULT_USER_AGENT}
    if referer:
        headers["Referer"] = referer
    return headers


def _load_cookies_from_storage_state(session_path: Path | None) -> requests.cookies.RequestsCookieJar | None:
    """Return a requests cookie jar loaded from a Playwright storage-state file."""
    if session_path is None or not session_path.exists():
        return None

    try:
        state = json.loads(session_path.read_text())
    except Exception:
        return None

    jar = requests.cookies.RequestsCookieJar()
    for cookie in state.get("cookies", []):
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "")
        if not name or not value:
            continue
        domain = str(cookie.get("domain") or "").strip()
        path = str(cookie.get("path") or "/").strip() or "/"
        if domain:
            jar.set(name, value, domain=domain, path=path)
        else:
            jar.set(name, value, path=path)

    return jar if jar else None


def _build_retry_session() -> requests.Session:
    """Return a requests session with conservative retry behavior."""
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=_HTTP_RETRY, pool_connections=16, pool_maxsize=16)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _is_route_error(exc: Exception) -> bool:
    """Return True when the exception looks like a transient routing/address-family issue."""
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "no route to host",
            "[errno 65]",
            "network is unreachable",
            "address family not supported",
        )
    )


def _streaming_get(
    url: str,
    *,
    referer: str | None = None,
    timeout: int = 60,
    cookies: requests.cookies.RequestsCookieJar | None = None,
) -> tuple[requests.Response, requests.Session]:
    """Open a streaming GET response with retry and IPv4 fallback for flaky CDN routes."""
    last_error: Exception | None = None
    for force_ipv4 in (False, True):
        session = _build_retry_session()
        try:
            if force_ipv4:
                with _IPV4_REQUEST_LOCK:
                    original = urllib3_connection.allowed_gai_family
                    urllib3_connection.allowed_gai_family = lambda: socket.AF_INET
                    try:
                        response = session.get(
                            url,
                            headers=_build_request_headers(referer),
                            cookies=cookies,
                            stream=True,
                            timeout=timeout,
                        )
                    finally:
                        urllib3_connection.allowed_gai_family = original
            else:
                response = session.get(
                    url,
                    headers=_build_request_headers(referer),
                    cookies=cookies,
                    stream=True,
                    timeout=timeout,
                )
            return response, session
        except Exception as exc:
            session.close()
            last_error = exc
            if not force_ipv4 and _is_route_error(exc):
                log_event(
                    "media",
                    "download_retry",
                    level="warning",
                    strategy="ipv4_fallback",
                    url=compact_url(url),
                    reason=exc,
                )
                continue
        break

    if last_error is None:
        raise RuntimeError(f"Could not open media response for {url}")
    raise last_error


def _looks_like_direct_video_asset(url: str) -> bool:
    """Return True when the URL points to a CDN asset instead of a web page."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    query = parsed.query.lower()
    if any(path.endswith(ext) for ext in _DIRECT_VIDEO_EXTENSIONS):
        return True
    if any(hint in host for hint in _DIRECT_VIDEO_HOST_HINTS):
        return True
    if "tiktok.com" in host and host not in _SOCIAL_WEB_PAGE_HOSTS:
        return "/video/" in path or "mime_type=video" in query or "ply_type=" in query
    if host in _SOCIAL_WEB_PAGE_HOSTS:
        return False
    if any(domain in host for domain in ("instagram.com", "tiktok.com", "facebook.com")):
        return "/video/" in path or "mime_type=video" in query
    return False


def _video_extension_for_response(url: str, content_type: str | None, fallback_ext: str) -> str:
    """Choose a sensible local extension for a direct video download."""
    normalized = (content_type or "").split(";", 1)[0].strip().lower()
    if normalized in _VIDEO_CONTENT_TYPE_EXTENSIONS:
        return _VIDEO_CONTENT_TYPE_EXTENSIONS[normalized]

    path = urlparse(url).path.lower()
    for ext in _DIRECT_VIDEO_EXTENSIONS:
        if path.endswith(ext):
            return ext

    return fallback_ext if fallback_ext.startswith(".") else f".{fallback_ext}"


def _download_direct_video(
    url: str,
    dest_dir: Path,
    filename_stem: str,
    cfg: "MediaConfig",
    *,
    referer: str | None = None,
    session_path: Path | None = None,
) -> Path | None:
    """Download a direct CDN video URL over HTTP instead of using yt-dlp."""
    dest_path: Path | None = None
    session: requests.Session | None = None
    downloaded = 0
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        resp, session = _streaming_get(
            url,
            referer=referer,
            timeout=60,
            cookies=_load_cookies_from_storage_state(session_path),
        )
        with resp:
            resp.raise_for_status()

            content_type = resp.headers.get("Content-Type")
            normalized_type = (content_type or "").split(";", 1)[0].strip().lower()
            if normalized_type.startswith("text/html"):
                logger.debug(f"[media] Direct video URL returned HTML instead of media: {url[:80]}")
                return None

            content_length = resp.headers.get("Content-Length")
            if content_length:
                size_mb = int(content_length) / (1024 * 1024)
                if size_mb > cfg.max_file_size_mb:
                    log_event(
                        "media",
                        "download_skipped",
                        level="warning",
                        asset="direct_video",
                        reason="size_limit",
                        size_mb=round(size_mb, 1),
                        limit_mb=cfg.max_file_size_mb,
                    )
                    return None

            extension = _video_extension_for_response(url, content_type, cfg.video_format)
            dest_path = dest_dir / f"{filename_stem}{extension}"
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if downloaded / (1024 * 1024) > cfg.max_file_size_mb:
                        log_event(
                            "media",
                            "download_failed",
                            level="warning",
                            asset="direct_video",
                            url=compact_url(url),
                            reason="size_limit_exceeded_during_stream",
                        )
                        dest_path.unlink(missing_ok=True)
                        return None

        if downloaded <= 0 and dest_path is not None:
            log_event(
                "media",
                "download_failed",
                level="warning",
                asset="direct_video",
                url=compact_url(url),
                reason="empty_response_body",
            )
            dest_path.unlink(missing_ok=True)
            return None

        logger.debug(f"[media] Downloaded direct video -> {dest_path}")
        return dest_path
    except Exception as e:
        log_event(
            "media",
            "download_failed",
            level="warning",
            asset="direct_video",
            url=compact_url(url),
            reason=e,
        )
        if dest_path is not None:
            dest_path.unlink(missing_ok=True)
        return None
    finally:
        if session is not None:
            session.close()


# ─── Path helpers ─────────────────────────────────────────────────────────────

def build_media_path(
    data_dir: Path,
    platform: str,
    account: str,
    item_id: str,
    date: str,
    filename: str,
    content_group: str | None = None,
) -> Path:
    """
    Construct canonical media path.
    date should be YYYY-MM-DD (UTC).
    Example:
        data/media/instagram/johndoe/posts/2026-03-29/ABC123/photo.jpg
    """
    safe_account = re.sub(r"[^\w.-]", "_", account)
    safe_item_id = re.sub(r"[^\w.-]", "_", item_id)
    base = data_dir / "media" / platform / safe_account
    if content_group:
        safe_group = re.sub(r"[^\w.-]", "_", content_group)
        base = base / safe_group
    return base / date / safe_item_id / filename


def build_media_dir(
    data_dir: Path,
    platform: str,
    account: str,
    item_id: str,
    date: str,
    content_group: str | None = None,
) -> Path:
    """Construct the canonical directory for a post or story's saved assets."""
    return build_media_path(
        data_dir,
        platform,
        account,
        item_id,
        date,
        "_",
        content_group=content_group,
    ).parent


def today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ─── Hash ────────────────────────────────────────────────────────────────────

def compute_sha256(file_path: Path) -> str:
    """Return the hex SHA-256 of a file."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ─── Deduplication ───────────────────────────────────────────────────────────

def deduplicate_or_save(conn, file_path: Path, **meta) -> bool:
    """
    Compute the file's SHA-256 and attach it to the requested post/story.

    We only skip exact duplicates that are already attached to the same parent item.
    Cross-item duplicates are still stored so every archived post/story/highlight keeps
    its own local file set even when Instagram reuses the same underlying asset bytes.
    """
    from src.db import insert_media_file, media_sha_exists_for_owner

    try:
        if not file_path.exists() or file_path.stat().st_size <= 0:
            logger.warning(f"[media] Refusing to attach empty file: {file_path}")
            file_path.unlink(missing_ok=True)
            return False
        sha = compute_sha256(file_path)
        if media_sha_exists_for_owner(
            conn,
            sha,
            post_id=meta.get("post_id"),
            story_id=meta.get("story_id"),
            file_type=meta.get("file_type"),
        ):
            logger.debug(
                f"[media] Duplicate hash {sha[:8]}… already attached to the same item — "
                f"skipping {file_path.name}"
            )
            file_path.unlink(missing_ok=True)
            return False
        auto_meta = probe_media_file(file_path)
        merged_meta = {
            "file_size": auto_meta.get("file_size"),
            "width": auto_meta.get("width"),
            "height": auto_meta.get("height"),
            "duration_s": auto_meta.get("duration_s"),
            **meta,
        }
        insert_media_file(conn, str(file_path), sha, **merged_meta)
        return True
    except Exception as e:
        logger.warning(f"[media] deduplicate_or_save error for {file_path}: {e}")
        return False


# ─── Photo download ──────────────────────────────────────────────────────────

def download_photo(
    url: str,
    dest_path: Path,
    cfg: "MediaConfig",
    referer: str | None = None,
) -> Path | None:
    """
    Download a photo via HTTP GET.
    Returns the destination path on success, None on failure.
    """
    session: requests.Session | None = None
    try:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        resp, session = _streaming_get(url, referer=referer, timeout=30)
        with resp:
            resp.raise_for_status()

            # Check file size before writing
            content_length = resp.headers.get("Content-Length")
            if content_length:
                size_mb = int(content_length) / (1024 * 1024)
                if size_mb > cfg.max_file_size_mb:
                    log_event(
                        "media",
                        "download_skipped",
                        level="warning",
                        asset="photo",
                        reason="size_limit",
                        size_mb=round(size_mb, 1),
                        limit_mb=cfg.max_file_size_mb,
                    )
                    return None

            with open(dest_path, "wb") as f:
                downloaded = 0
                for chunk in resp.iter_content(chunk_size=65536):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if downloaded / (1024 * 1024) > cfg.max_file_size_mb:
                        log_event(
                            "media",
                            "download_failed",
                            level="warning",
                            asset="photo",
                            url=compact_url(url),
                            reason="size_limit_exceeded_during_stream",
                        )
                        dest_path.unlink(missing_ok=True)
                        return None

        logger.debug(f"[media] Downloaded photo → {dest_path}")
        return dest_path
    except Exception as e:
        log_event(
            "media",
            "download_failed",
            level="warning",
            asset="photo",
            url=compact_url(url),
            reason=e,
        )
        dest_path.unlink(missing_ok=True)
        return None
    finally:
        if session is not None:
            session.close()


def download_binary(
    url: str,
    dest_path: Path,
    cfg: "MediaConfig",
    *,
    referer: str | None = None,
) -> Path | None:
    """
    Download an arbitrary binary attachment via HTTP GET.
    Unlike download_photo(), this intentionally does not assume image content.
    """
    session: requests.Session | None = None
    try:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        resp, session = _streaming_get(url, referer=referer, timeout=60)
        with resp:
            resp.raise_for_status()

            content_length = resp.headers.get("Content-Length")
            if content_length:
                size_mb = int(content_length) / (1024 * 1024)
                if size_mb > cfg.max_file_size_mb:
                    log_event(
                        "media",
                        "download_skipped",
                        level="warning",
                        asset="attachment",
                        reason="size_limit",
                        size_mb=round(size_mb, 1),
                        limit_mb=cfg.max_file_size_mb,
                    )
                    return None

            with open(dest_path, "wb") as f:
                downloaded = 0
                for chunk in resp.iter_content(chunk_size=65536):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if downloaded / (1024 * 1024) > cfg.max_file_size_mb:
                        log_event(
                            "media",
                            "download_failed",
                            level="warning",
                            asset="attachment",
                            url=compact_url(url),
                            reason="size_limit_exceeded_during_stream",
                        )
                        dest_path.unlink(missing_ok=True)
                        return None

        logger.debug(f"[media] Downloaded attachment -> {dest_path}")
        return dest_path
    except Exception as e:
        log_event(
            "media",
            "download_failed",
            level="warning",
            asset="attachment",
            url=compact_url(url),
            reason=e,
        )
        dest_path.unlink(missing_ok=True)
        return None
    finally:
        if session is not None:
            session.close()


# ─── Video download (yt-dlp) ─────────────────────────────────────────────────

def download_video(
    url: str,
    dest_dir: Path,
    filename_stem: str,
    cfg: "MediaConfig",
    *,
    referer: str | None = None,
    session_path: Path | None = None,
) -> Path | None:
    """
    Download a video, preferring direct CDN downloads and falling back to yt-dlp.
    The output filename is derived from filename_stem, for example:
        filename_stem="video"    -> video.mp4
        filename_stem="video_01" -> video_01.mp4
    Returns the final file path on success, None on failure.
    """
    try:
        if _looks_like_direct_video_asset(url):
            direct_path = _download_direct_video(
                url,
                dest_dir,
                filename_stem,
                cfg,
                referer=referer,
                session_path=session_path,
            )
            if direct_path:
                return direct_path

        dest_dir.mkdir(parents=True, exist_ok=True)
        output_template = str(dest_dir / f"{filename_stem}.%(ext)s")

        cmd = [
            "yt-dlp",
            "--no-playlist",
            "--user-agent", _DEFAULT_USER_AGENT,
            "--merge-output-format", cfg.video_format,
            "--max-filesize", f"{cfg.max_file_size_mb}m",
            "--output", output_template,
            "--no-warnings",
            "--quiet",
        ]

        if referer:
            cmd.extend(["--add-header", f"Referer:{referer}"])

        if cfg.yt_dlp_extra_args:
            cmd.extend(cfg.yt_dlp_extra_args)

        cmd.append(url)

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

        if result.returncode != 0:
            log_event(
                "media",
                "download_failed",
                level="warning",
                asset="video",
                method="yt-dlp",
                url=compact_url(url),
                reason=(result.stderr or result.stdout or "").strip()[:200] or "yt-dlp_returned_non_zero",
            )
            return None

        # Find the downloaded file (yt-dlp appends the actual extension)
        for ext in [cfg.video_format, "mp4", "mkv", "webm"]:
            candidate = dest_dir / f"{filename_stem}.{ext}"
            if candidate.exists() and candidate.stat().st_size > 0:
                logger.debug(f"[media] Downloaded video → {candidate}")
                return candidate
            if candidate.exists():
                candidate.unlink(missing_ok=True)

        for candidate in dest_dir.glob(f"{filename_stem}.*"):
            if candidate.is_file() and candidate.stat().st_size > 0:
                logger.debug(f"[media] Downloaded video → {candidate}")
                return candidate
            if candidate.is_file():
                candidate.unlink(missing_ok=True)

        log_event(
            "media",
            "download_failed",
            level="warning",
            asset="video",
            method="yt-dlp",
            url=compact_url(url),
            reason="output_file_not_found",
            dest_dir=dest_dir,
        )
        return None

    except subprocess.TimeoutExpired:
        log_event(
            "media",
            "download_failed",
            level="warning",
            asset="video",
            method="yt-dlp",
            url=compact_url(url),
            reason="timeout",
        )
        return None
    except Exception as e:
        log_event(
            "media",
            "download_failed",
            level="warning",
            asset="video",
            method="yt-dlp",
            url=compact_url(url),
            reason=e,
        )
        return None


def probe_media_file(file_path: Path) -> dict[str, object]:
    """Return best-effort file metadata for a saved photo/video using ffprobe."""
    info: dict[str, object] = {}
    is_image = file_path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    try:
        stat = file_path.stat()
        info["file_size"] = stat.st_size
    except Exception:
        return info

    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_streams",
                "-show_format",
                str(file_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return info

    if result.returncode != 0 or not result.stdout.strip():
        return info

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return info

    streams = payload.get("streams") or []
    video_stream = next(
        (
            stream for stream in streams
            if isinstance(stream, dict) and stream.get("codec_type") == "video"
        ),
        None,
    )
    if isinstance(video_stream, dict):
        width = video_stream.get("width")
        height = video_stream.get("height")
        if isinstance(width, int):
            info["width"] = width
        if isinstance(height, int):
            info["height"] = height

    info["has_audio"] = any(
        isinstance(stream, dict) and stream.get("codec_type") == "audio"
        for stream in streams
    )
    if is_image:
        info["has_audio"] = False
        return info

    duration_value = ((payload.get("format") or {}).get("duration"))
    if duration_value not in {None, ""}:
        try:
            info["duration_s"] = float(duration_value)
        except (TypeError, ValueError):
            pass

    return info


# ─── Story screenshot ────────────────────────────────────────────────────────

async def screenshot_story(page: Page, dest_path: Path) -> Path | None:
    """
    Take a JPEG screenshot of the current page (story viewer).
    Returns dest_path on success, None on failure.
    """
    try:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(
            path=str(dest_path),
            type="jpeg",
            quality=85,
            full_page=False,
        )
        logger.debug(f"[media] Story screenshot → {dest_path}")
        return dest_path
    except Exception as e:
        logger.warning(f"[media] Screenshot failed: {e}")
        return None


# ─── Carousel photo download ─────────────────────────────────────────────────

def download_carousel_photos(urls: list[str], dest_dir: Path,
                              cfg: "MediaConfig") -> list[Path]:
    """
    Download multiple carousel photos. Filenames: photo_01.jpg, photo_02.jpg, …
    Returns a list of successfully downloaded paths.
    """
    paths = []
    for i, url in enumerate(urls, start=1):
        filename = f"photo_{i:02d}.{cfg.photo_format}"
        dest_path = dest_dir / filename
        result = download_photo(url, dest_path, cfg)
        if result:
            paths.append(result)
    return paths


# ─── Thumbnail extraction ────────────────────────────────────────────────────

def extract_thumbnail(video_path: Path, filename: str | None = None) -> Path | None:
    """
    Use ffmpeg to extract the first frame of a video as thumb.jpg.
    Returns the thumbnail path, or None if ffmpeg fails.
    """
    if not video_path.exists():
        return None
    thumb_path = video_path.parent / (filename or "thumb.jpg")
    try:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-vframes", "1",
            "-q:v", "3",
            str(thumb_path),
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        if result.returncode == 0 and thumb_path.exists():
            logger.debug(f"[media] Thumbnail extracted → {thumb_path}")
            return thumb_path
    except Exception as e:
        logger.debug(f"[media] Thumbnail extraction failed: {e}")
    return None
