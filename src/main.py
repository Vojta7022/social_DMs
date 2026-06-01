"""
main.py — Entry point and APScheduler job loop.

Jobs:
    job_scrape_posts        IntervalTrigger — check for new posts/videos
    job_scrape_stories      IntervalTrigger — check for new stories
    job_scrape_followers    IntervalTrigger — diff follower/following lists
    job_scrape_dms          IntervalTrigger — archive direct messages
    job_rebuild_profiles    IntervalTrigger — regenerate profile.json files
    job_flush_notifications IntervalTrigger — send pending Telegram notifications
    job_daily_digest        CronTrigger     — morning summary message

All scraping jobs open their own browser context (never shared), load/save the
session, and close the context when done. Errors are caught, logged, and sent
to Telegram without crashing the scheduler.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import sys
import warnings

# playwright-stealth 1.0.6 imports pkg_resources at module load time, which
# triggers a deprecation warning from setuptools. Suppress it since we can't
# change the third-party package.
warnings.filterwarnings("ignore", message="pkg_resources is deprecated", category=UserWarning)
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apscheduler.events import EVENT_JOB_ERROR, JobExecutionEvent
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from src.comment_utils import build_comment_tree, normalize_comment_records
from src.config import Config, TargetConfig, load_config
from src.db import (
    delete_post_media,
    get_account_id,
    get_daily_digest_data,
    get_existing_repost_ids_for_target,
    has_successful_scrape,
    init_db,
    insert_post,
    insert_repost_history,
    insert_story,
    get_post_media_files,
    get_post_comments,
    get_post_comment_history,
    get_post_media_count,
    get_post_media_records,
    get_post_row_id,
    get_repost_history_row_id,
    get_story_media_files,
    get_story_media_records,
    get_story_row_id,
    log_scrape,
    mark_unnotified,
    post_has_media,
    replace_dm_message_media,
    replace_dm_participants,
    replace_post_comments,
    story_has_media,
    update_post,
    update_repost_history,
    update_story,
    update_media_file,
    upsert_dm_conversation,
    upsert_dm_message,
    upsert_account,
    upsert_target,
)
from src.logging_utils import log_event, normalize_job_name
from src.proxy_manager import ProxyManager
from src.telegram_bot import build_bot, flush_notifications, send_daily_digest, send_error, send_new_post
from src.utils import (
    normalize_facebook_profile_identifier as _normalize_facebook_profile_identifier,
    parse_iso_timestamp as _parse_iso_timestamp,
    platform_profile_url as _platform_profile_url,
)


_SESSION_RECOVERY_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}
_SESSION_RECOVERY_RECENT_SUCCESS: dict[tuple[str, str], float] = {}
_SESSION_RECOVERY_SKIP_WINDOW_S = 60.0
_SUPPORTED_PLATFORMS = {"instagram", "tiktok", "facebook"}


class InteractiveLoginRequired(Exception):
    """Raised when a job should pause for manual re-authentication."""

    def __init__(self, platform: str, username: str, reason: str) -> None:
        super().__init__(reason)
        self.platform = platform
        self.username = username
        self.reason = reason


def _should_send_error_alert(exc: Exception) -> bool:
    """Return False for operational re-auth flows that are already handled locally."""
    if isinstance(exc, InteractiveLoginRequired):
        return False
    if "manual interactive login is required" in str(exc).lower():
        return False
    return True


# ─── Setup logging ──────────────────────────────────────────────────────────

def _setup_logging(cfg: Config) -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=cfg.log_level,
        format="[{time:YYYY-MM-DD HH:mm:ss}] [{level}] {message}",
        colorize=True,
    )
    logger.add(
        cfg.logging.file,
        level=cfg.log_level,
        rotation=cfg.logging.rotation,
        retention=cfg.logging.retention,
        format="[{time:YYYY-MM-DD HH:mm:ss}] [{level}] {message}",
        encoding="utf-8",
    )


# ─── Scraper session wrapper ─────────────────────────────────────────────────

def _session_recovery_lock(platform: str, username: str) -> asyncio.Lock:
    """Return a per-account lock used to serialize interactive re-login flows."""
    key = (platform, username)
    lock = _SESSION_RECOVERY_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _SESSION_RECOVERY_LOCKS[key] = lock
    return lock


async def _recover_session_interactively(
    platform: str,
    username: str,
    reason: str,
    cfg: Config,
    proxy_mgr: ProxyManager,
    bot,
) -> None:
    """Notify the operator, open a headed login window, and wait for recovery."""
    key = (platform, username)
    lock = _session_recovery_lock(platform, username)

    async with lock:
        now = asyncio.get_running_loop().time()
        last_success = _SESSION_RECOVERY_RECENT_SUCCESS.get(key)
        if last_success is not None and now - last_success < _SESSION_RECOVERY_SKIP_WINDOW_S:
            log_event(
                "main",
                "session_recovery_skip",
                platform=platform,
                account=username,
                reason="recent_successful_reauth",
            )
            return

        log_event(
            "main",
            "session_recovery_needed",
            level="warning",
            platform=platform,
            account=username,
            reason=reason,
        )
        if bot and cfg.notifications.error_alerts and _should_send_error_alert(Exception(reason)):
            await send_error(
                bot,
                cfg.notifications.chat_id,
                f"session_recovery/{platform}/{username}",
                Exception(reason),
                message_thread_id=cfg.notifications.error_thread(),
            )

        await interactive_login(f"{platform}:{username}", cfg, proxy_mgr)
        now = asyncio.get_running_loop().time()
        _SESSION_RECOVERY_RECENT_SUCCESS[key] = now
        # Prune entries older than 10 minutes to prevent unbounded dict growth.
        stale_keys = [k for k, t in _SESSION_RECOVERY_RECENT_SUCCESS.items() if now - t > 600.0]
        for _stale_key in stale_keys:
            _SESSION_RECOVERY_RECENT_SUCCESS.pop(_stale_key, None)
            _SESSION_RECOVERY_LOCKS.pop(_stale_key, None)


async def _run_with_session(
    platform: str,
    account_username: str,
    fn,
    cfg: Config,
    conn,
    proxy_mgr: ProxyManager,
    bot=None,
):
    """
    Open a stealth browser context for account_username, load saved session,
    run fn(page, cfg), save session, close browser.

    fn signature: async def fn(page, cfg) -> result
    """
    from src.browser.stealth import (
        BrowserProfileInUseError,
        close_browser,
        launch_browser,
        LogoutError,
        profile_dir_for,
        save_session,
        session_path_for,
    )
    from src.config import AccountConfig

    account = next(
        (a for a in cfg.accounts if a.platform == platform and a.username == account_username),
        None,
    )
    if not account:
        raise ValueError(f"Account not found in config: {platform}/{account_username}")

    proxy_url = proxy_mgr.get_for_account(account)
    session_path = session_path_for(cfg.sessions_dir, platform, account_username)
    profile_suffix = cfg.browser_profile_suffix or None
    base_profile_dir = profile_dir_for(cfg.sessions_dir, platform, account_username)
    profile_dir = profile_dir_for(
        cfg.sessions_dir,
        platform,
        account_username,
        profile_suffix=profile_suffix,
    )

    recovery_reason: str | None = None
    max_attempts = 2

    for attempt in range(max_attempts):
        try:
            playwright, browser, context, profile_lock_path = await launch_browser(
                proxy_url=proxy_url,
                headless=True,
                session_path=session_path,
                profile_dir=profile_dir,
                profile_seed_dir=base_profile_dir if profile_suffix else None,
            )
        except BrowserProfileInUseError as exc:
            raise RuntimeError(str(exc)) from exc

        recovery_reason = None
        try:
            page = await context.new_page()
            return await fn(page, cfg)
        except InteractiveLoginRequired as exc:
            recovery_reason = exc.reason
        except LogoutError as exc:
            recovery_reason = str(exc)
        finally:
            try:
                saved = await save_session(
                    context,
                    session_path,
                    platform=platform,
                    preserve_existing_if_missing_auth=True,
                )
                if not saved:
                    log_event(
                        "main",
                        "session_persist_skip",
                        level="warning",
                        platform=platform,
                        account=account_username,
                        reason="anonymous_state_detected",
                    )
            except Exception as e:
                log_event(
                    "main",
                    "session_persist_failed",
                    level="warning",
                    platform=platform,
                    account=account_username,
                    error=e,
                )
            await close_browser(playwright, browser, profile_lock_path)

        if not recovery_reason or attempt >= max_attempts - 1:
            break

        await _recover_session_interactively(
            platform,
            account_username,
            recovery_reason,
            cfg,
            proxy_mgr,
            bot,
        )

    if recovery_reason:
        raise LogoutError(recovery_reason)

    raise RuntimeError(f"Session runner exited unexpectedly for {platform}/{account_username}")


# ─── Login helper ────────────────────────────────────────────────────────────

async def _ensure_logged_in(
    page,
    platform: str,
    username: str,
    cfg: Config,
    *,
    interactive_recovery: bool = True,
):
    """Return a clean page in a logged-in state, probing auth on a throwaway page."""
    if platform == "instagram":
        from src.scrapers.instagram import check_login_state, login
    elif platform == "tiktok":
        from src.scrapers.tiktok import check_login_state, login
    elif platform == "facebook":
        from src.scrapers.facebook import check_login_state, login
    else:
        raise ValueError(f"Unknown platform: {platform}")

    probe_page = await page.context.new_page()
    try:
        if await check_login_state(probe_page):
            fresh_page = await page.context.new_page()
            try:
                await page.close()
            except Exception:
                pass
            return fresh_page
    finally:
        try:
            await probe_page.close()
        except Exception:
            pass

    if interactive_recovery:
        raise InteractiveLoginRequired(
            platform,
            username,
            f"Detected a signed-out session for {platform}/{username}; manual interactive login is required.",
        )

    password = cfg.credentials.get((platform, username), "")
    if not password:
        raise ValueError(f"Missing password for {platform}/{username}")

    if not _session_has_auth_cookie(cfg.sessions_dir, platform, username):
        log_event(
            "main",
            "session_missing_auth",
            level="warning",
            platform=platform,
            account=username,
            hint=f"python3 src/main.py --interactive-login {platform}:{username}",
        )

    log_event(
        "main",
        "session_relogin_start",
        platform=platform,
        account=username,
    )
    login_page = await page.context.new_page()
    try:
        if not await login(login_page, username, password, cfg.stealth):
            raise LogoutError(f"Login failed for {platform}/{username}")
        fresh_page = await page.context.new_page()
    except Exception:
        await login_page.close()
        raise

    try:
        await page.close()
    except Exception:
        pass
    try:
        await login_page.close()
    except Exception:
        pass
    return fresh_page


def _session_has_auth_cookie(sessions_dir: Path, platform: str, username: str) -> bool:
    """Return True when the saved storage-state file contains an auth cookie."""
    from src.browser.stealth import session_path_for

    session_path = session_path_for(sessions_dir, platform, username)
    if not session_path.exists():
        return False

    try:
        state = json.loads(session_path.read_text())
    except Exception:
        return False

    cookie_names = {
        cookie.get("name")
        for cookie in state.get("cookies", [])
        if isinstance(cookie, dict)
    }

    required = {
        "instagram": {"sessionid"},
        "facebook": {"c_user", "xs"},
        "tiktok": {"sessionid", "sessionid_ss", "sid_guard", "sid_tt", "uid_tt", "uid_tt_ss"},
    }.get(platform, set())

    if platform == "tiktok":
        return bool(cookie_names & required)
    return required.issubset(cookie_names)


def _normalise_post_media_items(post: dict) -> list[dict[str, str]]:
    """
    Return downloadable media items for a post.
    Prefer direct asset URLs emitted by the scrapers; only fall back to the post
    permalink for video-like posts where yt-dlp can still resolve the media.
    """
    items: list[dict[str, str]] = []

    def _tiktok_video_fallback_urls(source_url: str) -> list[str]:
        raw_metadata = post.get("raw_metadata")
        if not isinstance(raw_metadata, dict):
            return []
        detail_item = raw_metadata.get("detail_item_payload")
        if not isinstance(detail_item, dict):
            return []
        video = detail_item.get("video")
        if not isinstance(video, dict):
            return []

        urls: list[str] = []

        def _add_url(candidate: object) -> None:
            if not isinstance(candidate, str) or not candidate.startswith("http"):
                return
            if candidate == source_url or candidate in urls:
                return
            urls.append(candidate)

        _add_url(video.get("playAddr"))
        _add_url(video.get("downloadAddr"))

        play_addr_struct = video.get("PlayAddrStruct")
        if isinstance(play_addr_struct, dict):
            for candidate in play_addr_struct.get("UrlList") or []:
                _add_url(candidate)

        for bitrate_info in video.get("bitrateInfo") or []:
            if not isinstance(bitrate_info, dict):
                continue
            play_addr = bitrate_info.get("PlayAddr")
            if not isinstance(play_addr, dict):
                continue
            for candidate in play_addr.get("UrlList") or []:
                _add_url(candidate)

        return urls

    for item in post.get("media_items") or []:
        media_type = item.get("type")
        media_url = item.get("url")
        if media_type not in {"photo", "video"}:
            continue
        if not isinstance(media_url, str) or not media_url.startswith("http"):
            continue
        source_url = item.get("source_url") or post.get("url") or media_url
        fallback_urls = _tiktok_video_fallback_urls(source_url) if media_type == "video" else []
        items.append(
            {
                "type": media_type,
                "url": media_url,
                "source_url": source_url,
                "fallback_urls": fallback_urls,
            }
        )

    if items:
        return items

    post_url = post.get("url")
    if isinstance(post_url, str) and post_url.startswith("http"):
        if post.get("post_type") in {"video", "reel"}:
            fallback_urls = _tiktok_video_fallback_urls(post_url)
            primary_url = fallback_urls[0] if fallback_urls else post_url
            return [
                {
                    "type": "video",
                    "url": primary_url,
                    "source_url": post_url,
                    "fallback_urls": fallback_urls[1:] if fallback_urls else [],
                }
            ]

    return []



def _post_storage_date(post: dict) -> str:
    """Use the post's original timestamp for archive folders when available."""
    for candidate in (post.get("posted_at"), post.get("discovered_at")):
        parsed = _parse_iso_timestamp(candidate)
        if parsed:
            return parsed.strftime("%Y-%m-%d")
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _story_storage_date(story: dict) -> str:
    """Use the story's original timestamp for archive folders when available."""
    posted_at = _parse_iso_timestamp(story.get("posted_at"))
    if posted_at:
        return posted_at.strftime("%Y-%m-%d")

    expires_at = _parse_iso_timestamp(story.get("expires_at"))
    if expires_at:
        return (expires_at - timedelta(hours=24)).strftime("%Y-%m-%d")

    discovered_at = _parse_iso_timestamp(story.get("discovered_at"))
    if discovered_at:
        return discovered_at.strftime("%Y-%m-%d")

    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _relative_to_data_root(file_path: str) -> str:
    """Normalise an on-disk path to be relative to the data/ root when possible."""
    path = Path(file_path)
    if path.is_absolute():
        parts = path.parts
    else:
        parts = path.parts

    if "media" in parts:
        return str(Path(*parts[parts.index("media"):]))
    if "profiles" in parts:
        return str(Path(*parts[parts.index("profiles"):]))
    if "data" in parts:
        return str(Path(*parts[parts.index("data") + 1:]))
    return str(path)


def _data_root_path(cfg: Config) -> Path:
    """Resolve the configured data dir to an absolute filesystem path."""
    if cfg.data_dir.is_absolute():
        return cfg.data_dir.resolve()
    return (Path.cwd() / cfg.data_dir).resolve()


def _media_root_path(cfg: Config) -> Path:
    """Resolve the archive media root directory to an absolute path."""
    return _data_root_path(cfg) / "media"


def _account_archive_root(cfg: Config, platform: str, username: str) -> Path:
    """Return the per-profile media root used for bucketed archive folders."""
    safe_username = re.sub(r"[^\w.-]", "_", username)
    return cfg.data_dir / "media" / platform / safe_username


def _ensure_profile_archive_dirs(cfg: Config, platform: str, username: str) -> None:
    """Create stable archive buckets for a profile, even when some are still empty."""
    base_dir = _account_archive_root(cfg, platform, username)
    for group in ("posts", "reposts", "mentions", "stories", "story_highlights"):
        (base_dir / group).mkdir(parents=True, exist_ok=True)


def _post_archive_group(post: dict) -> str:
    """Map post content scopes to stable archive folder names."""
    scope = str(post.get("content_scope") or "feed").strip().lower()
    return {
        "feed": "posts",
        "reposts": "reposts",
        "mentions": "mentions",
    }.get(scope, scope or "posts")


def _story_archive_group(story: dict) -> str:
    """Map story content scopes to stable archive folder names."""
    scope = str(story.get("content_scope") or "story").strip().lower()
    return {
        "story": "stories",
        "highlight": "story_highlights",
    }.get(scope, scope or "stories")


def _post_archive_dir(cfg: Config, post: dict) -> Path:
    from src.media_downloader import build_media_dir

    return build_media_dir(
        cfg.data_dir,
        post["platform"],
        post.get("target_username") or post.get("username") or "unknown",
        post["post_id"],
        _post_storage_date(post),
        content_group=_post_archive_group(post),
    )


def _legacy_post_archive_dir(cfg: Config, post: dict) -> Path:
    """Return the pre-bucket archive directory used before content groups."""
    from src.media_downloader import build_media_dir

    return build_media_dir(
        cfg.data_dir,
        post["platform"],
        post.get("target_username") or post.get("username") or "unknown",
        post["post_id"],
        _post_storage_date(post),
    )


def _story_archive_dir(cfg: Config, story: dict) -> Path:
    from src.media_downloader import build_media_dir

    return build_media_dir(
        cfg.data_dir,
        story["platform"],
        story.get("target_username") or story.get("username") or "unknown",
        story["story_id"],
        _story_storage_date(story),
        content_group=_story_archive_group(story),
    )


def _legacy_story_archive_dir(cfg: Config, story: dict) -> Path:
    """Return the pre-bucket archive directory used before content groups."""
    from src.media_downloader import build_media_dir

    return build_media_dir(
        cfg.data_dir,
        story["platform"],
        story.get("target_username") or story.get("username") or "unknown",
        story["story_id"],
        _story_storage_date(story),
    )


def _enrich_media_record(conn, record) -> dict:
    """Return a saved-media descriptor with probed file metadata."""
    from src.media_downloader import probe_media_file

    row = dict(record)
    file_type = row.get("file_type")
    file_path = row["file_path"]
    full_path = _resolve_saved_media_path(file_path)
    probed: dict[str, object] = {}
    if full_path.exists():
        probed = probe_media_file(full_path)

    updates = {}
    for field in ("file_size", "width", "height", "duration_s"):
        if row.get(field) is None and probed.get(field) is not None:
            updates[field] = probed[field]
    if file_type in {"photo", "thumbnail", "screenshot"} and row.get("duration_s") is not None:
        updates["duration_s"] = None
    if updates:
        update_media_file(conn, row["id"], **updates)
        row.update(updates)

    duration_value = row.get("duration_s")
    try:
        duration_s = float(duration_value) if duration_value is not None else None
    except (TypeError, ValueError):
        duration_s = None

    return {
        "media_file_id": row["id"],
        "file_type": file_type,
        "file_path": _relative_to_data_root(file_path),
        "filename": full_path.name,
        "exists_on_disk": full_path.exists(),
        "sha256": row.get("sha256"),
        "downloaded_at": row.get("downloaded_at"),
        "file_size": row.get("file_size"),
        "width": row.get("width"),
        "height": row.get("height"),
        "duration_s": None if file_type in {"photo", "thumbnail", "screenshot"} else duration_s,
        "has_audio": False if file_type in {"photo", "thumbnail", "screenshot"} else probed.get("has_audio"),
    }


def _story_metadata_dict(value) -> dict:
    """Parse story metadata blobs stored in SQLite as JSON strings."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {}
    return {}


def _comment_record_for_export(comment) -> dict:
    """Normalize a DB comment row into export-friendly JSON."""
    return {
        "comment_id": comment["external_comment_id"],
        "comment_key": comment["comment_key"],
        "parent_comment_id": comment["parent_comment_id"],
        "root_comment_id": comment["root_comment_id"],
        "reply_to_comment_id": comment["reply_to_comment_id"],
        "username": comment["username"],
        "user_id": comment["user_id"],
        "display_name": comment["display_name"],
        "profile_pic_url": comment["profile_pic_url"],
        "is_verified": bool(comment["is_verified"]),
        "text": comment["text"],
        "commented_at": comment["commented_at"],
        "commented_at_label": comment["commented_at_label"],
        "like_count": comment["like_count"],
        "reply_count": comment["reply_count"],
        "depth": comment["depth"],
        "thread_path": comment["thread_path"],
        "comment_order": comment["comment_order"],
        "first_seen_at": comment["first_seen_at"],
        "last_seen_at": comment["last_seen_at"],
        "removed_at": comment["removed_at"],
        "is_current": bool(comment["is_current"]),
        "raw_payload": _story_metadata_dict(comment["raw_payload_json"]),
    }


def _post_comments_are_authoritative(post: dict) -> bool:
    """Return whether the current scrape produced a trustworthy full comment snapshot."""
    if "comments_snapshot_complete" in post:
        return bool(post.get("comments_snapshot_complete"))
    return True


def _write_post_archive_metadata(conn, post_row_id: int, post: dict, cfg: Config) -> None:
    """Persist a post.json sidecar and caption.txt next to the saved media."""
    row = conn.execute("""
        SELECT p.*, t.username AS target_username
        FROM posts p
        JOIN targets t ON t.id = p.target_id
        WHERE p.id=?
    """, (post_row_id,)).fetchone()
    if not row:
        return

    row_data = dict(row)
    raw_metadata = _story_metadata_dict(row_data.get("raw_metadata_json"))
    archive_dir = _post_archive_dir(cfg, row_data)
    archive_dir.mkdir(parents=True, exist_ok=True)

    saved_media = [
        _enrich_media_record(conn, record)
        for record in get_post_media_records(conn, post_row_id)
    ]
    media_paths = [record["file_path"] for record in saved_media]
    comments = normalize_comment_records(
        [_comment_record_for_export(comment) for comment in get_post_comments(conn, post_row_id)]
    )
    comment_history = normalize_comment_records(
        [_comment_record_for_export(comment) for comment in get_post_comment_history(conn, post_row_id)]
    )

    payload = {
        "platform": row_data["platform"],
        "target_username": row_data["target_username"],
        "post_id": row_data["post_id"],
        "post_type": row_data["post_type"],
        "content_scope": row_data["content_scope"],
        "archive_group": _post_archive_group(row_data),
        "collection_name": row_data["collection_name"],
        "url": row_data["url"],
        "caption": row_data["caption"],
        "posted_at": row_data["posted_at"],
        "discovered_at": row_data["discovered_at"],
        "like_count": row_data["like_count"],
        "comment_count": row_data["comment_count"],
        "share_count": row_data["share_count"],
        "view_count": row_data["view_count"],
        "is_repost": bool(row_data["is_repost"]),
        "repost_source_url": row_data["repost_source_url"],
        "repost_source_account": row_data["repost_source_account"],
        "owner": post.get("owner") or raw_metadata.get("owner"),
        "comments_supported": raw_metadata.get("comments_supported", post.get("comments_supported")),
        "comments_snapshot_complete": raw_metadata.get(
            "comments_snapshot_complete",
            post.get("comments_snapshot_complete"),
        ),
        "comments_unavailable_reason": raw_metadata.get(
            "comments_unavailable_reason",
            post.get("comments_unavailable_reason"),
        ),
        "comments": comments,
        "comment_tree": build_comment_tree(comments),
        "comment_history": comment_history,
        "comment_history_tree": build_comment_tree(comment_history),
        "media_paths": media_paths,
        "saved_media": saved_media,
        "media_items": post.get("media_items") or raw_metadata.get("media_items") or [],
        "raw_metadata": raw_metadata,
    }

    metadata_path = archive_dir / "post.json"
    metadata_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    caption_path = archive_dir / "caption.txt"
    caption = row_data.get("caption") or ""
    if caption:
        caption_path.write_text(caption, encoding="utf-8")
    else:
        caption_path.unlink(missing_ok=True)


def _write_story_archive_metadata(conn, story_row_id: int, story: dict, cfg: Config) -> None:
    """Persist a story.json sidecar and optional story_text.txt next to the saved media."""
    row = conn.execute("""
        SELECT s.*, t.username AS target_username
        FROM stories s
        JOIN targets t ON t.id = s.target_id
        WHERE s.id=?
    """, (story_row_id,)).fetchone()
    if not row:
        return

    row_data = dict(row)
    archive_dir = _story_archive_dir(cfg, row_data)
    archive_dir.mkdir(parents=True, exist_ok=True)

    saved_media = [
        _enrich_media_record(conn, record)
        for record in get_story_media_records(conn, story_row_id)
    ]
    media_paths = [record["file_path"] for record in saved_media]
    metadata = _story_metadata_dict(row_data.get("metadata_json"))

    payload = {
        "platform": row_data["platform"],
        "target_username": row_data["target_username"],
        "story_id": row_data["story_id"],
        "story_type": row_data["story_type"],
        "content_scope": row_data["content_scope"],
        "archive_group": _story_archive_group(row_data),
        "collection_name": row_data["collection_name"],
        "url": row_data["url"],
        "posted_at": row_data["posted_at"],
        "expires_at": row_data["expires_at"],
        "discovered_at": row_data["discovered_at"],
        "metadata": metadata,
        "media_paths": media_paths,
        "saved_media": saved_media,
    }

    metadata_path = archive_dir / "story.json"
    metadata_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    story_text = (
        metadata.get("caption")
        or metadata.get("text")
        or metadata.get("accessibility_caption")
        or ""
    )
    text_path = archive_dir / "story_text.txt"
    if story_text:
        text_path.write_text(story_text, encoding="utf-8")
    else:
        text_path.unlink(missing_ok=True)


def _download_post_media(conn, post_row_id: int, post: dict, cfg: Config) -> None:
    """Download all discovered media assets for a post and register them in SQLite."""
    from src.media_downloader import (
        build_media_dir,
        deduplicate_or_save,
        download_photo,
        download_video,
        extract_thumbnail,
    )

    media_items = _normalise_post_media_items(post)
    if not media_items:
        logger.debug(f"[main] No downloadable media assets resolved for {post['platform']}/{post['post_id']}")
        return

    session_path = _post_media_session_path(cfg, post)

    dest_dir = _post_archive_dir(cfg, post)
    dest_dir.mkdir(parents=True, exist_ok=True)

    media_totals = Counter(item["type"] for item in media_items)
    media_seen: defaultdict[str, int] = defaultdict(int)

    for item in media_items:
        media_type = item["type"]
        media_seen[media_type] += 1
        index = media_seen[media_type]
        total = media_totals[media_type]

        if media_type == "photo":
            filename = (
                f"photo.{cfg.media.photo_format}"
                if total == 1
                else f"photo_{index:02d}.{cfg.media.photo_format}"
            )
            # Try primary URL then any CDN mirror fallbacks (TikTok signs URLs
            # with a short TTL so the first mirror may have expired).
            photo_candidates: list[str] = [item["url"]] + list(item.get("fallback_urls") or [])
            path = None
            for photo_url in photo_candidates:
                path = download_photo(
                    photo_url,
                    dest_dir / filename,
                    cfg.media,
                    referer=item.get("source_url"),
                )
                if path:
                    break
            if path:
                deduplicate_or_save(conn, path, post_id=post_row_id, file_type="photo")
            continue

        stem = "video" if total == 1 else f"video_{index:02d}"
        source_url = item.get("source_url")
        path = None
        candidate_urls: list[str] = []
        for candidate in [item["url"], *(item.get("fallback_urls") or [])]:
            if isinstance(candidate, str) and candidate.startswith("http") and candidate not in candidate_urls:
                candidate_urls.append(candidate)

        for candidate_url in candidate_urls:
            path = download_video(
                candidate_url,
                dest_dir,
                stem,
                cfg.media,
                referer=source_url,
                session_path=session_path,
            )
            if path:
                break

        if not path and source_url and source_url not in candidate_urls:
            path = download_video(
                source_url,
                dest_dir,
                stem,
                cfg.media,
                referer=source_url,
                session_path=session_path,
            )
        if path:
            deduplicate_or_save(conn, path, post_id=post_row_id, file_type="video")
            thumb_name = "thumb.jpg" if total == 1 else f"{stem}_thumb.jpg"
            thumb = extract_thumbnail(path, thumb_name)
            if thumb:
                deduplicate_or_save(conn, thumb, post_id=post_row_id, file_type="thumbnail")


def _post_media_storage_prefix(cfg: Config, post: dict) -> str:
    return str(_post_archive_dir(cfg, post))


def _post_media_session_path(cfg: Config, post: dict) -> Path | None:
    """Resolve the login session file that should authorize post-media downloads."""
    from src.browser.stealth import session_path_for

    platform = str(post.get("platform") or "").strip()
    if not platform:
        return None

    account_username = str(post.get("using_account") or "").strip()
    target_username = str(post.get("target_username") or post.get("username") or "").strip()

    if not account_username and target_username:
        for target_cfg in _configured_targets(cfg):
            if target_cfg.platform == platform and target_cfg.username == target_username:
                account_username = target_cfg.using_account or target_cfg.username
                break

    if not account_username:
        return None

    session_path = session_path_for(cfg.sessions_dir, platform, account_username)
    return session_path if session_path.exists() else None


def _repair_post_media_if_needed(conn, post_row_id: int, post: dict, cfg: Config) -> None:
    """Rebuild saved media when the stored rows do not match the current scraper payload."""
    expected_items = _normalise_post_media_items(post)
    if not expected_items:
        return

    existing_rows = get_post_media_records(conn, post_row_id)
    primary_rows = [row for row in existing_rows if row["file_type"] in {"photo", "video"}]
    existing_count = len(primary_rows)
    expected_count = len(expected_items)
    existing_types = [str(row["file_type"] or "") for row in primary_rows]
    expected_types = [str(item.get("type") or "") for item in expected_items]
    expected_prefix = _post_media_storage_prefix(cfg, post)
    has_wrong_location = any(
        not Path(row["file_path"]).as_posix().startswith(expected_prefix)
        for row in primary_rows
    )
    has_unusable_primary_files = any(
        not _saved_media_file_is_usable(row["file_path"])
        for row in primary_rows
    )
    has_low_resolution_primary_photos = any(
        _post_media_record_looks_low_resolution(row)
        for row in primary_rows
    )
    has_type_mismatch = existing_types != expected_types

    if (
        existing_count == expected_count
        and not has_wrong_location
        and not has_unusable_primary_files
        and not has_low_resolution_primary_photos
        and not has_type_mismatch
    ):
        return

    log_event(
        "main",
        "media_repair",
        job="post_media",
        platform=post["platform"],
        target=post.get("target_username"),
        item_kind="post",
        item_id=post["post_id"],
        have=existing_count,
        expected=expected_count,
        low_resolution=has_low_resolution_primary_photos,
    )
    stale_paths = delete_post_media(conn, post_row_id)
    for path_str in stale_paths:
        try:
            Path(path_str).unlink(missing_ok=True)
        except Exception:
            continue

    _download_post_media(conn, post_row_id, post, cfg)


def _story_audio_expected(metadata: dict) -> bool:
    """Return True when the story metadata hints that audio should be preserved."""
    if "audio_expected" in metadata:
        return bool(metadata.get("audio_expected"))
    music = metadata.get("music")
    return isinstance(music, list) and bool(music)


def _story_media_candidates_for_download(story: dict) -> list[dict]:
    """Return normalized story media candidates in the order they should be tried."""
    metadata = _story_metadata_dict(story.get("metadata"))
    candidates: list[dict] = []
    seen_urls: set[str] = set()

    def _append(kind: str | None, url: object, source: str, raw: dict | None = None) -> None:
        if kind not in {"image", "video"}:
            return
        if not isinstance(url, str) or not url.startswith("http") or url in seen_urls:
            return
        seen_urls.add(url)
        candidate = {
            "kind": kind,
            "url": url,
            "source": source,
        }
        for key in ("width", "height", "bitrate"):
            value = (raw or {}).get(key)
            if isinstance(value, int) and value > 0:
                candidate[key] = value
        candidates.append(candidate)

    for raw_candidate in metadata.get("media_candidates") or []:
        if not isinstance(raw_candidate, dict):
            continue
        _append(
            raw_candidate.get("kind"),
            raw_candidate.get("url"),
            raw_candidate.get("source") or "metadata",
            raw=raw_candidate,
        )

    primary_kind = story.get("story_type")
    if primary_kind in {"image", "video"}:
        _append(primary_kind, story.get("url"), "story_url")

    permalink = story.get("story_permalink") or metadata.get("story_permalink")
    if (
        isinstance(permalink, str)
        and permalink.startswith("http")
        and (
            story.get("story_type") == "video"
            or _story_audio_expected(metadata)
            or not candidates
        )
    ):
        _append("video", permalink, "story_permalink")

    return candidates


def _merge_story_runtime_metadata(conn, story_row_id: int, story: dict) -> None:
    """Preserve downloader-derived metadata when a story row is refreshed."""
    row = conn.execute(
        "SELECT metadata_json FROM stories WHERE id=?",
        (story_row_id,),
    ).fetchone()
    if not row:
        return

    existing_metadata = _story_metadata_dict(row["metadata_json"])
    incoming_metadata = _story_metadata_dict(story.get("metadata"))
    for key in ("archive_capture", "selected_media_candidate"):
        if key in existing_metadata and key not in incoming_metadata:
            incoming_metadata[key] = existing_metadata[key]
    story["metadata"] = incoming_metadata


def _delete_story_media(conn, story_row_id: int) -> list[str]:
    """Delete stored media rows for a story and return their file paths."""
    rows = get_story_media_records(conn, story_row_id)
    if not rows:
        return []
    file_paths = [str(row["file_path"]) for row in rows]
    conn.execute("DELETE FROM media_files WHERE story_id=?", (story_row_id,))
    conn.commit()
    return file_paths


def _story_media_repair_reason(conn, story_row_id: int, story: dict) -> str | None:
    """Return a repair reason when the saved story media is clearly degraded."""
    from src.media_downloader import probe_media_file

    existing_rows = get_story_media_records(conn, story_row_id)
    if not existing_rows:
        return "missing archived media"

    metadata = _story_metadata_dict(story.get("metadata"))
    capture = metadata.get("archive_capture") if isinstance(metadata.get("archive_capture"), dict) else {}
    candidates = _story_media_candidates_for_download(story)
    video_candidate_count = sum(1 for candidate in candidates if candidate["kind"] == "video")
    attempted_video_candidates = int(capture.get("attempted_video_candidates") or 0)
    exhausted_video_candidates = bool(capture.get("exhausted_video_candidates"))
    audio_expected = _story_audio_expected(metadata)

    video_paths: list[Path] = []
    for row in existing_rows:
        file_path = _resolve_saved_media_path(row["file_path"])
        if not file_path.exists():
            return "archived media file is missing on disk"
        if row["file_type"] == "video":
            video_paths.append(file_path)

    if video_candidate_count and not video_paths:
        if not exhausted_video_candidates or attempted_video_candidates < video_candidate_count:
            return "video-capable story was archived without video"

    if audio_expected and video_paths:
        has_audio = any(bool(probe_media_file(path).get("has_audio")) for path in video_paths)
        if not has_audio and (
            not exhausted_video_candidates or attempted_video_candidates < video_candidate_count
        ):
            return "story music was detected but archived video has no audio"

    return None


def _ensure_story_media_backfilled(conn, story_row_id: int, story: dict, cfg: Config) -> bool:
    """Retry a story/highlight download when metadata exists but no files were persisted."""
    if story_has_media(conn, story_row_id):
        return True

    candidates = _story_media_candidates_for_download(story)
    if not candidates:
        return False

    logger.warning(
        f"[main] Story/highlight {story['platform']}/{story['story_id']} has raw media candidates "
        "but no archived files; retrying download from metadata candidates."
    )
    _download_story_media(conn, story_row_id, story, cfg)
    return story_has_media(conn, story_row_id)


def _download_story_media(conn, story_row_id: int, story: dict, cfg: Config) -> None:
    """Download the primary media asset for a story/highlight and register it in SQLite."""
    from src.media_downloader import (
        deduplicate_or_save,
        download_photo,
        download_video,
        probe_media_file,
    )

    metadata = _story_metadata_dict(story.get("metadata"))
    story["metadata"] = metadata
    candidates = _story_media_candidates_for_download(story)
    if not candidates:
        logger.debug(f"[main] No downloadable media asset resolved for {story['platform']}/{story['story_id']}")
        return

    dest_dir = _story_archive_dir(cfg, story)
    dest_dir.mkdir(parents=True, exist_ok=True)

    referer = story.get("story_permalink") or metadata.get("story_permalink") or story.get("url")
    referer_value = referer if isinstance(referer, str) else None
    audio_expected = _story_audio_expected(metadata)
    video_candidates = [candidate for candidate in candidates if candidate["kind"] == "video"]
    image_candidates = [candidate for candidate in candidates if candidate["kind"] == "image"]

    selected_path: Path | None = None
    selected_candidate: dict | None = None
    selected_file_type: str | None = None
    selected_has_audio = False
    attempted_video_candidates = 0
    attempted_image_candidates = 0

    for index, candidate in enumerate(video_candidates, start=1):
        attempted_video_candidates += 1
        stem = "video" if len(video_candidates) == 1 else f"video_candidate_{index:02d}"
        path = download_video(
            candidate["url"],
            dest_dir,
            stem,
            cfg.media,
            referer=referer_value,
        )
        if not path:
            continue

        has_audio = bool(probe_media_file(path).get("has_audio"))
        if selected_path is None:
            selected_path = path
            selected_candidate = candidate
            selected_file_type = "video"
            selected_has_audio = has_audio
        elif has_audio and not selected_has_audio:
            if selected_path.exists():
                selected_path.unlink(missing_ok=True)
            selected_path = path
            selected_candidate = candidate
            selected_file_type = "video"
            selected_has_audio = True
        else:
            path.unlink(missing_ok=True)

        if selected_has_audio:
            break

    if selected_path is None:
        for candidate in image_candidates:
            attempted_image_candidates += 1
            path = download_photo(
                candidate["url"],
                dest_dir / f"photo.{cfg.media.photo_format}",
                cfg.media,
                referer=referer_value,
            )
            if not path:
                continue
            selected_path = path
            selected_candidate = candidate
            selected_file_type = "photo"
            selected_has_audio = False
            break

    if selected_path and selected_file_type == "video":
        canonical_path = selected_path.with_name(f"video{selected_path.suffix}")
        if canonical_path != selected_path:
            canonical_path.unlink(missing_ok=True)
            selected_path.rename(canonical_path)
            selected_path = canonical_path
        deduplicate_or_save(conn, selected_path, story_id=story_row_id, file_type="video")
        for candidate in image_candidates:
            attempted_image_candidates += 1
            preview_path = download_photo(
                candidate["url"],
                dest_dir / f"preview.{cfg.media.photo_format}",
                cfg.media,
                referer=referer_value,
            )
            if not preview_path:
                continue
            deduplicate_or_save(conn, preview_path, story_id=story_row_id, file_type="thumbnail")
            metadata["preview_media_candidate"] = candidate
            break
    elif selected_path and selected_file_type == "photo":
        deduplicate_or_save(conn, selected_path, story_id=story_row_id, file_type="photo")

    metadata["archive_capture"] = {
        "audio_expected": audio_expected,
        "selected_kind": selected_file_type,
        "audio_preserved": selected_has_audio if selected_file_type == "video" else False,
        "attempted_video_candidates": attempted_video_candidates,
        "attempted_image_candidates": attempted_image_candidates,
        "video_candidates_available": len(video_candidates),
        "image_candidates_available": len(image_candidates),
        "exhausted_video_candidates": attempted_video_candidates >= len(video_candidates),
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if selected_candidate:
        metadata["selected_media_candidate"] = {
            **selected_candidate,
            "file_type": selected_file_type,
            "has_audio": selected_has_audio if selected_file_type == "video" else False,
        }
    story["metadata"] = metadata
    update_story(conn, story_row_id, story)


def _safe_dm_path_component(value: str | None, fallback: str) -> str:
    """Return a filesystem-safe DM path component."""
    cleaned = re.sub(r"[^\w.-]+", "_", str(value or "").strip())
    return cleaned or fallback


def _dm_message_storage_date(message: dict) -> str:
    """Use the original DM timestamp for attachment folders when available."""
    sent_at = _parse_iso_timestamp(message.get("sent_at"))
    if sent_at:
        return sent_at.strftime("%Y-%m-%d")
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _dm_media_dir(
    cfg: Config,
    platform: str,
    account_username: str,
    conversation_id: str,
    message: dict,
) -> Path:
    safe_account = _safe_dm_path_component(account_username, "unknown")
    safe_conversation = _safe_dm_path_component(conversation_id, "conversation")
    safe_message = _safe_dm_path_component(
        message.get("message_id") or message.get("message_key"),
        "message",
    )
    return (
        cfg.data_dir
        / "media"
        / platform
        / safe_account
        / "dms"
        / safe_conversation
        / _dm_message_storage_date(message)
        / safe_message
    )


def _normalise_dm_attachment_type(attachment: dict) -> str:
    raw = str(
        attachment.get("file_type")
        or attachment.get("attachment_type")
        or attachment.get("type")
        or "unknown"
    ).strip().lower()
    if raw in {"image", "photo", "picture"}:
        return "photo"
    if raw in {"video", "clip"}:
        return "video"
    if raw in {"audio", "voice"}:
        return raw
    if raw in {"document", "file"}:
        return "document"
    if raw in {"xma_preview", "share_preview", "share"}:
        return "share_preview"
    if raw in {"sticker", "gif"}:
        return raw
    return raw or "unknown"


def _guess_dm_attachment_extension(attachment: dict, file_type: str, cfg: Config) -> str:
    mime_type = str(attachment.get("mime_type") or "").strip().lower()
    source_url = (
        attachment.get("download_url")
        or attachment.get("source_url")
        or attachment.get("preview_url")
        or ""
    )
    path_ext = Path(urlparse(str(source_url)).path).suffix.lower()
    if path_ext and len(path_ext) <= 8:
        return path_ext
    if mime_type.startswith("image/"):
        subtype = mime_type.split("/", 1)[1]
        return ".jpg" if subtype in {"jpeg", "pjpeg"} else f".{subtype}"
    if mime_type.startswith("video/"):
        subtype = mime_type.split("/", 1)[1]
        return f".{subtype}"
    if mime_type.startswith("audio/"):
        subtype = mime_type.split("/", 1)[1]
        return f".{subtype}"
    if file_type == "video":
        return f".{cfg.media.video_format}"
    if file_type in {"photo", "share_preview", "sticker", "gif"}:
        return f".{cfg.media.photo_format}"
    if file_type in {"audio", "voice"}:
        return ".m4a"
    if file_type == "document":
        return ".bin"
    return ".bin"


def _download_dm_attachments(
    conn,
    message_row_id: int,
    platform: str,
    account_username: str,
    conversation_id: str,
    conversation_url: str | None,
    message: dict,
    cfg: Config,
) -> list[dict]:
    """Download DM attachments and return DB-ready attachment records."""
    from src.media_downloader import (
        compute_sha256,
        download_binary,
        download_photo,
        download_video,
        probe_media_file,
    )

    saved_attachments: list[dict] = []
    attachment_rows = message.get("attachments") or []
    if not attachment_rows:
        replace_dm_message_media(conn, message_row_id, [])
        return saved_attachments

    dest_dir = _dm_media_dir(cfg, platform, account_username, conversation_id, message)
    dest_dir.mkdir(parents=True, exist_ok=True)

    type_totals = Counter(
        _normalise_dm_attachment_type(attachment)
        for attachment in attachment_rows
    )
    type_seen: defaultdict[str, int] = defaultdict(int)

    for attachment in attachment_rows:
        file_type = _normalise_dm_attachment_type(attachment)
        source_url = (
            attachment.get("download_url")
            or attachment.get("source_url")
            or attachment.get("preview_url")
            or attachment.get("url")
        )
        if not isinstance(source_url, str) or not source_url.startswith("http"):
            continue

        type_seen[file_type] += 1
        index = type_seen[file_type]
        total = type_totals[file_type]
        stem = file_type if total == 1 else f"{file_type}_{index:02d}"
        extension = _guess_dm_attachment_extension(attachment, file_type, cfg)
        dest_path = dest_dir / f"{stem}{extension}"

        path = dest_path if dest_path.exists() else None
        if path is None:
            if file_type == "video" and extension not in {".mp4", ".mov", ".mkv", ".webm"}:
                path = download_video(
                    source_url,
                    dest_dir,
                    stem,
                    cfg.media,
                    referer=conversation_url or source_url,
                )
            elif file_type in {"photo", "share_preview", "sticker", "gif"}:
                path = download_photo(
                    source_url,
                    dest_path,
                    cfg.media,
                    referer=conversation_url or source_url,
                )
            else:
                path = download_binary(
                    source_url,
                    dest_path,
                    cfg.media,
                    referer=conversation_url or source_url,
                )
            if path is None and file_type == "video":
                path = download_binary(
                    source_url,
                    dest_path.with_suffix(".mp4"),
                    cfg.media,
                    referer=conversation_url or source_url,
                )

        if path is None or not path.exists():
            continue

        probe = probe_media_file(path)
        saved_attachments.append(
            {
                "attachment_id": attachment.get("attachment_id"),
                "attachment_order": attachment.get("attachment_order", index - 1),
                "file_path": str(path),
                "file_type": file_type,
                "mime_type": attachment.get("mime_type"),
                "sha256": compute_sha256(path),
                "file_size": probe.get("file_size"),
                "width": probe.get("width"),
                "height": probe.get("height"),
                "duration_s": probe.get("duration_s"),
                "source_url": source_url,
                "preview_text": attachment.get("preview_text"),
                "metadata": attachment.get("metadata"),
            }
        )

    replace_dm_message_media(conn, message_row_id, saved_attachments)
    return saved_attachments


def _rebuild_dm_archive_for_account(conn, cfg: Config, platform: str, username: str) -> None:
    """Rebuild one account-level DM archive if the owned account exists in SQLite."""
    from src.dm_builder import build_dm_archive

    try:
        account_id = get_account_id(conn, platform, username)
    except Exception:
        return
    build_dm_archive(conn, account_id, cfg.accounts_dir)


# ─── Jobs ─────────────────────────────────────────────────────────────────────

def _own_profile_target(account) -> TargetConfig | None:
    """Project an owned login account into a monitored target when enabled."""
    if not getattr(account, "monitor_own_profile", False):
        return None
    return TargetConfig(
        platform=account.platform,
        username=account.username,
        check_posts=True,
        check_stories=True,
        check_highlights=account.platform == "instagram",
        check_mentions=False,
        using_account=account.username,
        using_accounts=[account.username],
    )


def _configured_targets(cfg: Config) -> list[TargetConfig]:
    """Return explicit targets plus any enabled own-account profile targets."""
    merged: list[TargetConfig] = []
    seen: set[tuple[str, str]] = set()

    for target in cfg.targets:
        key = (target.platform, target.username)
        if key in seen:
            continue
        merged.append(target)
        seen.add(key)

    for account in cfg.accounts:
        own_target = _own_profile_target(account)
        if not own_target:
            continue
        key = (own_target.platform, own_target.username)
        if key in seen:
            continue
        merged.append(own_target)
        seen.add(key)

    return merged


def _iter_targets(
    cfg: Config,
    selected_targets: set[tuple[str, str]] | None = None,
    conn=None,
    skip_paused: bool = False,
):
    for target in _configured_targets(cfg):
        if selected_targets and (target.platform, target.username) not in selected_targets:
            continue
        if skip_paused and conn is not None:
            row = conn.execute(
                "SELECT is_paused FROM targets WHERE platform=? AND username=?",
                (target.platform, target.username),
            ).fetchone()
            if row and row["is_paused"]:
                continue
        yield target


def _get_batch(
    cfg: Config,
    job_key: str,
    conn,
    selected_targets: set[tuple[str, str]] | None = None,
) -> list:
    """
    Return the slice of targets to process for this job run.
    If selected_targets is specified (e.g. scan_now), skip batching.
    Batch size and rotation index are read from scanner_config DB.
    After returning the slice, the index is advanced for the next run.
    """
    from src.db import get_config, set_config
    all_targets = list(_iter_targets(cfg, selected_targets, conn=conn, skip_paused=True))

    if selected_targets:
        return all_targets  # explicit selection overrides batching

    batch_size = int(get_config(conn, f"{job_key}_batch_size") or "0")
    if batch_size <= 0 or batch_size >= len(all_targets):
        return all_targets  # no batching

    idx = int(get_config(conn, f"{job_key}_batch_index") or "0")
    total = len(all_targets)
    start = (idx * batch_size) % total
    end = start + batch_size

    if end <= total:
        batch = all_targets[start:end]
    else:
        # Wrap around
        batch = all_targets[start:] + all_targets[:end - total]

    next_idx = idx + 1
    set_config(conn, f"{job_key}_batch_index", str(next_idx))
    return batch


def _group_targets_by_account_session(targets: list[TargetConfig]) -> list[list[TargetConfig]]:
    """Group targets so one browser profile/session is never used concurrently."""
    grouped: dict[tuple[str, str], list[TargetConfig]] = defaultdict(list)
    for target in targets:
        grouped[(target.platform, target.using_account or target.username)].append(target)
    return list(grouped.values())


async def _run_target_groups_concurrently(
    targets: list[TargetConfig],
    runner,
) -> None:
    """Run independent account/platform groups in parallel, serialising within each group."""
    groups = _group_targets_by_account_session(targets)
    if not groups:
        return
    if len(groups) == 1:
        await runner(groups[0])
        return
    await asyncio.gather(*(runner(group) for group in groups))


def _iter_accounts(cfg: Config, selected_accounts: set[tuple[str, str]] | None = None):
    for account in cfg.accounts:
        if selected_accounts and (account.platform, account.username) not in selected_accounts:
            continue
        yield account


def _parse_platform_filters(values: list[str] | None) -> set[str] | None:
    """Parse repeatable --platform filters into a normalized platform set."""
    if not values:
        return None

    parsed = {
        str(value).strip().lower()
        for value in values
        if str(value).strip()
    }
    invalid = sorted(parsed - _SUPPORTED_PLATFORMS)
    if invalid:
        raise ValueError(
            "Invalid platform filter(s): "
            + ", ".join(invalid)
            + ". Use one or more of: instagram, tiktok, facebook."
        )
    return parsed or None


def _filter_config_by_platforms(cfg: Config, selected_platforms: set[str] | None) -> Config:
    """Restrict the active account/target config to the requested platforms."""
    if not selected_platforms:
        return cfg

    cfg.accounts = [
        account
        for account in cfg.accounts
        if account.platform in selected_platforms
    ]
    cfg.targets = [
        target
        for target in cfg.targets
        if target.platform in selected_platforms
    ]
    return cfg


def _parse_selectors(values: list[str] | None, label: str) -> set[tuple[str, str]] | None:
    if not values:
        return None

    parsed: set[tuple[str, str]] = set()
    for value in values:
        platform, sep, username = value.partition(":")
        platform = platform.strip().lower()
        username = username.strip()
        if not sep or not platform or not username:
            raise ValueError(f"Invalid {label} selector {value!r}. Use platform:username.")
        parsed.add((platform, username))
    return parsed


def _parse_target_selectors(cfg: Config, values: list[str] | None) -> set[tuple[str, str]] | None:
    if not values:
        return None

    return {_resolve_target_reference(cfg, value, label="target selector") for value in values}


def _parse_optional_target_ref(value: str | None) -> tuple[str | None, str] | None:
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        raise ValueError("Target range values cannot be blank.")
    platform, sep, username = raw.partition(":")
    if sep:
        platform = platform.strip().lower()
        username = username.strip()
        if not platform or not username:
            raise ValueError(
                f"Invalid target range selector {value!r}. Use username or platform:username."
            )
        return platform, username
    return None, raw


def _resolve_target_reference(
    cfg: Config,
    value: str,
    *,
    label: str = "target range selector",
) -> tuple[str, str]:
    ref = _parse_optional_target_ref(value)
    if ref is None:
        raise ValueError(f"{label.capitalize()} cannot be blank.")

    platform_hint, username = ref
    if platform_hint == "facebook":
        username = _normalize_facebook_profile_identifier(username)
    matches = [
        (target.platform, target.username)
        for target in _configured_targets(cfg)
        if target.username == username and (platform_hint is None or target.platform == platform_hint)
    ]
    if not matches:
        raise ValueError(
            f"{label.capitalize()} {value!r} was not found in the configured target list."
        )

    unique_matches = list(dict.fromkeys(matches))
    if len(unique_matches) > 1:
        choices = ", ".join(f"{platform}:{name}" for platform, name in unique_matches)
        raise ValueError(
            f"{label.capitalize()} {value!r} is ambiguous. Use one of: {choices}"
        )
    return unique_matches[0]


def _select_targets_by_range(
    cfg: Config,
    start_value: str | None,
    end_value: str | None,
) -> set[tuple[str, str]] | None:
    if not start_value and not end_value:
        return None

    ordered_targets = list(_configured_targets(cfg))
    ordered_keys = [(target.platform, target.username) for target in ordered_targets]
    if not ordered_keys:
        return set()

    start_key = (
        _resolve_target_reference(cfg, start_value, label="target range selector")
        if start_value
        else ordered_keys[0]
    )
    end_key = (
        _resolve_target_reference(cfg, end_value, label="target range selector")
        if end_value
        else ordered_keys[-1]
    )

    start_index = ordered_keys.index(start_key)
    end_index = ordered_keys.index(end_key)
    if start_index > end_index:
        raise ValueError(
            f"Target range start {start_value!r} appears after end {end_value!r} in settings.yaml."
        )

    return set(ordered_keys[start_index : end_index + 1])


def _combine_target_filters(
    explicit_targets: set[tuple[str, str]] | None,
    ranged_targets: set[tuple[str, str]] | None,
) -> set[tuple[str, str]] | None:
    if explicit_targets is None:
        return ranged_targets
    if ranged_targets is None:
        return explicit_targets

    combined = explicit_targets & ranged_targets
    if not combined:
        raise ValueError("The explicit target filters and target range do not overlap.")
    return combined


def _parse_post_selector(value: str) -> tuple[str, str, str]:
    """Parse platform:username:post_id selectors used for post replays."""
    platform, sep, remainder = value.partition(":")
    username, sep2, post_id = remainder.partition(":")
    platform = platform.strip().lower()
    username = username.strip()
    post_id = post_id.strip()
    if not sep or not sep2 or not platform or not username or not post_id:
        raise ValueError(
            f"Invalid post selector {value!r}. Use platform:username:post_id."
        )
    return platform, username, post_id


def _resolve_saved_media_path(file_path: str) -> Path:
    """Resolve a stored media path from SQLite to an absolute on-disk path."""
    path = Path(file_path)
    if path.is_absolute():
        return path
    return (Path.cwd() / path).resolve()


def _saved_media_file_is_usable(file_path: str) -> bool:
    """Return True when a stored media file exists and is not empty."""
    path = _resolve_saved_media_path(file_path)
    try:
        return path.exists() and path.stat().st_size > 0
    except OSError:
        return False


def _post_media_record_looks_low_resolution(record) -> bool:
    """Return True when a saved primary post photo is only thumbnail-sized."""
    file_type = record["file_type"] if "file_type" in record.keys() else None
    if str(file_type or "") != "photo":
        return False

    try:
        width_value = record["width"] if "width" in record.keys() else 0
        width = int(width_value or 0)
    except (TypeError, ValueError):
        width = 0
    try:
        height_value = record["height"] if "height" in record.keys() else 0
        height = int(height_value or 0)
    except (TypeError, ValueError):
        height = 0

    longest_edge = max(width, height)
    return 0 < longest_edge <= 320


def _connection_result_list(result: dict | None, list_type: str) -> tuple[bool, list]:
    """Read one followers/following list from a scraper result, honoring partial availability flags."""
    if not isinstance(result, dict):
        return True, []

    available = result.get(f"{list_type}_available")
    if available is False:
        return False, []

    values = result.get(list_type)
    if isinstance(values, list):
        return True, values
    return True, []


def _maybe_mark_notified(conn, table: str, row_ids: list[int], notify_new_items: bool) -> None:
    if notify_new_items or not row_ids:
        return
    from src.db import mark_notified
    mark_notified(conn, table, row_ids)


def _resolve_post_scraper(platform: str, scrape_kind: str):
    if platform == "instagram":
        if scrape_kind == "posts":
            from src.scrapers.instagram import scrape_posts
            return scrape_posts
        if scrape_kind == "reposts":
            from src.scrapers.instagram import scrape_reposts
            return scrape_reposts
        if scrape_kind == "mentions":
            from src.scrapers.instagram import scrape_mentions
            return scrape_mentions
    elif platform == "tiktok":
        if scrape_kind == "posts":
            from src.scrapers.tiktok import scrape_posts
            return scrape_posts
        if scrape_kind == "reposts":
            from src.scrapers.tiktok import scrape_reposts
            return scrape_reposts
        if scrape_kind == "mentions":
            from src.scrapers.tiktok import scrape_mentions
            return scrape_mentions
    elif platform == "facebook":
        if scrape_kind == "posts":
            from src.scrapers.facebook import scrape_posts
            return scrape_posts
        if scrape_kind == "mentions":
            from src.scrapers.facebook import scrape_mentions
            return scrape_mentions
    raise ValueError(f"Unsupported post scrape kind {scrape_kind!r} for platform {platform!r}")


def _platform_supports_reposts(platform: str) -> bool:
    """Return whether the platform exposes a repost surface we can scrape."""
    return platform in {"instagram", "tiktok"}


def _resolve_story_scraper(platform: str, scrape_kind: str):
    if platform == "instagram":
        if scrape_kind == "stories":
            from src.scrapers.instagram import scrape_stories
            return scrape_stories
        if scrape_kind == "highlights":
            from src.scrapers.instagram import scrape_highlights
            return scrape_highlights
    elif platform == "tiktok":
        if scrape_kind == "stories":
            from src.scrapers.tiktok import scrape_stories
            return scrape_stories
        if scrape_kind == "highlights":
            from src.scrapers.tiktok import scrape_highlights
            return scrape_highlights
    elif platform == "facebook":
        if scrape_kind == "stories":
            from src.scrapers.facebook import scrape_stories
            return scrape_stories
        if scrape_kind == "highlights":
            from src.scrapers.facebook import scrape_highlights
            return scrape_highlights
    raise ValueError(f"Unsupported story scrape kind {scrape_kind!r} for platform {platform!r}")


def _resolve_target_connection_scraper(platform: str):
    if platform == "instagram":
        from src.scrapers.instagram import scrape_target_connections
        return scrape_target_connections
    if platform == "tiktok":
        from src.scrapers.tiktok import scrape_target_connections
        return scrape_target_connections
    if platform == "facebook":
        from src.scrapers.facebook import scrape_target_connections
        return scrape_target_connections
    raise ValueError(f"Unsupported target connection scraper for platform {platform!r}")


def _resolve_dm_scraper(platform: str):
    if platform == "instagram":
        from src.scrapers.instagram import scrape_dms
        return scrape_dms
    if platform == "tiktok":
        from src.scrapers.tiktok import scrape_dms
        return scrape_dms
    if platform == "facebook":
        from src.scrapers.facebook import scrape_dms
        return scrape_dms
    raise ValueError(f"Unsupported DM scraper for platform {platform!r}")


def _rebuild_profile_for_target(conn, cfg: Config, platform: str, username: str) -> None:
    """Rebuild a single target profile.json if the target exists in the DB."""
    from src.profile_builder import build_profile

    _ensure_profile_archive_dirs(cfg, platform, username)
    row = conn.execute(
        "SELECT id FROM targets WHERE platform=? AND username=?",
        (platform, username),
    ).fetchone()
    if row:
        build_profile(conn, row["id"], cfg.profiles_dir)


def _resolve_profile_scraper(platform: str):
    if platform == "instagram":
        from src.scrapers.instagram import scrape_profile_metadata
        return scrape_profile_metadata
    if platform == "tiktok":
        from src.scrapers.tiktok import scrape_profile_metadata
        return scrape_profile_metadata
    if platform == "facebook":
        from src.scrapers.facebook import scrape_profile_metadata
        return scrape_profile_metadata
    raise ValueError(f"Unsupported profile metadata scraper for platform {platform!r}")


async def _refresh_target_profile_metadata(
    page,
    platform: str,
    target_username: str,
    conn,
    cfg: Config,
) -> None:
    """Best-effort refresh of profile metadata before rebuilding profile archives."""
    try:
        scraper = _resolve_profile_scraper(platform)
        metadata = await scraper(page, target_username, cfg)
    except Exception as e:
        log_event(
            "main",
            "profile_refresh_error",
            level="warning",
            platform=platform,
            target=target_username,
            error=e,
        )
        return

    if not metadata:
        return

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    metadata["last_scraped_at"] = now_str
    target_id = upsert_target(conn, platform, target_username, **metadata)

    # Track bio / profile field changes
    _check_profile_field_changes(conn, target_id, metadata, now_str)

    # Track avatar changes
    pic_url = metadata.get("profile_pic_url")
    if pic_url:
        await _check_avatar_change(conn, target_id, platform, target_username, pic_url, cfg, now_str)
    cover_photo_url = metadata.get("cover_photo_url")
    if cover_photo_url:
        await _check_visual_asset_change(
            conn,
            target_id,
            platform,
            target_username,
            cover_photo_url,
            cfg,
            now_str,
            asset_kind="cover_photo",
        )


def _check_profile_field_changes(conn, target_id: int, metadata: dict, detected_at: str) -> None:
    """Diff tracked profile fields against history and insert change records."""
    from src.db import get_latest_profile_field, insert_profile_change
    field_map = {
        "bio_snapshot": "bio",
        "display_name": "display_name",
    }
    for meta_key, history_field in field_map.items():
        new_val = str(metadata.get(meta_key) or "").strip()
        if not new_val:
            continue
        old_val = get_latest_profile_field(conn, target_id, history_field)
        if old_val is None or old_val != new_val:
            insert_profile_change(conn, target_id, history_field, old_val, new_val, detected_at)
            log_event(
                "main",
                "profile_field_changed",
                field=history_field,
                target_id=target_id,
            )


async def _check_avatar_change(
    conn,
    target_id: int,
    platform: str,
    target_username: str,
    pic_url: str,
    cfg: Config,
    detected_at: str,
) -> None:
    """Download the profile picture and record it if the SHA-256 has changed."""
    await _check_visual_asset_change(
        conn,
        target_id,
        platform,
        target_username,
        pic_url,
        cfg,
        detected_at,
        asset_kind="avatar",
    )


async def _check_visual_asset_change(
    conn,
    target_id: int,
    platform: str,
    target_username: str,
    asset_url: str,
    cfg: Config,
    detected_at: str,
    *,
    asset_kind: str,
) -> None:
    """Download a tracked profile asset and record it when the hash changes."""
    import tempfile
    from src.db import get_latest_avatar_sha256, insert_avatar_record
    from src.media_downloader import compute_sha256, download_photo

    try:
        # All file I/O and HTTP work is synchronous — run in a thread so we don't
        # block the event loop during the download and SHA computation.
        def _do_asset_check_sync() -> tuple[str, str] | None:
            """Download, hash, and archive the asset. Returns (dest_path, sha) or None."""
            import tempfile as _tempfile
            referer: str | None = None
            if platform == "tiktok":
                referer = _platform_profile_url(platform, target_username)
            elif platform == "facebook":
                referer = _platform_profile_url(platform, target_username)

            with _tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp_path = Path(tmp.name)

            try:
                result = download_photo(asset_url, tmp_path, cfg.media, referer=referer)
                if not result or not tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)
                    return None

                sha = compute_sha256(tmp_path)
                existing_sha = get_latest_avatar_sha256(conn, target_id, asset_kind=asset_kind)

                if existing_sha == sha:
                    tmp_path.unlink(missing_ok=True)
                    return None

                asset_dir_name = {
                    "avatar": "avatars",
                    "cover_photo": "cover_photos",
                }.get(asset_kind, asset_kind)
                asset_dir = cfg.media_dir / platform / target_username / asset_dir_name
                asset_dir.mkdir(parents=True, exist_ok=True)
                ts = detected_at.replace(":", "-").replace("T", "_")[:19]
                dest = asset_dir / f"{ts}_{sha[:8]}.jpg"
                tmp_path.rename(dest)
                return str(dest), sha
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise

        outcome = await asyncio.to_thread(_do_asset_check_sync)
        if outcome is None:
            return
        dest_str, sha = outcome

        insert_avatar_record(
            conn,
            target_id,
            dest_str,
            sha,
            detected_at,
            asset_kind=asset_kind,
        )
        log_event(
            "main",
            f"{asset_kind}_changed",
            platform=platform,
            target=target_username,
            sha256=sha[:8],
        )
    except Exception as e:
        log_event(
            "main",
            f"{asset_kind}_check_error",
            level="warning",
            platform=platform,
            target=target_username,
            error=e,
        )
        try:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


def _prepare_repost_history_record(post: dict, target_username: str) -> dict:
    """Normalise scraper metadata into the metadata-only repost_history shape."""
    prepared = dict(post)
    owner = prepared.get("owner") if isinstance(prepared.get("owner"), dict) else {}
    owner_username = str(owner.get("username") or "").strip() or None

    source_username = (
        prepared.get("source_username")
        or prepared.get("repost_source_account")
        or (owner_username if owner_username and owner_username != target_username else None)
    )
    source_user_id = prepared.get("source_user_id")
    if not source_user_id and source_username and source_username == owner_username:
        source_user_id = owner.get("user_id")

    source_display_name = prepared.get("source_display_name")
    if not source_display_name and source_username and source_username == owner_username:
        source_display_name = owner.get("display_name")

    prepared["repost_id"] = str(prepared.get("repost_id") or prepared.get("post_id") or "").strip()
    prepared["source_platform"] = prepared.get("source_platform") or prepared.get("platform")
    prepared["source_username"] = source_username
    prepared["source_user_id"] = source_user_id
    prepared["source_display_name"] = source_display_name
    prepared["source_url"] = prepared.get("source_url") or prepared.get("repost_source_url") or prepared.get("url")
    prepared["repost_url"] = prepared.get("repost_url") or prepared.get("url")
    prepared["original_caption"] = prepared.get("original_caption") if "original_caption" in prepared else prepared.get("caption")
    prepared["reposted_at"] = prepared.get("reposted_at")
    prepared["original_posted_at"] = prepared.get("original_posted_at") if "original_posted_at" in prepared else prepared.get("posted_at")
    prepared["raw_metadata"] = {
        key: value
        for key, value in prepared.items()
        if key not in {
            "media_items",
            "comments",
            "comment_tree",
            "target_username",
        }
    }
    return prepared


async def _run_post_scrape_for_target(
    target_cfg,
    cfg: Config,
    conn,
    bot,
    proxy_mgr: ProxyManager,
    *,
    scrape_kind: str,
    job_type: str,
    notify_new_items: bool = True,
    replay_existing_items: bool = False,
) -> int:
    platform = target_cfg.platform
    target_username = target_cfg.username
    account_username = target_cfg.using_account
    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    skip_known_existing = job_type.startswith("bootstrap_") and not replay_existing_items

    try:
        target_id = upsert_target(conn, platform, target_username)
        scraper = _resolve_post_scraper(platform, scrape_kind)

        async def _scrape(page, cfg):
            page = await _ensure_logged_in(page, platform, account_username, cfg)
            await _refresh_target_profile_metadata(page, platform, target_username, conn, cfg)

            async def _run_scraper(scrape_page):
                if skip_known_existing:
                    try:
                        return await scraper(
                            scrape_page,
                            target_username,
                            conn,
                            cfg,
                            skip_known_existing=True,
                        )
                    except TypeError as exc:
                        if "skip_known_existing" not in str(exc):
                            raise
                return await scraper(scrape_page, target_username, conn, cfg)

            return await _run_scraper(page)

        discovered_posts = await _run_with_session(
            platform, account_username, _scrape, cfg, conn, proxy_mgr, bot=bot
        )

        inserted_ids: list[int] = []
        refreshed_existing = 0
        skipped_existing = 0
        replay_existing_ids: list[int] = []
        for post in (discovered_posts or []):
            post["target_username"] = target_username
            post_row_id = insert_post(conn, target_id, post)
            if not post_row_id:
                existing_post_row_id = get_post_row_id(
                    conn,
                    target_id,
                    platform,
                    post["post_id"],
                    post.get("content_scope", "feed"),
                )
                if existing_post_row_id:
                    if skip_known_existing and post_has_media(conn, existing_post_row_id):
                        skipped_existing += 1
                        continue
                    refreshed_existing += 1
                    update_post(conn, existing_post_row_id, post)
                    if _post_comments_are_authoritative(post):
                        replace_post_comments(
                            conn,
                            existing_post_row_id,
                            platform,
                            post.get("comments") or [],
                        )
                    if cfg.media.enabled and not post_has_media(conn, existing_post_row_id):
                        log_event(
                            "main",
                            "media_backfill",
                            job=normalize_job_name(job_type),
                            platform=platform,
                            target=target_username,
                            item_kind="post",
                            item_id=post["post_id"],
                        )
                        await asyncio.to_thread(_download_post_media, conn, existing_post_row_id, post, cfg)
                    elif cfg.media.enabled and get_post_media_count(conn, existing_post_row_id):
                        await asyncio.to_thread(_repair_post_media_if_needed, conn, existing_post_row_id, post, cfg)
                    _write_post_archive_metadata(conn, existing_post_row_id, post, cfg)
                    if replay_existing_items and notify_new_items:
                        replay_existing_ids.append(existing_post_row_id)
                continue
            inserted_ids.append(post_row_id)
            if _post_comments_are_authoritative(post):
                replace_post_comments(conn, post_row_id, platform, post.get("comments") or [])
            if cfg.media.enabled:
                await asyncio.to_thread(_download_post_media, conn, post_row_id, post, cfg)
            _write_post_archive_metadata(conn, post_row_id, post, cfg)

        log_event(
            "main",
            "job_result",
            job=normalize_job_name(job_type),
            platform=platform,
            target=target_username,
            discovered=len(discovered_posts or []),
            inserted=len(inserted_ids),
            refreshed_existing=refreshed_existing,
            skipped_existing=skipped_existing,
        )
        if replay_existing_items and notify_new_items and replay_existing_ids:
            mark_unnotified(conn, "posts", replay_existing_ids)
        _maybe_mark_notified(conn, "posts", inserted_ids, notify_new_items)
        log_scrape(
            conn, job_type, "success", started_at,
            platform=platform, target_id=target_id,
            detail=f"{len(inserted_ids)} saved from {scrape_kind}",
        )
        _rebuild_profile_for_target(conn, cfg, platform, target_username)
        return len(inserted_ids)

    except Exception as e:
        log_event(
            "main",
            "job_error",
            level="error",
            job=normalize_job_name(job_type),
            platform=platform,
            target=target_username,
            error=e,
        )
        log_scrape(conn, job_type, "error", started_at, platform=platform, detail=str(e))
        if cfg.notifications.error_alerts:
            await send_error(
                bot,
                cfg.notifications.chat_id,
                f"{job_type}/{platform}/{target_username}",
                e,
                message_thread_id=cfg.notifications.error_thread(),
            )
        return 0


def _update_proximity_scores(conn, target_id: int) -> None:
    """Recompute and persist social proximity scores for a monitored target."""
    try:
        from src.intelligence import compute_proximity_scores
        from src.db import update_target_proximity
        import json
        scores = compute_proximity_scores(conn, target_id)
        update_target_proximity(conn, target_id, json.dumps(scores, ensure_ascii=False))
    except Exception as e:
        log_event("main", "proximity_score_error", level="warning", target_id=target_id, error=e)


async def _run_repost_ai_analysis(
    repost_history_id: int,
    repost: dict,
    target_username: str,
    conn,
    cfg: Config,
    bot,
) -> None:
    """Download repost media temporarily, run Gemini analysis, then delete temp files."""
    import tempfile
    from src.ai_extractor import (
        analyze_repost,
        extract_audio_as_bytes,
        extract_repost_frames,
        push_to_vector_store,
        store_ai_analysis,
    )
    from src.media_downloader import download_video
    from src.telegram_bot import send_repost_analysis

    media_url = (
        repost.get("source_url")
        or repost.get("repost_url")
        or repost.get("url")
    )
    caption = repost.get("original_caption") or repost.get("caption") or ""
    source_username = repost.get("source_username")

    video_path = None
    tmp_dir = None
    try:
        if media_url:
            tmp_dir = tempfile.mkdtemp(prefix="repost_ai_")
            session_path = _post_media_session_path(
                cfg,
                {
                    "platform": repost.get("source_platform") or repost.get("platform"),
                    "target_username": target_username,
                },
            )
            video_path = download_video(
                media_url,
                Path(tmp_dir),
                "repost",
                cfg.media,
                session_path=session_path,
            )

        frames: list[bytes] = []
        audio_bytes = None
        if video_path and video_path.exists():
            frames = await asyncio.to_thread(extract_repost_frames, str(video_path), n=4)
            audio_bytes = await asyncio.to_thread(extract_audio_as_bytes, str(video_path))

        analysis = analyze_repost(caption, frames, audio_bytes, cfg.gemini_api_key)
        if analysis:
            store_ai_analysis(conn, repost_history_id, analysis)
            push_to_vector_store(
                target_id=conn.execute(
                    "SELECT target_id FROM repost_history WHERE id=?",
                    (repost_history_id,),
                ).fetchone()["target_id"],
                target_username=target_username,
                analysis=analysis,
                detected_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                data_dir=cfg.data_dir,
            )
            post_thread_id = cfg.notifications.thread_id_for_post(
                repost.get("platform", "instagram"), "reposts"
            )
            await send_repost_analysis(
                bot,
                cfg.notifications.chat_id,
                target_username,
                source_username,
                analysis,
                message_thread_id=post_thread_id,
            )
    except Exception as e:
        log_event(
            "main",
            "repost_ai_error",
            level="warning",
            repost_id=repost_history_id,
            error=e,
        )
    finally:
        # Always clean up temp media — reposts are extraction-only
        if video_path:
            try:
                video_path.unlink(missing_ok=True)
            except Exception:
                pass
        if tmp_dir:
            import shutil
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass


async def _run_repost_scrape_for_target(
    target_cfg,
    cfg: Config,
    conn,
    bot,
    proxy_mgr: ProxyManager,
    *,
    job_type: str,
    notify_new_items: bool = False,
    replay_existing_items: bool = False,
) -> int:
    """Scrape metadata-only repost history for one target."""
    platform = target_cfg.platform
    target_username = target_cfg.username
    account_username = target_cfg.using_account
    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    skip_known_existing = job_type.startswith("bootstrap_") and not replay_existing_items

    try:
        from src.intelligence import add_behavior_to_reposts, add_sentiment_to_reposts

        target_id = upsert_target(conn, platform, target_username)
        scraper = _resolve_post_scraper(platform, "reposts")

        async def _scrape(page, cfg):
            page = await _ensure_logged_in(page, platform, account_username, cfg)
            await _refresh_target_profile_metadata(page, platform, target_username, conn, cfg)

            async def _run_scraper(scrape_page):
                if skip_known_existing:
                    try:
                        return await scraper(
                            scrape_page,
                            target_username,
                            conn,
                            cfg,
                            skip_known_existing=True,
                        )
                    except TypeError as exc:
                        if "skip_known_existing" not in str(exc):
                            raise
                return await scraper(scrape_page, target_username, conn, cfg)

            return await _run_scraper(page)

        discovered_reposts = await _run_with_session(
            platform, account_username, _scrape, cfg, conn, proxy_mgr, bot=bot
        )
        prepared_reposts = [
            _prepare_repost_history_record(repost, target_username)
            for repost in (discovered_reposts or [])
            if repost
        ]
        prepared_reposts = add_behavior_to_reposts(add_sentiment_to_reposts(prepared_reposts))

        inserted_ids: list[int] = []
        refreshed_existing = 0
        skipped_existing = 0
        existing_repost_ids = get_existing_repost_ids_for_target(conn, target_id) if skip_known_existing else set()

        for repost in prepared_reposts:
            repost_id = repost.get("repost_id")
            if not repost_id:
                continue

            if skip_known_existing and repost_id in existing_repost_ids:
                skipped_existing += 1
                continue

            existing_row_id = get_repost_history_row_id(conn, target_id, platform, repost_id)
            if existing_row_id:
                refreshed_existing += 1
                update_repost_history(conn, existing_row_id, repost)
                continue

            row_id = insert_repost_history(conn, target_id, repost)
            if row_id:
                inserted_ids.append(row_id)
                if cfg.gemini_api_key:
                    await _run_repost_ai_analysis(
                        row_id, repost, target_username, conn, cfg, bot
                    )

        log_event(
            "main",
            "job_result",
            job=normalize_job_name(job_type),
            platform=platform,
            target=target_username,
            discovered=len(discovered_reposts or []),
            inserted=len(inserted_ids),
            refreshed_existing=refreshed_existing,
            skipped_existing=skipped_existing,
        )
        log_scrape(
            conn, job_type, "success", started_at,
            platform=platform, target_id=target_id,
            detail=f"{len(inserted_ids)} metadata-only repost records saved",
        )
        _rebuild_profile_for_target(conn, cfg, platform, target_username)
        return len(inserted_ids)

    except Exception as e:
        log_event(
            "main",
            "job_error",
            level="error",
            job=normalize_job_name(job_type),
            platform=platform,
            target=target_username,
            error=e,
        )
        log_scrape(conn, job_type, "error", started_at, platform=platform, detail=str(e))
        if cfg.notifications.error_alerts:
            await send_error(
                bot,
                cfg.notifications.chat_id,
                f"{job_type}/{platform}/{target_username}",
                e,
                message_thread_id=cfg.notifications.error_thread(),
            )
        return 0


async def _run_story_scrape_for_target(
    target_cfg,
    cfg: Config,
    conn,
    bot,
    proxy_mgr: ProxyManager,
    *,
    scrape_kind: str,
    job_type: str,
    notify_new_items: bool = True,
    replay_existing_items: bool = False,
) -> int:
    platform = target_cfg.platform
    target_username = target_cfg.username
    account_username = target_cfg.using_account
    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    skip_known_existing = job_type.startswith("bootstrap_") and not replay_existing_items

    try:
        target_id = upsert_target(conn, platform, target_username)
        scraper = _resolve_story_scraper(platform, scrape_kind)
        inserted_story_payloads: list[dict] = []

        async def _scrape(page, cfg):
            page = await _ensure_logged_in(page, platform, account_username, cfg)
            await _refresh_target_profile_metadata(page, platform, target_username, conn, cfg)

            async def _run_scraper(scrape_page):
                if skip_known_existing:
                    try:
                        return await scraper(
                            scrape_page,
                            target_username,
                            conn,
                            cfg,
                            skip_known_existing=True,
                        )
                    except TypeError as exc:
                        if "skip_known_existing" not in str(exc):
                            raise
                return await scraper(scrape_page, target_username, conn, cfg)

            # TikTok story scraping is more reliable on a fresh page. Reusing the
            # profile-refresh page can leave TikTok in a half-hydrated state where
            # the story avatar never mounts even though the embedded story flag is present.
            if platform == "tiktok" and scrape_kind == "stories":
                fresh_page = await page.context.new_page()
                try:
                    return await _run_scraper(fresh_page)
                finally:
                    await fresh_page.close()

            return await _run_scraper(page)

        discovered_stories = await _run_with_session(
            platform, account_username, _scrape, cfg, conn, proxy_mgr, bot=bot
        )

        inserted_ids: list[int] = []
        skipped_existing = 0
        replay_existing_ids: list[int] = []
        for story in (discovered_stories or []):
            story["target_username"] = target_username
            story_row_id = insert_story(conn, target_id, story)
            if not story_row_id:
                existing_story_row_id = get_story_row_id(conn, platform, story["story_id"])
                if existing_story_row_id:
                    existing_has_media = story_has_media(conn, existing_story_row_id)
                    _merge_story_runtime_metadata(conn, existing_story_row_id, story)
                    update_story(conn, existing_story_row_id, story)

                    repair_reason = None
                    if cfg.media.enabled:
                        repair_reason = _story_media_repair_reason(
                            conn,
                            existing_story_row_id,
                            story,
                        )
                        if repair_reason:
                            log_event(
                                "main",
                                "media_repair",
                                job=normalize_job_name(job_type),
                                platform=platform,
                                target=target_username,
                                item_kind="story",
                                item_id=story["story_id"],
                                reason=repair_reason,
                            )
                            stale_paths = _delete_story_media(conn, existing_story_row_id)
                            for path_str in stale_paths:
                                try:
                                    _resolve_saved_media_path(path_str).unlink(missing_ok=True)
                                except Exception:
                                    continue
                            _download_story_media(conn, existing_story_row_id, story, cfg)
                            _ensure_story_media_backfilled(
                                conn,
                                existing_story_row_id,
                                story,
                                cfg,
                            )
                        elif not existing_has_media:
                            log_event(
                                "main",
                                "media_backfill",
                                job=normalize_job_name(job_type),
                                platform=platform,
                                target=target_username,
                                item_kind="story",
                                item_id=story["story_id"],
                            )
                            _download_story_media(conn, existing_story_row_id, story, cfg)
                            _ensure_story_media_backfilled(
                                conn,
                                existing_story_row_id,
                                story,
                                cfg,
                            )

                    if skip_known_existing and existing_has_media and not repair_reason:
                        skipped_existing += 1
                    _write_story_archive_metadata(conn, existing_story_row_id, story, cfg)
                    if replay_existing_items and notify_new_items:
                        replay_existing_ids.append(existing_story_row_id)
                continue
            inserted_ids.append(story_row_id)
            inserted_story_payloads.append(dict(story))
            if cfg.media.enabled:
                _download_story_media(conn, story_row_id, story, cfg)
                _ensure_story_media_backfilled(conn, story_row_id, story, cfg)
            _write_story_archive_metadata(conn, story_row_id, story, cfg)

        if replay_existing_items and notify_new_items and replay_existing_ids:
            mark_unnotified(conn, "stories", replay_existing_ids)
        _maybe_mark_notified(conn, "stories", inserted_ids, notify_new_items)
        log_event(
            "main",
            "job_result",
            job=normalize_job_name(job_type),
            platform=platform,
            target=target_username,
            discovered=len(discovered_stories or []),
            inserted=len(inserted_ids),
            skipped_existing=skipped_existing,
            replay_existing=len(replay_existing_ids),
        )
        log_scrape(
            conn, job_type, "success", started_at,
            platform=platform, target_id=target_id,
            detail=(
                f"{len(inserted_ids)} saved from {scrape_kind}"
                + (f", {skipped_existing} already archived skipped" if skipped_existing else "")
            ),
        )
        _rebuild_profile_for_target(conn, cfg, platform, target_username)
        if cfg.digital_twin.enabled and inserted_story_payloads:
            from src.digital_twin import maybe_queue_story_initiation_draft

            for story_payload in inserted_story_payloads:
                if str(story_payload.get("content_scope") or "story").strip().lower() != "story":
                    continue
                try:
                    await maybe_queue_story_initiation_draft(
                        platform=platform,
                        account_username=account_username,
                        target_username=target_username,
                        story_payload=story_payload,
                        bot=bot,
                    )
                except Exception as exc:
                    log_event(
                        "main",
                        "digital_twin_story_draft_failed",
                        level="warning",
                        platform=platform,
                        target=target_username,
                        story_id=story_payload.get("story_id"),
                        error=exc,
                    )
        return len(inserted_ids)

    except Exception as e:
        log_event(
            "main",
            "job_error",
            level="error",
            job=normalize_job_name(job_type),
            platform=platform,
            target=target_username,
            error=e,
        )
        log_scrape(conn, job_type, "error", started_at, platform=platform, detail=str(e))
        if cfg.notifications.error_alerts:
            await send_error(
                bot,
                cfg.notifications.chat_id,
                f"{job_type}/{platform}/{target_username}",
                e,
                message_thread_id=cfg.notifications.error_thread(),
            )
        return 0


async def job_scrape_posts(
    cfg: Config,
    conn,
    bot,
    proxy_mgr: ProxyManager,
    selected_targets: set[tuple[str, str]] | None = None,
    notify_new_items: bool = True,
    job_type: str = "posts",
) -> None:
    """Scrape normal feed posts for the selected targets."""
    targets_batch = [
        target_cfg
        for target_cfg in _get_batch(cfg, "posts", conn, selected_targets)
        if target_cfg.check_posts
    ]

    async def _run_group(group: list[TargetConfig]) -> None:
        for target_cfg in group:
            await _run_post_scrape_for_target(
                target_cfg, cfg, conn, bot, proxy_mgr,
                scrape_kind="posts",
                job_type=job_type,
                notify_new_items=notify_new_items,
            )

    await _run_target_groups_concurrently(targets_batch, _run_group)


async def job_scrape_reposts(
    cfg: Config,
    conn,
    bot,
    proxy_mgr: ProxyManager,
    selected_targets: set[tuple[str, str]] | None = None,
    notify_new_items: bool = True,
    job_type: str = "reposts",
) -> None:
    """Scrape metadata-only repost feeds for supported targets."""
    targets_batch = [
        target_cfg
        for target_cfg in _get_batch(cfg, "reposts", conn, selected_targets)
        if target_cfg.check_posts and _platform_supports_reposts(target_cfg.platform)
    ]

    async def _run_group(group: list[TargetConfig]) -> None:
        for target_cfg in group:
            await _run_repost_scrape_for_target(
                target_cfg, cfg, conn, bot, proxy_mgr,
                job_type=job_type,
                notify_new_items=notify_new_items,
            )

    await _run_target_groups_concurrently(targets_batch, _run_group)


async def job_scrape_mentions(
    cfg: Config,
    conn,
    bot,
    proxy_mgr: ProxyManager,
    selected_targets: set[tuple[str, str]] | None = None,
    notify_new_items: bool = True,
    job_type: str = "mentions",
) -> None:
    """Scrape mention/tagged-post feeds for the selected targets."""
    targets_batch = [
        target_cfg
        for target_cfg in _get_batch(cfg, "mentions", conn, selected_targets)
        if target_cfg.check_mentions
    ]

    async def _run_group(group: list[TargetConfig]) -> None:
        for target_cfg in group:
            await _run_post_scrape_for_target(
                target_cfg, cfg, conn, bot, proxy_mgr,
                scrape_kind="mentions",
                job_type=job_type,
                notify_new_items=notify_new_items,
            )

    await _run_target_groups_concurrently(targets_batch, _run_group)


async def _get_instagram_story_tray(
    cfg: Config,
    conn,
    proxy_mgr: ProxyManager,
    bot,
    account_username: str,
) -> set[str] | None:
    """
    Open one browser session, load the Instagram home feed, and return the set
    of usernames that have active stories in the story tray.

    Returns None on any error so the caller can fall back to per-profile scraping.
    """
    try:
        async def _tray_fn(page, cfg):
            page = await _ensure_logged_in(page, "instagram", account_username, cfg)
            from src.scrapers.instagram import get_story_tray_usernames
            return await get_story_tray_usernames(page, cfg)

        result = await _run_with_session(
            "instagram", account_username, _tray_fn, cfg, conn, proxy_mgr, bot=bot
        )
        return result if isinstance(result, set) else None
    except Exception as exc:
        log_event("main", "story_tray_check_failed", level="warning", error=str(exc))
        return None


async def job_scrape_stories(
    cfg: Config,
    conn,
    bot,
    proxy_mgr: ProxyManager,
    selected_targets: set[tuple[str, str]] | None = None,
    notify_new_items: bool = True,
    job_type: str = "stories",
) -> None:
    """Scrape active stories for the selected targets.

    For Instagram targets, navigates to the home feed story tray once to identify
    which targets have active stories before visiting each profile individually.
    Targets not visible in the tray are skipped (no active stories).
    """
    targets_batch = [
        t for t in _get_batch(cfg, "stories", conn, selected_targets)
        if t.check_stories
    ]

    async def _run_group(group: list[TargetConfig]) -> None:
        run_targets = group
        if group and group[0].platform == "instagram":
            account_username = group[0].using_account
            tray_usernames = await _get_instagram_story_tray(cfg, conn, proxy_mgr, bot, account_username)
            if tray_usernames:
                before = len(run_targets)
                run_targets = [t for t in run_targets if t.username.lower() in tray_usernames]
                skipped = before - len(run_targets)
                if skipped:
                    log_event("main", "job_skip", job="stories", reason="not_in_story_tray", skipped=skipped)
            elif tray_usernames is not None:
                log_event("main", "story_tray_empty_fallback", ig_targets=len(run_targets))

        for target_cfg in run_targets:
            await _run_story_scrape_for_target(
                target_cfg, cfg, conn, bot, proxy_mgr,
                scrape_kind="stories",
                job_type=job_type,
                notify_new_items=notify_new_items,
            )

    await _run_target_groups_concurrently(targets_batch, _run_group)


async def job_scrape_highlights(
    cfg: Config,
    conn,
    bot,
    proxy_mgr: ProxyManager,
    selected_targets: set[tuple[str, str]] | None = None,
    notify_new_items: bool = True,
    job_type: str = "highlights",
) -> None:
    """Scrape highlight collections for the selected targets."""
    targets_batch = [
        target_cfg
        for target_cfg in _get_batch(cfg, "highlights", conn, selected_targets)
        if target_cfg.check_highlights
    ]

    async def _run_group(group: list[TargetConfig]) -> None:
        for target_cfg in group:
            await _run_story_scrape_for_target(
                target_cfg, cfg, conn, bot, proxy_mgr,
                scrape_kind="highlights",
                job_type=job_type,
                notify_new_items=notify_new_items,
            )

    await _run_target_groups_concurrently(targets_batch, _run_group)


async def job_scrape_followers(
    cfg: Config,
    conn,
    bot,
    proxy_mgr: ProxyManager,
    selected_accounts: set[tuple[str, str]] | None = None,
    selected_targets: set[tuple[str, str]] | None = None,
) -> None:
    """Scrape follower/following deltas for own accounts and monitored targets."""
    from src.db import (
        diff_target_follow_snapshots,
        insert_target_follow_events,
        save_target_follow_snapshot,
    )

    owned_account_keys = {
        (account.platform, account.username)
        for account in cfg.accounts
    }
    account_filters = set(selected_accounts or set())
    if selected_targets:
        account_filters.update({
            key
            for key in selected_targets
            if key in owned_account_keys
        })

    run_accounts = bool(account_filters) or (selected_accounts is None and selected_targets is None)
    run_targets = selected_targets is not None or (selected_accounts is None and selected_targets is None)

    if run_accounts:
        async def _run_account(account_cfg) -> None:
            platform = account_cfg.platform
            username = account_cfg.username
            started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            try:
                account_id = upsert_account(
                    conn, platform, username,
                    session_file=f"{platform}_{username}.json"
                )

                async def _scrape_followers_fn(page, cfg):
                    page = await _ensure_logged_in(page, platform, username, cfg)
                    await _refresh_target_profile_metadata(page, platform, username, conn, cfg)
                    if platform == "instagram":
                        from src.scrapers.instagram import scrape_followers
                        return await scrape_followers(page, username, conn, cfg)
                    if platform == "tiktok":
                        from src.scrapers.tiktok import scrape_followers
                        return await scrape_followers(page, username, conn, cfg)
                    if platform == "facebook":
                        from src.scrapers.facebook import scrape_followers
                        return await scrape_followers(page, username, conn, cfg)
                    raise ValueError(f"Unknown platform: {platform}")

                result = await _run_with_session(
                    platform, username, _scrape_followers_fn, cfg, conn, proxy_mgr, bot=bot
                )
                followers_available, followers = _connection_result_list(result, "followers")
                following_available, following = _connection_result_list(result, "following")
                events = (result or {}).get("events") or []

                update_fields = {
                    "last_scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
                if followers_available:
                    update_fields["follower_count"] = len(followers)
                if following_available:
                    update_fields["following_count"] = len(following)
                upsert_target(conn, platform, username, **update_fields)
                _rebuild_profile_for_target(conn, cfg, platform, username)
                log_event(
                    "main",
                    "job_result",
                    job="followers",
                    scope="account",
                    platform=platform,
                    account=username,
                    followers=len(followers) if followers_available else "unavailable",
                    following=len(following) if following_available else "unavailable",
                    events=len(events),
                )

                log_scrape(
                    conn, "followers", "success", started_at,
                    platform=platform, account_id=account_id,
                    detail=(
                        f"{len(events)} account events, "
                        f"followers={'unavailable' if not followers_available else len(followers)}, "
                        f"following={'unavailable' if not following_available else len(following)}"
                    ),
                )
                if events:
                    await job_flush_notifications(cfg, conn, bot, kinds={"followers"})

            except Exception as e:
                log_event(
                    "main",
                    "job_error",
                    level="error",
                    job="followers",
                    scope="account",
                    platform=platform,
                    account=username,
                    error=e,
                )
                log_scrape(conn, "followers", "error", started_at, platform=platform, detail=str(e))
                if cfg.notifications.error_alerts and _should_send_error_alert(e):
                    await send_error(
                        bot,
                        cfg.notifications.chat_id,
                        f"scrape_followers/{platform}/{username}",
                        e,
                        message_thread_id=cfg.notifications.error_thread(),
                    )

        accounts_batch = list(_iter_accounts(cfg, account_filters or None))
        if accounts_batch:
            await asyncio.gather(*(_run_account(account_cfg) for account_cfg in accounts_batch))

    if run_targets:
        targets_batch = [
            target_cfg
            for target_cfg in _get_batch(cfg, "followers", conn, selected_targets)
            if (target_cfg.platform, target_cfg.username) not in owned_account_keys
        ]

        async def _run_group(group: list[TargetConfig]) -> None:
            for target_cfg in group:
                platform = target_cfg.platform
                target_username = target_cfg.username
                account_username = target_cfg.using_account
                started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                target_id = None

                try:
                    target_id = upsert_target(conn, platform, target_username)
                    scraper = _resolve_target_connection_scraper(platform)

                    async def _scrape_target_connections_fn(page, cfg):
                        page = await _ensure_logged_in(page, platform, account_username, cfg)
                        return await scraper(page, target_username, conn, cfg)

                    result = await _run_with_session(
                        platform, account_username, _scrape_target_connections_fn, cfg, conn, proxy_mgr, bot=bot
                    )
                    followers_available, followers = _connection_result_list(result, "followers")
                    following_available, following = _connection_result_list(result, "following")

                    events: list[dict] = []
                    if followers_available:
                        save_target_follow_snapshot(conn, target_id, "followers", followers)
                        events = diff_target_follow_snapshots(conn, target_id, "followers")
                    if platform != "facebook" and following_available:
                        save_target_follow_snapshot(conn, target_id, "following", following)
                        events.extend(diff_target_follow_snapshots(conn, target_id, "following"))
                    elif platform == "facebook" and following_available:
                        save_target_follow_snapshot(conn, target_id, "following", following)
                    if events:
                        insert_target_follow_events(conn, target_id, events)

                    update_fields = {
                        "last_scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    }
                    if followers_available:
                        update_fields["follower_count"] = len(followers)
                    if following_available:
                        update_fields["following_count"] = len(following)
                    upsert_target(conn, platform, target_username, **update_fields)
                    _rebuild_profile_for_target(conn, cfg, platform, target_username)
                    _update_proximity_scores(conn, target_id)
                    log_event(
                        "main",
                        "job_result",
                        job="followers",
                        scope="target",
                        platform=platform,
                        target=target_username,
                        followers=len(followers) if followers_available else "unavailable",
                        following=len(following) if following_available else "unavailable",
                        events=len(events),
                    )

                    log_scrape(
                        conn, "followers", "success", started_at,
                        platform=platform, target_id=target_id,
                        detail=(
                            f"{len(events)} target events, "
                            f"followers={'unavailable' if not followers_available else len(followers)}, "
                            f"following={'unavailable' if not following_available else len(following)}"
                        ),
                    )
                    if events:
                        await job_flush_notifications(cfg, conn, bot, kinds={"followers"})

                except Exception as e:
                    log_event(
                        "main",
                        "job_error",
                        level="error",
                        job="followers",
                        scope="target",
                        platform=platform,
                        target=target_username,
                        error=e,
                    )
                    log_scrape(
                        conn, "followers", "error", started_at,
                        platform=platform, target_id=target_id, detail=str(e),
                    )
                    if cfg.notifications.error_alerts and _should_send_error_alert(e):
                        await send_error(
                            bot,
                            cfg.notifications.chat_id,
                            f"scrape_followers_target/{platform}/{target_username}",
                            e,
                            message_thread_id=cfg.notifications.error_thread(),
                        )

        await _run_target_groups_concurrently(targets_batch, _run_group)


async def job_scrape_dms(
    cfg: Config,
    conn,
    bot,
    proxy_mgr: ProxyManager,
    selected_accounts: set[tuple[str, str]] | None = None,
    selected_targets: set[tuple[str, str]] | None = None,
    job_type: str = "dms",
) -> None:
    """Scrape owned-account DM history into account-level archives."""
    owned_account_keys = {
        (account.platform, account.username)
        for account in cfg.accounts
    }
    account_filters = set(selected_accounts or set())
    if selected_targets:
        account_filters.update({
            key
            for key in selected_targets
            if key in owned_account_keys
        })

    run_accounts = bool(account_filters) or (selected_accounts is None and selected_targets is None)
    if not run_accounts:
        return

    async def _run_account(account_cfg) -> None:
        platform = account_cfg.platform
        username = account_cfg.username
        started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        try:
            account_id = upsert_account(
                conn,
                platform,
                username,
                session_file=f"{platform}_{username}.json",
            )
            scraper = _resolve_dm_scraper(platform)

            async def _scrape_dms_fn(page, cfg):
                page = await _ensure_logged_in(page, platform, username, cfg)
                return await scraper(page, username, conn, cfg)

            conversations = await _run_with_session(
                platform,
                username,
                _scrape_dms_fn,
                cfg,
                conn,
                proxy_mgr,
                bot=bot,
            )

            saved_conversations = 0
            saved_messages = 0
            saved_attachments = 0
            for conversation in conversations or []:
                if conversation.get("is_broadcast_channel"):
                    continue
                conversation.setdefault("platform", platform)
                conversation_row_id = upsert_dm_conversation(conn, account_id, conversation)
                replace_dm_participants(
                    conn,
                    conversation_row_id,
                    conversation.get("participants") or [],
                )

                for index, message in enumerate(conversation.get("messages") or []):
                    message.setdefault("message_order", index)
                    message_row_id = upsert_dm_message(
                        conn,
                        conversation_row_id,
                        platform,
                        message,
                    )
                    attachments: list[dict] = []
                    if cfg.media.enabled:
                        attachments = _download_dm_attachments(
                            conn,
                            message_row_id,
                            platform,
                            username,
                            conversation["conversation_id"],
                            conversation.get("conversation_url"),
                            message,
                            cfg,
                        )
                    else:
                        replace_dm_message_media(conn, message_row_id, [])
                    saved_messages += 1
                    saved_attachments += len(attachments)

                saved_conversations += 1
                if cfg.digital_twin.enabled:
                    latest_inbound = None
                    for message in reversed(conversation.get("messages") or []):
                        if not message.get("is_self"):
                            latest_inbound = message
                            break
                    if latest_inbound:
                        from src.digital_twin import queue_inbound_reply_draft

                        try:
                            await queue_inbound_reply_draft(
                                platform=platform,
                                account_username=username,
                                conversation_id=conversation["conversation_id"],
                                bot=bot,
                                source_message_id=str(
                                    latest_inbound.get("message_id")
                                    or latest_inbound.get("message_key")
                                    or ""
                                ).strip() or None,
                            )
                        except Exception as exc:
                            log_event(
                                "main",
                                "digital_twin_reply_draft_failed",
                                level="warning",
                                platform=platform,
                                account=username,
                                conversation_id=conversation.get("conversation_id"),
                                error=exc,
                            )

            _rebuild_dm_archive_for_account(conn, cfg, platform, username)
            log_event(
                "main",
                "job_result",
                job=normalize_job_name(job_type),
                platform=platform,
                account=username,
                conversations=saved_conversations,
                messages=saved_messages,
                attachments=saved_attachments,
            )
            log_scrape(
                conn,
                job_type,
                "success",
                started_at,
                platform=platform,
                account_id=account_id,
                detail=(
                    f"conversations={saved_conversations}, "
                    f"messages={saved_messages}, attachments={saved_attachments}"
                ),
            )
        except Exception as e:
            log_event(
                "main",
                "job_error",
                level="error",
                job=normalize_job_name(job_type),
                platform=platform,
                account=username,
                error=e,
            )
            log_scrape(conn, job_type, "error", started_at, platform=platform, detail=str(e))
            if cfg.notifications.error_alerts:
                await send_error(
                    bot,
                    cfg.notifications.chat_id,
                    f"{job_type}/{platform}/{username}",
                    e,
                    message_thread_id=cfg.notifications.error_thread(),
                )

    accounts_batch = list(_iter_accounts(cfg, account_filters or None))
    if accounts_batch:
        await asyncio.gather(*(_run_account(account_cfg) for account_cfg in accounts_batch))


async def job_rebuild_profiles(
    cfg: Config,
    conn,
    selected_targets: set[tuple[str, str]] | None = None,
) -> None:
    """Rebuild profile.json for the selected targets."""
    from src.profile_builder import rebuild_all_profiles

    try:
        rebuild_all_profiles(conn, cfg.profiles_dir, list(_iter_targets(cfg, selected_targets)))
    except Exception as e:
        log_event("main", "job_error", level="error", job="profiles", error=e)


async def job_flush_notifications(
    cfg: Config,
    conn,
    bot,
    *,
    kinds: set[str] | None = None,
) -> None:
    """Drain pending Telegram notifications from the DB."""
    try:
        await flush_notifications(
            bot,
            cfg.notifications,
            conn,
            cfg.data_dir,
            kinds=kinds,
        )
    except Exception as e:
        log_event("main", "job_error", level="error", job="notifications", error=e)

    # Run vibe shift detection for all active targets (best-effort)
    if kinds is None:
        await _run_vibe_shift_checks(cfg, conn, bot)


async def _run_vibe_shift_checks(cfg: Config, conn, bot) -> None:
    """Check for mood/vibe shifts across all active targets and log alerts."""
    try:
        from src.intelligence import check_vibe_shift
        from src.db import insert_vibe_shift_alert, get_all_targets_with_stats
        threshold = getattr(getattr(cfg, "schedule", None), "vibe_shift_threshold", 2.5)
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        for target in get_all_targets_with_stats(conn):
            if not target.get("is_active") or target.get("is_paused"):
                continue
            target_id = target["id"]
            try:
                shifted, delta, direction = check_vibe_shift(conn, target_id, threshold=threshold)
                if shifted:
                    description = (
                        f"Mood shifted {direction} by {abs(delta):.1f} pts "
                        f"over the last 14 analyses"
                    )
                    insert_vibe_shift_alert(
                        conn, target_id, delta, direction, description, now_str
                    )
                    log_event(
                        "main",
                        "vibe_shift_detected",
                        target_id=target_id,
                        direction=direction,
                        delta=round(delta, 2),
                    )
            except Exception as e:
                log_event(
                    "main", "vibe_shift_check_error",
                    level="warning", target_id=target_id, error=e,
                )
    except Exception as e:
        log_event("main", "vibe_shift_checks_error", level="warning", error=e)


async def job_daily_digest(cfg: Config, conn, bot) -> None:
    """Send the morning digest message."""
    if not cfg.notifications.daily_digest:
        return
    try:
        data = get_daily_digest_data(conn)
        await send_daily_digest(
            bot,
            cfg.notifications.chat_id,
            data,
            message_thread_id=cfg.notifications.general_thread(),
        )
    except Exception as e:
        log_event("main", "job_error", level="error", job="daily_digest", error=e)
        if cfg.notifications.error_alerts:
            await send_error(
                bot,
                cfg.notifications.chat_id,
                "daily_digest",
                e,
                message_thread_id=cfg.notifications.error_thread(),
            )


async def run_initial_archive(
    cfg: Config,
    conn,
    bot,
    proxy_mgr: ProxyManager,
    selected_targets: set[tuple[str, str]] | None = None,
    force: bool = False,
    include_reposts: bool = False,
    notify_new_items_override: bool | None = None,
    replay_existing_items: bool = False,
) -> None:
    """Run one-time archive capture for newly added targets when the toggle is enabled."""
    if not force and not cfg.archive.run_initial_capture_on_startup:
        return

    notify_new_items = (
        False
        if notify_new_items_override is None
        else notify_new_items_override
    )
    pending_targets: list[TargetConfig] = []
    for target_cfg in _iter_targets(cfg, selected_targets):
        target_id = upsert_target(conn, target_cfg.platform, target_cfg.username)
        if not force and has_successful_scrape(
            conn, "initial_archive", target_id=target_id, platform=target_cfg.platform
        ):
            continue
        pending_targets.append(target_cfg)

    async def _run_group(group: list[TargetConfig]) -> None:
        for target_cfg in group:
            target_id = upsert_target(conn, target_cfg.platform, target_cfg.username)
            started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            counts: list[str] = []

            try:
                if target_cfg.check_posts:
                    saved = await _run_post_scrape_for_target(
                        target_cfg, cfg, conn, bot, proxy_mgr,
                        scrape_kind="posts",
                        job_type="bootstrap_posts",
                        notify_new_items=notify_new_items,
                        replay_existing_items=replay_existing_items,
                    )
                    counts.append(f"posts={saved}")
                    if include_reposts and _platform_supports_reposts(target_cfg.platform):
                        saved = await _run_repost_scrape_for_target(
                            target_cfg, cfg, conn, bot, proxy_mgr,
                            job_type="bootstrap_reposts",
                            notify_new_items=notify_new_items,
                            replay_existing_items=replay_existing_items,
                        )
                        counts.append(f"reposts={saved}")
                if target_cfg.check_mentions:
                    saved = await _run_post_scrape_for_target(
                        target_cfg, cfg, conn, bot, proxy_mgr,
                        scrape_kind="mentions",
                        job_type="bootstrap_mentions",
                        notify_new_items=notify_new_items,
                        replay_existing_items=replay_existing_items,
                    )
                    counts.append(f"mentions={saved}")
                if target_cfg.check_stories:
                    saved = await _run_story_scrape_for_target(
                        target_cfg, cfg, conn, bot, proxy_mgr,
                        scrape_kind="stories",
                        job_type="bootstrap_stories",
                        notify_new_items=notify_new_items,
                        replay_existing_items=replay_existing_items,
                    )
                    counts.append(f"stories={saved}")
                if target_cfg.check_highlights:
                    saved = await _run_story_scrape_for_target(
                        target_cfg, cfg, conn, bot, proxy_mgr,
                        scrape_kind="highlights",
                        job_type="bootstrap_highlights",
                        notify_new_items=notify_new_items,
                        replay_existing_items=replay_existing_items,
                    )
                    counts.append(f"highlights={saved}")

                await job_rebuild_profiles(
                    cfg, conn,
                    selected_targets={(target_cfg.platform, target_cfg.username)},
                )
                log_scrape(
                    conn, "initial_archive", "success", started_at,
                    platform=target_cfg.platform, target_id=target_id,
                    detail=", ".join(counts) if counts else "No archive jobs enabled",
                )
            except Exception as e:
                log_event(
                    "main",
                    "job_error",
                    level="error",
                    job="initial_archive",
                    platform=target_cfg.platform,
                    target=target_cfg.username,
                    error=e,
                )
                log_scrape(
                    conn,
                    "initial_archive",
                    "error",
                    started_at,
                    platform=target_cfg.platform,
                    target_id=target_id,
                    detail=str(e),
                )
                if cfg.notifications.error_alerts:
                    await send_error(
                        bot,
                        cfg.notifications.chat_id,
                        f"initial_archive/{target_cfg.platform}/{target_cfg.username}",
                        e,
                        message_thread_id=cfg.notifications.error_thread(),
                    )

    await _run_target_groups_concurrently(pending_targets, _run_group)


async def run_requested_jobs(
    action: str,
    cfg: Config,
    conn,
    bot,
    proxy_mgr: ProxyManager,
    *,
    selected_targets: set[tuple[str, str]] | None = None,
    selected_accounts: set[tuple[str, str]] | None = None,
    include_reposts_in_bootstrap: bool = False,
) -> None:
    """Run one or more jobs immediately, optionally filtered to a target/account subset."""
    manual_filtered_bootstrap = action == "bootstrap" and (
        selected_targets is not None or selected_accounts is not None
    )
    notify_new_items = action != "bootstrap"
    if action == "bootstrap":
        log_event(
            "main",
            "bootstrap_notice",
            action=action,
            telegram_notifications="suppressed",
            detail="only_future_updates_will_be_sent",
        )

    if action == "bootstrap":
        await run_initial_archive(
            cfg,
            conn,
            bot,
            proxy_mgr,
            selected_targets=selected_targets,
            force=True,
            include_reposts=include_reposts_in_bootstrap,
            notify_new_items_override=notify_new_items,
            replay_existing_items=manual_filtered_bootstrap,
        )
        await job_scrape_followers(
            cfg,
            conn,
            bot,
            proxy_mgr,
            selected_accounts=selected_accounts,
            selected_targets=selected_targets,
        )
        await job_scrape_dms(
            cfg,
            conn,
            bot,
            proxy_mgr,
            selected_accounts=selected_accounts,
            selected_targets=selected_targets,
            job_type="bootstrap_dms",
        )
    else:
        if action in {"posts", "all"}:
            await job_scrape_posts(cfg, conn, bot, proxy_mgr, selected_targets=selected_targets, notify_new_items=notify_new_items)
        if action in {"reposts", "all"}:
            await job_scrape_reposts(cfg, conn, bot, proxy_mgr, selected_targets=selected_targets, notify_new_items=notify_new_items)
        if action in {"mentions", "all"}:
            await job_scrape_mentions(cfg, conn, bot, proxy_mgr, selected_targets=selected_targets, notify_new_items=notify_new_items)
        if action in {"stories", "all"}:
            await job_scrape_stories(cfg, conn, bot, proxy_mgr, selected_targets=selected_targets, notify_new_items=notify_new_items)
        if action in {"highlights", "all"}:
            await job_scrape_highlights(cfg, conn, bot, proxy_mgr, selected_targets=selected_targets, notify_new_items=notify_new_items)
        if action in {"followers", "all"}:
            await job_scrape_followers(
                cfg,
                conn,
                bot,
                proxy_mgr,
                selected_accounts=selected_accounts,
                selected_targets=selected_targets,
            )
        if action in {"dms", "all"}:
            await job_scrape_dms(
                cfg,
                conn,
                bot,
                proxy_mgr,
                selected_accounts=selected_accounts,
                selected_targets=selected_targets,
            )
        if action in {"profiles", "all"}:
            await job_rebuild_profiles(cfg, conn, selected_targets=selected_targets)

    if notify_new_items and action in {
        "posts", "reposts", "mentions", "stories", "highlights", "followers", "all", "bootstrap",
    }:
        notification_kinds: set[str] | None = None
        if action in {"posts", "reposts", "mentions"}:
            notification_kinds = {"posts"}
        elif action in {"stories", "highlights"}:
            notification_kinds = {"stories"}
        elif action == "followers":
            notification_kinds = {"followers"}
        await job_flush_notifications(cfg, conn, bot, kinds=notification_kinds)


# ─── Error listener ──────────────────────────────────────────────────────────

def _make_error_listener(bot, cfg: Config):
    def _listener(event: JobExecutionEvent):
        if event.exception:
            logger.error(f"[scheduler] Job {event.job_id} crashed: {event.exception}")
            if cfg.notifications.error_alerts:
                asyncio.ensure_future(
                    send_error(
                        bot,
                        cfg.notifications.chat_id,
                        f"scheduler/{event.job_id}",
                        event.exception,
                        message_thread_id=cfg.notifications.error_thread(),
                    )
                )
    return _listener


# ─── Scheduler builder ───────────────────────────────────────────────────────

_SCRAPING_JOB_IDS = {
    "scrape_posts", "scrape_reposts", "scrape_stories",
    "scrape_highlights", "scrape_mentions", "scrape_followers",
}


def _pause_scraping_jobs(scheduler) -> None:
    """Pause all periodic scraping jobs while keeping flush/digest running."""
    for job_id in _SCRAPING_JOB_IDS:
        job = scheduler.get_job(job_id)
        if job:
            job.pause()


def _resume_scraping_jobs(scheduler) -> None:
    """Resume all scraping jobs that exist in the scheduler."""
    for job_id in _SCRAPING_JOB_IDS:
        job = scheduler.get_job(job_id)
        if job:
            job.resume()


def _suppress_pending_notifications(conn) -> int:
    """
    On startup, mark all currently-pending notifications as sent.
    Prevents a flood of stale Telegram messages when flush_notifications first runs.
    Returns the total number of rows silenced.
    """
    tables = [
        "posts", "stories", "follower_events", "target_follow_events",
        "target_profile_history", "target_avatar_history", "vibe_shift_alerts",
    ]
    total = 0
    for table in tables:
        try:
            cur = conn.execute(f"UPDATE {table} SET notified=1 WHERE notified=0")
            total += cur.rowcount
        except Exception:
            pass
    conn.commit()
    return total


def _trigger_stories_immediately(scheduler) -> bool:
    """
    Advance the stories job's next_run_time to now so it fires on the next
    scheduler tick instead of waiting for the full interval.
    Does nothing if the job doesn't exist or is paused.
    """
    job = scheduler.get_job("scrape_stories")
    if not job or job.next_run_time is None:
        return False
    scheduler.modify_job("scrape_stories", next_run_time=datetime.now(timezone.utc))
    return True


def _reschedule_job(scheduler, job_id: str, minutes: int, cfg: Config, conn, bot, proxy_mgr: ProxyManager) -> bool:
    """Reschedule a job with a new interval (minutes). Returns True if job exists."""
    job = scheduler.get_job(job_id)
    if not job:
        return False
    was_paused = job.next_run_time is None
    job.reschedule(trigger=IntervalTrigger(minutes=minutes, jitter=min(60, minutes // 4)))
    if was_paused:
        job.pause()
    return True


def _db_interval(conn, key: str, fallback_minutes: int) -> int:
    """Return interval from scanner_config, falling back to the given default."""
    from src.db import get_config
    val = get_config(conn, f"{key}_interval_minutes")
    if val is not None:
        try:
            return max(1, int(val))
        except ValueError:
            pass
    return fallback_minutes


def _db_flag(conn, key: str, default: bool = True) -> bool:
    """Return boolean flag from scanner_config."""
    from src.db import get_config
    val = get_config(conn, f"{key}_enabled")
    if val is not None:
        return val.strip() == "1"
    return default


def build_scheduler(cfg: Config, conn, bot, proxy_mgr: ProxyManager) -> AsyncIOScheduler:
    """
    Build the APScheduler with intervals from scanner_config DB (falls back to
    settings.yaml). The scheduler is ALWAYS started by the caller; whether it is
    paused (is_scanning=0) or running (is_scanning=1) is handled in async_main().
    """
    scheduler = AsyncIOScheduler()
    monitored_targets = _configured_targets(cfg)

    posts_interval = _db_interval(conn, "posts", cfg.schedule.posts_interval_minutes)
    stories_interval = _db_interval(conn, "stories", cfg.schedule.stories_interval_minutes)
    followers_interval = _db_interval(conn, "followers", cfg.schedule.followers_interval_minutes)
    highlights_interval = _db_interval(conn, "highlights", cfg.schedule.stories_interval_minutes)
    mentions_interval = _db_interval(conn, "mentions", cfg.schedule.posts_interval_minutes)

    if _db_flag(conn, "posts"):
        scheduler.add_job(
            job_scrape_posts,
            trigger=IntervalTrigger(minutes=posts_interval, jitter=60),
            args=[cfg, conn, bot, proxy_mgr],
            id="scrape_posts",
            name="Scrape new posts",
            max_instances=1,
        )
    if _db_flag(conn, "reposts") and any(
        t.check_posts and _platform_supports_reposts(t.platform) for t in monitored_targets
    ):
        reposts_interval = _db_interval(conn, "reposts", posts_interval)
        scheduler.add_job(
            job_scrape_reposts,
            trigger=IntervalTrigger(minutes=reposts_interval, jitter=60),
            args=[cfg, conn, bot, proxy_mgr],
            id="scrape_reposts",
            name="Scrape repost feeds",
            max_instances=1,
        )
    if _db_flag(conn, "stories"):
        scheduler.add_job(
            job_scrape_stories,
            trigger=IntervalTrigger(minutes=stories_interval, jitter=60),
            args=[cfg, conn, bot, proxy_mgr],
            id="scrape_stories",
            name="Scrape new stories",
            max_instances=1,
        )
    if _db_flag(conn, "highlights") and any(t.check_highlights for t in monitored_targets):
        scheduler.add_job(
            job_scrape_highlights,
            trigger=IntervalTrigger(minutes=highlights_interval, jitter=90),
            args=[cfg, conn, bot, proxy_mgr],
            id="scrape_highlights",
            name="Scrape highlight collections",
            max_instances=1,
        )
    if _db_flag(conn, "mentions") and any(t.check_mentions for t in monitored_targets):
        scheduler.add_job(
            job_scrape_mentions,
            trigger=IntervalTrigger(minutes=mentions_interval, jitter=60),
            args=[cfg, conn, bot, proxy_mgr],
            id="scrape_mentions",
            name="Scrape mention feeds",
            max_instances=1,
        )
    if _db_flag(conn, "followers"):
        scheduler.add_job(
            job_scrape_followers,
            trigger=IntervalTrigger(minutes=followers_interval, jitter=60),
            args=[cfg, conn, bot, proxy_mgr],
            id="scrape_followers",
            name="Scrape followers/following",
            max_instances=1,
        )
    if cfg.accounts:
        scheduler.add_job(
            job_scrape_dms,
            trigger=IntervalTrigger(minutes=cfg.schedule.dms_interval_minutes, jitter=90),
            args=[cfg, conn, bot, proxy_mgr],
            id="scrape_dms",
            name="Scrape direct messages",
            max_instances=1,
        )
    scheduler.add_job(
        job_rebuild_profiles,
        trigger=IntervalTrigger(
            hours=cfg.schedule.profile_rebuild_interval_hours, jitter=120
        ),
        args=[cfg, conn],
        id="rebuild_profiles",
        name="Rebuild profile JSON files",
        max_instances=1,
    )
    scheduler.add_job(
        job_flush_notifications,
        trigger=IntervalTrigger(minutes=2),
        args=[cfg, conn, bot],
        id="flush_notifications",
        name="Flush pending Telegram notifications",
        max_instances=1,
    )

    if cfg.notifications.daily_digest:
        h, m = cfg.schedule.daily_digest_time.split(":")
        scheduler.add_job(
            job_daily_digest,
            trigger=CronTrigger(hour=int(h), minute=int(m)),
            args=[cfg, conn, bot],
            id="daily_digest",
            name="Daily digest",
        )

    scheduler.add_listener(_make_error_listener(bot, cfg), EVENT_JOB_ERROR)
    return scheduler


async def interactive_login(account_selector: str, cfg: Config,
                            proxy_mgr: ProxyManager) -> None:
    """
    Launch a headed browser for one-time manual authentication and save the session.
    Useful when headless login is blocked by anti-automation challenges.
    """
    from src.browser.stealth import (
        close_browser,
        launch_browser,
        profile_dir_for,
        save_session,
        session_path_for,
    )

    platform, sep, username = account_selector.partition(":")
    platform = platform.strip().lower()
    username = username.strip()
    if not sep or not platform or not username:
        raise ValueError(
            f"Invalid interactive login selector {account_selector!r}. Use platform:username."
        )

    account = next(
        (a for a in cfg.accounts if a.platform == platform and a.username == username),
        None,
    )
    if not account:
        raise ValueError(f"Account not found in config: {platform}/{username}")

    proxy_url = proxy_mgr.get_for_account(account)
    session_path = session_path_for(cfg.sessions_dir, platform, username)
    profile_dir = profile_dir_for(cfg.sessions_dir, platform, username)

    login_urls = {
        "instagram": "https://www.instagram.com/accounts/login/",
        "tiktok": "https://www.tiktok.com/login/phone-or-email/email",
        "facebook": "https://www.facebook.com/login",
    }
    login_url = login_urls.get(platform)
    if not login_url:
        raise ValueError(f"Interactive login is not supported for platform {platform!r}")

    playwright, browser, context, profile_lock_path = await launch_browser(
        proxy_url=proxy_url,
        headless=False,
        session_path=session_path,
        profile_dir=profile_dir,
    )

    try:
        page = await _interactive_login_page(context)
        await _close_redundant_blank_pages(context, keep_page=page)
        await page.goto(login_url, wait_until="domcontentloaded", timeout=30_000)
        log_event(
            "main",
            "interactive_login_waiting",
            platform=platform,
            account=username,
            detail="complete_login_in_opened_browser_window",
        )

        timeout_seconds = 15 * 60
        started_at = asyncio.get_running_loop().time()
        facebook_nudge_attempted = False
        chrome_cookie_fallback_attempted = False
        while asyncio.get_running_loop().time() - started_at < timeout_seconds:
            if await _manual_login_complete(context, platform):
                await save_session(
                    context,
                    session_path,
                    platform=platform,
                    preserve_existing_if_missing_auth=False,
                )
                log_event(
                    "main",
                    "interactive_login_saved",
                    platform=platform,
                    account=username,
                    path=session_path,
                )
                return
            if (
                platform == "facebook"
                and not facebook_nudge_attempted
                and asyncio.get_running_loop().time() - started_at >= 20
                and await _facebook_login_flow_stalled(context)
            ):
                facebook_nudge_attempted = True
                await _facebook_nudge_stalled_login(context)
            if (
                platform == "facebook"
                and not chrome_cookie_fallback_attempted
                and asyncio.get_running_loop().time() - started_at >= 45
                and await _facebook_login_flow_stalled(context)
            ):
                chrome_cookie_fallback_attempted = True
                if _try_import_cookies_from_chrome(account_selector, cfg):
                    log_event(
                        "main",
                        "interactive_login_saved",
                        platform=platform,
                        account=username,
                        path=session_path,
                        detail="imported_from_real_chrome",
                    )
                    return
            await asyncio.sleep(2)

        raise TimeoutError(
            f"Timed out waiting for manual login completion for {platform}/{username}"
        )
    finally:
        await close_browser(playwright, browser, profile_lock_path)


def _import_cookies_from_chrome(account_selector: str, cfg: "Config") -> None:
    """
    Read Facebook session cookies from the user's real Chrome and write them into the
    scraper session file.  Chrome's Cookies SQLite is read via browser-cookie3 (which
    handles macOS Keychain decryption automatically — you may see a Keychain access
    dialog; click Allow).

    Usage:  python3 src/main.py --import-cookies facebook:vojtech.ponrt
    """
    try:
        import browser_cookie3  # type: ignore
    except ImportError:
        raise RuntimeError(
            "browser-cookie3 is not installed. Run:\n"
            "  pip install browser-cookie3"
        )

    platform, sep, username = account_selector.partition(":")
    platform = platform.strip().lower()
    username = username.strip()
    if not sep or not platform or not username:
        raise ValueError(
            f"Invalid selector {account_selector!r}. Use platform:username, e.g. facebook:vojtech.ponrt"
        )

    _REQUIRED: dict[str, set[str]] = {
        "facebook": {"c_user", "xs"},
    }
    if platform not in _REQUIRED:
        raise ValueError(f"--import-cookies is only supported for: {', '.join(_REQUIRED)}")

    domain_filter = f".{platform}.com"
    log_event("main", "import_cookies_start", platform=platform, account=username)

    try:
        jar = browser_cookie3.chrome(domain_name=domain_filter)
    except Exception as exc:
        raise RuntimeError(
            f"Could not read Chrome cookies for {domain_filter}: {exc}\n"
            "Make sure Chrome is installed and you are logged in to Facebook in Chrome."
        ) from exc

    # Convert http.cookiejar.Cookie → Playwright cookie dict
    playwright_cookies: list[dict] = []
    for c in jar:
        playwright_cookies.append({
            "name": c.name,
            "value": c.value,
            "domain": c.domain if c.domain.startswith(".") else f".{c.domain}",
            "path": c.path or "/",
            "expires": int(c.expires) if c.expires else -1,
            "httpOnly": bool(getattr(c, "_rest", {}).get("HttpOnly") is not None),
            "secure": bool(c.secure),
            "sameSite": "None",
        })

    found_names = {c["name"] for c in playwright_cookies}
    missing = _REQUIRED[platform] - found_names
    if missing:
        raise RuntimeError(
            f"Required auth cookies not found in Chrome for {platform}: {missing}.\n"
            f"Make sure you are logged in to {platform}.com in your normal Chrome."
        )

    from src.browser.stealth import session_path_for
    session_path = session_path_for(cfg.sessions_dir, platform, username)
    session_path.parent.mkdir(parents=True, exist_ok=True)

    # Preserve existing localStorage / origins if present
    existing: dict = {}
    if session_path.exists():
        try:
            existing = json.loads(session_path.read_text())
        except Exception:
            existing = {}

    existing["cookies"] = playwright_cookies
    with open(session_path, "w") as f:
        json.dump(existing, f)

    log_event(
        "main",
        "import_cookies_done",
        platform=platform,
        account=username,
        cookie_count=len(playwright_cookies),
        path=str(session_path),
    )
    logger.info(
        f"[import-cookies] Imported {len(playwright_cookies)} cookies → {session_path}. "
        "You can now run bootstrap without --interactive-login."
    )


def _try_import_cookies_from_chrome(account_selector: str, cfg: "Config") -> bool:
    """Best-effort cookie import used as a Facebook recovery fallback."""
    try:
        _import_cookies_from_chrome(account_selector, cfg)
        return True
    except Exception as exc:
        log_event(
            "main",
            "import_cookies_failed",
            level="warning",
            selector=account_selector,
            error=exc,
        )
        return False


async def _interactive_login_page(context):
    """Reuse the existing blank tab in a persistent context when possible."""
    pages = list(getattr(context, "pages", []) or [])
    if not pages:
        return await context.new_page()

    blank_urls = {"", "about:blank", "chrome://newtab/", "chrome://new-tab-page/"}
    for page in pages:
        if (getattr(page, "url", "") or "").strip().lower() in blank_urls:
            return page
    return pages[0]


async def _close_redundant_blank_pages(context, *, keep_page) -> None:
    """Close extra startup tabs so manual login stays in a single visible tab."""
    blank_urls = {"", "about:blank", "chrome://newtab/", "chrome://new-tab-page/"}
    for page in list(getattr(context, "pages", []) or []):
        if page is keep_page:
            continue
        if (getattr(page, "url", "") or "").strip().lower() not in blank_urls:
            continue
        try:
            await page.close()
        except Exception:
            continue


async def _facebook_login_flow_stalled(context) -> bool:
    """Detect Facebook login/checkpoint pages that have stopped making progress."""
    for page in list(getattr(context, "pages", []) or []):
        url = (getattr(page, "url", "") or "").lower()
        if "facebook.com" not in url:
            continue

        try:
            body_text = await page.locator("body").inner_text()
        except Exception:
            body_text = ""

        normalized = " ".join((body_text or "").lower().split())
        if "/login" in url and "log in to facebook" in normalized and "forgotten password" in normalized:
            return True
        if "two_step_verification" not in url and "checkpoint" not in url:
            continue
        if not normalized:
            return True
        if normalized in {"facebook", "from meta", "facebook from meta"}:
            return True
        if len(normalized) < 40 and "meta" in normalized and "facebook" in normalized:
            return True
    return False


async def _facebook_nudge_stalled_login(context) -> None:
    """Force Facebook off a forever-spinning login/checkpoint page."""
    for page in list(getattr(context, "pages", []) or []):
        url = (getattr(page, "url", "") or "").lower()
        if "facebook.com" not in url:
            continue
        if not any(token in url for token in ("/login", "two_step_verification", "checkpoint")):
            continue
        try:
            await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=15_000)
        except Exception:
            continue


async def _manual_login_complete(context, platform: str) -> bool:
    """Detect whether a manual login finished by inspecting auth cookies."""
    cookies = await context.cookies()
    cookie_names = {
        cookie.get("name")
        for cookie in cookies
        if isinstance(cookie, dict) and str(cookie.get("value") or "").strip()
    }

    if platform == "instagram":
        return "sessionid" in cookie_names
    if platform == "facebook":
        if not {"c_user", "xs"}.issubset(cookie_names):
            return False
        from src.scrapers.facebook import check_login_state

        probe_page = await context.new_page()
        try:
            return await check_login_state(probe_page)
        finally:
            await probe_page.close()
    if platform == "tiktok":
        real_auth = {"sessionid", "sessionid_ss", "sid_guard", "sid_tt", "uid_tt", "uid_tt_ss"}
        if not (cookie_names & real_auth):
            return False
        from src.scrapers.tiktok import check_login_state

        probe_page = await context.new_page()
        try:
            return await check_login_state(probe_page)
        finally:
            await probe_page.close()
    return False


def _cleanup_legacy_archive_dir(path: Path) -> None:
    """Remove old sidecar files from an archive directory if they exist."""
    for filename in ("post.json", "caption.txt", "story.json", "story_text.txt", ".DS_Store"):
        try:
            (path / filename).unlink(missing_ok=True)
        except Exception:
            continue


def _prune_empty_directories(path: Path, stop_at: Path) -> None:
    """Delete empty directories up to, but not including, stop_at."""
    current = path.resolve()
    stop = stop_at.resolve()
    if current == stop or stop not in current.parents:
        return

    while current != stop and stop in current.parents:
        ds_store = current / ".DS_Store"
        if ds_store.exists():
            ds_store.unlink(missing_ok=True)
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _move_media_file_to_archive(
    conn,
    media_file_id: int,
    source_path: Path,
    dest_path: Path,
    *,
    stored_dest_path: str,
) -> None:
    """Move one saved asset to its canonical archive location and update SQLite."""
    source_abs = source_path.resolve()
    dest_abs = dest_path.resolve()
    dest_abs.parent.mkdir(parents=True, exist_ok=True)

    if source_abs != dest_abs:
        if source_abs.exists():
            if dest_abs.exists():
                source_abs.unlink(missing_ok=True)
            else:
                shutil.move(str(source_abs), str(dest_abs))
        elif not dest_abs.exists():
            logger.warning(
                f"[main] Missing media file during storage migration: {stored_dest_path}"
            )

    update_media_file(conn, media_file_id, file_path=stored_dest_path)


def migrate_storage_layout(cfg: Config, conn) -> None:
    """Move existing archives into the bucketed layout and rewrite sidecar metadata."""
    profiles_dir = cfg.data_dir / "profiles"
    media_root = _media_root_path(cfg)
    moved_rows = 0
    rewritten_sidecars = 0
    touched_targets: set[tuple[str, str]] = set()

    post_rows = conn.execute("""
        SELECT p.*, t.username AS target_username
        FROM posts p
        JOIN targets t ON t.id = p.target_id
        ORDER BY p.id
    """).fetchall()
    for row in post_rows:
        row_data = dict(row)
        target_key = (row_data["platform"], row_data["target_username"])
        touched_targets.add(target_key)

        archive_dir = _post_archive_dir(cfg, row_data)
        legacy_dir = _legacy_post_archive_dir(cfg, row_data)
        media_rows = get_post_media_records(conn, row_data["id"])
        source_dirs: set[Path] = set()

        for media_row in media_rows:
            source_abs = _resolve_saved_media_path(media_row["file_path"])
            source_dirs.add(source_abs.parent)
            stored_dest = str(archive_dir / source_abs.name)
            dest_abs = _resolve_saved_media_path(stored_dest)
            if media_row["file_path"] != stored_dest:
                moved_rows += 1
            _move_media_file_to_archive(
                conn,
                media_row["id"],
                source_abs,
                dest_abs,
                stored_dest_path=stored_dest,
            )

        _write_post_archive_metadata(conn, row_data["id"], row_data, cfg)
        rewritten_sidecars += 1

        legacy_abs = _resolve_saved_media_path(str(legacy_dir))
        if legacy_abs != _resolve_saved_media_path(str(archive_dir)):
            _cleanup_legacy_archive_dir(legacy_abs)
            _prune_empty_directories(legacy_abs, media_root)
        for source_dir in source_dirs:
            if source_dir != _resolve_saved_media_path(str(archive_dir)):
                _cleanup_legacy_archive_dir(source_dir)
                _prune_empty_directories(source_dir, media_root)

    story_rows = conn.execute("""
        SELECT s.*, t.username AS target_username
        FROM stories s
        JOIN targets t ON t.id = s.target_id
        ORDER BY s.id
    """).fetchall()
    for row in story_rows:
        row_data = dict(row)
        target_key = (row_data["platform"], row_data["target_username"])
        touched_targets.add(target_key)

        archive_dir = _story_archive_dir(cfg, row_data)
        legacy_dir = _legacy_story_archive_dir(cfg, row_data)
        media_rows = get_story_media_records(conn, row_data["id"])
        source_dirs: set[Path] = set()

        for media_row in media_rows:
            source_abs = _resolve_saved_media_path(media_row["file_path"])
            source_dirs.add(source_abs.parent)
            stored_dest = str(archive_dir / source_abs.name)
            dest_abs = _resolve_saved_media_path(stored_dest)
            if media_row["file_path"] != stored_dest:
                moved_rows += 1
            _move_media_file_to_archive(
                conn,
                media_row["id"],
                source_abs,
                dest_abs,
                stored_dest_path=stored_dest,
            )

        _write_story_archive_metadata(conn, row_data["id"], row_data, cfg)
        rewritten_sidecars += 1

        legacy_abs = _resolve_saved_media_path(str(legacy_dir))
        if legacy_abs != _resolve_saved_media_path(str(archive_dir)):
            _cleanup_legacy_archive_dir(legacy_abs)
            _prune_empty_directories(legacy_abs, media_root)
        for source_dir in source_dirs:
            if source_dir != _resolve_saved_media_path(str(archive_dir)):
                _cleanup_legacy_archive_dir(source_dir)
                _prune_empty_directories(source_dir, media_root)

    for platform, username in sorted(touched_targets):
        _ensure_profile_archive_dirs(cfg, platform, username)
        _rebuild_profile_for_target(conn, cfg, platform, username)

    log_event(
        "main",
        "storage_migration_result",
        moved_rows=moved_rows,
        rewritten_sidecars=rewritten_sidecars,
        touched_profiles=len(touched_targets),
    )


async def resend_post_notification(post_selector: str, cfg: Config, conn, bot) -> None:
    """Replay one archived post notification to Telegram without mutating DB state."""
    platform, username, post_id = _parse_post_selector(post_selector)

    post_row = conn.execute("""
        SELECT p.*, t.username AS target_username
        FROM posts p
        JOIN targets t ON t.id = p.target_id
        WHERE p.platform=? AND t.username=? AND p.post_id=?
    """, (platform, username, post_id)).fetchone()
    if not post_row:
        raise ValueError(f"Archived post not found: {platform}/{username}/{post_id}")

    media_rows = get_post_media_files(conn, post_row["id"])
    media_paths = [
        _resolve_saved_media_path(row["file_path"])
        for row in media_rows
    ]

    await send_new_post(
        bot,
        cfg.notifications.chat_id,
        post_row,
        media_paths=media_paths,
        message_thread_id=cfg.notifications.thread_id_for_post(
            post_row["platform"],
            post_row["content_scope"],
        ),
    )
    log_event(
        "main",
        "notification_resent",
        platform=platform,
        target=username,
        item_kind="post",
        item_id=post_id,
    )


def repair_missing_story_media(
    cfg: Config,
    conn,
    *,
    selected_targets: set[tuple[str, str]] | None = None,
) -> tuple[int, int]:
    """Backfill story/highlight files from stored raw metadata when media rows are missing."""
    sql = """
        SELECT s.*, t.username AS target_username
        FROM stories s
        JOIN targets t ON t.id = s.target_id
        WHERE NOT EXISTS (
            SELECT 1
            FROM media_files mf
            WHERE mf.story_id=s.id
              AND mf.file_type IN ('photo', 'video', 'screenshot')
        )
        ORDER BY s.id ASC
    """
    rows = conn.execute(sql).fetchall()
    repaired = 0
    failed = 0
    touched_targets: set[tuple[str, str]] = set()

    for row in rows:
        row_data = dict(row)
        target_key = (row_data["platform"], row_data["target_username"])
        if selected_targets and target_key not in selected_targets:
            continue

        story = {
            "platform": row_data["platform"],
            "story_id": row_data["story_id"],
            "story_type": row_data["story_type"],
            "content_scope": row_data["content_scope"],
            "collection_name": row_data["collection_name"],
            "url": row_data["url"],
            "posted_at": row_data["posted_at"],
            "expires_at": row_data["expires_at"],
            "discovered_at": row_data["discovered_at"],
            "target_username": row_data["target_username"],
            "metadata": _story_metadata_dict(row_data.get("metadata_json")),
        }
        if not _story_media_candidates_for_download(story):
            failed += 1
            continue

        _download_story_media(conn, row_data["id"], story, cfg)
        success = _ensure_story_media_backfilled(conn, row_data["id"], story, cfg)
        _write_story_archive_metadata(conn, row_data["id"], story, cfg)
        touched_targets.add(target_key)
        if success:
            repaired += 1
        else:
            failed += 1

    for platform, username in sorted(touched_targets):
        _rebuild_profile_for_target(conn, cfg, platform, username)

    log_event(
        "main",
        "repair_result",
        job="story_media",
        repaired=repaired,
        failed=failed,
        targets=len(touched_targets),
    )
    return repaired, failed


def repair_missing_post_media(
    cfg: Config,
    conn,
    *,
    selected_targets: set[tuple[str, str]] | None = None,
) -> tuple[int, int]:
    """Backfill post/mention/repost files from archived post sidecars when media is missing."""
    sql = """
        SELECT p.*, t.username AS target_username
        FROM posts p
        JOIN targets t ON t.id = p.target_id
        ORDER BY p.id ASC
    """
    rows = conn.execute(sql).fetchall()
    repaired = 0
    failed = 0
    touched_targets: set[tuple[str, str]] = set()

    for row in rows:
        row_data = dict(row)
        target_key = (row_data["platform"], row_data["target_username"])
        if selected_targets and target_key not in selected_targets:
            continue

        sidecar_path = _post_archive_dir(cfg, row_data) / "post.json"
        if not sidecar_path.exists():
            continue

        try:
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(
                f"[main] Could not parse post sidecar for media repair: {sidecar_path}: {exc}"
            )
            failed += 1
            continue

        post = {
            "platform": row_data["platform"],
            "post_id": row_data["post_id"],
            "post_type": row_data["post_type"],
            "content_scope": row_data["content_scope"],
            "collection_name": row_data["collection_name"],
            "url": row_data["url"],
            "caption": row_data["caption"],
            "posted_at": row_data["posted_at"],
            "discovered_at": row_data["discovered_at"],
            "like_count": row_data["like_count"],
            "comment_count": row_data["comment_count"],
            "share_count": row_data["share_count"],
            "view_count": row_data["view_count"],
            "is_repost": bool(row_data["is_repost"]),
            "repost_source_url": row_data["repost_source_url"],
            "repost_source_account": row_data["repost_source_account"],
            "target_username": row_data["target_username"],
            "owner": sidecar.get("owner"),
            "media_items": sidecar.get("media_items") or [],
            "raw_metadata": sidecar.get("raw_metadata"),
        }

        expected_items = _normalise_post_media_items(post)
        if not expected_items:
            continue

        existing_rows = get_post_media_records(conn, row_data["id"])
        primary_rows = [record for record in existing_rows if record["file_type"] in {"photo", "video"}]
        expected_prefix = _post_media_storage_prefix(cfg, post)
        has_wrong_location = any(
            not Path(record["file_path"]).as_posix().startswith(expected_prefix)
            for record in primary_rows
        )
        has_unusable_primary_files = any(
            not _saved_media_file_is_usable(record["file_path"])
            for record in primary_rows
        )
        has_low_resolution_primary_photos = any(
            _post_media_record_looks_low_resolution(record)
            for record in primary_rows
        )
        needs_repair = (
            len(primary_rows) != len(expected_items)
            or has_wrong_location
            or has_unusable_primary_files
            or has_low_resolution_primary_photos
        )
        if not needs_repair:
            continue

        _repair_post_media_if_needed(conn, row_data["id"], post, cfg)
        _write_post_archive_metadata(conn, row_data["id"], post, cfg)
        touched_targets.add(target_key)

        repaired_rows = get_post_media_records(conn, row_data["id"])
        repaired_primary_rows = [
            record for record in repaired_rows if record["file_type"] in {"photo", "video"}
        ]
        if len(repaired_primary_rows) >= len(expected_items):
            repaired += 1
        else:
            failed += 1

    for platform, username in sorted(touched_targets):
        _rebuild_profile_for_target(conn, cfg, platform, username)

    log_event(
        "main",
        "repair_result",
        job="post_media",
        repaired=repaired,
        failed=failed,
        targets=len(touched_targets),
    )
    return repaired, failed


# ─── Entry point ─────────────────────────────────────────────────────────────

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the social-scanner scheduler or one-shot jobs.")
    parser.add_argument(
        "--run-now",
        choices=["posts", "reposts", "stories", "mentions", "highlights", "followers", "dms", "profiles", "all", "bootstrap"],
        help="Run selected job(s) immediately before exiting.",
    )
    parser.add_argument(
        "--with-reposts",
        action="store_true",
        help="Include repost scraping during bootstrap runs. Bootstrap skips reposts by default.",
    )
    parser.add_argument(
        "--target",
        action="append",
        help="Limit target jobs to a specific monitored target in username or platform:username format. Repeatable.",
    )
    parser.add_argument(
        "--target-from",
        "--from",
        dest="target_from",
        help="Limit target jobs to the ordered target range starting at this username or platform:username.",
    )
    parser.add_argument(
        "--target-to",
        "--to",
        dest="target_to",
        help="Limit target jobs to the ordered target range ending at this username or platform:username.",
    )
    parser.add_argument(
        "--account",
        action="append",
        help="Limit account-level jobs (followers, dms) to a specific own account in platform:username format. Repeatable.",
    )
    parser.add_argument(
        "--platform",
        action="append",
        help="Limit commands to one or more platforms, for example instagram, tiktok, or facebook. Repeatable.",
    )
    parser.add_argument(
        "--profile-suffix",
        "--parallel",
        dest="profile_suffix",
        help="Use an isolated browser profile slot for parallel-safe one-off runs, for example slot-a or slot-b.",
    )
    parser.add_argument(
        "--keep-running",
        action="store_true",
        help="With one-shot commands, continue into scheduler mode after the immediate action completes.",
    )
    parser.add_argument(
        "--interactive-login",
        help="Open a headed browser and save a session for the given account in platform:username format.",
    )
    parser.add_argument(
        "--import-cookies",
        help=(
            "Read Facebook session cookies from your real Chrome and save them as the scraper session. "
            "Format: platform:username  (e.g. facebook:vojtech.ponrt). "
            "Make sure you are logged in to Facebook in Chrome before running this."
        ),
    )
    parser.add_argument(
        "--resend-post",
        help="Re-send one archived post notification to Telegram in platform:username:post_id format.",
    )
    parser.add_argument(
        "--migrate-storage-layout",
        action="store_true",
        help="Move existing saved media into the bucketed posts/reposts/stories/mentions/story_highlights layout.",
    )
    parser.add_argument(
        "--repair-missing-story-media",
        action="store_true",
        help="Backfill story/highlight files from stored raw media URLs when the DB row has no saved media.",
    )
    parser.add_argument(
        "--repair-missing-post-media",
        action="store_true",
        help="Backfill post/mention/repost files from archived post sidecars when media is missing or incomplete.",
    )
    parser.add_argument(
        "--repair-missing-media",
        action="store_true",
        help="Run both post and story media repair passes before normal execution.",
    )
    return parser.parse_args(argv)


async def async_main(args: argparse.Namespace) -> None:
    config_path = Path("settings.yaml")
    env_path = Path(".env.local") if Path(".env.local").exists() else Path(".env")

    cfg = load_config(config_path, env_path)
    selected_platforms = _parse_platform_filters(args.platform)
    cfg = _filter_config_by_platforms(cfg, selected_platforms)
    explicit_targets = _parse_target_selectors(cfg, args.target)
    selected_accounts = _parse_selectors(args.account, "account")
    cfg.browser_profile_suffix = (args.profile_suffix or "").strip()
    ranged_targets = _select_targets_by_range(cfg, args.target_from, args.target_to)
    selected_targets = _combine_target_filters(explicit_targets, ranged_targets)
    _setup_logging(cfg)

    log_event("main", "run_start", mode=args.run_now or "scheduler")
    if cfg.browser_profile_suffix:
        log_event(
            "main",
            "run_option",
            option="browser_profile_suffix",
            value=cfg.browser_profile_suffix,
        )
    if selected_platforms:
        log_event(
            "main",
            "platform_filter",
            platforms=",".join(sorted(selected_platforms)),
            configured_accounts=len(cfg.accounts),
            configured_targets=len(cfg.targets),
        )
    if ranged_targets is not None:
        log_event(
            "main",
            "target_filter",
            mode="range",
            selected_profiles=len(selected_targets or set()),
        )

    conn = init_db(cfg.db_path)
    monitored_targets = _configured_targets(cfg)
    platform_target_filters = (
        {(target.platform, target.username) for target in monitored_targets}
        if selected_platforms
        else None
    )

    # Seed own accounts into DB
    for acc in cfg.accounts:
        upsert_account(
            conn, acc.platform, acc.username,
            session_file=f"{acc.platform}_{acc.username}.json"
        )

    for target in monitored_targets:
        upsert_target(conn, target.platform, target.username)

    if args.migrate_storage_layout:
        migrate_storage_layout(cfg, conn)
        if (
            not args.keep_running
            and not args.run_now
            and not args.resend_post
            and not args.repair_missing_post_media
            and not args.repair_missing_story_media
            and not args.repair_missing_media
        ):
            conn.close()
            return

    if args.repair_missing_post_media or args.repair_missing_media:
        repair_missing_post_media(
            cfg,
            conn,
            selected_targets=selected_targets or platform_target_filters,
        )
        if (
            not args.keep_running
            and not args.run_now
            and not args.resend_post
            and not args.repair_missing_story_media
            and not args.repair_missing_media
        ):
            conn.close()
            return

    if args.repair_missing_story_media or args.repair_missing_media:
        repair_missing_story_media(
            cfg,
            conn,
            selected_targets=selected_targets or platform_target_filters,
        )
        if not args.keep_running and not args.run_now and not args.resend_post:
            conn.close()
            return

    bot = build_bot(cfg.telegram_bot_token)
    proxy_mgr = ProxyManager(
        cfg.proxies,
        conn,
        bot=bot,
        notification_chat_id=cfg.notifications.chat_id,
        error_thread_id=cfg.notifications.error_thread(),
    )

    if args.interactive_login:
        await interactive_login(args.interactive_login, cfg, proxy_mgr)
        conn.close()
        return

    if args.import_cookies:
        _import_cookies_from_chrome(args.import_cookies, cfg)
        conn.close()
        return

    if args.resend_post:
        await resend_post_notification(args.resend_post, cfg, conn, bot)
        if not args.keep_running and not args.run_now:
            conn.close()
            return

    if args.run_now:
        # CLI one-shot mode: run specific job and exit (no scheduler)
        await run_requested_jobs(
            args.run_now,
            cfg,
            conn,
            bot,
            proxy_mgr,
            selected_targets=selected_targets,
            selected_accounts=selected_accounts,
            include_reposts_in_bootstrap=args.with_reposts,
        )
        if not args.keep_running:
            conn.close()
            return

    from src.db import get_config, set_config
    scheduler = build_scheduler(cfg, conn, bot, proxy_mgr)
    # Always start the APScheduler event loop, but pause all scraping jobs
    # if is_scanning=0.  flush_notifications and daily_digest still run so
    # already-queued Telegram messages are delivered while scanning is paused.
    scheduler.start()
    is_scanning = get_config(conn, "is_scanning", "0") == "1"

    # Suppress any notifications queued before this startup to avoid replaying
    # stale alerts when flush_notifications first runs.
    suppressed = _suppress_pending_notifications(conn)
    if suppressed:
        log_event("main", "startup_notifications_suppressed", count=suppressed)

    if not is_scanning:
        _pause_scraping_jobs(scheduler)
        log_event("main", "scheduler_started", scanning="paused")
    else:
        log_event("main", "scheduler_started", scanning="active")
        # Stories are highest priority — fire immediately instead of waiting for
        # the first interval to elapse (critical when offline for a long time).
        if _trigger_stories_immediately(scheduler):
            log_event("main", "stories_job_triggered_immediately")

    from src.telegram_commands import start_command_listener, stop_command_listener
    command_app = await start_command_listener(
        cfg.telegram_bot_token, cfg, conn, bot, proxy_mgr, scheduler
    )

    # Announce startup state clearly
    scanning_status = "Scanning is *ACTIVE*" if is_scanning else "Scanning is *PAUSED* — send /start\\_scanning to begin"
    from src.telegram_bot import _safe_send, _esc
    await _safe_send(
        bot,
        cfg.notifications.chat_id,
        f"🟢 *social\\-scanner ready*\n"
        f"Monitoring {_esc(str(len(monitored_targets)))} profiles\\.\n"
        f"{scanning_status}",
        message_thread_id=cfg.notifications.general_thread(),
    )

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        log_event("main", "shutdown")
        await stop_command_listener(command_app)
        scheduler.shutdown()
        conn.close()


def main() -> None:
    asyncio.run(async_main(parse_args()))


if __name__ == "__main__":
    main()
