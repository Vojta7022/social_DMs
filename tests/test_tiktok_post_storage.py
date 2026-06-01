from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.db import get_post_comments, init_db, insert_post, replace_post_comments, upsert_target


class TikTokPostStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_dir.cleanup)
        self.db_path = Path(self._tmp_dir.name) / "social_scanner.db"
        self.conn = init_db(self.db_path)
        self.addCleanup(self.conn.close)

        self.target_id = upsert_target(self.conn, "tiktok", "target_user")

    def test_insert_post_merges_raw_metadata_with_normalized_fields(self) -> None:
        post_row_id = insert_post(
            self.conn,
            self.target_id,
            {
                "platform": "tiktok",
                "post_id": "post-1",
                "post_type": "carousel",
                "content_scope": "feed",
                "url": "https://www.tiktok.com/@target_user/video/post-1",
                "caption": "hello",
                "comment_count": 11,
                "comments_snapshot_complete": False,
                "comments_unavailable_reason": "blocked by captcha",
                "raw_metadata": {
                    "detail_item_payload": {
                        "id": "post-1",
                        "imagePost": {"images": [{"imageURL": {"urlList": ["https://cdn.example/1.jpg"]}}]},
                    }
                },
            },
        )

        row = self.conn.execute(
            "SELECT raw_metadata_json FROM posts WHERE id=?",
            (post_row_id,),
        ).fetchone()
        payload = json.loads(row["raw_metadata_json"])
        self.assertEqual(payload["detail_item_payload"]["id"], "post-1")
        self.assertFalse(payload["comments_snapshot_complete"])
        self.assertEqual(payload["comments_unavailable_reason"], "blocked by captcha")
        self.assertEqual(payload["comment_count"], 11)

    def test_replace_post_comments_preserves_root_and_raw_payload_for_id_only_comment(self) -> None:
        post_row_id = insert_post(
            self.conn,
            self.target_id,
            {
                "platform": "tiktok",
                "post_id": "post-2",
                "post_type": "video",
                "content_scope": "feed",
                "url": "https://www.tiktok.com/@target_user/video/post-2",
            },
        )

        replace_post_comments(
            self.conn,
            post_row_id,
            "tiktok",
            [
                {
                    "comment_id": "comment-1",
                    "parent_comment_id": None,
                    "root_comment_id": "comment-1",
                    "reply_to_comment_id": None,
                    "username": "alice",
                    "user_id": "100",
                    "display_name": "Alice",
                    "text": "",
                    "commented_at": "2026-03-21T18:47:19Z",
                    "raw_payload": {"cid": "comment-1", "text": ""},
                }
            ],
        )

        rows = get_post_comments(self.conn, post_row_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["external_comment_id"], "comment-1")
        self.assertEqual(rows[0]["root_comment_id"], "comment-1")
        self.assertEqual(rows[0]["text"], "")
        self.assertEqual(json.loads(rows[0]["raw_payload_json"])["cid"], "comment-1")


if __name__ == "__main__":
    unittest.main()
