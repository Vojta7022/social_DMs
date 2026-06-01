from __future__ import annotations

import unittest

from src.config import (
    AccountConfig,
    ConfigError,
    TargetConfig,
    _parse_target_item,
    _normalise_target_accounts,
)


class ConfigTargetAccountTests(unittest.TestCase):
    def test_stale_using_account_falls_back_to_only_platform_account(self) -> None:
        accounts = [AccountConfig(platform="tiktok", username="vojtechponrt0")]
        targets = [
            TargetConfig(
                platform="tiktok",
                username="a.vorlovaa",
                using_account="krejzi_pepa",
            )
        ]

        normalised = _normalise_target_accounts(targets, accounts)
        self.assertEqual(normalised[0].using_account, "vojtechponrt0")
        self.assertEqual(normalised[0].using_accounts, ["vojtechponrt0"])

    def test_missing_using_account_uses_all_platform_accounts_as_pool(self) -> None:
        accounts = [
            AccountConfig(platform="instagram", username="one"),
            AccountConfig(platform="instagram", username="two"),
            AccountConfig(platform="instagram", username="three"),
        ]
        targets = [
            TargetConfig(platform="instagram", username="alice"),
            TargetConfig(platform="instagram", username="bob"),
        ]

        normalised = _normalise_target_accounts(targets, accounts)

        for target in normalised:
            self.assertEqual(target.using_accounts, ["one", "two", "three"])
            self.assertIn(target.using_account, {"one", "two", "three"})

        rerun = _normalise_target_accounts(targets, accounts)
        self.assertEqual(
            [target.using_account for target in normalised],
            [target.using_account for target in rerun],
        )

    def test_unknown_using_account_raises_when_platform_has_multiple_accounts(self) -> None:
        accounts = [
            AccountConfig(platform="instagram", username="one"),
            AccountConfig(platform="instagram", username="two"),
        ]
        targets = [
            TargetConfig(
                platform="instagram",
                username="alice",
                using_account="missing",
            )
        ]

        with self.assertRaises(ConfigError):
            _normalise_target_accounts(targets, accounts)

    def test_using_accounts_pool_is_preserved_and_resolved(self) -> None:
        accounts = [
            AccountConfig(platform="instagram", username="one"),
            AccountConfig(platform="instagram", username="two"),
            AccountConfig(platform="instagram", username="three"),
        ]
        targets = [
            TargetConfig(
                platform="instagram",
                username="alice",
                using_account="stale",
                using_accounts=["two", "three"],
            )
        ]

        normalised = _normalise_target_accounts(targets, accounts)
        self.assertEqual(normalised[0].using_accounts, ["two", "three"])
        self.assertIn(normalised[0].using_account, {"two", "three"})

    def test_facebook_target_string_url_is_normalized(self) -> None:
        target = _parse_target_item(
            "facebook",
            "https://www.facebook.com/profile.php?id=100010800738465",
        )
        self.assertEqual(target.username, "profile.php?id=100010800738465")

    def test_facebook_target_mapping_accepts_profile_url_alias(self) -> None:
        target = _parse_target_item(
            "facebook",
            {
                "profile_url": "https://www.facebook.com/profile.php?id=100010800738465",
                "using_account": "vojtech.ponrt",
            },
        )
        self.assertEqual(target.username, "profile.php?id=100010800738465")
        self.assertEqual(target.using_account, "vojtech.ponrt")

    def test_facebook_target_numeric_id_is_normalized(self) -> None:
        target = _parse_target_item("facebook", 100010800738465)
        self.assertEqual(target.username, "profile.php?id=100010800738465")


if __name__ == "__main__":
    unittest.main()
