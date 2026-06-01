from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.browser.stealth import session_path_for
from src.main import _normalise_post_media_items, _post_media_session_path
from src.media_downloader import (
    _load_cookies_from_storage_state,
    _looks_like_direct_video_asset,
)


class TikTokMediaDownloadTests(unittest.TestCase):
    def test_tiktok_cdn_url_is_treated_as_direct_video_asset(self) -> None:
        url = (
            "https://v16-webapp-prime.tiktok.com/video/tos/no1a/tos-no1a-ve-0068-no/example/"
            "?mime_type=video_mp4&policy=2&tk=tt_chain_token"
        )
        self.assertTrue(_looks_like_direct_video_asset(url))

    def test_tiktok_permalink_is_not_treated_as_direct_video_asset(self) -> None:
        self.assertFalse(
            _looks_like_direct_video_asset("https://www.tiktok.com/@target_user/video/1234567890")
        )

    def test_load_cookies_from_storage_state_reads_requests_cookie_jar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            session_path = Path(tmp_dir) / "tiktok_login.json"
            session_path.write_text(
                json.dumps(
                    {
                        "cookies": [
                            {
                                "name": "sessionid",
                                "value": "abc123",
                                "domain": ".tiktok.com",
                                "path": "/",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            jar = _load_cookies_from_storage_state(session_path)

        self.assertIsNotNone(jar)
        assert jar is not None
        self.assertEqual(jar.get("sessionid", domain=".tiktok.com", path="/"), "abc123")

    def test_normalise_post_media_items_adds_tiktok_raw_metadata_fallback_urls(self) -> None:
        items = _normalise_post_media_items(
            {
                "platform": "tiktok",
                "post_type": "video",
                "url": "https://www.tiktok.com/@target_user/video/123",
                "media_items": [
                    {
                        "type": "video",
                        "url": "https://webapp-no1a.tiktok.com/video/main.mp4",
                        "source_url": "https://www.tiktok.com/@target_user/video/123",
                    }
                ],
                "raw_metadata": {
                    "detail_item_payload": {
                        "video": {
                            "PlayAddrStruct": {
                                "UrlList": [
                                    "https://v16-webapp-prime.tiktok.com/video/play-1.mp4",
                                    "https://v19-webapp-prime.tiktok.com/video/play-1.mp4",
                                ]
                            },
                            "bitrateInfo": [
                                {
                                    "PlayAddr": {
                                        "UrlList": [
                                            "https://www.tiktok.com/aweme/v1/play/?item_id=123",
                                        ]
                                    }
                                }
                            ],
                        }
                    }
                },
            }
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["url"], "https://webapp-no1a.tiktok.com/video/main.mp4")
        self.assertEqual(
            items[0]["fallback_urls"],
            [
                "https://v16-webapp-prime.tiktok.com/video/play-1.mp4",
                "https://v19-webapp-prime.tiktok.com/video/play-1.mp4",
                "https://www.tiktok.com/aweme/v1/play/?item_id=123",
            ],
        )


class PostMediaSessionPathTests(unittest.TestCase):
    def test_post_media_session_path_uses_target_account_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sessions_dir = Path(tmp_dir) / "sessions"
            sessions_dir.mkdir(parents=True, exist_ok=True)
            session_path = session_path_for(sessions_dir, "tiktok", "login_user")
            session_path.write_text("{}", encoding="utf-8")

            cfg = SimpleNamespace(sessions_dir=sessions_dir)
            target_cfg = SimpleNamespace(platform="tiktok", username="a.vorlovaa", using_account="login_user")

            with patch("src.main._configured_targets", return_value=[target_cfg]):
                resolved = _post_media_session_path(
                    cfg,
                    {
                        "platform": "tiktok",
                        "target_username": "a.vorlovaa",
                    },
                )

        self.assertEqual(resolved, session_path)


if __name__ == "__main__":
    unittest.main()
