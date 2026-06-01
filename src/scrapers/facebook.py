"""
scrapers/facebook.py — Facebook scraper.

Handles login, post scraping, story scraping, and friends/followers list scraping.

Notes:
- Facebook's UI is heavily React-based and frequently changes its class names.
  Selectors here target ARIA roles and data-testid attributes which tend to be
  more stable than generated class names.
- Facebook often shows "one-tap login" or cookie consent dialogs — these are
  handled in the login flow.
- Friends and followers lists require being logged in as the account owner.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

from loguru import logger
from playwright.async_api import Page

from src.browser.human import human_click, human_scroll, human_type, random_delay, short_delay
from src.browser.stealth import CaptchaError, LogoutError, assert_no_captcha
from src.logging_utils import log_event
from src.utils import (
    facebook_profile_url,
    normalize_facebook_profile_identifier,
    short_wait as _short_wait,
)

if TYPE_CHECKING:
    from src.config import Config, StealthConfig

PLATFORM = "facebook"
BASE_URL = "https://www.facebook.com"
_FB_PHOTO_SECTION_TOKENS = ("sk=photos", "/photos", "photos_by", "photos_of", "photos_all")
_FB_ALBUM_SECTION_TOKENS = ("sk=albums", "sk=photos_albums", "/albums", "photos_albums")
_FB_PHOTO_LINK_HINTS = ("/photo/", "story_fbid=", "fbid=", "/videos/")
_FB_ALBUM_LINK_HINTS = ("/media/set/", "/media_set/", "/albums/", "set=a.", "set=oa.")
_FB_CDN_HOST_HINTS = ("fbcdn.net", "fbsbx.com")


# ─── Login ───────────────────────────────────────────────────────────────────

async def check_login_state(page: Page) -> bool:
    """Navigate to Facebook home and verify the user is logged in."""
    try:
        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30_000)
        await _short_wait()

        current_url = page.url.lower()
        if "/login" in current_url or "/checkpoint" in current_url:
            return False

        logged_out_selectors = [
            "form[data-testid='royal_login_form']",
            "form#login_form",
            "input#email",
            "input#pass",
            "a[href*='/login/']",
            "button:has-text('Log in')",
        ]
        for selector in logged_out_selectors:
            try:
                if await page.query_selector(selector):
                    return False
            except Exception:
                continue

        logged_in_selectors = [
            "a[href*='/messages/']",
            "a[aria-label='Messenger']",
            "a[href*='/friends/']",
            "[aria-label='Facebook']",
            "div[role='feed']",
        ]
        for selector in logged_in_selectors:
            try:
                if await page.query_selector(selector):
                    return True
            except Exception:
                continue

        if await _page_has_facebook_auth_cookie(page):
            log_event(
                "facebook",
                "login_state_warning",
                level="warning",
                reason="auth_cookie_present_without_clear_selector",
            )
            return True
        return False
    except Exception as e:
        if await _page_has_facebook_auth_cookie(page):
            log_event(
                "facebook",
                "login_state_warning",
                level="warning",
                reason="auth_cookie_present_after_probe_error",
                error=e,
            )
            return True
        log_event("facebook", "login_state_error", level="warning", error=e)
        return False


async def _page_has_facebook_auth_cookie(page: Page) -> bool:
    """Return True when the current browser context still has Facebook auth cookies."""
    try:
        cookies = await page.context.cookies([BASE_URL])
    except Exception:
        try:
            cookies = await page.context.cookies()
        except Exception:
            return False

    names = {
        cookie.get("name")
        for cookie in cookies
        if isinstance(cookie, dict) and str(cookie.get("value") or "").strip()
    }
    return {"c_user", "xs"}.issubset(names)


async def login(page: Page, username: str, password: str,
                cfg: "StealthConfig") -> bool:
    """
    Log in to Facebook via the email + password form.
    Handles cookie consent, "one-tap" dialogs, and 2FA alerts.
    """
    log_event("facebook", "login_start", account=username)
    await page.goto(f"{BASE_URL}/login", wait_until="domcontentloaded", timeout=30_000)
    await random_delay(cfg)

    # Dismiss cookie consent banner if present
    try:
        consent_btn = await page.query_selector(
            "button[data-cookiebanner='accept_button'], "
            "button[title='Allow all cookies'], "
            "[data-testid='cookie-policy-manage-dialog-accept-button']"
        )
        if consent_btn:
            await consent_btn.click()
            await _short_wait()
    except Exception:
        pass

    if await _detect_captcha(page):
        raise CaptchaError(f"[facebook] CAPTCHA on login page for {username}")

    # Fill email
    await human_type(page, "input#email", username, cfg)
    await _short_wait()

    # Fill password
    await human_type(page, "input#pass", password, cfg)
    await random_delay(cfg)

    # Submit
    await human_click(page, "button[name='login'], input[name='login']", cfg)
    await page.wait_for_load_state("networkidle", timeout=20_000)
    await random_delay(cfg)

    # Handle "Save password?" browser prompt (dismiss)
    try:
        not_now = await page.query_selector("button:has-text('Not Now')")
        if not_now:
            await not_now.click()
    except Exception:
        pass

    # Detect 2FA / checkpoint — headless browser cannot complete this; user must
    # run --interactive-login once to save the session, then bootstrap will reuse it.
    if "two_step_verification" in page.url or "checkpoint" in page.url:
        raise CaptchaError(
            f"[facebook] 2FA/checkpoint required for {username}. "
            "Run:  python3 src/main.py --interactive-login facebook:{username}"
        )

    if await _detect_captcha(page):
        raise CaptchaError(f"[facebook] CAPTCHA after login for {username}")

    logged_in = await check_login_state(page)
    if logged_in:
        log_event("facebook", "login_success", account=username)
    else:
        log_event("facebook", "login_failed", level="error", account=username)
    return logged_in


# ─── Post scraping ───────────────────────────────────────────────────────────

async def scrape_posts(page: Page, target: str, conn, cfg: "Config") -> list[dict]:
    """
    Visit target's Facebook timeline and collect new posts not yet in DB.
    For each post we then navigate to its detail page to get full-res media
    and comments.
    Returns a list of new post dicts.
    """
    log_event("facebook", "scrape_start", kind="posts", target=target)
    profile_url = facebook_profile_url(target) or f"{BASE_URL}/{target}"
    await page.goto(profile_url, wait_until="domcontentloaded", timeout=30_000)
    await random_delay(cfg.stealth)
    await assert_no_captcha(page, PLATFORM)
    feed_posts = await _scrape_post_surface(page, target, cfg, content_scope="feed")
    photo_posts = await _scrape_photo_surface_posts(page, target, cfg)
    posts = _merge_post_candidates(feed_posts, photo_posts)

    # Enrich each post with better media and comments from the detail page.
    # We skip posts without a usable permalink.
    total_posts = len(posts)
    if not posts:
        log_event("facebook", "scrape_result", kind="posts", target=target, items=0)
        return []

    batch_size = 3
    indexed_posts = list(enumerate(posts, start=1))
    enriched: list[dict] = []

    async def _enrich_logged(index: int, post: dict) -> dict:
        log_event(
            "facebook",
            "detail_enrich_start",
            target=target,
            post_id=post.get("post_id"),
            index=index,
            total=total_posts,
            source_surface=_post_source_surface(post),
        )
        result = post
        if post.get("url") and post["url"].startswith("http"):
            result = await _enrich_post_with_detail_page(page, post, cfg)
        log_event(
            "facebook",
            "detail_enrich_result",
            target=target,
            post_id=result.get("post_id"),
            index=index,
            total=total_posts,
            media_items=len(result.get("media_items") or []),
            comments=len(result.get("comments") or []),
        )
        return result

    for offset in range(0, len(indexed_posts), batch_size):
        batch = indexed_posts[offset:offset + batch_size]
        enriched.extend(await asyncio.gather(*(_enrich_logged(index, post) for index, post in batch)))

    log_event("facebook", "scrape_result", kind="posts", target=target, items=len(enriched))
    return enriched


def _merge_post_candidates(*groups: list[dict]) -> list[dict]:
    """Merge post candidates by post_id while preserving discovery order."""
    merged: dict[str, dict] = {}
    ordered_ids: list[str] = []

    for group in groups:
        for post in group or []:
            post_id = str(post.get("post_id") or "").strip()
            if not post_id:
                continue
            if post_id not in merged:
                merged[post_id] = post
                ordered_ids.append(post_id)
                continue

            existing = merged[post_id]
            if not existing.get("collection_name") and post.get("collection_name"):
                existing["collection_name"] = post.get("collection_name")
            if (not existing.get("caption")) and post.get("caption"):
                existing["caption"] = post.get("caption")
            if (not existing.get("posted_at")) and post.get("posted_at"):
                existing["posted_at"] = post.get("posted_at")
            if (not existing.get("url")) and post.get("url"):
                existing["url"] = post.get("url")

            existing_raw = existing.get("raw_metadata")
            if not isinstance(existing_raw, dict):
                existing_raw = {}
            new_raw = post.get("raw_metadata")
            if isinstance(new_raw, dict):
                existing_raw.update(new_raw)
                existing["raw_metadata"] = existing_raw

    return [merged[post_id] for post_id in ordered_ids]


async def _scrape_photo_surface_posts(page: Page, target: str, cfg: "Config") -> list[dict]:
    """Collect photo and album permalink seeds for profiles with sparse timelines."""
    photo_posts: list[dict] = []
    visited_photo_sections: set[str] = set()
    for section_url in _photo_section_urls(target):
        if not section_url or section_url in visited_photo_sections:
            continue
        visited_photo_sections.add(section_url)
        if not await _open_profile_surface_url(page, section_url, cfg, surface="photos"):
            continue
        descriptors = await _collect_facebook_link_descriptors(
            page,
            cfg,
            kind="photo",
            max_scrolls=6,
            max_empty_scrolls=2,
        )
        log_event(
            "facebook",
            "surface_result",
            target=target,
            surface="photos",
            section=section_url,
            items=len(descriptors),
        )
        for descriptor in descriptors:
            seed = _seed_post_from_permalink(
                descriptor["url"],
                source_surface="photos",
            )
            if seed:
                photo_posts.append(seed)

    if not photo_posts and await _open_photo_surface(page, target, cfg):
        descriptors = await _collect_facebook_link_descriptors(
            page,
            cfg,
            kind="photo",
            max_scrolls=6,
            max_empty_scrolls=2,
        )
        for descriptor in descriptors:
            seed = _seed_post_from_permalink(
                descriptor["url"],
                source_surface="photos",
            )
            if seed:
                photo_posts.append(seed)

    album_posts: list[dict] = []
    visited_album_sections: set[str] = set()
    for section_url in _album_section_urls(target):
        if not section_url or section_url in visited_album_sections:
            continue
        visited_album_sections.add(section_url)
        if not await _open_profile_surface_url(page, section_url, cfg, surface="albums"):
            continue
        descriptors = await _collect_facebook_link_descriptors(
            page,
            cfg,
            kind="album",
            max_scrolls=4,
            max_empty_scrolls=2,
        )
        log_event(
            "facebook",
            "surface_result",
            target=target,
            surface="albums",
            section=section_url,
            items=len(descriptors),
        )
        for descriptor in descriptors:
            album_posts.extend(await _scrape_album_photo_seed_posts(page, descriptor, cfg))

    if not album_posts and await _open_album_surface(page, target, cfg):
        for descriptor in await _collect_facebook_link_descriptors(
            page,
            cfg,
            kind="album",
            max_scrolls=4,
            max_empty_scrolls=2,
        ):
            album_posts.extend(await _scrape_album_photo_seed_posts(page, descriptor, cfg))

    results = _merge_post_candidates(photo_posts, album_posts)
    log_event(
        "facebook",
        "scrape_result",
        kind="photo_surfaces",
        target=target,
        items=len(results),
        direct_photos=len(photo_posts),
        album_derived=len(album_posts),
    )
    return results


async def _open_photo_surface(page: Page, target: str, cfg: "Config") -> bool:
    """Best-effort open of the Facebook Photos surface for a target profile."""
    if await _photo_surface_loaded(page):
        return True

    selectors = [
        "[role='tab']:has-text('Photos')",
        "a:has-text('Photos')",
        "div[role='button']:has-text('Photos')",
    ]
    for selector in selectors:
        try:
            if not await page.query_selector(selector):
                continue
            await human_click(page, selector, cfg.stealth)
            await short_delay(cfg.stealth)
            await assert_no_captcha(page, PLATFORM)
            if await _photo_surface_loaded(page):
                return True
        except Exception:
            continue

    for candidate_url in _photo_section_urls(target):
        try:
            await page.goto(candidate_url, wait_until="domcontentloaded", timeout=30_000)
            await random_delay(cfg.stealth)
            await assert_no_captcha(page, PLATFORM)
            if await _photo_surface_loaded(page):
                return True
        except Exception:
            continue

    return False


async def _open_album_surface(page: Page, target: str, cfg: "Config") -> bool:
    """Best-effort open of the Facebook Albums surface for a target profile."""
    if await _album_surface_loaded(page):
        return True

    selectors = [
        "[role='tab']:has-text('Albums')",
        "a:has-text('Albums')",
        "div[role='button']:has-text('Albums')",
    ]
    for selector in selectors:
        try:
            if not await page.query_selector(selector):
                continue
            await human_click(page, selector, cfg.stealth)
            await short_delay(cfg.stealth)
            await assert_no_captcha(page, PLATFORM)
            if await _album_surface_loaded(page):
                return True
        except Exception:
            continue

    for candidate_url in _album_section_urls(target):
        try:
            await page.goto(candidate_url, wait_until="domcontentloaded", timeout=30_000)
            await random_delay(cfg.stealth)
            await assert_no_captcha(page, PLATFORM)
            if await _album_surface_loaded(page):
                return True
        except Exception:
            continue

    return False


async def _open_profile_surface_url(page: Page, section_url: str, cfg: "Config", *, surface: str) -> bool:
    """Open one explicit Facebook profile surface URL and verify it loaded."""
    try:
        await page.goto(section_url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(350)
        await assert_no_captcha(page, PLATFORM)
    except Exception:
        return False

    if surface == "photos":
        return await _photo_surface_loaded(page)
    if surface == "albums":
        return await _album_surface_loaded(page)
    return False


def _photo_section_urls(target: str) -> list[str]:
    """Return candidate Photos URLs for slug and profile.php targets."""
    urls: list[str] = []
    seen: set[str] = set()
    for section in ("photos", "photos_by", "photos_of", "photos_all"):
        candidate = facebook_profile_url(target, section=section)
        if candidate and candidate not in seen:
            urls.append(candidate)
            seen.add(candidate)
    return urls


def _album_section_urls(target: str) -> list[str]:
    """Return candidate Albums URLs for slug and profile.php targets."""
    urls: list[str] = []
    seen: set[str] = set()
    for section in ("albums", "photos_albums"):
        candidate = facebook_profile_url(target, section=section)
        if candidate and candidate not in seen:
            urls.append(candidate)
            seen.add(candidate)
    return urls


async def _photo_surface_loaded(page: Page) -> bool:
    """Heuristic check for a visible Facebook Photos surface."""
    current_url = page.url.lower()
    if any(token in current_url for token in _FB_PHOTO_SECTION_TOKENS):
        return True
    selectors = [
        "[role='tab'][aria-selected='true']:has-text('Photos')",
        "a[aria-current='page']:has-text('Photos')",
        "h1:has-text('Photos')",
    ]
    for selector in selectors:
        try:
            if await page.query_selector(selector):
                return True
        except Exception:
            continue
    return False


async def _album_surface_loaded(page: Page) -> bool:
    """Heuristic check for a visible Facebook Albums surface."""
    current_url = page.url.lower()
    if any(token in current_url for token in _FB_ALBUM_SECTION_TOKENS):
        return True
    selectors = [
        "[role='tab'][aria-selected='true']:has-text('Albums')",
        "a[aria-current='page']:has-text('Albums')",
        "h1:has-text('Albums')",
    ]
    for selector in selectors:
        try:
            if await page.query_selector(selector):
                return True
        except Exception:
            continue
    return False


async def _collect_facebook_link_descriptors(
    page: Page,
    cfg: "Config",
    *,
    kind: str,
    max_scrolls: int | None = None,
    max_empty_scrolls: int | None = None,
) -> list[dict]:
    """Collect unique photo or album links from the current Facebook surface."""
    hints = _FB_PHOTO_LINK_HINTS if kind == "photo" else _FB_ALBUM_LINK_HINTS
    descriptors: list[dict] = []
    seen_keys: set[str] = set()
    scroll_attempts_without_new = 0

    scroll_limit = max_scrolls if max_scrolls is not None else max(cfg.archive.max_post_scrolls, 1)
    empty_limit = (
        max_empty_scrolls
        if max_empty_scrolls is not None
        else max(cfg.archive.max_empty_post_scrolls, 1)
    )

    for _ in range(max(scroll_limit, 1)):
        raw_links: list[dict] = []
        try:
            raw_links = await page.evaluate(
                """
                (patterns) => Array.from(document.querySelectorAll("a[href]"))
                    .map((anchor) => ({
                        href: anchor.href || anchor.getAttribute("href") || "",
                        text: (anchor.innerText || anchor.textContent || "").trim(),
                        aria_label: anchor.getAttribute("aria-label") || "",
                        title: anchor.getAttribute("title") || "",
                    }))
                    .filter((item) => patterns.some((pattern) => item.href.includes(pattern)));
                """,
                list(hints),
            )
        except Exception:
            raw_links = []

        added = 0
        for raw in raw_links or []:
            href = _absolute_facebook_url(raw.get("href"))
            if not href:
                continue
            key = _candidate_descriptor_key(href)
            if not key:
                continue
            if kind == "photo" and not _extract_post_id(href):
                continue
            if kind == "album" and not _extract_album_id(href):
                continue
            if key in seen_keys:
                continue
            seen_keys.add(key)
            descriptors.append(
                {
                    "url": href,
                    "label": _link_text_value(raw),
                }
            )
            added += 1

        if added == 0:
            scroll_attempts_without_new += 1
            if scroll_attempts_without_new >= max(empty_limit, 1):
                break
        else:
            scroll_attempts_without_new = 0

        if await _detect_captcha(page):
            break
        await human_scroll(page, "down", cfg.stealth, total_px=800)
        await short_delay(cfg.stealth)

    return descriptors


def _candidate_descriptor_key(url: str) -> str | None:
    """Return a stable dedupe key for photo and album permalinks."""
    post_id = _extract_post_id(url)
    if post_id:
        return f"post:{post_id}"
    album_id = _extract_album_id(url)
    if album_id:
        return f"album:{album_id}"
    clean = url.split("#", 1)[0].strip()
    return clean or None


def _post_source_surface(post: dict) -> str:
    """Return the discovery surface label for a Facebook post candidate."""
    raw_metadata = post.get("raw_metadata")
    if isinstance(raw_metadata, dict):
        value = str(raw_metadata.get("source_surface") or "").strip().lower()
        if value:
            return value
    return "feed"


def _link_text_value(raw: dict) -> str | None:
    """Choose the most useful short label from a scraped anchor descriptor."""
    for key in ("text", "aria_label", "title"):
        value = str(raw.get(key) or "").strip()
        if not value:
            continue
        for line in value.splitlines():
            clean = " ".join(line.split())
            if clean:
                return clean[:160]
    return None


def _clean_collection_name(value: str | None) -> str | None:
    """Normalize album titles so they stay readable in saved metadata."""
    text = " ".join(str(value or "").replace("| Facebook", "").split()).strip()
    if not text:
        return None
    if text.lower() in {"facebook", "photos", "albums"}:
        return None
    if re.fullmatch(r"\d+\s+(items?|photos?)", text.lower()):
        return None
    return text[:200]


def _seed_post_from_permalink(
    url: str | None,
    *,
    collection_name: str | None = None,
    source_surface: str | None = None,
) -> dict | None:
    """Create a minimal post record that can be enriched via the detail page."""
    absolute_url = _absolute_facebook_url(url)
    if not absolute_url:
        return None

    post_id = _extract_post_id(absolute_url)
    if not post_id:
        return None

    cleaned_collection = _clean_collection_name(collection_name)
    raw_metadata: dict[str, object] = {}
    if source_surface:
        raw_metadata["source_surface"] = source_surface
    if cleaned_collection:
        raw_metadata["collection_name_source"] = cleaned_collection

    return {
        "platform": PLATFORM,
        "post_id": post_id,
        "post_type": "photo",
        "content_scope": "feed",
        "collection_name": cleaned_collection,
        "url": absolute_url,
        "caption": None,
        "posted_at": None,
        "is_repost": False,
        "repost_source_url": None,
        "repost_source_account": None,
        "media_items": [],
        "comments": [],
        "comments_supported": True,
        "comments_snapshot_complete": False,
        "comments_unavailable_reason": "Pending detail-page fetch.",
        "raw_metadata": raw_metadata,
    }


async def _scrape_album_photo_seed_posts(page: Page, descriptor: dict, cfg: "Config") -> list[dict]:
    """Open one album page and harvest photo seeds from within it."""
    album_url = descriptor.get("url")
    if not album_url:
        return []

    try:
        await page.goto(album_url, wait_until="domcontentloaded", timeout=30_000)
        await random_delay(cfg.stealth)
        await assert_no_captcha(page, PLATFORM)
    except Exception as exc:
        logger.debug(f"[facebook] album surface load failed for {album_url}: {exc}")
        return []

    collection_name = _clean_collection_name(descriptor.get("label"))
    if not collection_name:
        collection_name = _clean_collection_name(
            await _read_attr(page, ["meta[property='og:title']"], "content")
            or await _read_text(page, ["[role='main'] h1", "h1"])
        )

    seeds: list[dict] = []
    for item in await _collect_facebook_link_descriptors(
        page,
        cfg,
        kind="photo",
        max_scrolls=6,
        max_empty_scrolls=2,
    ):
        seed = _seed_post_from_permalink(
            item["url"],
            collection_name=collection_name,
            source_surface="albums",
        )
        if seed:
            seeds.append(seed)

    return seeds


def _absolute_facebook_url(url: str | None) -> str | None:
    """Normalise Facebook links to absolute https URLs without fragments."""
    raw = str(url or "").strip()
    if not raw:
        return None

    candidate = urljoin(f"{BASE_URL}/", raw)
    parsed = urlparse(candidate)
    host = parsed.netloc.lower()
    if not host or not (host.endswith("facebook.com") or host.endswith("fb.com")):
        return None

    return parsed._replace(
        scheme="https",
        netloc="www.facebook.com" if host.endswith("facebook.com") else parsed.netloc,
        fragment="",
    ).geturl()


async def _scrape_post_surface(
    page: Page,
    target: str,
    cfg: "Config",
    *,
    content_scope: str,
) -> list[dict]:
    """Scrape whichever Facebook post surface is currently open."""
    new_posts: list[dict] = []
    seen_ids: set[str] = set()
    scroll_attempts_without_new = 0

    for _ in range(max(cfg.archive.max_post_scrolls, 1)):
        post_elements = await page.query_selector_all(
            "div[data-pagelet*='FeedUnit'], "
            "div[role='article'][aria-label]"
        )

        for el in post_elements:
            post_data = await _extract_post_metadata(page, el, target)
            if not post_data:
                continue
            post_data["content_scope"] = content_scope
            if post_data["post_id"] in seen_ids:
                continue
            seen_ids.add(post_data["post_id"])
            new_posts.append(post_data)

        prev_count = len(new_posts)
        await human_scroll(page, "down", cfg.stealth, total_px=800)
        if len(new_posts) == prev_count:
            scroll_attempts_without_new += 1
            if scroll_attempts_without_new >= max(cfg.archive.max_empty_post_scrolls, 1):
                break
        else:
            scroll_attempts_without_new = 0

    return new_posts


async def _extract_post_metadata(page: Page, el, target: str) -> dict | None:
    """Extract metadata from a Facebook post element."""
    try:
        # Try to get a permalink to the post
        permalink = None
        timestamp_link = await el.query_selector(
            "a[href*='/posts/'], a[href*='/photo/'], a[href*='/videos/'], a[href*='story_fbid'], a[href*='fbid=']"
        )
        if timestamp_link:
            permalink = await timestamp_link.get_attribute("href")

        if not permalink:
            return None

        # Normalise URL
        permalink = _absolute_facebook_url(permalink) or permalink

        # Extract a post_id from the URL
        post_id = _extract_post_id(permalink)
        if not post_id:
            return None

        # Caption / text
        caption = None
        try:
            text_el = await el.query_selector(
                "div[data-ad-preview='message'], div[dir='auto']"
            )
            if text_el:
                caption = await text_el.inner_text()
        except Exception:
            pass

        media_items = await _extract_media_items(el, permalink)
        photo_count = sum(1 for item in media_items if item["type"] == "photo")
        video_count = sum(1 for item in media_items if item["type"] == "video")
        has_carousel_navigation = await el.query_selector("[aria-label='Next photo'], [aria-label='Next']") is not None

        post_type = "photo"
        if has_carousel_navigation or photo_count > 1 or video_count > 1:
            post_type = "carousel"
        elif video_count or "/videos/" in permalink:
            post_type = "video"

        # Detect repost (shared post)
        is_repost = False
        repost_source_url = None
        repost_source_account = None
        shared_header = await el.query_selector(
            "a[href*='/posts/']:nth-child(1), div[data-testid='story-subtitle'] a"
        )
        if shared_header:
            inner_text = await shared_header.inner_text()
            if "shared" in inner_text.lower() or "reposted" in inner_text.lower():
                is_repost = True
                repost_source_url = await shared_header.get_attribute("href")

        return {
            "platform": PLATFORM,
            "post_id": post_id,
            "post_type": post_type,
            "content_scope": "feed",
            "collection_name": None,
            "url": permalink,
            "caption": caption,
            "is_repost": is_repost,
            "repost_source_url": _absolute_facebook_url(repost_source_url) or repost_source_url,
            "repost_source_account": repost_source_account,
            "media_items": media_items,
            "comments": [],
            "comments_supported": True,
            "comments_snapshot_complete": False,
            "comments_unavailable_reason": None,
        }
    except Exception as e:
        logger.warning(f"[facebook] Post metadata extraction error: {e}")
        return None


async def scrape_mentions(page: Page, target: str, conn, cfg: "Config") -> list[dict]:
    log_event("facebook", "scrape_start", kind="mentions", target=target)
    if not await _open_mentions_surface(page, target, cfg):
        log_event("facebook", "scrape_result", kind="mentions", target=target, items=0)
        return []

    mentions = await _scrape_post_surface(page, target, cfg, content_scope="mentions")
    log_event("facebook", "scrape_result", kind="mentions", target=target, items=len(mentions))
    return mentions


async def scrape_highlights(page: Page, target: str, conn, cfg: "Config") -> list[dict]:
    log_event("facebook", "scrape_unavailable", kind="highlights", target=target, reason="platform_not_supported")
    return []


async def scrape_profile_metadata(page: Page, target: str, cfg: "Config") -> dict:
    """Read the current Facebook profile header metadata for a target profile."""
    profile_url = facebook_profile_url(target) or f"{BASE_URL}/{target}"
    await page.goto(profile_url, wait_until="domcontentloaded", timeout=30_000)
    await random_delay(cfg.stealth)
    await assert_no_captcha(page, PLATFORM)

    try:
        body_text = await page.locator("body").inner_text()
    except Exception:
        body_text = ""

    profile_pic_url = await _read_attr(
        page,
        [
            "image[xlink\\:href]",
            "svg image",
            "img[alt*='profile picture']",
        ],
        "xlink:href",
    ) or await _read_attr(page, ["img[alt*='profile picture']"], "src")

    cover_photo_url = await _infer_cover_photo_url(page, profile_pic_url)

    return {
        "display_name": await _read_text(page, ["h1"]),
        "bio_snapshot": await _read_text(
            page,
            [
                "[data-pagelet='ProfileTilesFeed_0'] [dir='auto']",
                "[data-pagelet*='ProfileTilesFeed'] [dir='auto']",
                "div[role='main'] [dir='auto']",
            ],
        ),
        "follower_count": _parse_count_from_text(body_text, r"([\d.,KMBkmb]+)\s+followers"),
        "following_count": _parse_count_from_text(body_text, r"([\d.,KMBkmb]+)\s+following"),
        "post_count": None,
        "profile_pic_url": profile_pic_url,
        "cover_photo_url": cover_photo_url,
    }


async def _open_mentions_surface(page: Page, target: str, cfg: "Config") -> bool:
    """Best-effort open of Facebook's mentions/tagged-post surface."""
    profile_url = facebook_profile_url(target) or f"{BASE_URL}/{target}"
    await page.goto(profile_url, wait_until="domcontentloaded", timeout=30_000)
    await random_delay(cfg.stealth)
    await assert_no_captcha(page, PLATFORM)

    selectors = [
        "[role='tab']:has-text('Mentions')",
        "[role='tab']:has-text('Tagged')",
        "a:has-text('Mentions')",
        "a:has-text('Tagged')",
        "div[role='button']:has-text('Mentions')",
        "div[role='button']:has-text('Tagged')",
    ]
    for selector in selectors:
        try:
            if not await page.query_selector(selector):
                continue
            await human_click(page, selector, cfg.stealth)
            await short_delay(cfg.stealth)
            await assert_no_captcha(page, PLATFORM)
            if await _mentions_surface_loaded(page):
                return True
        except Exception:
            continue

    candidate_urls = [profile_url]
    if "profile.php?id=" not in profile_url:
        candidate_urls.extend([
            f"{profile_url}/tagged/",
            f"{profile_url}/mentions",
            f"{profile_url}/allactivity?category_key=posts_you_are_tagged_in",
            f"{profile_url}/allactivity?category_key=tagged_in_posts",
        ])
    for candidate_url in candidate_urls:
        try:
            await page.goto(candidate_url, wait_until="domcontentloaded", timeout=30_000)
            await random_delay(cfg.stealth)
            await assert_no_captcha(page, PLATFORM)
            if await _mentions_surface_loaded(page):
                return True
        except Exception:
            continue

    return False


async def _mentions_surface_loaded(page: Page) -> bool:
    """Heuristic check for a tagged/mentions surface after navigation."""
    current_url = page.url.lower()
    if any(token in current_url for token in ("/tagged", "/mentions", "category_key=posts_you_are_tagged_in", "category_key=tagged_in_posts")):
        return True

    try:
        body_text = (await page.locator("body").inner_text()).lower()
    except Exception:
        return False
    return "mentions" in body_text or "tagged" in body_text


async def _infer_cover_photo_url(page: Page, profile_pic_url: str | None) -> str | None:
    """Extract the visible cover photo with selector and geometry fallbacks."""
    selectors = [
        "img[alt*='cover photo']",
        "a[href*='cover'] img",
        "image[aria-label*='cover']",
        "[data-pagelet='ProfileCover'] img",
    ]
    cover_url = await _read_attr(page, selectors, "src")
    if cover_url and cover_url != profile_pic_url:
        return cover_url

    try:
        result = await page.evaluate("""
            () => {
                const images = Array.from(document.querySelectorAll("img"));
                const candidates = images
                    .map((img) => {
                        const rect = img.getBoundingClientRect();
                        return {
                            src: img.currentSrc || img.src || "",
                            width: img.naturalWidth || rect.width || 0,
                            height: img.naturalHeight || rect.height || 0,
                            top: rect.top || 0,
                        };
                    })
                    .filter((img) => (
                        img.src &&
                        !img.src.startsWith("data:") &&
                        img.width >= 400 &&
                        img.height >= 120 &&
                        img.width > img.height * 1.5 &&
                        img.top < 900
                    ))
                    .sort((a, b) => (b.width * b.height) - (a.width * a.height));
                return candidates.length ? candidates[0].src : null;
            }
        """)
    except Exception:
        return None

    if isinstance(result, str) and result and result != profile_pic_url:
        return result
    return None


def _extract_post_id(url: str) -> str | None:
    """Extract a stable ID from various Facebook URL formats."""
    patterns = [
        r"story_fbid=(\d+)",
        r"[?&]fbid=(\d+)",
        r"/posts/(\d+)",
        r"pfbid=([A-Za-z0-9]+)",
        r"/videos/(\d+)",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def _extract_album_id(url: str) -> str | None:
    """Extract a stable album identifier from Facebook album URLs."""
    patterns = [
        r"[?&]set=(?:a|oa)\.([A-Za-z0-9._-]+)",
        r"/albums/([A-Za-z0-9._-]+)",
    ]
    for pat in patterns:
        match = re.search(pat, url)
        if match:
            return match.group(1)
    return None


def _classify_post_type(url: str, media_items: list[dict], current_type: str | None = None) -> str:
    """Infer the post type from the permalink and resolved media list."""
    if current_type in {"repost", "reel"}:
        return current_type

    photo_count = sum(1 for item in media_items if item.get("type") == "photo")
    video_count = sum(1 for item in media_items if item.get("type") == "video")
    total_count = photo_count + video_count

    if total_count > 1:
        return "carousel"
    if video_count or "/videos/" in url:
        return "video"
    return "photo"


async def _extract_media_items(el, source_url: str) -> list[dict]:
    """Collect large image/video assets from a Facebook feed post element."""
    media_items: list[dict] = []
    seen_urls: set[str] = set()

    videos = await el.query_selector_all("video")
    for video in videos:
        src = await video.get_attribute("src")
        if not src or src in seen_urls or src.startswith("data:") or src.startswith("blob:"):
            continue
        media_items.append({"type": "video", "url": src, "source_url": source_url})
        seen_urls.add(src)

    # Use JS to pick the best-quality URL per image:
    #   1. Largest srcset entry (multiple CDN resolutions, sort by width descriptor)
    #   2. currentSrc (what the browser actually loaded after lazy-load)
    #   3. data-src (lazy-load placeholder)
    #   4. src attribute fallback
    image_data: list[dict] = []
    try:
        image_data = await el.evaluate("""
            (el) => {
                const results = [];
                for (const img of Array.from(el.querySelectorAll("img"))) {
                    let best = null;
                    if (img.srcset) {
                        const parts = img.srcset.split(",")
                            .map(s => s.trim().split(/\\s+/))
                            .filter(p => p.length >= 1 && p[0].startsWith("http"));
                        parts.sort((a, b) => {
                            const wa = parseFloat((a[1] || "0").replace(/[^\\d.]/g, "")) || 0;
                            const wb = parseFloat((b[1] || "0").replace(/[^\\d.]/g, "")) || 0;
                            return wb - wa;
                        });
                        if (parts.length) best = parts[0][0];
                    }
                    if (!best) best = img.currentSrc || img.getAttribute("data-src") || img.src;
                    if (!best || best.startsWith("data:") || best.startsWith("blob:")) continue;
                    results.push({
                        url: best,
                        width: img.naturalWidth || img.clientWidth || 0,
                        height: img.naturalHeight || img.clientHeight || 0,
                    });
                }
                return results;
            }
        """)
    except Exception:
        pass

    for item in (image_data or []):
        url = str(item.get("url") or "")
        width = int(item.get("width") or 0)
        height = int(item.get("height") or 0)
        if not url or url in seen_urls:
            continue
        if _is_static_facebook_asset(url):
            continue
        if width and height and width < 200 and height < 200:
            continue
        media_items.append(
            {
                "type": "photo",
                "url": url,
                "source_url": source_url,
                "width": width,
                "height": height,
            }
        )
        seen_urls.add(url)

    return media_items


def _facebook_media_size_hint(url: str | None) -> int:
    """Infer a rough media-size hint from Facebook CDN URL tokens."""
    raw = str(url or "")
    hints: list[int] = []
    for match in re.finditer(r"(?i)(?:^|[_?&=.-])[ps](\d{2,4})x(\d{2,4})(?:[_?&=.-]|$)", raw):
        hints.extend([int(match.group(1)), int(match.group(2))])
    for match in re.finditer(r"(?i)(?:^|[_?&=.-])w(\d{2,4})(?:[_?&=.-]|$)", raw):
        hints.append(int(match.group(1)))
    for match in re.finditer(r"(?i)(?:^|[_?&=.-])h(\d{2,4})(?:[_?&=.-]|$)", raw):
        hints.append(int(match.group(1)))
    return max(hints) if hints else 0


def _is_static_facebook_asset(url: str | None) -> bool:
    """Return whether a URL points to Facebook UI chrome instead of user media."""
    parsed = urlparse(str(url or ""))
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    return (
        "static.xx.fbcdn.net" in host
        or path.endswith("/rsrc.php")
        or "/rsrc.php/" in path
        or "emoji.php" in path
    )


def _looks_like_facebook_cdn_media(url: str | None) -> bool:
    """Return whether a URL points to a likely Facebook CDN media asset."""
    parsed = urlparse(str(url or ""))
    host = parsed.netloc.lower()
    if _is_static_facebook_asset(url):
        return False
    return any(hint in host for hint in _FB_CDN_HOST_HINTS)


def _is_probable_facebook_photo_asset_url(url: str | None) -> bool:
    """Return whether a URL looks like a real Facebook photo asset."""
    raw = str(url or "").strip()
    if not raw.startswith("http") or _is_static_facebook_asset(raw):
        return False

    parsed = urlparse(raw)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if "scontent" in host:
        return True
    if "fbcdn.net" not in host and "fbsbx.com" not in host:
        return False
    return any(token in path for token in ("/v/t", ".jpg", ".jpeg", ".png", ".webp"))


def _is_probably_low_res_media_url(url: str | None) -> bool:
    """Heuristic detection for Facebook thumbnail-sized image URLs."""
    raw = str(url or "").lower()
    size_hint = _facebook_media_size_hint(raw)
    if 0 < size_hint <= 320:
        return True
    return any(
        token in raw
        for token in (
            "p160x160",
            "s160x160",
            "p200x200",
            "s200x200",
            "p240x240",
            "s240x240",
            "p320x320",
            "s320x320",
        )
    )


def _is_photo_permalink(url: str | None) -> bool:
    """Return whether the current Facebook URL looks like a photo detail page."""
    raw = str(url or "").lower()
    return "fbid=" in raw or "photo.php" in raw or "/photo/" in raw


def _photo_candidate_visual_size(
    url: str | None,
    width: int | None = None,
    height: int | None = None,
) -> int:
    """Estimate the visible size of a discovered Facebook photo candidate."""
    try:
        width_value = int(width or 0)
    except (TypeError, ValueError):
        width_value = 0
    try:
        height_value = int(height or 0)
    except (TypeError, ValueError):
        height_value = 0
    return max(width_value, height_value, _facebook_media_size_hint(url))


def _is_large_photo_candidate(
    url: str | None,
    width: int | None = None,
    height: int | None = None,
    *,
    min_edge: int = 200,
) -> bool:
    """Return whether a photo candidate looks large enough to be a real post photo."""
    return _photo_candidate_visual_size(url, width, height) >= min_edge


def _has_high_confidence_photo_candidate(items: list[dict]) -> bool:
    """Return whether the candidate pool already contains a likely real Facebook photo."""
    for item in items:
        if item.get("type") != "photo":
            continue
        url = item.get("url")
        if not _is_probable_facebook_photo_asset_url(url):
            continue
        if _is_probably_low_res_media_url(url):
            continue
        if _is_large_photo_candidate(url, item.get("width"), item.get("height"), min_edge=240):
            return True
    return False


def _media_candidate_score(item: dict) -> int:
    """Score media candidates so higher-quality Facebook assets win."""
    url = str(item.get("url") or "")
    width = int(item.get("width") or 0)
    height = int(item.get("height") or 0)
    score = _photo_candidate_visual_size(url, width, height)

    if item.get("type") == "video":
        score += 20_000
    if _looks_like_facebook_cdn_media(url):
        score += 5_000
    if item.get("type") == "photo" and _is_probable_facebook_photo_asset_url(url):
        score += 20_000
    score += _facebook_media_size_hint(url)
    if _is_probably_low_res_media_url(url):
        score -= 10_000
    else:
        score += 2_000

    lowered = url.lower()
    if any(token in lowered for token in ("profile_pic", "profilepic", "safe_image", "emoji")):
        score -= 5_000
    return score


def _prefer_media_candidates(
    items: list[dict],
    *,
    prefer_single_photo: bool = False,
) -> list[dict]:
    """Deduplicate and rank discovered media candidates by likely quality."""
    best_by_url: dict[str, dict] = {}
    for item in items:
        url = str(item.get("url") or "").strip()
        media_type = str(item.get("type") or "").strip()
        if media_type not in {"photo", "video"} or not url.startswith("http"):
            continue
        if media_type == "photo" and _is_static_facebook_asset(url):
            continue
        candidate = {
            "type": media_type,
            "url": url,
            "source_url": item.get("source_url"),
            "width": item.get("width"),
            "height": item.get("height"),
        }
        existing = best_by_url.get(url)
        if existing is None or _media_candidate_score(candidate) > _media_candidate_score(existing):
            best_by_url[url] = candidate

    ranked = sorted(best_by_url.values(), key=_media_candidate_score, reverse=True)
    if not ranked:
        return []

    if prefer_single_photo:
        for candidate in ranked:
            if candidate.get("type") == "photo":
                return [
                    {
                        "type": candidate["type"],
                        "url": candidate["url"],
                        "source_url": candidate.get("source_url"),
                    }
                ]

    return [
        {
            "type": candidate["type"],
            "url": candidate["url"],
            "source_url": candidate.get("source_url"),
        }
        for candidate in ranked
    ]


def _has_usable_media_items(items: list[dict]) -> bool:
    """Return whether the current media list contains a likely usable asset."""
    for item in items:
        if item.get("type") == "video":
            return True
        if item.get("type") == "photo" and not _is_probably_low_res_media_url(item.get("url")):
            return True
    return False


async def _wait_for_detail_media(page: Page, timeout_ms: int) -> None:
    """Wait briefly for Facebook detail pages to hydrate their media payloads."""
    try:
        await page.wait_for_function(
            """
            () => Boolean(
                (() => {
                    const isRealPhotoUrl = (value) => {
                        const raw = String(value || "").trim().toLowerCase();
                        if (!raw.startsWith("http")) return false;
                        if (
                            raw.includes("static.xx.fbcdn.net")
                            || raw.includes("/rsrc.php")
                            || raw.includes("emoji.php")
                        ) {
                            return false;
                        }
                        return (
                            raw.includes("scontent")
                            || raw.includes("/v/t")
                            || raw.includes(".jpg")
                            || raw.includes(".jpeg")
                            || raw.includes(".png")
                            || raw.includes("stp=")
                        );
                    };

                    for (const img of Array.from(document.images || [])) {
                        const width = img.naturalWidth || img.clientWidth || img.width || 0;
                        const height = img.naturalHeight || img.clientHeight || img.height || 0;
                        if (Math.max(width, height) < 240) continue;

                        const candidates = [
                            img.currentSrc,
                            img.getAttribute("data-src"),
                            img.getAttribute("src"),
                        ].filter(Boolean);
                        const srcset = img.getAttribute("srcset") || img.srcset || "";
                        for (const entry of srcset.split(",")) {
                            const candidate = entry.trim().split(/\\s+/)[0];
                            if (candidate) candidates.push(candidate);
                        }
                        if (candidates.some(isRealPhotoUrl)) {
                            return true;
                        }
                    }

                    if (document.querySelector("video[src], source[src]")) {
                        return true;
                    }

                    return performance.getEntriesByType("resource").some(
                        (entry) => isRealPhotoUrl(entry && entry.name)
                    );
                })()
            )
            """,
            timeout=timeout_ms,
        )
    except Exception:
        return


async def _extract_primary_photo_candidates(page: Page, source_url: str) -> list[dict]:
    """Collect likely real full-size Facebook photo assets from the current detail page."""
    try:
        raw_items = await page.evaluate(
            """
            () => {
                const results = [];
                const seen = new Set();

                const push = (rawUrl, width, height) => {
                    const url = String(rawUrl || "").trim();
                    if (!url || !url.startsWith("http") || seen.has(url)) return;
                    seen.add(url);
                    results.push({
                        url,
                        width: Number(width) || 0,
                        height: Number(height) || 0,
                    });
                };

                const pushSrcset = (srcset, width, height) => {
                    for (const entry of String(srcset || "").split(",")) {
                        const parts = entry.trim().split(/\\s+/).filter(Boolean);
                        if (!parts.length) continue;
                        const url = parts[0];
                        const descriptor = parts[1] || "";
                        const parsedWidth = descriptor.endsWith("w")
                            ? parseFloat(descriptor.replace(/[^\\d.]/g, "")) || 0
                            : 0;
                        push(url, Math.max(Number(width) || 0, parsedWidth), height);
                    }
                };

                for (const img of Array.from(document.images || [])) {
                    const width = img.naturalWidth || img.clientWidth || img.width || 0;
                    const height = img.naturalHeight || img.clientHeight || img.height || 0;
                    push(img.currentSrc, width, height);
                    push(img.getAttribute("data-src"), width, height);
                    push(img.getAttribute("src"), width, height);
                    pushSrcset(img.getAttribute("srcset") || img.srcset || "", width, height);
                }

                return results;
            }
            """
        )
    except Exception:
        return []

    media_items: list[dict] = []
    for item in raw_items or []:
        url = str(item.get("url") or "").strip()
        width = int(item.get("width") or 0)
        height = int(item.get("height") or 0)
        if not _is_probable_facebook_photo_asset_url(url):
            continue
        if not _is_large_photo_candidate(url, width, height, min_edge=240):
            continue
        media_items.append(
            {
                "type": "photo",
                "url": url,
                "source_url": source_url,
                "width": width,
                "height": height,
            }
        )
    return media_items


def _decode_embedded_facebook_url(raw: str) -> str:
    """Decode Facebook CDN URLs embedded in HTML/JSON strings."""
    return (
        raw.replace("\\u0025", "%")
        .replace("\\u0026", "&")
        .replace("\\u003d", "=")
        .replace("\\u003D", "=")
        .replace("\\/", "/")
        .replace("&amp;", "&")
    )


async def _extract_media_items_from_page_html(page: Page, source_url: str) -> list[dict]:
    """Scan the rendered HTML for Facebook CDN media URLs not present in the DOM yet."""
    try:
        html = await page.content()
    except Exception:
        return []

    candidates: set[str] = set()
    patterns = [
        r"https:\\/\\/[^\"'\\s<>]+",
        r"https://[^\"'\\s<>]+",
    ]
    for pattern in patterns:
        for match in re.findall(pattern, html):
            decoded = _decode_embedded_facebook_url(match)
            if not decoded.startswith("http") or not _looks_like_facebook_cdn_media(decoded):
                continue
            candidates.add(decoded)

    media_items: list[dict] = []
    for candidate in sorted(candidates):
        lowered = candidate.lower()
        media_type = "video" if any(token in lowered for token in (".mp4", "/v/t", "video")) else "photo"
        if media_type == "photo" and not any(
            token in lowered for token in (".jpg", ".jpeg", ".png", ".webp", "stp=", "scontent")
        ):
            continue
        media_items.append({"type": media_type, "url": candidate, "source_url": source_url})

    return media_items


async def _extract_media_items_from_resource_entries(page: Page, source_url: str) -> list[dict]:
    """Read already-loaded browser resource entries for likely Facebook CDN media."""
    try:
        resources = await page.evaluate(
            """
            () => performance.getEntriesByType("resource")
                .map((entry) => entry.name || "")
                .filter(Boolean)
            """
        )
    except Exception:
        return []

    media_items: list[dict] = []
    seen: set[str] = set()
    for resource in resources or []:
        url = str(resource or "")
        if not url.startswith("http") or url in seen or not _looks_like_facebook_cdn_media(url):
            continue
        lowered = url.lower()
        media_type = "video" if any(token in lowered for token in (".mp4", "/v/t", "video")) else "photo"
        if media_type == "photo" and not _is_probable_facebook_photo_asset_url(url):
            continue
        seen.add(url)
        media_items.append({"type": media_type, "url": url, "source_url": source_url})
    return media_items


async def _extract_detail_caption(page: Page) -> str | None:
    """Best-effort extract of the main caption/description from a detail page."""
    candidates: list[str] = []
    meta_description = await _read_attr(
        page,
        ["meta[property='og:description']", "meta[name='description']"],
        "content",
    )
    if meta_description:
        candidates.append(meta_description)

    try:
        visible_texts = await page.evaluate(
            """
            () => Array.from(
                document.querySelectorAll(
                    "[role='main'] div[data-ad-preview='message'], [role='main'] div[dir='auto']"
                )
            )
                .map((el) => (el.innerText || el.textContent || "").trim())
                .filter(Boolean)
            """
        )
    except Exception:
        visible_texts = []

    for text in visible_texts or []:
        candidates.append(text)

    for candidate in candidates:
        cleaned = " ".join(str(candidate or "").replace("| Facebook", "").split()).strip()
        if not cleaned:
            continue
        if cleaned.lower() in {"facebook", "photos", "albums"}:
            continue
        if cleaned.lower().startswith("see translation"):
            continue
        return cleaned[:4000]

    return None


def _parse_datetime_attribute(value: str | None) -> str | None:
    """Normalise Facebook datetime-like attributes to ISO 8601 UTC."""
    raw = str(value or "").strip()
    if not raw:
        return None

    epoch_value = _facebook_coerce_time(raw)
    if epoch_value:
        return epoch_value

    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _extract_detail_posted_at(page: Page) -> str | None:
    """Best-effort extract of the post timestamp from a detail page."""
    selector_specs = [
        ("time[datetime]", "datetime"),
        ("abbr[data-utime]", "data-utime"),
        ("a[href*='comment_id'][data-utime]", "data-utime"),
    ]
    for selector, attribute in selector_specs:
        try:
            element = await page.query_selector(selector)
            if not element:
                continue
            value = await element.get_attribute(attribute)
            parsed = _parse_datetime_attribute(value)
            if parsed:
                return parsed
        except Exception:
            continue

    for selector in (
        "meta[property='article:published_time']",
        "meta[property='og:updated_time']",
    ):
        try:
            value = await page.get_attribute(selector, "content")
        except Exception:
            value = None
        parsed = _parse_datetime_attribute(value)
        if parsed:
            return parsed

    return None


# ─── Post detail enrichment (media + comments) ───────────────────────────────

async def _scrape_post_comments(
    page: Page,
    post_url: str,
    cfg: "Config",
    *,
    navigate: bool = True,
    expand_threads: bool = True,
    timeout_ms: int = 30_000,
) -> list[dict]:
    """
    Navigate to a Facebook post permalink and extract visible comments.
    Tries to expand "View more comments" up to a few times before collecting.
    """
    try:
        if navigate:
            await page.goto(post_url, wait_until="domcontentloaded", timeout=timeout_ms)
            await random_delay(cfg.stealth)
            if await _detect_captcha(page):
                return []

        # Expand visible comment threads
        if expand_threads:
            for _ in range(4):
                try:
                    more_btn = await page.query_selector(
                        "div[role='button']:has-text('View more comments'), "
                        "div[role='button']:has-text('Most relevant'), "
                        "span:has-text('View more comments')"
                    )
                    if not more_btn:
                        break
                    await more_btn.click()
                    await _short_wait()
                except Exception:
                    break

        comments: list[dict] = []
        comment_els = await page.query_selector_all(
            "div[aria-label*='Comment by'], "
            "div[data-testid='UFI2Comment/root_depth_0'], "
            "[role='article'][aria-label*='comment']"
        )

        for el in comment_els:
            try:
                author_link = await el.query_selector("a[role='link']")
                author_name: str | None = None
                author_url: str | None = None
                if author_link:
                    author_name = (await author_link.inner_text()).strip() or None
                    author_url = await author_link.get_attribute("href")
                    if author_url and not author_url.startswith("http"):
                        author_url = BASE_URL + author_url

                text_el = await el.query_selector("[dir='auto']")
                text = ((await text_el.inner_text()).strip() or None) if text_el else None

                commented_at: str | None = None
                time_el = await el.query_selector("a[href*='comment_id'], abbr, time")
                if time_el:
                    ts_str = (
                        await time_el.get_attribute("data-utime")
                        or await time_el.get_attribute("datetime")
                    )
                    if ts_str:
                        try:
                            epoch = int(ts_str)
                            commented_at = datetime.fromtimestamp(
                                epoch, tz=timezone.utc
                            ).strftime("%Y-%m-%dT%H:%M:%SZ")
                        except (ValueError, OSError):
                            commented_at = ts_str

                if not text and not author_name:
                    continue

                comments.append({
                    "comment_id": None,
                    "root_comment_id": None,
                    "reply_to_comment_id": None,
                    "username": _fb_username_from_url(author_url),
                    "user_id": None,
                    "display_name": author_name,
                    "profile_pic_url": None,
                    "is_verified": False,
                    "text": text,
                    "commented_at": commented_at,
                    "commented_at_label": None,
                    "like_count": None,
                    "metadata": {},
                })
            except Exception:
                continue

        return comments
    except Exception as e:
        logger.warning(f"[facebook] Could not scrape comments from {post_url}: {e}")
        return []


async def _enrich_post_with_detail_page(
    page: Page, post: dict, cfg: "Config"
) -> dict:
    """
    Navigate to the post's permalink to get better-quality media and comments.
    Returns the enriched post dict (mutated in-place and also returned).
    """
    url = post.get("url") or ""
    if not url or not url.startswith("http"):
        return post

    source_surface = _post_source_surface(post)
    detail_timeout_ms = 8_000 if source_surface in {"photos", "albums"} else 10_000
    use_fresh_detail_page = True
    prefer_photo_assets = source_surface in {"photos", "albums"} or _is_photo_permalink(url)
    detail_page = page
    opened_temp_page = False

    try:
        if use_fresh_detail_page:
            try:
                detail_page = await page.context.new_page()
                opened_temp_page = True
            except Exception:
                detail_page = page
                opened_temp_page = False

        await detail_page.goto(url, wait_until="domcontentloaded", timeout=detail_timeout_ms)
        await detail_page.wait_for_timeout(250)
        if await _detect_captcha(detail_page):
            return post
        if use_fresh_detail_page:
            await _wait_for_detail_media(detail_page, timeout_ms=min(detail_timeout_ms, 1_500))

        canonical_url = _absolute_facebook_url(
            await _read_attr(detail_page, ["meta[property='og:url']"], "content") or detail_page.url
        )
        if canonical_url:
            post["url"] = canonical_url
            canonical_post_id = _extract_post_id(canonical_url)
            if canonical_post_id:
                post["post_id"] = canonical_post_id

        # Better media: prefer hydrated DOM assets, then fall back to HTML-embedded URLs.
        media_candidates: list[dict] = []
        if prefer_photo_assets:
            media_candidates.extend(
                await _extract_primary_photo_candidates(detail_page, post.get("url") or url)
            )
        try:
            og_image = await detail_page.get_attribute("meta[property='og:image']", "content")
            if (
                og_image
                and og_image.startswith("http")
                and (not prefer_photo_assets or _is_probable_facebook_photo_asset_url(og_image))
            ):
                media_candidates.append({"type": "photo", "url": og_image, "source_url": url})
        except Exception:
            pass

        article = await detail_page.query_selector("[role='article'], [role='main']")
        if article:
            page_media = await _extract_media_items(article, post.get("url") or url)
            media_candidates.extend(page_media)

        should_scan_page_wide = (
            source_surface in {"photos", "albums"}
            or _is_photo_permalink(post.get("url") or url)
            or not _has_usable_media_items(media_candidates)
        )
        if should_scan_page_wide:
            if not _has_high_confidence_photo_candidate(media_candidates):
                body = await detail_page.query_selector("body")
                if body:
                    media_candidates.extend(await _extract_media_items(body, post.get("url") or url))
            if not _has_high_confidence_photo_candidate(media_candidates):
                media_candidates.extend(
                    await _extract_media_items_from_resource_entries(
                        detail_page, post.get("url") or url
                    )
                )

        new_media = _prefer_media_candidates(
            media_candidates,
            prefer_single_photo=use_fresh_detail_page,
        )
        if new_media:
            post["media_items"] = new_media
        post["post_type"] = _classify_post_type(
            post.get("url") or url,
            post.get("media_items") or [],
            post.get("post_type"),
        )

        if not post.get("caption"):
            caption = await _extract_detail_caption(detail_page)
            if caption:
                post["caption"] = caption

        if not post.get("posted_at"):
            posted_at = await _extract_detail_posted_at(detail_page)
            if posted_at:
                post["posted_at"] = posted_at

        post["comments"] = []
        post["comments_supported"] = True
        post["comments_snapshot_complete"] = False
        post["comments_unavailable_reason"] = "Skipped for faster Facebook archive capture."

        raw_metadata = post.get("raw_metadata")
        if not isinstance(raw_metadata, dict):
            raw_metadata = {}
        raw_metadata["detail_page_url"] = post.get("url") or url
        raw_metadata["detail_enriched"] = True
        raw_metadata["detail_timeout_ms"] = detail_timeout_ms
        raw_metadata["media_candidate_count"] = len(media_candidates)
        post["raw_metadata"] = raw_metadata

        if not post.get("media_items"):
            log_event(
                "facebook",
                "detail_media_missing",
                level="warning",
                post_id=post.get("post_id"),
                target=post.get("target_username"),
                source_surface=source_surface,
                url=post.get("url") or url,
            )

    except Exception as e:
        logger.warning(f"[facebook] Detail enrichment failed for {url}: {e}")
    finally:
        if opened_temp_page:
            try:
                await detail_page.close()
            except Exception:
                pass

    return post


def _fb_username_from_url(profile_url: str | None) -> str | None:
    """Extract a Facebook username or numeric ID from a profile URL."""
    normalized = normalize_facebook_profile_identifier(profile_url)
    return normalized or None


# ─── Story scraping ──────────────────────────────────────────────────────────

async def scrape_stories(page: Page, target: str, conn, cfg: "Config") -> list[dict]:
    """
    Check for and capture target's Facebook stories.
    """
    log_event("facebook", "scrape_start", kind="stories", target=target)
    await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30_000)
    await random_delay(cfg.stealth)
    await assert_no_captcha(page, PLATFORM)

    stories: list[dict] = []

    # Look for the target's story card in the story tray at the top of the feed
    story_card = await page.query_selector(
        f"a[href*='{target}'][aria-label*='story'], "
        "div[data-pagelet='StoriesTray'] a"
    )
    if not story_card:
        # Also try visiting the target's profile directly
        profile_url = facebook_profile_url(target) or f"{BASE_URL}/{target}"
        await page.goto(profile_url, wait_until="domcontentloaded", timeout=30_000)
        await _short_wait()
        story_card = await page.query_selector(
            "div[data-pagelet='ProfileTimeline'] div[role='button'] img"
        )

    if not story_card:
        logger.debug(f"[facebook] No stories visible for {target}")
        return []

    try:
        await story_card.click()
        await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        await _short_wait()

        story_count = 0
        while story_count < 30:
            if await _detect_captcha(page):
                raise CaptchaError(f"[facebook] CAPTCHA during story scrape for {target}")

            story_data = await _extract_story_metadata(page, target)
            if story_data:
                stories.append(story_data)

            next_btn = await page.query_selector(
                "div[aria-label='Next card'], button[aria-label='Next']"
            )
            if not next_btn:
                break
            await next_btn.click()
            await _short_wait()
            story_count += 1

    except (CaptchaError, LogoutError):
        raise
    except Exception as e:
        log_event("facebook", "scrape_error", level="warning", kind="stories", target=target, error=e)

    log_event("facebook", "scrape_result", kind="stories", target=target, items=len(stories))
    return stories


async def _extract_story_metadata(page: Page, target: str) -> dict | None:
    """Extract story_id and type from the Facebook story viewer."""
    try:
        url = page.url
        # Facebook story URLs: /stories/{profile_id}/{story_id}
        match = re.search(r"/stories/[^/]+/([A-Za-z0-9]+)", url)
        story_id = match.group(1) if match else url.split("?")[0].split("/")[-1]

        video_el = await page.query_selector("video")
        story_type = "video" if video_el else "image"

        media_url = None
        if video_el:
            media_url = await video_el.get_attribute("src")
        else:
            img_el = await page.query_selector("img[class*='story'], img[data-visualcompletion]")
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
        logger.warning(f"[facebook] Could not extract story metadata: {e}")
        return None


# ─── Follower / friends scraping ─────────────────────────────────────────────

async def scrape_target_connections(page: Page, target_username: str, conn,
                                    cfg: "Config") -> dict[str, list[str]]:
    """
    Scrape connection lists for a monitored Facebook target profile.
    Facebook exposes friends more naturally than separate follower/following lists,
    so the same friend set is stored as both followers and following.
    """
    log_event("facebook", "scrape_start", kind="target_connections", target=target_username)
    friends = await _scrape_friends_list(page, target_username, cfg)
    return {
        "followers": friends,
        "following": friends,
    }

async def scrape_followers(page: Page, own_username: str, conn,
                            cfg: "Config") -> dict:
    """
    Scrape own friends/followers and following list, save snapshots, return delta events.
    Facebook uses "friends" for mutual connections; we treat the full friends list
    as both followers and following (since friendship is mutual).
    """
    from src.db import (
        diff_follower_snapshots,
        get_account_id,
        insert_follower_events,
        save_follower_snapshot,
    )

    log_event("facebook", "scrape_start", kind="followers", account=own_username)
    account_id = get_account_id(conn, PLATFORM, own_username)

    friends = await _scrape_friends_list(page, own_username, cfg)

    # Store as both followers and following since FB friends are mutual
    save_follower_snapshot(conn, account_id, "followers", friends)
    save_follower_snapshot(conn, account_id, "following", friends)

    events: list[dict] = []
    events.extend(diff_follower_snapshots(conn, account_id, "followers"))

    if events:
        insert_follower_events(conn, account_id, events)
        log_event("facebook", "scrape_events", kind="followers", account=own_username, events=len(events))

    return {
        "followers": friends,
        "following": friends,
        "events": events,
    }


_FB_PROFILE_EXCLUDE = frozenset({
    "pages", "groups", "events", "profile.php", "photo", "video",
    "login", "l.php", "sharer", "share", "dialog",
})


async def _scrape_friends_list(page: Page, username: str,
                                cfg: "Config") -> list[str]:
    """
    Collect friend / follower identifiers for a Facebook profile.

    Strategy (tried in order):
    1.  /{username}/friends  — works for the logged-in account's own friends
        and for public profiles that show their friend list.
    2.  /{username}/followers — some public pages expose this separately.
    3.  Profile page sidebar — surfaces the "Friends in common" or visible
        friends section even when the full list is private.
    """
    usernames: set[str] = set()

    async def _harvest_profile_links() -> None:
        link_selectors = [
            "div[data-pagelet*='ProfileAppSection'] a[href*='facebook.com']",
            "a[data-hovercard-prefer-more-content-show='1']",
            "[role='list'] a[href*='facebook.com']",
            "ul a[href*='facebook.com']",
        ]
        for selector in link_selectors:
            try:
                links = await page.query_selector_all(selector)
                for link in links:
                    href = await link.get_attribute("href")
                    if not href:
                        continue
                    m = re.search(r"facebook\.com/([^/?#]+)", href)
                    if m and m.group(1) not in _FB_PROFILE_EXCLUDE:
                        usernames.add(normalize_facebook_profile_identifier(m.group(1)))
                    # Also handle /profile.php?id=...
                    mid = re.search(r"[?&]id=(\d+)", href)
                    if mid:
                        usernames.add(f"profile.php?id={mid.group(1)}")
            except Exception:
                continue

    candidate_urls = [
        facebook_profile_url(username, section="friends") or f"{BASE_URL}/{username}/friends",
        facebook_profile_url(username, section="followers") or f"{BASE_URL}/{username}/followers",
    ]

    for list_url in candidate_urls:
        try:
            await page.goto(list_url, wait_until="domcontentloaded", timeout=30_000)
            await random_delay(cfg.stealth)
            if await _detect_captcha(page):
                continue

            # If redirected to login or profile root, the list is private — skip
            current = page.url.lower()
            if "/login" in current or current.rstrip("/").endswith(username.lower()):
                continue

            # Scroll and harvest
            scroll_attempts_without_new = 0
            while scroll_attempts_without_new < 5:
                prev = len(usernames)
                await _harvest_profile_links()
                if len(usernames) == prev:
                    scroll_attempts_without_new += 1
                else:
                    scroll_attempts_without_new = 0
                await human_scroll(page, "down", cfg.stealth, total_px=800)
                await _short_wait()

        except Exception as e:
            logger.debug(f"[facebook] friends list URL {list_url} failed: {e}")
            continue

    # Fallback: visible friends on the profile page itself
    if not usernames:
        try:
            profile_url = facebook_profile_url(username) or f"{BASE_URL}/{username}"
            await page.goto(profile_url, wait_until="domcontentloaded", timeout=30_000)
            await random_delay(cfg.stealth)
            await _harvest_profile_links()
        except Exception:
            pass

    log_event("facebook", "scrape_result", kind="friends", target=username, items=len(usernames))
    return list(usernames)


async def scrape_dms(page: Page, own_username: str, conn, cfg: "Config") -> list[dict]:
    """Best-effort scrape of Facebook Messenger conversations for the logged-in account."""
    log_event("facebook", "scrape_start", kind="dms", account=own_username)
    messages_url = f"{BASE_URL}/messages/"
    await page.goto(messages_url, wait_until="domcontentloaded", timeout=30_000)
    await random_delay(cfg.stealth)
    if "/login" in page.url.lower():
        raise LogoutError(f"[facebook] DM scrape redirected to login for {own_username}")
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
            if "facebook.com" not in resp.url and "messenger.com" not in resp.url:
                return
            content_type = (resp.headers.get("content-type") or "").lower()
            if "json" not in content_type:
                return
            payloads.append(json.loads(await resp.text()))
        except Exception:
            return

    page.on("response", _capture_response)
    rows = await _facebook_visible_dm_rows(page)
    conversations: list[dict] = []
    seen_ids: set[str] = set()

    for row in rows:
        await page.goto(messages_url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(1_500)
        capture_state["payloads"] = []
        thread_hint = await _facebook_open_dm_row(page, row["index"])
        await page.wait_for_timeout(4_000)
        payloads = list(capture_state["payloads"] or [])
        capture_state["payloads"] = None

        messages, metadata = _facebook_messages_from_payloads(payloads, own_username)
        if not messages:
            messages = await _facebook_messages_from_dom(page, own_username)
        if not messages:
            continue

        conversation_id = (
            metadata.get("conversation_id")
            or thread_hint
            or re.sub(r"[^\w.-]+", "_", row.get("text") or f"row_{row['index']}")
        )
        if conversation_id in seen_ids:
            continue
        seen_ids.add(conversation_id)

        title = metadata.get("title") or row.get("text") or None
        participants = metadata.get("participants") or _facebook_participants_from_messages(messages, own_username)
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

    log_event("facebook", "scrape_result", kind="dms", account=own_username, conversations=len(conversations))
    return conversations


async def _facebook_visible_dm_rows(page: Page) -> list[dict]:
    try:
        rows = await page.evaluate("""
            () => {
                const selectors = [
                    "[role='grid'] [role='row']",
                    "[role='list'] [role='listitem']",
                    "a[href*='/messages/t/']",
                    "div[role='link'][href*='/messages/t/']",
                    "div[aria-label][role='button']",
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
        result.append({"index": int(row.get("index", 0)), "text": text})
    return result[:100]


async def _facebook_open_dm_row(page: Page, row_index: int) -> str | None:
    try:
        locator = page.locator(
            "[role='grid'] [role='row'], [role='list'] [role='listitem'], "
            "a[href*='/messages/t/'], div[aria-label][role='button']"
        )
        if row_index >= await locator.count():
            return None
        await locator.nth(row_index).click(timeout=10_000)
        await page.wait_for_timeout(2_000)
    except Exception:
        return None

    match = re.search(r"/messages/(?:t|e2ee/t)/([^/?#]+)", page.url)
    if match:
        return match.group(1)
    return None


def _facebook_messages_from_payloads(payloads: list[dict], own_username: str) -> tuple[list[dict], dict]:
    messages: list[dict] = []
    metadata: dict = {}
    seen: set[str] = set()
    for payload in payloads:
        for obj in _facebook_walk_json(payload):
            candidate = _facebook_normalize_message_obj(obj, own_username)
            if not candidate:
                continue
            message_id = candidate.get("message_id") or f"synthetic:{candidate.get('sent_at')}:{candidate.get('text')}"
            if message_id in seen:
                continue
            seen.add(message_id)
            messages.append(candidate)
            metadata.setdefault("conversation_id", _facebook_first_present(obj, "thread_key", "thread_id", "conversation_id", "other_user_fbid"))
            metadata.setdefault("title", _facebook_first_present(obj, "thread_name", "name", "title"))

    messages.sort(key=lambda item: item.get("sent_at") or "")
    for index, message in enumerate(messages):
        message["message_order"] = index
    return messages, metadata


def _facebook_normalize_message_obj(obj: dict, own_username: str) -> dict | None:
    keys = set(obj.keys())
    signal_keys = {
        "message_id", "messageId", "id", "timestamp_precise", "timestamp", "snippet",
        "text", "body", "message_sender", "sender_id", "author", "thread_key", "thread_id",
    }
    if len(keys & signal_keys) < 2:
        return None

    message_id = _facebook_first_present(obj, "message_id", "messageId", "id")
    sent_at = _facebook_coerce_time(
        _facebook_first_present(obj, "timestamp_precise", "timestamp", "timestamp_ms", "send_time")
    )
    text = _facebook_first_present(obj, "text", "snippet", "body", "message")
    sender = obj.get("message_sender") if isinstance(obj.get("message_sender"), dict) else {}
    author = obj.get("author") if isinstance(obj.get("author"), dict) else {}
    sender_username = _facebook_first_present(sender, "username", "short_name") or _facebook_first_present(author, "username", "short_name")
    sender_user_id = _facebook_first_present(sender, "id", "user_id") or _facebook_first_present(author, "id", "user_id") or _facebook_first_present(obj, "sender_id", "actor_id")
    attachments = _facebook_generic_attachments(obj)
    if not any([message_id, sent_at, text, attachments]):
        return None

    return {
        "message_id": message_id,
        "message_key": message_id,
        "sender_username": sender_username,
        "sender_user_id": sender_user_id,
        "sender_display_name": _facebook_first_present(sender, "name", "short_name") or _facebook_first_present(author, "name", "short_name") or sender_username,
        "sent_at": sent_at,
        "sent_at_label": None,
        "message_type": "video" if attachments and attachments[0].get("attachment_type") == "video" else ("photo" if attachments else ("text" if text else "unknown")),
        "text": text,
        "status_text": None,
        "reply_to_message_id": _facebook_first_present(obj, "replied_to_message_id", "reply_to_message_id"),
        "message_order": None,
        "is_self": bool(sender_username and sender_username == own_username),
        "attachments": attachments,
        "raw_payload": obj,
    }


async def _facebook_messages_from_dom(page: Page, own_username: str) -> list[dict]:
    try:
        rows = await page.evaluate("""
            () => {
                const nodes = Array.from(document.querySelectorAll("[role='main'] [role='row'], [role='main'] [data-testid]"));
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
                    return {index, text, attachments: [...images, ...videos]};
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
                "message_type": "video" if row.get("attachments") and row["attachments"][0].get("attachment_type") == "video" else ("photo" if row.get("attachments") else "text"),
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


def _facebook_participants_from_messages(messages: list[dict], own_username: str) -> list[dict]:
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
                    "profile_url": None,
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
                "profile_url": None,
                "profile_pic_url": None,
                "is_self": True,
                "participant_order": len(participants),
                "metadata": {},
            }
        )
    return participants


def _facebook_generic_attachments(obj: dict) -> list[dict]:
    attachments: list[dict] = []
    candidates = []
    for key in ("attachments", "attachment", "blob_attachments", "image", "images", "video", "videos", "media"):
        value = obj.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            candidates.extend(value)
        else:
            candidates.append(value)

    for index, item in enumerate(candidates):
        if isinstance(item, str):
            if not item.startswith("http"):
                continue
            attachment_type = "video" if ".mp4" in item else "photo"
            attachments.append(
                {
                    "attachment_id": f"{index}",
                    "attachment_order": index,
                    "attachment_type": attachment_type,
                    "source_url": item,
                    "preview_url": item,
                    "mime_type": "video/mp4" if attachment_type == "video" else "image/jpeg",
                    "preview_text": None,
                    "metadata": {},
                }
            )
            continue
        if not isinstance(item, dict):
            continue
        source_url = _facebook_first_present(item, "playable_url", "image_url", "uri", "url", "preview_url")
        if not source_url or not str(source_url).startswith("http"):
            continue
        mime_type = _facebook_first_present(item, "mime_type", "mimeType")
        attachment_type = "video" if "video" in str(mime_type or source_url) else "photo"
        attachments.append(
            {
                "attachment_id": _facebook_first_present(item, "id", "attachment_id", "legacy_attachment_id") or f"{index}",
                "attachment_order": index,
                "attachment_type": attachment_type,
                "source_url": source_url,
                "preview_url": _facebook_first_present(item, "preview_url", "thumbnail_url", "image_url"),
                "mime_type": mime_type or ("video/mp4" if attachment_type == "video" else "image/jpeg"),
                "preview_text": _facebook_first_present(item, "title", "alt"),
                "metadata": {"raw_attachment": item},
            }
        )
    return attachments


def _facebook_walk_json(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _facebook_walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _facebook_walk_json(child)


def _facebook_first_present(obj: dict, *keys):
    for key in keys:
        value = obj.get(key)
        if value not in {None, ""}:
            return value
    return None


def _facebook_coerce_time(value) -> str | None:
    if value in {None, ""}:
        return None
    text = str(value)
    try:
        raw = int(text)
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
    for f in ["captcha", "checkpoint", "two_step", "unusual_activity", "verify"]:
        if f in url:
            return True
    el = await page.query_selector("iframe[src*='captcha'], #captcha")
    return el is not None




def _parse_count_from_text(body_text: str, pattern: str) -> int | None:
    """Parse follower/following counts from a body-text regex match."""
    match = re.search(pattern, body_text or "", flags=re.IGNORECASE)
    if not match:
        return None
    return _parse_compact_count(match.group(1))


def _parse_compact_count(value: str | None) -> int | None:
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
