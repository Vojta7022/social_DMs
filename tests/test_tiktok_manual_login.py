from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from src.main import _manual_login_complete


class _FakePage:
    async def close(self) -> None:
        return None


class _FakeContext:
    def __init__(self, cookies: list[dict]) -> None:
        self._cookies = cookies
        self._pages_created = 0

    async def cookies(self):
        return list(self._cookies)

    async def new_page(self):
        self._pages_created += 1
        return _FakePage()


class TikTokManualLoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_guest_tt_chain_token_does_not_finish_manual_login(self) -> None:
        context = _FakeContext([{"name": "tt_chain_token", "value": "guest-cookie"}])
        self.assertFalse(await _manual_login_complete(context, "tiktok"))
        self.assertEqual(context._pages_created, 0)

    async def test_real_auth_cookie_still_requires_logged_in_probe(self) -> None:
        context = _FakeContext([{"name": "sessionid", "value": "real-cookie"}])
        with patch("src.scrapers.tiktok.check_login_state", new=AsyncMock(return_value=False)):
            self.assertFalse(await _manual_login_complete(context, "tiktok"))
        self.assertEqual(context._pages_created, 1)

    async def test_real_auth_cookie_and_probe_complete_manual_login(self) -> None:
        context = _FakeContext([{"name": "sid_tt", "value": "real-cookie"}])
        with patch("src.scrapers.tiktok.check_login_state", new=AsyncMock(return_value=True)):
            self.assertTrue(await _manual_login_complete(context, "tiktok"))
        self.assertEqual(context._pages_created, 1)


if __name__ == "__main__":
    unittest.main()
