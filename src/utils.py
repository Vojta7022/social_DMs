"""
utils.py — Project-wide utility functions shared across all modules.

Imports ONLY stdlib and loguru to avoid circular imports. Nothing here may
import from other src.* modules.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from loguru import logger


# ─── Timestamp parsing ────────────────────────────────────────────────────────

def parse_iso_timestamp(value: str | None) -> datetime | None:
    """
    Parse an ISO 8601 string into a timezone-aware UTC datetime.
    Accepts trailing 'Z' or '+00:00'. Returns None on any failure.

    Replaces: main._parse_iso_timestamp, deep_analysis._parse_datetime
    """
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# ─── Short async wait ─────────────────────────────────────────────────────────

async def short_wait(lo: float = 0.8, hi: float = 2.0) -> None:
    """
    Await a random delay in [lo, hi] seconds.

    Replaces: scrapers/tiktok._short_wait, scrapers/facebook._short_wait
    """
    await asyncio.sleep(random.uniform(lo, hi))


# ─── Social profile identifiers / URLs ───────────────────────────────────────

_FACEBOOK_RESERVED_PATHS = frozenset({
    "pages",
    "groups",
    "events",
    "profile.php",
    "photo",
    "video",
    "watch",
    "search",
    "home",
    "login",
    "marketplace",
})


def normalize_facebook_profile_identifier(value: str | None) -> str:
    """
    Normalize Facebook profile references to either:
      - a vanity slug, e.g. "vorlova.anezka"
      - "profile.php?id=<numeric_id>"

    Accepts bare IDs, profile.php identifiers, and full Facebook URLs.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""

    if raw.isdigit():
        return f"profile.php?id={raw}"

    if raw.startswith("profile.php"):
        match = re.search(r"(?:^|[?&])id=(\d+)", raw)
        if match:
            return f"profile.php?id={match.group(1)}"
        return raw.strip("/ ")

    parsed = urlparse(raw)
    if parsed.scheme and parsed.netloc:
        host = parsed.netloc.lower()
        if host.endswith("facebook.com") or host.endswith("fb.com"):
            path = parsed.path.strip("/ ")
            if path == "profile.php":
                profile_id = (parse_qs(parsed.query).get("id") or [""])[0].strip()
                if profile_id:
                    return f"profile.php?id={profile_id}"
            if path:
                first = path.split("/", 1)[0].strip()
                if first and first not in _FACEBOOK_RESERVED_PATHS:
                    return first

    cleaned = raw.strip("/ ").lstrip("@")
    if cleaned.isdigit():
        return f"profile.php?id={cleaned}"
    return cleaned


def facebook_profile_url(identifier: str | None, *, section: str | None = None) -> str | None:
    """Build a live Facebook profile URL from a slug or profile.php ID reference."""
    normalized = normalize_facebook_profile_identifier(identifier)
    if not normalized:
        return None

    if normalized.startswith("profile.php?id="):
        base_url = f"https://www.facebook.com/{normalized}"
        if section:
            return f"{base_url}&sk={section}"
        return base_url

    clean = normalized.strip("/ ")
    if not clean:
        return None
    if section:
        return f"https://www.facebook.com/{clean}/{section}"
    return f"https://www.facebook.com/{clean}"


def platform_profile_url(platform: str | None, username: str | None) -> str | None:
    """Build a live profile URL for supported platforms."""
    platform_key = str(platform or "").strip().lower()
    raw_username = str(username or "").strip()
    if not raw_username:
        return None

    if platform_key == "instagram":
        return f"https://www.instagram.com/{raw_username.lstrip('@').strip('/')}/"
    if platform_key == "tiktok":
        handle = raw_username.lstrip("@").strip("/ ")
        if not handle:
            return None
        return f"https://www.tiktok.com/@{handle}"
    if platform_key == "facebook":
        return facebook_profile_url(raw_username)
    return None


# ─── ffprobe / ffmpeg helpers ─────────────────────────────────────────────────

def probe_video_duration(video_path: Path) -> float:
    """
    Return video duration in seconds using ffprobe. Falls back to 30.0 on failure.

    Replaces: deep_analysis._probe_video_duration,
              inline ffprobe block in ai_extractor.extract_repost_frames
    """
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_streams",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.debug(
                f"[utils] ffprobe non-zero exit for {video_path.name}, assuming 30s"
            )
            return 30.0
        info = json.loads(result.stdout)
        for stream in info.get("streams", []):
            try:
                duration = float(stream.get("duration") or 0)
                if duration > 0:
                    return duration
            except (TypeError, ValueError):
                continue
    except Exception as exc:
        logger.debug(f"[utils] ffprobe error for {video_path.name}: {exc}")
    return 30.0


def extract_video_frames(video_path: Path, n: int = 4, *, skip_fraction: float = 0.05) -> list[bytes]:
    """
    Extract n evenly-spaced JPEG frames from a video, returning raw bytes per frame.
    Skips the first and last skip_fraction of the duration to avoid title/end cards.
    Returns a shorter list if extraction partially fails.

    Replaces: ai_extractor.extract_repost_frames body,
              deep_analysis._extract_video_frames body (callers wrap into dict shape)
    """
    if not video_path.exists():
        return []

    duration_s = probe_video_duration(video_path)
    offsets: list[float] = [
        duration_s * (skip_fraction + (1.0 - 2 * skip_fraction) * i / max(n - 1, 1))
        for i in range(n)
    ]

    frames: list[bytes] = []
    try:
        with tempfile.TemporaryDirectory(prefix="scanner-frames-") as tmpdir:
            for i, offset in enumerate(offsets):
                frame_path = Path(tmpdir) / f"frame_{i:02d}.jpg"
                result = subprocess.run(
                    [
                        "ffmpeg", "-v", "quiet",
                        "-ss", f"{offset:.3f}",
                        "-i", str(video_path),
                        "-frames:v", "1",
                        "-q:v", "5",
                        str(frame_path),
                    ],
                    capture_output=True,
                    timeout=30,
                )
                if result.returncode == 0 and frame_path.exists():
                    frames.append(frame_path.read_bytes())
                else:
                    logger.debug(
                        f"[utils] ffmpeg frame {i} failed "
                        f"(returncode={result.returncode}) for {video_path.name}"
                    )
    except Exception as exc:
        logger.warning(f"[utils] Frame extraction failed for {video_path.name}: {exc}")

    return frames


# ─── Tempfile / directory cleanup ─────────────────────────────────────────────

def safe_unlink(path: Path | str | None) -> None:
    """Delete a file silently. No-op if path is None or the file does not exist."""
    if path is None:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        pass


def safe_rmtree(directory: Path | str | None) -> None:
    """Remove a directory tree silently. No-op if directory is None."""
    if directory is None:
        return
    try:
        shutil.rmtree(str(directory), ignore_errors=True)
    except Exception:
        pass
