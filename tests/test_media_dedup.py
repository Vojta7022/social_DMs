from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys
import types

if "loguru" not in sys.modules:
    module = types.ModuleType("loguru")

    class _Logger:
        def __getattr__(self, name):
            return lambda *args, **kwargs: None

    module.logger = _Logger()
    sys.modules["loguru"] = module

from src.db import get_story_media_records, init_db, insert_story, upsert_target
from src.media_downloader import deduplicate_or_save


class MediaDedupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "test.db"
        self.conn = init_db(self.db_path)
        self.target_id = upsert_target(self.conn, "instagram", "alice.test")

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def test_identical_story_media_is_saved_for_multiple_items(self) -> None:
        story_one_id = insert_story(
            self.conn,
            self.target_id,
            {
                "platform": "instagram",
                "story_id": "story-one",
                "story_type": "video",
                "content_scope": "highlight",
                "url": "https://example.com/story-one.mp4",
                "metadata": {},
            },
        )
        story_two_id = insert_story(
            self.conn,
            self.target_id,
            {
                "platform": "instagram",
                "story_id": "story-two",
                "story_type": "video",
                "content_scope": "highlight",
                "url": "https://example.com/story-two.mp4",
                "metadata": {},
            },
        )
        assert story_one_id is not None
        assert story_two_id is not None

        first_path = self.root / "story-one.mp4"
        second_path = self.root / "story-two.mp4"
        payload = b"same-media-bytes"
        first_path.write_bytes(payload)
        second_path.write_bytes(payload)

        self.assertTrue(deduplicate_or_save(self.conn, first_path, story_id=story_one_id, file_type="video"))
        self.assertTrue(deduplicate_or_save(self.conn, second_path, story_id=story_two_id, file_type="video"))
        self.assertEqual(len(get_story_media_records(self.conn, story_one_id)), 1)
        self.assertEqual(len(get_story_media_records(self.conn, story_two_id)), 1)

    def test_identical_story_media_is_not_duplicated_within_same_item(self) -> None:
        story_id = insert_story(
            self.conn,
            self.target_id,
            {
                "platform": "instagram",
                "story_id": "story-one",
                "story_type": "video",
                "content_scope": "highlight",
                "url": "https://example.com/story-one.mp4",
                "metadata": {},
            },
        )
        assert story_id is not None

        first_path = self.root / "story-one.mp4"
        second_path = self.root / "story-one-copy.mp4"
        payload = b"same-media-bytes"
        first_path.write_bytes(payload)
        second_path.write_bytes(payload)

        self.assertTrue(deduplicate_or_save(self.conn, first_path, story_id=story_id, file_type="video"))
        self.assertFalse(deduplicate_or_save(self.conn, second_path, story_id=story_id, file_type="video"))
        self.assertEqual(len(get_story_media_records(self.conn, story_id)), 1)

    def test_zero_byte_media_is_rejected_and_removed(self) -> None:
        story_id = insert_story(
            self.conn,
            self.target_id,
            {
                "platform": "instagram",
                "story_id": "story-empty",
                "story_type": "video",
                "content_scope": "highlight",
                "url": "https://example.com/story-empty.mp4",
                "metadata": {},
            },
        )
        assert story_id is not None

        empty_path = self.root / "empty.mp4"
        empty_path.write_bytes(b"")

        self.assertFalse(deduplicate_or_save(self.conn, empty_path, story_id=story_id, file_type="video"))
        self.assertFalse(empty_path.exists())
        self.assertEqual(len(get_story_media_records(self.conn, story_id)), 0)


if __name__ == "__main__":
    unittest.main()
