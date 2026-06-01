"""
scrapers/tiktok.py — TikTok scraper.

Handles login, video/post scraping, story scraping, and follower/following list scraping.

Notes:
- TikTok heavily uses client-side rendering and lazy loading.
- Login often requires solving a slide/rotate CAPTCHA — raise CaptchaError for operator to handle.
- Video URLs from TikTok are signed and expire; yt-dlp handles downloading them.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from loguru import logger
from playwright.async_api import Page

from src.browser.human import human_click, human_scroll, human_type, random_delay, short_delay
from src.browser.stealth import CaptchaError, LogoutError, assert_no_captcha
from src.comment_utils import normalize_comment_records
from src.logging_utils import log_event
from src.utils import short_wait as _short_wait

if TYPE_CHECKING:
    from src.config import Config, StealthConfig

PLATFORM = "tiktok"
BASE_URL = "https://www.tiktok.com"
_TIKTOK_AUTH_COOKIE_NAMES = {
    "sessionid",
    "sessionid_ss",
    "sid_guard",
    "sid_tt",
    "uid_tt",
    "uid_tt_ss",
}
_TIKTOK_LOGGED_OUT_SELECTORS = [
    "a[href*='/login']",
    "button:has-text('Log in')",
    "button:has-text('Sign up')",
    "[data-e2e='top-login-button']",
    "[data-e2e='login-button']",
    "input[name='username']",
    "input[type='password']",
]
_TIKTOK_LOGGED_IN_SELECTORS = [
    "a[href*='/messages']",
    "a[href*='/inbox']",
    "a[href*='/upload']",
    "[data-e2e='nav-profile']",
    "header a[href*='/@']",
]
_TIKTOK_FOLLOW_LIST_SCENES = {
    "followers": {"67"},
    "following": {"21"},
}


# ─── Login ───────────────────────────────────────────────────────────────────

async def check_login_state(page: Page) -> bool:
    """Navigate to TikTok home and check if we are logged in."""
    try:
        await page.goto(f"{BASE_URL}/foryou?lang=en", wait_until="domcontentloaded", timeout=30_000)
        await _short_wait()

        current_url = page.url.lower()
        if "/login" in current_url or "/signup" in current_url:
            return False

        if await _page_shows_login_prompt(page):
            return False

        for selector in _TIKTOK_LOGGED_IN_SELECTORS:
            try:
                if await page.query_selector(selector):
                    return True
            except Exception:
                continue

        if await _page_has_tiktok_auth_cookie(page):
            log_event(
                "tiktok",
                "login_state_warning",
                level="warning",
                reason="real_auth_cookie_present_without_clear_selector",
            )
            return True
        return False
    except Exception as e:
        if await _page_has_tiktok_auth_cookie(page):
            log_event(
                "tiktok",
                "login_state_warning",
                level="warning",
                reason="auth_cookie_present_after_probe_error",
                error=e,
            )
            return True
        log_event("tiktok", "login_state_error", level="warning", error=e)
        return False


async def _page_has_tiktok_auth_cookie(page: Page) -> bool:
    """Return True when the browser context still carries a real TikTok auth cookie."""
    try:
        cookies = await page.context.cookies([BASE_URL])
    except Exception:
        try:
            cookies = await page.context.cookies()
        except Exception:
            return False

    return any(
        cookie.get("name") in _TIKTOK_AUTH_COOKIE_NAMES
        and str(cookie.get("value") or "").strip()
        for cookie in cookies
        if isinstance(cookie, dict)
    )


async def _page_shows_login_prompt(page: Page) -> bool:
    """Best-effort check for TikTok guest/login surfaces rendered inside a normal profile page."""
    for selector in _TIKTOK_LOGGED_OUT_SELECTORS:
        try:
            if await page.query_selector(selector):
                return True
        except Exception:
            continue

    try:
        body_text = ((await page.locator("body").inner_text()) or "").lower()
    except Exception:
        body_text = ""

    guest_markers = (
        "log in to tiktok",
        "use phone / email / username",
        "continue with facebook",
        "continue with google",
    )
    return any(marker in body_text for marker in guest_markers)


def _dialog_text_requests_login(text: str) -> bool:
    """Return True when a TikTok dialog is a login/signup interstitial rather than a follow list."""
    lowered = text.lower()
    return (
        "log in to tiktok" in lowered
        or "use phone / email / username" in lowered
        or "continue with facebook" in lowered
        or "continue with google" in lowered
    )


async def _raise_for_login_prompt(page: Page, detail: str) -> None:
    """Raise LogoutError when TikTok renders a guest/login prompt inside the current page."""
    if await _page_shows_login_prompt(page):
        raise LogoutError(f"[tiktok] {detail}: TikTok rendered a login prompt instead of account data")


def _detach_page_listener(page: Page, event_name: str, handler) -> None:
    """Remove a Playwright event listener when supported by the current page object."""
    remove_listener = getattr(page, "remove_listener", None)
    if callable(remove_listener):
        remove_listener(event_name, handler)
        return
    off = getattr(page, "off", None)
    if callable(off):
        off(event_name, handler)


def _install_json_capture(page: Page, api_fragment: str) -> tuple[list[dict], object]:
    """Capture JSON responses for a specific TikTok API endpoint while the page navigates."""
    payloads: list[dict] = []

    async def _capture_response(resp) -> None:
        try:
            if api_fragment not in resp.url:
                return
            body = await resp.body()
            if not body:
                return
            payloads.append(json.loads(body.decode("utf-8")))
        except Exception:
            return

    page.on("response", _capture_response)
    return payloads, _capture_response


async def _read_universal_scope(page: Page) -> dict:
    """Return TikTok's embedded page bootstrap scope when available."""
    try:
        raw = await page.locator("script#__UNIVERSAL_DATA_FOR_REHYDRATION__").inner_text()
        data = json.loads(raw)
        scope = data.get("__DEFAULT_SCOPE__")
        if isinstance(scope, dict):
            return scope
    except Exception:
        pass
    return {}


def _user_detail_from_scope(scope: dict) -> dict:
    """Return the embedded TikTok profile payload when present."""
    if not isinstance(scope, dict):
        return {}
    detail = scope.get("webapp.user-detail") or {}
    if not isinstance(detail, dict):
        return {}
    user_info = detail.get("userInfo") or {}
    return user_info if isinstance(user_info, dict) else {}


def _user_story_status_from_scope(scope: dict) -> bool:
    """Return whether TikTok's embedded profile payload says the user has an active story."""
    user = _user_detail_from_scope(scope).get("user") or {}
    if not isinstance(user, dict):
        return False
    value = user.get("UserStoryStatus")
    if isinstance(value, bool):
        return value
    try:
        return int(value or 0) > 0
    except (TypeError, ValueError):
        return False


def _follow_list_expected_total(scope: dict, list_type: str) -> int | None:
    """Read the expected follower/following total from the profile bootstrap payload."""
    stats = _user_detail_from_scope(scope).get("stats") or {}
    if not isinstance(stats, dict):
        return None
    key = "followerCount" if list_type == "followers" else "followingCount"
    value = stats.get(key)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _follow_list_sec_uid(scope: dict) -> str | None:
    """Return the target profile secUid from the embedded bootstrap payload."""
    user = _user_detail_from_scope(scope).get("user") or {}
    if not isinstance(user, dict):
        return None
    sec_uid = str(user.get("secUid") or "").strip()
    return sec_uid or None


def _follow_list_payload_matches(url: str, target_sec_uid: str | None, list_type: str) -> bool:
    """Return True when a TikTok API response is for the target's requested connection list."""
    if "/api/user/list/" not in url:
        return False

    query = parse_qs(urlparse(url).query)
    if target_sec_uid:
        if query.get("secUid", [""])[0] != target_sec_uid:
            return False

    allowed_scenes = _TIKTOK_FOLLOW_LIST_SCENES.get(list_type) or set()
    if allowed_scenes:
        scene = query.get("scene", [""])[0]
        if scene not in allowed_scenes:
            return False

    return True


def _usernames_from_follow_payload(payload: dict) -> set[str]:
    """Extract TikTok usernames from a follow-list API payload."""
    usernames: set[str] = set()
    for entry in payload.get("userList") or []:
        if not isinstance(entry, dict):
            continue
        user = entry.get("user") if isinstance(entry.get("user"), dict) else entry
        username = str((user or {}).get("uniqueId") or "").strip()
        if username:
            usernames.add(username)
    return usernames


def _install_follow_list_capture(page: Page, target_sec_uid: str | None, list_type: str) -> tuple[dict, object]:
    """Capture usernames from the correct TikTok follow-list API responses."""
    state = {"usernames": set()}

    async def _capture_response(resp) -> None:
        try:
            if not _follow_list_payload_matches(resp.url, target_sec_uid, list_type):
                return
            body = await resp.body()
            if not body:
                return
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return

        state["usernames"].update(_usernames_from_follow_payload(payload))

    page.on("response", _capture_response)
    return state, _capture_response


def _canonical_video_url_for_item(item: dict, fallback_username: str) -> str | None:
    """Build a stable TikTok permalink for an API item payload."""
    if not isinstance(item, dict):
        return None

    share_info = item.get("shareInfo") or {}
    share_url = share_info.get("shareUrl")
    if isinstance(share_url, str) and ("/video/" in share_url or "/photo/" in share_url):
        return share_url

    post_id = str(item.get("id") or "").strip()
    if not post_id:
        return None

    author = item.get("author") or {}
    username = str(author.get("uniqueId") or fallback_username or "").strip()
    if not username:
        return None

    # TikTok photo/carousel posts live at /photo/ — navigating to /video/ causes a
    # redirect that lands on a page whose JS scope uses "webapp.photo-detail" instead
    # of "webapp.video-detail", breaking metadata extraction.
    is_photo_post = bool(item.get("imagePost"))
    path_type = "photo" if is_photo_post else "video"
    return f"{BASE_URL}/@{username}/{path_type}/{post_id}"


def _direct_video_url_from_item(item: dict) -> str | None:
    """Extract the best direct-play video URL from a TikTok API item payload."""
    if not isinstance(item, dict):
        return None

    video = item.get("video") or {}
    candidate_paths = [
        ("downloadAddr",),
        ("playAddr",),
        ("PlayAddrStruct", "UrlList"),
        ("playAddrH264",),
    ]
    for path in candidate_paths:
        cursor = video
        for key in path:
            if not isinstance(cursor, dict):
                cursor = None
                break
            cursor = cursor.get(key)
        if isinstance(cursor, str) and cursor.startswith("http"):
            return cursor
        if isinstance(cursor, list):
            for url in cursor:
                if isinstance(url, str) and url.startswith("http"):
                    return url

    bitrate_info = video.get("bitrateInfo")
    if isinstance(bitrate_info, list):
        for entry in bitrate_info:
            play_addr = (entry or {}).get("PlayAddr") or {}
            for url in play_addr.get("UrlList") or []:
                if isinstance(url, str) and url.startswith("http"):
                    return url
    return None


def _owner_from_item_payload(item: dict) -> dict | None:
    """Normalise the TikTok author block into the archive owner schema."""
    author = item.get("author") or {}
    if not isinstance(author, dict):
        return None

    username = str(author.get("uniqueId") or "").strip()
    if not username:
        return None

    user_id = str(author.get("id") or author.get("uid") or author.get("secUid") or "").strip()
    return {
        "username": username,
        "user_id": user_id or None,
        "display_name": author.get("nickname") or username,
        "profile_pic_url": (
            author.get("avatarLarger")
            or author.get("avatarMedium")
            or author.get("avatarThumb")
        ),
        "is_verified": bool(author.get("verified") or author.get("customVerify")),
    }


def _photo_media_items_from_item(item: dict, source_url: str) -> list[dict]:
    """Extract all photo-mode assets from a TikTok item payload."""
    image_post = item.get("imagePost") or {}
    if not isinstance(image_post, dict):
        return []

    media_items: list[dict] = []
    seen_urls: set[str] = set()

    def _append_photo(raw_image: dict, index: int) -> None:
        if not isinstance(raw_image, dict):
            return
        image_url = raw_image.get("imageURL") or {}
        if not isinstance(image_url, dict):
            return
        candidates: list[str] = []
        for candidate in image_url.get("urlList") or []:
            if isinstance(candidate, str) and candidate.startswith("http") and candidate not in seen_urls:
                candidates.append(candidate)
        if not candidates:
            return
        primary = candidates[0]
        seen_urls.add(primary)
        # Keep the remaining CDN mirrors as fallbacks — TikTok signs URLs with a
        # short TTL and the first URL may have expired by download time.
        media_items.append(
            {
                "type": "photo",
                "url": primary,
                "fallback_urls": candidates[1:],
                "source_url": source_url,
                "media_index": index,
                "width": raw_image.get("imageWidth"),
                "height": raw_image.get("imageHeight"),
            }
        )

    for index, raw_image in enumerate(image_post.get("images") or [], start=1):
        _append_photo(raw_image, index)

    if media_items:
        return media_items

    cover_url = image_post.get("cover")
    if isinstance(cover_url, str) and cover_url.startswith("http"):
        return [{"type": "photo", "url": cover_url, "source_url": source_url, "media_index": 1}]
    return []


def _story_permalink_for_item(item: dict, fallback_username: str) -> str | None:
    """Build a stable TikTok story permalink for an API story item when possible."""
    if not isinstance(item, dict):
        return None

    share_info = item.get("shareInfo") or {}
    share_url = share_info.get("shareUrl")
    if isinstance(share_url, str) and "/story/" in share_url:
        return share_url

    story_id = str(item.get("id") or "").strip()
    if not story_id:
        return None

    author = item.get("author") or {}
    username = str(author.get("uniqueId") or fallback_username or "").strip()
    if not username:
        return None
    return f"{BASE_URL}/@{username}/story/{story_id}"


def _story_media_candidates_from_item(item: dict, source_url: str) -> list[dict]:
    """Extract prioritized download candidates from a TikTok story payload."""
    candidates: list[dict] = []
    seen_urls: set[str] = set()

    def _append(
        kind: str,
        url: str | None,
        source: str,
        *,
        width: int | None = None,
        height: int | None = None,
        bitrate: int | None = None,
    ) -> None:
        raw = str(url or "").strip()
        if kind not in {"image", "video"} or not raw.startswith("http") or raw in seen_urls:
            return
        seen_urls.add(raw)
        candidate = {"kind": kind, "url": raw, "source": source}
        for key, value in (("width", width), ("height", height), ("bitrate", bitrate)):
            if isinstance(value, int) and value > 0:
                candidate[key] = value
        candidates.append(candidate)

    video = item.get("video") or {}
    if isinstance(video, dict):
        _append(
            "video",
            video.get("downloadAddr") or video.get("playAddr") or _direct_video_url_from_item(item),
            "video.primary",
            width=video.get("width"),
            height=video.get("height"),
            bitrate=video.get("bitrate"),
        )
        for url in (video.get("playAddrH264"),):
            _append(
                "video",
                url,
                "video.playAddrH264",
                width=video.get("width"),
                height=video.get("height"),
                bitrate=video.get("bitrate"),
            )

        play_addr_struct = video.get("PlayAddrStruct") or {}
        if isinstance(play_addr_struct, dict):
            for url in play_addr_struct.get("UrlList") or []:
                _append(
                    "video",
                    url,
                    "video.PlayAddrStruct.UrlList",
                    width=play_addr_struct.get("Width") or video.get("width"),
                    height=play_addr_struct.get("Height") or video.get("height"),
                    bitrate=video.get("bitrate"),
                )

        bitrate_info = video.get("bitrateInfo")
        if isinstance(bitrate_info, list):
            for entry in bitrate_info:
                if not isinstance(entry, dict):
                    continue
                play_addr = entry.get("PlayAddr") or {}
                urls = play_addr.get("UrlList") if isinstance(play_addr, dict) else []
                for url in urls or []:
                    _append(
                        "video",
                        url,
                        "video.bitrateInfo",
                        width=play_addr.get("Width") if isinstance(play_addr, dict) else video.get("width"),
                        height=play_addr.get("Height") if isinstance(play_addr, dict) else video.get("height"),
                        bitrate=entry.get("Bitrate") or video.get("bitrate"),
                    )

        for source, url in (
            ("video.cover", video.get("cover")),
            ("video.dynamicCover", video.get("dynamicCover")),
            ("video.originCover", video.get("originCover")),
        ):
            _append("image", url, source, width=video.get("width"), height=video.get("height"))

    for media_item in _photo_media_items_from_item(item, source_url):
        _append(
            "image",
            media_item.get("url"),
            "imagePost.primary",
            width=media_item.get("width"),
            height=media_item.get("height"),
        )
        for fallback_url in media_item.get("fallback_urls") or []:
            _append(
                "image",
                fallback_url,
                "imagePost.fallback",
                width=media_item.get("width"),
                height=media_item.get("height"),
            )

    return candidates


def _story_metadata_from_item_payload(
    item: dict,
    target: str,
    *,
    raw_payload_key: str = "story_item_payload",
) -> dict | None:
    """Normalise a TikTok story API item into the scraper's story schema."""
    if not isinstance(item, dict):
        return None

    story_id = str(item.get("id") or "").strip()
    story_permalink = _story_permalink_for_item(item, target)
    if not story_id:
        return None

    media_candidates = _story_media_candidates_from_item(item, story_permalink or "")
    primary_video = next(
        (candidate["url"] for candidate in media_candidates if candidate.get("kind") == "video"),
        None,
    )
    primary_image = next(
        (candidate["url"] for candidate in media_candidates if candidate.get("kind") == "image"),
        None,
    )
    story_type = "video" if primary_video else "image"
    primary_url = primary_video or primary_image or story_permalink
    if not primary_url:
        return None

    story_info = item.get("story") or {}
    music = item.get("music") or {}
    metadata: dict = {
        "caption": item.get("desc") or None,
        "text": item.get("desc") or None,
        "story_permalink": story_permalink,
        "audio_expected": bool(primary_video),
        "media_candidates": media_candidates,
        raw_payload_key: item,
    }
    owner = _owner_from_item_payload(item)
    if owner:
        metadata["owner"] = owner
    if isinstance(music, dict) and music:
        metadata["music"] = [
            {
                "id": music.get("id"),
                "title": music.get("title"),
                "author_name": music.get("authorName"),
                "play_url": music.get("playUrl"),
            }
        ]

    return {
        "platform": PLATFORM,
        "story_id": story_id,
        "story_type": story_type,
        "content_scope": "story",
        "collection_name": None,
        "url": primary_url,
        "story_permalink": story_permalink,
        "posted_at": _coerce_epoch_to_iso(item.get("createTime")),
        "expires_at": _coerce_epoch_to_iso(story_info.get("ExpiredAt")),
        "metadata": metadata,
    }


def _story_metadata_from_payloads(
    payloads: list[dict],
    target: str,
    *,
    raw_payload_key: str = "story_item_payload",
) -> list[dict]:
    """Flatten TikTok story API payloads into unique story metadata records."""
    stories: list[dict] = []
    seen_story_ids: set[str] = set()

    for payload in payloads:
        item_list = payload.get("itemList")
        if not isinstance(item_list, list):
            continue
        for item in item_list:
            story = _story_metadata_from_item_payload(
                item,
                target,
                raw_payload_key=raw_payload_key,
            )
            if not story:
                continue
            story_id = str(story.get("story_id") or "")
            if story_id in seen_story_ids:
                continue
            seen_story_ids.add(story_id)
            stories.append(story)

    return stories


async def _dispatch_story_entry_click(page: Page, selectors: list[str]) -> bool:
    """Best-effort DOM-dispatched click for TikTok story entry points."""
    try:
        return bool(
            await page.evaluate(
                """
                (selectors) => {
                    for (const selector of selectors) {
                        for (const node of Array.from(document.querySelectorAll(selector))) {
                            if (!(node instanceof HTMLElement)) continue;
                            const rect = node.getBoundingClientRect();
                            if (!rect.width || !rect.height) continue;
                            node.scrollIntoView({ block: "center", inline: "center" });
                            for (const type of ["pointerdown", "mousedown", "mouseup", "click"]) {
                                node.dispatchEvent(
                                    new MouseEvent(type, {
                                        bubbles: true,
                                        cancelable: true,
                                        composed: true,
                                        view: window,
                                    })
                                );
                            }
                            if (typeof node.click === "function") {
                                node.click();
                            }
                            return true;
                        }
                    }
                    return false;
                }
                """,
                selectors,
            )
        )
    except Exception:
        return False


def _posted_at_from_item_payload(item: dict) -> str | None:
    """Return the post timestamp from the TikTok payload when available."""
    return _coerce_epoch_to_iso(item.get("createTime") or item.get("scheduleTime"))


def _metadata_from_item_payload(
    item: dict,
    target: str,
    *,
    content_scope: str,
    raw_payload_key: str = "item_payload",
) -> dict | None:
    """Normalise a TikTok post/repost API item into the scraper's post schema."""
    url = _canonical_video_url_for_item(item, target)
    post_id = str(item.get("id") or "").strip()
    if not url or not post_id:
        return None

    stats = item.get("stats") or {}
    direct_video_url = _direct_video_url_from_item(item)
    photo_media_items = _photo_media_items_from_item(item, url)
    original_item = item.get("originalItem") if isinstance(item.get("originalItem"), dict) else None
    original_author = (original_item or {}).get("author") or {}
    repost_source_url = _canonical_video_url_for_item(original_item or {}, "") if original_item else None
    owner = _owner_from_item_payload(item)

    if photo_media_items and not direct_video_url:
        media_items = photo_media_items
        post_type = "carousel" if len(photo_media_items) > 1 else "photo"
    else:
        media_items = [
            {
                "type": "video",
                "url": direct_video_url or url,
                "source_url": url,
                "width": (item.get("video") or {}).get("width"),
                "height": (item.get("video") or {}).get("height"),
                "duration": (item.get("video") or {}).get("duration"),
            }
        ]
        post_type = "video"

    return {
        "platform": PLATFORM,
        "post_id": post_id,
        "post_type": post_type,
        "content_scope": content_scope,
        "collection_name": None,
        "url": url,
        "caption": item.get("desc") or None,
        "posted_at": _posted_at_from_item_payload(item),
        "view_count": stats.get("playCount"),
        "like_count": stats.get("diggCount"),
        "comment_count": stats.get("commentCount"),
        "share_count": stats.get("shareCount"),
        "is_repost": bool(original_item),
        "repost_source_url": repost_source_url,
        "repost_source_account": original_author.get("uniqueId") or None,
        "owner": owner,
        "comments": [],
        "comments_supported": True,
        "comments_snapshot_complete": False,
        "comments_unavailable_reason": "TikTok comments require a detail-page fetch.",
        "media_items": media_items,
        "raw_metadata": {
            raw_payload_key: item,
        },
    }


def _metadata_from_item_payloads(
    payloads: list[dict],
    target: str,
    *,
    content_scope: str,
    raw_payload_key: str = "item_payload",
) -> list[dict]:
    """Flatten TikTok API responses into unique post metadata records."""
    results: list[dict] = []
    seen_post_ids: set[str] = set()

    for payload in payloads:
        item_list = payload.get("itemList")
        if not isinstance(item_list, list):
            continue
        for item in item_list:
            post = _metadata_from_item_payload(
                item,
                target,
                content_scope=content_scope,
                raw_payload_key=raw_payload_key,
            )
            if not post:
                continue
            post_id = str(post.get("post_id") or "")
            if post_id in seen_post_ids:
                continue
            seen_post_ids.add(post_id)
            results.append(post)
    return results


def _merge_post_metadata(base_post: dict, detail_post: dict) -> dict:
    """Merge detail-page fields into a profile-list post payload."""
    merged = dict(base_post)
    merged.update(detail_post)

    base_raw = base_post.get("raw_metadata")
    detail_raw = detail_post.get("raw_metadata")
    if isinstance(base_raw, dict) or isinstance(detail_raw, dict):
        merged_raw: dict = {}
        if isinstance(base_raw, dict):
            merged_raw.update(base_raw)
        if isinstance(detail_raw, dict):
            merged_raw.update(detail_raw)
        merged["raw_metadata"] = merged_raw

    return merged


def _post_needs_detail_enrichment(post: dict) -> bool:
    """Return True when we should open the post detail page for richer metadata."""
    return bool(
        post.get("comment_count")
        or post.get("post_type") in {"photo", "carousel"}
        or not post.get("posted_at")
        or not (post.get("media_items") or [])
    )


def _filter_known_post_records(conn, target: str, posts: list[dict]) -> list[dict]:
    """Filter out already-known post ids for one target during bootstrap-style scrapes."""
    from src.db import get_existing_post_ids_for_target, get_target_id, upsert_target

    try:
        target_id = get_target_id(conn, PLATFORM, target)
    except ValueError:
        target_id = upsert_target(conn, PLATFORM, target)

    existing_post_ids = get_existing_post_ids_for_target(conn, target_id, content_scope="feed")
    if not existing_post_ids:
        return posts
    return [
        post for post in posts
        if str(post.get("post_id") or "") not in existing_post_ids
    ]


async def _collect_video_links_via_yt_dlp(target: str, cfg: "Config") -> list[str]:
    """Use yt-dlp's flat playlist extractor as a TikTok profile fallback."""
    profile_url = f"{BASE_URL}/@{target}"
    playlist_limit = max(cfg.archive.max_post_scrolls * 40, 80)

    def _extract_links() -> list[str]:
        from yt_dlp import YoutubeDL

        options = {
            "quiet": True,
            "skip_download": True,
            "extract_flat": True,
            "ignoreerrors": True,
            "playlistend": playlist_limit,
        }
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(profile_url, download=False)

        entries = info.get("entries") or []
        links: list[str] = []
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            url = entry.get("url") or entry.get("webpage_url")
            if not isinstance(url, str) or "/video/" not in url:
                continue
            canonical = url if url.startswith("http") else BASE_URL + url
            if canonical in seen:
                continue
            seen.add(canonical)
            links.append(canonical)
        return links

    try:
        return await asyncio.to_thread(_extract_links)
    except Exception as exc:
        log_event(
            "tiktok",
            "scrape_fallback_error",
            level="warning",
            kind="posts",
            target=f"@{target}",
            source="yt_dlp_profile",
            error=exc,
        )
        return []


async def login(page: Page, username: str, password: str,
                cfg: "StealthConfig") -> bool:
    """
    Log in to TikTok via the email/phone + password form.
    TikTok often shows a slider/rotation CAPTCHA during login — raises CaptchaError if detected.
    """
    log_event("tiktok", "login_start", account=username)
    await page.goto(f"{BASE_URL}/login/phone-or-email/email", wait_until="domcontentloaded",
                    timeout=30_000)
    await random_delay(cfg)

    if await _detect_captcha(page):
        raise CaptchaError(f"[tiktok] CAPTCHA on login page for {username}")

    # Fill email / username
    await human_type(page, "input[name='username'], input[type='text']", username, cfg)
    await _short_wait()

    await human_type(page, "input[type='password']", password, cfg)
    await random_delay(cfg)

    await human_click(page, "button[type='submit'], button:has-text('Log in')", cfg)
    await page.wait_for_load_state("networkidle", timeout=20_000)
    await random_delay(cfg)

    if await _detect_captcha(page):
        raise CaptchaError(f"[tiktok] CAPTCHA after login attempt for {username}")

    logged_in = await check_login_state(page)
    if logged_in:
        log_event("tiktok", "login_success", account=username)
    else:
        log_event("tiktok", "login_failed", level="error", account=username)
    return logged_in


# ─── Post / video scraping ───────────────────────────────────────────────────

async def scrape_posts(
    page: Page,
    target: str,
    conn,
    cfg: "Config",
    *,
    skip_known_existing: bool = False,
) -> list[dict]:
    """
    Visit target's profile and collect new video/post URLs not yet in DB.
    Returns a list of new post dicts.
    """
    log_event("tiktok", "scrape_start", kind="posts", target=f"@{target}")
    profile_url = f"{BASE_URL}/@{target}"
    payloads, handler = _install_json_capture(page, "/api/post/item_list/")
    try:
        await page.goto(profile_url, wait_until="domcontentloaded", timeout=30_000)
        await random_delay(cfg.stealth)
        await assert_no_captcha(page, PLATFORM)
        await _raise_for_login_prompt(page, f"post scrape for @{target}")

        new_posts = _metadata_from_item_payloads(
            payloads,
            target,
            content_scope="feed",
            raw_payload_key="profile_item_payload",
        )
        if skip_known_existing and new_posts:
            original_count = len(new_posts)
            new_posts = _filter_known_post_records(conn, target, new_posts)
            skipped_count = original_count - len(new_posts)
            if skipped_count:
                log_event(
                    "tiktok",
                    "scrape_skip_existing",
                    kind="posts",
                    target=f"@{target}",
                    skipped=skipped_count,
                )
        if new_posts:
            enriched_posts: list[dict] = []
            for post in new_posts:
                if not _post_needs_detail_enrichment(post):
                    enriched_posts.append(post)
                    continue
                detail_post = await _extract_video_metadata(
                    page,
                    post["url"],
                    target,
                    cfg,
                    seed_post=post,
                )
                enriched_posts.append(detail_post or post)
            log_event("tiktok", "scrape_links_found", kind="posts", target=f"@{target}", links=len(enriched_posts))
            log_event("tiktok", "scrape_result", kind="posts", target=f"@{target}", items=len(enriched_posts))
            return enriched_posts

        video_links = await _collect_video_links(page, cfg)
        if not video_links:
            video_links = await _collect_video_links_via_yt_dlp(target, cfg)
        if skip_known_existing and video_links:
            candidate_posts = [
                {"post_id": _video_id_from_url(url), "url": url}
                for url in video_links
            ]
            known_ids = {
                str(post.get("post_id") or "")
                for post in _filter_known_post_records(conn, target, candidate_posts)
            }
            original_count = len(video_links)
            video_links = [
                url for url in video_links
                if str(_video_id_from_url(url) or "") in known_ids
            ]
            skipped_count = original_count - len(video_links)
            if skipped_count:
                log_event(
                    "tiktok",
                    "scrape_skip_existing",
                    kind="posts",
                    target=f"@{target}",
                    skipped=skipped_count,
                )
        log_event("tiktok", "scrape_links_found", kind="posts", target=f"@{target}", links=len(video_links))

        new_posts = []
        for video_url in video_links:
            metadata = await _extract_video_metadata(page, video_url, target, cfg)
            if metadata:
                new_posts.append(metadata)
    finally:
        _detach_page_listener(page, "response", handler)

    log_event("tiktok", "scrape_result", kind="posts", target=f"@{target}", items=len(new_posts))
    return new_posts


async def _wait_for_video_grid(page: Page, timeout_ms: int = 12_000) -> None:
    """Wait briefly for TikTok's profile grid to hydrate before scrolling it."""
    deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)
    selectors = [
        "div[data-e2e='user-post-item'] a",
        "a[href*='/video/']",
    ]
    while asyncio.get_running_loop().time() < deadline:
        for selector in selectors:
            try:
                if await page.locator(selector).count():
                    return
            except Exception:
                continue
        await asyncio.sleep(0.4)


async def _collect_video_links(
    page: Page,
    cfg: "Config",
    *,
    max_scrolls: int | None = None,
    max_empty_scrolls: int | None = None,
    wait_for_grid: bool = True,
) -> list[str]:
    """Scroll through the profile grid and collect video URLs."""
    links: set[str] = set()
    empty_scrolls = 0
    if wait_for_grid:
        await _wait_for_video_grid(page)

    scroll_budget = max_scrolls if max_scrolls is not None else cfg.archive.max_post_scrolls
    empty_budget = (
        max_empty_scrolls if max_empty_scrolls is not None else cfg.archive.max_empty_post_scrolls
    )

    for _ in range(max(scroll_budget, 1)):
        before = len(links)
        anchors = await page.query_selector_all(
            "div[data-e2e='user-post-item'] a, a[href*='/video/']"
        )
        for a in anchors:
            href = await a.get_attribute("href")
            if href and "/video/" in href:
                links.add(href if href.startswith("http") else BASE_URL + href)
        if len(links) == before:
            empty_scrolls += 1
            if empty_scrolls >= max(empty_budget, 1):
                break
        else:
            empty_scrolls = 0
        await human_scroll(page, "down", cfg.stealth, total_px=800)

    return list(links)


def _repost_scroll_limits(cfg: "Config") -> tuple[int, int]:
    """Use a deeper scroll budget for repost tabs than for standard TikTok profiles."""
    return (
        max(cfg.archive.max_post_scrolls * 3, 30),
        max(cfg.archive.max_empty_post_scrolls + 2, 5),
    )


def _video_id_from_url(url: str) -> str | None:
    """Extract the canonical TikTok video ID from a profile/video URL."""
    match = re.search(r"/video/(\d+)", url)
    if not match:
        return None
    return match.group(1)


async def _extract_video_metadata(
    page: Page,
    video_url: str,
    target: str,
    cfg: "Config",
    *,
    seed_post: dict | None = None,
) -> dict | None:
    """Navigate to a TikTok post detail page and extract richer metadata."""
    try:
        await page.goto(video_url, wait_until="domcontentloaded", timeout=20_000)
        await _short_wait()
        await _raise_for_login_prompt(page, f"video scrape for @{target}")

        # Extract post_id from URL: tiktok.com/@user/video/{id} or /photo/{id}
        # Also check page.url in case TikTok redirected (e.g. /video/ → /photo/)
        match = re.search(r"/(?:video|photo)/(\d+)", video_url) or re.search(
            r"/(?:video|photo)/(\d+)", page.url
        )
        if not match:
            return None
        post_id = match.group(1)

        scope = await _read_universal_scope(page)
        # Photo/carousel posts use "webapp.photo-detail"; video posts use
        # "webapp.video-detail".  Try both so we handle redirects gracefully.
        detail_item = None
        if scope:
            for scope_key in ("webapp.video-detail", "webapp.photo-detail"):
                candidate = ((scope.get(scope_key) or {}).get("itemInfo") or {}).get("itemStruct")
                if isinstance(candidate, dict):
                    detail_item = candidate
                    break
        if isinstance(detail_item, dict):
            metadata = _metadata_from_item_payload(
                detail_item,
                target,
                content_scope=(seed_post or {}).get("content_scope", "feed"),
                raw_payload_key="detail_item_payload",
            )
            if metadata:
                metadata["url"] = video_url
                for media_item in metadata.get("media_items") or []:
                    media_item["source_url"] = video_url
                if seed_post:
                    metadata = _merge_post_metadata(seed_post, metadata)
                comments, comment_state = await _extract_comments(page, metadata, cfg)
                metadata.update(comment_state)
                if comments is not None:
                    metadata["comments"] = comments
                return metadata

        direct_video_url = video_url
        has_video_dom = False
        try:
            video_el = await page.query_selector("video")
            if video_el:
                has_video_dom = True
                direct_video_url = await video_el.get_attribute("src") or video_url
        except Exception:
            pass

        # Detect if it's a repost (duet or stitch)
        is_repost = False
        repost_source_url = None
        repost_source_account = None
        try:
            stitch_el = await page.query_selector("[data-e2e='stitch-tag'], [data-e2e='duet-tag']")
            if stitch_el:
                is_repost = True
                # Try to find the original video link
                orig_link = await page.query_selector("a[href*='/video/']:not([href*='" + post_id + "'])")
                if orig_link:
                    repost_source_url = await orig_link.get_attribute("href")
                    # Extract username from URL /@username/video/...
                    m2 = re.search(r"/@([^/]+)/", repost_source_url or "")
                    if m2:
                        repost_source_account = m2.group(1)
        except Exception:
            pass

        # Caption
        caption = None
        try:
            cap_el = await page.query_selector(
                "[data-e2e='video-desc'], span[data-e2e='new-desc-span']"
            )
            if cap_el:
                caption = await cap_el.inner_text()
        except Exception:
            pass

        # View / like counts
        view_count = await _parse_count(page, "[data-e2e='video-views']")
        like_count = await _parse_count(page, "[data-e2e='like-count']")
        comment_count = await _parse_count(page, "[data-e2e='comment-count']")
        share_count = await _parse_count(page, "[data-e2e='share-count']")

        metadata = {
            "platform": PLATFORM,
            "post_id": post_id,
            "post_type": "video",
            "content_scope": "feed",
            "collection_name": None,
            "url": video_url,
            "caption": caption,
            "view_count": view_count,
            "like_count": like_count,
            "comment_count": comment_count,
            "share_count": share_count,
            "is_repost": is_repost,
            "repost_source_url": repost_source_url,
            "repost_source_account": repost_source_account,
            "media_items": [
                {
                    "type": "video",
                    "url": direct_video_url,
                    "source_url": video_url,
                }
            ],
        }
        if seed_post:
            metadata = _merge_post_metadata(seed_post, metadata)
            if (
                seed_post.get("post_type") in {"photo", "carousel"}
                and not has_video_dom
                and seed_post.get("media_items")
            ):
                metadata["post_type"] = seed_post.get("post_type")
                metadata["media_items"] = seed_post.get("media_items") or metadata.get("media_items") or []
        comments, comment_state = await _extract_comments(page, metadata, cfg)
        metadata.update(comment_state)
        if comments is not None:
            metadata["comments"] = comments
        return metadata
    except Exception as e:
        logger.warning(f"[tiktok] Could not extract video metadata from {video_url}: {e}")
        return None


async def _page_mentions_reposts(page: Page) -> bool:
    """Return whether the current TikTok DOM exposes a repost tab/surface."""
    try:
        return bool(await page.evaluate("""
            () => {
                const elements = Array.from(document.querySelectorAll("a, button, [role='tab']"));
                return elements.some((el) => {
                    const blob = [
                        el.innerText || el.textContent || "",
                        el.getAttribute("aria-label") || "",
                        el.getAttribute("href") || "",
                        el.getAttribute("data-e2e") || "",
                    ].join(" ").toLowerCase();
                    return blob.includes("repost");
                });
            }
        """))
    except Exception:
        return False


async def _reposts_surface_selected(page: Page) -> bool:
    """Best-effort check that the current TikTok tab state is actually on reposts."""
    try:
        return bool(await page.evaluate("""
            () => {
                const elements = Array.from(document.querySelectorAll("a, button, [role='tab']"));
                return elements.some((el) => {
                    const blob = [
                        el.innerText || el.textContent || "",
                        el.getAttribute("aria-label") || "",
                        el.getAttribute("href") || "",
                    ].join(" ").toLowerCase();
                    if (!blob.includes("repost")) return false;

                    const selected = (el.getAttribute("aria-selected") || "").toLowerCase();
                    const current = (el.getAttribute("aria-current") || "").toLowerCase();
                    const state = (el.getAttribute("data-state") || "").toLowerCase();
                    const classes = String(el.className || "").toLowerCase();
                    return (
                        selected == "true" ||
                        current == "page" ||
                        state == "active" ||
                        classes.includes("active")
                    );
                });
            }
        """))
    except Exception:
        return False


async def _activate_reposts_surface(page: Page, target: str, cfg: "Config") -> bool:
    """Try to enter TikTok's repost surface via tab click or direct profile route."""
    profile_url = f"{BASE_URL}/@{target}"
    await page.goto(profile_url, wait_until="domcontentloaded", timeout=30_000)
    await random_delay(cfg.stealth)
    await assert_no_captcha(page, PLATFORM)
    await _raise_for_login_prompt(page, f"repost scrape for @{target}")

    selectors = [
        "[role='tab']:has-text('Reposts')",
        "button:has-text('Reposts')",
        "a:has-text('Reposts')",
        "a[href*='tab=reposts']",
    ]
    for selector in selectors:
        try:
            if not await page.query_selector(selector):
                continue
            await human_click(page, selector, cfg.stealth)
            await short_delay(cfg.stealth)
            if await _reposts_surface_selected(page):
                return True
        except Exception:
            continue

    direct_urls = [
        f"{profile_url}?tab=reposts",
        f"{profile_url}/reposts",
    ]
    for candidate in direct_urls:
        try:
            await page.goto(candidate, wait_until="domcontentloaded", timeout=30_000)
            await random_delay(cfg.stealth)
            await assert_no_captcha(page, PLATFORM)
            if (
                await _page_mentions_reposts(page)
                and (
                    await _reposts_surface_selected(page)
                    or page.url.lower().rstrip("/").endswith("/reposts")
                )
            ):
                return True
        except Exception:
            continue

    return False


def _mark_video_as_repost_surface(post_data: dict, target: str) -> dict:
    """Normalise metadata for videos discovered via a profile repost surface."""
    post_data["content_scope"] = "reposts"
    post_data["is_repost"] = True

    if not post_data.get("repost_source_url") and post_data.get("url"):
        post_data["repost_source_url"] = post_data["url"]

    if not post_data.get("repost_source_account"):
        match = re.search(r"/@([^/]+)/", str(post_data.get("url") or ""))
        if match and match.group(1) != target:
            post_data["repost_source_account"] = match.group(1)

    return post_data


async def scrape_reposts(
    page: Page,
    target: str,
    conn,
    cfg: "Config",
    *,
    skip_known_existing: bool = False,
) -> list[dict]:
    """
    Scrape profile reposts for the target account.
    These are stored as metadata-only repost history records.
    """
    log_event("tiktok", "scrape_start", kind="reposts", target=f"@{target}")
    payloads, handler = _install_json_capture(page, "/api/repost/item_list/")
    try:
        if not await _activate_reposts_surface(page, target, cfg):
            log_event("tiktok", "scrape_result", kind="reposts", target=f"@{target}", items=0)
            return []

        reposts = [
            _mark_video_as_repost_surface(post, target)
            for post in _metadata_from_item_payloads(payloads, target, content_scope="reposts")
        ]
        if reposts:
            log_event("tiktok", "scrape_links_found", kind="reposts", target=f"@{target}", links=len(reposts))
            log_event("tiktok", "scrape_result", kind="reposts", target=f"@{target}", items=len(reposts))
            return reposts

        repost_scrolls, repost_empty_scrolls = _repost_scroll_limits(cfg)
        video_links = await _collect_video_links(
            page,
            cfg,
            max_scrolls=repost_scrolls,
            max_empty_scrolls=repost_empty_scrolls,
            wait_for_grid=True,
        )

        if skip_known_existing:
            from src.db import get_existing_repost_ids_for_target, get_target_id, upsert_target

            try:
                target_id = get_target_id(conn, PLATFORM, target)
            except ValueError:
                target_id = upsert_target(conn, PLATFORM, target)

            existing_post_ids = get_existing_repost_ids_for_target(conn, target_id)
            if existing_post_ids:
                original_count = len(video_links)
                video_links = [
                    link for link in video_links
                    if (_video_id_from_url(link) or "") not in existing_post_ids
                ]
                skipped_count = original_count - len(video_links)
                if skipped_count:
                    log_event(
                        "tiktok",
                        "scrape_skip_existing",
                        kind="reposts",
                        target=f"@{target}",
                        skipped=skipped_count,
                    )

        log_event("tiktok", "scrape_links_found", kind="reposts", target=f"@{target}", links=len(video_links))

        reposts = []
        for video_url in video_links:
            metadata = await _extract_video_metadata(page, video_url, target, cfg)
            if metadata:
                reposts.append(_mark_video_as_repost_surface(metadata, target))
    finally:
        _detach_page_listener(page, "response", handler)

    log_event("tiktok", "scrape_result", kind="reposts", target=f"@{target}", items=len(reposts))
    return reposts


async def scrape_mentions(page: Page, target: str, conn, cfg: "Config") -> list[dict]:
    log_event("tiktok", "scrape_unavailable", kind="mentions", target=f"@{target}", reason="not_implemented")
    return []


async def scrape_highlights(page: Page, target: str, conn, cfg: "Config") -> list[dict]:
    log_event("tiktok", "scrape_unavailable", kind="highlights", target=f"@{target}", reason="platform_not_supported")
    return []


async def scrape_profile_metadata(page: Page, target: str, cfg: "Config") -> dict:
    """Read the current TikTok profile header metadata for a target profile."""
    profile_url = f"{BASE_URL}/@{target}"
    await page.goto(profile_url, wait_until="domcontentloaded", timeout=30_000)
    await random_delay(cfg.stealth)
    await assert_no_captcha(page, PLATFORM)
    await _raise_for_login_prompt(page, f"profile metadata scrape for @{target}")

    display_name = await _read_text(
        page,
        [
            "h1[data-e2e='user-title']",
            "h2[data-e2e='user-title']",
            "h1",
        ],
    )
    bio_text = await _read_text(page, ["h2[data-e2e='user-bio']", "div[data-e2e='user-bio']"])
    profile_pic_url = await _read_attr(
        page,
        [
            "img[data-e2e='user-avatar']",
            "span[data-e2e='user-avatar'] img",
        ],
        "src",
    )
    scope = await _read_universal_scope(page)
    user_detail = (scope.get("webapp.user-detail") or {}) if scope else {}
    user_info = ((user_detail.get("userInfo") or {}).get("user")) or {}
    stats = ((user_detail.get("userInfo") or {}).get("stats")) or {}

    display_name = display_name or user_info.get("nickname")
    bio_text = bio_text or user_info.get("signature")
    profile_pic_url = profile_pic_url or user_info.get("avatarLarger") or user_info.get("avatarMedium")

    return {
        "display_name": display_name,
        "bio_snapshot": bio_text,
        "follower_count": await _parse_count(page, "[data-e2e='followers-count']") or stats.get("followerCount"),
        "following_count": await _parse_count(page, "[data-e2e='following-count']") or stats.get("followingCount"),
        "post_count": await _parse_count(page, "[data-e2e='video-count']") or stats.get("videoCount"),
        "profile_pic_url": profile_pic_url,
    }


async def _parse_count(page: Page, selector: str) -> int | None:
    """Extract a numeric count from a selector, handling K/M suffixes."""
    try:
        el = await page.query_selector(selector)
        if el:
            return _parse_count_text(await el.inner_text())
    except Exception:
        pass
    return None


def _comment_endpoint_kind(url: str) -> str | None:
    """Return the TikTok web comment endpoint kind for one response URL."""
    if "/api/comment/list/reply/" in url:
        return "reply"
    if "/api/comment/list/" in url:
        return "list"
    return None


def _append_tiktok_comment_record(
    records: list[dict],
    by_id: dict[str, dict],
    record: dict | None,
) -> None:
    """Append or merge one normalised TikTok comment row."""
    if not record:
        return

    comment_id = str(record.get("comment_id") or "").strip()
    if comment_id and comment_id in by_id:
        existing = by_id[comment_id]
        for key, value in record.items():
            if value in (None, "", [], {}):
                continue
            existing[key] = value
        return

    stored = dict(record)
    records.append(stored)
    if comment_id:
        by_id[comment_id] = stored


def _normalise_tiktok_comment(
    raw_comment: dict,
    *,
    fallback_parent_id: str | None = None,
    root_comment_id: str | None = None,
) -> dict | None:
    """Map one raw TikTok comment object into the shared archive comment schema."""
    if not isinstance(raw_comment, dict):
        return None

    comment_id = str(raw_comment.get("cid") or raw_comment.get("id") or "").strip()
    if not comment_id:
        return None

    user = raw_comment.get("user") if isinstance(raw_comment.get("user"), dict) else {}
    username = (
        _first_present(user, "unique_id", "uniqueId", "username", "sec_uid")
        or _first_present(raw_comment, "unique_id", "uniqueId", "username")
    )
    display_name = _first_present(user, "nickname", "nick_name", "display_name", "name") or username
    direct_parent_id = (
        _first_present(raw_comment, "reply_to_reply_id", "reply_id", "reply_comment_id")
        or fallback_parent_id
    )
    root_id = str(root_comment_id or fallback_parent_id or comment_id).strip() or None

    return {
        "comment_id": comment_id,
        "parent_comment_id": str(direct_parent_id).strip() if direct_parent_id else None,
        "root_comment_id": root_id,
        "reply_to_comment_id": str(direct_parent_id).strip() if direct_parent_id else None,
        "username": str(username).strip() if username else None,
        "user_id": (
            str(
                _first_present(user, "uid", "id", "user_id", "sec_uid")
                or _first_present(raw_comment, "uid", "id", "user_id")
                or ""
            ).strip()
            or None
        ),
        "display_name": str(display_name).strip() if display_name else None,
        "profile_pic_url": _first_present(
            user,
            "avatar_larger",
            "avatarLarger",
            "avatar_medium",
            "avatarMedium",
            "avatar_thumb",
            "avatarThumb",
            "avatar_url",
        ),
        "is_verified": bool(user.get("verified") or user.get("custom_verify")),
        "text": str(raw_comment.get("text") or "").strip(),
        "commented_at": _coerce_epoch_to_iso(
            raw_comment.get("create_time") or raw_comment.get("createTime")
        ),
        "commented_at_label": _first_present(raw_comment, "time_text", "create_time_text"),
        "like_count": _coerce_int(raw_comment.get("digg_count") or raw_comment.get("like_count")),
        "reply_count": (
            _coerce_int(raw_comment.get("reply_comment_total"))
            or _coerce_int(raw_comment.get("reply_comment_count"))
            or len(raw_comment.get("reply_comment") or [])
        ),
        "raw_payload": raw_comment,
    }


def _comment_snapshot_from_payloads(
    payloads: list[dict],
) -> tuple[list[dict], bool]:
    """Flatten TikTok comment-list and reply-list payloads into one deduplicated thread."""
    records: list[dict] = []
    by_id: dict[str, dict] = {}
    top_level_total: int | None = None
    top_level_has_more = False
    expected_reply_totals: dict[str, int] = {}
    reply_payload_has_more: dict[str, bool] = {}

    for entry in payloads:
        if entry.get("kind") != "list":
            continue
        payload = entry.get("payload") or {}
        if not isinstance(payload, dict) or payload.get("status_code") not in (None, 0):
            continue

        total = _coerce_int(payload.get("total"))
        if total is not None:
            top_level_total = max(top_level_total or 0, total)
        top_level_has_more = bool(payload.get("has_more"))

        for raw_comment in payload.get("comments") or []:
            record = _normalise_tiktok_comment(raw_comment)
            _append_tiktok_comment_record(records, by_id, record)

            parent_id = str((record or {}).get("comment_id") or "").strip()
            if not parent_id:
                continue
            expected_reply_totals[parent_id] = max(
                expected_reply_totals.get(parent_id, 0),
                _coerce_int(raw_comment.get("reply_comment_total"))
                or len(raw_comment.get("reply_comment") or []),
            )
            for raw_reply in raw_comment.get("reply_comment") or []:
                _append_tiktok_comment_record(
                    records,
                    by_id,
                    _normalise_tiktok_comment(
                        raw_reply,
                        fallback_parent_id=parent_id,
                        root_comment_id=parent_id,
                    ),
                )

    for entry in payloads:
        if entry.get("kind") != "reply":
            continue
        payload = entry.get("payload") or {}
        if not isinstance(payload, dict) or payload.get("status_code") not in (None, 0):
            continue

        parent_id = parse_qs(urlparse(entry.get("url") or "").query).get("comment_id", [None])[0]
        if not parent_id:
            continue
        reply_payload_has_more[parent_id] = bool(payload.get("has_more"))
        for raw_reply in payload.get("comments") or []:
            _append_tiktok_comment_record(
                records,
                by_id,
                _normalise_tiktok_comment(
                    raw_reply,
                    fallback_parent_id=parent_id,
                    root_comment_id=parent_id,
                ),
            )

    captured_top_level = sum(1 for record in records if not record.get("parent_comment_id"))
    captured_replies_by_root: dict[str, int] = {}
    for record in records:
        if not record.get("parent_comment_id"):
            continue
        root_id = str(record.get("root_comment_id") or record.get("parent_comment_id") or "").strip()
        if not root_id:
            continue
        captured_replies_by_root[root_id] = captured_replies_by_root.get(root_id, 0) + 1

    complete = not top_level_has_more
    if top_level_total is not None:
        complete = complete and captured_top_level >= top_level_total

    for parent_id, expected_total in expected_reply_totals.items():
        if expected_total > captured_replies_by_root.get(parent_id, 0):
            complete = False
        if reply_payload_has_more.get(parent_id):
            complete = False

    return normalize_comment_records(records), complete


def _comment_item_struct_from_payload(payload: dict | None) -> dict | None:
    """Best-effort extraction of a TikTok detail itemStruct from a raw payload."""
    if not isinstance(payload, dict):
        return None

    item_info = payload.get("itemInfo") if isinstance(payload.get("itemInfo"), dict) else None
    item_struct = (item_info or {}).get("itemStruct")
    if isinstance(item_struct, dict):
        return item_struct

    direct_item_struct = payload.get("itemStruct")
    if isinstance(direct_item_struct, dict):
        return direct_item_struct

    if isinstance(payload.get("comments"), list):
        return payload
    return None


def _comment_snapshot_from_item_struct(item_struct: dict | None) -> list[dict]:
    """Normalize comments embedded in TikTok's hydrated itemStruct detail payload."""
    if not isinstance(item_struct, dict):
        return []

    records: list[dict] = []
    by_id: dict[str, dict] = {}
    for raw_comment in item_struct.get("comments") or []:
        record = _normalise_tiktok_comment(raw_comment)
        _append_tiktok_comment_record(records, by_id, record)

        parent_id = str((record or {}).get("comment_id") or "").strip()
        if not parent_id:
            continue
        for raw_reply in raw_comment.get("reply_comment") or []:
            _append_tiktok_comment_record(
                records,
                by_id,
                _normalise_tiktok_comment(
                    raw_reply,
                    fallback_parent_id=parent_id,
                    root_comment_id=parent_id,
                ),
            )

    return normalize_comment_records(records)


def _merge_tiktok_comment_records(*comment_sets: list[dict] | None) -> list[dict]:
    """Merge multiple TikTok comment snapshots into one deduplicated ordered list."""
    records: list[dict] = []
    by_id: dict[str, dict] = {}
    for comment_set in comment_sets:
        for record in comment_set or []:
            payload = dict(record) if isinstance(record, dict) else None
            _append_tiktok_comment_record(records, by_id, payload)
    return normalize_comment_records(records)


def _tiktok_comment_seed_item_structs(post: dict) -> list[dict]:
    """Read any embedded TikTok comment seed payloads already stored on the post metadata."""
    raw_metadata = post.get("raw_metadata") if isinstance(post.get("raw_metadata"), dict) else {}
    item_structs: list[dict] = []
    for key in ("detail_item_payload", "item_payload"):
        item_struct = _comment_item_struct_from_payload(raw_metadata.get(key))
        if isinstance(item_struct, dict):
            item_structs.append(item_struct)
    return item_structs


async def _embedded_tiktok_comments(page: Page, post: dict) -> list[dict]:
    """Return any comments already embedded in the TikTok detail payload or live page state."""
    comments = _merge_tiktok_comment_records(
        *[_comment_snapshot_from_item_struct(item_struct) for item_struct in _tiktok_comment_seed_item_structs(post)]
    )
    if comments:
        return comments

    scope = await _read_universal_scope(page)
    detail_item_struct = _comment_item_struct_from_payload(scope.get("webapp.video-detail") if scope else None)
    return _comment_snapshot_from_item_struct(detail_item_struct)


def _tiktok_comment_headers_indicate_verification(headers: dict | None) -> bool:
    """Detect TikTok verification/captcha challenges from comment response headers."""
    if not isinstance(headers, dict):
        return False

    for key, value in headers.items():
        key_text = str(key or "").strip().lower()
        value_text = str(value or "").strip().lower()
        if not key_text and not value_text:
            continue
        if "bdturing" in key_text or "bdturing" in value_text:
            return True
        if "captcha" in key_text or "captcha" in value_text:
            return True
        if key_text.startswith("x-vc-") and "verify" in value_text:
            return True
    return False


def _tiktok_comment_errors_require_verification(errors: list[dict]) -> bool:
    """Return True when any captured comment response was blocked by verification."""
    for error in errors:
        if not isinstance(error, dict):
            continue
        if error.get("reason") == "verification_required":
            return True
        if _tiktok_comment_headers_indicate_verification(error.get("headers")):
            return True
    return False


async def _click_first_visible_selector(
    page: Page,
    selectors: list[str],
    cfg: "Config",
) -> bool:
    """Click the first visible element matched by any selector, skipping hidden duplicates."""
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = await locator.count()
        except Exception:
            continue

        for index in range(min(count, 8)):
            candidate = locator.nth(index)
            try:
                if not await candidate.is_visible():
                    continue
                try:
                    await candidate.scroll_into_view_if_needed(timeout=1_500)
                except Exception:
                    pass
                await short_delay(cfg.stealth)
                try:
                    await candidate.click(timeout=4_000)
                except Exception:
                    await candidate.click(timeout=4_000, force=True)
                return True
            except Exception:
                continue
    return False


async def _comment_panel_present(page: Page) -> bool:
    """Return whether the TikTok detail page already exposes a comment panel surface."""
    selectors = [
        "div[class*='DivCommentListContainer']",
        "div[class*='DivCommentMain']",
        "[data-e2e='comment-list']",
    ]
    for selector in selectors:
        try:
            if await page.query_selector(selector):
                return True
        except Exception:
            continue
    return False


async def _click_first_reply_expander(page: Page) -> bool:
    """Click the next visible TikTok reply-expander button inside the comment panel."""
    try:
        return bool(await page.evaluate("""
            () => {
                const root =
                    document.querySelector("div[class*='DivCommentMain']")
                    || document.querySelector("div[class*='DivCommentListContainer']");
                if (!root) return false;

                const candidates = Array.from(
                    root.querySelectorAll("button, [role='button'], span, p, div")
                ).filter((el) => {
                    if (!(el instanceof HTMLElement)) return false;
                    if (el.dataset.socialScannerReplyClick === "1") return false;
                    const text = (el.innerText || "").trim().toLowerCase();
                    if (!text || text.startsWith("add comment")) return false;
                    if (!text.includes("repl")) return false;
                    if (!text.includes("view") && !text.includes("more") && !text.includes("show")) {
                        return false;
                    }
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                });

                const target = candidates[0];
                if (!target) return false;
                target.dataset.socialScannerReplyClick = "1";
                target.click();
                return true;
            }
        """))
    except Exception:
        return False


async def _scroll_comment_panel(page: Page) -> None:
    """Scroll TikTok's comment list surface so the web app requests more pages."""
    try:
        await page.evaluate("""
            () => {
                const roots = [
                    document.querySelector("div[class*='DivCommentListContainer']"),
                    document.querySelector("div[class*='DivCommentMain']"),
                    document.querySelector("[data-e2e='comment-list']"),
                ].filter(Boolean);
                if (!roots.length) return;

                for (const root of roots) {
                    const candidates = [root, ...root.querySelectorAll("*")];
                    const scrollable = candidates.find((node) => {
                        if (!(node instanceof HTMLElement)) return false;
                        const style = window.getComputedStyle(node);
                        return (
                            (style.overflowY === "auto" || style.overflowY === "scroll")
                            && node.scrollHeight > node.clientHeight
                        );
                    });
                    const target = scrollable || root;
                    if (target instanceof HTMLElement) {
                        target.scrollTop += 1600;
                    }
                }
            }
        """)
    except Exception:
        await page.mouse.wheel(0, 1600)


async def _capture_comment_payloads(page: Page, cfg: "Config") -> tuple[list[dict], list[dict]]:
    """Open TikTok's comment panel and capture every comment/reply JSON payload we can."""
    payloads: list[dict] = []
    errors: list[dict] = []
    seen_urls: set[str] = set()

    async def _capture_response(resp) -> None:
        kind = _comment_endpoint_kind(resp.url)
        if not kind or resp.url in seen_urls:
            return
        seen_urls.add(resp.url)
        try:
            headers = await resp.all_headers()
        except Exception:
            headers = {}
        if _tiktok_comment_headers_indicate_verification(headers):
            errors.append(
                {
                    "kind": kind,
                    "url": resp.url,
                    "reason": "verification_required",
                    "headers": headers,
                }
            )
            return
        try:
            text = await resp.text()
        except Exception as exc:
            errors.append(
                {
                    "kind": kind,
                    "url": resp.url,
                    "reason": str(exc),
                    "headers": headers,
                }
            )
            return
        if not text.strip():
            errors.append(
                {
                    "kind": kind,
                    "url": resp.url,
                    "reason": "empty_body",
                    "headers": headers,
                }
            )
            return
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            errors.append(
                {
                    "kind": kind,
                    "url": resp.url,
                    "reason": f"invalid_json:{exc}",
                    "headers": headers,
                }
            )
            return
        if isinstance(payload, dict):
            payloads.append({"kind": kind, "url": resp.url, "payload": payload})

    page.on("response", _capture_response)
    try:
        panel_present = await _comment_panel_present(page)
        if not panel_present:
            opened = await _click_first_visible_selector(
                page,
                [
                    "strong[data-e2e='comment-count']",
                    "[data-e2e='comment-count']",
                    "span[data-e2e='comment-icon']",
                    "[data-e2e='comment-icon']",
                ],
                cfg,
            )
            if not opened and not await _comment_panel_present(page):
                errors.append(
                    {
                        "kind": "list",
                        "url": page.url,
                        "reason": "comment_surface_not_found",
                    }
                )
                return payloads, errors
            await asyncio.sleep(1.5)

        await _click_first_visible_selector(
            page,
            [
                "[role='tab']:has-text('Comments')",
                "button[data-e2e='comments']",
                "button:has-text('Comments')",
            ],
            cfg,
        )
        await asyncio.sleep(1.0)

        stagnant_rounds = 0
        for _ in range(12):
            before = len(payloads)
            while await _click_first_reply_expander(page):
                await short_delay(cfg.stealth)
                await asyncio.sleep(0.8)

            await _scroll_comment_panel(page)
            await asyncio.sleep(1.2)

            if len(payloads) == before:
                stagnant_rounds += 1
                if stagnant_rounds >= 3:
                    break
            else:
                stagnant_rounds = 0
    finally:
        _detach_page_listener(page, "response", _capture_response)

    return payloads, errors


async def _extract_comments(page: Page, post: dict, cfg: "Config") -> tuple[list[dict] | None, dict]:
    """Return a best-effort full TikTok comment snapshot plus capture status metadata."""
    expected_count = _coerce_int(post.get("comment_count")) or 0
    if expected_count <= 0:
        return [], {
            "comments_supported": True,
            "comments_snapshot_complete": True,
            "comments_unavailable_reason": None,
        }

    payloads, errors = await _capture_comment_payloads(page, cfg)
    payload_comments, complete = _comment_snapshot_from_payloads(payloads)
    embedded_comments = await _embedded_tiktok_comments(page, post)
    comments = _merge_tiktok_comment_records(payload_comments, embedded_comments)
    authoritative_payload_snapshot = bool(payloads) and complete
    if authoritative_payload_snapshot and comments:
        return comments, {
            "comments_supported": True,
            "comments_snapshot_complete": True,
            "comments_unavailable_reason": None,
        }
    if authoritative_payload_snapshot and not comments and expected_count == 0:
        return [], {
            "comments_supported": True,
            "comments_snapshot_complete": True,
            "comments_unavailable_reason": None,
        }

    reason = "TikTok did not return a complete comment payload."
    verification_required = _tiktok_comment_errors_require_verification(errors)
    if verification_required and comments:
        reason = (
            "TikTok required a verification challenge while paginating comments; "
            "only the embedded detail-page comments were archived."
        )
    elif verification_required:
        reason = "TikTok required a verification challenge while loading comments."
    elif await _detect_captcha(page):
        reason = "TikTok presented a captcha while loading comments."
    elif errors:
        first_error = errors[0]
        reason = (
            "TikTok comment fetch returned an unreadable payload"
            f" ({first_error.get('reason')})."
        )
    elif comments:
        reason = "TikTok only exposed a partial embedded comment snapshot on the detail page."

    return comments or None, {
        "comments_supported": True,
        "comments_snapshot_complete": False,
        "comments_unavailable_reason": reason,
    }


# ─── Story scraping ──────────────────────────────────────────────────────────

async def scrape_stories(page: Page, target: str, conn, cfg: "Config") -> list[dict]:
    """
    Check if target has a story available and capture its metadata.
    TikTok stories appear as a story ring on the profile page.
    """
    log_event("tiktok", "scrape_start", kind="stories", target=f"@{target}")
    profile_url = f"{BASE_URL}/@{target}"
    payloads, handler = _install_json_capture(page, "/api/story/item_list/")
    stories: list[dict] = []
    try:
        await page.goto(profile_url, wait_until="domcontentloaded", timeout=30_000)
        await random_delay(cfg.stealth)
        await assert_no_captcha(page, PLATFORM)
        await _raise_for_login_prompt(page, f"story scrape for @{target}")

        scope = await _read_universal_scope(page)
        has_story_from_scope = _user_story_status_from_scope(scope)
        has_story_from_avatar = False
        try:
            has_story_from_avatar = bool(
                await page.evaluate(
                    """
                    () => {
                        const avatar = document.querySelector("[data-e2e='user-avatar']");
                        if (!(avatar instanceof HTMLElement)) return false;
                        const inline = avatar.getAttribute("style") || "";
                        const match = inline.match(/--user-avatar-ring-thickness:\\s*([0-9.]+)/);
                        return Boolean(match && parseFloat(match[1]) > 0);
                    }
                    """
                )
            )
        except Exception:
            has_story_from_avatar = False

        if not has_story_from_scope and not has_story_from_avatar:
            logger.debug(f"[tiktok] No stories for @{target}")
            return []

        try:
            await page.wait_for_selector(
                "[data-e2e='user-avatar'], [data-e2e='user-story-avatar'], div[class*='StoryRing'], div[class*='story-ring']",
                timeout=5_000,
            )
        except Exception:
            pass

        clicked = await _click_first_visible_selector(
            page,
            [
                "[data-e2e='user-avatar']",
                "[data-e2e='user-story-avatar']",
                "div[class*='StoryRing']",
                "div[class*='story-ring']",
            ],
            cfg,
        )
        if not clicked:
            clicked = await _dispatch_story_entry_click(
                page,
                [
                    "[data-e2e='user-avatar']",
                    "[data-e2e='user-story-avatar']",
                    "div[class*='StoryRing']",
                    "div[class*='story-ring']",
                ],
            )
        if not clicked:
            logger.debug(f"[tiktok] Story entry point not clickable for @{target}")
            return []

        for _ in range(20):
            if payloads:
                break
            await page.wait_for_timeout(250)
        stories = _story_metadata_from_payloads(
            payloads,
            target,
            raw_payload_key="story_api_payload",
        )
        if stories:
            log_event("tiktok", "scrape_result", kind="stories", target=f"@{target}", items=len(stories))
            return stories

        if await _detect_captcha(page):
            raise CaptchaError(f"[tiktok] CAPTCHA during story scrape for {target}")

        await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        await _short_wait()

        story_count = 0
        while story_count < 30:
            if await _detect_captcha(page):
                raise CaptchaError(f"[tiktok] CAPTCHA during story scrape for {target}")

            story_data = await _extract_story_metadata(page, target)
            if story_data:
                stories.append(story_data)

            next_btn = await page.query_selector(
                "button[aria-label='Next'], div[data-e2e='story-next']"
            )
            if not next_btn:
                break
            await next_btn.click()
            await _short_wait()
            story_count += 1

    except (CaptchaError, LogoutError):
        raise
    except Exception as e:
        log_event("tiktok", "scrape_error", level="warning", kind="stories", target=f"@{target}", error=e)
    finally:
        _detach_page_listener(page, "response", handler)

    log_event("tiktok", "scrape_result", kind="stories", target=f"@{target}", items=len(stories))
    return stories


async def _extract_story_metadata(page: Page, target: str) -> dict | None:
    """Extract story_id and type from TikTok story viewer."""
    try:
        url = page.url
        # URL format varies: may contain story id as query param or path segment
        match = re.search(r"story[_/]?(\d+)", url)
        story_id = match.group(1) if match else url.split("?")[0].split("/")[-1]

        video_el = await page.query_selector("video")
        story_type = "video" if video_el else "image"

        media_url = None
        if video_el:
            media_url = await video_el.get_attribute("src")
        else:
            img_el = await page.query_selector("img[srcset], img[src*='story']")
            if img_el:
                media_url = await img_el.get_attribute("src")

        return {
            "platform": PLATFORM,
            "story_id": story_id,
            "story_type": story_type,
            "content_scope": "story",
            "collection_name": None,
            "url": media_url or url,
        }
    except Exception as e:
        logger.warning(f"[tiktok] Could not extract story metadata: {e}")
        return None


# ─── Follower / following scraping ──────────────────────────────────────────

async def scrape_target_connections(page: Page, target_username: str, conn,
                                    cfg: "Config") -> dict:
    """Scrape follower/following lists for a monitored TikTok target profile."""
    log_event("tiktok", "scrape_start", kind="target_connections", target=f"@{target_username}")
    return await _scrape_connection_lists_best_effort(page, target_username, cfg, log_scope="target")

async def scrape_followers(page: Page, own_username: str, conn,
                            cfg: "Config") -> dict:
    """
    Scrape own follower and following lists, save snapshots, return delta events.
    """
    from src.db import (
        diff_follower_snapshots,
        get_account_id,
        insert_follower_events,
        save_follower_snapshot,
    )

    log_event("tiktok", "scrape_start", kind="followers", account=f"@{own_username}")
    account_id = get_account_id(conn, PLATFORM, own_username)

    result = await _scrape_connection_lists_best_effort(page, own_username, cfg, log_scope="account")
    followers = result.get("followers") or []
    following = result.get("following") or []
    followers_available = bool(result.get("followers_available"))
    following_available = bool(result.get("following_available"))
    events: list[dict] = []
    if followers_available:
        save_follower_snapshot(conn, account_id, "followers", followers)
        events.extend(diff_follower_snapshots(conn, account_id, "followers"))
    if following_available:
        save_follower_snapshot(conn, account_id, "following", following)
        events.extend(diff_follower_snapshots(conn, account_id, "following"))

    if events:
        insert_follower_events(conn, account_id, events)
        log_event("tiktok", "scrape_events", kind="followers", account=f"@{own_username}", events=len(events))

    result["events"] = events
    return result


async def _scrape_connection_lists_best_effort(
    page: Page,
    username: str,
    cfg: "Config",
    *,
    log_scope: str,
) -> dict:
    """Scrape whichever TikTok connection lists are accessible without discarding partial results."""
    result = {
        "followers": [],
        "following": [],
        "followers_available": False,
        "following_available": False,
        "errors": {},
    }

    for list_type in ("followers", "following"):
        try:
            result[list_type] = await _scrape_follow_list_in_fresh_page(page, username, list_type, cfg)
            result[f"{list_type}_available"] = True
        except (CaptchaError, LogoutError):
            raise
        except Exception as exc:
            log_payload = {
                "kind": list_type,
                "reason": exc,
            }
            if log_scope == "account":
                log_payload["account"] = f"@{username}"
            else:
                log_payload["target"] = f"@{username}"
            log_event("tiktok", "scrape_unavailable", level="warning", **log_payload)
            result["errors"][list_type] = str(exc)

    return result


async def _scrape_follow_list(page: Page, username: str, list_type: str,
                               cfg: "Config") -> list[str]:
    """Open the followers or following dialog and scroll to collect all usernames."""
    profile_url = f"{BASE_URL}/@{username}"
    await page.goto(profile_url, wait_until="domcontentloaded", timeout=30_000)
    await random_delay(cfg.stealth)
    await assert_no_captcha(page, PLATFORM)
    await _raise_for_login_prompt(page, f"{list_type} scrape for @{username}")
    scope = await _read_universal_scope(page)
    target_sec_uid = _follow_list_sec_uid(scope)
    expected_total = _follow_list_expected_total(scope, list_type)

    payload_state, payload_handler = _install_follow_list_capture(page, target_sec_uid, list_type)
    try:
        # Click on followers/following count. TikTok occasionally leaves the page in a
        # half-rendered SPA state, so retry once after a hard reload.
        click_selectors = [
            f"a:has([data-e2e='{list_type}-count'])",
            f"[data-e2e='{list_type}-count']",
            f"a[href$='/{list_type}']",
            f"strong[title*='{list_type}']",
        ]
        clicked = False
        for attempt in range(2):
            for selector in click_selectors:
                try:
                    if not await page.query_selector(selector):
                        continue
                    await page.locator(selector).first.click(timeout=8_000)
                    clicked = True
                    break
                except Exception:
                    try:
                        await human_click(page, selector, cfg.stealth)
                        clicked = True
                        break
                    except Exception:
                        continue

            if clicked:
                break

            await page.reload(wait_until="domcontentloaded", timeout=30_000)
            await random_delay(cfg.stealth)
            await assert_no_captcha(page, PLATFORM)
            await _raise_for_login_prompt(page, f"{list_type} scrape for @{username}")

        if not clicked:
            raise TimeoutError(f"Timed out clicking TikTok {list_type} count for @{username}")

        await _short_wait()

        dialog = None
        deadline = asyncio.get_running_loop().time() + 10
        username_key = username.strip().lower()
        while asyncio.get_running_loop().time() < deadline:
            dialogs = await page.query_selector_all("div[role='dialog']")
            for candidate in dialogs:
                try:
                    text = (await candidate.inner_text()).strip()
                except Exception:
                    continue
                if _dialog_text_requests_login(text):
                    raise LogoutError(f"[tiktok] {list_type} list for @{username} opened a login prompt")
                lowered = text.lower()
                if username_key and username_key in lowered:
                    dialog = candidate
                    break
                if (
                    list_type in lowered
                    and "notifications" not in lowered
                    and await candidate.query_selector("a[href*='/@']")
                ):
                    dialog = candidate
                    break
            if dialog:
                break
            await asyncio.sleep(0.4)

        if not dialog:
            raise TimeoutError(f"Timed out waiting for TikTok {list_type} dialog for @{username}")

        usernames: set[str] = set()
        scroll_attempts_without_new = 0
        max_scroll_attempts = max(20, min(90, (expected_total or 0) // 6 + 20))

        while scroll_attempts_without_new < 8 and max_scroll_attempts > 0:
            max_scroll_attempts -= 1
            prev_count = len(usernames)
            usernames.update(payload_state["usernames"])

            links = await dialog.query_selector_all("a[href*='/@']")
            for link in links:
                href = await link.get_attribute("href")
                if href:
                    m = re.search(r"/@([^/?]+)", href)
                    if m:
                        usernames.add(m.group(1))

            if expected_total and len(usernames) >= expected_total:
                break

            if len(usernames) == prev_count:
                scroll_attempts_without_new += 1
            else:
                scroll_attempts_without_new = 0

            try:
                await dialog.evaluate("""
                    (el) => {
                        const walker = [el, ...el.querySelectorAll("*")];
                        const scrollable = walker.find((node) => {
                            if (!(node instanceof HTMLElement)) return false;
                            const style = window.getComputedStyle(node);
                            return (
                                (style.overflowY === "auto" || style.overflowY === "scroll")
                                && node.scrollHeight > node.clientHeight
                            );
                        });
                        (scrollable || el).scrollTop += 2000;
                    }
                """)
            except Exception:
                await human_scroll(page, "down", cfg.stealth)

            await short_delay(cfg.stealth)

        usernames.update(payload_state["usernames"])

        if expected_total and len(usernames) < expected_total:
            log_event(
                "tiktok",
                "scrape_partial",
                level="warning",
                kind=list_type,
                target=f"@{username}",
                expected=expected_total,
                items=len(usernames),
            )

        result = sorted(usernames)
        log_event("tiktok", "scrape_result", kind=list_type, target=f"@{username}", items=len(result))
        return result
    finally:
        _detach_page_listener(page, "response", payload_handler)


async def _scrape_follow_list_in_fresh_page(page: Page, username: str, list_type: str,
                                            cfg: "Config") -> list[str]:
    """Run a follow-list scrape in a clean page to avoid leftover modal/SPA state."""
    list_page = await page.context.new_page()
    try:
        return await _scrape_follow_list(list_page, username, list_type, cfg)
    finally:
        try:
            await list_page.close()
        except Exception:
            pass


async def scrape_dms(page: Page, own_username: str, conn, cfg: "Config") -> list[dict]:
    """Best-effort scrape of TikTok direct messages for the logged-in account."""
    log_event("tiktok", "scrape_start", kind="dms", account=f"@{own_username}")
    messages_url = f"{BASE_URL}/messages?lang=en"
    await page.goto(messages_url, wait_until="domcontentloaded", timeout=30_000)
    await random_delay(cfg.stealth)
    if "/login" in page.url.lower():
        raise LogoutError(f"[tiktok] DM scrape redirected to login for {own_username}")
    await assert_no_captcha(page, PLATFORM)
    await page.wait_for_timeout(5_000)

    capture_state: dict[str, list[dict] | None] = {"payloads": None}

    async def _capture_response(resp) -> None:
        payloads = capture_state.get("payloads")
        if payloads is None:
            return
        try:
            req = resp.request
            if req.resource_type not in {"xhr", "fetch"}:
                return
            if "tiktok.com" not in resp.url:
                return
            content_type = (resp.headers.get("content-type") or "").lower()
            if "json" not in content_type:
                return
            payloads.append(json.loads(await resp.text()))
        except Exception:
            return

    page.on("response", _capture_response)
    rows = await _tiktok_visible_dm_rows(page)
    conversations: list[dict] = []
    seen_ids: set[str] = set()

    for row in rows:
        await page.goto(messages_url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(1_500)
        capture_state["payloads"] = []
        thread_hint = await _tiktok_open_dm_row(page, row["index"])
        await page.wait_for_timeout(3_500)
        payloads = list(capture_state["payloads"] or [])
        capture_state["payloads"] = None

        messages, metadata = _tiktok_messages_from_payloads(payloads, own_username)
        if not messages:
            messages = await _tiktok_messages_from_dom(page, own_username)
        if not messages:
            continue

        conversation_id = (
            metadata.get("conversation_id")
            or thread_hint
            or row.get("conversation_id")
            or re.sub(r"[^\w.-]+", "_", row.get("text") or f"row_{row['index']}")
        )
        if conversation_id in seen_ids:
            continue
        seen_ids.add(conversation_id)

        title = metadata.get("title") or row.get("text") or None
        participants = metadata.get("participants") or _participants_from_messages(messages, own_username)
        conversations.append(
            {
                "platform": PLATFORM,
                "conversation_id": conversation_id,
                "conversation_type": "group" if len(participants) > 2 else "direct",
                "title": title,
                "subtitle": None,
                "conversation_url": page.url,
                "is_group": len(participants) > 2,
                "is_broadcast_channel": False,
                "last_message_preview": messages[-1].get("text") if messages else None,
                "last_message_at": messages[-1].get("sent_at") if messages else None,
                "participants": participants,
                "messages": messages,
                "metadata": {
                    "source": "network_payload" if payloads else "dom_fallback",
                    **metadata,
                },
            }
        )

    log_event("tiktok", "scrape_result", kind="dms", account=f"@{own_username}", conversations=len(conversations))
    return conversations


async def _tiktok_visible_dm_rows(page: Page) -> list[dict]:
    try:
        rows = await page.evaluate("""
            () => {
                const selectors = [
                    "[data-e2e='chat-item']",
                    "[data-e2e='chat-list-item']",
                    "[data-e2e*='chat']",
                    "[data-e2e*='conversation']",
                    "[class*='ConversationItem']",
                    "[class*='ChatItem']",
                    "aside [role='button']",
                ];
                const elements = [];
                for (const selector of selectors) {
                    for (const el of document.querySelectorAll(selector)) {
                        if (!elements.includes(el)) elements.push(el);
                    }
                }
                return elements.map((el, index) => ({
                    index,
                    text: (el.innerText || el.textContent || "").trim(),
                    href: el.getAttribute("href") || "",
                }));
            }
        """)
    except Exception:
        return []

    result = []
    for row in rows or []:
        text = str(row.get("text") or "").strip()
        if not text or len(text) > 240:
            continue
        result.append(
            {
                "index": int(row.get("index", 0)),
                "text": text,
                "conversation_id": None,
            }
        )
    return result[:100]


async def _tiktok_open_dm_row(page: Page, row_index: int) -> str | None:
    try:
        locator = page.locator(
            "[data-e2e='chat-item'], [data-e2e='chat-list-item'], [data-e2e*='chat'], "
            "[data-e2e*='conversation'], [class*='ConversationItem'], [class*='ChatItem'], "
            "aside [role='button']"
        )
        if row_index >= await locator.count():
            return None
        await locator.nth(row_index).click(timeout=10_000)
        await page.wait_for_timeout(2_000)
    except Exception:
        return None

    match = re.search(r"(conversation_id|chat_id|cid|id)=([^&#]+)", page.url)
    if match:
        return match.group(2)
    return None


def _tiktok_messages_from_payloads(payloads: list[dict], own_username: str) -> tuple[list[dict], dict]:
    messages: list[dict] = []
    metadata: dict = {}
    seen: set[str] = set()
    for payload in payloads:
        for obj in _walk_json(payload):
            candidate = _tiktok_normalize_message_obj(obj, own_username)
            if not candidate:
                continue
            message_id = candidate.get("message_id") or f"synthetic:{candidate.get('sent_at')}:{candidate.get('text')}"
            if message_id in seen:
                continue
            seen.add(message_id)
            messages.append(candidate)
            metadata.setdefault("conversation_id", _first_present(obj, "conversation_id", "conversationId", "chat_id", "dialog_id"))
            metadata.setdefault("title", _first_present(obj, "conversation_name", "conversationName", "title", "nickname"))

    messages.sort(key=lambda item: item.get("sent_at") or "")
    for index, message in enumerate(messages):
        message["message_order"] = index
    return messages, metadata


def _tiktok_normalize_message_obj(obj: dict, own_username: str) -> dict | None:
    keys = set(obj.keys())
    signal_keys = {
        "msg_id", "message_id", "id", "create_time", "send_time", "timestamp",
        "text", "content", "sender_uid", "sender_id", "conversation_id", "conversationId",
    }
    if len(keys & signal_keys) < 2:
        return None

    message_id = _first_present(obj, "msg_id", "message_id", "id")
    sent_at = _coerce_epoch_to_iso(_first_present(obj, "create_time", "send_time", "timestamp"))
    text = _first_present(obj, "text", "content", "text_body", "body")
    sender = obj.get("sender") if isinstance(obj.get("sender"), dict) else {}
    sender_username = (
        _first_present(sender, "username", "unique_id", "nickname")
        or _first_present(obj, "sender_username", "sender_unique_id")
    )
    sender_user_id = _first_present(sender, "uid", "id", "sec_uid") or _first_present(obj, "sender_uid", "sender_id", "from_uid")
    attachments = _generic_attachments_from_obj(obj)
    if not any([message_id, sent_at, text, attachments]):
        return None

    return {
        "message_id": message_id,
        "message_key": message_id,
        "sender_username": sender_username,
        "sender_user_id": sender_user_id,
        "sender_display_name": _first_present(sender, "nickname", "display_name", "name") or sender_username,
        "sent_at": sent_at,
        "sent_at_label": None,
        "message_type": _message_type_from_attachments(attachments, text),
        "text": text,
        "status_text": None,
        "reply_to_message_id": _first_present(obj, "reply_to_message_id", "reply_msg_id"),
        "message_order": None,
        "is_self": bool(sender_username and sender_username == own_username),
        "attachments": attachments,
        "raw_payload": obj,
    }


async def _tiktok_messages_from_dom(page: Page, own_username: str) -> list[dict]:
    try:
        rows = await page.evaluate("""
            () => {
                const nodes = Array.from(document.querySelectorAll("[data-e2e*='message'], [class*='Message']"));
                return nodes.map((el, index) => {
                    const text = (el.innerText || el.textContent || "").trim();
                    const images = Array.from(el.querySelectorAll("img")).map((img, imgIndex) => ({
                        attachment_id: `${index}-img-${imgIndex}`,
                        attachment_order: imgIndex,
                        attachment_type: "photo",
                        source_url: img.getAttribute("src"),
                        preview_url: img.getAttribute("src"),
                        mime_type: "image/jpeg",
                        preview_text: img.getAttribute("alt") || null,
                        metadata: {},
                    }));
                    const videos = Array.from(el.querySelectorAll("video")).map((video, videoIndex) => ({
                        attachment_id: `${index}-video-${videoIndex}`,
                        attachment_order: images.length + videoIndex,
                        attachment_type: "video",
                        source_url: video.getAttribute("src"),
                        preview_url: video.getAttribute("poster"),
                        mime_type: "video/mp4",
                        preview_text: null,
                        metadata: {},
                    }));
                    return {
                        index,
                        text,
                        attachments: [...images, ...videos],
                    };
                }).filter((row) => row.text || row.attachments.length);
            }
        """)
    except Exception:
        return []

    messages = []
    for row in rows or []:
        messages.append(
            {
                "message_id": f"dom:{row['index']}",
                "message_key": f"dom:{row['index']}",
                "sender_username": None,
                "sender_user_id": None,
                "sender_display_name": None,
                "sent_at": None,
                "sent_at_label": None,
                "message_type": _message_type_from_attachments(row.get("attachments") or [], row.get("text")),
                "text": row.get("text") or None,
                "status_text": None,
                "reply_to_message_id": None,
                "message_order": int(row.get("index", 0)),
                "is_self": False,
                "attachments": row.get("attachments") or [],
                "raw_payload": {"source": "dom"},
            }
        )
    return messages


def _participants_from_messages(messages: list[dict], own_username: str) -> list[dict]:
    participants: list[dict] = []
    seen: set[str] = set()
    for message in messages:
        username = message.get("sender_username")
        if username and username not in seen:
            seen.add(username)
            participants.append(
                {
                    "username": username,
                    "user_id": message.get("sender_user_id"),
                    "display_name": message.get("sender_display_name"),
                    "profile_url": f"{BASE_URL}/@{username}" if username else None,
                    "profile_pic_url": None,
                    "is_self": bool(username == own_username),
                    "participant_order": len(participants),
                    "metadata": {},
                }
            )
    if own_username and own_username not in seen:
        participants.append(
            {
                "username": own_username,
                "user_id": None,
                "display_name": own_username,
                "profile_url": f"{BASE_URL}/@{own_username}",
                "profile_pic_url": None,
                "is_self": True,
                "participant_order": len(participants),
                "metadata": {},
            }
        )
    return participants


def _generic_attachments_from_obj(obj: dict) -> list[dict]:
    attachments: list[dict] = []
    candidates = []
    for key in ("attachments", "attachment", "media", "image", "images", "video", "videos", "sticker"):
        value = obj.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            candidates.extend(value)
        else:
            candidates.append(value)

    for index, item in enumerate(candidates):
        if isinstance(item, str):
            source_url = item if item.startswith("http") else None
            if not source_url:
                continue
            attachment_type = "video" if ".mp4" in item else "photo"
            attachments.append(
                {
                    "attachment_id": f"{index}",
                    "attachment_order": index,
                    "attachment_type": attachment_type,
                    "source_url": source_url,
                    "preview_url": source_url,
                    "mime_type": "video/mp4" if attachment_type == "video" else "image/jpeg",
                    "preview_text": None,
                    "metadata": {},
                }
            )
            continue
        if not isinstance(item, dict):
            continue
        source_url = _first_present(item, "url", "uri", "download_url", "src", "image_url", "video_url")
        if not source_url or not str(source_url).startswith("http"):
            continue
        mime_type = _first_present(item, "mime_type", "mimeType")
        attachment_type = "video" if "video" in str(mime_type or source_url) else "photo"
        attachments.append(
            {
                "attachment_id": _first_present(item, "id", "attachment_id", "media_id") or f"{index}",
                "attachment_order": index,
                "attachment_type": attachment_type,
                "source_url": source_url,
                "preview_url": _first_present(item, "preview_url", "poster", "cover_url", "thumbnail"),
                "mime_type": mime_type or ("video/mp4" if attachment_type == "video" else "image/jpeg"),
                "preview_text": _first_present(item, "alt", "caption", "title"),
                "metadata": {"raw_attachment": item},
            }
        )
    return attachments


def _message_type_from_attachments(attachments: list[dict], text: str | None) -> str:
    if attachments:
        return "video" if attachments[0].get("attachment_type") == "video" else "photo"
    if text:
        return "text"
    return "unknown"


def _walk_json(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _first_present(obj: dict, *keys):
    for key in keys:
        value = obj.get(key)
        if value not in {None, ""}:
            return value
    return None


def _coerce_int(value) -> int | None:
    if value in {None, ""}:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_epoch_to_iso(value) -> str | None:
    if value in {None, ""}:
        return None
    try:
        raw = int(str(value))
    except Exception:
        return None
    if raw > 10_000_000_000:
        timestamp = raw / 1000
    else:
        timestamp = raw
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None


# ─── Helpers ─────────────────────────────────────────────────────────────────

async def _detect_captcha(page: Page) -> bool:
    url = page.url.lower()
    for f in ["captcha", "challenge", "verify", "unusual"]:
        if f in url:
            return True
    el = await page.query_selector(
        "iframe[src*='captcha'], #captcha, [class*='captcha'], [id*='captcha'], [class*='secsdk-captcha']"
    )
    if el is not None:
        return True
    try:
        body_text = ((await page.locator("body").inner_text()) or "").lower()
    except Exception:
        body_text = ""
    return any(
        marker in body_text
        for marker in (
            "drag the slider",
            "fit the puzzle",
            "rotate the shape",
            "complete the puzzle",
            "security verification",
        )
    )




def _parse_count_text(value: str | None) -> int | None:
    """Parse compact TikTok count text such as 1.2K or 3M."""
    if not value:
        return None
    text = value.strip().upper().replace(",", "")
    if not text:
        return None
    try:
        if text.endswith("K"):
            return int(float(text[:-1]) * 1_000)
        if text.endswith("M"):
            return int(float(text[:-1]) * 1_000_000)
        if text.endswith("B"):
            return int(float(text[:-1]) * 1_000_000_000)
        return int(text) if text.isdigit() else None
    except ValueError:
        return None


async def _read_text(page: Page, selectors: list[str]) -> str | None:
    for selector in selectors:
        try:
            element = await page.query_selector(selector)
            if not element:
                continue
            text = (await element.inner_text()).strip()
            if text:
                return text
        except Exception:
            continue
    return None


async def _read_attr(page: Page, selectors: list[str], attr: str) -> str | None:
    for selector in selectors:
        try:
            element = await page.query_selector(selector)
            if not element:
                continue
            value = await element.get_attribute(attr)
            if value:
                return value
        except Exception:
            continue
    return None
