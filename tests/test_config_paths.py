from __future__ import annotations

import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from src.config import load_config


class ConfigPathResolutionTests(unittest.TestCase):
    def test_load_config_resolves_relative_data_dir_and_log_path_from_settings_location(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_root = Path(tmp_dir) / "social-scanner"
            project_root.mkdir(parents=True, exist_ok=True)
            config_path = project_root / "settings.yaml"
            config_path.write_text(
                textwrap.dedent(
                    """
                    app:
                      data_dir: ./data
                    accounts: []
                    targets: {}
                    """
                ).strip()
            )

            with patch.dict(
                os.environ,
                {
                    "TELEGRAM_BOT_TOKEN": "token",
                    "TELEGRAM_CHAT_ID": "chat",
                },
                clear=True,
            ):
                cfg = load_config(config_path, None)

            expected_data_dir = (project_root / "data").resolve()
            self.assertEqual(cfg.data_dir, expected_data_dir)
            self.assertEqual(cfg.sessions_dir, expected_data_dir / "sessions")
            self.assertEqual(cfg.media_dir, expected_data_dir / "media")
            self.assertEqual(cfg.profiles_dir, expected_data_dir / "profiles")
            self.assertEqual(cfg.accounts_dir, expected_data_dir / "accounts")
            self.assertEqual(
                Path(cfg.logging.file),
                expected_data_dir / "social_scanner.log",
            )


if __name__ == "__main__":
    unittest.main()
