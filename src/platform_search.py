"""
platform_search.py — Live cross-platform account discovery.

Searches Instagram, TikTok, and Facebook for real accounts matching a given
display name, using authenticated Playwright sessions (same sessions the main
scraper uses). Results are real accounts found on each platform — no fake
username variants.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any
from src.utils import platform_profile_url

_PLATFORMS = ("instagram", "tiktok", "facebook")

def platform_url(platform: str, username: str) -> str:
    return platform_profile_url(platform, username) or f"https://{platform}.com/{username}"


# ── Instagram search ──────────────────────────────────────────────────────────

async def _search_instagram(page, query: str) -> list[dict]:
    """
    Use Instagram's internal topsearch API (requires authenticated session).
    Returns list of {username, display_name, profile_url, profile_pic_url}.
    """
    import json as _json
    results = []
    url = f"https://www.instagram.com/web/search/topsearch/?query={_urlencode(query)}&context=blended&count=10"
    try:
        response = await page.evaluate(
            """async (url) => {
                const r = await fetch(url, {credentials: 'include'});
                if (!r.ok) return null;
                return await r.json();
            }""",
            url,
        )
        if not isinstance(response, dict):
            return []
        for user in (response.get("users") or []):
            u = user.get("user") or {}
            username = u.get("username") or ""
            if not username:
                continue
            results.append({
                "platform": "instagram",
                "username": username,
                "display_name": u.get("full_name") or username,
                "profile_url": platform_url("instagram", username),
                "profile_pic_url": u.get("profile_pic_url"),
                "follower_count": u.get("follower_count"),
                "is_verified": bool(u.get("is_verified")),
                "source": "live_search",
            })
    except Exception:
        pass
    return results


# ── TikTok search ─────────────────────────────────────────────────────────────

async def _search_tiktok(page, query: str) -> list[dict]:
    """
    Navigate to TikTok user search and extract result cards.
    """
    results = []
    try:
        url = f"https://www.tiktok.com/search/user?q={_urlencode(query)}"
        await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        await page.wait_for_timeout(3000)

        # Extract user cards from the search results
        cards = await page.evaluate(r"""
            () => {
                const cards = [];
                // TikTok search result user cards
                document.querySelectorAll('[data-e2e="search-user-card"], [data-e2e="user-card"], .tiktok-card').forEach(card => {
                    const link = card.querySelector('a[href*="/@"]');
                    const username = link ? (link.href.match(/\/@([^/?#]+)/) || [])[1] : null;
                    const displayName = card.querySelector('[data-e2e="search-user-unique-id"], .user-name, strong')?.textContent?.trim();
                    const picEl = card.querySelector('img[src*="tiktokcdn"], img[src*="muscdn"]');
                    if (username) cards.push({
                        username,
                        display_name: displayName || username,
                        profile_pic_url: picEl ? picEl.src : null,
                    });
                });
                // Fallback: look for any /@username links in the page
                if (cards.length === 0) {
                    const seen = new Set();
                    document.querySelectorAll('a[href*="/@"]').forEach(a => {
                        const m = a.href.match(/\/@([^/?&#]+)/);
                        if (!m) return;
                        const u = m[1];
                        if (seen.has(u) || u.length < 2) return;
                        seen.add(u);
                        const label = a.querySelector('[class*="name"], strong, span')?.textContent?.trim();
                        cards.push({username: u, display_name: label || u, profile_pic_url: null});
                    });
                }
                return cards.slice(0, 10);
            }
        """)
        for c in (cards or []):
            username = (c.get("username") or "").strip("@ ")
            if not username:
                continue
            results.append({
                "platform": "tiktok",
                "username": username,
                "display_name": c.get("display_name") or username,
                "profile_url": platform_url("tiktok", username),
                "profile_pic_url": c.get("profile_pic_url"),
                "source": "live_search",
            })
    except Exception:
        pass
    return results


# ── Facebook search ───────────────────────────────────────────────────────────

async def _search_facebook(page, query: str) -> list[dict]:
    """
    Navigate to Facebook people search and extract result cards.
    """
    results = []
    try:
        url = f"https://www.facebook.com/search/people/?q={_urlencode(query)}"
        await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        await page.wait_for_timeout(3000)

        cards = await page.evaluate(r"""
            () => {
                const results = [];
                const seen = new Set();

                // People search result items — Facebook uses role="article" or data-pagelet
                const candidates = [
                    ...document.querySelectorAll('[data-pagelet] a[href*="facebook.com"]'),
                    ...document.querySelectorAll('a[href*="/profile.php"], a[href*="facebook.com/"]'),
                ];
                for (const a of candidates) {
                    const href = a.href || '';
                    // Extract profile identifier — either /username or ?id=NUM
                    let identifier = null;
                    const idMatch = href.match(/[?&]id=(\d+)/);
                    const nameMatch = href.match(/facebook\.com\/([^/?#&]+)/);
                    if (idMatch) {
                        identifier = 'profile.php?id=' + idMatch[1];
                    } else if (nameMatch && !['search','home','watch','groups','marketplace','profile.php'].includes(nameMatch[1])) {
                        identifier = nameMatch[1];
                    }
                    if (!identifier || seen.has(identifier)) continue;
                    seen.add(identifier);

                    // Display name: nearest text heading near this link
                    const container = a.closest('div[class]') || a.parentElement;
                    const heading = container ? container.querySelector('span[dir], strong, h3, h2') : null;
                    const displayName = heading ? heading.textContent.trim() : '';

                    // Profile pic
                    const img = container ? container.querySelector('img[src*="fbcdn"], img[src*="facebook"]') : null;

                    if (displayName && displayName.length > 1) {
                        results.push({
                            identifier,
                            display_name: displayName,
                            profile_pic_url: img ? img.src : null,
                        });
                    }
                    if (results.length >= 10) break;
                }
                return results;
            }
        """)
        for c in (cards or []):
            identifier = (c.get("identifier") or "").strip()
            if not identifier:
                continue
            results.append({
                "platform": "facebook",
                "username": identifier,
                "display_name": c.get("display_name") or identifier,
                "profile_url": platform_url("facebook", identifier),
                "profile_pic_url": c.get("profile_pic_url"),
                "source": "live_search",
            })
    except Exception:
        pass
    return results


# ── Dispatcher ────────────────────────────────────────────────────────────────

_SEARCH_FN = {
    "instagram": _search_instagram,
    "tiktok": _search_tiktok,
    "facebook": _search_facebook,
}


async def search_platform(
    page,
    platform: str,
    query: str,
) -> list[dict]:
    """Search one platform for accounts matching query. Requires authenticated page."""
    fn = _SEARCH_FN.get(platform)
    if not fn:
        return []
    return await fn(page, query)


async def run_cross_platform_search(
    *,
    project_root: Path,
    source_platform: str,
    source_username: str,
    display_name: str,
) -> list[dict]:
    """
    Launch authenticated browser sessions for each platform and search for
    accounts matching display_name. Skips the source platform.

    Returns a flat list of real account dicts found across all platforms.
    """
    from src.config import load_config
    from src.db import get_connection
    from src.proxy_manager import ProxyManager
    from src.main import _ensure_logged_in, _run_with_session

    def _env_path(root: Path) -> Path:
        local = root / ".env.local"
        return local if local.exists() else root / ".env"

    cfg = load_config(project_root / "settings.yaml", _env_path(project_root))
    conn = get_connection(cfg.db_path)
    proxy_mgr = ProxyManager(cfg.proxies, conn)

    # Find one configured account per target platform
    platform_accounts: dict[str, str] = {}
    for acc in cfg.accounts:
        if acc.platform not in platform_accounts:
            platform_accounts[acc.platform] = acc.username

    query = display_name.strip()
    if not query:
        conn.close()
        return []

    all_results: list[dict] = []

    for target_platform in _PLATFORMS:
        if target_platform == source_platform:
            continue
        account_username = platform_accounts.get(target_platform)
        if not account_username:
            continue

        async def _search_fn(page, runtime_cfg, _tp=target_platform):
            page = await _ensure_logged_in(page, _tp, account_username, runtime_cfg)
            return await search_platform(page, _tp, query)

        try:
            results = await _run_with_session(
                target_platform,
                account_username,
                _search_fn,
                cfg,
                conn,
                proxy_mgr,
            )
            all_results.extend(results or [])
        except Exception:
            pass  # Don't block if one platform fails

    conn.close()
    return all_results


def _urlencode(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")
