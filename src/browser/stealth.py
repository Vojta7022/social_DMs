"""
browser/stealth.py — Playwright browser launch with stealth patches and session persistence.

Each account gets its own BrowserContext with saved cookies + localStorage so
logins survive between runs. Contexts are never shared across jobs.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
from playwright_stealth import stealth_async


# ─── Login / CAPTCHA detection patterns ──────────────────────────────────────

_LOGOUT_URL_FRAGMENTS = {
    "instagram": ["/accounts/login", "/accounts/emailsignup"],
    "tiktok": ["/login", "/signup"],
    "facebook": ["/login", "/checkpoint"],
}

_CAPTCHA_URL_FRAGMENTS = [
    "captcha", "challenge", "checkpoint", "verify", "unusual_traffic",
    "recaptcha", "hcaptcha", "arkose",
]

_CAPTCHA_SELECTORS = [
    "iframe[src*='captcha']",
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    "#captcha",
    "[data-testid='challenge']",
]

_AUTH_COOKIE_NAMES = {
    "instagram": {"sessionid"},
    "facebook": {"c_user", "xs"},
    "tiktok": {"sessionid", "tt_chain_token"},
}


# ─── Exceptions ──────────────────────────────────────────────────────────────

class LogoutError(Exception):
    pass


class CaptchaError(Exception):
    pass


class BrowserProfileInUseError(RuntimeError):
    pass


# ─── Paths ───────────────────────────────────────────────────────────────────

def session_path_for(sessions_dir: Path, platform: str, username: str) -> Path:
    """Return canonical path: sessions/{platform}_{username}.json"""
    safe = username.replace("/", "_").replace("\\", "_")
    return sessions_dir / f"{platform}_{safe}.json"


def profile_dir_for(
    sessions_dir: Path,
    platform: str,
    username: str,
    profile_suffix: str | None = None,
) -> Path:
    """Return canonical persistent Chromium profile dir for one account."""
    safe = username.replace("/", "_").replace("\\", "_")
    base_name = f"{platform}_{safe}"
    if not profile_suffix:
        return sessions_dir.parent / "browser_profiles" / base_name

    suffix = re.sub(r"[^A-Za-z0-9._-]+", "_", profile_suffix.strip())
    if not suffix:
        return sessions_dir.parent / "browser_profiles" / base_name
    return sessions_dir.parent / "browser_profiles" / f"{base_name}__{suffix}"


def _profile_lock_path(profile_dir: Path) -> Path:
    return profile_dir / ".social_scanner.lock"


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _acquire_profile_lock(profile_dir: Path) -> Path:
    """Take an app-level lock for one persistent browser profile directory."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    lock_path = _profile_lock_path(profile_dir)

    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            pid = 0
            try:
                payload = json.loads(lock_path.read_text())
                pid = int(payload.get("pid") or 0)
            except Exception:
                pid = 0

            if pid and _pid_is_alive(pid):
                raise BrowserProfileInUseError(
                    f"Browser profile {profile_dir.name!r} is already in use by PID {pid}. "
                    "Use a different `--profile-suffix` for a parallel run."
                )

            lock_path.unlink(missing_ok=True)
            continue

        with os.fdopen(fd, "w") as handle:
            json.dump(
                {
                    "pid": os.getpid(),
                    "acquired_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
                handle,
            )
        return lock_path


def _release_profile_lock(lock_path: Path | None) -> None:
    if not lock_path:
        return
    try:
        lock_path.unlink(missing_ok=True)
    except Exception:
        pass


def _seed_isolated_profile_dir(base_profile_dir: Path, profile_dir: Path) -> None:
    """Clone the canonical browser profile into an isolated per-run profile dir."""
    if profile_dir.exists():
        return

    if not base_profile_dir.exists():
        profile_dir.mkdir(parents=True, exist_ok=True)
        return

    volatile_names = {
        ".social_scanner.lock",
        "DevToolsActivePort",
        "lockfile",
        "LOCK",
        "Cache",
        "Code Cache",
        "GPUCache",
        "DawnCache",
        "GrShaderCache",
        "ShaderCache",
        "blob_storage",
        "Crashpad",
        "CrashpadMetrics-active.pma",
    }

    def _ignore_volatile(_dir: str, names: list[str]) -> set[str]:
        ignored: set[str] = set()
        for name in names:
            if name in volatile_names:
                ignored.add(name)
                continue
            if name.startswith("Singleton") or name.startswith("Crashpad"):
                ignored.add(name)
        return ignored

    def _copy_profile_file(src: str, dst: str) -> str:
        try:
            return shutil.copy2(src, dst)
        except FileNotFoundError:
            # Chromium cache entries can disappear while the source profile is idle.
            return dst

    shutil.copytree(
        base_profile_dir,
        profile_dir,
        ignore=_ignore_volatile,
        copy_function=_copy_profile_file,
    )


# ─── Session persistence ─────────────────────────────────────────────────────

def _storage_state_has_auth_cookie(storage_state: dict, platform: str) -> bool:
    """Return True when a Playwright storage-state dict contains auth cookies."""
    required = _AUTH_COOKIE_NAMES.get(platform, set())
    if not required:
        return False

    cookie_names = {
        cookie.get("name")
        for cookie in storage_state.get("cookies", [])
        if isinstance(cookie, dict)
    }
    if platform == "tiktok":
        return bool(cookie_names & required)
    return required.issubset(cookie_names)


async def context_has_auth_cookie(context: BrowserContext, platform: str) -> bool:
    """Return True when the live browser context currently has auth cookies."""
    cookies = await context.cookies()
    return _storage_state_has_auth_cookie({"cookies": cookies}, platform)


async def save_session(
    context: BrowserContext,
    session_path: Path,
    *,
    platform: str | None = None,
    preserve_existing_if_missing_auth: bool = False,
) -> bool:
    """Serialize cookies + localStorage to a JSON session file."""
    session_path.parent.mkdir(parents=True, exist_ok=True)
    storage_state = await context.storage_state()

    if (
        platform
        and preserve_existing_if_missing_auth
        and session_path.exists()
        and not _storage_state_has_auth_cookie(storage_state, platform)
    ):
        try:
            existing_state = json.loads(session_path.read_text())
        except Exception:
            existing_state = {}
        if _storage_state_has_auth_cookie(existing_state, platform):
            return False

    with open(session_path, "w") as f:
        json.dump(storage_state, f)
    return True


async def load_session(context: BrowserContext, session_path: Path) -> bool:
    """
    Restore cookies + localStorage from a session file.
    Returns True if the file existed and was loaded, False otherwise.
    """
    if not session_path.exists():
        return False
    try:
        with open(session_path) as f:
            state = json.load(f)
        await context.add_cookies(state.get("cookies", []))
        # localStorage is restored via storage_state on context creation (see launch_browser)
        return True
    except Exception:
        return False


# ─── Browser launch ──────────────────────────────────────────────────────────

async def launch_browser(
    proxy_url: str | None = None,
    headless: bool = True,
    session_path: Path | None = None,
    profile_dir: Path | None = None,
    profile_seed_dir: Path | None = None,
) -> tuple[Playwright, Browser, BrowserContext, Path | None]:
    """
    Launch a stealth Chromium context.

    If session_path is provided and the file exists, the context is initialised
    with that storage state (cookies + localStorage) so we start already logged in.

    Returns (playwright, browser, context). The caller is responsible for calling
    close_browser() when done.
    """
    playwright = await async_playwright().start()

    launch_opts = _build_launch_options(headless=headless, proxy_url=proxy_url)
    context_opts = _build_context_options(headless=headless)

    browser: Browser
    profile_lock_path: Path | None = None
    if profile_dir:
        if profile_seed_dir and profile_seed_dir != profile_dir:
            _seed_isolated_profile_dir(profile_seed_dir, profile_dir)
        else:
            profile_dir.mkdir(parents=True, exist_ok=True)

        profile_lock_path = _acquire_profile_lock(profile_dir)
        try:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                **launch_opts,
                **context_opts,
            )
        except Exception:
            _release_profile_lock(profile_lock_path)
            raise
        browser = context.browser
    else:
        browser = await playwright.chromium.launch(**launch_opts)

        # Load session state if a session file already exists
        if session_path and session_path.exists():
            try:
                context_opts["storage_state"] = str(session_path)
            except Exception:
                pass  # corrupted session — start fresh

        context = await browser.new_context(**context_opts)

    if session_path and session_path.exists():
        try:
            await load_session(context, session_path)
        except Exception:
            pass

    if headless:
        # Playwright-stealth remains useful for unattended scraping, but the
        # headed manual-login window should stay as close to stock Chrome as
        # possible to avoid tripping Meta's login flow.
        context.on("page", lambda page: _apply_stealth_on_page(page))

        existing_pages = list(context.pages)
        if existing_pages:
            for page in existing_pages:
                await _run_stealth_safely(page)
        else:
            page = await context.new_page()
            await _run_stealth_safely(page)
            await page.close()

    return playwright, browser, context, profile_lock_path


def _build_launch_options(*, headless: bool, proxy_url: str | None) -> dict:
    """Return browser launch options tuned for either headless scraping or manual login."""
    args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--disable-infobars",
        "--disable-extensions",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    launch_opts: dict = {
        "headless": headless,
        "args": args,
    }

    if proxy_url:
        launch_opts["proxy"] = {"server": proxy_url}

    if not headless:
        # Headed login should stay as close to a normal Chrome session as possible.
        # Meta is far more sensitive to Playwright's automation defaults here than
        # it is during normal headless scraping with an already-saved session.
        launch_opts["channel"] = "chrome"
        launch_opts["ignore_default_args"] = ["--enable-automation"]
        launch_opts["args"] = [
            "--no-first-run",
            "--no-default-browser-check",
        ]

    return launch_opts


def _build_context_options(*, headless: bool) -> dict:
    """Return BrowserContext options with a more natural headed fingerprint."""
    context_opts: dict = {
        "viewport": {"width": 1366, "height": 768},
        "java_script_enabled": True,
        "accept_downloads": True,
    }

    if headless:
        context_opts.update(
            {
                "user_agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                "locale": "en-US",
                "timezone_id": "America/New_York",
            }
        )

    return context_opts


def _apply_stealth_on_page(page: Page) -> None:
    """Callback registered on context to patch every newly opened page."""
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_run_stealth_safely(page))
    except Exception:
        pass


async def apply_stealth(page: Page) -> None:
    """Explicitly apply stealth patches to a page. Call after page.goto() if needed."""
    await _run_stealth_safely(page)


async def _run_stealth_safely(page: Page) -> None:
    """Apply playwright-stealth while swallowing benign page-close races."""
    try:
        await stealth_async(page)
    except Exception as exc:
        if "Target page, context or browser has been closed" in str(exc):
            return
        logger.debug(f"[stealth] Failed to apply stealth patches: {exc}")


# ─── Viewport jitter ─────────────────────────────────────────────────────────

async def random_viewport_jitter(page: Page) -> None:
    """Slightly resize the viewport by a random amount to avoid fingerprinting."""
    import random
    w = 1366 + random.randint(-50, 50)
    h = 768 + random.randint(-30, 30)
    await page.set_viewport_size({"width": w, "height": h})


# ─── Detection helpers ───────────────────────────────────────────────────────

async def detect_logout(page: Page, platform: str) -> bool:
    """Return True if the current page looks like a login/signup page."""
    url = page.url.lower()
    for fragment in _LOGOUT_URL_FRAGMENTS.get(platform, []):
        if fragment in url:
            return True
    return False


async def detect_captcha(page: Page) -> bool:
    """Return True if a CAPTCHA or challenge page is detected."""
    url = page.url.lower()
    for fragment in _CAPTCHA_URL_FRAGMENTS:
        if fragment in url:
            return True
    for selector in _CAPTCHA_SELECTORS:
        try:
            element = await page.query_selector(selector)
            if element:
                return True
        except Exception:
            pass
    return False


async def assert_no_captcha(page: Page, platform: str) -> None:
    """Raise CaptchaError or LogoutError if detected. Call before any scraping action."""
    if await detect_captcha(page):
        raise CaptchaError(f"CAPTCHA detected on {platform} at {page.url}")
    if await detect_logout(page, platform):
        raise LogoutError(f"Logged out of {platform}, redirected to {page.url}")


# ─── Teardown ────────────────────────────────────────────────────────────────

async def close_browser(
    playwright: Playwright,
    browser: Browser,
    profile_lock_path: Path | None = None,
) -> None:
    """Gracefully close the browser and Playwright instance."""
    try:
        await browser.close()
    except Exception:
        pass
    try:
        await playwright.stop()
    except Exception:
        pass
    _release_profile_lock(profile_lock_path)
