from __future__ import annotations

import unittest

from src.scrapers.facebook import check_login_state


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
        url: str = "https://www.facebook.com/",
        selectors: dict[str, bool] | None = None,
    ) -> None:
        self.context = _FakeContext(cookies or [])
        self._goto_error = goto_error
        self.url = url
        self._selectors = selectors or {}

    async def goto(self, *_args, **_kwargs):
        if self._goto_error:
            raise self._goto_error

    async def query_selector(self, selector: str):
        return object() if self._selectors.get(selector) else None


class FacebookLoginStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_with_auth_cookies_is_treated_as_logged_in(self) -> None:
        page = _FakePage(
            cookies=[
                {"name": "c_user", "value": "123"},
                {"name": "xs", "value": "abc"},
            ],
            goto_error=RuntimeError("Page.goto: Timeout 30000ms exceeded."),
        )
        self.assertTrue(await check_login_state(page))

    async def test_login_form_still_wins_even_with_cookies(self) -> None:
        page = _FakePage(
            cookies=[
                {"name": "c_user", "value": "123"},
                {"name": "xs", "value": "abc"},
            ],
            selectors={"form[data-testid='royal_login_form']": True},
        )
        self.assertFalse(await check_login_state(page))


if __name__ == "__main__":
    unittest.main()
