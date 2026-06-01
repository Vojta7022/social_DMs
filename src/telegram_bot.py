"""
telegram_bot.py — All Telegram notification functions.

All send functions are async, never raise on failure (errors are logged instead),
and use _safe_send() with 3-attempt exponential backoff.

Message format uses Markdown (ParseMode.MARKDOWN_V2).
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from loguru import logger
from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.helpers import escape_markdown
from src.utils import platform_profile_url as _platform_profile_url

if TYPE_CHECKING:
    from src.config import NotificationsConfig


# ─── Bot factory ─────────────────────────────────────────────────────────────

def build_bot(token: str) -> Bot:
    """Instantiate a python-telegram-bot Bot object."""
    return Bot(token=token)


# ─── Safe send wrapper ───────────────────────────────────────────────────────

def _thread_kwargs(message_thread_id: int | None) -> dict:
    """Return Telegram API kwargs for topic routing when a thread ID is configured."""
    if message_thread_id is None:
        return {}
    return {"message_thread_id": message_thread_id}


def _thread_retry_candidates(message_thread_id: int | None) -> list[int | None]:
    """Try the configured topic first, then retry in the General/root chat if needed."""
    if message_thread_id is None:
        return [None]
    return [message_thread_id, None]


def _is_missing_thread_error(exc: Exception) -> bool:
    """Detect Telegram's stale-topic error so content is not silently dropped."""
    return "message thread not found" in str(exc).lower()


def _is_parse_entities_error(exc: Exception) -> bool:
    """Detect Telegram Markdown parsing failures so we can fall back to plain text."""
    return isinstance(exc, BadRequest) and "can't parse entities" in str(exc).lower()


_MDV2_LINK_RE = re.compile(r"\[((?:\\.|[^\]])+)\]\(((?:\\.|[^)])+)\)")
_MDV2_ESCAPED_CHAR_RE = re.compile(r"\\([\\_*\[\]()~`>#+\-=|{}.!])")


def _unescape_markdown_v2(text: str) -> str:
    """Undo Telegram MarkdownV2 escaping for plain-text fallback delivery."""
    if not text:
        return ""
    return _MDV2_ESCAPED_CHAR_RE.sub(r"\1", text)


def _markdown_v2_to_plain_text(text: str) -> str:
    """Best-effort conversion from MarkdownV2 to readable plain text."""
    if not text:
        return ""

    def _replace_link(match: re.Match[str]) -> str:
        label = _unescape_markdown_v2(match.group(1))
        url = _unescape_markdown_v2(match.group(2))
        return f"{label}: {url}"

    plain = _MDV2_LINK_RE.sub(_replace_link, text)
    plain = plain.replace("```", "")
    plain = re.sub(r"(?<!\\)\|\|", "", plain)
    plain = re.sub(r"(?<!\\)[*_~`]", "", plain)
    return _unescape_markdown_v2(plain)


async def _send_markdown_or_plain(
    *,
    label: str,
    text: str,
    send_markdown: Callable[[], Awaitable[object]],
    send_plain: Callable[[str], Awaitable[object]],
) -> object:
    """Deliver rich MarkdownV2 when valid, otherwise retry immediately as plain text."""
    try:
        return await send_markdown()
    except Exception as exc:
        if not _is_parse_entities_error(exc):
            raise
        logger.trace(f"[telegram] {label} MarkdownV2 parse failed; delivering as plain text instead: {exc}")
        return await send_plain(_markdown_v2_to_plain_text(text))


async def _send_with_topic_fallback(
    send_once: Callable[[int | None], Awaitable[object]],
    *,
    label: str,
    chat_id: str,
    message_thread_id: int | None = None,
) -> bool:
    """Retry without a topic when Telegram says the configured thread no longer exists."""
    for thread_candidate in _thread_retry_candidates(message_thread_id):
        should_try_next_thread = False
        for attempt in range(3):
            try:
                await send_once(thread_candidate)
                if thread_candidate != message_thread_id:
                    logger.warning(
                        f"[telegram] Delivered {label} to {chat_id} without the configured topic "
                        f"after topic {message_thread_id} failed."
                    )
                return True
            except Exception as e:
                if thread_candidate is not None and _is_missing_thread_error(e):
                    logger.warning(
                        f"[telegram] {label} topic {thread_candidate} was not found in {chat_id}; "
                        "retrying without a topic."
                    )
                    should_try_next_thread = True
                    break

                retry_after = getattr(e, "retry_after", None)
                wait = int(retry_after) if isinstance(retry_after, (int, float)) and retry_after > 0 else 2 ** attempt
                logger.warning(
                    f"[telegram] {label} send failed (attempt {attempt+1}/3): {e}. Retrying in {wait}s"
                )
                if attempt < 2:
                    await asyncio.sleep(wait)
        if should_try_next_thread:
            continue
        else:
            break

    logger.error(f"[telegram] Permanently failed to send {label} to {chat_id}")
    return False


async def _safe_send(
    bot: Bot,
    chat_id: str,
    text: str,
    *,
    message_thread_id: int | None = None,
    **kwargs,
) -> bool:
    """
    Send a Telegram message with 3-attempt exponential backoff.
    Returns True on success, False on permanent failure.
    Never raises.
    """
    async def _send_once(thread_id: int | None) -> None:
        async def _send_markdown() -> object:
            return await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN_V2,
                **_thread_kwargs(thread_id),
                **kwargs,
            )

        async def _send_plain(plain_text: str) -> object:
            return await bot.send_message(
                chat_id=chat_id,
                text=plain_text,
                **_thread_kwargs(thread_id),
                **kwargs,
            )

        await _send_markdown_or_plain(
            label="message",
            text=text,
            send_markdown=_send_markdown,
            send_plain=_send_plain,
        )

    return await _send_with_topic_fallback(
        _send_once,
        label="message",
        chat_id=chat_id,
        message_thread_id=message_thread_id,
    )


async def _safe_send_photo(
    bot: Bot,
    chat_id: str,
    photo_path: Path,
    caption: str = "",
    *,
    message_thread_id: int | None = None,
) -> bool:
    """Send a photo file with optional caption. Returns True on success."""
    async def _send_once(thread_id: int | None) -> None:
        async def _send_markdown() -> object:
            with open(photo_path, "rb") as f:
                return await bot.send_photo(
                    chat_id=chat_id,
                    photo=InputFile(f),
                    caption=caption[:1024],
                    parse_mode=ParseMode.MARKDOWN_V2,
                    **_thread_kwargs(thread_id),
                )

        async def _send_plain(plain_text: str) -> object:
            with open(photo_path, "rb") as f:
                return await bot.send_photo(
                    chat_id=chat_id,
                    photo=InputFile(f),
                    caption=plain_text[:1024],
                    **_thread_kwargs(thread_id),
                )

        await _send_markdown_or_plain(
            label="photo",
            text=caption,
            send_markdown=_send_markdown,
            send_plain=_send_plain,
        )

    return await _send_with_topic_fallback(
        _send_once,
        label="photo",
        chat_id=chat_id,
        message_thread_id=message_thread_id,
    )


async def _safe_send_video(
    bot: Bot,
    chat_id: str,
    video_path: Path,
    caption: str = "",
    *,
    message_thread_id: int | None = None,
) -> bool:
    """Send a video file with optional caption. Returns True on success."""
    async def _send_once(thread_id: int | None) -> None:
        async def _send_markdown() -> object:
            with open(video_path, "rb") as f:
                return await bot.send_video(
                    chat_id=chat_id,
                    video=InputFile(f),
                    caption=caption[:1024],
                    parse_mode=ParseMode.MARKDOWN_V2,
                    **_thread_kwargs(thread_id),
                )

        async def _send_plain(plain_text: str) -> object:
            with open(video_path, "rb") as f:
                return await bot.send_video(
                    chat_id=chat_id,
                    video=InputFile(f),
                    caption=plain_text[:1024],
                    **_thread_kwargs(thread_id),
                )

        await _send_markdown_or_plain(
            label="video",
            text=caption,
            send_markdown=_send_markdown,
            send_plain=_send_plain,
        )

    return await _send_with_topic_fallback(
        _send_once,
        label="video",
        chat_id=chat_id,
        message_thread_id=message_thread_id,
    )


async def _safe_send_media_group(
    bot: Bot,
    chat_id: str,
    media_paths: list[Path],
    caption: str = "",
    *,
    message_thread_id: int | None = None,
) -> bool:
    """Send multiple photos/videos as Telegram albums, chunked to Telegram's limits."""
    if not media_paths:
        return False

    chunks = [media_paths[i:i + 10] for i in range(0, len(media_paths), 10)]

    def _build_media_chunk(
        chunk: list[Path],
        *,
        include_caption: bool,
        caption_text: str,
        rich_caption: bool,
    ) -> tuple[list[InputMediaPhoto | InputMediaVideo], list[object]]:
        open_files = []
        media = []
        for index, path in enumerate(chunk):
            fh = open(path, "rb")
            open_files.append(fh)
            kwargs = {}
            if include_caption and index == 0 and caption_text:
                kwargs["caption"] = caption_text[:1024]
                if rich_caption:
                    kwargs["parse_mode"] = ParseMode.MARKDOWN_V2

            suffix = path.suffix.lower()
            if suffix in (".mp4", ".mkv", ".webm", ".mov"):
                media.append(InputMediaVideo(media=fh, filename=path.name, **kwargs))
            else:
                media.append(InputMediaPhoto(media=fh, filename=path.name, **kwargs))
        return media, open_files

    def _close_open_files(open_files: list[object]) -> None:
        for fh in open_files:
            try:
                fh.close()
            except Exception:
                pass

    async def _send_once(thread_id: int | None) -> None:
        first_chunk = True
        for chunk in chunks:
            open_files = []
            try:
                media, open_files = _build_media_chunk(
                    chunk,
                    include_caption=first_chunk,
                    caption_text=caption,
                    rich_caption=first_chunk and bool(caption),
                )
                await bot.send_media_group(
                    chat_id=chat_id,
                    media=media,
                    **_thread_kwargs(thread_id),
                )
            except Exception as exc:
                if not first_chunk or not caption or not _is_parse_entities_error(exc):
                    raise
                _close_open_files(open_files)
                media, open_files = _build_media_chunk(
                    chunk,
                    include_caption=True,
                    caption_text=_markdown_v2_to_plain_text(caption),
                    rich_caption=False,
                )
                logger.trace(
                    f"[telegram] media group MarkdownV2 parse failed; delivering caption as plain text instead: {exc}"
                )
                await bot.send_media_group(
                    chat_id=chat_id,
                    media=media,
                    **_thread_kwargs(thread_id),
                )
            finally:
                _close_open_files(open_files)
            first_chunk = False

    return await _send_with_topic_fallback(
        _send_once,
        label="media group",
        chat_id=chat_id,
        message_thread_id=message_thread_id,
    )


def _esc(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    if not text:
        return ""
    return escape_markdown(text, version=2)


def _esc_url(url: str) -> str:
    """Escape the URL portion of a MarkdownV2 inline link."""
    if not url:
        return ""
    return escape_markdown(url, version=2, entity_type="text_link")


def _markdown_link(label: str, url: str) -> str:
    """Build a safe MarkdownV2 inline link."""
    if not url:
        return _esc(label)
    return f"[{_esc(label)}]({_esc_url(url)})"


def _profile_link(platform: str, raw_username: str) -> str:
    """Return a MarkdownV2 inline link that opens the profile in the platform's app."""
    if not raw_username:
        return ""
    p = platform.lower()
    if p in ("twitter", "x"):
        url = f"https://x.com/{raw_username}"
    else:
        url = _platform_profile_url(platform, raw_username)
    if not url:
        return f"@{_esc(raw_username)}"
    return _markdown_link(f"@{raw_username}", url)


def _record_dict(record: dict | sqlite3.Row) -> dict:
    """Normalize sqlite3.Row objects into plain dicts for downstream access."""
    if isinstance(record, sqlite3.Row):
        return dict(record)
    return record


def _json_loads(value):
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return None


async def _safe_send_message_with_result(
    bot: Bot,
    chat_id: str,
    text: str,
    *,
    message_thread_id: int | None = None,
    **kwargs,
) -> Message | None:
    """Send a Telegram message and return the Message object when delivery succeeds."""
    holder: dict[str, Message | None] = {"message": None}

    async def _send_once(thread_id: int | None) -> None:
        async def _send_markdown() -> object:
            return await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN_V2,
                **_thread_kwargs(thread_id),
                **kwargs,
            )

        async def _send_plain(plain_text: str) -> object:
            return await bot.send_message(
                chat_id=chat_id,
                text=plain_text,
                **_thread_kwargs(thread_id),
                **kwargs,
            )

        holder["message"] = await _send_markdown_or_plain(
            label="message",
            text=text,
            send_markdown=_send_markdown,
            send_plain=_send_plain,
        )

    success = await _send_with_topic_fallback(
        _send_once,
        label="message",
        chat_id=chat_id,
        message_thread_id=message_thread_id,
    )
    return holder["message"] if success else None


def _resolve_saved_path(file_path: str, data_dir: Path | None = None) -> Path:
    """Resolve a stored DB path to a real on-disk Path."""
    path = Path(file_path)
    if path.is_absolute():
        return path

    candidates = []
    if data_dir is not None:
        candidates.append((data_dir.parent / path).resolve())
        candidates.append((data_dir / path).resolve())
    candidates.append((Path.cwd() / path).resolve())

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _format_posted_at(value: str | None) -> str | None:
    """Format an ISO timestamp for human-readable Telegram copy."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None
    return dt.strftime("%Y-%m-%d %H:%M UTC")


# ─── New post notification ────────────────────────────────────────────────────

async def send_new_post(
    bot: Bot,
    chat_id: str,
    post: dict | sqlite3.Row,
    media_paths: list[Path] | None = None,
    *,
    message_thread_id: int | None = None,
) -> bool:
    """Send a new-post notification. Attaches saved photos/videos when available."""
    try:
        post = _record_dict(post)
        platform = _esc(post["platform"].capitalize())
        target = _esc(post["target_username"])
        post_type = _esc(post["post_type"])
        content_scope = post.get("content_scope") or "feed"
        url = post["url"]
        caption = post.get("caption") or ""
        caption_snippet = _esc(caption[:120] + ("…" if len(caption) > 120 else ""))
        is_repost = bool(post.get("is_repost"))

        repost_line = ""
        if is_repost and post.get("repost_source_account"):
            repost_line = f"\n↩️ Repost from: {_profile_link(post['platform'], post['repost_source_account'])}"

        scope_label = {
            "mentions": "mention ",
            "reposts": "repost ",
        }.get(content_scope, "")
        posted_line = ""
        posted_at = _format_posted_at(post.get("posted_at"))
        if posted_at:
            posted_line = f"\n🗓 {_esc(posted_at)}"
        text = (
            f"📸 *New {scope_label}{post_type}* on {platform}\n"
            f"👤 {_profile_link(post['platform'], post['target_username'])}{repost_line}{posted_line}\n"
            f"🔗 {_markdown_link('View post', url)}"
        )
        if caption_snippet:
            text += f"\n💬 _{caption_snippet}_"

        existing_media = [path for path in (media_paths or []) if path.exists()]
        if existing_media:
            if len(existing_media) == 1:
                media_path = existing_media[0]
                suffix = media_path.suffix.lower()
                if suffix in (".mp4", ".mkv", ".webm", ".mov"):
                    sent = await _safe_send_video(
                        bot,
                        chat_id,
                        media_path,
                        caption=text,
                        message_thread_id=message_thread_id,
                    )
                else:
                    sent = await _safe_send_photo(
                        bot,
                        chat_id,
                        media_path,
                        caption=text,
                        message_thread_id=message_thread_id,
                    )
            else:
                sent = await _safe_send_media_group(
                    bot,
                    chat_id,
                    existing_media,
                    caption=text,
                    message_thread_id=message_thread_id,
                )
            if not sent:
                return await _safe_send(bot, chat_id, text, message_thread_id=message_thread_id)
            return True
        return await _safe_send(bot, chat_id, text, message_thread_id=message_thread_id)

    except Exception as e:
        logger.error(f"[telegram] send_new_post error: {e}")
        return False


# ─── New story notification ───────────────────────────────────────────────────

async def send_new_story(
    bot: Bot,
    chat_id: str,
    story: dict | sqlite3.Row,
    media_path: Path | None = None,
    *,
    message_thread_id: int | None = None,
) -> bool:
    """Send a new-story notification with optional screenshot."""
    try:
        story = _record_dict(story)
        platform = _esc(story["platform"].capitalize())
        target = _esc(story["target_username"])
        story_type = _esc(story.get("story_type", "image"))
        content_scope = story.get("content_scope") or "story"
        collection_name = story.get("collection_name")

        if content_scope == "highlight":
            highlight_suffix = ""
            if collection_name:
                highlight_suffix = f" \\({_esc(str(collection_name))}\\)"
            headline = f"⭐ *New highlight {story_type}* on {platform}{highlight_suffix}"
        else:
            headline = f"🎭 *New {story_type} story* on {platform}"

        text = f"{headline}\n👤 {_profile_link(story['platform'], story['target_username'])}"
        posted_at = _format_posted_at(story.get("posted_at"))
        if posted_at:
            text += f"\n🗓 {_esc(posted_at)}"

        if media_path and media_path.exists():
            suffix = media_path.suffix.lower()
            if suffix in (".mp4", ".mkv", ".webm", ".mov"):
                return await _safe_send_video(
                    bot,
                    chat_id,
                    media_path,
                    caption=text,
                    message_thread_id=message_thread_id,
                )
            return await _safe_send_photo(
                bot,
                chat_id,
                media_path,
                caption=text,
                message_thread_id=message_thread_id,
            )
        return await _safe_send(bot, chat_id, text, message_thread_id=message_thread_id)

    except Exception as e:
        logger.error(f"[telegram] send_new_story error: {e}")
        return False


# ─── Follower event notification ──────────────────────────────────────────────

_FOLLOWER_EVENT_EMOJI = {
    "gained_follower": "➕",
    "lost_follower": "➖",
    "started_following": "👣",
    "stopped_following": "🚶",
}

_FOLLOWER_EVENT_TEXT = {
    "gained_follower": "New follower",
    "lost_follower": "Lost follower",
    "started_following": "Started following",
    "stopped_following": "Stopped following",
}

_FACEBOOK_FRIEND_EVENT_TEXT = {
    "gained_follower": "New friend",
    "lost_follower": "Lost friend",
    "started_following": "New friend",
    "stopped_following": "Lost friend",
}


async def send_follower_event(
    bot: Bot,
    chat_id: str,
    event: dict | sqlite3.Row,
    *,
    message_thread_id: int | None = None,
) -> bool:
    """Send a follower gained/lost or following started/stopped notification."""
    try:
        event = _record_dict(event)
        event_type = event["event_type"]
        emoji = _FOLLOWER_EVENT_EMOJI.get(event_type, "🔔")
        platform_name = event["platform"]
        label_map = _FACEBOOK_FRIEND_EVENT_TEXT if platform_name == "facebook" else _FOLLOWER_EVENT_TEXT
        label = _esc(label_map.get(event_type, event_type))
        platform = _esc(platform_name.capitalize())
        subject_kind = event.get("subject_kind") or ("account" if event.get("account_username") else "target")
        raw_subject_username = (
            event.get("subject_username")
            or event.get("account_username")
            or event.get("tracked_username")
            or ""
        )
        raw_target_username = event["target_username"]
        subject_label = "Tracked profile" if subject_kind == "target" else "Account"

        text = (
            f"{emoji} *{label}*\n"
            f"📱 {platform} {subject_label.lower()}: {_profile_link(platform_name, raw_subject_username)}\n"
            f"👤 {_profile_link(platform_name, raw_target_username)}"
        )
        return await _safe_send(bot, chat_id, text, message_thread_id=message_thread_id)

    except Exception as e:
        logger.error(f"[telegram] send_follower_event error: {e}")
        return False


# ─── Error alert ─────────────────────────────────────────────────────────────

async def send_error(
    bot: Bot,
    chat_id: str,
    context: str,
    exc: Exception,
    *,
    message_thread_id: int | None = None,
) -> None:
    """Send an error alert. Always swallows its own exceptions."""
    try:
        ctx = _esc(context)
        msg = _esc(str(exc)[:300])
        text = f"🚨 *Error in* `{ctx}`\n```\n{msg}\n```"
        await _safe_send(bot, chat_id, text, message_thread_id=message_thread_id)
    except Exception as e:
        logger.error(f"[telegram] send_error itself failed: {e}")


# ─── Daily digest ─────────────────────────────────────────────────────────────

async def send_daily_digest(
    bot: Bot,
    chat_id: str,
    data: dict,
    *,
    message_thread_id: int | None = None,
) -> None:
    """Send the daily morning briefing with 24h counts."""
    try:
        lines = [
            "☀️ *Daily Digest*\n",
            f"📸 New posts: *{data.get('new_posts', 0)}*",
            f"🎭 New stories: *{data.get('new_stories', 0)}*",
            f"➕ New followers: *{data.get('gained_followers', 0)}*",
            f"➖ Lost followers: *{data.get('lost_followers', 0)}*",
            f"👣 Started following: *{data.get('started_following', 0)}*",
            f"🚶 Stopped following: *{data.get('stopped_following', 0)}*",
            f"🖼 Avatar changes: *{data.get('avatar_changes', 0)}*",
            f"📝 Bio/profile changes: *{data.get('profile_changes', 0)}*",
            f"🧠 Vibe shift alerts: *{data.get('vibe_shifts', 0)}*",
        ]

        top_targets = data.get("top_targets") or []
        if top_targets:
            lines.append("\n*Most active targets:*")
            for t in top_targets:
                lines.append(f"  • `{_esc(t['username'])}` \\({_esc(t['platform'])}\\) — {t['post_count']} posts")

        text = "\n".join(lines)
        await _safe_send(bot, chat_id, text, message_thread_id=message_thread_id)

    except Exception as e:
        logger.error(f"[telegram] send_daily_digest error: {e}")


# ─── Flush pending notifications ─────────────────────────────────────────────

async def flush_notifications(
    bot: Bot,
    cfg: "NotificationsConfig",
    conn,
    data_dir: Path | None = None,
    kinds: set[str] | None = None,
) -> None:
    """
    Pull all unnotified rows from DB, send Telegram messages, then mark as notified.
    Called on a 2-minute interval so scraping and notification sending are decoupled.
    """
    from src.db import (
        get_post_media_files,
        get_story_media_files,
        get_unnotified_follower_events,
        get_unnotified_target_follow_events,
        get_unnotified_posts,
        get_unnotified_stories,
        mark_notified,
    )

    enabled_kinds = kinds or {"posts", "stories", "followers"}

    # Posts
    if cfg.new_post and "posts" in enabled_kinds:
        posts = get_unnotified_posts(conn)
        attempted_post_ids: list[int] = []
        for post_row in posts:
            post = _record_dict(post_row)
            media_rows = get_post_media_files(conn, post["id"])
            media_paths = [
                _resolve_saved_path(row["file_path"], data_dir)
                for row in media_rows
            ]
            post_thread_id = cfg.thread_id_for_post(
                post["platform"],
                post.get("content_scope"),
            )
            delivered = await send_new_post(
                bot,
                cfg.chat_id,
                post,
                media_paths=media_paths,
                message_thread_id=post_thread_id,
            )
            # Always mark as notified after attempting, even if Telegram send
            # failed — prevents infinite retry loops every 2 minutes.
            attempted_post_ids.append(post["id"])
            if not delivered:
                logger.warning(
                    f"[telegram] send failed for post id={post['id']} "
                    f"target={post.get('target_username')} — marking notified anyway"
                )
        if attempted_post_ids:
            mark_notified(conn, "posts", attempted_post_ids)

    # Stories
    if cfg.new_story and "stories" in enabled_kinds:
        stories = get_unnotified_stories(conn)
        attempted_story_ids: list[int] = []
        for story_row in stories:
            story = _record_dict(story_row)
            media_rows = get_story_media_files(conn, story["id"])
            media_path = None
            if media_rows:
                media_path = _resolve_saved_path(media_rows[0]["file_path"], data_dir)
            story_thread_id = cfg.thread_id_for_story(
                story["platform"],
                story.get("content_scope"),
            )
            delivered = await send_new_story(
                bot,
                cfg.chat_id,
                story,
                media_path=media_path,
                message_thread_id=story_thread_id,
            )
            attempted_story_ids.append(story["id"])
            if not delivered:
                logger.warning(
                    f"[telegram] send failed for story id={story['id']} "
                    f"target={story.get('target_username')} — marking notified anyway"
                )
        if attempted_story_ids:
            mark_notified(conn, "stories", attempted_story_ids)

    # Follower events
    if "followers" in enabled_kinds:
        events = [
            *get_unnotified_follower_events(conn),
            *get_unnotified_target_follow_events(conn),
        ]
        events.sort(key=lambda row: row["detected_at"])
        account_event_ids: list[int] = []
        target_event_ids: list[int] = []
        for event_row in events:
            event = _record_dict(event_row)
            event_type = event["event_type"]
            should_send = (
                (event_type == "gained_follower" and cfg.follower_gained)
                or (event_type == "lost_follower" and cfg.follower_lost)
                or (event_type == "started_following" and cfg.following_started)
                or (event_type == "stopped_following" and cfg.following_stopped)
            )
            delivered = not should_send
            if should_send:
                event_thread_id = cfg.thread_id_for_connection(event["platform"])
                delivered = await send_follower_event(
                    bot,
                    cfg.chat_id,
                    event,
                    message_thread_id=event_thread_id,
                )
            if delivered:
                if event.get("subject_kind") == "target":
                    target_event_ids.append(event["id"])
                else:
                    account_event_ids.append(event["id"])

        if account_event_ids:
            mark_notified(conn, "follower_events", account_event_ids)
        if target_event_ids:
            mark_notified(conn, "target_follow_events", target_event_ids)

    # Profile field changes
    from src.db import (
        get_unnotified_profile_changes,
        mark_profile_change_notified,
        get_unnotified_avatar_changes,
        mark_avatar_change_notified,
        get_unnotified_vibe_shifts,
        mark_vibe_shift_notified,
    )
    for row in get_unnotified_profile_changes(conn):
        change = _record_dict(row)
        await send_profile_change(
            bot,
            cfg.chat_id,
            change["target_username"],
            change["field"],
            change.get("old_value"),
            change["new_value"],
            platform=change.get("platform", "instagram"),
            message_thread_id=cfg.general_thread(),
        )
        mark_profile_change_notified(conn, change["id"])

    # Avatar changes
    for row in get_unnotified_avatar_changes(conn):
        change = _record_dict(row)
        await send_avatar_change(
            bot,
            cfg.chat_id,
            change["target_username"],
            change["file_path"],
            change["detected_at"],
            platform=change.get("platform", "instagram"),
            asset_kind=change.get("asset_kind", "avatar"),
            message_thread_id=cfg.general_thread(),
        )
        mark_avatar_change_notified(conn, change["id"])

    # Vibe shift alerts
    for row in get_unnotified_vibe_shifts(conn):
        alert = _record_dict(row)
        await send_vibe_shift_alert(
            bot,
            cfg.chat_id,
            alert["target_username"],
            alert["direction"],
            alert["delta"],
            alert.get("description"),
            platform=alert.get("platform", "instagram"),
            message_thread_id=cfg.general_thread(),
        )
        mark_vibe_shift_notified(conn, alert["id"])


# ─── New notification send functions ─────────────────────────────────────────

async def send_profile_change(
    bot: Bot,
    chat_id: str,
    target_username: str,
    field: str,
    old_value: str | None,
    new_value: str,
    *,
    platform: str = "instagram",
    message_thread_id: int | None = None,
) -> bool:
    """Alert when a tracked profile field (bio, display_name) changes."""
    field_label = {"bio": "Bio", "display_name": "Display name"}.get(field, field.replace("_", " ").title())
    old_snippet = _esc((old_value or "")[:200]) if old_value else "_\\(empty\\)_"
    new_snippet = _esc(new_value[:200])
    text = (
        f"📝 *Profile change detected*\n"
        f"Target: {_profile_link(platform, target_username)}\n"
        f"Field: *{_esc(field_label)}*\n"
        f"Before: {old_snippet}\n"
        f"After: {new_snippet}"
    )
    return await _safe_send(bot, chat_id, text, message_thread_id=message_thread_id)


async def send_avatar_change(
    bot: Bot,
    chat_id: str,
    target_username: str,
    file_path: str,
    detected_at: str,
    *,
    platform: str = "instagram",
    asset_kind: str = "avatar",
    message_thread_id: int | None = None,
) -> bool:
    """Alert when a tracked profile image asset changes."""
    asset_label = {
        "avatar": "profile picture",
        "cover_photo": "cover photo",
    }.get(asset_kind, asset_kind.replace("_", " "))
    caption = (
        f"🖼 *New {asset_label}*\n"
        f"Target: {_profile_link(platform, target_username)}\n"
        f"Detected: {_esc(detected_at[:16].replace('T', ' '))} UTC"
    )
    path = Path(file_path)
    if path.exists():
        return await _safe_send_photo(
            bot, chat_id, path, caption=caption, message_thread_id=message_thread_id
        )
    return await _safe_send(bot, chat_id, caption, message_thread_id=message_thread_id)


async def send_repost_analysis(
    bot: Bot,
    chat_id: str,
    target_username: str,
    source_username: str | None,
    analysis: dict,
    *,
    message_thread_id: int | None = None,
) -> bool:
    """Send structured Gemini AI analysis of a repost to Telegram."""
    src = f"`{_esc(source_username)}`" if source_username else "unknown"
    mood = analysis.get("mood_vibe", "")
    score = analysis.get("mood_score", "?")
    summary = str(analysis.get("overall_summary") or "")[:300]
    hobbies = ", ".join(analysis.get("hobbies") or []) or "—"
    political = str(analysis.get("intellectual_political_themes") or "")[:150]
    financial = str(analysis.get("financial_indicators") or "")[:150]
    relationship = str(analysis.get("relationship_clues") or "")[:150]
    age_gender = str(analysis.get("age_gender_estimate") or "")[:100]

    text = (
        f"🧠 *AI Repost Analysis*\n"
        f"Target: `{_esc(target_username)}` reposts from {src}\n\n"
        f"*Mood:* {_esc(str(mood))} \\(score: {_esc(str(score))}/10\\)\n"
        f"*Hobbies:* {_esc(hobbies)}\n"
        f"*Financial clues:* {_esc(financial)}\n"
        f"*Relationship clues:* {_esc(relationship)}\n"
        f"*Age/gender:* {_esc(age_gender)}\n"
        f"*Themes:* {_esc(political)}\n\n"
        f"*Summary:* {_esc(summary)}"
    )
    return await _safe_send(bot, chat_id, text, message_thread_id=message_thread_id)


async def send_deep_profile_summary(
    bot: Bot,
    chat_id: str,
    target_username: str,
    summary: dict,
    *,
    platform: str = "instagram",
    report_path: str | None = None,
    artifact_path: str | None = None,
    message_thread_id: int | None = None,
) -> bool:
    """Send a concise safe deep-profile summary for one archived target."""
    source_breakdown = summary.get("source_breakdown") if isinstance(summary.get("source_breakdown"), list) else []
    top_activities = summary.get("top_activities") if isinstance(summary.get("top_activities"), list) else []
    top_themes = summary.get("top_recurring_themes") if isinstance(summary.get("top_recurring_themes"), list) else []
    top_locations = summary.get("top_location_signals") if isinstance(summary.get("top_location_signals"), list) else []
    top_brands = summary.get("top_brands_or_logos") if isinstance(summary.get("top_brands_or_logos"), list) else []

    def _list_text(values: list[dict], fallback: str = "none") -> str:
        labels = [
            str(value.get("label") or "").strip()
            for value in values[:4]
            if isinstance(value, dict) and str(value.get("label") or "").strip()
        ]
        return ", ".join(labels) if labels else fallback

    lines = [
        "🧠 *Deep Intelligence Summary*",
        f"Target: {_profile_link(platform, target_username)}",
        f"Archive files scanned: *{int(summary.get('scanned_media_files', 0) or 0)}*",
        f"Visual entries analyzed: *{int(summary.get('analyzed_entries', 0) or 0)}*",
        f"Video frames analyzed: *{int(summary.get('video_frames_analyzed', 0) or 0)}*",
        f"Source breakdown: {_esc(_list_text(source_breakdown))}",
        f"Top activities: {_esc(_list_text(top_activities))}",
        f"Recurring themes: {_esc(_list_text(top_themes))}",
        f"Explicit location signals: {_esc(_list_text(top_locations))}",
        f"Visible brands/logos: {_esc(_list_text(top_brands))}",
    ]

    overview = str(summary.get("overview") or "").strip()
    if overview:
        lines.append(f"Summary: {_esc(overview[:280])}")
    if report_path:
        lines.append(f"Report: `{_esc(report_path)}`")
    if artifact_path:
        lines.append(f"Artifact: `{_esc(artifact_path)}`")

    text = "\n".join(lines)
    return await _safe_send(bot, chat_id, text, message_thread_id=message_thread_id)


async def send_vibe_shift_alert(
    bot: Bot,
    chat_id: str,
    target_username: str,
    direction: str,
    delta: float,
    description: str | None,
    *,
    platform: str = "instagram",
    message_thread_id: int | None = None,
) -> bool:
    """High-priority alert when a target's mood shifts significantly."""
    arrow = "↗️" if direction == "positive" else "↘️"
    desc_text = f"\n{_esc(description[:200])}" if description else ""
    text = (
        f"⚠️ *Vibe Shift Alert* {arrow}\n"
        f"Target: {_profile_link(platform, target_username)}\n"
        f"Direction: *{_esc(direction)}* | Delta: *{_esc(f'{abs(delta):.1f}')} pts*"
        f"{desc_text}"
    )
    return await _safe_send(bot, chat_id, text, message_thread_id=message_thread_id)


async def disable_digital_twin_controls(
    bot: Bot,
    chat_id: str,
    message_id: int,
) -> None:
    """Remove inline review buttons from an older draft card."""
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=None,
        )
    except Exception as exc:
        logger.warning(f"[telegram] Could not disable digital twin controls for message {message_id}: {exc}")


def _digital_twin_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Approve", callback_data=f"dt:approve:{draft_id}"),
                InlineKeyboardButton("Reject", callback_data=f"dt:reject:{draft_id}"),
                InlineKeyboardButton("Edit", callback_data=f"dt:edit:{draft_id}"),
            ]
        ]
    )


async def send_digital_twin_draft(
    bot: Bot,
    chat_id: str,
    draft: dict | sqlite3.Row,
    *,
    message_thread_id: int | None = None,
) -> Message | None:
    """Post one AI draft to the dedicated Telegram review queue with inline controls."""
    payload = _record_dict(draft)
    trigger_payload = _json_loads(payload.get("trigger_payload_json")) or {}
    risk_flags = _json_loads(payload.get("risk_flags_json")) or []
    route = _esc(str(payload.get("route") or "blocked"))
    route_reason = _esc(str(payload.get("route_reason") or "No route reason recorded.")[:220])
    trigger_type = _esc(str(payload.get("trigger_type") or "unknown"))
    draft_text = _esc(str(payload.get("draft_text") or ""))
    target_username = str(payload.get("target_username") or "")
    platform = str(payload.get("platform") or "instagram")
    source_message_id = _esc(str(payload.get("source_message_id") or ""))
    matched_keywords = ", ".join(str(item) for item in (trigger_payload.get("matched_keywords") or []) if str(item).strip())
    keyword_line = f"\nKeywords: {_esc(matched_keywords)}" if matched_keywords else ""
    source_line = f"\nSource message: `{source_message_id}`" if source_message_id else ""
    risk_line = ""
    if risk_flags:
        risk_line = f"\nRisk flags: {_esc(', '.join(str(flag) for flag in risk_flags[:4]))}"

    text = (
        f"🤖 *Digital Twin Draft \\#{payload['id']}*\n"
        f"Target: {_profile_link(platform, target_username)}\n"
        f"Trigger: *{trigger_type}*\n"
        f"Route: *{route}*\n"
        f"Route reason: {route_reason}"
        f"{keyword_line}"
        f"{source_line}"
        f"{risk_line}\n\n"
        f"*Draft text*\n"
        f"{draft_text}"
    )
    return await _safe_send_message_with_result(
        bot,
        chat_id,
        text,
        message_thread_id=message_thread_id,
        reply_markup=_digital_twin_keyboard(int(payload["id"])),
    )


async def send_digital_twin_edit_prompt(
    bot: Bot,
    chat_id: str,
    *,
    draft_id: int,
    target_username: str,
    message_thread_id: int | None = None,
) -> bool:
    """Prompt the reviewer to send corrected text for a draft in edit mode."""
    text = (
        f"✍️ *Editing draft \\#{draft_id}*\n"
        f"Send the replacement message body for `{_esc(target_username)}` in your next Telegram reply\\.\n"
        f"The edited text will be queued back for approval before anything is sent\\."
    )
    return await _safe_send(bot, chat_id, text, message_thread_id=message_thread_id)


async def send_digital_twin_result(
    bot: Bot,
    chat_id: str,
    *,
    draft_id: int,
    target_username: str,
    status: str,
    detail: dict,
    message_thread_id: int | None = None,
) -> bool:
    """Post the result of an approval decision or send attempt back to the review thread."""
    detail_text = ""
    if status == "send_failed":
        detail_text = _esc(str(detail.get("error") or "Unknown error.")[:300])
    elif status == "sent":
        route = _esc(str(detail.get("route") or "unknown"))
        conversation_id = _esc(str(detail.get("conversation_id") or ""))
        detail_text = f"Route: *{route}*"
        if conversation_id:
            detail_text += f"\nConversation: `{conversation_id}`"

    headline = {
        "sent": "✅ *Draft sent*",
        "rejected": "🗑 *Draft rejected*",
        "send_failed": "⚠️ *Draft send failed*",
    }.get(status, "ℹ️ *Draft updated*")

    text = (
        f"{headline}\n"
        f"Draft: *\\#{draft_id}*\n"
        f"Target: `{_esc(target_username)}`"
    )
    if detail_text:
        text += f"\n{detail_text}"
    return await _safe_send(bot, chat_id, text, message_thread_id=message_thread_id)
