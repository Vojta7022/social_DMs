from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, patch

from src.scrapers.instagram import scrape_dms


class _FakeRequest:
    def __init__(self, post_data: str) -> None:
        self.resource_type = "fetch"
        self.post_data = post_data


class _FakeResponse:
    def __init__(self, post_data: str, payload: dict) -> None:
        self.url = "https://www.instagram.com/api/graphql"
        self.request = _FakeRequest(post_data)
        self._payload = payload

    async def text(self) -> str:
        return json.dumps(self._payload)


class _FakePage:
    def __init__(self) -> None:
        self._handlers: dict[str, object] = {}

    def on(self, event: str, handler) -> None:
        self._handlers[event] = handler

    async def goto(self, *_args, **_kwargs) -> None:
        handler = self._handlers.get("response")
        if not handler:
            return
        response = _FakeResponse(
            "fb_api_req_friendly_name=PolarisDirectInboxQuery&doc_id=123",
            {
                "data": {
                    "get_slide_mailbox_for_iris_subscription": {
                        "threads_by_folder": {
                            "edges": [
                                {
                                    "node": {
                                        "as_ig_direct_thread": {
                                            "thread_key": "thread-1",
                                            "thread_title": "Example thread",
                                            "thread_subtype": "IG_ONLY_ONE_TO_ONE",
                                            "users": [{"username": "example.user", "full_name": "Example User"}],
                                        }
                                    }
                                }
                            ],
                            "page_info": {"has_next_page": True},
                        }
                    }
                }
            },
        )
        await handler(response)

    async def wait_for_timeout(self, _timeout_ms: int) -> None:
        return None


class _FakeCfg:
    class _Stealth:
        delay_mean_ms = 1
        delay_std_ms = 1
        delay_min_ms = 1
        delay_max_ms = 2

    stealth = _Stealth()


class InstagramDmScrapeTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_mailbox_with_zero_resolved_threads_raises(self) -> None:
        page = _FakePage()

        with patch("src.scrapers.instagram.random_delay", new=AsyncMock()), \
            patch("src.scrapers.instagram.assert_no_captcha", new=AsyncMock()), \
            patch("src.scrapers.instagram._instagram_collect_visible_dm_candidates", new=AsyncMock(return_value=[])), \
            patch("src.scrapers.instagram._instagram_fetch_thread", new=AsyncMock(return_value=None)), \
            patch("src.scrapers.instagram.check_login_state", new=AsyncMock(return_value=True)):
            with self.assertRaisesRegex(RuntimeError, "could not fetch any thread details"):
                await scrape_dms(page, "vojtechponrt", None, _FakeCfg())


if __name__ == "__main__":
    unittest.main()
