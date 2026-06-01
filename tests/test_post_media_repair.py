from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.db import init_db, insert_media_file, insert_post, upsert_target
from src.main import (
    _post_archive_dir,
    _post_media_record_looks_low_resolution,
    _saved_media_file_is_usable,
    repair_missing_post_media,
)


class PostMediaRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_dir.cleanup)
        self.root = Path(self._tmp_dir.name)
        self.cfg = SimpleNamespace(
            data_dir=self.root,
            media=SimpleNamespace(max_file_size_mb=50, video_format="mp4", yt_dlp_extra_args=[]),
            sessions_dir=self.root / "sessions",
        )
        self.cfg.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.conn = init_db(self.root / "social_scanner.db")
        self.addCleanup(self.conn.close)
        self.target_id = upsert_target(self.conn, "tiktok", "target_user")

    def test_saved_media_file_is_usable_rejects_zero_byte_files(self) -> None:
        media_path = self.root / "video.mp4"
        media_path.write_bytes(b"")
        self.assertFalse(_saved_media_file_is_usable(str(media_path)))

        media_path.write_bytes(b"video-bytes")
        self.assertTrue(_saved_media_file_is_usable(str(media_path)))

    def test_post_media_record_looks_low_resolution_detects_small_photos(self) -> None:
        self.assertTrue(
            _post_media_record_looks_low_resolution(
                {"file_type": "photo", "width": 160, "height": 213}
            )
        )
        self.assertFalse(
            _post_media_record_looks_low_resolution(
                {"file_type": "photo", "width": 1080, "height": 1350}
            )
        )

    def test_repair_missing_post_media_repairs_zero_byte_primary_file(self) -> None:
        post = {
            "platform": "tiktok",
            "post_id": "post-1",
            "post_type": "video",
            "content_scope": "feed",
            "url": "https://www.tiktok.com/@target_user/video/post-1",
            "caption": "hello",
            "posted_at": "2026-04-03T00:00:00Z",
        }
        post_row_id = insert_post(self.conn, self.target_id, post)
        assert post_row_id is not None

        archive_post = {
            **post,
            "target_username": "target_user",
            "collection_name": None,
        }
        archive_dir = _post_archive_dir(self.cfg, archive_post)
        archive_dir.mkdir(parents=True, exist_ok=True)
        (archive_dir / "post.json").write_text(
            json.dumps(
                {
                    "media_items": [
                        {
                            "type": "video",
                            "url": "https://cdn.example/video.mp4",
                            "source_url": post["url"],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        zero_path = archive_dir / "video.mp4"
        zero_path.write_bytes(b"")
        insert_media_file(
            self.conn,
            str(zero_path),
            hashlib.sha256(b"").hexdigest(),
            post_id=post_row_id,
            file_type="video",
            file_size=0,
        )

        def fake_download(conn, repaired_post_row_id: int, repaired_post: dict, cfg) -> None:
            repaired_path = _post_archive_dir(cfg, repaired_post) / "video.mp4"
            payload = b"repaired-video"
            repaired_path.write_bytes(payload)
            insert_media_file(
                conn,
                str(repaired_path),
                hashlib.sha256(payload).hexdigest(),
                post_id=repaired_post_row_id,
                file_type="video",
                file_size=len(payload),
            )

        with patch("src.main._download_post_media", side_effect=fake_download), patch(
            "src.main._rebuild_profile_for_target"
        ) as rebuild_profile:
            repaired, failed = repair_missing_post_media(
                self.cfg,
                self.conn,
                selected_targets={("tiktok", "target_user")},
            )

        self.assertEqual(repaired, 1)
        self.assertEqual(failed, 0)
        rebuild_profile.assert_called_once_with(self.conn, self.cfg, "tiktok", "target_user")

    def test_repair_missing_post_media_repairs_low_resolution_photo(self) -> None:
        post = {
            "platform": "tiktok",
            "post_id": "post-2",
            "post_type": "photo",
            "content_scope": "feed",
            "url": "https://www.tiktok.com/@target_user/video/post-2",
            "caption": "hello",
            "posted_at": "2026-04-03T00:00:00Z",
        }
        post_row_id = insert_post(self.conn, self.target_id, post)
        assert post_row_id is not None

        archive_post = {
            **post,
            "target_username": "target_user",
            "collection_name": None,
        }
        archive_dir = _post_archive_dir(self.cfg, archive_post)
        archive_dir.mkdir(parents=True, exist_ok=True)
        (archive_dir / "post.json").write_text(
            json.dumps(
                {
                    "media_items": [
                        {
                            "type": "photo",
                            "url": "https://cdn.example/photo-full.jpg",
                            "source_url": post["url"],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        low_res_path = archive_dir / "photo.jpg"
        low_res_payload = b"tiny-photo"
        low_res_path.write_bytes(low_res_payload)
        insert_media_file(
            self.conn,
            str(low_res_path),
            hashlib.sha256(low_res_payload).hexdigest(),
            post_id=post_row_id,
            file_type="photo",
            file_size=len(low_res_payload),
            width=160,
            height=213,
        )

        def fake_download(conn, repaired_post_row_id: int, repaired_post: dict, cfg) -> None:
            repaired_path = _post_archive_dir(cfg, repaired_post) / "photo.jpg"
            payload = b"large-photo"
            repaired_path.write_bytes(payload)
            insert_media_file(
                conn,
                str(repaired_path),
                hashlib.sha256(payload).hexdigest(),
                post_id=repaired_post_row_id,
                file_type="photo",
                file_size=len(payload),
                width=1080,
                height=1350,
            )

        with patch("src.main._download_post_media", side_effect=fake_download), patch(
            "src.main._rebuild_profile_for_target"
        ) as rebuild_profile:
            repaired, failed = repair_missing_post_media(
                self.cfg,
                self.conn,
                selected_targets={("tiktok", "target_user")},
            )

        self.assertEqual(repaired, 1)
        self.assertEqual(failed, 0)
        rebuild_profile.assert_called_once_with(self.conn, self.cfg, "tiktok", "target_user")


if __name__ == "__main__":
    unittest.main()
