from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from src.viewer import create_app
from src.viewer.catalog import ArchiveCatalog


class ViewerArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_root = Path(self.temp_dir.name) / "data"
        self._build_fixture(self.data_root)
        self.catalog = ArchiveCatalog(self.data_root)
        self.client = TestClient(create_app(self.data_root))

    def tearDown(self) -> None:
        self.client.close()
        self.temp_dir.cleanup()

    def test_catalog_splits_content_and_resolves_media_urls(self) -> None:
        profile = self.catalog.get_profile("instagram", "alice.test")
        self.assertIsNotNone(profile)
        assert profile is not None

        self.assertEqual(profile["counts"]["posts"], 1)
        self.assertEqual(profile["counts"]["mentions"], 1)
        self.assertEqual(profile["counts"]["reposts"], 1)
        self.assertEqual(profile["counts"]["stories"], 1)
        self.assertEqual(profile["counts"]["highlights"], 1)

        first_post = profile["collections"]["posts"][0]
        self.assertEqual(first_post["media_assets"][0]["url"], "/media/instagram/alice.test/posts/2026-03-01/POST1/photo_01.jpg")
        self.assertEqual(first_post["kind"], "posts")

        mention = profile["collections"]["mentions"][0]
        self.assertEqual(mention["item_id"], "MENTION1")
        self.assertEqual(mention["kind"], "mentions")

        highlight = profile["collections"]["highlights"][0]
        self.assertEqual(highlight["kind"], "highlights")
        self.assertEqual(highlight["media_assets"][0]["media_type"], "video")

        forecast = profile["intelligence"]["next_post_prediction"]
        self.assertEqual(forecast["predicted_at"], "2026-04-01T08:00:00Z")
        self.assertEqual(forecast["post_count_used"], 3)
        self.assertEqual(profile["intelligence"]["sentiment"]["posts_scored"], 2)
        self.assertTrue(profile["deep_profile"]["available"])
        self.assertEqual(profile["deep_profile"]["summary"]["analyzed_entries"], 3)
        self.assertEqual(profile["deep_profile"]["items"][0]["analysis_media_type"], "video_frame")

    def test_dashboard_and_profile_routes_render(self) -> None:
        dashboard = self.client.get("/?scope=tracked&content=with_any&sort_profiles=followers&profile_dir=desc")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("alice.test", dashboard.text)
        self.assertIn("Archive Dashboard", dashboard.text)
        self.assertIn("Open Archive", dashboard.text)
        self.assertIn("Sort accounts", dashboard.text)
        self.assertIn('href="/profiles/instagram/alice.test"', dashboard.text)
        self.assertNotIn('<a href="/profiles/instagram/alice.test" class="profile-card-link">', dashboard.text)

        profile_page = self.client.get("/profiles/instagram/alice.test?tab=mentions&sort=comments&dir=desc&media_filter=with_caption")
        self.assertEqual(profile_page.status_code, 200)
        self.assertIn("Mentioned on the team post", profile_page.text)
        self.assertIn("Mentions", profile_page.text)
        self.assertIn("Filter media", profile_page.text)
        self.assertIn("?tab=mentions&amp;sort=comments&amp;dir=desc&amp;media_filter=with_caption", profile_page.text)

    def test_dashboard_search_returns_item_matches_for_post_id(self) -> None:
        dashboard = self.client.get("/?platform=instagram&q=POST1")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("Matching archived items", dashboard.text)
        self.assertIn("Open Archived Item", dashboard.text)
        self.assertIn("POST1", dashboard.text)
        self.assertIn("/profiles/instagram/alice.test/items/posts/POST1", dashboard.text)

        api_response = self.client.get("/api/profiles?platform=instagram&q=POST1")
        self.assertEqual(api_response.status_code, 200)
        payload = api_response.json()
        self.assertEqual(len(payload["item_matches"]), 1)
        self.assertEqual(payload["item_matches"][0]["item_id"], "POST1")
        self.assertEqual(payload["item_matches"][0]["kind"], "posts")

    def test_profile_overview_renders_graph_visualization(self) -> None:
        profile_page = self.client.get("/profiles/instagram/alice.test")
        self.assertEqual(profile_page.status_code, 200)
        self.assertIn("2026-04-01 08:00 UTC", profile_page.text)
        self.assertIn("2026-04-01 06:00 UTC to 2026-04-01 10:00 UTC", profile_page.text)
        self.assertIn("Average vibe", profile_page.text)
        self.assertIn("Social graph export", profile_page.text)
        self.assertIn('class="graph-map"', profile_page.text)
        self.assertIn("Follower nodes", profile_page.text)
        self.assertIn("/api/profiles/instagram/alice.test/graph", profile_page.text)

    def test_item_detail_route_renders_video_and_metadata(self) -> None:
        detail = self.client.get("/profiles/instagram/alice.test/items/posts/POST1?tab=posts&sort=comments&dir=desc&media_filter=with_comments")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("Raw metadata", detail.text)
        self.assertIn("Looks good", detail.text)
        self.assertIn("1 reply", detail.text)
        self.assertIn("Thanks!", detail.text)
        self.assertIn("Per-user like lists are not archived yet", detail.text)
        self.assertIn("/profiles/instagram/alice.test?tab=posts&amp;sort=comments&amp;dir=desc&amp;media_filter=with_comments", detail.text)
        self.assertIn("https://www.instagram.com/commenter.one/", detail.text)
        self.assertIn("https://www.instagram.com/replier.one/", detail.text)

    def test_profile_posts_tab_renders_comment_tree_under_post_cards(self) -> None:
        page = self.client.get("/profiles/instagram/alice.test?tab=posts&sort=comments&dir=desc&media_filter=with_comments")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Comments", page.text)
        self.assertIn("Looks good", page.text)
        self.assertIn("Thanks!", page.text)
        self.assertIn("https://www.instagram.com/commenter.one/", page.text)
        self.assertIn("https://www.instagram.com/replier.one/", page.text)

    def test_item_detail_route_shows_comment_capture_reason_when_tree_is_empty(self) -> None:
        detail = self.client.get("/profiles/instagram/alice.test/items/mentions/MENTION1?tab=mentions&sort=comments&dir=desc&media_filter=with_comments")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("No archived comment tree is available for this snapshot.", detail.text)
        self.assertIn("2 comments were reported on the platform", detail.text)
        self.assertIn("TikTok did not return a complete comment payload.", detail.text)

    def test_highlight_detail_route_renders_video_and_extra_visuals(self) -> None:
        detail = self.client.get("/profiles/instagram/alice.test/items/highlights/HIGHLIGHT1?tab=highlights&sort=newest&dir=desc&media_filter=all")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("video.mp4", detail.text)
        self.assertIn("audio", detail.text)
        self.assertIn("<video", detail.text)
        self.assertIn("Extra Visual Candidates", detail.text)
        self.assertIn("https://www.instagram.com/alice.test/", detail.text)

    def test_api_profile_route_returns_profile_payload(self) -> None:
        response = self.client.get("/api/profiles/instagram/alice.test")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["username"], "alice.test")
        self.assertIn("collections", payload)
        self.assertEqual(payload["connections"]["followers"]["count"], 2)

    def test_api_profile_graph_route_returns_graph_payload(self) -> None:
        response = self.client.get("/api/profiles/instagram/alice.test/graph")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["platform"], "instagram")
        self.assertEqual(payload["username"], "alice.test")
        self.assertEqual(len(payload["nodes"]), 4)
        self.assertEqual(len(payload["links"]), 3)

    def test_deep_profile_route_and_api_render_generated_artifact(self) -> None:
        page = self.client.get("/profiles/instagram/alice.test?tab=deep_profile")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Deep Profile", page.text)
        self.assertIn("Visible Brands Or Logos", page.text)
        self.assertIn("tennis", page.text)
        self.assertIn("/api/profiles/instagram/alice.test/deep-profile", page.text)

        api_response = self.client.get("/api/profiles/instagram/alice.test/deep-profile")
        self.assertEqual(api_response.status_code, 200)
        payload = api_response.json()
        self.assertTrue(payload["available"])
        self.assertEqual(payload["summary"]["analyzed_entries"], 3)
        self.assertEqual(payload["items"][0]["analysis"]["social_context_visible"]["category"], "group")

    def test_catalog_loads_account_archives_and_conversations(self) -> None:
        account = self.catalog.get_account("instagram", "vojtechponrt")
        self.assertIsNotNone(account)
        assert account is not None

        self.assertEqual(account["archive_counts"]["conversations"], 1)
        self.assertEqual(account["archive_counts"]["messages"], 2)

        owner, conversation = self.catalog.get_conversation("instagram", "vojtechponrt", "thread-1")
        self.assertIsNotNone(owner)
        self.assertIsNotNone(conversation)
        assert conversation is not None

        self.assertEqual(conversation["message_count"], 2)
        self.assertEqual(conversation["primary_profile"]["username"], "nina.example")
        self.assertTrue(conversation["messages"][1]["sender"]["linked_profile"]["available"])
        self.assertEqual(conversation["messages"][1]["attachments"][0]["url"], "/media/instagram/dms/vojtechponrt/thread-1/2026-03-01/message-2/photo_01.jpg")

    def test_accounts_and_messages_routes_render(self) -> None:
        accounts = self.client.get("/accounts?sort_accounts=messages&account_dir=desc")
        self.assertEqual(accounts.status_code, 200)
        self.assertIn("Account Archives", accounts.text)
        self.assertIn("Open Account", accounts.text)
        self.assertIn("Viewer sections", accounts.text)
        self.assertIn("Messages", accounts.text)
        self.assertIn("vojtechponrt", accounts.text)

        messages = self.client.get("/messages?q=nina")
        self.assertEqual(messages.status_code, 200)
        self.assertIn("Message Inbox", messages.text)
        self.assertIn("Nina Example", messages.text)
        self.assertIn("/accounts/instagram/vojtechponrt/conversations/thread-1", messages.text)
        self.assertIn("/profiles/instagram/nina.example", messages.text)

    def test_conversation_route_and_api_render_saved_messages(self) -> None:
        detail = self.client.get("/accounts/instagram/vojtechponrt/conversations/thread-1")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("Hey Nina", detail.text)
        self.assertIn("See you soon", detail.text)
        self.assertIn("photo_01.jpg", detail.text)
        self.assertIn("Send from Instagram", detail.text)
        self.assertIn("/profiles/instagram/nina.example", detail.text)

        api_response = self.client.get("/api/accounts/instagram/vojtechponrt/conversations/thread-1")
        self.assertEqual(api_response.status_code, 200)
        payload = api_response.json()
        self.assertEqual(payload["conversation_id"], "thread-1")
        self.assertEqual(len(payload["messages"]), 2)

    def test_profile_page_shows_related_conversations(self) -> None:
        detail = self.client.get("/profiles/instagram/nina.example")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("Direct messages", detail.text)
        self.assertIn("/accounts/instagram/vojtechponrt/conversations/thread-1", detail.text)

    def test_conversation_send_endpoints(self) -> None:
        redirect = self.client.post(
            "/accounts/instagram/vojtechponrt/conversations/thread-1/send",
            data={"message_text": ""},
            follow_redirects=False,
        )
        self.assertEqual(redirect.status_code, 303)
        self.assertIn("error=empty_message", redirect.headers["location"])

        with patch(
            "src.message_gateway.send_owned_account_message",
            new=AsyncMock(
                return_value={
                    "platform": "instagram",
                    "username": "vojtechponrt",
                    "conversation_id": "thread-1",
                    "message_count": 3,
                }
            ),
        ) as mocked_send:
            api_response = self.client.post(
                "/api/accounts/instagram/vojtechponrt/conversations/thread-1/send",
                json={"text": "hello"},
            )
            self.assertEqual(api_response.status_code, 200)
            self.assertEqual(api_response.json()["status"], "ok")
            mocked_send.assert_awaited_once()

    def _build_fixture(self, data_root: Path) -> None:
        profile_dir = data_root / "profiles" / "instagram" / "alice.test"
        nina_profile_dir = data_root / "profiles" / "instagram" / "nina.example"
        own_profile_dir = data_root / "profiles" / "instagram" / "vojtechponrt"
        media_root = data_root / "media" / "instagram" / "alice.test"
        account_dir = data_root / "accounts" / "instagram" / "vojtechponrt"
        dm_conversation_dir = account_dir / "dms" / "conversations" / "thread-1"
        dm_media_dir = data_root / "media" / "instagram" / "dms" / "vojtechponrt" / "thread-1" / "2026-03-01" / "message-2"

        post_dir = media_root / "posts" / "2026-03-01" / "POST1"
        mention_dir = media_root / "mentions" / "2026-03-02" / "MENTION1"
        repost_dir = media_root / "reposts" / "2026-03-03" / "REPOST1"
        story_dir = media_root / "stories" / "2026-03-04" / "STORY1"
        highlight_dir = media_root / "story_highlights" / "2026-03-05" / "HIGHLIGHT1"

        for path in (
            profile_dir,
            nina_profile_dir,
            own_profile_dir,
            post_dir,
            mention_dir,
            repost_dir,
            story_dir,
            highlight_dir,
            account_dir,
            dm_conversation_dir,
            dm_media_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

        (post_dir / "photo_01.jpg").write_bytes(b"fake-jpg")
        (mention_dir / "photo_01.jpg").write_bytes(b"fake-jpg")
        (repost_dir / "photo_01.jpg").write_bytes(b"fake-jpg")
        (story_dir / "video.mp4").write_bytes(b"fake-mp4")
        (highlight_dir / "video.mp4").write_bytes(b"fake-mp4")
        (dm_media_dir / "photo_01.jpg").write_bytes(b"fake-dm")

        profile_json = {
            "schema_version": 7,
            "generated_at": "2026-03-30T12:00:00Z",
            "platform": "instagram",
            "username": "alice.test",
            "is_own_account": False,
            "bio": {
                "display_name": "Alice Test",
                "bio_text": "archived profile for viewer tests",
                "follower_count": 2,
                "following_count": 1,
                "post_count": 3,
                "profile_pic_url": "https://example.com/alice.jpg",
                "snapshot_at": "2026-03-30T12:00:00Z",
            },
            "followers": {"snapshot_at": "2026-03-30T12:00:00Z", "count": 2},
            "following": {"snapshot_at": "2026-03-30T12:00:00Z", "count": 1},
            "media_summary": {
                "total_posts": 3,
                "total_stories_captured": 2,
                "total_mention_posts": 1,
                "total_repost_posts": 1,
                "total_highlight_items": 1,
                "total_media_files": 5,
                "total_size_bytes": 1024,
            },
            "archive_counts": {"posts": 3, "reposts": 1, "stories": 2},
            "files": {
                "posts": "posts.json",
                "reposts": "reposts.json",
                "stories": "stories.json",
                "connections": "connections.json",
                "intelligence": "intelligence.json",
                "graph": "graph.json",
            },
        }
        posts_json = {
            "schema_version": 7,
            "generated_at": "2026-03-30T12:00:00Z",
            "platform": "instagram",
            "username": "alice.test",
            "items": [
                {
                    "post_id": "POST1",
                    "post_type": "photo",
                    "content_scope": "feed",
                    "collection_name": None,
                    "url": "https://instagram.example/POST1",
                    "caption": "Feed post caption",
                    "posted_at": "2026-03-01T08:00:00Z",
                    "discovered_at": "2026-03-30T12:00:00Z",
                    "like_count": 11,
                    "comment_count": 1,
                    "view_count": None,
                    "share_count": None,
                    "is_repost": False,
                    "repost_source_url": None,
                    "repost_source_account": None,
                    "comments": [
                        {
                            "comment_id": "C1",
                            "parent_comment_id": None,
                            "username": "commenter.one",
                            "display_name": "Commenter One",
                            "text": "Looks good",
                            "commented_at": "2026-03-01T09:00:00Z",
                            "like_count": 3,
                            "reply_count": 1,
                        }
                    ],
                    "comment_tree": [
                        {
                            "comment_id": "C1",
                            "parent_comment_id": None,
                            "username": "commenter.one",
                            "display_name": "Commenter One",
                            "text": "Looks good",
                            "commented_at": "2026-03-01T09:00:00Z",
                            "like_count": 3,
                            "reply_count": 1,
                            "replies": [
                                {
                                    "comment_id": "C2",
                                    "parent_comment_id": "C1",
                                    "username": "replier.one",
                                    "display_name": "Replier One",
                                    "text": "Thanks!",
                                    "commented_at": "2026-03-01T09:05:00Z",
                                    "like_count": 1,
                                    "reply_count": 0,
                                    "replies": [],
                                }
                            ],
                        }
                    ],
                    "media_paths": ["media/instagram/alice.test/posts/2026-03-01/POST1/photo_01.jpg"],
                },
                {
                    "post_id": "MENTION1",
                    "post_type": "carousel",
                    "content_scope": "mentions",
                    "collection_name": None,
                    "url": "https://instagram.example/MENTION1",
                    "caption": "Mentioned on the team post",
                    "posted_at": "2026-03-02T10:00:00Z",
                    "discovered_at": "2026-03-30T12:05:00Z",
                    "like_count": 5,
                    "comment_count": 2,
                    "view_count": None,
                    "share_count": None,
                    "is_repost": False,
                    "repost_source_url": None,
                    "repost_source_account": None,
                    "comments_supported": True,
                    "comments_snapshot_complete": False,
                    "comments_unavailable_reason": "TikTok did not return a complete comment payload.",
                    "comments": [],
                    "comment_tree": [],
                    "media_paths": ["media/instagram/alice.test/mentions/2026-03-02/MENTION1/photo_01.jpg"],
                },
            ],
        }
        reposts_json = {
            "schema_version": 7,
            "generated_at": "2026-03-30T12:00:00Z",
            "platform": "instagram",
            "username": "alice.test",
            "items": [
                {
                    "post_id": "REPOST1",
                    "post_type": "photo",
                    "content_scope": "reposts",
                    "collection_name": None,
                    "url": "https://instagram.example/REPOST1",
                    "caption": "Reblogged content",
                    "posted_at": "2026-03-03T11:00:00Z",
                    "discovered_at": "2026-03-30T12:10:00Z",
                    "like_count": 1,
                    "comment_count": 0,
                    "view_count": None,
                    "share_count": None,
                    "is_repost": True,
                    "repost_source_url": "https://instagram.example/original",
                    "repost_source_account": "source.account",
                    "comments": [],
                    "comment_tree": [],
                    "media_paths": ["media/instagram/alice.test/reposts/2026-03-03/REPOST1/photo_01.jpg"],
                }
            ],
        }
        stories_json = {
            "schema_version": 7,
            "generated_at": "2026-03-30T12:00:00Z",
            "platform": "instagram",
            "username": "alice.test",
            "items": [
                {
                    "story_id": "STORY1",
                    "story_type": "video",
                    "content_scope": "story",
                    "collection_name": None,
                    "url": "https://instagram.example/STORY1",
                    "posted_at": "2026-03-04T12:00:00Z",
                    "discovered_at": "2026-03-30T12:15:00Z",
                    "expires_at": "2026-03-05T12:00:00Z",
                    "metadata": {"caption": "Story caption", "owner": {"username": "alice.test"}},
                    "media_paths": ["media/instagram/alice.test/stories/2026-03-04/STORY1/video.mp4"],
                },
                {
                    "story_id": "HIGHLIGHT1",
                    "story_type": "video",
                    "content_scope": "highlight",
                    "collection_name": "Trips",
                    "url": "https://instagram.example/HIGHLIGHT1",
                    "posted_at": "2026-03-05T14:00:00Z",
                    "discovered_at": "2026-03-30T12:20:00Z",
                    "expires_at": None,
                    "metadata": {"caption": "Highlight caption", "owner": {"username": "alice.test"}},
                    "media_paths": ["media/instagram/alice.test/story_highlights/2026-03-05/HIGHLIGHT1/video.mp4"],
                },
            ],
        }
        connections_json = {
            "schema_version": 7,
            "generated_at": "2026-03-30T12:00:00Z",
            "platform": "instagram",
            "username": "alice.test",
            "followers": {
                "snapshot_at": "2026-03-30T12:00:00Z",
                "count": 2,
                "usernames": ["bob", "carol"],
                "users": [
                    {"username": "bob", "display_name": "Bob", "user_id": "1", "is_verified": False, "is_private": False},
                    {"username": "carol", "display_name": "Carol", "user_id": "2", "is_verified": True, "is_private": True},
                ],
            },
            "following": {
                "snapshot_at": "2026-03-30T12:00:00Z",
                "count": 1,
                "usernames": ["dave"],
                "users": [
                    {"username": "dave", "display_name": "Dave", "user_id": "3", "is_verified": False, "is_private": False}
                ],
            },
            "follower_delta_log": [{"event_type": "gained_follower", "target_username": "carol", "detected_at": "2026-03-30T11:00:00Z"}],
            "following_delta_log": [{"event_type": "started_following", "target_username": "dave", "detected_at": "2026-03-30T11:05:00Z"}],
        }
        intelligence_json = {
            "schema_version": 1,
            "generated_at": "2026-03-30T12:00:00Z",
            "platform": "instagram",
            "username": "alice.test",
            "next_post_prediction": {
                "status": "ready",
                "generated_at": "2026-03-30T12:00:00Z",
                "confidence": 0.58,
                "basis": {
                    "post_count_used": 3,
                    "first_post_at": "2026-02-26T08:00:00Z",
                    "last_post_at": "2026-03-29T08:00:00Z",
                },
                "prediction": {
                    "predicted_at": "2026-04-01T08:00:00Z",
                    "window_start": "2026-04-01T06:00:00Z",
                    "window_end": "2026-04-01T10:00:00Z",
                    "expected_hour_utc": 8,
                    "expected_weekday_utc": "Wednesday",
                },
                "patterns": {
                    "median_interval_hours": 72.0,
                    "mean_interval_hours": 72.0,
                    "stddev_interval_hours": 0.0,
                    "median_absolute_deviation_hours": 0.0,
                    "most_common_hours_utc": [8],
                    "most_common_weekdays_utc": ["Wednesday", "Saturday", "Sunday"],
                },
                "notes": [
                    "Prediction is based on archived feed-post timestamps only.",
                    "All times are stored in UTC.",
                ],
            },
            "sentiment_overview": {
                "status": "ready",
                "average_vibe_score": 0.42,
                "posts_scored": 2,
                "positive_posts": 1,
                "neutral_posts": 1,
                "negative_posts": 0,
            },
            "geoparsed_locations": {
                "status": "ready",
                "top_locations": [
                    {
                        "name": "Prague",
                        "occurrences": 2,
                        "max_confidence": 0.9,
                        "source_types": ["caption", "location_tag"],
                    }
                ],
            },
            "linked_profiles": {
                "status": "ready",
                "matches": [
                    {
                        "platform": "tiktok",
                        "username": "alice.test",
                        "confidence": 0.93,
                        "confidence_label": "high",
                        "reasons": ["shared_username"],
                    }
                ],
            },
            "repost_analytics": {
                "status": "ready",
                "total_reposts": 1,
                "average_vibe_score": 0.15,
                "active_window": "all_time",
                "deviation_flags": 0,
                "top_categories": [{"category": "humor", "count": 1, "share": "100%"}],
            },
        }
        graph_json = {
            "schema_version": 1,
            "generated_at": "2026-03-30T12:00:00Z",
            "platform": "instagram",
            "username": "alice.test",
            "format": "networkx-node-link",
            "directed": True,
            "multigraph": False,
            "graph": {
                "name": "instagram:alice.test",
                "target_node_id": "instagram:username:alice.test",
                "followers_snapshot_at": "2026-03-30T12:00:00Z",
                "following_snapshot_at": "2026-03-30T12:00:00Z",
            },
            "nodes": [
                {
                    "id": "instagram:username:alice.test",
                    "platform": "instagram",
                    "username": "alice.test",
                    "user_id": None,
                    "display_name": "Alice Test",
                    "kind": "target",
                    "is_target_profile": True,
                },
                {
                    "id": "instagram:user_id:1",
                    "platform": "instagram",
                    "username": "bob",
                    "user_id": "1",
                    "display_name": "Bob",
                    "kind": "follower",
                    "is_target_profile": False,
                },
                {
                    "id": "instagram:user_id:2",
                    "platform": "instagram",
                    "username": "carol",
                    "user_id": "2",
                    "display_name": "Carol",
                    "kind": "follower",
                    "is_target_profile": False,
                },
                {
                    "id": "instagram:user_id:3",
                    "platform": "instagram",
                    "username": "dave",
                    "user_id": "3",
                    "display_name": "Dave",
                    "kind": "following",
                    "is_target_profile": False,
                },
            ],
            "links": [
                {
                    "source": "instagram:user_id:1",
                    "target": "instagram:username:alice.test",
                    "relationship": "follows_target",
                    "snapshot_at": "2026-03-30T12:00:00Z",
                },
                {
                    "source": "instagram:user_id:2",
                    "target": "instagram:username:alice.test",
                    "relationship": "follows_target",
                    "snapshot_at": "2026-03-30T12:00:00Z",
                },
                {
                    "source": "instagram:username:alice.test",
                    "target": "instagram:user_id:3",
                    "relationship": "target_follows",
                    "snapshot_at": "2026-03-30T12:00:00Z",
                },
            ],
        }
        deep_analysis_json = {
            "schema_version": 1,
            "analysis_type": "safe_deep_media_profile",
            "generated_at": "2026-03-30T12:40:00Z",
            "platform": "instagram",
            "username": "alice.test",
            "model": "gemma4:latest",
            "prompt_version": "deep_media_safe_v1",
            "artifact_relative_path": "profiles/instagram/alice.test/deep_analysis.json",
            "report_relative_path": "profiles/alice.test.md",
            "summary": {
                "overview": "Analyzed 3 visual entries from 2 archived media files. Most common visible activities: tennis, walking. Recurring visual themes: travel, seaside.",
                "scanned_media_files": 2,
                "source_images": 1,
                "source_videos": 1,
                "analyzed_entries": 3,
                "video_frames_analyzed": 2,
                "unique_videos_represented": 1,
                "date_range": {
                    "earliest": "2026-03-01T08:00:00Z",
                    "latest": "2026-03-05T14:00:00Z",
                },
                "source_breakdown": [
                    {"label": "highlights", "count": 2},
                    {"label": "posts", "count": 1},
                ],
                "top_activities": [
                    {"label": "tennis", "count": 2},
                    {"label": "walking", "count": 1},
                ],
                "top_objects": [
                    {"label": "racket", "count": 2},
                    {"label": "coastline", "count": 1},
                ],
                "top_brands_or_logos": [
                    {"label": "Nike", "count": 1},
                ],
                "top_style_signals": [
                    {"label": "sporty casual", "count": 2},
                ],
                "top_location_signals": [
                    {"label": "seaside promenade", "count": 1},
                ],
                "top_recurring_themes": [
                    {"label": "travel", "count": 2},
                    {"label": "seaside", "count": 1},
                ],
                "top_media_tones": [
                    {"label": "lifestyle montage", "count": 2},
                ],
                "social_context_breakdown": [
                    {"label": "group", "count": 2},
                    {"label": "solo", "count": 1},
                ],
            },
            "items": [
                {
                    "entry_id": "frame-2",
                    "platform": "instagram",
                    "target_username": "alice.test",
                    "source_kind": "story_highlights",
                    "source_item_id": "HIGHLIGHT1",
                    "source_relative_path": "media/instagram/alice.test/story_highlights/2026-03-05/HIGHLIGHT1/video.mp4",
                    "source_media_type": "video",
                    "analysis_media_type": "video_frame",
                    "posted_at": "2026-03-05T14:00:00Z",
                    "discovered_at": "2026-03-30T12:20:00Z",
                    "collection_name": "Trips",
                    "content_scope": "highlight",
                    "caption": "Highlight caption",
                    "text_context": "Highlight caption seaside promenade",
                    "frame_index": 2,
                    "frame_offset_s": 6.2,
                    "source_sha256": "sha-frame-2",
                    "model": "gemma4:latest",
                    "prompt_version": "deep_media_safe_v1",
                    "analyzed_at": "2026-03-30T12:40:00Z",
                    "analysis": {
                        "visual_summary": "A group standing near the seaside promenade in a travel-style clip.",
                        "activities": ["tennis", "walking"],
                        "visible_objects": ["racket", "coastline"],
                        "visible_brands_or_logos": ["Nike"],
                        "fashion_and_style_signals": ["sporty casual"],
                        "explicit_location_signals": ["seaside promenade"],
                        "social_context_visible": {"category": "group", "notes": "Several people are visible together."},
                        "recurring_themes": ["travel", "seaside"],
                        "media_tone": ["lifestyle montage"],
                        "confidence_notes": "Location is inferred only from visible shoreline and promenade features.",
                    },
                },
                {
                    "entry_id": "frame-1",
                    "platform": "instagram",
                    "target_username": "alice.test",
                    "source_kind": "story_highlights",
                    "source_item_id": "HIGHLIGHT1",
                    "source_relative_path": "media/instagram/alice.test/story_highlights/2026-03-05/HIGHLIGHT1/video.mp4",
                    "source_media_type": "video",
                    "analysis_media_type": "video_frame",
                    "posted_at": "2026-03-05T14:00:00Z",
                    "discovered_at": "2026-03-30T12:20:00Z",
                    "collection_name": "Trips",
                    "content_scope": "highlight",
                    "caption": "Highlight caption",
                    "text_context": "Highlight caption",
                    "frame_index": 1,
                    "frame_offset_s": 2.1,
                    "source_sha256": "sha-frame-1",
                    "model": "gemma4:latest",
                    "prompt_version": "deep_media_safe_v1",
                    "analyzed_at": "2026-03-30T12:39:00Z",
                    "analysis": {
                        "visual_summary": "People in motion in a sporty travel clip.",
                        "activities": ["tennis"],
                        "visible_objects": ["racket"],
                        "visible_brands_or_logos": [],
                        "fashion_and_style_signals": ["sporty casual"],
                        "explicit_location_signals": [],
                        "social_context_visible": {"category": "group", "notes": "More than two people appear in frame."},
                        "recurring_themes": ["travel"],
                        "media_tone": ["lifestyle montage"],
                        "confidence_notes": "The frame is motion-heavy.",
                    },
                },
                {
                    "entry_id": "img-1",
                    "platform": "instagram",
                    "target_username": "alice.test",
                    "source_kind": "posts",
                    "source_item_id": "POST1",
                    "source_relative_path": "media/instagram/alice.test/posts/2026-03-01/POST1/photo_01.jpg",
                    "source_media_type": "image",
                    "analysis_media_type": "image",
                    "posted_at": "2026-03-01T08:00:00Z",
                    "discovered_at": "2026-03-30T12:00:00Z",
                    "collection_name": None,
                    "content_scope": "feed",
                    "caption": "Feed post caption",
                    "text_context": "Feed post caption",
                    "frame_index": None,
                    "frame_offset_s": None,
                    "source_sha256": "sha-img-1",
                    "model": "gemma4:latest",
                    "prompt_version": "deep_media_safe_v1",
                    "analyzed_at": "2026-03-30T12:38:00Z",
                    "analysis": {
                        "visual_summary": "A solo outdoor portrait with a relaxed travel feel.",
                        "activities": ["walking"],
                        "visible_objects": ["coastline"],
                        "visible_brands_or_logos": [],
                        "fashion_and_style_signals": ["casual streetwear"],
                        "explicit_location_signals": [],
                        "social_context_visible": {"category": "solo", "notes": "One person is clearly foregrounded."},
                        "recurring_themes": ["travel"],
                        "media_tone": ["lifestyle still"],
                        "confidence_notes": "The background is partially blurred.",
                    },
                },
            ],
            "safety": {
                "scope": "Visible or explicitly stated content only.",
                "excluded_inferences": [
                    "mental health or emotional diagnosis",
                    "financial status or wealth tier",
                    "biometric guesses such as age or gender",
                    "relationship status",
                ],
            },
        }
        nina_profile_json = {
            "schema_version": 7,
            "generated_at": "2026-03-30T12:31:00Z",
            "platform": "instagram",
            "username": "nina.example",
            "is_own_account": False,
            "bio": {
                "display_name": "Nina Example",
                "bio_text": "DM-linked profile bundle",
                "follower_count": 12,
                "following_count": 9,
                "post_count": 0,
                "profile_pic_url": "https://example.com/nina.jpg",
                "snapshot_at": "2026-03-30T12:31:00Z",
            },
            "followers": {"snapshot_at": "2026-03-30T12:31:00Z", "count": 12},
            "following": {"snapshot_at": "2026-03-30T12:31:00Z", "count": 9},
            "media_summary": {"total_media_files": 0, "total_size_bytes": 0},
            "archive_counts": {},
            "files": {
                "posts": "posts.json",
                "reposts": "reposts.json",
                "stories": "stories.json",
                "connections": "connections.json",
                "intelligence": "intelligence.json",
                "graph": "graph.json",
            },
        }
        own_profile_json = {
            "schema_version": 7,
            "generated_at": "2026-03-30T12:32:00Z",
            "platform": "instagram",
            "username": "vojtechponrt",
            "is_own_account": True,
            "bio": {
                "display_name": "Vojtech Ponrt",
                "bio_text": "Owned account profile bundle",
                "follower_count": 21,
                "following_count": 18,
                "post_count": 4,
                "profile_pic_url": "https://example.com/vojtech.jpg",
                "snapshot_at": "2026-03-30T12:32:00Z",
            },
            "followers": {"snapshot_at": "2026-03-30T12:32:00Z", "count": 21},
            "following": {"snapshot_at": "2026-03-30T12:32:00Z", "count": 18},
            "media_summary": {"total_media_files": 0, "total_size_bytes": 0},
            "archive_counts": {},
            "files": {
                "posts": "posts.json",
                "reposts": "reposts.json",
                "stories": "stories.json",
                "connections": "connections.json",
                "intelligence": "intelligence.json",
                "graph": "graph.json",
            },
        }

        sidecar_post = {
            "platform": "instagram",
            "target_username": "alice.test",
            "post_id": "POST1",
            "caption": "Feed post caption",
            "saved_media": [
                {
                    "file_type": "photo",
                    "file_path": "media/instagram/alice.test/posts/2026-03-01/POST1/photo_01.jpg",
                    "filename": "photo_01.jpg",
                    "exists_on_disk": True,
                    "file_size": 8,
                    "width": 1200,
                    "height": 1500,
                    "duration_s": None,
                    "has_audio": False,
                }
            ],
            "comments": posts_json["items"][0]["comments"],
            "comment_tree": posts_json["items"][0]["comment_tree"],
        }
        sidecar_story = {
            "platform": "instagram",
            "target_username": "alice.test",
            "story_id": "HIGHLIGHT1",
            "story_type": "video",
            "content_scope": "highlight",
            "collection_name": "Trips",
            "metadata": {
                "caption": "Highlight caption",
                "location": {"name": "Prague"},
                "media_candidates": [
                    {
                        "kind": "image",
                        "url": "https://cdn.example.test/highlight-preview.jpg",
                        "source": "embedded_image_versions",
                        "width": 720,
                        "height": 1280,
                    }
                ],
            },
            "saved_media": [
                {
                    "file_type": "video",
                    "file_path": "media/instagram/alice.test/story_highlights/2026-03-05/HIGHLIGHT1/video.mp4",
                    "filename": "video.mp4",
                    "exists_on_disk": True,
                    "file_size": 8,
                    "width": 720,
                    "height": 1280,
                    "duration_s": 12.3,
                    "has_audio": True,
                }
            ],
        }
        account_json = {
            "schema_version": 1,
            "generated_at": "2026-03-30T12:30:00Z",
            "platform": "instagram",
            "username": "vojtechponrt",
            "archive_counts": {
                "conversations": 1,
                "messages": 2,
                "attachments": 1,
            },
            "files": {
                "dms_index": "dms/index.json",
            },
        }
        dms_index_json = {
            "schema_version": 1,
            "generated_at": "2026-03-30T12:30:00Z",
            "platform": "instagram",
            "account_username": "vojtechponrt",
            "conversation_count": 1,
            "message_count": 2,
            "attachment_count": 1,
            "items": [
                {
                    "conversation_id": "thread-1",
                    "conversation_type": "direct",
                    "title": "Nina Example",
                    "subtitle": None,
                    "is_group": False,
                    "is_broadcast_channel": False,
                    "participant_usernames": ["nina.example"],
                    "message_count": 2,
                    "attachment_count": 1,
                    "last_message_preview": "See you soon",
                    "last_message_at": "2026-03-01T09:05:00Z",
                    "last_scraped_at": "2026-03-30T12:30:00Z",
                    "conversation_path": "conversations/thread-1/conversation.json",
                    "messages_path": "conversations/thread-1/messages.json",
                }
            ],
        }
        conversation_json = {
            "schema_version": 1,
            "generated_at": "2026-03-30T12:30:00Z",
            "platform": "instagram",
            "account_username": "vojtechponrt",
            "conversation_id": "thread-1",
            "conversation_type": "direct",
            "title": "Nina Example",
            "subtitle": None,
            "conversation_url": "https://www.instagram.com/direct/t/thread-1/",
            "is_group": False,
            "is_broadcast_channel": False,
            "last_message_preview": "See you soon",
            "last_message_at": "2026-03-01T09:05:00Z",
            "discovered_at": "2026-03-30T12:30:00Z",
            "last_scraped_at": "2026-03-30T12:30:00Z",
            "participants": [
                {
                    "username": "vojtechponrt",
                    "user_id": "self-1",
                    "display_name": "Vojtech Ponrt",
                    "profile_url": "https://www.instagram.com/vojtechponrt/",
                    "profile_pic_url": None,
                    "is_self": True,
                    "participant_order": 0,
                    "metadata": {},
                },
                {
                    "username": "nina.example",
                    "user_id": "user-2",
                    "display_name": "Nina Example",
                    "profile_url": "https://www.instagram.com/nina.example/",
                    "profile_pic_url": None,
                    "is_self": False,
                    "participant_order": 1,
                    "metadata": {},
                },
            ],
            "message_count": 2,
            "attachment_count": 1,
            "files": {
                "messages": "messages.json",
            },
        }
        messages_json = {
            "schema_version": 1,
            "generated_at": "2026-03-30T12:30:00Z",
            "platform": "instagram",
            "account_username": "vojtechponrt",
            "conversation_id": "thread-1",
            "items": [
                {
                    "message_id": "msg-1",
                    "message_key": "msg-1",
                    "sender": {
                        "username": "vojtechponrt",
                        "user_id": "self-1",
                        "display_name": "Vojtech Ponrt",
                        "is_self": True,
                    },
                    "sent_at": "2026-03-01T09:00:00Z",
                    "sent_at_label": "09:00",
                    "message_type": "text",
                    "text": "Hey Nina",
                    "status_text": "Delivered",
                    "reply_to_message_id": None,
                    "message_order": 0,
                    "discovered_at": "2026-03-30T12:30:00Z",
                    "last_seen_at": "2026-03-30T12:30:00Z",
                    "attachments": [],
                },
                {
                    "message_id": "msg-2",
                    "message_key": "msg-2",
                    "sender": {
                        "username": "nina.example",
                        "user_id": "user-2",
                        "display_name": "Nina Example",
                        "is_self": False,
                    },
                    "sent_at": "2026-03-01T09:05:00Z",
                    "sent_at_label": "09:05",
                    "message_type": "text",
                    "text": "See you soon",
                    "status_text": None,
                    "reply_to_message_id": None,
                    "message_order": 1,
                    "discovered_at": "2026-03-30T12:30:00Z",
                    "last_seen_at": "2026-03-30T12:30:00Z",
                    "attachments": [
                        {
                            "attachment_id": "att-1",
                            "attachment_order": 0,
                            "file_type": "photo",
                            "mime_type": "image/jpeg",
                            "file_path": "media/instagram/dms/vojtechponrt/thread-1/2026-03-01/message-2/photo_01.jpg",
                            "filename": "photo_01.jpg",
                            "sha256": "abc123",
                            "file_size": 7,
                            "width": 1200,
                            "height": 1600,
                            "duration_s": None,
                            "source_url": "https://cdn.example.test/photo_01.jpg",
                            "preview_text": "Photo attachment",
                            "downloaded_at": "2026-03-30T12:30:00Z",
                            "metadata": {},
                        }
                    ],
                },
            ],
        }

        self._write_json(profile_dir / "profile.json", profile_json)
        self._write_json(profile_dir / "posts.json", posts_json)
        self._write_json(profile_dir / "reposts.json", reposts_json)
        self._write_json(profile_dir / "stories.json", stories_json)
        self._write_json(profile_dir / "connections.json", connections_json)
        self._write_json(profile_dir / "intelligence.json", intelligence_json)
        self._write_json(profile_dir / "graph.json", graph_json)
        self._write_json(profile_dir / "deep_analysis.json", deep_analysis_json)
        for directory, payload, follower_count, following_count in (
            (nina_profile_dir, nina_profile_json, 12, 9),
            (own_profile_dir, own_profile_json, 21, 18),
        ):
            username = payload["username"]
            self._write_json(directory / "profile.json", payload)
            self._write_json(directory / "posts.json", {
                "schema_version": 7,
                "generated_at": payload["generated_at"],
                "platform": "instagram",
                "username": username,
                "items": [],
            })
            self._write_json(directory / "reposts.json", {
                "schema_version": 7,
                "generated_at": payload["generated_at"],
                "platform": "instagram",
                "username": username,
                "items": [],
            })
            self._write_json(directory / "stories.json", {
                "schema_version": 7,
                "generated_at": payload["generated_at"],
                "platform": "instagram",
                "username": username,
                "items": [],
            })
            self._write_json(directory / "connections.json", {
                "schema_version": 7,
                "generated_at": payload["generated_at"],
                "platform": "instagram",
                "username": username,
                "followers": {"snapshot_at": payload["generated_at"], "count": follower_count, "usernames": []},
                "following": {"snapshot_at": payload["generated_at"], "count": following_count, "usernames": []},
            })
            self._write_json(directory / "intelligence.json", {
                "schema_version": 1,
                "generated_at": payload["generated_at"],
                "platform": "instagram",
                "username": username,
            })
            self._write_json(directory / "graph.json", {
                "schema_version": 1,
                "generated_at": payload["generated_at"],
                "platform": "instagram",
                "username": username,
                "format": "networkx-node-link",
                "directed": True,
                "graph": {"name": f"instagram:{username}", "target_node_id": f"instagram:username:{username}"},
                "nodes": [],
                "links": [],
            })
        self._write_json(post_dir / "post.json", sidecar_post)
        self._write_json(highlight_dir / "story.json", sidecar_story)
        self._write_json(account_dir / "account.json", account_json)
        self._write_json(account_dir / "dms" / "index.json", dms_index_json)
        self._write_json(dm_conversation_dir / "conversation.json", conversation_json)
        self._write_json(dm_conversation_dir / "messages.json", messages_json)
        (post_dir / "caption.txt").write_text("Feed post caption", encoding="utf-8")
        (highlight_dir / "story_text.txt").write_text("Highlight transcript", encoding="utf-8")

    def _write_json(self, path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
