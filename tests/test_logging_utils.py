from __future__ import annotations

import unittest

from src.logging_utils import build_log_message, compact_url, normalize_job_name


class LoggingUtilsTests(unittest.TestCase):
    def test_build_log_message_uses_stable_key_value_format(self) -> None:
        message = build_log_message(
            "instagram",
            "scrape_result",
            kind="posts",
            target="@alice.test",
            items=12,
            note="fresh page retry",
        )
        self.assertEqual(
            message,
            '[instagram] scrape_result | kind=posts | target=@alice.test | items=12 | note="fresh page retry"',
        )

    def test_compact_url_drops_query_noise(self) -> None:
        compact = compact_url(
            "https://example.com/path/to/media.jpg?token=secret&expires=123",
            max_length=120,
        )
        self.assertEqual(compact, "https://example.com/path/to/media.jpg")

    def test_normalize_job_name_strips_bootstrap_prefix(self) -> None:
        self.assertEqual(normalize_job_name("bootstrap_posts"), "posts")
        self.assertEqual(normalize_job_name("followers"), "followers")


if __name__ == "__main__":
    unittest.main()
