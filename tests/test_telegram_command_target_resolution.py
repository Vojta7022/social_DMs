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
from src.telegram_commands import _resolve_command_target


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
            AccountConfig(platform="instagram", username="ig_login_one"),
            AccountConfig(platform="instagram", username="ig_login_two"),
            AccountConfig(platform="facebook", username="fb_login"),
        ],
        targets=[
            TargetConfig(platform="instagram", username="alice", using_account="ig_login_one", using_accounts=["ig_login_one"]),
            TargetConfig(platform="facebook", username="alice", using_account="fb_login", using_accounts=["fb_login"]),
            TargetConfig(platform="instagram", username="bob", using_account="ig_login_two", using_accounts=["ig_login_two"]),
        ],
    )


class TelegramCommandTargetResolutionTests(unittest.TestCase):
    def test_bare_unique_target_resolves(self) -> None:
        cfg = _sample_config()

        resolved = _resolve_command_target(cfg, "bob", command_name="scan_now")

        self.assertEqual(resolved, ("instagram", "bob"))

    def test_bare_ambiguous_target_raises(self) -> None:
        cfg = _sample_config()

        with self.assertRaisesRegex(ValueError, "ambiguous"):
            _resolve_command_target(cfg, "alice", command_name="pause")

    def test_platform_qualified_target_resolves(self) -> None:
        cfg = _sample_config()

        resolved = _resolve_command_target(cfg, "facebook:alice", command_name="resume")

        self.assertEqual(resolved, ("facebook", "alice"))


if __name__ == "__main__":
    unittest.main()
