from __future__ import annotations

from unittest.mock import patch
import unittest

from src.intelligence import (
    add_behavior_to_reposts,
    add_geoparsing_to_posts,
    add_geoparsing_to_stories,
    add_sentiment_to_posts,
    add_sentiment_to_reposts,
    build_intelligence_bundle,
    build_social_graph_bundle,
)


class FakeAnalyzer:
    def polarity_scores(self, text: str) -> dict[str, float]:
        normalized = text.lower()
        if "love" in normalized or "great" in normalized:
            return {"compound": 0.8, "pos": 0.8, "neu": 0.2, "neg": 0.0}
        if "hate" in normalized or "bad" in normalized:
            return {"compound": -0.7, "pos": 0.0, "neu": 0.3, "neg": 0.7}
        return {"compound": 0.0, "pos": 0.0, "neu": 1.0, "neg": 0.0}


class IntelligenceTests(unittest.TestCase):
    def test_forecast_ready_for_regular_feed_posts(self) -> None:
        posts = [
            {"content_scope": "feed", "posted_at": "2026-03-01T10:00:00Z"},
            {"content_scope": "feed", "posted_at": "2026-03-04T10:00:00Z"},
            {"content_scope": "feed", "posted_at": "2026-03-07T10:00:00Z"},
            {"content_scope": "feed", "posted_at": "2026-03-10T10:00:00Z"},
        ]

        bundle = build_intelligence_bundle(
            platform="instagram",
            username="demo",
            generated_at="2026-03-30T00:00:00Z",
            bio={},
            posts=posts,
            stories=[],
        )

        forecast = bundle["next_post_prediction"]
        self.assertEqual(forecast["status"], "ready")
        self.assertEqual(forecast["basis"]["post_count_used"], 4)
        self.assertEqual(forecast["prediction"]["predicted_at"], "2026-03-13T10:00:00Z")
        self.assertGreater(forecast["confidence"], 0.0)
        self.assertIn(10, forecast["patterns"]["most_common_hours_utc"])

    def test_forecast_ready_for_two_feed_posts_with_low_confidence(self) -> None:
        posts = [
            {"content_scope": "feed", "posted_at": "2026-03-01T10:00:00Z"},
            {"content_scope": "feed", "posted_at": "2026-03-05T10:00:00Z"},
        ]

        bundle = build_intelligence_bundle(
            platform="instagram",
            username="demo",
            generated_at="2026-03-30T00:00:00Z",
            bio={},
            posts=posts,
            stories=[],
        )

        forecast = bundle["next_post_prediction"]
        self.assertEqual(forecast["status"], "ready")
        self.assertEqual(forecast["basis"]["post_count_used"], 2)
        self.assertEqual(forecast["prediction"]["predicted_at"], "2026-03-09T10:00:00Z")
        self.assertGreater(forecast["confidence"], 0.0)
        self.assertLess(forecast["confidence"], 0.5)
        self.assertIn("two archived feed-post timestamps", " ".join(forecast["notes"]))

    def test_forecast_ignores_non_feed_posts(self) -> None:
        posts = [
            {"content_scope": "mentions", "posted_at": "2026-03-01T10:00:00Z"},
            {"content_scope": "reposts", "posted_at": "2026-03-02T10:00:00Z"},
            {"content_scope": "feed", "posted_at": "2026-03-03T10:00:00Z"},
            {"content_scope": "feed", "posted_at": "2026-03-04T10:00:00Z"},
        ]

        bundle = build_intelligence_bundle(
            platform="instagram",
            username="demo",
            generated_at="2026-03-30T00:00:00Z",
            bio={},
            posts=posts,
            stories=[],
        )

        forecast = bundle["next_post_prediction"]
        self.assertEqual(forecast["status"], "ready")
        self.assertEqual(forecast["basis"]["post_count_used"], 2)
        self.assertEqual(forecast["prediction"]["predicted_at"], "2026-03-05T10:00:00Z")

    def test_sentiment_enriches_posts_and_rolls_up_profile_vibe(self) -> None:
        posts = [
            {
                "post_id": "A",
                "content_scope": "feed",
                "caption": "Love this trip",
                "comments": [{"text": "great vibes"}],
            },
            {
                "post_id": "B",
                "content_scope": "feed",
                "caption": "bad day",
                "comments": [{"text": "hate this"}],
            },
        ]

        with patch("src.intelligence._get_vader_analyzer", return_value=FakeAnalyzer()):
            enriched = add_sentiment_to_posts(posts)
            bundle = build_intelligence_bundle(
                platform="instagram",
                username="demo",
                generated_at="2026-03-30T00:00:00Z",
                bio={},
                posts=enriched,
                stories=[],
            )

        self.assertEqual(enriched[0]["sentiment"]["status"], "ready")
        self.assertGreater(enriched[0]["vibe_score"], 0)
        self.assertLess(enriched[1]["vibe_score"], 0)

        summary = bundle["sentiment_overview"]
        self.assertEqual(summary["status"], "ready")
        self.assertEqual(summary["posts_scored"], 2)
        self.assertEqual(summary["positive_posts"], 1)
        self.assertEqual(summary["negative_posts"], 1)
        self.assertEqual(summary["most_positive_post_id"], "A")
        self.assertEqual(summary["most_negative_post_id"], "B")

    def test_social_graph_export_is_networkx_compatible(self) -> None:
        graph = build_social_graph_bundle(
            platform="instagram",
            username="demo",
            generated_at="2026-03-30T00:00:00Z",
            followers={
                "snapshot_at": "2026-03-30T00:00:00Z",
                "users": [
                    {"username": "alice", "user_id": "101", "display_name": "Alice"},
                ],
            },
            following={
                "snapshot_at": "2026-03-30T00:00:00Z",
                "users": [
                    {"username": "bob", "user_id": "202", "display_name": "Bob"},
                ],
            },
        )

        self.assertEqual(graph["format"], "networkx_node_link")
        self.assertTrue(graph["directed"])
        self.assertEqual(len(graph["nodes"]), 3)
        self.assertEqual(len(graph["links"]), 2)
        self.assertEqual(graph["graph"]["target_node_id"], "instagram:username:demo")
        self.assertEqual(graph["links"][0]["relationship"], "follows_target")

    def test_geoparsing_extracts_caption_and_story_location_signals(self) -> None:
        posts = add_geoparsing_to_posts(
            [
                {
                    "post_id": "A",
                    "caption": "Weekend in Prague with friends #Praha",
                    "posted_at": "2026-03-01T10:00:00Z",
                }
            ]
        )
        stories = add_geoparsing_to_stories(
            [
                {
                    "story_id": "S1",
                    "content_scope": "highlight",
                    "posted_at": "2026-03-02T10:00:00Z",
                    "metadata": {
                        "locations": [{"name": "Rome", "city": "Rome"}],
                        "hashtags": ["#Italy"],
                    },
                }
            ]
        )

        bundle = build_intelligence_bundle(
            platform="instagram",
            username="demo",
            generated_at="2026-03-30T00:00:00Z",
            bio={"bio_text": "Based in Brno"},
            posts=posts,
            stories=stories,
        )

        locations = bundle["geoparsed_locations"]
        self.assertEqual(locations["status"], "ready")
        names = {entry["normalized_name"] for entry in locations["top_locations"]}
        self.assertIn("prague", names)
        self.assertIn("rome", names)
        self.assertIn("brno", names)

    def test_cross_platform_linking_uses_username_and_bio_links(self) -> None:
        bundle = build_intelligence_bundle(
            platform="instagram",
            username="john.doe",
            generated_at="2026-03-30T00:00:00Z",
            bio={"bio_text": "TikTok: @john_doe and https://facebook.com/john.doe"},
            posts=[],
            stories=[],
            candidate_profiles=[
                {"platform": "tiktok", "username": "john_doe", "display_name": "John Doe", "bio_snapshot": ""},
                {"platform": "facebook", "username": "john.doe", "display_name": "John Doe", "bio_snapshot": ""},
            ],
        )

        links = bundle["linked_profiles"]
        self.assertEqual(links["status"], "ready")
        self.assertEqual(links["match_count"], 2)
        self.assertEqual(links["matches"][0]["confidence_label"], "very_high")

    def test_repost_analytics_tracks_categories_sources_and_deviations(self) -> None:
        reposts = []
        for day in range(20, 30):
            reposts.append(
                {
                    "post_id": f"old-{day}",
                    "caption": "hate this political news",
                    "source_username": "old_source",
                    "source_platform": "instagram",
                    "original_posted_at": f"2026-01-{day:02d}T10:00:00Z",
                }
            )
        for day in range(25, 31):
            reposts.append(
                {
                    "post_id": f"new-{day}",
                    "caption": "love this funny meme",
                    "source_username": "meme_page",
                    "source_platform": "instagram",
                    "original_posted_at": f"2026-03-{day:02d}T10:00:00Z",
                }
            )

        with patch("src.intelligence._get_vader_analyzer", return_value=FakeAnalyzer()):
            enriched = add_behavior_to_reposts(add_sentiment_to_reposts(reposts))
            bundle = build_intelligence_bundle(
                platform="instagram",
                username="demo",
                generated_at="2026-03-31T00:00:00Z",
                bio={},
                posts=[],
                reposts=enriched,
                stories=[],
            )

        analytics = bundle["repost_analytics"]
        self.assertEqual(analytics["status"], "ready")
        self.assertEqual(analytics["total_reposts"], 16)
        self.assertEqual(analytics["top_categories"][0]["category"], "social_political")
        self.assertTrue(any(source["source_username"] == "meme_page" for source in analytics["high_frequency_sources"]))
        deviation_flags = {
            deviation["kind"]: deviation["flagged"]
            for deviation in analytics["deviations"]
        }
        self.assertTrue(deviation_flags["volume_spike"])
        self.assertTrue(deviation_flags["vibe_shift"])
        self.assertTrue(deviation_flags["source_shift"])


if __name__ == "__main__":
    unittest.main()
