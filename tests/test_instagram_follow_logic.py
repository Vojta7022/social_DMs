from __future__ import annotations

import unittest

from src.scrapers.instagram import _should_accept_graphql_follow_result


class InstagramFollowLogicTests(unittest.TestCase):
    def test_accepts_small_positive_count_mismatch_when_list_is_complete(self) -> None:
        self.assertTrue(
            _should_accept_graphql_follow_result(
                collected_count=851,
                reported_count=850,
                expected_count=850,
            )
        )

    def test_accepts_small_negative_count_mismatch_when_scalar_counts_are_stale(self) -> None:
        self.assertTrue(
            _should_accept_graphql_follow_result(
                collected_count=849,
                reported_count=850,
                expected_count=850,
            )
        )

    def test_accepts_two_count_negative_mismatch_when_profile_header_is_lagging(self) -> None:
        self.assertTrue(
            _should_accept_graphql_follow_result(
                collected_count=1171,
                reported_count=1172,
                expected_count=1173,
            )
        )

    def test_rejects_when_graphql_is_still_far_too_short(self) -> None:
        self.assertFalse(
            _should_accept_graphql_follow_result(
                collected_count=30,
                reported_count=850,
                expected_count=850,
            )
        )

    def test_rejects_large_positive_mismatch(self) -> None:
        self.assertFalse(
            _should_accept_graphql_follow_result(
                collected_count=900,
                reported_count=850,
                expected_count=850,
            )
        )


if __name__ == "__main__":
    unittest.main()
