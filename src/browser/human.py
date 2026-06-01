"""
browser/human.py — Human-like interaction helpers for Playwright pages.

All delays are driven by StealthConfig so values can be tuned in settings.yaml
without touching code.
"""

from __future__ import annotations

import asyncio
import math
import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page

    from src.config import StealthConfig


# ─── Delay ───────────────────────────────────────────────────────────────────

async def random_delay(cfg: "StealthConfig") -> None:
    """Sleep for a Gaussian-jittered duration clamped to [delay_min_ms, delay_max_ms]."""
    ms = random.gauss(cfg.delay_mean_ms, cfg.delay_std_ms)
    ms = max(cfg.delay_min_ms, min(cfg.delay_max_ms, ms))
    await asyncio.sleep(ms / 1000.0)


async def short_delay(cfg: "StealthConfig") -> None:
    """A quick pause — roughly half the normal delay, used between micro-actions."""
    ms = random.gauss(cfg.delay_mean_ms * 0.4, cfg.delay_std_ms * 0.3)
    ms = max(200, min(cfg.delay_mean_ms, ms))
    await asyncio.sleep(ms / 1000.0)


# ─── Typing ──────────────────────────────────────────────────────────────────

async def human_type(page: "Page", selector: str, text: str,
                     cfg: "StealthConfig") -> None:
    """
    Type text into an element with per-character delays derived from typing_wpm.
    Clicks the element first to ensure focus.
    """
    await human_click(page, selector, cfg)
    # Average ms per character at given WPM (5 chars/word average)
    avg_char_ms = 60_000 / (cfg.typing_wpm * 5)
    for char in text:
        await page.type(selector, char, delay=0)
        jitter = random.gauss(avg_char_ms, avg_char_ms * 0.3)
        jitter = max(30, min(300, jitter))
        await asyncio.sleep(jitter / 1000.0)


# ─── Mouse movement ──────────────────────────────────────────────────────────

def bezier_mouse_path(
    start: tuple[float, float],
    end: tuple[float, float],
    steps: int,
) -> list[tuple[float, float]]:
    """
    Generate a Bézier curve path between two points.
    Two random control points introduce natural curvature.
    """
    x0, y0 = start
    x3, y3 = end

    # Random control points offset from the straight-line midpoint
    mid_x = (x0 + x3) / 2
    mid_y = (y0 + y3) / 2
    spread = math.dist(start, end) * 0.3
    x1 = mid_x + random.uniform(-spread, spread)
    y1 = mid_y + random.uniform(-spread, spread)
    x2 = mid_x + random.uniform(-spread, spread)
    y2 = mid_y + random.uniform(-spread, spread)

    path = []
    for i in range(steps + 1):
        t = i / steps
        mt = 1 - t
        # Cubic Bézier formula
        x = mt**3 * x0 + 3 * mt**2 * t * x1 + 3 * mt * t**2 * x2 + t**3 * x3
        y = mt**3 * y0 + 3 * mt**2 * t * y1 + 3 * mt * t**2 * y2 + t**3 * y3
        path.append((x, y))
    return path


async def human_click(page: "Page", selector: str, cfg: "StealthConfig") -> None:
    """
    Move the mouse along a Bézier curve to the element's centre, then click.
    Falls back to a direct click if the element can't be located for movement.
    """
    try:
        element = await page.query_selector(selector)
        if element is None:
            await page.click(selector)
            return

        box = await element.bounding_box()
        if box is None:
            await page.click(selector)
            return

        target_x = box["x"] + box["width"] / 2 + random.uniform(-5, 5)
        target_y = box["y"] + box["height"] / 2 + random.uniform(-3, 3)

        # Get current mouse position (approximate — start from a random edge)
        start_x = random.uniform(100, page.viewport_size["width"] - 100) if page.viewport_size else 400
        start_y = random.uniform(100, page.viewport_size["height"] - 100) if page.viewport_size else 300

        path = bezier_mouse_path(
            (start_x, start_y), (target_x, target_y), cfg.mouse_curve_steps
        )

        for x, y in path:
            await page.mouse.move(x, y)
            await asyncio.sleep(random.uniform(0.005, 0.020))

        await asyncio.sleep(random.uniform(0.05, 0.15))
        await page.mouse.click(target_x, target_y)

    except Exception:
        # Fallback — direct Playwright click
        await page.click(selector)


# ─── Scrolling ───────────────────────────────────────────────────────────────

async def human_scroll(
    page: "Page",
    direction: str,
    cfg: "StealthConfig",
    total_px: int = 800,
) -> None:
    """
    Scroll the page in cfg.scroll_steps incremental steps with jitter between each.

    direction: 'down' | 'up'
    total_px: total pixels to scroll (approximate)
    """
    sign = -1 if direction == "up" else 1
    step_px = total_px / cfg.scroll_steps

    for _ in range(cfg.scroll_steps):
        jitter_px = step_px + random.uniform(-step_px * 0.3, step_px * 0.3)
        await page.mouse.wheel(0, sign * jitter_px)
        pause_ms = random.gauss(120, 40)
        await asyncio.sleep(max(40, pause_ms) / 1000.0)

    await short_delay(cfg)


# ─── Viewport jitter ─────────────────────────────────────────────────────────

async def random_viewport_jitter(page: "Page") -> None:
    """Slightly resize viewport by ±50px to vary fingerprint."""
    vp = page.viewport_size or {"width": 1366, "height": 768}
    w = vp["width"] + random.randint(-50, 50)
    h = vp["height"] + random.randint(-30, 30)
    w = max(1024, w)
    h = max(600, h)
    await page.set_viewport_size({"width": w, "height": h})
