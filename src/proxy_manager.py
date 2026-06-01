"""
proxy_manager.py — Proxy pool management with round-robin or random rotation.

Proxy failure state is persisted in the proxies DB table so bad proxies
remain disabled across container restarts. A Telegram alert is sent when
a proxy is automatically disabled due to repeated failures.
"""

from __future__ import annotations

import random
import sqlite3
from collections import deque
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from src.config import AccountConfig, ProxyConfig

MAX_FAILURES = 3  # disable proxy after this many consecutive failures


class ProxyManager:
    def __init__(
        self,
        cfg: "ProxyConfig",
        conn: sqlite3.Connection,
        bot=None,
        notification_chat_id: str = "",
        error_thread_id: int | None = None,
    ) -> None:
        """
        Load proxies from settings into the DB, then build the rotation deque.

        bot and notification_chat_id are optional; if provided, Telegram alerts are sent
        when a proxy is disabled.
        """
        self._conn = conn
        self._rotation = cfg.rotation
        self._bot = bot
        self._notification_chat_id = notification_chat_id
        self._error_thread_id = error_thread_id
        self._proxies: deque[str] = deque()
        self._configured_pool = self._sanitize_pool(cfg.pool)

        # Seed DB with proxies from settings (upsert so existing state is preserved)
        self._seed_from_config(list(self._configured_pool))
        self._reload_active()

    # ─── Public API ──────────────────────────────────────────────────────────

    def next_proxy(self) -> str | None:
        """
        Return the next proxy URL per the rotation strategy.
        Returns None if the pool is empty.
        """
        if not self._proxies:
            return None

        if self._rotation == "random":
            return random.choice(list(self._proxies))

        # round_robin: rotate the deque and return front
        self._proxies.rotate(-1)
        return self._proxies[0]

    def get_for_account(self, account: "AccountConfig") -> str | None:
        """
        Return the proxy for an account.
        Uses account.proxy if explicitly set; otherwise delegates to next_proxy().
        """
        if account.proxy:
            return account.proxy
        return self.next_proxy()

    def mark_failure(self, proxy_url: str) -> None:
        """
        Increment failure count for a proxy.
        Disables the proxy and sends a Telegram alert after MAX_FAILURES.
        """
        from src.db import disable_proxy, update_proxy_failure

        new_count = update_proxy_failure(self._conn, proxy_url)
        logger.warning(f"[proxy] Failure #{new_count} for {proxy_url}")

        if new_count >= MAX_FAILURES:
            disable_proxy(self._conn, proxy_url)
            logger.error(f"[proxy] Disabled proxy after {new_count} failures: {proxy_url}")
            try:
                self._proxies.remove(proxy_url)
            except ValueError:
                pass
            self._send_proxy_alert(proxy_url, new_count)

    def mark_success(self, proxy_url: str) -> None:
        """Reset failure count and update last_used_at for a healthy proxy."""
        from src.db import reset_proxy

        reset_proxy(self._conn, proxy_url)

    def reload(self) -> None:
        """Re-read active proxies from the DB (supports runtime proxy updates)."""
        self._reload_active()
        logger.info(f"[proxy] Reloaded pool — {len(self._proxies)} active proxies")

    # ─── Internal ────────────────────────────────────────────────────────────

    def _seed_from_config(self, pool: list[str]) -> None:
        """Insert proxies from settings into DB (ignore if already present)."""
        from src.db import upsert_proxy

        for url in pool:
            if url:
                upsert_proxy(self._conn, url)

    def _reload_active(self) -> None:
        """Rebuild in-memory deque from active DB proxies."""
        from src.db import get_active_proxies

        rows = get_active_proxies(self._conn)
        if self._configured_pool:
            rows = [r for r in rows if r["url"] in self._configured_pool]
        else:
            rows = []
        self._proxies = deque(r["url"] for r in rows)
        logger.debug(f"[proxy] Pool: {len(self._proxies)} active proxies")

    def _sanitize_pool(self, pool: list[str]) -> set[str]:
        """
        Drop blank/example placeholder proxies so sample config values are never used
        as live network settings.
        """
        cleaned: set[str] = set()
        for url in pool:
            if not url:
                continue
            normalized = url.strip()
            if not normalized:
                continue
            lower = normalized.lower()
            if "example.com" in lower or "proxy1.example.com" in lower or "proxy2.example.com" in lower:
                logger.warning(f"[proxy] Ignoring placeholder proxy from config: {normalized}")
                continue
            cleaned.add(normalized)
        return cleaned

    def _send_proxy_alert(self, proxy_url: str, failure_count: int) -> None:
        """Fire-and-forget Telegram alert when a proxy is disabled."""
        if not self._bot or not self._notification_chat_id:
            return
        import asyncio

        async def _alert():
            from src.telegram_bot import send_error

            await send_error(
                self._bot,
                self._notification_chat_id,
                "proxy_manager",
                Exception(
                    f"Proxy disabled after {failure_count} failures: {proxy_url}"
                ),
                message_thread_id=self._error_thread_id,
            )

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(_alert())
        except Exception:
            pass
