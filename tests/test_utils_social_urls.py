from __future__ import annotations

import unittest

from src.utils import facebook_profile_url, normalize_facebook_profile_identifier, platform_profile_url


class SocialUrlHelperTests(unittest.TestCase):
    def test_normalize_facebook_full_profile_url(self) -> None:
        self.assertEqual(
            normalize_facebook_profile_identifier(
                "https://www.facebook.com/profile.php?id=100010800738465"
            ),
            "profile.php?id=100010800738465",
        )

    def test_normalize_facebook_numeric_id(self) -> None:
        self.assertEqual(
            normalize_facebook_profile_identifier("100010800738465"),
            "profile.php?id=100010800738465",
        )

    def test_facebook_profile_url_uses_profile_php_for_numeric_ids(self) -> None:
        self.assertEqual(
            facebook_profile_url("100010800738465"),
            "https://www.facebook.com/profile.php?id=100010800738465",
        )

    def test_facebook_profile_url_keeps_vanity_slug(self) -> None:
        self.assertEqual(
            facebook_profile_url("vorlova.anezka"),
            "https://www.facebook.com/vorlova.anezka",
        )

    def test_facebook_profile_url_builds_sk_sections_for_profile_php_ids(self) -> None:
        self.assertEqual(
            facebook_profile_url("profile.php?id=100010800738465", section="friends"),
            "https://www.facebook.com/profile.php?id=100010800738465&sk=friends",
        )

    def test_facebook_profile_url_builds_generic_sections_for_profile_php_ids(self) -> None:
        self.assertEqual(
            facebook_profile_url("profile.php?id=100010800738465", section="photos"),
            "https://www.facebook.com/profile.php?id=100010800738465&sk=photos",
        )

    def test_facebook_profile_url_builds_sections_for_vanity_profiles(self) -> None:
        self.assertEqual(
            facebook_profile_url("vorlova.anezka", section="photos"),
            "https://www.facebook.com/vorlova.anezka/photos",
        )

    def test_platform_profile_url_delegates_facebook_identifier_handling(self) -> None:
        self.assertEqual(
            platform_profile_url("facebook", "100010800738465"),
            "https://www.facebook.com/profile.php?id=100010800738465",
        )


if __name__ == "__main__":
    unittest.main()
