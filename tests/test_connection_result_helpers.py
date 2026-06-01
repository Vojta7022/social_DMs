from __future__ import annotations

import unittest

from src.main import _connection_result_list


class ConnectionResultHelperTests(unittest.TestCase):
    def test_legacy_result_defaults_to_available(self) -> None:
        available, users = _connection_result_list({"followers": ["alice"]}, "followers")
        self.assertTrue(available)
        self.assertEqual(users, ["alice"])

    def test_partial_result_marks_unavailable_list(self) -> None:
        available, users = _connection_result_list(
            {
                "followers": ["alice"],
                "followers_available": True,
                "following": [],
                "following_available": False,
            },
            "following",
        )
        self.assertFalse(available)
        self.assertEqual(users, [])


if __name__ == "__main__":
    unittest.main()
