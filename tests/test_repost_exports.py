from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


if "loguru" not in sys.modules:
    module = types.ModuleType("loguru")

    class _Logger:
        def __getattr__(self, name):
            return lambda *args, **kwargs: None

    module.logger = _Logger()
    sys.modules["loguru"] = module


from src.db import init_db, insert_repost_history, upsert_target
from src.profile_builder import build_profile


class FakeAnalyzer:
    def polarity_scores(self, text: str) -> dict[str, float]:
        lowered = text.lower()
        if "love" in lowered or "funny" in lowered:
            return {"compound": 0.7, "pos": 0.7, "neu": 0.3, "neg": 0.0}
        return {"compound": 0.0, "pos": 0.0, "neu": 1.0, "neg": 0.0}


class RepostExportTests(unittest.TestCase):
    def test_profile_builder_exports_metadata_only_reposts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            conn = init_db(base / "archive.sqlite")
            target_id = upsert_target(
                conn,
                "instagram",
                "demo_user",
                display_name="Demo User",
                bio_snapshot="Bio text",
                follower_count=100,
                following_count=50,
                post_count=3,
            )
            insert_repost_history(
                conn,
                target_id,
                {
                    "platform": "instagram",
                    "repost_id": "R1",
                    "source_platform": "instagram",
                    "source_username": "meme_page",
                    "source_display_name": "Meme Page",
                    "source_url": "https://www.instagram.com/meme_page/p/R1/",
                    "repost_url": "https://www.instagram.com/demo_user/reposts/R1/",
                    "original_caption": "love this funny meme",
                    "original_posted_at": "2026-03-01T10:00:00Z",
                    "discovered_at": "2026-03-30T10:00:00Z",
                },
            )

            with patch("src.intelligence._get_vader_analyzer", return_value=FakeAnalyzer()):
                profile_path = build_profile(conn, target_id, base / "profiles")

            self.assertIsNotNone(profile_path)
            bundle_dir = profile_path.parent
            profile = json.loads((bundle_dir / "profile.json").read_text())
            reposts = json.loads((bundle_dir / "reposts.json").read_text())
            intelligence = json.loads((bundle_dir / "intelligence.json").read_text())

            self.assertEqual(profile["archive_counts"]["reposts"], 1)
            self.assertEqual(intelligence["repost_analytics"]["total_reposts"], 1)

            item = reposts["items"][0]
            self.assertEqual(item["content_scope"], "reposts")
            self.assertEqual(item["media_paths"], [])
            self.assertEqual(item["behavior_category"], "humor")
            self.assertEqual(item["repost_source_account"], "meme_page")

            conn.close()


if __name__ == "__main__":
    unittest.main()
