"""
scrapers/instagram.py — Instagram scraper.

Handles login, post scraping, story scraping, and follower/following list scraping.
All actions use human mimicry helpers to reduce bot detection.

Notes:
- Instagram's web app is a React SPA — selectors may need updating if IG changes its DOM.
- Follower/following lists require being logged in as the account owner.
- Stories expire after 24h; check interval should be ≤12h to reliably catch them.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urlsplit, urlunsplit

import requests
from loguru import logger
from playwright.async_api import Page, Response

from src.browser.human import human_click, human_scroll, human_type, random_delay, short_delay
from src.browser.stealth import CaptchaError, LogoutError, assert_no_captcha, detect_logout
from src.comment_utils import normalize_comment_records
from src.logging_utils import log_event

if TYPE_CHECKING:
    from src.config import Config, StealthConfig

PLATFORM = "instagram"
BASE_URL = "https://www.instagram.com"
PROFILE_POST_LINK_SELECTOR = (
    "article a[href*='/p/'], article a[href*='/reel/'], article a[href*='/tv/'], "
    "main a[href*='/p/'], main a[href*='/reel/'], main a[href*='/tv/']"
)
FOLLOW_GRAPHQL_QUERIES = {
    "followers": {
        "edge_name": "edge_followed_by",
        "query_hash": "c76146de99bb02f6415203be841dd25a",
    },
    "following": {
        "edge_name": "edge_follow",
        "query_hash": "d04b0a864b4b54837c0d870b0e77e076",
    },
}

# ─── Login ───────────────────────────────────────────────────────────────────

async def check_login_state(page: Page) -> bool:
    """Navigate to Instagram home and verify the user is logged in."""
    try:
        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(800)

        current_url = page.url.lower()
        if "/accounts/login" in current_url or "/accounts/emailsignup" in current_url:
            return False

        logged_out_selectors = [
            "input[name='username']",
            "input[name='email']",
            "input[name='password']",
            "input[name='pass']",
            "form button[type='submit']",
            "form input[type='submit']",
            "a[href='/accounts/login/']",
            "button:has-text('Log in')",
            "input[autocomplete*='username']",
        ]
        for selector in logged_out_selectors:
            try:
                if await page.query_selector(selector):
                    return False
            except Exception:
                continue

        logged_in_selectors = [
            "nav a[href='/direct/inbox/']",
            "nav a[href='/explore/']",
            "a[href='/accounts/activity/']",
            "svg[aria-label='Home']",
            "svg[aria-label='Search']",
        ]
        for selector in logged_in_selectors:
            try:
                if await page.query_selector(selector):
                    return True
            except Exception:
                continue

        if await _page_has_instagram_auth_cookie(page):
            log_event(
                "instagram",
                "login_state_warning",
                level="warning",
                reason="auth_cookie_present_without_clear_selector",
            )
            return True

        return False
    except Exception as e:
        if await _page_has_instagram_auth_cookie(page):
            log_event(
                "instagram",
                "login_state_warning",
                level="warning",
                reason="auth_cookie_present_after_probe_error",
                error=e,
            )
            return True
        log_event("instagram", "login_state_error", level="warning", error=e)
        return False


async def _page_has_instagram_auth_cookie(page: Page) -> bool:
    """Return True when the current browser context still has a sessionid cookie."""
    try:
        cookies = await page.context.cookies([BASE_URL])
    except Exception:
        try:
            cookies = await page.context.cookies()
        except Exception:
            return False

    return any(
        cookie.get("name") == "sessionid" and str(cookie.get("value") or "").strip()
        for cookie in cookies
        if isinstance(cookie, dict)
    )


async def login(page: Page, username: str, password: str,
                cfg: "StealthConfig") -> bool:
    """
    Log in to Instagram with username + password.
    Attempts to handle the "Save info" and notification dialogs that appear post-login.
    Returns True on success.
    """
    log_event("instagram", "login_start", account=username)
    await page.goto(f"{BASE_URL}/accounts/login/", wait_until="domcontentloaded", timeout=30_000)
    await random_delay(cfg)
    await _dismiss_cookie_banner(page, cfg)
    if await _handle_saved_profile_prompt(page, username, cfg):
        log_event("instagram", "login_success", account=username, source="saved_profile_prompt")
        return True

    if await detect_captcha_on_page(page):
        raise CaptchaError(f"[instagram] CAPTCHA on login page for {username}")

    username_selector = await _wait_for_any_selector(
        page,
        [
            "input[name='username']",
            "input[name='email']",
            "input[autocomplete*='username']",
            "input[aria-label='Phone number, username, or email']",
            "form input[type='text']",
        ],
        timeout_ms=25_000,
    )
    password_selector = await _wait_for_any_selector(
        page,
        [
            "input[name='password']",
            "input[name='pass']",
            "input[autocomplete='current-password']",
            "input[aria-label='Password']",
            "form input[type='password']",
        ],
        timeout_ms=10_000,
    )

    if not username_selector or not password_selector:
        if await _handle_saved_profile_prompt(page, username, cfg):
            log_event("instagram", "login_success", account=username, source="saved_profile_prompt")
            return True
        username_selector = await _wait_for_any_selector(
            page,
            [
                "input[name='username']",
                "input[name='email']",
                "input[autocomplete*='username']",
                "input[aria-label='Phone number, username, or email']",
                "form input[type='text']",
            ],
            timeout_ms=10_000,
        )
        password_selector = await _wait_for_any_selector(
            page,
            [
                "input[name='password']",
                "input[name='pass']",
                "input[autocomplete='current-password']",
                "input[aria-label='Password']",
                "form input[type='password']",
            ],
            timeout_ms=5_000,
        )

    if not username_selector or not password_selector:
        debug_path = await _write_login_debug_capture(page, username)
        raise RuntimeError(
            f"[instagram] Login form fields not found for {username} at {page.url}. "
            f"Debug screenshot: {debug_path}"
        )

    # Fill username
    await human_type(page, username_selector, username, cfg)
    await short_delay(cfg)

    # Fill password
    await human_type(page, password_selector, password, cfg)
    await random_delay(cfg)

    # Submit
    if not await _submit_login_form(page, password_selector, cfg):
        debug_path = await _write_login_debug_capture(page, username)
        raise RuntimeError(
            f"[instagram] Login submit action not available for {username} at {page.url}. "
            f"Debug screenshot: {debug_path}"
        )
    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5_000)
        except Exception:
            pass
    await random_delay(cfg)

    # Handle "Save your login info?" dialog
    try:
        save_btn = await page.query_selector("button:has-text('Save Info')")
        if save_btn:
            await human_click(page, "button:has-text('Save Info')", cfg)
            await short_delay(cfg)
    except Exception:
        pass

    # Handle "Turn on notifications?" dialog
    try:
        notif_btn = await page.query_selector("button:has-text('Not Now')")
        if notif_btn:
            await human_click(page, "button:has-text('Not Now')", cfg)
            await short_delay(cfg)
    except Exception:
        pass

    if await detect_captcha_on_page(page):
        raise CaptchaError(f"[instagram] CAPTCHA after login for {username}")

    logged_in = await check_login_state(page)
    if not logged_in:
        debug_path = await _write_login_debug_capture(page, username)
        failure_text = await _describe_login_failure(page)
        log_event(
            "instagram",
            "login_failed",
            level="error",
            account=username,
            page_url=page.url,
            detail=failure_text,
            debug_path=debug_path,
        )
    else:
        log_event("instagram", "login_success", account=username)
    return logged_in


# ─── Post scraping ───────────────────────────────────────────────────────────

async def scrape_posts(
    page: Page,
    target: str,
    conn,
    cfg: "Config",
    *,
    skip_known_existing: bool = False,
) -> list[dict]:
    """
    Visit target's profile and collect any posts not yet in the DB.
    Returns a list of new post dicts ready to pass to db.insert_post().
    """
    from src.db import get_target_id

    log_event("instagram", "scrape_start", kind="posts", target=f"@{target}")
    url = f"{BASE_URL}/{target}/"
    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    await random_delay(cfg.stealth)
    await assert_no_captcha(page, PLATFORM)
    overview = await _refresh_profile_overview(page, target, conn) or {}

    expected_posts = _coerce_int(overview.get("post_count")) or await _get_profile_post_count_hint(page)
    # Collect post links from the grid
    post_links = await _collect_profile_post_links(
        page,
        cfg,
        expected_link_count=expected_posts,
    )
    if not post_links and (expected_posts or 0) > 0:
        fallback_links = await _collect_visible_post_links(page)
        if fallback_links:
            post_links = fallback_links
            log_event(
                "instagram",
                "scrape_recovery",
                level="warning",
                kind="posts",
                target=f"@{target}",
                links=len(post_links),
                source="dom_fallback",
            )

    if (expected_posts or 0) > 0 and len(post_links) < expected_posts:
        log_event(
            "instagram",
            "scrape_retry",
            level="warning",
            kind="posts",
            target=f"@{target}",
            expected=expected_posts,
            captured=len(post_links),
            reason="fresh_page_retry",
        )
        retry_page = await page.context.new_page()
        try:
            await retry_page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await random_delay(cfg.stealth)
            await assert_no_captcha(retry_page, PLATFORM)
            overview = await _refresh_profile_overview(retry_page, target, conn) or overview
            expected_posts = _coerce_int(overview.get("post_count")) or expected_posts
            retry_links = await _collect_profile_post_links(
                retry_page,
                cfg,
                expected_link_count=expected_posts,
            )
            if retry_links:
                post_links = retry_links
            if len(post_links) < (expected_posts or 0):
                fallback_links = await _collect_visible_post_links(retry_page)
                if fallback_links:
                    post_links = list({*post_links, *fallback_links})
                    log_event(
                        "instagram",
                        "scrape_recovery",
                        level="warning",
                        kind="posts",
                        target=f"@{target}",
                        links=len(post_links),
                        source="fresh_page_fallback",
                    )

            if len(post_links) < (expected_posts or 0):
                debug_path = await _write_profile_debug_capture(retry_page, target, "post_grid_empty")
                log_event(
                    "instagram",
                    "scrape_partial",
                    level="warning",
                    kind="posts",
                    target=f"@{target}",
                    expected=expected_posts,
                    captured=len(post_links),
                    debug_path=debug_path,
                )
        finally:
            try:
                await retry_page.close()
            except Exception:
                pass

    log_event("instagram", "scrape_links_found", kind="posts", target=f"@{target}", links=len(post_links))

    try:
        target_id = get_target_id(conn, PLATFORM, target)
    except ValueError:
        from src.db import upsert_target
        target_id = upsert_target(conn, PLATFORM, target)

    if skip_known_existing:
        from src.db import get_existing_post_ids_for_target

        existing_post_ids = get_existing_post_ids_for_target(
            conn,
            target_id,
            content_scope="feed",
            require_media=True,
        )
        if existing_post_ids:
            original_count = len(post_links)
            post_links = [
                link for link in post_links
                if (_post_id_from_url(link) or "") not in existing_post_ids
            ]
            skipped_count = original_count - len(post_links)
            if skipped_count:
                log_event(
                    "instagram",
                    "scrape_skip_existing",
                    kind="posts",
                    target=f"@{target}",
                    skipped=skipped_count,
                )

    new_posts = []
    for post_url in post_links:
        post_data = await _extract_post_metadata(page, post_url, target, cfg)
        if post_data:
            new_posts.append(post_data)

    log_event("instagram", "scrape_result", kind="posts", target=f"@{target}", items=len(new_posts))
    return new_posts


async def _collect_profile_post_links(
    page: Page,
    cfg: "Config",
    *,
    wait_for_grid: bool = True,
    max_scrolls: int | None = None,
    max_empty_scrolls: int | None = None,
    expected_link_count: int | None = None,
) -> list[str]:
    """Scroll the current profile grid and collect post href attributes."""
    links: set[str] = set()
    empty_scrolls = 0
    if wait_for_grid:
        await _wait_for_profile_post_grid(page, cfg)

    scroll_budget = max_scrolls if max_scrolls is not None else cfg.archive.max_post_scrolls
    if expected_link_count and expected_link_count > 0:
        estimated_rows = max((expected_link_count + 2) // 3, 1)
        scroll_budget = max(
            scroll_budget,
            min(estimated_rows + 8, max(cfg.archive.max_post_scrolls * 4, 40)),
        )
    empty_budget = (
        max_empty_scrolls if max_empty_scrolls is not None else cfg.archive.max_empty_post_scrolls
    )
    if expected_link_count and expected_link_count > 24:
        empty_budget = max(empty_budget, 6)

    for _ in range(max(scroll_budget, 1)):
        before = len(links)
        anchors = await page.query_selector_all(PROFILE_POST_LINK_SELECTOR)
        for a in anchors:
            href = await a.get_attribute("href")
            if href:
                links.add(href if href.startswith("http") else BASE_URL + href)
        if expected_link_count and len(links) >= expected_link_count:
            break
        if len(links) == before:
            empty_scrolls += 1
            if empty_scrolls >= max(empty_budget, 1):
                break
        else:
            empty_scrolls = 0
        await human_scroll(page, "down", cfg.stealth, total_px=600)

    return list(links)


def _repost_scroll_limits(cfg: "Config") -> tuple[int, int]:
    """Use a deeper scroll budget for repost surfaces than for normal profile grids."""
    return (
        max(cfg.archive.max_post_scrolls * 3, 30),
        max(cfg.archive.max_empty_post_scrolls + 2, 5),
    )


async def _collect_visible_post_links(page: Page) -> list[str]:
    """Collect visible post/reel/tv hrefs from the current DOM as a fallback."""
    try:
        hrefs = await page.evaluate("""
            () => {
                const found = new Set();
                for (const anchor of document.querySelectorAll("main a[href], article a[href]")) {
                    const href = anchor.getAttribute("href") || "";
                    if (!/\\/(p|reel|tv)\\//.test(href)) continue;
                    found.add(href.startsWith("http") ? href : `${window.location.origin}${href}`);
                }
                return Array.from(found);
            }
        """)
    except Exception:
        return []

    return [href for href in hrefs or [] if isinstance(href, str) and href.startswith("http")]


def _post_id_from_url(post_url: str) -> str | None:
    """Extract the Instagram shortcode from a /p/, /reel/, or /tv/ URL."""
    match = re.search(r"/(?:p|reel|tv)/([A-Za-z0-9_-]+)/?", post_url)
    return match.group(1) if match else None


def _filter_existing_story_rows(
    stories: list[dict],
    existing_story_ids: set[str],
) -> tuple[list[dict], int]:
    """Remove already archived story/highlight items by story_id."""
    if not existing_story_ids:
        return stories, 0
    filtered = [
        story for story in stories
        if story.get("story_id") not in existing_story_ids
    ]
    return filtered, len(stories) - len(filtered)


async def _wait_for_profile_post_grid(page: Page, cfg: "Config") -> None:
    """
    Instagram profile pages often render the header before the post grid hydrates.
    If the profile text says there are posts, wait briefly for the anchors to appear.
    """
    expected_posts = await _get_profile_post_count_hint(page)
    if expected_posts == 0:
        return

    deadline = asyncio.get_running_loop().time() + (12 if expected_posts else 6)
    while asyncio.get_running_loop().time() < deadline:
        try:
            if await page.locator(PROFILE_POST_LINK_SELECTOR).count():
                return
        except Exception:
            pass
        await asyncio.sleep(0.5)

    if expected_posts:
        logger.debug(
            f"[instagram] Profile shows {expected_posts} posts but grid links were not ready yet for {page.url}"
        )


async def _get_profile_post_count_hint(page: Page) -> int | None:
    """Best-effort parse of the visible profile post count from page text."""
    try:
        body_text = await page.locator("body").inner_text()
    except Exception:
        return None

    match = re.search(r"(\d[\d,]*)\s+posts?\b", body_text, flags=re.IGNORECASE)
    if not match:
        return None

    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _parse_count_token(value: str | None) -> int | None:
    """Parse Instagram count tokens such as 851, 1,234, or 1.2M."""
    if not value:
        return None

    token = value.strip().replace(" ", "")
    if not token:
        return None

    multiplier = 1
    suffix = token[-1].lower()
    if suffix in {"k", "m", "b"}:
        multiplier = {
            "k": 1_000,
            "m": 1_000_000,
            "b": 1_000_000_000,
        }[suffix]
        token = token[:-1]

    if multiplier == 1:
        digits = re.sub(r"[^\d]", "", token)
        return int(digits) if digits else None

    normalised = token.replace(",", "")
    try:
        return int(float(normalised) * multiplier)
    except ValueError:
        return None


def _parse_profile_overview_meta(meta: dict, target: str) -> dict | None:
    """Extract current profile metadata from Instagram meta tags."""
    description = html.unescape((meta.get("description") or meta.get("og_description") or "").strip())
    og_title = html.unescape((meta.get("og_title") or "").strip())
    og_image = meta.get("og_image")
    fields: dict[str, object] = {}

    counts_match = re.search(
        r"(?P<followers>[\d.,KMBkmb]+)\s+Followers?,\s*"
        r"(?P<following>[\d.,KMBkmb]+)\s+Following,\s*"
        r"(?P<posts>[\d.,KMBkmb]+)\s+Posts?",
        description,
        flags=re.IGNORECASE,
    )
    if counts_match:
        fields["follower_count"] = _parse_count_token(counts_match.group("followers"))
        fields["following_count"] = _parse_count_token(counts_match.group("following"))
        fields["post_count"] = _parse_count_token(counts_match.group("posts"))

    profile_match = re.search(
        rf"-\s*(?P<display_name>.*?)\s*\(@{re.escape(target)}\)\s*on Instagram"
        r"(?:\s*:\s*\"(?P<bio>.*)\"\s*)?$",
        description,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if profile_match:
        display_name = (profile_match.group("display_name") or "").strip()
        if display_name:
            fields["display_name"] = display_name
        bio_text = profile_match.group("bio")
        fields["bio_snapshot"] = bio_text.strip() if isinstance(bio_text, str) else ""
    elif description:
        fields["bio_snapshot"] = ""

    if "display_name" not in fields and og_title:
        title_match = re.search(
            rf"^(?P<display_name>.*?)\s*\(@{re.escape(target)}\)\s*[•·-]",
            og_title,
        )
        if title_match:
            display_name = (title_match.group("display_name") or "").strip()
            if display_name:
                fields["display_name"] = display_name

    if isinstance(og_image, str) and og_image.strip():
        fields["profile_pic_url"] = og_image.strip()

    return fields or None


async def scrape_profile_metadata(page: Page, target: str, cfg: "Config") -> dict:
    """Read the current Instagram profile header metadata for a target profile."""
    profile_url = f"{BASE_URL}/{target}/"
    await page.goto(profile_url, wait_until="domcontentloaded", timeout=30_000)
    await random_delay(cfg.stealth)
    await assert_no_captcha(page, PLATFORM)

    return await _read_profile_overview_meta(page, target)


async def _read_profile_overview_meta(page: Page, target: str) -> dict:
    """Read profile overview meta tags from the current Instagram page."""

    try:
        meta = await page.evaluate("""
            () => {
                const get = (selector) => {
                    const node = document.querySelector(selector);
                    if (!node) return null;
                    return node.getAttribute("content") || node.content || null;
                };
                return {
                    og_title: get("meta[property='og:title']"),
                    og_description: get("meta[property='og:description']"),
                    description: get("meta[name='description']"),
                    og_image: get("meta[property='og:image']"),
                };
            }
        """)
    except Exception as exc:
        logger.debug(f"[instagram] Could not read profile meta tags for @{target}: {exc}")
        return {}

    return _parse_profile_overview_meta(meta or {}, target) or {}


async def _refresh_profile_overview(page: Page, target: str, conn) -> dict | None:
    """Persist the current Instagram profile header values into the targets table."""
    from src.db import upsert_target

    overview = await _read_profile_overview_meta(page, target)

    if not overview:
        return None

    overview["last_scraped_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        upsert_target(conn, PLATFORM, target, **overview)
    except Exception as exc:
        logger.warning(f"[instagram] Could not persist profile overview for @{target}: {exc}")
        return None
    return overview


async def _wait_for_post_media(page: Page) -> None:
    """Wait briefly for Instagram to hydrate full-size media nodes on the post page."""
    deadline = asyncio.get_running_loop().time() + 8
    while asyncio.get_running_loop().time() < deadline:
        try:
            has_media = await page.evaluate("""
                () => Array.from(document.querySelectorAll('video, img')).some((el) => {
                    const tag = el.tagName.toLowerCase();
                    const url = el.currentSrc || el.src || el.getAttribute('src');
                    const width = tag === 'video'
                        ? (el.videoWidth || el.clientWidth || 0)
                        : (el.naturalWidth || el.clientWidth || 0);
                    const height = tag === 'video'
                        ? (el.videoHeight || el.clientHeight || 0)
                        : (el.naturalHeight || el.clientHeight || 0);
                    if (!url || !url.startsWith('http') || url.startsWith('data:')) return false;
                    if (tag === 'video') return true;
                    return width >= 200 || height >= 200;
                })
            """)
            if has_media:
                return
        except Exception:
            pass
        await asyncio.sleep(0.5)


async def _extract_post_metadata(page: Page, post_url: str, target: str,
                                  cfg: "Config") -> dict | None:
    """Navigate to a post page and extract metadata. Returns None on failure."""
    post_page: Page | None = None
    try:
        post_page = await page.context.new_page()
        await post_page.goto(post_url, wait_until="domcontentloaded", timeout=20_000)
        await short_delay(cfg.stealth)
        await _wait_for_post_media(post_page)

        # Extract native post_id from URL (/p/{post_id}/ or /reel/{post_id}/)
        match = re.search(r"/(p|reel|tv)/([A-Za-z0-9_-]+)/?", post_url)
        if not match:
            return None
        post_id = match.group(2)
        post_type = "reel" if match.group(1) in ("reel", "tv") else "photo"
        embedded_post = await _extract_embedded_post_payload(post_page, post_id)
        embedded_comments = await _extract_all_comments(post_page, embedded_post)
        carousel_media_count = _coerce_int((embedded_post or {}).get("carousel_media_count")) or 0
        has_carousel_navigation = (
            carousel_media_count > 1
            or await post_page.query_selector("button[aria-label='Next']") is not None
        )

        # Caption
        caption = _extract_caption_from_embedded_post(embedded_post)
        if not caption:
            try:
                cap_el = await post_page.query_selector("div[data-testid='post-comment-root'] span")
                if cap_el:
                    caption = await cap_el.inner_text()
            except Exception:
                pass

        media_items = _extract_media_items_from_embedded_post(embedded_post, post_url)
        if not media_items:
            media_items = await _extract_post_media_items(post_page, cfg)
        photo_count = sum(1 for item in media_items if item["type"] == "photo")
        video_count = sum(1 for item in media_items if item["type"] == "video")

        if has_carousel_navigation or photo_count > 1 or video_count > 1:
            post_type = "carousel"
        elif video_count or (embedded_post or {}).get("media_type") == 2:
            post_type = "reel" if match.group(1) in ("reel", "tv") else "video"

        # Like count (best-effort — Instagram often hides this)
        like_count = _coerce_int((embedded_post or {}).get("like_count"))
        if like_count is None:
            try:
                like_el = await post_page.query_selector("section span:has-text('like')")
                if like_el:
                    text = await like_el.inner_text()
                    nums = re.findall(r"[\d,]+", text)
                    if nums:
                        like_count = int(nums[0].replace(",", ""))
            except Exception:
                pass

        comment_count = _coerce_int((embedded_post or {}).get("comment_count"))
        view_count = _coerce_int((embedded_post or {}).get("view_count"))
        posted_at = _timestamp_to_iso((embedded_post or {}).get("taken_at"))
        owner = _extract_owner_from_embedded_post(embedded_post)

        # Detect repost
        is_repost, repost_source_url, repost_source_account = await _detect_repost(post_page)

        return {
            "platform": PLATFORM,
            "post_id": post_id,
            "post_type": post_type,
            "content_scope": "feed",
            "collection_name": None,
            "url": post_url,
            "caption": caption,
            "like_count": like_count,
            "comment_count": comment_count,
            "view_count": view_count,
            "posted_at": posted_at,
            "is_repost": is_repost,
            "repost_source_url": repost_source_url,
            "repost_source_account": repost_source_account,
            "owner": owner,
            "comments": embedded_comments,
            "media_items": media_items,
        }
    except Exception as e:
        logger.warning(f"[instagram] Could not extract post metadata from {post_url}: {e}")
        return None
    finally:
        if post_page is not None:
            try:
                await post_page.close()
            except Exception:
                pass


async def _detect_repost(page: Page) -> tuple[bool, str | None, str | None]:
    """
    Detect if the current post page is a repost/share of another account's content.
    Returns (is_repost, source_url, source_account).
    """
    try:
        # Instagram reshares often include a "Originally shared by @username" header
        repost_el = await page.query_selector("[data-testid='media-share-header'] a")
        if repost_el:
            source_account = await repost_el.inner_text()
            href = await repost_el.get_attribute("href")
            source_url = BASE_URL + href if href else None
            return True, source_url, source_account.lstrip("@")
    except Exception:
        pass
    return False, None, None


async def _embedded_json_payloads(page: Page) -> list[dict]:
    """Parse all inline application/json scripts into Python objects."""
    payloads: list[dict] = []
    try:
        script_texts = await page.eval_on_selector_all(
            "script[type='application/json']",
            "els => els.map(el => el.textContent || '')",
        )
    except Exception:
        return payloads

    for text in script_texts or []:
        if not text:
            continue
        try:
            payload = json.loads(text)
        except Exception:
            continue
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads


def _find_nested_values(obj, target_key: str) -> list[object]:
    """Return every nested value stored under target_key."""
    found: list[object] = []

    def _visit(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == target_key:
                    found.append(value)
                _visit(value)
        elif isinstance(node, list):
            for item in node:
                _visit(item)

    _visit(obj)
    return found


async def _extract_embedded_post_payload(page: Page, post_id: str) -> dict | None:
    """Return the embedded JSON payload for the current post when available."""
    payloads = await _embedded_json_payloads(page)
    for payload in payloads:
        for value in _find_nested_values(payload, "xdt_api__v1__media__shortcode__web_info"):
            if not isinstance(value, dict):
                continue
            for item in value.get("items") or []:
                if isinstance(item, dict) and item.get("code") == post_id:
                    return item
    return None


def _extract_media_pk_from_embedded_post(post: dict | None) -> str | None:
    """Return the numeric Instagram media PK used by the internal comments API."""
    if not isinstance(post, dict):
        return None
    for key in ("pk", "id", "media_id"):
        value = post.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _normalise_comment_record(node: dict, parent_comment_id: str | None = None) -> dict | None:
    """Convert a raw Instagram comment payload into the archive comment shape."""
    if not isinstance(node, dict):
        return None

    comment_id = str(node.get("pk") or node.get("id") or "").strip()
    if not comment_id:
        return None

    user = node.get("user") or node.get("owner") or {}
    comment_text = node.get("text")
    if comment_text is None:
        comment_text = ""
    elif not isinstance(comment_text, str):
        comment_text = str(comment_text)

    parent_id = node.get("parent_comment_id")
    if parent_id is None:
        parent_id = parent_comment_id

    return {
        "comment_id": comment_id,
        "parent_comment_id": str(parent_id).strip() if parent_id not in (None, "") else None,
        "username": user.get("username"),
        "user_id": str(user.get("pk") or user.get("id") or "").strip() or None,
        "display_name": user.get("full_name") or user.get("username"),
        "profile_pic_url": user.get("profile_pic_url"),
        "is_verified": bool(user.get("is_verified")),
        "text": comment_text,
        "commented_at": _timestamp_to_iso(node.get("created_at") or node.get("created_at_utc")),
        "like_count": _coerce_int(node.get("comment_like_count") or node.get("like_count")),
        "reply_count": _coerce_int(node.get("child_comment_count") or node.get("reply_count")) or 0,
    }


def _iter_child_comment_nodes(node: dict) -> list[dict]:
    """Return any embedded child/reply comment payloads attached to a comment node."""
    children: list[dict] = []

    for key in ("preview_child_comments", "child_comments"):
        value = node.get(key)
        if isinstance(value, list):
            children.extend(item for item in value if isinstance(item, dict))

    threaded = node.get("edge_threaded_comments")
    if isinstance(threaded, dict):
        for edge in threaded.get("edges") or []:
            child = edge.get("node") if isinstance(edge, dict) else None
            if isinstance(child, dict):
                children.append(child)

    return children


def _merge_comment_records(existing: dict, incoming: dict) -> None:
    """Update an existing comment snapshot with any richer fields we just discovered."""
    for field in (
        "parent_comment_id",
        "username",
        "user_id",
        "display_name",
        "profile_pic_url",
        "text",
        "commented_at",
        "like_count",
    ):
        if existing.get(field) in (None, "") and incoming.get(field) not in (None, ""):
            existing[field] = incoming[field]

    if incoming.get("is_verified"):
        existing["is_verified"] = True

    existing_reply_count = _coerce_int(existing.get("reply_count")) or 0
    incoming_reply_count = _coerce_int(incoming.get("reply_count")) or 0
    existing["reply_count"] = max(existing_reply_count, incoming_reply_count)


def _append_comment_tree_node(
    node: dict,
    ordered_comments: list[dict],
    comments_by_id: dict[str, dict],
    *,
    parent_comment_id: str | None = None,
) -> None:
    """Append a comment node and any already-attached child replies in depth-first order."""
    record = _normalise_comment_record(node, parent_comment_id)
    if not record:
        return

    comment_id = record["comment_id"]
    existing = comments_by_id.get(comment_id)
    if existing is None:
        comments_by_id[comment_id] = record
        ordered_comments.append(record)
    else:
        _merge_comment_records(existing, record)

    for child in _iter_child_comment_nodes(node):
        _append_comment_tree_node(
            child,
            ordered_comments,
            comments_by_id,
            parent_comment_id=comment_id,
        )


def _extract_comment_nodes_from_api_response(payload: dict) -> list[dict]:
    """Pull comment node lists out of Instagram API responses."""
    for key in ("comments", "child_comments", "comment_threads", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _response_has_more_comments(payload: dict) -> bool:
    """Return whether an Instagram comment response advertises more pages."""
    return bool(
        payload.get("has_more_comments")
        or payload.get("has_more_headload_comments")
        or payload.get("has_more_tail_comments")
        or payload.get("next_max_id")
        or payload.get("next_min_id")
    )


def _response_comment_cursor(payload: dict) -> str | None:
    """Return the pagination cursor for the next comment page when available."""
    for key in ("next_max_id", "next_min_id", "max_id", "min_id"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return None


async def _fetch_instagram_api_json(page: Page, url: str) -> dict | None:
    """Fetch JSON from Instagram using the active logged-in browser session."""
    try:
        result = await page.evaluate("""
            async (requestUrl) => {
                const headers = {
                    accept: "application/json",
                    "x-requested-with": "XMLHttpRequest",
                    "x-ig-app-id": "936619743392459",
                    "x-asbd-id": "359341",
                };

                const csrfMatch = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
                if (csrfMatch) {
                    headers["x-csrftoken"] = decodeURIComponent(csrfMatch[1]);
                }

                const claimSources = [
                    window.localStorage?.getItem("www-claim-v2"),
                    window.sessionStorage?.getItem("www-claim-v2"),
                ];
                const claim = claimSources.find((value) => typeof value === "string" && value.trim());
                if (claim) {
                    headers["x-ig-www-claim"] = claim;
                }

                const response = await fetch(requestUrl, {
                    credentials: "include",
                    headers,
                });
                const text = await response.text();
                let data = null;
                try {
                    data = JSON.parse(text);
                } catch (error) {
                    data = null;
                }
                return {
                    ok: response.ok,
                    status: response.status,
                    data,
                    preview: text.slice(0, 300),
                };
            }
        """, url)
    except Exception as exc:
        logger.debug(f"[instagram] Comments API request failed for {url}: {exc}")
        return None

    if not isinstance(result, dict):
        return None

    data = result.get("data")
    if not result.get("ok") or not isinstance(data, dict):
        logger.debug(
            f"[instagram] Comments API returned status={result.get('status')} for {url}: "
            f"{result.get('preview')!r}"
        )
        return None
    return data


def _response_follow_cursor(payload: dict) -> str | None:
    """Return the pagination cursor for follower/following API pages."""
    for key in ("next_max_id", "max_id", "next_min_id", "min_id"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _extract_follow_entries_from_api_payload(payload: dict) -> list[dict]:
    """Normalise Instagram follower/following API rows into lightweight records."""
    entries: list[dict] = []
    for user in payload.get("users") or []:
        if not isinstance(user, dict):
            continue
        username = str(user.get("username") or "").strip()
        if not username:
            continue
        display_name = str(user.get("full_name") or "").strip() or None
        entries.append({
            "username": username,
            "display_name": display_name,
            "user_id": str(user.get("pk") or user.get("id") or "").strip() or None,
            "profile_pic_url": str(user.get("profile_pic_url") or "").strip() or None,
            "is_verified": bool(user.get("is_verified")),
            "is_private": bool(user.get("is_private")),
        })
    return entries


def _merge_follow_entry(store: dict[str, dict], entry: dict) -> None:
    """Merge one follower/following entry into an identity-keyed map."""
    username = str(entry.get("username") or "").strip()
    if not username:
        return

    user_id = str(entry.get("user_id") or "").strip() or None
    username_key = f"username:{username.casefold()}"
    identity = f"id:{user_id}" if user_id else username_key

    current = store.get(identity)
    if current is None and user_id and username_key in store:
        current = store.pop(username_key)
    if current is None:
        current = {
            "username": username,
            "display_name": None,
            "user_id": user_id,
            "profile_pic_url": None,
            "is_verified": False,
            "is_private": False,
        }

    if entry.get("display_name") and not current.get("display_name"):
        current["display_name"] = entry["display_name"]
    if entry.get("profile_pic_url") and not current.get("profile_pic_url"):
        current["profile_pic_url"] = entry["profile_pic_url"]
    if user_id and not current.get("user_id"):
        current["user_id"] = user_id

    current["username"] = username or current["username"]
    current["is_verified"] = bool(current.get("is_verified")) or bool(entry.get("is_verified"))
    current["is_private"] = bool(current.get("is_private")) or bool(entry.get("is_private"))
    store[identity] = current


def _follow_api_seed_url_from_page(page_resource_urls: list[str], list_type: str) -> str | None:
    """Pick the most recent follower/following API URL seen by the current page."""
    for resource_url in reversed(page_resource_urls):
        lowered = resource_url.lower()
        if (
            "/api/v1/friendships/" in lowered
            and f"/{list_type}/" in lowered
        ):
            return resource_url
    return None


def _follow_api_page_url(seed_url: str, cursor: str | None = None) -> str:
    """Build a paginated follower/following API URL from the captured seed URL."""
    parsed = urlsplit(seed_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["count"] = "25"
    if cursor:
        query["max_id"] = cursor
    else:
        query.pop("max_id", None)
    return urlunsplit(parsed._replace(query=urlencode(query)))


def _follow_completion_floor(expected_count: int | None) -> int | None:
    """Return a practical stop threshold when Instagram displays an approximate total."""
    if not expected_count or expected_count <= 0:
        return None
    tolerance = max(5, int(expected_count * 0.03))
    return max(1, expected_count - tolerance)


def _required_follow_count(expected_count: int | None) -> int | None:
    """
    Return the minimum count required for a trustworthy scrape result.

    Instagram displays exact counts below 10k on profile pages, so we require an
    exact match there. For larger abbreviated counts (e.g. 12.3k), allow a small
    tolerance because the visible profile number is approximate.
    """
    if not expected_count or expected_count <= 0:
        return None
    if expected_count < 10_000:
        return expected_count
    return _follow_completion_floor(expected_count)


def _follow_count_is_complete(collected_count: int, expected_count: int | None) -> bool:
    """Return True when the collected follower/following count is complete enough to trust."""
    required_count = _required_follow_count(expected_count)
    if not required_count:
        return True
    return collected_count >= required_count


def _should_accept_graphql_follow_result(
    *,
    collected_count: int,
    reported_count: int | None,
    expected_count: int | None,
) -> bool:
    """
    Return True when a GraphQL follower/following list is trustworthy enough to keep.

    Instagram occasionally returns a fully paginated identity list while its scalar
    count fields lag behind by one or two accounts. In that case we prefer the full
    identity list over falling back to the far less reliable UI/modal scraper.
    """
    if collected_count <= 0:
        return False

    candidates = [
        count
        for count in (reported_count, expected_count)
        if isinstance(count, int) and count > 0
    ]
    if not candidates:
        return True

    baseline = max(*candidates, collected_count)
    if baseline < 10_000:
        tolerance = 2
    else:
        tolerance = max(3, int(baseline * 0.01))

    nearest_gap = min(abs(collected_count - count) for count in candidates)
    return nearest_gap <= tolerance


def _assert_follow_count_complete(username: str, list_type: str, collected_count: int,
                                  expected_count: int | None) -> None:
    """Raise when Instagram returned a partial follower/following list."""
    required_count = _required_follow_count(expected_count)
    if not required_count or collected_count >= required_count:
        return

    if expected_count and expected_count < 10_000:
        expectation = f"{expected_count} visible on the profile"
    else:
        expectation = f"about {expected_count} visible on the profile"

    raise RuntimeError(
        f"[instagram] Incomplete {list_type} scrape for @{username}: collected {collected_count}, "
        f"but the profile shows {expectation}. Refresh the Instagram session with "
        "`python3 src/main.py --interactive-login instagram:<account>` and retry."
    )


async def _context_has_instagram_auth_cookie(page: Page) -> bool:
    """Return True when the current Playwright context has an Instagram session cookie."""
    try:
        cookies = await page.context.cookies([BASE_URL])
    except Exception:
        cookies = await page.context.cookies()
    return any(
        cookie.get("name") == "sessionid" and str(cookie.get("value") or "").strip()
        for cookie in cookies
    )


async def _context_cookie_map(page: Page) -> dict[str, str]:
    """Return a simple cookie-name map for the current Playwright context."""
    try:
        cookies = await page.context.cookies([BASE_URL])
    except Exception:
        cookies = await page.context.cookies()
    result: dict[str, str] = {}
    for cookie in cookies:
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "").strip()
        if name and value:
            result[name] = value
    return result


def _extract_profile_user_id_from_html(html_text: str) -> str | None:
    """Extract Instagram's numeric profile id from page HTML."""
    patterns = [
        r"profilePage_(\d+)",
        r'"logging_page_id"\s*:\s*"profilePage_(\d+)"',
        r'"profile_id"\s*:\s*"(\d+)"',
        r'"profile_id"\s*:\s*(\d+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, html_text)
        if match:
            return match.group(1)
    return None


async def _resolve_profile_user_id(page: Page, username: str) -> str | None:
    """Resolve a target profile's numeric Instagram user id from its page source."""
    try:
        html_text = await page.content()
    except Exception as exc:
        logger.debug(f"[instagram] Could not read page HTML for @{username}: {exc}")
        return None

    user_id = _extract_profile_user_id_from_html(html_text)
    if user_id:
        return user_id

    logger.debug(f"[instagram] Could not find profilePage_* user id in @{username} page source")
    return None


def _normalise_graphql_follow_node(node: dict) -> dict | None:
    """Normalise one follower/following GraphQL node into our snapshot shape."""
    if not isinstance(node, dict):
        return None

    username = str(node.get("username") or "").strip()
    if not username:
        return None

    return {
        "username": username,
        "display_name": str(node.get("full_name") or "").strip() or None,
        "user_id": str(node.get("id") or "").strip() or None,
        "profile_pic_url": str(node.get("profile_pic_url") or "").strip() or None,
        "is_verified": bool(node.get("is_verified")),
        "is_private": bool(node.get("is_private")),
    }


def _fetch_follow_entries_via_graphql(
    user_id: str,
    session_id: str,
    list_type: str,
    username: str,
) -> dict | None:
    """Fetch the full follower/following list via Instagram's GraphQL endpoint."""
    query = FOLLOW_GRAPHQL_QUERIES.get(list_type)
    if not query:
        return None

    session = requests.Session()
    session.cookies.set("sessionid", session_id, domain="www.instagram.com")
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Referer": f"{BASE_URL}/{username}/",
        }
    )

    seen_cursors: set[str] = set()
    entries_by_identity: dict[str, dict] = {}
    cursor = ""
    reported_count: int | None = None

    for _ in range(1000):
        variables = json.dumps(
            {
                "id": user_id,
                "include_reel": "false",
                "fetch_mutual": "false",
                "first": 50,
                "after": cursor,
            },
            separators=(",", ":"),
        )
        try:
            response = session.get(
                f"{BASE_URL}/graphql/query/",
                params={
                    "variables": variables,
                    "query_hash": query["query_hash"],
                },
                timeout=30,
            )
        except requests.RequestException as exc:
            logger.debug(f"[instagram] GraphQL {list_type} request failed for @{username}: {exc}")
            return None

        if response.status_code != 200:
            logger.debug(
                f"[instagram] GraphQL {list_type} status={response.status_code} for @{username}: "
                f"{response.text[:300]!r}"
            )
            return None

        try:
            payload = response.json()
        except ValueError:
            logger.debug(
                f"[instagram] GraphQL {list_type} returned non-JSON for @{username}: "
                f"{response.text[:300]!r}"
            )
            return None

        edge_payload = (((payload.get("data") or {}).get("user") or {}).get(query["edge_name"]) or {})
        if not isinstance(edge_payload, dict):
            logger.debug(f"[instagram] GraphQL {list_type} payload missing edge data for @{username}")
            return None

        if reported_count is None:
            reported_count = _coerce_int(edge_payload.get("count"))

        for edge in edge_payload.get("edges") or []:
            node = edge.get("node") if isinstance(edge, dict) else None
            entry = _normalise_graphql_follow_node(node)
            if not entry:
                continue
            identity = entry.get("user_id") or entry["username"].casefold()
            current = entries_by_identity.get(identity) or {
                "username": entry["username"],
                "display_name": None,
                "user_id": entry.get("user_id"),
                "profile_pic_url": None,
                "is_verified": False,
                "is_private": False,
            }
            if entry.get("display_name") and not current.get("display_name"):
                current["display_name"] = entry["display_name"]
            if entry.get("profile_pic_url") and not current.get("profile_pic_url"):
                current["profile_pic_url"] = entry["profile_pic_url"]
            if entry.get("user_id") and not current.get("user_id"):
                current["user_id"] = entry["user_id"]
            current["is_verified"] = bool(current.get("is_verified")) or bool(entry.get("is_verified"))
            current["is_private"] = bool(current.get("is_private")) or bool(entry.get("is_private"))
            current["username"] = entry["username"] or current["username"]
            entries_by_identity[identity] = current

        page_info = edge_payload.get("page_info") or {}
        if not page_info.get("has_next_page"):
            break

        next_cursor = str(page_info.get("end_cursor") or "").strip()
        if not next_cursor or next_cursor in seen_cursors:
            break

        seen_cursors.add(next_cursor)
        cursor = next_cursor
        time.sleep(0.35)

    entries = sorted(
        entries_by_identity.values(),
        key=lambda item: (
            str(item.get("username") or "").casefold(),
            str(item.get("user_id") or ""),
        ),
    )
    return {
        "entries": entries,
        "reported_count": reported_count,
    }


async def _scrape_follow_list_via_graphql(
    page: Page,
    username: str,
    list_type: str,
    *,
    expected_count: int | None = None,
) -> list[dict] | None:
    """Fetch followers/following via sessionid + profile user id + GraphQL pagination."""
    cookies = await _context_cookie_map(page)
    session_id = cookies.get("sessionid")
    if not session_id:
        return None

    user_id = await _resolve_profile_user_id(page, username)
    if not user_id:
        return None

    result = await asyncio.to_thread(
        _fetch_follow_entries_via_graphql,
        user_id,
        session_id,
        list_type,
        username,
    )
    if result is None:
        return None

    entries = result.get("entries") or []
    reported_count = _coerce_int(result.get("reported_count"))
    if not _should_accept_graphql_follow_result(
        collected_count=len(entries),
        reported_count=reported_count,
        expected_count=expected_count,
    ):
        if reported_count is not None:
            logger.warning(
                f"[instagram] GraphQL {list_type} for @{username} collected {len(entries)} "
                f"but the endpoint reported {reported_count}; falling back."
            )
        return None

    if (
        reported_count is not None
        and len(entries) != reported_count
    ):
        logger.warning(
            f"[instagram] GraphQL {list_type} for @{username} collected {len(entries)} "
            f"while Instagram reported {reported_count}; keeping the GraphQL identity list."
        )

    if reported_count is not None:
        for entry in entries:
            entry["_source"] = "graphql"
            entry["_reported_count"] = reported_count

    logger.info(
        f"[instagram] GraphQL collected {len(entries)} {list_type} for @{username}"
        + (f" (reported {reported_count})" if reported_count is not None else "")
    )
    return entries


async def _scrape_follow_list_via_instagrapi(page: Page, username: str, list_type: str) -> list[dict] | None:
    """
    Try the Instagram mobile/private API via instagrapi using the live browser session cookies.

    This path is much more reliable than the web modal when the saved browser session
    truly contains a valid authenticated Instagram session.
    """
    try:
        from instagrapi import Client
    except Exception as exc:
        logger.debug(f"[instagram] instagrapi unavailable for {list_type} scrape: {exc}")
        return None

    cookies = await _context_cookie_map(page)
    sessionid = cookies.get("sessionid")
    ds_user_id = cookies.get("ds_user_id")
    if not sessionid or not ds_user_id:
        return None

    def _run() -> list[dict] | None:
        import logging

        # instagrapi's public_request logger is noisy on modern Instagram web
        # responses; we only care about the authenticated private API path here.
        logging.getLogger("public_request").setLevel(logging.CRITICAL)

        client = Client()
        settings = client.get_settings()
        settings["cookies"] = {
            key: value
            for key, value in cookies.items()
            if key in {"sessionid", "csrftoken", "ds_user_id", "mid", "ig_did", "rur"}
        }
        settings["authorization_data"] = {
            "ds_user_id": ds_user_id,
            "sessionid": sessionid,
            "should_use_header_over_cookies": True,
        }
        settings["mid"] = cookies.get("mid")
        rur = cookies.get("rur")
        if rur:
            settings["ig_u_rur"] = rur.strip("\"").split(",", 1)[0]

        client.set_settings(settings)
        client.init()

        target_user = client.user_info_by_username_v1(username)
        target_user_id = str(target_user.pk)
        mapping = (
            client.user_followers(target_user_id, amount=0)
            if list_type == "followers"
            else client.user_following(target_user_id, amount=0)
        )

        entries: list[dict] = []
        for user in mapping.values():
            entries.append({
                "username": getattr(user, "username", None),
                "display_name": getattr(user, "full_name", None) or None,
                "user_id": str(getattr(user, "pk", "") or getattr(user, "id", "") or "").strip() or None,
                "profile_pic_url": getattr(user, "profile_pic_url", None) or None,
                "is_verified": bool(getattr(user, "is_verified", False)),
                "is_private": bool(getattr(user, "is_private", False)),
            })
        return [entry for entry in entries if entry.get("username")]

    try:
        return await asyncio.to_thread(_run)
    except Exception as exc:
        logger.debug(f"[instagram] instagrapi {list_type} scrape failed for @{username}: {exc}")
        return None


def _follow_api_response_matches(url: str, list_type: str) -> bool:
    """Return True when a response URL belongs to the follower/following API stream."""
    lowered = url.lower()
    return "/api/v1/friendships/" in lowered and f"/{list_type}/" in lowered


def _install_follow_api_capture(page: Page, list_type: str):
    """
    Capture the exact JSON pages Instagram loads while the follow modal scrolls.

    This is more reliable than replaying the requests ourselves because the UI is
    already providing the right auth, referer, and pagination state.
    """
    payload_queue: list[dict] = []
    state = {
        "response_count": 0,
        "has_more": None,
        "last_response_at": None,
    }

    async def _listener(response: Response) -> None:
        if not _follow_api_response_matches(response.url, list_type):
            return
        try:
            data = await response.json()
        except Exception:
            return
        if not isinstance(data, dict) or not isinstance(data.get("users"), list):
            return
        payload_queue.append(data)
        state["response_count"] += 1
        if "has_more" in data:
            state["has_more"] = bool(data.get("has_more"))
        state["last_response_at"] = asyncio.get_running_loop().time()

    page.on("response", _listener)

    def _detach() -> None:
        try:
            page.remove_listener("response", _listener)
        except Exception:
            pass

    return payload_queue, state, _detach


async def _scrape_follow_list_via_api(
    page: Page,
    list_type: str,
    *,
    expected_count: int | None = None,
    seed_url: str | None = None,
) -> list[dict] | None:
    """Use Instagram's in-session friendships API when the browser has already opened the list."""
    if not seed_url:
        resource_urls = await page.evaluate("""
            () => performance.getEntriesByType("resource").map((entry) => entry.name)
        """)
        if not isinstance(resource_urls, list):
            return None

        seed_url = _follow_api_seed_url_from_page(
            [str(url) for url in resource_urls if isinstance(url, str)],
            list_type,
        )
    if not seed_url:
        return None

    users: dict[str, dict] = {}
    cursor: str | None = None
    seen_cursors: set[str] = set()
    completion_floor = None
    if expected_count and expected_count > 0:
        tolerance = max(5, int(expected_count * 0.03))
        completion_floor = max(1, expected_count - tolerance)

    for _ in range(200):
        page_url = _follow_api_page_url(seed_url, cursor=cursor)
        payload = await _fetch_instagram_api_json(page, page_url)
        if not payload:
            break

        for entry in _extract_follow_entries_from_api_payload(payload):
            _merge_follow_entry(users, entry)

        if completion_floor and len(users) >= completion_floor:
            logger.info(
                f"[instagram] API pagination reached {len(users)} / expected ~{expected_count} "
                f"{list_type}."
            )
            break

        next_cursor = _response_follow_cursor(payload)
        has_more = bool(payload.get("has_more")) or bool(next_cursor)
        if not has_more or not next_cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    if not users:
        return None
    return sorted(
        users.values(),
        key=lambda item: (
            str(item.get("username") or "").casefold(),
            str(item.get("user_id") or ""),
        ),
    )


async def _fetch_comment_page(
    page: Page,
    media_pk: str,
    *,
    cursor: str | None = None,
) -> dict | None:
    """Fetch one page of top-level comments for a post."""
    url = (
        f"{BASE_URL}/api/v1/media/{media_pk}/comments/"
        f"?can_support_threading=true&permalink_enabled=false"
    )
    if cursor:
        url += f"&max_id={quote(cursor, safe='')}"
    return await _fetch_instagram_api_json(page, url)


async def _fetch_child_comment_page(
    page: Page,
    media_pk: str,
    comment_id: str,
    *,
    cursor: str | None = None,
) -> dict | None:
    """Fetch one page of replies for a single parent comment."""
    url = (
        f"{BASE_URL}/api/v1/media/{media_pk}/comments/{comment_id}/child_comments/"
        f"?can_support_threading=true"
    )
    if cursor:
        url += f"&max_id={quote(cursor, safe='')}"
    return await _fetch_instagram_api_json(page, url)


async def _fetch_all_comment_replies(
    page: Page,
    media_pk: str,
    comment_id: str,
) -> list[dict]:
    """Fetch every paginated reply for a parent comment."""
    replies: list[dict] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()

    for _ in range(50):
        payload = await _fetch_child_comment_page(page, media_pk, comment_id, cursor=cursor)
        if not payload:
            break

        replies.extend(_extract_comment_nodes_from_api_response(payload))

        next_cursor = _response_comment_cursor(payload)
        if not _response_has_more_comments(payload) or not next_cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    return replies


async def _fetch_all_comments_via_api(page: Page, media_pk: str) -> list[dict]:
    """Fetch the full comment tree for a post using Instagram's in-session JSON API."""
    ordered_comments: list[dict] = []
    comments_by_id: dict[str, dict] = {}
    cursor: str | None = None
    seen_cursors: set[str] = set()

    for _ in range(50):
        payload = await _fetch_comment_page(page, media_pk, cursor=cursor)
        if not payload:
            break

        for node in _extract_comment_nodes_from_api_response(payload):
            _append_comment_tree_node(node, ordered_comments, comments_by_id)

        next_cursor = _response_comment_cursor(payload)
        if not _response_has_more_comments(payload) or not next_cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    if not ordered_comments:
        return []

    reply_queue = [
        comment["comment_id"]
        for comment in ordered_comments
        if (_coerce_int(comment.get("reply_count")) or 0) > 0
    ]
    processed_reply_parents: set[str] = set()

    while reply_queue:
        parent_comment_id = reply_queue.pop(0)
        if parent_comment_id in processed_reply_parents:
            continue
        processed_reply_parents.add(parent_comment_id)

        parent_comment = comments_by_id.get(parent_comment_id)
        if not parent_comment:
            continue

        expected_replies = _coerce_int(parent_comment.get("reply_count")) or 0
        current_replies = sum(
            1 for comment in ordered_comments
            if comment.get("parent_comment_id") == parent_comment_id
        )
        if expected_replies <= current_replies:
            continue

        for reply_node in await _fetch_all_comment_replies(page, media_pk, parent_comment_id):
            reply_record = _normalise_comment_record(reply_node, parent_comment_id)
            if not reply_record:
                continue

            already_seen = reply_record["comment_id"] in comments_by_id
            _append_comment_tree_node(
                reply_node,
                ordered_comments,
                comments_by_id,
                parent_comment_id=parent_comment_id,
            )

            if not already_seen and (_coerce_int(reply_record.get("reply_count")) or 0) > 0:
                reply_queue.append(reply_record["comment_id"])

    return normalize_comment_records(ordered_comments)


async def _extract_embedded_comments(page: Page) -> list[dict]:
    """Return the best-effort embedded comment snapshot for the current post page."""
    payloads = await _embedded_json_payloads(page)
    comments: list[dict] = []
    comments_by_id: dict[str, dict] = {}

    for payload in payloads:
        for value in _find_nested_values(payload, "xdt_api__v1__media__media_id__comments__connection"):
            if not isinstance(value, dict):
                continue
            for edge in value.get("edges") or []:
                node = edge.get("node") if isinstance(edge, dict) else None
                if isinstance(node, dict):
                    _append_comment_tree_node(node, comments, comments_by_id)

    return normalize_comment_records(comments)


async def _extract_all_comments(page: Page, embedded_post: dict | None) -> list[dict]:
    """
    Return the fullest comment tree we can recover for the current post page.

    We prefer the in-session Instagram JSON API because it allows paginating the
    full comment tree, including replies. If that path is unavailable, fall back
    to the embedded JSON snapshot already present in the page source.
    """
    media_pk = _extract_media_pk_from_embedded_post(embedded_post)
    if media_pk:
        api_comments = await _fetch_all_comments_via_api(page, media_pk)
        if api_comments:
            return api_comments

    return await _extract_embedded_comments(page)


def _extract_caption_from_embedded_post(post: dict | None) -> str | None:
    caption = (post or {}).get("caption")
    if isinstance(caption, dict):
        text = caption.get("text")
        if isinstance(text, str) and text.strip():
            return text
    return None


def _extract_owner_from_embedded_post(post: dict | None) -> dict | None:
    owner = None
    if isinstance(post, dict):
        owner = post.get("user") or post.get("owner")
    if not isinstance(owner, dict):
        return None
    username = owner.get("username")
    if not username:
        return None
    return {
        "username": username,
        "user_id": str(owner.get("pk") or owner.get("id") or ""),
        "display_name": owner.get("full_name") or username,
        "profile_pic_url": owner.get("profile_pic_url"),
        "is_verified": bool(owner.get("is_verified")),
    }


def _extract_media_items_from_embedded_post(post: dict | None, source_url: str) -> list[dict]:
    """Build photo/video asset URLs from the embedded post payload."""
    if not isinstance(post, dict):
        return []

    media_items: list[dict] = []
    seen_urls: set[str] = set()

    def _append_media(media: dict) -> None:
        video_versions = media.get("video_versions") or []
        if isinstance(video_versions, list):
            for video in video_versions:
                if not isinstance(video, dict):
                    continue
                url = video.get("url")
                if isinstance(url, str) and url.startswith("http") and url not in seen_urls:
                    media_items.append({"type": "video", "url": url, "source_url": source_url})
                    seen_urls.add(url)
                    return

        image_versions = ((media.get("image_versions2") or {}).get("candidates") or [])
        best_image = None
        best_area = -1
        for candidate in image_versions:
            if not isinstance(candidate, dict):
                continue
            url = candidate.get("url")
            if not isinstance(url, str) or not url.startswith("http"):
                continue
            area = int(candidate.get("width") or 0) * int(candidate.get("height") or 0)
            if area > best_area:
                best_area = area
                best_image = url

        if isinstance(best_image, str) and best_image not in seen_urls:
            media_items.append({"type": "photo", "url": best_image, "source_url": source_url})
            seen_urls.add(best_image)

    carousel_media = post.get("carousel_media") or []
    if isinstance(carousel_media, list) and carousel_media:
        for media in carousel_media:
            if isinstance(media, dict):
                _append_media(media)
        return media_items

    _append_media(post)
    return media_items


def _timestamp_to_iso(value) -> str | None:
    """Convert a Unix timestamp into an ISO 8601 UTC string."""
    ts = _coerce_int(value)
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _coerce_int(value) -> int | None:
    """Best-effort integer coercion."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _extract_post_media_items(page: Page, cfg: "Config") -> list[dict]:
    """
    Collect direct media asset URLs from the current Instagram post page.
    For carousels, click through the slides and gather each unique image/video.
    """
    media_items: list[dict] = []
    seen_urls: set[str] = set()

    async def _collect_visible_media() -> None:
        candidates = await page.evaluate("""
            () => {
                const nodes = [];
                document.querySelectorAll("video, img").forEach((el) => {
                    const tag = el.tagName.toLowerCase();
                    const type = tag === "video" ? "video" : "photo";
                    const url = el.currentSrc || el.src || el.getAttribute("src");
                    const width = tag === "video"
                        ? (el.videoWidth || el.clientWidth || 0)
                        : (el.naturalWidth || el.clientWidth || 0);
                    const height = tag === "video"
                        ? (el.videoHeight || el.clientHeight || 0)
                        : (el.naturalHeight || el.clientHeight || 0);
                    nodes.push({ type, url, width, height });
                });
                return nodes;
            }
        """)

        for candidate in candidates or []:
            media_type = candidate.get("type")
            media_url = candidate.get("url")
            width = int(candidate.get("width") or 0)
            height = int(candidate.get("height") or 0)

            if media_type not in {"photo", "video"}:
                continue
            if not isinstance(media_url, str) or not media_url.startswith("http"):
                continue
            if media_url.startswith("data:") or media_url in seen_urls:
                continue
            if media_type == "photo" and width and height and width < 200 and height < 200:
                continue

            media_items.append({
                "type": media_type,
                "url": media_url,
                "source_url": page.url,
            })
            seen_urls.add(media_url)

    for _ in range(16):
        await _collect_visible_media()
        if media_items:
            break
        await asyncio.sleep(0.5)

    for _ in range(15):
        next_btn = await page.query_selector("button[aria-label='Next']")
        if not next_btn:
            break

        aria_disabled = await next_btn.get_attribute("aria-disabled")
        disabled = await next_btn.get_attribute("disabled")
        if aria_disabled == "true" or disabled is not None:
            break

        before = len(media_items)
        await human_click(page, "button[aria-label='Next']", cfg.stealth)
        await short_delay(cfg.stealth)
        await _collect_visible_media()

        if len(media_items) == before:
            break

    return media_items


def _mark_post_as_repost_surface(post_data: dict, target: str) -> dict:
    """Normalise metadata for posts discovered via the profile repost surface."""
    post_data["content_scope"] = "reposts"
    post_data["is_repost"] = True

    owner = post_data.get("owner")
    owner_username = owner.get("username") if isinstance(owner, dict) else None
    if owner_username and owner_username != target and not post_data.get("repost_source_account"):
        post_data["repost_source_account"] = owner_username
    if not post_data.get("repost_source_url") and post_data.get("url"):
        post_data["repost_source_url"] = post_data["url"]
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
    log_event("instagram", "scrape_start", kind="reposts", target=f"@{target}")
    reposts_url = f"{BASE_URL}/{target}/reposts/"
    await page.goto(reposts_url, wait_until="domcontentloaded", timeout=30_000)
    await random_delay(cfg.stealth)
    if await detect_logout(page, PLATFORM):
        log_event("instagram", "scrape_unavailable", level="warning", kind="reposts", target=f"@{target}", reason="session_logged_out")
        return []
    await assert_no_captcha(page, PLATFORM)
    await _refresh_profile_overview(page, target, conn)

    repost_scrolls, repost_empty_scrolls = _repost_scroll_limits(cfg)
    post_links = await _collect_profile_post_links(
        page,
        cfg,
        wait_for_grid=True,
        max_scrolls=repost_scrolls,
        max_empty_scrolls=repost_empty_scrolls,
    )
    if not post_links:
        fallback_links = await _collect_visible_post_links(page)
        if fallback_links:
            post_links = fallback_links
            log_event(
                "instagram",
                "scrape_recovery",
                level="warning",
                kind="reposts",
                target=f"@{target}",
                links=len(post_links),
                source="dom_fallback",
            )

    if not post_links:
        retry_page = await page.context.new_page()
        try:
            await retry_page.goto(reposts_url, wait_until="domcontentloaded", timeout=30_000)
            await random_delay(cfg.stealth)
            await retry_page.wait_for_timeout(5_000)
            if await detect_logout(retry_page, PLATFORM):
                log_event("instagram", "scrape_unavailable", level="warning", kind="reposts", target=f"@{target}", reason="session_logged_out")
                return []
            await assert_no_captcha(retry_page, PLATFORM)
            post_links = await _collect_profile_post_links(
                retry_page,
                cfg,
                wait_for_grid=True,
                max_scrolls=repost_scrolls,
                max_empty_scrolls=repost_empty_scrolls,
            )
            if not post_links:
                fallback_links = await _collect_visible_post_links(retry_page)
                if fallback_links:
                    post_links = fallback_links
                    log_event(
                        "instagram",
                        "scrape_recovery",
                        level="warning",
                        kind="reposts",
                        target=f"@{target}",
                        links=len(post_links),
                        source="fresh_page_fallback",
                    )
        finally:
            try:
                await retry_page.close()
            except Exception:
                pass

    if skip_known_existing:
        from src.db import get_existing_repost_ids_for_target, get_target_id, upsert_target

        try:
            target_id = get_target_id(conn, PLATFORM, target)
        except ValueError:
            target_id = upsert_target(conn, PLATFORM, target)

        existing_post_ids = get_existing_repost_ids_for_target(conn, target_id)
        if existing_post_ids:
            original_count = len(post_links)
            post_links = [
                link for link in post_links
                if (_post_id_from_url(link) or "") not in existing_post_ids
            ]
            skipped_count = original_count - len(post_links)
            if skipped_count:
                log_event(
                    "instagram",
                    "scrape_skip_existing",
                    kind="reposts",
                    target=f"@{target}",
                    skipped=skipped_count,
                )

    reposts: list[dict] = []
    for post_url in post_links:
        post_data = await _extract_post_metadata(page, post_url, target, cfg)
        if not post_data:
            continue
        reposts.append(_mark_post_as_repost_surface(post_data, target))

    log_event("instagram", "scrape_result", kind="reposts", target=f"@{target}", items=len(reposts))
    return reposts


async def scrape_mentions(
    page: Page,
    target: str,
    conn,
    cfg: "Config",
    *,
    skip_known_existing: bool = False,
) -> list[dict]:
    """
    Scrape tagged posts for the target account.
    These are stored as regular posts with content_scope='mentions'.
    """
    log_event("instagram", "scrape_start", kind="mentions", target=f"@{target}")
    tagged_url = f"{BASE_URL}/{target}/tagged/"
    await page.goto(tagged_url, wait_until="domcontentloaded", timeout=30_000)
    await random_delay(cfg.stealth)
    if await detect_logout(page, PLATFORM):
        log_event("instagram", "scrape_unavailable", level="warning", kind="mentions", target=f"@{target}", reason="session_logged_out")
        return []
    await assert_no_captcha(page, PLATFORM)

    post_links = await _collect_profile_post_links(page, cfg, wait_for_grid=False)
    if not post_links:
        fallback_links = await _collect_visible_post_links(page)
        if fallback_links:
            post_links = fallback_links
            log_event(
                "instagram",
                "scrape_recovery",
                level="warning",
                kind="mentions",
                target=f"@{target}",
                links=len(post_links),
                source="dom_fallback",
            )

    if not post_links:
        log_event(
            "instagram",
            "scrape_retry",
            level="warning",
            kind="mentions",
            target=f"@{target}",
            reason="fresh_page_retry",
        )
        retry_page = await page.context.new_page()
        try:
            await retry_page.goto(tagged_url, wait_until="domcontentloaded", timeout=30_000)
            await random_delay(cfg.stealth)
            await retry_page.wait_for_timeout(5_000)
            if await detect_logout(retry_page, PLATFORM):
                log_event("instagram", "scrape_unavailable", level="warning", kind="mentions", target=f"@{target}", reason="session_logged_out")
                return []
            await assert_no_captcha(retry_page, PLATFORM)
            post_links = await _collect_profile_post_links(retry_page, cfg, wait_for_grid=False)
            if not post_links:
                fallback_links = await _collect_visible_post_links(retry_page)
                if fallback_links:
                    post_links = fallback_links
                    log_event(
                        "instagram",
                        "scrape_recovery",
                        level="warning",
                        kind="mentions",
                        target=f"@{target}",
                        links=len(post_links),
                        source="fresh_page_fallback",
                    )
            if not post_links:
                debug_path = await _write_profile_debug_capture(retry_page, target, "tagged_empty")
                log_event(
                    "instagram",
                    "scrape_partial",
                    level="warning",
                    kind="mentions",
                    target=f"@{target}",
                    captured=0,
                    debug_path=debug_path,
                )
        finally:
            try:
                await retry_page.close()
            except Exception:
                pass

    if skip_known_existing:
        from src.db import get_existing_post_ids_for_target, get_target_id, upsert_target

        try:
            target_id = get_target_id(conn, PLATFORM, target)
        except ValueError:
            target_id = upsert_target(conn, PLATFORM, target)

        existing_post_ids = get_existing_post_ids_for_target(
            conn,
            target_id,
            content_scope="mentions",
            require_media=True,
        )
        if existing_post_ids:
            original_count = len(post_links)
            post_links = [
                link for link in post_links
                if (_post_id_from_url(link) or "") not in existing_post_ids
            ]
            skipped_count = original_count - len(post_links)
            if skipped_count:
                log_event(
                    "instagram",
                    "scrape_skip_existing",
                    kind="mentions",
                    target=f"@{target}",
                    skipped=skipped_count,
                )

    mentions: list[dict] = []
    for post_url in post_links:
        post_data = await _extract_post_metadata(page, post_url, target, cfg)
        if not post_data:
            continue
        post_data["content_scope"] = "mentions"
        mentions.append(post_data)

    log_event("instagram", "scrape_result", kind="mentions", target=f"@{target}", items=len(mentions))
    return mentions


async def scrape_highlights(
    page: Page,
    target: str,
    conn,
    cfg: "Config",
    *,
    skip_known_existing: bool = False,
) -> list[dict]:
    """
    Scrape highlight reels for a target profile.
    Highlight items are stored in the stories table with content_scope='highlight'.
    """
    log_event("instagram", "scrape_start", kind="highlights", target=f"@{target}")
    profile_url = f"{BASE_URL}/{target}/"
    await page.goto(profile_url, wait_until="domcontentloaded", timeout=30_000)
    await random_delay(cfg.stealth)
    await assert_no_captcha(page, PLATFORM)
    await _refresh_profile_overview(page, target, conn)
    from src.db import get_existing_story_ids_for_target, get_target_id, upsert_target

    try:
        target_id = get_target_id(conn, PLATFORM, target)
    except ValueError:
        target_id = upsert_target(conn, PLATFORM, target)

    highlight_links = await _collect_highlight_links(page)
    if not highlight_links:
        log_event(
            "instagram",
            "scrape_retry",
            level="warning",
            kind="highlights",
            target=f"@{target}",
            reason="fresh_page_retry",
        )
        retry_page = await page.context.new_page()
        try:
            await retry_page.goto(profile_url, wait_until="domcontentloaded", timeout=30_000)
            await random_delay(cfg.stealth)
            await retry_page.wait_for_timeout(5_000)
            await assert_no_captcha(retry_page, PLATFORM)
            await _refresh_profile_overview(retry_page, target, conn)
            highlight_links = await _collect_highlight_links(retry_page)
            if not highlight_links:
                debug_path = await _write_profile_debug_capture(retry_page, target, "highlights_empty")
                log_event(
                    "instagram",
                    "scrape_partial",
                    level="warning",
                    kind="highlights",
                    target=f"@{target}",
                    captured=0,
                    debug_path=debug_path,
                )
        finally:
            try:
                await retry_page.close()
            except Exception:
                pass

    if not highlight_links:
        log_event("instagram", "scrape_result", kind="highlights", target=f"@{target}", items=0)
        return []

    stories: list[dict] = []
    seen_story_ids: set[str] = set()

    for highlight in highlight_links:
        highlight_url = highlight["url"]
        highlight_title = highlight["title"]
        highlight_id = _extract_highlight_id(highlight_url) or hashlib.sha1(
            highlight_url.encode("utf-8")
        ).hexdigest()[:12]

        try:
            await page.goto(highlight_url, wait_until="domcontentloaded", timeout=30_000)
            await short_delay(cfg.stealth)
        except Exception as e:
            logger.warning(f"[instagram] Could not open highlight {highlight_title!r} for @{target}: {e}")
            continue

        reel_node = await _extract_reel_node(
            page,
            username=target,
            collection_name=highlight_title,
        )
        if reel_node:
            collection = (reel_node.get("title") or highlight_title or "").strip() or highlight_title
            payload_stories = _stories_from_reel_node(
                reel_node,
                target=target,
                content_scope="highlight",
                collection_name=collection,
                highlight_id=highlight_id,
                limit=max(cfg.archive.max_highlight_items_per_collection, 1),
            )
            for story_data in payload_stories:
                story_id = story_data["story_id"]
                if story_id in seen_story_ids:
                    continue
                seen_story_ids.add(story_id)
                stories.append(story_data)
            continue

        item_count = 0
        duplicate_streak = 0
        while item_count < max(cfg.archive.max_highlight_items_per_collection, 1):
            if await detect_captcha_on_page(page):
                raise CaptchaError(f"[instagram] CAPTCHA during highlight scrape for {target}")

            story_data = await _extract_highlight_story_metadata(
                page, highlight_id, highlight_title, item_count
            )
            if story_data:
                story_id = story_data["story_id"]
                if story_id in seen_story_ids:
                    duplicate_streak += 1
                else:
                    seen_story_ids.add(story_id)
                    stories.append(story_data)
                    duplicate_streak = 0
            else:
                duplicate_streak += 1

            if duplicate_streak >= 2:
                break

            next_btn = await page.query_selector("button[aria-label='Next']")
            if not next_btn:
                break

            await human_click(page, "button[aria-label='Next']", cfg.stealth)
            await short_delay(cfg.stealth)

            current_highlight_id = _extract_highlight_id(page.url) or highlight_id
            if current_highlight_id != highlight_id:
                break

            item_count += 1

    if skip_known_existing and stories:
        existing_story_ids = get_existing_story_ids_for_target(
            conn,
            target_id,
            content_scope="highlight",
            require_media=True,
        )
        if existing_story_ids:
            stories, skipped_count = _filter_existing_story_rows(
                stories,
                existing_story_ids,
            )
            if skipped_count:
                log_event(
                    "instagram",
                    "scrape_skip_existing",
                    kind="highlights",
                    target=f"@{target}",
                    skipped=skipped_count,
                )

    log_event("instagram", "scrape_result", kind="highlights", target=f"@{target}", items=len(stories))
    return stories


async def _collect_highlight_links(page: Page) -> list[dict[str, str]]:
    """Collect highlight bubble links from the current Instagram profile page."""
    candidates = await page.evaluate("""
        () => {
            return Array.from(document.querySelectorAll("a[href*='/stories/highlights/']"))
                .map((node) => {
                    const rawText = [
                        node.getAttribute("title"),
                        node.getAttribute("aria-label"),
                        node.textContent,
                    ].filter(Boolean).join(" ").trim();
                    return {
                        url: node.href,
                        title: rawText.replace(/\\s+/g, " ").trim() || "highlight",
                    };
                });
        }
    """)

    results: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for candidate in candidates or []:
        url = candidate.get("url")
        if not isinstance(url, str) or not url.startswith("http") or url in seen_urls:
            continue
        seen_urls.add(url)
        results.append({
            "url": url,
            "title": (candidate.get("title") or "highlight").strip(),
        })
    return results


def _extract_highlight_id(url: str) -> str | None:
    match = re.search(r"/stories/highlights/(\d+)", url)
    return match.group(1) if match else None


async def _extract_reel_node(
    page: Page,
    *,
    username: str | None = None,
    collection_name: str | None = None,
) -> dict | None:
    """Return the best-matching embedded Instagram reel node for a story/highlight page."""
    payloads = await _embedded_json_payloads(page)
    candidates: list[tuple[int, dict]] = []

    def _append_candidate(node: dict) -> None:
        if not isinstance(node, dict):
            return
        score = 0
        reel_user = node.get("user") or {}
        if username and isinstance(reel_user, dict) and reel_user.get("username") == username:
            score += 2
        title = (node.get("title") or node.get("name") or "").strip()
        if collection_name and title == collection_name:
            score += 1
        if node.get("items"):
            score += 1
        candidates.append((score, node))

    for payload in payloads:
        for connection in _find_nested_values(payload, "xdt_api__v1__feed__reels_media__connection"):
            if not isinstance(connection, dict):
                continue
            for edge in connection.get("edges") or []:
                node = edge.get("node") if isinstance(edge, dict) else None
                _append_candidate(node)

        for reels_media in _find_nested_values(payload, "reels_media"):
            if not isinstance(reels_media, list):
                continue
            for node in reels_media:
                _append_candidate(node)

    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _story_caption_text(caption) -> str | None:
    """Return a plain-text story caption when Instagram includes one in the payload."""
    if isinstance(caption, str) and caption.strip():
        return caption.strip()
    if isinstance(caption, dict):
        text = caption.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return None


def _normalise_story_media_candidate(
    *,
    kind: str,
    url: str | None,
    source: str,
    width: int | None = None,
    height: int | None = None,
    bitrate: int | None = None,
) -> dict | None:
    """Return a normalized story media candidate when the URL is usable."""
    if kind not in {"image", "video"}:
        return None
    if not isinstance(url, str) or not url.startswith("http"):
        return None
    candidate = {
        "kind": kind,
        "url": url,
        "source": source,
    }
    if isinstance(width, int) and width > 0:
        candidate["width"] = width
    if isinstance(height, int) and height > 0:
        candidate["height"] = height
    if isinstance(bitrate, int) and bitrate > 0:
        candidate["bitrate"] = bitrate
    return candidate


def _rank_story_media_candidate(candidate: dict) -> tuple[int, int, int]:
    """Return a ranking key that prefers larger, higher-bitrate assets."""
    width = int(candidate.get("width") or 0)
    height = int(candidate.get("height") or 0)
    bitrate = int(candidate.get("bitrate") or 0)
    return (width * height, bitrate, max(width, height))


def _story_video_candidates(item: dict) -> list[dict]:
    """Return ranked video candidates extracted from nested story payload data."""
    candidates: list[dict] = []
    seen_urls: set[str] = set()

    for versions in _find_nested_values(item, "video_versions"):
        if not isinstance(versions, list):
            continue
        for entry in versions:
            if not isinstance(entry, dict):
                continue
            candidate = _normalise_story_media_candidate(
                kind="video",
                url=entry.get("url"),
                source="embedded_video_versions",
                width=entry.get("width") if isinstance(entry.get("width"), int) else None,
                height=entry.get("height") if isinstance(entry.get("height"), int) else None,
                bitrate=entry.get("bit_rate") if isinstance(entry.get("bit_rate"), int) else None,
            )
            if not candidate or candidate["url"] in seen_urls:
                continue
            seen_urls.add(candidate["url"])
            candidates.append(candidate)

    candidates.sort(key=_rank_story_media_candidate, reverse=True)
    return candidates


def _story_image_candidates(item: dict) -> list[dict]:
    """Return ranked still-image candidates extracted from nested story payload data."""
    candidates: list[dict] = []
    seen_urls: set[str] = set()

    for image_versions in _find_nested_values(item, "image_versions2"):
        if not isinstance(image_versions, dict):
            continue
        for entry in image_versions.get("candidates") or []:
            if not isinstance(entry, dict):
                continue
            candidate = _normalise_story_media_candidate(
                kind="image",
                url=entry.get("url"),
                source="embedded_image_versions",
                width=entry.get("width") if isinstance(entry.get("width"), int) else None,
                height=entry.get("height") if isinstance(entry.get("height"), int) else None,
            )
            if not candidate or candidate["url"] in seen_urls:
                continue
            seen_urls.add(candidate["url"])
            candidates.append(candidate)

    candidates.sort(key=_rank_story_media_candidate, reverse=True)
    return candidates


def _best_story_image_url(item: dict) -> str | None:
    """Choose the highest-resolution still image URL from an Instagram story payload."""
    candidates = _story_image_candidates(item)
    return candidates[0]["url"] if candidates else None


def _best_story_video_url(item: dict) -> str | None:
    """Choose the best downloadable video URL from an Instagram story payload."""
    candidates = _story_video_candidates(item)
    return candidates[0]["url"] if candidates else None


def _dedupe_strings(values) -> list[str]:
    """Return distinct non-empty strings while preserving order."""
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        cleaned = value.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def _extract_story_music(item: dict) -> list[dict]:
    """Return simplified music sticker metadata from a story payload."""
    tracks: list[dict] = []
    for sticker in item.get("story_music_stickers") or []:
        if not isinstance(sticker, dict):
            continue
        asset = sticker.get("music_asset_info") or {}
        if not isinstance(asset, dict):
            continue
        title = asset.get("title")
        artist = asset.get("display_artist")
        if title or artist:
            tracks.append({"title": title, "artist": artist})
    return tracks


def _extract_story_link_urls(item: dict) -> list[str]:
    """Return outbound URLs referenced by link stickers or CTAs."""
    urls: list[str] = []
    for key in ("story_link_stickers", "story_cta", "story_feed_media"):
        for value in _find_nested_values(item.get(key) or [], "url"):
            if isinstance(value, str) and value.startswith("http"):
                urls.append(value)
    return _dedupe_strings(urls)


def _extract_story_locations(item: dict) -> list[dict]:
    """Return simplified location stickers from a story payload."""
    locations: list[dict] = []
    for sticker in item.get("story_locations") or []:
        if not isinstance(sticker, dict):
            continue
        entry = {
            "id": None,
            "name": None,
        }
        for value in _find_nested_values(sticker, "id"):
            if isinstance(value, (str, int)):
                entry["id"] = str(value)
                break
        for value in _find_nested_values(sticker, "name"):
            if isinstance(value, str) and value.strip():
                entry["name"] = value.strip()
                break
        if entry["id"] or entry["name"]:
            locations.append(entry)
    return locations


def _extract_story_hashtags(item: dict) -> list[str]:
    """Return hashtag names from story sticker metadata."""
    values = []
    for sticker in item.get("story_hashtags") or []:
        values.extend(_find_nested_values(sticker, "name"))
        values.extend(_find_nested_values(sticker, "hashtag"))
    hashtags = []
    for value in _dedupe_strings(values):
        hashtags.append(value if value.startswith("#") else f"#{value}")
    return hashtags


def _extract_story_mentions(item: dict) -> list[str]:
    """Return mentioned usernames from tappables/stickers when present."""
    values = []
    for key in ("story_bloks_tappables", "story_bloks_stickers", "story_mentions"):
        values.extend(_find_nested_values(item.get(key) or [], "username"))
    mentions = []
    for value in _dedupe_strings(values):
        mentions.append(value if value.startswith("@") else f"@{value}")
    return mentions


def _extract_story_prompt_counts(item: dict, key: str) -> list[dict]:
    """Return lightweight copies of countdown/question/slider story widgets."""
    result: list[dict] = []
    for entry in item.get(key) or []:
        if not isinstance(entry, dict):
            continue
        simplified = {
            "id": None,
            "title": None,
            "text": None,
        }
        for value in _find_nested_values(entry, "id"):
            if isinstance(value, (str, int)):
                simplified["id"] = str(value)
                break
        for field in ("title", "text", "prompt"):
            for value in _find_nested_values(entry, field):
                if isinstance(value, str) and value.strip():
                    simplified["text" if field != "title" else "title"] = value.strip()
                    break
            if simplified["title"] or simplified["text"]:
                break
        if simplified["id"] or simplified["title"] or simplified["text"]:
            result.append(simplified)
    return result


def _story_metadata_from_payload_item(item: dict, reel_user: dict | None) -> dict:
    """Build a concise story metadata blob from Instagram's embedded reel payload."""
    reel_user = reel_user or {}
    caption_text = _story_caption_text(item.get("caption"))
    video_candidates = _story_video_candidates(item)
    image_candidates = _story_image_candidates(item)
    music = _extract_story_music(item)
    metadata = {
        "story_pk": str(item.get("pk") or ""),
        "story_code": item.get("code"),
        "caption": caption_text,
        "accessibility_caption": item.get("accessibility_caption"),
        "owner": {
            "id": str(reel_user.get("id") or reel_user.get("pk") or ""),
            "username": reel_user.get("username"),
            "profile_pic_url": reel_user.get("profile_pic_url"),
            "is_verified": bool(reel_user.get("is_verified")),
        },
        "music": music,
        "link_urls": _extract_story_link_urls(item),
        "locations": _extract_story_locations(item),
        "hashtags": _extract_story_hashtags(item),
        "mentions": _extract_story_mentions(item),
        "countdowns": _extract_story_prompt_counts(item, "story_countdowns"),
        "questions": _extract_story_prompt_counts(item, "story_questions"),
        "sliders": _extract_story_prompt_counts(item, "story_sliders"),
        "can_reply": item.get("can_reply"),
        "can_reshare": item.get("can_reshare"),
        "viewer_count": item.get("viewer_count"),
        "duration_seconds": item.get("video_duration"),
        "app_attribution": item.get("story_app_attribution"),
        "audio_expected": bool(music),
        "media_candidates": [*video_candidates, *image_candidates],
        "comments": [],
        "comments_supported": False,
        "comments_unavailable_reason": (
            "Instagram web does not expose public comments for stories/highlights."
        ),
    }
    return metadata


def _story_from_payload_item(
    item: dict,
    *,
    target: str,
    content_scope: str,
    collection_name: str | None,
    reel_user: dict | None,
    highlight_id: str | None = None,
) -> dict | None:
    """Convert one embedded Instagram reel item into the app's story row format."""
    story_pk = str(item.get("pk") or "")
    if not story_pk:
        return None

    metadata = _story_metadata_from_payload_item(item, reel_user)
    media_candidates = metadata.get("media_candidates") or []
    video_url = next(
        (
            candidate.get("url")
            for candidate in media_candidates
            if isinstance(candidate, dict) and candidate.get("kind") == "video"
        ),
        None,
    )
    image_url = next(
        (
            candidate.get("url")
            for candidate in media_candidates
            if isinstance(candidate, dict) and candidate.get("kind") == "image"
        ),
        None,
    )
    story_type = "video" if video_url else "image" if image_url else "text"
    permalink = f"{BASE_URL}/stories/{target}/{story_pk}/"

    if content_scope == "highlight":
        if not highlight_id:
            return None
        story_id = f"highlight_{highlight_id}_{story_pk}"
    else:
        story_id = story_pk

    metadata["story_permalink"] = permalink
    metadata["initial_story_type"] = story_type
    metadata["capture_source"] = "embedded_reel_payload"

    return {
        "platform": PLATFORM,
        "story_id": story_id,
        "story_type": story_type,
        "content_scope": content_scope,
        "collection_name": collection_name,
        "url": video_url or image_url or permalink,
        "posted_at": _timestamp_to_iso(item.get("taken_at")),
        "expires_at": _timestamp_to_iso(item.get("expiring_at")),
        "story_permalink": permalink,
        "metadata": metadata,
    }


def _stories_from_reel_node(
    reel_node: dict,
    *,
    target: str,
    content_scope: str,
    collection_name: str | None,
    highlight_id: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Convert an embedded Instagram reel node into scraper story rows."""
    stories: list[dict] = []
    reel_user = reel_node.get("user") if isinstance(reel_node.get("user"), dict) else {}

    items = reel_node.get("items") or []
    if limit is not None:
        items = items[:limit]

    for item in items:
        if not isinstance(item, dict):
            continue
        story = _story_from_payload_item(
            item,
            target=target,
            content_scope=content_scope,
            collection_name=collection_name,
            reel_user=reel_user,
            highlight_id=highlight_id,
        )
        if story:
            stories.append(story)
    return stories


async def _extract_story_datetime(page: Page) -> str | None:
    """Extract the visible story/highlight timestamp from the viewer when present."""
    try:
        datetime_value = await page.eval_on_selector(
            "time[datetime]",
            "el => el.getAttribute('datetime')",
        )
    except Exception:
        return None

    if not isinstance(datetime_value, str) or not datetime_value.strip():
        return None
    try:
        return datetime.fromisoformat(datetime_value.replace("Z", "+00:00")).astimezone(
            timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


async def _extract_story_viewer_media_candidates(page: Page) -> list[dict]:
    """Return visible story viewer media candidates from the current DOM."""
    raw_candidates = await page.evaluate("""
        () => {
            const results = [];
            const seen = new Set();
            const push = (kind, url, source, width = null, height = null) => {
                if (typeof url !== "string" || !url.startsWith("http")) {
                    return;
                }
                const key = `${kind}|${url}`;
                if (seen.has(key)) {
                    return;
                }
                seen.add(key);
                results.push({ kind, url, source, width, height });
            };

            const video = document.querySelector("video");
            if (video) {
                push(
                    "video",
                    video.currentSrc || video.src || null,
                    "viewer_dom_video",
                    video.videoWidth || null,
                    video.videoHeight || null
                );
                for (const source of video.querySelectorAll("source")) {
                    push("video", source.src || null, "viewer_dom_source");
                }
                push("image", video.poster || null, "viewer_dom_poster");
            }

            const image = document.querySelector("img[decoding='auto']");
            if (image) {
                push(
                    "image",
                    image.currentSrc || image.src || null,
                    "viewer_dom_image",
                    image.naturalWidth || null,
                    image.naturalHeight || null
                );
            }
            return results;
        }
    """)

    candidates: list[dict] = []
    for entry in raw_candidates or []:
        if not isinstance(entry, dict):
            continue
        candidate = _normalise_story_media_candidate(
            kind=entry.get("kind"),
            url=entry.get("url"),
            source=entry.get("source") or "viewer_dom",
            width=entry.get("width") if isinstance(entry.get("width"), int) else None,
            height=entry.get("height") if isinstance(entry.get("height"), int) else None,
        )
        if candidate:
            candidates.append(candidate)

    video_candidates = [candidate for candidate in candidates if candidate["kind"] == "video"]
    image_candidates = [candidate for candidate in candidates if candidate["kind"] == "image"]
    video_candidates.sort(key=_rank_story_media_candidate, reverse=True)
    image_candidates.sort(key=_rank_story_media_candidate, reverse=True)
    return [*video_candidates, *image_candidates]


async def _extract_highlight_story_metadata(
    page: Page,
    highlight_id: str,
    highlight_title: str,
    item_index: int,
) -> dict | None:
    """Build a stable story row from the currently visible highlight item."""
    try:
        media_candidates = await _extract_story_viewer_media_candidates(page)
        story_type = next(
            (
                candidate["kind"]
                for candidate in media_candidates
                if candidate["kind"] in {"video", "image"}
            ),
            "image",
        )
        media_url = next(
            (
                candidate["url"]
                for candidate in media_candidates
                if candidate["kind"] == story_type
            ),
            None,
        )

        posted_at = await _extract_story_datetime(page)
        seed = media_url or f"{page.url}|{highlight_id}|{item_index}"
        story_hash = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
        story_id = f"highlight_{highlight_id}_{story_hash}"

        return {
            "platform": PLATFORM,
            "story_id": story_id,
            "story_type": story_type,
            "content_scope": "highlight",
            "collection_name": highlight_title,
            "url": media_url or page.url,
            "posted_at": posted_at,
            "story_permalink": page.url,
            "metadata": {
                "accessibility_caption": None,
                "audio_expected": False,
                "capture_source": "viewer_dom",
                "comments": [],
                "comments_supported": False,
                "comments_unavailable_reason": (
                    "Instagram web does not expose public comments for stories/highlights."
                ),
                "initial_story_type": story_type,
                "media_candidates": media_candidates,
                "story_permalink": page.url,
            },
        }
    except Exception as e:
        logger.warning(f"[instagram] Could not extract highlight story metadata: {e}")
        return None


# ─── Story scraping ──────────────────────────────────────────────────────────

async def get_story_tray_usernames(page: Page, cfg: "Config") -> set[str]:
    """
    Navigate to the Instagram home feed and return the set of usernames that
    have active stories visible in the story tray at the top of the feed.

    This is used as a pre-screen before visiting individual target profiles:
    targets not appearing in the tray almost certainly have no active stories,
    so we can skip the per-profile page load for them.

    Returns an empty set (not None) on success even if no stories are visible.
    Raises on hard errors (captcha, logout) so the caller can decide to fall back.
    """
    await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30_000)
    await random_delay(cfg.stealth)
    await assert_no_captcha(page, PLATFORM)

    payloads = await _embedded_json_payloads(page)
    usernames: set[str] = set()

    found_connection_key = False
    found_reels_media_key = False

    for payload in payloads:
        # Primary source: GraphQL connection used by the home feed story tray
        connections = _find_nested_values(payload, "xdt_api__v1__feed__reels_media__connection")
        if connections:
            found_connection_key = True
        for connection in connections:
            if not isinstance(connection, dict):
                continue
            for edge in (connection.get("edges") or []):
                node = edge.get("node") if isinstance(edge, dict) else None
                if not isinstance(node, dict):
                    continue
                user = node.get("user") or {}
                username = user.get("username") if isinstance(user, dict) else None
                if username:
                    usernames.add(username.lower())

        # Fallback: older reels_media list format
        reels_list = _find_nested_values(payload, "reels_media")
        if reels_list:
            found_reels_media_key = True
        for reels_media in reels_list:
            if not isinstance(reels_media, list):
                continue
            for node in reels_media:
                if not isinstance(node, dict):
                    continue
                user = node.get("user") or {}
                username = user.get("username") if isinstance(user, dict) else None
                if username:
                    usernames.add(username.lower())

    log_event(
        "instagram", "story_tray_check",
        usernames_with_stories=len(usernames),
        payloads_found=len(payloads),
        found_connection_key=found_connection_key,
        found_reels_media_key=found_reels_media_key,
    )
    return usernames


async def scrape_stories(
    page: Page,
    target: str,
    conn,
    cfg: "Config",
    *,
    skip_known_existing: bool = False,
) -> list[dict]:
    """
    Visit target's profile story ring and capture story metadata + screenshots.
    Returns a list of new story dicts.
    """
    log_event("instagram", "scrape_start", kind="stories", target=f"@{target}")
    profile_url = f"{BASE_URL}/{target}/"
    await page.goto(profile_url, wait_until="domcontentloaded", timeout=30_000)
    await random_delay(cfg.stealth)
    await assert_no_captcha(page, PLATFORM)
    await _refresh_profile_overview(page, target, conn)
    from src.db import get_existing_story_ids_for_target, get_target_id, upsert_target

    try:
        target_id = get_target_id(conn, PLATFORM, target)
    except ValueError:
        target_id = upsert_target(conn, PLATFORM, target)

    direct_story_url = f"{BASE_URL}/stories/{target}/"
    try:
        await page.goto(direct_story_url, wait_until="domcontentloaded", timeout=30_000)
        await short_delay(cfg.stealth)
        if not await detect_logout(page, PLATFORM):
            reel_node = await _extract_reel_node(page, username=target)
            if reel_node:
                stories = _stories_from_reel_node(
                    reel_node,
                    target=target,
                    content_scope="story",
                    collection_name=None,
                )
                if skip_known_existing and stories:
                    existing_story_ids = get_existing_story_ids_for_target(
                        conn,
                        target_id,
                        content_scope="story",
                        require_media=True,
                    )
                    if existing_story_ids:
                        stories, skipped_count = _filter_existing_story_rows(
                            stories,
                            existing_story_ids,
                        )
                        if skipped_count:
                            log_event(
                                "instagram",
                                "scrape_skip_existing",
                                kind="stories",
                                target=f"@{target}",
                                skipped=skipped_count,
                            )
                log_event("instagram", "scrape_result", kind="stories", target=f"@{target}", items=len(stories))
                return stories
    except Exception as e:
        logger.debug(f"[instagram] Direct story viewer open failed for @{target}: {e}")

    await page.goto(profile_url, wait_until="domcontentloaded", timeout=30_000)
    await short_delay(cfg.stealth)

    # Check if the story ring is present (canvas element around the profile pic)
    story_ring = await page.query_selector(
        "canvas[style*='border'], div[role='button'][tabindex='0'] canvas"
    )
    if not story_ring:
        logger.debug(f"[instagram] No stories available for @{target}")
        return []

    stories = []
    try:
        story_open_selector = await _wait_for_any_selector(
            page,
            [
                "header section div[role='button']:first-child",
                "header canvas",
                "div[role='button'][tabindex='0'] canvas",
            ],
            timeout_ms=7_500,
        )
        if not story_open_selector:
            logger.debug(f"[instagram] Story ring could not be opened for @{target}")
            return []

        # Click on the story ring to open stories
        await human_click(page, story_open_selector, cfg.stealth)
        await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        await short_delay(cfg.stealth)

        reel_node = await _extract_reel_node(page, username=target)
        if reel_node:
            stories.extend(
                _stories_from_reel_node(
                    reel_node,
                    target=target,
                    content_scope="story",
                    collection_name=None,
                )
            )
            if skip_known_existing and stories:
                existing_story_ids = get_existing_story_ids_for_target(
                    conn,
                    target_id,
                    content_scope="story",
                    require_media=True,
                )
                if existing_story_ids:
                    stories, skipped_count = _filter_existing_story_rows(
                        stories,
                        existing_story_ids,
                    )
                    if skipped_count:
                        log_event(
                            "instagram",
                            "scrape_skip_existing",
                            kind="stories",
                            target=f"@{target}",
                            skipped=skipped_count,
                        )
            log_event("instagram", "scrape_result", kind="stories", target=f"@{target}", items=len(stories))
            return stories

        story_count = 0
        while story_count < 50:  # safety cap
            if await detect_captcha_on_page(page):
                raise CaptchaError(f"[instagram] CAPTCHA during story scrape for {target}")

            story_data = await _extract_story_metadata(page, target)
            if story_data:
                stories.append(story_data)

            # Try to go to next story
            next_btn = await page.query_selector("button[aria-label='Next']")
            if not next_btn:
                break
            await human_click(page, "button[aria-label='Next']", cfg.stealth)
            await short_delay(cfg.stealth)
            story_count += 1

    except (CaptchaError, LogoutError):
        raise
    except Exception as e:
        log_event("instagram", "scrape_error", level="warning", kind="stories", target=f"@{target}", error=e)

    if skip_known_existing and stories:
        existing_story_ids = get_existing_story_ids_for_target(
            conn,
            target_id,
            content_scope="story",
            require_media=True,
        )
        if existing_story_ids:
            stories, skipped_count = _filter_existing_story_rows(
                stories,
                existing_story_ids,
            )
            if skipped_count:
                log_event(
                    "instagram",
                    "scrape_skip_existing",
                    kind="stories",
                    target=f"@{target}",
                    skipped=skipped_count,
                )

    log_event("instagram", "scrape_result", kind="stories", target=f"@{target}", items=len(stories))
    return stories


async def _extract_story_metadata(page: Page, target: str) -> dict | None:
    """Extract story_id, type, and URL from the current story viewer page."""
    try:
        url = page.url
        # Story URL format: instagram.com/stories/{username}/{story_id}/
        match = re.search(r"/stories/[^/]+/(\d+)/?", url)
        story_id = match.group(1) if match else url.split("?")[0].rstrip("/").split("/")[-1]

        media_candidates = await _extract_story_viewer_media_candidates(page)
        story_type = next(
            (
                candidate["kind"]
                for candidate in media_candidates
                if candidate["kind"] in {"video", "image"}
            ),
            "image",
        )
        media_url = next(
            (
                candidate["url"]
                for candidate in media_candidates
                if candidate["kind"] == story_type
            ),
            None,
        )

        posted_at = await _extract_story_datetime(page)
        return {
            "platform": PLATFORM,
            "story_id": story_id,
            "story_type": story_type,
            "content_scope": "story",
            "collection_name": None,
            "url": media_url or url,
            "posted_at": posted_at,
            "story_permalink": url,
            "metadata": {
                "accessibility_caption": None,
                "audio_expected": False,
                "capture_source": "viewer_dom",
                "comments": [],
                "comments_supported": False,
                "comments_unavailable_reason": (
                    "Instagram web does not expose public comments for stories/highlights."
                ),
                "initial_story_type": story_type,
                "media_candidates": media_candidates,
                "story_permalink": url,
            },
        }
    except Exception as e:
        logger.warning(f"[instagram] Could not extract story metadata: {e}")
        return None


# ─── Follower / following scraping ──────────────────────────────────────────

async def scrape_target_connections(page: Page, target_username: str, conn,
                                    cfg: "Config") -> dict[str, list[str]]:
    """
    Scrape follower and following lists for a monitored target profile.
    Also refreshes the current profile header snapshot while on the page.
    """
    log_event("instagram", "scrape_start", kind="target_connections", target=f"@{target_username}")
    profile_url = f"{BASE_URL}/{target_username}/"
    await page.goto(profile_url, wait_until="domcontentloaded", timeout=30_000)
    await random_delay(cfg.stealth)
    await assert_no_captcha(page, PLATFORM)
    overview = await _refresh_profile_overview(page, target_username, conn) or {}

    followers = await _scrape_follow_list_in_fresh_page(
        page,
        target_username,
        "followers",
        cfg,
        expected_count=_coerce_int(overview.get("follower_count")),
    )
    following = await _scrape_follow_list_in_fresh_page(
        page,
        target_username,
        "following",
        cfg,
        expected_count=_coerce_int(overview.get("following_count")),
    )
    return {
        "followers": followers,
        "following": following,
    }

async def scrape_followers(page: Page, own_username: str, conn,
                            cfg: "Config") -> dict:
    """
    Scrape own follower and following lists, diff against DB snapshots,
    and return a list of follower_event dicts.

    Saves snapshots to DB and returns the latest lists plus computed delta events.
    """
    from src.db import (
        diff_follower_snapshots,
        get_account_id,
        insert_follower_events,
        save_follower_snapshot,
    )

    log_event("instagram", "scrape_start", kind="followers", account=f"@{own_username}")
    account_id = get_account_id(conn, PLATFORM, own_username)
    overview = await _refresh_profile_overview(page, own_username, conn) or {}

    followers = await _scrape_follow_list_in_fresh_page(
        page,
        own_username,
        "followers",
        cfg,
        expected_count=_coerce_int(overview.get("follower_count")),
    )
    following = await _scrape_follow_list_in_fresh_page(
        page,
        own_username,
        "following",
        cfg,
        expected_count=_coerce_int(overview.get("following_count")),
    )

    save_follower_snapshot(conn, account_id, "followers", followers)
    save_follower_snapshot(conn, account_id, "following", following)

    events: list[dict] = []
    events.extend(diff_follower_snapshots(conn, account_id, "followers"))
    events.extend(diff_follower_snapshots(conn, account_id, "following"))

    if events:
        insert_follower_events(conn, account_id, events)
        log_event("instagram", "scrape_events", kind="followers", account=f"@{own_username}", events=len(events))

    return {
        "followers": followers,
        "following": following,
        "events": events,
    }


async def _scrape_follow_list_in_fresh_page(
    page: Page,
    username: str,
    list_type: str,
    cfg: "Config",
    *,
    expected_count: int | None = None,
) -> list[dict]:
    """
    Scrape a follower/following list on a clean tab.

    Instagram's profile page can get into a bad UI state after other scraping
    work on the same page; a fresh tab is much more reliable for opening the
    followers/following overlay.
    """
    combined: dict[str, dict] = {}
    completion_floor = _follow_completion_floor(expected_count)
    max_passes = 2 if completion_floor else 1
    best_count = 0
    trusted_expected_count = expected_count

    for pass_index in range(max_passes):
        follow_page = await page.context.new_page()
        try:
            entries = await _scrape_follow_list(
                follow_page,
                username,
                list_type,
                cfg,
                expected_count=expected_count,
            )
        finally:
            try:
                await follow_page.close()
            except Exception:
                pass

        if entries:
            reported_counts = {
                _coerce_int(entry.get("_reported_count"))
                for entry in entries
                if isinstance(entry, dict) and entry.get("_source") == "graphql"
            }
            reported_counts.discard(None)
            if reported_counts:
                # The accepted GraphQL identity list is the freshest truth even when
                # Instagram's scalar count fields lag behind by one or two accounts.
                trusted_expected_count = len(entries)

        for entry in entries:
            _merge_follow_entry(combined, entry)

        combined_count = len(combined)
        active_completion_floor = _follow_completion_floor(trusted_expected_count)
        if not active_completion_floor or combined_count >= active_completion_floor:
            break
        if combined_count <= best_count:
            break
        best_count = combined_count
        log_event(
            "instagram",
            "scrape_retry",
            kind=list_type,
            target=f"@{username}",
            pass_index=pass_index + 1,
            captured=combined_count,
            expected=trusted_expected_count,
            reason="incomplete_pass_retry",
        )

    _assert_follow_count_complete(username, list_type, len(combined), trusted_expected_count)

    return sorted(
        combined.values(),
        key=lambda item: (
            str(item.get("username") or "").casefold(),
            str(item.get("user_id") or ""),
        ),
    )


async def _scrape_follow_list(page: Page, username: str, list_type: str,
                               cfg: "Config", *,
                               expected_count: int | None = None) -> list[dict]:
    """Open the followers or following view and scroll to collect all usernames."""
    if not await _context_has_instagram_auth_cookie(page):
        raise LogoutError(
            f"[instagram] Cannot scrape {list_type} for @{username} without an authenticated Instagram session. "
            "Run `python3 src/main.py --interactive-login instagram:<account>` and retry."
        )

    profile_url = f"{BASE_URL}/{username}/"
    payload_queue, api_state, detach_api_capture = _install_follow_api_capture(
        page,
        list_type,
    )
    await page.goto(profile_url, wait_until="domcontentloaded", timeout=30_000)
    await random_delay(cfg.stealth)
    await assert_no_captcha(page, PLATFORM)
    await page.wait_for_timeout(3_000)

    graphql_entries = await _scrape_follow_list_via_graphql(
        page,
        username,
        list_type,
        expected_count=expected_count,
    )
    if graphql_entries is not None:
        return graphql_entries

    api_entries = await _scrape_follow_list_via_instagrapi(page, username, list_type)
    if api_entries:
        log_event(
            "instagram",
            "scrape_recovery",
            kind=list_type,
            target=f"@{username}",
            source="instagrapi",
            items=len(api_entries),
        )
        if _follow_count_is_complete(len(api_entries), expected_count):
            return api_entries

    trigger_selectors = [
        f"a[href='/{username}/{list_type}/']",
        f"header a[href='/{username}/{list_type}/']",
        f"a[href$='/{list_type}/']",
        f"header a[href*='/{list_type}/']",
        f"main a[href*='/{list_type}/']",
        f"a:has-text('{list_type.capitalize()}')",
    ]

    dialog_opened = False
    list_page_opened = False
    primary_trigger = page.locator(f"main a[href='/{username}/{list_type}/']").first
    try:
        if await primary_trigger.count():
            await primary_trigger.click(timeout=5_000)
            await page.wait_for_timeout(5_000)
            if await page.locator("div[role='dialog']").count():
                dialog_opened = True
            elif f"/{list_type}/" in page.url:
                await page.wait_for_timeout(5_000)
                if await page.locator("div[role='dialog']").count():
                    dialog_opened = True
                else:
                    list_page_opened = True
    except Exception:
        pass

    for selector in trigger_selectors:
        if dialog_opened:
            break
        try:
            if not await page.query_selector(selector):
                continue
            try:
                await page.click(selector, timeout=5_000)
            except Exception:
                await human_click(page, selector, cfg.stealth)
            await page.wait_for_timeout(5_000)
            if await page.locator("div[role='dialog']").count():
                dialog_opened = True
            elif f"/{list_type}/" in page.url:
                await page.wait_for_timeout(5_000)
                if await page.locator("div[role='dialog']").count():
                    dialog_opened = True
                else:
                    list_page_opened = True
            break
        except Exception:
            continue

    if not dialog_opened and not list_page_opened:
        direct_url = f"{BASE_URL}/{username}/{list_type}/"
        await page.goto(direct_url, wait_until="domcontentloaded", timeout=30_000)
        await short_delay(cfg.stealth)
        if await detect_logout(page, PLATFORM):
            raise LogoutError(
                f"Instagram redirected follower scrape for @{username} {list_type} to login"
            )
        await page.wait_for_timeout(5_000)
        if await page.locator("div[role='dialog']").count():
            dialog_opened = True
        elif f"/{list_type}/" in page.url:
            list_page_opened = True

    await short_delay(cfg.stealth)
    if not dialog_opened and await page.locator("div[role='dialog']").count():
        dialog_opened = True
    if not dialog_opened:
        try:
            await page.goto(profile_url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(3_000)
            fallback_selector = f"a[href='/{username}/{list_type}/']"
            if await page.query_selector(fallback_selector):
                await page.click(fallback_selector, timeout=5_000)
                await page.wait_for_timeout(5_000)
                if await page.locator("div[role='dialog']").count():
                    dialog_opened = True
        except Exception:
            pass

    if dialog_opened:
        try:
            await page.wait_for_selector("div[role='dialog'] a[href^='/']", timeout=15_000)
        except Exception:
            await page.wait_for_timeout(4_000)
    elif list_page_opened:
        try:
            await page.wait_for_selector("main a[href^='/']", timeout=10_000)
        except Exception:
            await page.wait_for_timeout(2_000)

    users: dict[str, dict] = {}
    api_entries = await _scrape_follow_list_via_api(
        page,
        list_type,
        expected_count=expected_count,
    )
    if api_entries:
        for entry in api_entries:
            _merge_follow_entry(users, entry)
        log_event(
            "instagram",
            "scrape_recovery",
            kind=list_type,
            target=f"@{username}",
            source="api_pagination",
            items=len(api_entries),
        )
        if _follow_count_is_complete(len(users), expected_count):
            return sorted(
                users.values(),
                key=lambda item: (
                    str(item.get("username") or "").casefold(),
                    str(item.get("user_id") or ""),
                ),
            )

    scroll_attempts_without_new = 0
    total_scroll_attempts = 0
    completion_floor = _follow_completion_floor(expected_count)
    stable_limit = 20 if completion_floor else 8
    max_attempts = max(200, min(600, (expected_count or 100) * 2))
    root_selector = "div[role='dialog']" if dialog_opened else "main"
    logger.debug(
        f"[instagram] follower scrape state for @{username} {list_type}: "
        f"dialog_opened={dialog_opened}, list_page_opened={list_page_opened}, "
        f"url={page.url}, root_selector={root_selector}"
    )
    try:
        while scroll_attempts_without_new < stable_limit and total_scroll_attempts < max_attempts:
            prev_count = len(users)

            drained_payloads = list(payload_queue)
            payload_queue.clear()
            for payload in drained_payloads:
                for entry in _extract_follow_entries_from_api_payload(payload):
                    _merge_follow_entry(users, entry)

            visible_entries = await page.evaluate("""
                (selector) => {
                    const root = document.querySelector(selector) || document.querySelector("main") || document.body;
                    if (!root) return [];
                    return Array.from(root.querySelectorAll("a[href^='/']")).map((link) => ({
                        href: link.getAttribute("href") || "",
                        row_text: (
                            link.closest("li, article, section, div")?.innerText ||
                            link.innerText ||
                            ""
                        ).trim(),
                    }));
                }
            """, root_selector)
            logger.debug(
                f"[instagram] visible {list_type} entries for @{username}: "
                f"{len(visible_entries or [])} on pass {total_scroll_attempts + 1}"
            )
            for entry in visible_entries or []:
                href = (entry.get("href") or "").strip()
                match = re.match(r"^/([^/?#]+)/?$", href)
                if not match:
                    continue
                user = match.group(1).strip()
                if not user or user == username:
                    continue
                parts = [
                    value.strip()
                    for value in str(entry.get("row_text") or "").splitlines()
                    if value.strip()
                ]
                entry_record = {"username": user, "display_name": None}
                username_index = next(
                    (idx for idx, value in enumerate(parts) if value in {user, f"@{user}"}),
                    -1,
                )
                display_name = None
                if username_index >= 0 and username_index + 1 < len(parts):
                    display_name = parts[username_index + 1]
                elif len(parts) >= 2:
                    display_name = parts[1] if parts[0] == user else parts[0]
                if display_name and display_name != user:
                    entry_record["display_name"] = display_name
                _merge_follow_entry(users, entry_record)

            if completion_floor and len(users) >= completion_floor:
                log_event(
                    "instagram",
                    "scrape_completion",
                    kind=list_type,
                    target=f"@{username}",
                    captured=len(users),
                    expected=expected_count,
                    reason="completion_floor_reached",
                )
                break

            if len(users) == prev_count:
                scroll_attempts_without_new += 1
            else:
                scroll_attempts_without_new = 0

            scroll_state = await page.evaluate("""
                (selector) => {
                    const root = document.querySelector(selector) || document.querySelector("main") || document.body;
                    if (!root) return { scrolled: false, atBottom: true };

                    const candidates = Array.from(root.querySelectorAll("*")).filter((el) => {
                        const style = getComputedStyle(el);
                        return (
                            el.scrollHeight > el.clientHeight + 20 ||
                            ["auto", "scroll"].includes(style.overflowY)
                        );
                    });

                    const best = candidates
                        .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight))[0];

                    if (!best) {
                        return { scrolled: false, atBottom: true };
                    }

                    const previousTop = best.scrollTop;
                    const step = Math.max(Math.floor(best.clientHeight * 0.9), 600);
                    best.scrollTop = Math.min(best.scrollTop + step, best.scrollHeight);
                    return {
                        scrolled: best.scrollTop > previousTop,
                        atBottom: best.scrollTop + best.clientHeight >= best.scrollHeight - 5,
                        clientHeight: best.clientHeight,
                        scrollHeight: best.scrollHeight,
                        scrollTop: best.scrollTop,
                    };
                }
            """, root_selector)
            total_scroll_attempts += 1

            if not scroll_state.get("scrolled") and root_selector == "main":
                await human_scroll(page, "down", cfg.stealth, total_px=800)

            wait_ms = 1_500 if scroll_state.get("scrolled") else 2_500
            if api_state["response_count"] == 0:
                wait_ms = max(wait_ms, 3_500)
            await page.wait_for_timeout(wait_ms)

            if scroll_state.get("atBottom") and len(users) == prev_count:
                scroll_attempts_without_new += 1

            if (
                api_state["response_count"] > 0
                and api_state["has_more"] is False
                and scroll_state.get("atBottom")
                and len(users) == prev_count
            ):
                break
    finally:
        detach_api_capture()

    log_event("instagram", "scrape_result", kind=list_type, target=f"@{username}", items=len(users))
    return sorted(
        users.values(),
        key=lambda item: (
            str(item.get("username") or "").casefold(),
            str(item.get("user_id") or ""),
        ),
    )


# ─── Helpers ─────────────────────────────────────────────────────────────────

async def detect_captcha_on_page(page: Page) -> bool:
    """Quick CAPTCHA check without importing stealth module (avoids circular)."""
    url = page.url.lower()
    captcha_fragments = ["captcha", "challenge", "checkpoint", "verify", "unusual_traffic"]
    for f in captcha_fragments:
        if f in url:
            return True
    captcha_el = await page.query_selector(
        "iframe[src*='captcha'], iframe[src*='recaptcha'], #captcha"
    )
    return captcha_el is not None


async def _wait_for_any_selector(
    page: Page,
    selectors: list[str],
    *,
    timeout_ms: int,
    poll_ms: int = 250,
) -> str | None:
    """Return the first selector that resolves on the page within timeout_ms."""
    deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)
    while asyncio.get_running_loop().time() < deadline:
        for selector in selectors:
            try:
                if await page.query_selector(selector):
                    return selector
            except Exception:
                continue
        await asyncio.sleep(poll_ms / 1000)
    return None


async def _dismiss_cookie_banner(page: Page, cfg: "StealthConfig") -> None:
    """Best-effort dismissal of cookie banners that can block the login form."""
    selectors = [
        "button:has-text('Only allow essential cookies')",
        "button:has-text('Allow all cookies')",
        "button:has-text('Accept all')",
        "button:has-text('Accept')",
    ]
    for selector in selectors:
        try:
            if await page.query_selector(selector):
                await human_click(page, selector, cfg)
                await short_delay(cfg)
                return
        except Exception:
            continue


async def _handle_saved_profile_prompt(page: Page, username: str,
                                       cfg: "StealthConfig") -> bool:
    """
    Handle Instagram's remembered-account screen.
    Returns True if it successfully continued into a logged-in state.
    """
    try:
        body_text = await page.locator("body").inner_text()
    except Exception:
        return False

    body_lower = body_text.lower()
    if "continue" not in body_lower and "use another profile" not in body_lower:
        return False

    continue_selectors = [
        "div[role='button']:has-text('Continue')",
        "button:has-text('Continue')",
    ]
    use_other_selectors = [
        "div[role='button']:has-text('Use another profile')",
        "button:has-text('Use another profile')",
    ]

    for selector in use_other_selectors:
        try:
            element = await page.query_selector(selector)
            if not element or not await element.is_visible():
                continue
            await human_click(page, selector, cfg)
            await short_delay(cfg)
            return False
        except Exception:
            continue

    if username.lower() in body_lower:
        for selector in continue_selectors:
            try:
                element = await page.query_selector(selector)
                if not element or not await element.is_visible():
                    continue
                if await element.get_attribute("aria-disabled") == "true":
                    continue
                await human_click(page, selector, cfg)
                try:
                    await page.wait_for_load_state("networkidle", timeout=10_000)
                except Exception:
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=5_000)
                    except Exception:
                        pass
                return await check_login_state(page)
            except Exception:
                continue

    return False


async def _submit_login_form(page: Page, password_selector: str,
                             cfg: "StealthConfig") -> bool:
    """
    Submit the Instagram login form.
    Prefer the visible role=button control; fall back to Enter or form submission.
    """
    visible_submit_selectors = [
        "div[role='button'][aria-label='Log In']",
        "div[role='button'][aria-label='Log in']",
        "div[role='button']:has-text('Log in')",
        "div[role='button']:has-text('Log In')",
        "button[type='submit']",
        "form button:has-text('Log in')",
        "form button:has-text('Log In')",
    ]

    for selector in visible_submit_selectors:
        try:
            element = await page.query_selector(selector)
            if not element:
                continue
            if not await element.is_visible():
                continue
            if await element.get_attribute("aria-disabled") == "true":
                continue
            if await element.get_attribute("disabled") is not None:
                continue
            await human_click(page, selector, cfg)
            return True
        except Exception:
            continue

    try:
        await page.locator(password_selector).press("Enter")
        return True
    except Exception:
        pass

    try:
        return bool(await page.evaluate("""
            () => {
                const form = document.querySelector("form");
                if (!form) return false;
                if (typeof form.requestSubmit === "function") {
                    form.requestSubmit();
                    return true;
                }
                const submit = form.querySelector("button[type='submit'], input[type='submit']");
                if (submit) {
                    submit.click();
                    return true;
                }
                return false;
            }
        """))
    except Exception:
        return False


INSTAGRAM_DM_INBOX_DOC_ID = "26178302485155710"
INSTAGRAM_DM_THREAD_DOC_ID = "25920550077615885"
INSTAGRAM_DM_COMPOSER_SELECTOR = (
    "div[contenteditable='true'][role='textbox'], "
    "div[contenteditable='true'][aria-label*='Message'], "
    "textarea[placeholder*='Message']"
)
INSTAGRAM_DM_SEND_BUTTON_SELECTOR = (
    "button:has-text('Send'), "
    "button[aria-label='Send'], "
    "div[role='button'][aria-label='Send']"
)
INSTAGRAM_PROFILE_MESSAGE_BUTTON_SELECTOR = (
    "a[href*='/direct/t/'], "
    "button:has-text('Message'), "
    "div[role='button']:has-text('Message')"
)


async def scrape_dms(page: Page, own_username: str, conn, cfg: "Config") -> list[dict]:
    """Scrape private Instagram DM threads for the logged-in account."""
    log_event("instagram", "scrape_start", kind="dms", account=f"@{own_username}")
    inbox_url = f"{BASE_URL}/direct/inbox/"
    bootstrap: dict[str, object] = {
        "graphql_post_data": None,
        "thread_post_data": None,
        "mailbox_threads": {},
        "has_more_threads": False,
    }

    async def _capture_response(resp) -> None:
        try:
            req = resp.request
            if req.resource_type not in {"xhr", "fetch"}:
                return
            if "instagram.com/api/graphql" not in resp.url:
                return

            post_data = req.post_data or ""
            if post_data and not bootstrap["graphql_post_data"]:
                bootstrap["graphql_post_data"] = post_data

            text = await resp.text()
            if "PolarisDirectInboxQuery" in post_data and "get_slide_mailbox_for_iris_subscription" in text:
                payload = json.loads(text)
                mailbox = (payload.get("data") or {}).get("get_slide_mailbox_for_iris_subscription") or {}
                threads = (mailbox.get("threads_by_folder") or {}).get("edges") or []
                page_info = (mailbox.get("threads_by_folder") or {}).get("page_info") or {}
                if page_info.get("has_next_page"):
                    bootstrap["has_more_threads"] = True
                for edge in threads:
                    thread = ((edge or {}).get("node") or {}).get("as_ig_direct_thread") or {}
                    candidate = _instagram_mailbox_candidate(thread)
                    if not candidate:
                        continue
                    mailbox_threads: dict = bootstrap["mailbox_threads"]  # type: ignore[assignment]
                    mailbox_threads[candidate["conversation_id"]] = candidate

            if (
                "IGDThreadDetailMainViewContainerQuery" in post_data
                and "get_slide_thread_nullable" in text
                and not bootstrap["thread_post_data"]
            ):
                bootstrap["thread_post_data"] = post_data
        except Exception:
            return

    page.on("response", _capture_response)
    await page.goto(inbox_url, wait_until="domcontentloaded", timeout=30_000)
    await random_delay(cfg.stealth)
    await assert_no_captcha(page, PLATFORM)
    await page.wait_for_timeout(6_000)

    visible_candidates = await _instagram_collect_visible_dm_candidates(page, own_username)
    mailbox_threads: dict[str, dict] = dict(bootstrap["mailbox_threads"] or {})
    thread_candidates = list(mailbox_threads.values())
    if bootstrap.get("has_more_threads"):
        log_event(
            "instagram",
            "scrape_partial",
            level="warning",
            kind="dms",
            account=f"@{own_username}",
            reason="more_threads_available_than_first_page_exposed",
        )

    seen_thread_keys = {
        candidate["conversation_id"]
        for candidate in thread_candidates
        if candidate.get("conversation_id")
    }
    for candidate in visible_candidates:
        thread_key = candidate.get("conversation_id")
        if not thread_key or thread_key in seen_thread_keys:
            continue
        seen_thread_keys.add(thread_key)
        thread_candidates.append(candidate)

    if bootstrap.get("has_more_threads") and not thread_candidates:
        if not await check_login_state(page):
            raise LogoutError(
                f"Instagram DM scrape lost its authenticated session for @{own_username} before any thread candidates could be resolved."
            )
        raise RuntimeError(
            f"Instagram DM scrape for @{own_username} detected mailbox pagination but did not resolve any thread candidates."
        )

    template_post_data = bootstrap.get("thread_post_data") or bootstrap.get("graphql_post_data")
    if not isinstance(template_post_data, str) or not template_post_data:
        raise RuntimeError("Instagram DM GraphQL bootstrap payload was not captured")

    conversations: list[dict] = []
    fetched_threads = 0
    skipped_broadcast_threads = 0
    for candidate in thread_candidates:
        thread_key = candidate.get("conversation_id")
        if not thread_key:
            continue
        thread = await _instagram_fetch_thread(page, template_post_data, thread_key)
        if not thread:
            continue
        fetched_threads += 1
        conversation = _instagram_normalize_thread(thread, own_username, candidate)
        if not conversation:
            continue
        if conversation.get("is_broadcast_channel"):
            skipped_broadcast_threads += 1
            continue
        conversations.append(conversation)

    if thread_candidates and not conversations:
        if not await check_login_state(page):
            raise LogoutError(
                f"Instagram DM scrape lost its authenticated session for @{own_username} while fetching thread details."
            )
        if fetched_threads == 0:
            raise RuntimeError(
                f"Instagram DM scrape for @{own_username} discovered {len(thread_candidates)} candidate threads but could not fetch any thread details."
            )
        if skipped_broadcast_threads == fetched_threads:
            log_event(
                "instagram",
                "scrape_result",
                kind="dms",
                account=f"@{own_username}",
                conversations=0,
                skipped_broadcast_threads=skipped_broadcast_threads,
            )
            return []
        raise RuntimeError(
            f"Instagram DM scrape for @{own_username} fetched {fetched_threads} thread details but resolved zero non-broadcast conversations."
        )

    log_event("instagram", "scrape_result", kind="dms", account=f"@{own_username}", conversations=len(conversations))
    return conversations


def _instagram_mailbox_candidate(thread: dict) -> dict | None:
    conversation_id = str(
        thread.get("thread_key")
        or thread.get("thread_fbid")
        or thread.get("id")
        or ""
    ).strip()
    if not conversation_id:
        return None

    users = thread.get("users") or []
    title = thread.get("thread_title") or ", ".join(
        user.get("full_name") or user.get("username") or ""
        for user in users
        if user.get("full_name") or user.get("username")
    )
    return {
        "conversation_id": conversation_id,
        "title": title or None,
        "subtitle": thread.get("thread_subtype"),
        "conversation_url": f"{BASE_URL}/direct/t/{conversation_id}/",
        "is_group": bool(thread.get("is_group")),
        "is_broadcast_channel": bool(thread.get("creator_broadcast_thread_data")),
        "metadata": {
            "source": "mailbox_query",
            "thread_subtype": thread.get("thread_subtype"),
        },
    }


async def _instagram_collect_visible_dm_candidates(page: Page, own_username: str) -> list[dict]:
    """Scroll the inbox thread list and collect candidates, resolving thread keys when needed."""
    collected: dict[str, dict] = {}
    stalled_scrolls = 0
    resolved_row_signatures: set[tuple[str, str]] = set()

    for _ in range(120):
        for row in await _instagram_visible_dm_rows(page):
            candidate = _instagram_candidate_from_visible_dm_row(row)
            if not candidate and _instagram_row_likely_conversation(row, own_username):
                signature = (
                    str(row.get("text") or "").strip(),
                    str(row.get("aria_label") or "").strip(),
                )
                if signature not in resolved_row_signatures:
                    resolved_row_signatures.add(signature)
                    thread_key = await _instagram_open_visible_dm_row(page, row["index"])
                    if thread_key:
                        row = {**row, "thread_key": thread_key}
                        candidate = _instagram_candidate_from_visible_dm_row(row)
            if not candidate:
                continue
            collected[candidate["conversation_id"]] = candidate

        scroll_state = await _instagram_scroll_dm_thread_list(page)
        if not scroll_state or not scroll_state.get("moved"):
            stalled_scrolls += 1
            if stalled_scrolls >= 3:
                break
            await page.wait_for_timeout(400)
            continue

        stalled_scrolls = 0
        await page.wait_for_timeout(900)

    return list(collected.values())


def _instagram_row_likely_conversation(row: dict, own_username: str) -> bool:
    """Heuristic filter to avoid clicking controls like compose/new-note buttons."""
    text = str(row.get("text") or "").strip()
    aria_label = str(row.get("aria_label") or "").strip()
    combined = f"{text}\n{aria_label}".strip().lower()
    own_username = (own_username or "").strip().lower()

    if not combined:
        return False
    if combined in {own_username, f"@{own_username}"}:
        return False
    if combined.startswith("new message"):
        return False
    if "your note" in combined:
        return False
    if "first note of the week" in combined:
        return False
    if "status\n" in combined or combined.startswith("status"):
        return False
    if "\n" in combined or "·" in combined:
        return True
    return len(combined) >= 3


def _instagram_candidate_from_visible_dm_row(row: dict) -> dict | None:
    """Build a DM conversation candidate from an inbox row when its thread key is visible."""
    thread_key = str(row.get("thread_key") or "").strip()
    if not thread_key:
        return None

    return {
        "conversation_id": thread_key,
        "title": row.get("text"),
        "subtitle": None,
        "conversation_url": f"{BASE_URL}/direct/t/{thread_key}/",
        "is_group": False,
        "is_broadcast_channel": False,
        "metadata": {
            "source": "visible_thread_row",
            "row_text": row.get("text"),
            "row_aria_label": row.get("aria_label"),
            "row_href": row.get("href"),
        },
    }


async def _instagram_visible_dm_rows(page: Page) -> list[dict]:
    """Return visible inbox row descriptors in DOM order."""
    try:
        rows = await page.evaluate("""
            () => {
                const resolveHref = (value) => {
                    if (!value) return "";
                    try {
                        return new URL(value, window.location.origin).pathname;
                    } catch (error) {
                        return String(value || "");
                    }
                };
                return Array.from(
                    document.querySelectorAll("div[aria-label='Thread list'] [role='button']")
                ).map((el, index) => ({
                    index,
                    text: (el.innerText || el.textContent || "").trim(),
                    aria_label: el.getAttribute("aria-label") || "",
                    href: resolveHref(
                        (
                            el.closest("a[href*='/direct/t/']")
                            || el.querySelector("a[href*='/direct/t/']")
                            || el.parentElement?.querySelector("a[href*='/direct/t/']")
                        )?.getAttribute("href")
                    ),
                }));
            }
        """)
    except Exception:
        return []

    result = []
    for row in rows or []:
        text = str(row.get("text") or "").strip()
        aria_label = str(row.get("aria_label") or "").strip()
        href = str(row.get("href") or "").strip()
        thread_match = re.search(r"/direct/t/([^/?#]+)/", href)
        if not text and not aria_label and not href:
            continue
        result.append({
            "index": int(row.get("index", 0)),
            "text": text,
            "aria_label": aria_label,
            "href": href or None,
            "thread_key": thread_match.group(1) if thread_match else None,
        })
    return result


async def _instagram_scroll_dm_thread_list(page: Page) -> dict | None:
    """Scroll the nested inbox list so Instagram loads older thread rows."""
    try:
        return await page.evaluate("""
            () => {
                const isScrollable = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    return (
                        ["auto", "scroll"].includes(style.overflowY)
                        && (el.scrollHeight - el.clientHeight) > 24
                    );
                };

                const root = document.querySelector("div[aria-label='Thread list']");
                if (!root) return null;

                const candidates = [];
                const push = (el) => {
                    if (el && !candidates.includes(el)) candidates.push(el);
                };

                push(root);
                push(root.parentElement);
                push(root.parentElement?.parentElement);
                for (const el of Array.from(root.querySelectorAll("*")).slice(0, 500)) {
                    push(el);
                }

                let scroller = candidates.find(isScrollable) || null;
                let current = root;
                while (!scroller && current) {
                    if (isScrollable(current)) {
                        scroller = current;
                        break;
                    }
                    current = current.parentElement;
                }
                if (!scroller) return null;

                const before = scroller.scrollTop;
                const maxScroll = Math.max(scroller.scrollHeight - scroller.clientHeight, 0);
                const delta = Math.max(Math.floor(scroller.clientHeight * 0.82), 320);
                scroller.scrollTop = Math.min(before + delta, maxScroll);
                return {
                    before,
                    after: scroller.scrollTop,
                    max_scroll: maxScroll,
                    moved: scroller.scrollTop > before,
                };
            }
        """)
    except Exception:
        return None


async def _instagram_open_visible_dm_row(page: Page, row_index: int) -> str | None:
    """Click one inbox row and return the resolved thread key when it opened a DM."""
    prior_url = page.url
    try:
        buttons = page.locator("div[aria-label='Thread list'] [role='button']")
        if row_index >= await buttons.count():
            return None
        target = buttons.nth(row_index)
        await target.scroll_into_view_if_needed(timeout=10_000)
        await target.click(timeout=10_000)
    except Exception:
        return None

    for _ in range(12):
        match = re.search(r"/direct/t/([^/?#]+)/", page.url)
        if match:
            return match.group(1)
        if page.url != prior_url:
            await page.wait_for_timeout(300)
            continue
        await page.wait_for_timeout(250)
    return None


def _instagram_dm_conversation_url(conversation_id: str) -> str:
    return f"{BASE_URL}/direct/t/{conversation_id}/"


async def _instagram_open_dm_thread(
    page: Page,
    conversation_id: str,
    cfg: "Config",
) -> None:
    await page.goto(
        _instagram_dm_conversation_url(conversation_id),
        wait_until="domcontentloaded",
        timeout=30_000,
    )
    await random_delay(cfg.stealth)
    await assert_no_captcha(page, PLATFORM)
    await page.wait_for_timeout(3_500)


async def _instagram_wait_for_dm_composer(page: Page) -> None:
    await page.locator(INSTAGRAM_DM_COMPOSER_SELECTOR).first.wait_for(timeout=20_000)


async def _instagram_dm_composer_text(page: Page) -> str:
    composer = page.locator(INSTAGRAM_DM_COMPOSER_SELECTOR).first
    try:
        if await composer.evaluate("el => el.tagName === 'TEXTAREA'"):
            return str(await composer.input_value()).strip()
    except Exception:
        pass

    try:
        return str(
            await composer.evaluate(
                "(el) => (el.innerText || el.textContent || '').trim()"
            )
        ).strip()
    except Exception:
        return ""


async def send_dm_text(
    page: Page,
    own_username: str,
    conversation_id: str,
    text: str,
    cfg: "Config",
) -> dict[str, object]:
    """Send one text message in an Instagram DM thread using the logged-in web session."""
    message_text = str(text or "").strip()
    if not message_text:
        raise ValueError("Message text is required.")

    await _instagram_open_dm_thread(page, conversation_id, cfg)
    await _instagram_wait_for_dm_composer(page)

    existing_draft = await _instagram_dm_composer_text(page)
    if existing_draft:
        raise ValueError("Instagram already has an unsent draft in this conversation.")

    composer = page.locator(INSTAGRAM_DM_COMPOSER_SELECTOR).first
    await composer.click(timeout=10_000)
    await page.keyboard.type(message_text, delay=28)
    await page.wait_for_timeout(250)

    send_button = page.locator(INSTAGRAM_DM_SEND_BUTTON_SELECTOR).first
    sent = False
    try:
        if await send_button.count() and await send_button.is_visible():
            await send_button.click(timeout=10_000)
            sent = True
    except Exception:
        sent = False

    if not sent:
        await page.keyboard.press("Enter")

    for _ in range(40):
        if not await _instagram_dm_composer_text(page):
            break
        await page.wait_for_timeout(250)
    else:
        raise RuntimeError("Instagram did not clear the DM composer after send.")

    await page.wait_for_timeout(1_500)
    return {
        "platform": PLATFORM,
        "account_username": own_username,
        "conversation_id": conversation_id,
        "conversation_url": _instagram_dm_conversation_url(conversation_id),
        "text": message_text,
    }


def _instagram_extract_conversation_id_from_url(raw_url: str) -> str | None:
    match = re.search(r"/direct/t/([^/?#]+)/", raw_url or "")
    if match:
        return match.group(1)
    return None


async def _instagram_current_conversation_id(page: Page) -> str | None:
    conversation_id = _instagram_extract_conversation_id_from_url(page.url)
    if conversation_id:
        return conversation_id
    try:
        href = await page.evaluate(
            """
            () => {
                const link = document.querySelector("a[href*='/direct/t/']");
                return link ? link.getAttribute("href") : "";
            }
            """
        )
    except Exception:
        href = ""
    return _instagram_extract_conversation_id_from_url(str(href or ""))


async def send_new_dm_text(
    page: Page,
    own_username: str,
    target_username: str,
    text: str,
    cfg: "Config",
) -> dict[str, object]:
    """Start a new DM thread from a profile page and send the first message."""
    message_text = str(text or "").strip()
    if not message_text:
        raise ValueError("Message text is required.")
    if not target_username:
        raise ValueError("target_username is required.")

    await page.goto(f"{BASE_URL}/{target_username}/", wait_until="domcontentloaded", timeout=30_000)
    await random_delay(cfg.stealth)
    await assert_no_captcha(page, PLATFORM)
    await page.wait_for_timeout(2_000)

    message_button = page.locator(INSTAGRAM_PROFILE_MESSAGE_BUTTON_SELECTOR).first
    if not await message_button.count():
        raise RuntimeError(f"Instagram did not expose a Message button for @{target_username}.")

    await message_button.click(timeout=10_000)

    composer_ready = False
    for _ in range(40):
        try:
            await _instagram_wait_for_dm_composer(page)
            composer_ready = True
            break
        except Exception:
            if await _instagram_current_conversation_id(page):
                composer_ready = True
                break
            await page.wait_for_timeout(250)

    if not composer_ready:
        raise RuntimeError(f"Instagram did not open a DM composer for @{target_username}.")

    await _instagram_wait_for_dm_composer(page)
    composer = page.locator(INSTAGRAM_DM_COMPOSER_SELECTOR).first
    await composer.click(timeout=10_000)
    await page.keyboard.type(message_text, delay=28)
    await page.wait_for_timeout(250)

    send_button = page.locator(INSTAGRAM_DM_SEND_BUTTON_SELECTOR).first
    sent = False
    try:
        if await send_button.count() and await send_button.is_visible():
            await send_button.click(timeout=10_000)
            sent = True
    except Exception:
        sent = False

    if not sent:
        await page.keyboard.press("Enter")

    for _ in range(40):
        if not await _instagram_dm_composer_text(page):
            break
        await page.wait_for_timeout(250)
    else:
        raise RuntimeError("Instagram did not clear the DM composer after initiation send.")

    await page.wait_for_timeout(1_500)
    conversation_id = await _instagram_current_conversation_id(page)
    if not conversation_id:
        raise RuntimeError("Instagram initiation send did not expose a conversation_id.")

    return {
        "platform": PLATFORM,
        "account_username": own_username,
        "target_username": target_username,
        "conversation_id": conversation_id,
        "conversation_url": _instagram_dm_conversation_url(conversation_id),
        "text": message_text,
    }


async def scrape_dm_thread(
    page: Page,
    own_username: str,
    conversation_id: str,
    cfg: "Config",
) -> dict | None:
    """Fetch and normalize one Instagram DM thread from its thread page."""
    bootstrap: dict[str, object] = {
        "thread_post_data": None,
        "thread_payload": None,
    }

    async def _capture_response(resp) -> None:
        try:
            req = resp.request
            if req.resource_type not in {"xhr", "fetch"}:
                return
            if "instagram.com/api/graphql" not in resp.url:
                return

            post_data = req.post_data or ""
            text = await resp.text()
            if "IGDThreadDetailMainViewContainerQuery" not in post_data:
                return
            if "get_slide_thread_nullable" not in text:
                return
            payload = json.loads(text)
            thread = (
                ((payload.get("data") or {}).get("get_slide_thread_nullable") or {})
                .get("as_ig_direct_thread")
            )
            if not thread:
                return
            thread_key = str(
                thread.get("thread_key")
                or thread.get("thread_fbid")
                or thread.get("id")
                or ""
            ).strip()
            if thread_key != str(conversation_id):
                return
            bootstrap["thread_post_data"] = post_data
            bootstrap["thread_payload"] = thread
        except Exception:
            return

    page.on("response", _capture_response)
    await _instagram_open_dm_thread(page, conversation_id, cfg)

    thread = bootstrap.get("thread_payload")
    template_post_data = bootstrap.get("thread_post_data")
    if not isinstance(thread, dict) and isinstance(template_post_data, str) and template_post_data:
        thread = await _instagram_fetch_thread(page, template_post_data, conversation_id)
    elif isinstance(thread, dict):
        page_info = ((thread.get("slide_messages") or {}).get("page_info") or {})
        if page_info.get("has_next_page") and isinstance(template_post_data, str) and template_post_data:
            expanded = await _instagram_fetch_thread(page, template_post_data, conversation_id)
            if expanded:
                thread = expanded

    if not isinstance(thread, dict):
        return None
    return _instagram_normalize_thread(thread, own_username)


async def _instagram_fetch_thread(page: Page, template_post_data: str, thread_key: str) -> dict | None:
    """Fetch one DM thread via the same GraphQL surface used by the Instagram web app."""
    thread = await _instagram_fetch_thread_with_count(page, template_post_data, thread_key, 1_000)
    if not thread:
        return None

    page_info = ((thread.get("slide_messages") or {}).get("page_info") or {})
    if page_info.get("has_next_page"):
        expanded = await _instagram_fetch_thread_with_count(page, template_post_data, thread_key, 5_000)
        if expanded:
            thread = expanded
            page_info = ((thread.get("slide_messages") or {}).get("page_info") or {})

    if page_info.get("has_next_page"):
        logger.warning(
            f"[instagram] DM thread {thread_key} still reports more history after a 5000-message fetch"
        )
    return thread


async def _instagram_fetch_thread_with_count(
    page: Page,
    template_post_data: str,
    thread_key: str,
    message_count: int,
) -> dict | None:
    payload = await _instagram_graphql_fetch(
        page,
        template_post_data,
        doc_id=INSTAGRAM_DM_THREAD_DOC_ID,
        friendly_name="IGDThreadDetailMainViewContainerQuery",
        variables={
            "min_uq_seq_id": None,
            "thread_fbid": thread_key,
            "__relay_internal__pv__IGDEnableOffMsysMessagesListQErelayprovider": True,
            "__relay_internal__pv__IGDEnableOffMsysPinnedMessagesQErelayprovider": False,
            "__relay_internal__pv__IGDInitialMessagePageCountrelayprovider": message_count,
        },
    )
    return (
        ((payload.get("data") or {}).get("get_slide_thread_nullable") or {}).get("as_ig_direct_thread")
        if payload
        else None
    )


async def _instagram_graphql_fetch(
    page: Page,
    template_post_data: str,
    *,
    doc_id: str,
    friendly_name: str,
    variables: dict,
) -> dict | None:
    """Replay a logged-in Instagram GraphQL POST with updated variables/doc id."""
    params = parse_qs(template_post_data, keep_blank_values=True)
    body = {key: values[-1] for key, values in params.items()}
    body["fb_api_req_friendly_name"] = friendly_name
    body["doc_id"] = doc_id
    body["variables"] = json.dumps(variables, ensure_ascii=False, separators=(",", ":"))
    encoded = urlencode(body)

    try:
        result = await page.evaluate(
            """
            async (body) => {
                const resp = await fetch('/api/graphql', {
                    method: 'POST',
                    headers: {'content-type': 'application/x-www-form-urlencoded'},
                    credentials: 'include',
                    body,
                });
                return {status: resp.status, text: await resp.text()};
            }
            """,
            encoded,
        )
    except Exception as exc:
        logger.warning(f"[instagram] GraphQL DM fetch failed: {exc}")
        return None

    if int(result.get("status") or 0) != 200:
        logger.warning(
            f"[instagram] DM GraphQL returned HTTP {result.get('status')} for {friendly_name}"
        )
        return None

    try:
        return json.loads(result.get("text") or "{}")
    except Exception as exc:
        logger.warning(f"[instagram] Could not decode DM GraphQL payload: {exc}")
        return None


def _instagram_normalize_thread(thread: dict, own_username: str, candidate: dict | None = None) -> dict | None:
    """Convert Instagram thread payloads into the scanner's DM archive schema."""
    conversation_id = str(
        thread.get("thread_key")
        or thread.get("thread_fbid")
        or thread.get("id")
        or ""
    ).strip()
    if not conversation_id:
        return None

    thread_subtype = str(thread.get("thread_subtype") or "")
    is_broadcast = bool(thread.get("creator_broadcast_thread_data")) or "BROADCAST" in thread_subtype.upper()
    title = thread.get("thread_title") or (candidate or {}).get("title")
    users = thread.get("users") or []
    if not title:
        title = ", ".join(
            user.get("full_name") or user.get("username") or ""
            for user in users
            if user.get("full_name") or user.get("username")
        )

    viewer_id = str(thread.get("viewer_id") or "").strip()
    participants: list[dict] = []
    seen_participants: set[tuple[str, str]] = set()
    for index, user in enumerate(users):
        username = user.get("username")
        user_id = str(user.get("id") or user.get("interop_messaging_user_fbid") or "").strip()
        participant_key = (str(username or ""), user_id)
        seen_participants.add(participant_key)
        participants.append(
            {
                "username": username,
                "user_id": user_id or None,
                "display_name": user.get("full_name"),
                "profile_url": f"{BASE_URL}/{username}/" if username else None,
                "profile_pic_url": user.get("profile_pic_url"),
                "is_self": False,
                "participant_order": index,
                "metadata": {
                    "is_verified": bool(user.get("is_verified")),
                    "interop_messaging_user_fbid": user.get("interop_messaging_user_fbid"),
                },
            }
        )

    if own_username and (own_username, viewer_id) not in seen_participants:
        participants.append(
            {
                "username": own_username,
                "user_id": viewer_id or None,
                "display_name": None,
                "profile_url": f"{BASE_URL}/{own_username}/",
                "profile_pic_url": None,
                "is_self": True,
                "participant_order": len(participants),
                "metadata": {"viewer_id": viewer_id or None},
            }
        )

    message_edges = ((thread.get("slide_messages") or {}).get("edges") or [])
    messages: list[dict] = []
    for order, edge in enumerate(reversed(message_edges)):
        node = (edge or {}).get("node") or {}
        messages.append(
            _instagram_normalize_message(
                node,
                own_username=own_username,
                viewer_id=viewer_id,
                order=order,
            )
        )

    page_info = (thread.get("slide_messages") or {}).get("page_info") or {}
    newest_message = messages[-1] if messages else None
    last_message_preview = (
        newest_message.get("text")
        if newest_message and newest_message.get("text")
        else (candidate or {}).get("title")
    )

    return {
        "platform": PLATFORM,
        "conversation_id": conversation_id,
        "conversation_type": "group" if thread.get("is_group") else "direct",
        "title": title or None,
        "subtitle": (candidate or {}).get("subtitle"),
        "conversation_url": f"{BASE_URL}/direct/t/{conversation_id}/",
        "is_group": bool(thread.get("is_group")),
        "is_broadcast_channel": is_broadcast,
        "last_message_preview": last_message_preview,
        "last_message_at": _instagram_iso_from_ms(thread.get("last_activity_timestamp_ms")),
        "participants": participants,
        "messages": messages,
        "metadata": {
            "thread_id": thread.get("id"),
            "thread_fbid": thread.get("thread_fbid"),
            "thread_subtype": thread_subtype or None,
            "input_mode": thread.get("input_mode"),
            "nicknames": thread.get("nicknames") or [],
            "read_receipts": thread.get("slide_read_receipts") or [],
            "messages_truncated": bool(page_info.get("has_next_page")),
            "page_info": page_info,
            "candidate": candidate or {},
        },
    }


def _instagram_normalize_message(
    node: dict,
    *,
    own_username: str,
    viewer_id: str,
    order: int,
) -> dict:
    content = node.get("content") or {}
    content_typename = str(content.get("__typename") or "")
    sender = node.get("sender") or {}
    sender_user_dict = sender.get("user_dict") or {}
    sender_username = sender_user_dict.get("username")
    sender_user_id = str(sender.get("id") or node.get("sender_fbid") or "").strip()
    is_self = bool(sender_username and sender_username == own_username) or (
        bool(viewer_id) and sender_user_id == viewer_id
    )

    text = _instagram_message_text(node, content)
    attachments = _instagram_message_attachments(content)
    message_type = _instagram_message_type(content_typename, attachments, text)

    return {
        "message_id": node.get("message_id") or node.get("id"),
        "message_key": node.get("message_id") or node.get("offline_threading_id") or node.get("id"),
        "sender_username": sender_username or (own_username if is_self else None),
        "sender_user_id": sender_user_id or None,
        "sender_display_name": sender.get("name"),
        "sent_at": _instagram_iso_from_ms(node.get("timestamp_ms")),
        "sent_at_label": None,
        "message_type": message_type,
        "text": text,
        "status_text": None,
        "reply_to_message_id": node.get("replied_to_message_id"),
        "message_order": order,
        "is_self": is_self,
        "attachments": attachments,
        "raw_payload": node,
    }


def _instagram_message_type(content_typename: str, attachments: list[dict], text: str | None) -> str:
    typename = content_typename.replace("SlideMessage", "").replace("Content", "").lower()
    if attachments:
        attachment_type = attachments[0].get("attachment_type")
        if attachment_type == "video":
            return "video"
        if attachment_type == "photo":
            return "photo"
        if attachment_type == "share_preview":
            return "share"
    if typename == "admintext":
        return "system"
    if typename == "text":
        return "text"
    if typename:
        return typename
    if text:
        return "text"
    return "unknown"


def _instagram_message_text(node: dict, content: dict) -> str | None:
    if node.get("text_body"):
        return node.get("text_body")
    if content.get("text_body"):
        return content.get("text_body")
    if content.get("__typename") == "SlideMessageAdminText":
        return _instagram_join_text_fragments(content.get("text_fragments") or [])
    if content.get("__typename") == "SlideMessageXMAContent":
        xma = content.get("xma") or {}
        return (
            content.get("xma_text_body")
            or xma.get("title_text")
            or xma.get("xmaTitle")
            or xma.get("caption_body_text")
            or xma.get("subtitle_text")
        )
    return None


def _instagram_join_text_fragments(fragments: list[dict]) -> str | None:
    parts = []
    for fragment in fragments:
        plaintext = str(fragment.get("plaintext") or "").strip()
        if plaintext:
            parts.append(plaintext)
    if not parts:
        return None
    return "".join(parts)


def _instagram_message_attachments(content: dict) -> list[dict]:
    attachments: list[dict] = []
    typename = content.get("__typename")
    if typename == "SlideMessageImageContent":
        for index, attachment in enumerate(content.get("attachments") or []):
            attachments.append(_instagram_attachment_payload(attachment, "photo", index))
    elif typename == "SlideMessageVideosContent":
        for index, attachment in enumerate(content.get("videos") or []):
            attachments.append(_instagram_attachment_payload(attachment, "video", index))
    elif typename == "SlideMessageXMAContent":
        xma = content.get("xma") or {}
        preview = xma.get("preview_image") or xma.get("xmaPreviewImage") or {}
        preview_url = preview.get("url") or preview.get("fallback_url")
        if preview_url:
            attachments.append(
                {
                    "attachment_id": xma.get("target_id") or preview.get("attachment_fbid"),
                    "attachment_order": 0,
                    "attachment_type": "share_preview",
                    "source_url": preview_url,
                    "preview_url": preview.get("url"),
                    "mime_type": "image/jpeg",
                    "width": preview.get("width") or preview.get("preview_width"),
                    "height": preview.get("height") or preview.get("preview_height"),
                    "preview_text": " | ".join(
                        part
                        for part in [
                            xma.get("title_text") or xma.get("xmaTitle"),
                            xma.get("caption_body_text") or content.get("xma_text_body"),
                            xma.get("target_url"),
                        ]
                        if part
                    ) or None,
                    "metadata": {"xma": xma},
                }
            )
    return attachments


def _instagram_attachment_payload(attachment: dict, attachment_type: str, index: int) -> dict:
    source_url = (
        attachment.get("attachment_cdn_url")
        or attachment.get("preview_cdn_url")
        or attachment.get("attachment_cdn_fallback_url")
        or attachment.get("preview_cdn_fallback_url")
    )
    mime_type = "video/mp4" if attachment_type == "video" else "image/jpeg"
    return {
        "attachment_id": attachment.get("attachment_fbid"),
        "attachment_order": index,
        "attachment_type": attachment_type,
        "source_url": source_url,
        "preview_url": attachment.get("preview_cdn_url"),
        "mime_type": mime_type,
        "width": attachment.get("preview_width"),
        "height": attachment.get("preview_height"),
        "preview_text": None,
        "metadata": {"raw_attachment": attachment},
    }


def _instagram_iso_from_ms(value) -> str | None:
    if value in {None, ""}:
        return None
    try:
        dt = datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except Exception:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


async def _write_login_debug_capture(page: Page, username: str) -> Path:
    """Persist a screenshot of the login state for local debugging."""
    debug_dir = Path("data/debug")
    debug_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_username = re.sub(r"[^A-Za-z0-9_.-]+", "_", username)
    path = debug_dir / f"instagram_login_{safe_username}_{timestamp}.png"
    try:
        await page.screenshot(path=str(path), full_page=True)
    except Exception:
        pass
    return path


async def _write_profile_debug_capture(page: Page, target: str, suffix: str) -> Path:
    """Persist a screenshot of a profile page state for local debugging."""
    debug_dir = Path("data/debug")
    debug_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_target = re.sub(r"[^A-Za-z0-9_.-]+", "_", target)
    safe_suffix = re.sub(r"[^A-Za-z0-9_.-]+", "_", suffix)
    path = debug_dir / f"instagram_{safe_target}_{safe_suffix}_{timestamp}.png"
    try:
        await page.screenshot(path=str(path), full_page=True)
    except Exception:
        pass
    return path


async def _describe_login_failure(page: Page) -> str:
    """Return a compact human-readable summary of the post-login page."""
    try:
        texts = await page.evaluate("""
            () => {
                const re = /incorrect|password|login|code|security|challenge|checkpoint|confirm|verify|suspicious|unusual|try again|facebook/i;
                const seen = new Set();
                const lines = [];
                for (const el of document.querySelectorAll("h1, h2, h3, [role='alert'], span, div, p")) {
                    const text = (el.innerText || "").trim().replace(/\\s+/g, " ");
                    if (!text || text.length < 4 || text.length > 240) continue;
                    if (!re.test(text)) continue;
                    if (seen.has(text)) continue;
                    seen.add(text);
                    lines.push(text);
                    if (lines.length >= 6) break;
                }
                return lines;
            }
        """)
        if texts:
            return "Page hints: " + " | ".join(texts) + "."
    except Exception:
        pass

    try:
        body_text = await page.locator("body").inner_text()
        body_text = " ".join(body_text.split())
        if body_text:
            return f"Body text: {body_text[:240]}."
    except Exception:
        pass

    return "No visible failure text extracted."


async def short_delay(obj) -> None:  # type: ignore[override]
    """Adapter so short_delay can be called with either cfg or page (for convenience)."""
    import asyncio, random
    await asyncio.sleep(random.uniform(0.5, 1.5))
