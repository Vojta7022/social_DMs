from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.db import get_latest_avatar_sha256, init_db, insert_avatar_record, upsert_target


class ProfileAssetHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_dir.cleanup)
        self.db_path = Path(self._tmp_dir.name) / "social_scanner.db"
        self.conn = init_db(self.db_path)
        self.addCleanup(self.conn.close)

    def test_upsert_target_persists_cover_photo_url(self) -> None:
        target_id = upsert_target(
            self.conn,
            "facebook",
            "alice.profile",
            display_name="Alice",
            cover_photo_url="https://example.com/cover.jpg",
        )
        row = self.conn.execute("SELECT cover_photo_url FROM targets WHERE id=?", (target_id,)).fetchone()
        self.assertEqual(row["cover_photo_url"], "https://example.com/cover.jpg")

    def test_asset_history_tracks_avatar_and_cover_photo_independently(self) -> None:
        target_id = upsert_target(self.conn, "facebook", "alice.profile")
        insert_avatar_record(
            self.conn,
            target_id,
            "/tmp/avatar.jpg",
            "avatar-sha",
            "2026-04-02T10:00:00Z",
            asset_kind="avatar",
        )
        insert_avatar_record(
            self.conn,
            target_id,
            "/tmp/cover.jpg",
            "cover-sha",
            "2026-04-02T11:00:00Z",
            asset_kind="cover_photo",
        )

        self.assertEqual(
            get_latest_avatar_sha256(self.conn, target_id, asset_kind="avatar"),
            "avatar-sha",
        )
        self.assertEqual(
            get_latest_avatar_sha256(self.conn, target_id, asset_kind="cover_photo"),
            "cover-sha",
        )


if __name__ == "__main__":
    unittest.main()
