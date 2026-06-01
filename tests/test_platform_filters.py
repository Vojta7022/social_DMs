from __future__ import annotations

import unittest

from src.config import (
    AccountConfig,
    ArchiveConfig,
    Config,
    LoggingConfig,
    MediaConfig,
    NotificationsConfig,
    ProxyConfig,
    ScheduleConfig,
    StealthConfig,
    TargetConfig,
)
from src.main import (
    _configured_targets,
    _filter_config_by_platforms,
    _parse_platform_filters,
)


def _sample_config() -> Config:
    return Config(
        schedule=ScheduleConfig(),
        stealth=StealthConfig(),
        media=MediaConfig(),
        archive=ArchiveConfig(),
        notifications=NotificationsConfig(),
        proxies=ProxyConfig(),
        logging=LoggingConfig(),
        accounts=[
            AccountConfig(platform="instagram", username="ig_login", monitor_own_profile=True),
            AccountConfig(platform="tiktok", username="tt_login", monitor_own_profile=True),
            AccountConfig(platform="facebook", username="fb_login"),
        ],
        targets=[
            TargetConfig(platform="instagram", username="ig_target", using_account="ig_login"),
            TargetConfig(platform="tiktok", username="tt_target", using_account="tt_login"),
            TargetConfig(platform="facebook", username="fb_target", using_account="fb_login"),
        ],
    )


class PlatformFilterTests(unittest.TestCase):
    def test_parse_platform_filters_normalizes_and_deduplicates(self) -> None:
        parsed = _parse_platform_filters(["TikTok", " instagram ", "tiktok"])
        self.assertEqual(parsed, {"tiktok", "instagram"})

    def test_parse_platform_filters_rejects_unknown_platforms(self) -> None:
        with self.assertRaisesRegex(ValueError, "Invalid platform filter"):
            _parse_platform_filters(["linkedin"])

    def test_filter_config_by_platforms_keeps_only_requested_platforms(self) -> None:
        cfg = _sample_config()

        filtered = _filter_config_by_platforms(cfg, {"tiktok"})

        self.assertEqual([(a.platform, a.username) for a in filtered.accounts], [("tiktok", "tt_login")])
        self.assertEqual([(t.platform, t.username) for t in filtered.targets], [("tiktok", "tt_target")])

    def test_filtered_config_keeps_only_matching_own_profile_targets(self) -> None:
        cfg = _sample_config()
        filtered = _filter_config_by_platforms(cfg, {"tiktok"})

        configured = [(target.platform, target.username) for target in _configured_targets(filtered)]
        self.assertEqual(configured, [("tiktok", "tt_target"), ("tiktok", "tt_login")])


if __name__ == "__main__":
    unittest.main()
