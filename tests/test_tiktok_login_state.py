from __future__ import annotations

import unittest

from src.scrapers.tiktok import check_login_state


class _FakeContext:
    def __init__(self, cookies: list[dict]) -> None:
        self._cookies = cookies

    async def cookies(self, *_args, **_kwargs):
        return list(self._cookies)


class _FakeLocator:
    def __init__(self, text: str) -> None:
        self._text = text

    async def inner_text(self) -> str:
        return self._text


class _FakePage:
    def __init__(
        self,
        *,
        cookies: list[dict] | None = None,
        goto_error: Exception | None = None,
        url: str = "https://www.tiktok.com/foryou?lang=en",
        selectors: dict[str, bool] | None = None,
        body_text: str = "",
    ) -> None:
        self.context = _FakeContext(cookies or [])
        self._goto_error = goto_error
        self.url = url
        self._selectors = selectors or {}
        self._body_text = body_text

    async def goto(self, *_args, **_kwargs):
        if self._goto_error:
            raise self._goto_error

    async def query_selector(self, selector: str):
        return object() if self._selectors.get(selector) else None

    def locator(self, selector: str):
        if selector == "body":
            return _FakeLocator(self._body_text)
        raise AssertionError(f"Unexpected locator request: {selector}")


class TikTokLoginStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_with_auth_cookie_is_treated_as_logged_in(self) -> None:
        page = _FakePage(
            cookies=[{"name": "sessionid", "value": "abc123"}],
            goto_error=RuntimeError("Page.goto: Timeout 30000ms exceeded."),
        )
        self.assertTrue(await check_login_state(page))

    async def test_login_button_still_wins_even_with_cookie(self) -> None:
        page = _FakePage(
            cookies=[{"name": "sessionid", "value": "abc123"}],
            selectors={"a[href*='/login']": True},
        )
        self.assertFalse(await check_login_state(page))

    async def test_guest_tt_chain_token_is_not_treated_as_logged_in(self) -> None:
        page = _FakePage(
            cookies=[{"name": "tt_chain_token", "value": "guest-cookie"}],
            body_text="TikTok\nLog in to TikTok\nUse phone / email / username",
        )
        self.assertFalse(await check_login_state(page))


if __name__ == "__main__":
    unittest.main()
