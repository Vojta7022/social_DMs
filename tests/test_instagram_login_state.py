from __future__ import annotations

import unittest

from src.scrapers.instagram import check_login_state


class _FakeContext:
    def __init__(self, cookies: list[dict]) -> None:
        self._cookies = cookies

    async def cookies(self, *_args, **_kwargs):
        return list(self._cookies)


class _FakePage:
    def __init__(
        self,
        *,
        cookies: list[dict] | None = None,
        goto_error: Exception | None = None,
        url: str = "https://www.instagram.com/",
        selectors: dict[str, bool] | None = None,
    ) -> None:
        self.context = _FakeContext(cookies or [])
        self._goto_error = goto_error
        self.url = url
        self._selectors = selectors or {}

    async def goto(self, *_args, **_kwargs):
        if self._goto_error:
            raise self._goto_error

    async def wait_for_timeout(self, _timeout_ms: int):
        return None

    async def query_selector(self, selector: str):
        return object() if self._selectors.get(selector) else None


class InstagramLoginStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_with_session_cookie_is_treated_as_logged_in(self) -> None:
        page = _FakePage(
            cookies=[{"name": "sessionid", "value": "abc123"}],
            goto_error=RuntimeError("Page.goto: Timeout 30000ms exceeded."),
        )
        self.assertTrue(await check_login_state(page))

    async def test_login_form_still_wins_even_with_cookie(self) -> None:
        page = _FakePage(
            cookies=[{"name": "sessionid", "value": "abc123"}],
            selectors={"input[name='username']": True},
        )
        self.assertFalse(await check_login_state(page))


if __name__ == "__main__":
    unittest.main()
