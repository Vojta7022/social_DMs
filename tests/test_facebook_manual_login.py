from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from src.main import _facebook_login_flow_stalled, _facebook_nudge_stalled_login, _manual_login_complete


class _FakeLocator:
    def __init__(self, text: str) -> None:
        self._text = text

    async def inner_text(self) -> str:
        return self._text


class _FakePage:
    def __init__(self, url: str = "https://www.facebook.com/", body_text: str = "") -> None:
        self.url = url
        self._body_text = body_text
        self.closed = False
        self.goto_calls: list[str] = []

    def locator(self, selector: str):
        if selector != "body":
            raise AssertionError(f"Unexpected locator request: {selector}")
        return _FakeLocator(self._body_text)

    async def close(self) -> None:
        self.closed = True

    async def goto(self, url: str, **_kwargs) -> None:
        self.goto_calls.append(url)
        self.url = url


class _FakeContext:
    def __init__(self, cookies: list[dict], pages: list[_FakePage] | None = None) -> None:
        self._cookies = cookies
        self.pages = pages or []
        self._pages_created = 0

    async def cookies(self):
        return list(self._cookies)

    async def new_page(self):
        self._pages_created += 1
        return _FakePage()


class FacebookManualLoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_auth_cookies_still_require_logged_in_probe(self) -> None:
        context = _FakeContext(
            [
                {"name": "c_user", "value": "123"},
                {"name": "xs", "value": "abc"},
            ]
        )
        with patch("src.scrapers.facebook.check_login_state", new=AsyncMock(return_value=False)):
            self.assertFalse(await _manual_login_complete(context, "facebook"))
        self.assertEqual(context._pages_created, 1)

    async def test_auth_cookies_and_probe_complete_manual_login(self) -> None:
        context = _FakeContext(
            [
                {"name": "c_user", "value": "123"},
                {"name": "xs", "value": "abc"},
            ]
        )
        with patch("src.scrapers.facebook.check_login_state", new=AsyncMock(return_value=True)):
            self.assertTrue(await _manual_login_complete(context, "facebook"))
        self.assertEqual(context._pages_created, 1)

    async def test_blank_two_step_page_is_treated_as_stalled(self) -> None:
        context = _FakeContext(
            [],
            pages=[
                _FakePage(
                    url="https://www.facebook.com/two_step_verification/authentication/?foo=bar",
                    body_text="Facebook\nfrom Meta",
                )
            ],
        )
        self.assertTrue(await _facebook_login_flow_stalled(context))

    async def test_login_page_spinner_is_treated_as_stalled(self) -> None:
        context = _FakeContext(
            [],
            pages=[
                _FakePage(
                    url="https://www.facebook.com/login",
                    body_text="Log in to Facebook\nForgotten password?",
                )
            ],
        )
        self.assertTrue(await _facebook_login_flow_stalled(context))

    async def test_normal_facebook_page_is_not_treated_as_stalled(self) -> None:
        context = _FakeContext(
            [],
            pages=[
                _FakePage(
                    url="https://www.facebook.com/",
                    body_text="Home Feed Messenger Friends",
                )
            ],
        )
        self.assertFalse(await _facebook_login_flow_stalled(context))

    async def test_stalled_login_page_is_nudged_to_home(self) -> None:
        page = _FakePage(
            url="https://www.facebook.com/login",
            body_text="Log in to Facebook\nForgotten password?",
        )
        context = _FakeContext([], pages=[page])
        await _facebook_nudge_stalled_login(context)
        self.assertEqual(page.goto_calls, ["https://www.facebook.com/"])


if __name__ == "__main__":
    unittest.main()
