from __future__ import annotations

import unittest

from src.scrapers.facebook import (
    _facebook_media_size_hint,
    _extract_album_id,
    _extract_post_id,
    _is_probable_facebook_photo_asset_url,
    _is_probably_low_res_media_url,
    _is_static_facebook_asset,
    _merge_post_candidates,
    _prefer_media_candidates,
    _seed_post_from_permalink,
)


class FacebookPostHelperTests(unittest.TestCase):
    def test_extract_post_id_from_photo_fbid_url(self) -> None:
        self.assertEqual(
            _extract_post_id("https://www.facebook.com/photo/?fbid=1234567890&set=a.456"),
            "1234567890",
        )

    def test_extract_post_id_from_album_set_url_returns_none(self) -> None:
        self.assertIsNone(
            _extract_post_id("https://www.facebook.com/media/set/?set=a.4567890&type=3")
        )

    def test_extract_album_id_from_media_set_url(self) -> None:
        self.assertEqual(
            _extract_album_id("https://www.facebook.com/media/set/?set=a.4567890&type=3"),
            "4567890",
        )

    def test_seed_post_from_permalink_normalizes_relative_facebook_url(self) -> None:
        post = _seed_post_from_permalink(
            "/photo/?fbid=1234567890&set=a.4567890",
            collection_name="Weekend Trip | Facebook",
            source_surface="albums",
        )
        self.assertIsNotNone(post)
        assert post is not None
        self.assertEqual(post["post_id"], "1234567890")
        self.assertEqual(post["url"], "https://www.facebook.com/photo/?fbid=1234567890&set=a.4567890")
        self.assertEqual(post["collection_name"], "Weekend Trip")
        self.assertEqual(post["raw_metadata"]["source_surface"], "albums")

    def test_merge_post_candidates_backfills_collection_metadata(self) -> None:
        merged = _merge_post_candidates(
            [
                {
                    "post_id": "123",
                    "url": "https://www.facebook.com/photo/?fbid=123",
                    "caption": None,
                    "collection_name": None,
                    "raw_metadata": {"source_surface": "feed"},
                }
            ],
            [
                {
                    "post_id": "123",
                    "url": "https://www.facebook.com/photo/?fbid=123",
                    "caption": None,
                    "collection_name": "Album A",
                    "raw_metadata": {"collection_name_source": "Album A"},
                }
            ],
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["collection_name"], "Album A")
        self.assertEqual(merged[0]["raw_metadata"]["source_surface"], "feed")
        self.assertEqual(merged[0]["raw_metadata"]["collection_name_source"], "Album A")

    def test_low_res_media_url_is_detected(self) -> None:
        self.assertTrue(
            _is_probably_low_res_media_url(
                "https://scontent.example.fbcdn.net/v/t39.30808-6/1.jpg?stp=cp6_dst-jpg_p160x160_tt6"
            )
        )

    def test_media_size_hint_prefers_larger_tokens(self) -> None:
        self.assertEqual(
            _facebook_media_size_hint(
                "https://scontent.example.fbcdn.net/v/t39.30808-6/1.jpg?stp=dst-jpg_p1080x1350&_nc_cat=1"
            ),
            1350,
        )

    def test_static_facebook_asset_is_rejected(self) -> None:
        self.assertTrue(
            _is_static_facebook_asset("https://static.xx.fbcdn.net/rsrc.php/yk/r/Huwg88lmL14.webp")
        )
        self.assertFalse(
            _is_probable_facebook_photo_asset_url(
                "https://static.xx.fbcdn.net/rsrc.php/yk/r/Huwg88lmL14.webp"
            )
        )

    def test_prefer_media_candidates_chooses_real_photo_over_static_asset(self) -> None:
        chosen = _prefer_media_candidates(
            [
                {
                    "type": "photo",
                    "url": "https://static.xx.fbcdn.net/rsrc.php/yk/r/Huwg88lmL14.webp",
                    "source_url": "https://www.facebook.com/photo.php?fbid=1",
                },
                {
                    "type": "photo",
                    "url": (
                        "https://scontent-sin2-2.xx.fbcdn.net/v/t39.30808-6/488937437_2380198102337411_"
                        "1095729992036644340_n.jpg?stp=dst-jpg_p960x960&_nc_cat=1"
                    ),
                    "source_url": "https://www.facebook.com/photo.php?fbid=1",
                    "width": 955,
                    "height": 960,
                },
            ],
            prefer_single_photo=True,
        )
        self.assertEqual(len(chosen), 1)
        self.assertIn("scontent-sin2-2.xx.fbcdn.net", chosen[0]["url"])


if __name__ == "__main__":
    unittest.main()
