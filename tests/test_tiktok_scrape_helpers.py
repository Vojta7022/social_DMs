from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from src.scrapers import tiktok
from src.scrapers.tiktok import (
    _comment_snapshot_from_item_struct,
    _comment_snapshot_from_payloads,
    _follow_list_payload_matches,
    _metadata_from_item_payload,
    _story_metadata_from_payloads,
    _tiktok_comment_errors_require_verification,
    _user_story_status_from_scope,
    _usernames_from_follow_payload,
)


class _FakeFreshPage:
    def __init__(self, context) -> None:
        self.context = context
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeContext:
    def __init__(self) -> None:
        self.pages: list[_FakeFreshPage] = []

    async def new_page(self) -> _FakeFreshPage:
        page = _FakeFreshPage(self)
        self.pages.append(page)
        return page


class _FakeBasePage:
    def __init__(self) -> None:
        self.context = _FakeContext()


class _FakeLocatorElement:
    def __init__(self, *, visible: bool = True, click_raises: Exception | None = None) -> None:
        self.visible = visible
        self.click_raises = click_raises
        self.click_count = 0
        self.forced_click_count = 0

    async def is_visible(self) -> bool:
        return self.visible

    async def scroll_into_view_if_needed(self, timeout: int | None = None) -> None:
        return None

    async def click(self, timeout: int | None = None, force: bool = False) -> None:
        if force:
            self.forced_click_count += 1
        else:
            self.click_count += 1
        if self.click_raises is not None and not force:
            raise self.click_raises


class _FakeLocator:
    def __init__(self, elements: list[_FakeLocatorElement]) -> None:
        self.elements = elements

    async def count(self) -> int:
        return len(self.elements)

    def nth(self, index: int) -> _FakeLocatorElement:
        return self.elements[index]


class _FakeLocatorPage:
    def __init__(self, selectors: dict[str, list[_FakeLocatorElement]]) -> None:
        self.selectors = selectors

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self.selectors.get(selector, []))


class _FakeConfig:
    def __init__(self) -> None:
        self.stealth = object()


class TikTokScrapeHelperTests(unittest.TestCase):
    def test_metadata_from_item_payload_for_normal_post(self) -> None:
        item = {
            "id": "123",
            "desc": "hello world",
            "author": {"uniqueId": "target_user"},
            "stats": {
                "playCount": 100,
                "diggCount": 10,
                "commentCount": 2,
                "shareCount": 1,
            },
            "video": {
                "playAddr": "https://cdn.example/video.mp4",
            },
        }

        metadata = _metadata_from_item_payload(item, "target_user", content_scope="feed")
        assert metadata is not None
        self.assertEqual(metadata["url"], "https://www.tiktok.com/@target_user/video/123")
        self.assertEqual(metadata["caption"], "hello world")
        self.assertEqual(metadata["view_count"], 100)
        self.assertFalse(metadata["is_repost"])
        self.assertIsNone(metadata["repost_source_url"])
        self.assertIsNone(metadata["repost_source_account"])
        self.assertEqual(metadata["posted_at"], None)
        self.assertFalse(metadata["comments_snapshot_complete"])

    def test_metadata_from_item_payload_for_repost(self) -> None:
        item = {
            "id": "999",
            "desc": "reposted",
            "author": {"uniqueId": "target_user"},
            "stats": {},
            "video": {
                "playAddr": "https://cdn.example/repost.mp4",
            },
            "originalItem": {
                "id": "777",
                "author": {"uniqueId": "source_user"},
            },
        }

        metadata = _metadata_from_item_payload(item, "target_user", content_scope="reposts")
        assert metadata is not None
        self.assertTrue(metadata["is_repost"])
        self.assertEqual(metadata["repost_source_account"], "source_user")
        self.assertEqual(metadata["repost_source_url"], "https://www.tiktok.com/@source_user/video/777")

    def test_metadata_from_item_payload_for_photo_carousel(self) -> None:
        item = {
            "id": "555",
            "desc": "photo set",
            "createTime": "1774118839",
            "author": {
                "id": "42",
                "uniqueId": "target_user",
                "nickname": "Target User",
                "avatarMedium": "https://cdn.example/avatar.jpg",
                "verified": True,
            },
            "stats": {
                "playCount": 2463,
                "diggCount": 174,
                "commentCount": 11,
                "shareCount": 5,
            },
            "video": {
                "playAddr": "",
                "downloadAddr": "",
            },
            "imagePost": {
                "images": [
                    {
                        "imageURL": {"urlList": ["https://cdn.example/photo-1.jpg"]},
                        "imageWidth": 1080,
                        "imageHeight": 1440,
                    },
                    {
                        "imageURL": {"urlList": ["https://cdn.example/photo-2.jpg"]},
                        "imageWidth": 1080,
                        "imageHeight": 1440,
                    },
                ],
            },
        }

        metadata = _metadata_from_item_payload(
            item,
            "target_user",
            content_scope="feed",
            raw_payload_key="detail_item_payload",
        )
        assert metadata is not None
        self.assertEqual(metadata["post_type"], "carousel")
        self.assertEqual(
            metadata["posted_at"],
            "2026-03-21T18:47:19Z",
        )
        self.assertEqual(len(metadata["media_items"]), 2)
        self.assertEqual(metadata["media_items"][0]["type"], "photo")
        self.assertEqual(metadata["owner"]["username"], "target_user")
        self.assertTrue(metadata["owner"]["is_verified"])
        self.assertIn("detail_item_payload", metadata["raw_metadata"])

    def test_comment_snapshot_from_payloads_flattens_replies_and_detects_completion(self) -> None:
        payloads = [
            {
                "kind": "list",
                "url": "https://www.tiktok.com/api/comment/list/?cursor=0",
                "payload": {
                    "status_code": 0,
                    "cursor": 1,
                    "has_more": 0,
                    "total": 1,
                    "comments": [
                        {
                            "cid": "top-1",
                            "text": "top level",
                            "create_time": 1774118839,
                            "digg_count": 7,
                            "reply_comment_total": 2,
                            "user": {
                                "uid": "100",
                                "unique_id": "alice",
                                "nickname": "Alice",
                                "avatar_thumb": "https://cdn.example/alice.jpg",
                                "verified": True,
                            },
                            "reply_comment": [
                                {
                                    "cid": "reply-preview",
                                    "text": "preview reply",
                                    "create_time": 1774118840,
                                    "reply_id": "top-1",
                                    "user": {
                                        "uid": "101",
                                        "unique_id": "bob",
                                        "nickname": "Bob",
                                    },
                                }
                            ],
                        }
                    ],
                },
            },
            {
                "kind": "reply",
                "url": "https://www.tiktok.com/api/comment/list/reply/?comment_id=top-1&cursor=1",
                "payload": {
                    "status_code": 0,
                    "cursor": 2,
                    "has_more": 0,
                    "comments": [
                        {
                            "cid": "reply-full",
                            "text": "full reply",
                            "create_time": 1774118841,
                            "reply_id": "top-1",
                            "user": {
                                "uid": "102",
                                "unique_id": "charlie",
                                "nickname": "Charlie",
                            },
                        }
                    ],
                },
            },
        ]

        comments, complete = _comment_snapshot_from_payloads(payloads)
        self.assertTrue(complete)
        self.assertEqual([comment["comment_id"] for comment in comments], ["top-1", "reply-preview", "reply-full"])
        self.assertEqual(comments[1]["parent_comment_id"], "top-1")
        self.assertEqual(comments[2]["root_comment_id"], "top-1")
        self.assertEqual(comments[2]["commented_at"], "2026-03-21T18:47:21Z")

    def test_comment_snapshot_from_item_struct_reads_embedded_comments_and_replies(self) -> None:
        comments = _comment_snapshot_from_item_struct(
            {
                "comments": [
                    {
                        "cid": "top-1",
                        "text": "top level",
                        "create_time": 1774118839,
                        "reply_comment_total": 1,
                        "user": {
                            "uid": "100",
                            "unique_id": "alice",
                            "nickname": "Alice",
                        },
                        "reply_comment": [
                            {
                                "cid": "reply-1",
                                "text": "embedded reply",
                                "create_time": 1774118840,
                                "reply_id": "top-1",
                                "user": {
                                    "uid": "101",
                                    "unique_id": "bob",
                                    "nickname": "Bob",
                                },
                            }
                        ],
                    }
                ]
            }
        )

        self.assertEqual([comment["comment_id"] for comment in comments], ["top-1", "reply-1"])
        self.assertEqual(comments[1]["parent_comment_id"], "top-1")
        self.assertEqual(comments[1]["root_comment_id"], "top-1")

    def test_tiktok_comment_errors_require_verification_from_bdturing_headers(self) -> None:
        self.assertTrue(
            _tiktok_comment_errors_require_verification(
                [
                    {
                        "reason": "empty_body",
                        "headers": {
                            "bdturing-verify": "{\"code\":\"10000\"}",
                        },
                    }
                ]
            )
        )
        self.assertFalse(
            _tiktok_comment_errors_require_verification(
                [{"reason": "empty_body", "headers": {"content-length": "0"}}]
            )
        )

    def test_follow_payload_match_filters_out_wrong_scene_and_secuid(self) -> None:
        matching_url = (
            "https://www.tiktok.com/api/user/list/?scene=21"
            "&secUid=abc123&count=30"
        )
        wrong_scene_url = (
            "https://www.tiktok.com/api/user/list/?scene=151"
            "&secUid=abc123&count=30"
        )
        wrong_secuid_url = (
            "https://www.tiktok.com/api/user/list/?scene=21"
            "&secUid=someone-else&count=30"
        )

        self.assertTrue(_follow_list_payload_matches(matching_url, "abc123", "following"))
        self.assertFalse(_follow_list_payload_matches(wrong_scene_url, "abc123", "following"))
        self.assertFalse(_follow_list_payload_matches(wrong_secuid_url, "abc123", "following"))

    def test_usernames_from_follow_payload_reads_nested_users(self) -> None:
        payload = {
            "userList": [
                {"user": {"uniqueId": "alice"}},
                {"user": {"uniqueId": "bob"}},
                {"user": {"uniqueId": "alice"}},
                {},
            ]
        }

        self.assertEqual(_usernames_from_follow_payload(payload), {"alice", "bob"})

    def test_user_story_status_from_scope_reads_embedded_flag(self) -> None:
        scope = {
            "webapp.user-detail": {
                "userInfo": {
                    "user": {
                        "UserStoryStatus": 1,
                    }
                }
            }
        }

        self.assertTrue(_user_story_status_from_scope(scope))
        self.assertFalse(_user_story_status_from_scope({}))

    def test_story_metadata_from_payloads_reads_story_api_video_item(self) -> None:
        payloads = [
            {
                "itemList": [
                    {
                        "id": "7626099403710991638",
                        "createTime": 1775589637,
                        "desc": "story test",
                        "author": {
                            "id": "6941836821194720262",
                            "uniqueId": "dvorakova.k",
                            "nickname": "dvorakovaa",
                            "avatarLarger": "https://cdn.example/avatar.jpg",
                            "verified": False,
                        },
                        "story": {
                            "ExpiredAt": 1775676037000,
                            "Viewed": False,
                        },
                        "music": {
                            "id": "7113444778213231362",
                            "title": "original sound",
                            "authorName": "playlist",
                            "playUrl": "https://cdn.example/audio.mp3",
                        },
                        "video": {
                            "playAddr": "https://cdn.example/story-primary.mp4",
                            "cover": "https://cdn.example/story-cover.jpg",
                            "width": 576,
                            "height": 1024,
                            "bitrate": 872460,
                            "PlayAddrStruct": {
                                "Width": 576,
                                "Height": 1024,
                                "UrlList": [
                                    "https://cdn.example/story-primary.mp4",
                                    "https://cdn.example/story-backup.mp4",
                                ],
                            },
                        },
                    }
                ]
            }
        ]

        stories = _story_metadata_from_payloads(
            payloads,
            "dvorakova.k",
            raw_payload_key="story_api_payload",
        )
        self.assertEqual(len(stories), 1)
        story = stories[0]
        self.assertEqual(story["story_id"], "7626099403710991638")
        self.assertEqual(story["story_type"], "video")
        self.assertEqual(story["url"], "https://cdn.example/story-primary.mp4")
        self.assertEqual(story["posted_at"], "2026-04-07T19:20:37Z")
        self.assertEqual(story["expires_at"], "2026-04-08T19:20:37Z")
        self.assertEqual(
            story["story_permalink"],
            "https://www.tiktok.com/@dvorakova.k/story/7626099403710991638",
        )
        self.assertTrue(story["metadata"]["audio_expected"])
        self.assertEqual(story["metadata"]["caption"], "story test")
        self.assertIn("story_api_payload", story["metadata"])
        self.assertEqual(
            [candidate["kind"] for candidate in story["metadata"]["media_candidates"][:2]],
            ["video", "video"],
        )


class TikTokConnectionPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_click_first_visible_selector_skips_hidden_matches(self) -> None:
        hidden = _FakeLocatorElement(visible=False)
        visible = _FakeLocatorElement(visible=True)
        page = _FakeLocatorPage(
            {"strong[data-e2e='comment-count']": [hidden, visible]}
        )

        with patch.object(tiktok, "short_delay", new=AsyncMock()):
            clicked = await tiktok._click_first_visible_selector(
                page,
                ["strong[data-e2e='comment-count']"],
                _FakeConfig(),
            )

        self.assertTrue(clicked)
        self.assertEqual(hidden.click_count, 0)
        self.assertEqual(visible.click_count, 1)

    async def test_extract_comments_uses_embedded_snapshot_when_api_payload_is_missing(self) -> None:
        post = {
            "comment_count": 2,
            "raw_metadata": {
                "detail_item_payload": {
                    "comments": [
                        {
                            "cid": "top-1",
                            "text": "top level",
                            "create_time": 1774118839,
                            "reply_comment_total": 1,
                            "user": {
                                "uid": "100",
                                "unique_id": "alice",
                                "nickname": "Alice",
                            },
                            "reply_comment": [
                                {
                                    "cid": "reply-1",
                                    "text": "embedded reply",
                                    "create_time": 1774118840,
                                    "reply_id": "top-1",
                                    "user": {
                                        "uid": "101",
                                        "unique_id": "bob",
                                        "nickname": "Bob",
                                    },
                                }
                            ],
                        }
                    ]
                }
            },
        }

        with patch.object(
            tiktok,
            "_capture_comment_payloads",
            new=AsyncMock(return_value=([], [])),
        ), patch.object(
            tiktok,
            "_detect_captcha",
            new=AsyncMock(return_value=False),
        ):
            comments, state = await tiktok._extract_comments(object(), post, _FakeConfig())

        assert comments is not None
        self.assertEqual([comment["comment_id"] for comment in comments], ["top-1", "reply-1"])
        self.assertFalse(state["comments_snapshot_complete"])
        self.assertEqual(
            state["comments_unavailable_reason"],
            "TikTok only exposed a partial embedded comment snapshot on the detail page.",
        )

    async def test_extract_comments_reports_verification_block_when_headers_require_it(self) -> None:
        post = {
            "comment_count": 26,
            "raw_metadata": {"detail_item_payload": {"comments": []}},
        }

        with patch.object(
            tiktok,
            "_capture_comment_payloads",
            new=AsyncMock(
                return_value=(
                    [],
                    [
                        {
                            "kind": "list",
                            "reason": "empty_body",
                            "headers": {
                                "bdturing-verify": "{\"code\":\"10000\"}",
                            },
                        }
                    ],
                )
            ),
        ), patch.object(
            tiktok,
            "_detect_captcha",
            new=AsyncMock(return_value=False),
        ), patch.object(
            tiktok,
            "_read_universal_scope",
            new=AsyncMock(return_value={}),
        ):
            comments, state = await tiktok._extract_comments(object(), post, _FakeConfig())

        self.assertIsNone(comments)
        self.assertFalse(state["comments_snapshot_complete"])
        self.assertEqual(
            state["comments_unavailable_reason"],
            "TikTok required a verification challenge while loading comments.",
        )

    async def test_target_connections_use_fresh_pages_for_each_list(self) -> None:
        base_page = _FakeBasePage()

        with patch.object(
            tiktok,
            "_scrape_follow_list",
            new=AsyncMock(side_effect=[["follower_1"], ["following_1"]]),
        ) as scrape_mock:
            result = await tiktok.scrape_target_connections(base_page, "target_user", None, object())

        self.assertEqual(
            result,
            {
                "followers": ["follower_1"],
                "following": ["following_1"],
                "followers_available": True,
                "following_available": True,
                "errors": {},
            },
        )
        self.assertEqual(scrape_mock.await_count, 2)
        first_page = scrape_mock.await_args_list[0].args[0]
        second_page = scrape_mock.await_args_list[1].args[0]
        self.assertIsNot(first_page, second_page)
        self.assertTrue(first_page.closed)
        self.assertTrue(second_page.closed)

    async def test_target_connections_keep_partial_results_when_one_list_fails(self) -> None:
        base_page = _FakeBasePage()

        with patch.object(
            tiktok,
            "_scrape_follow_list_in_fresh_page",
            new=AsyncMock(side_effect=[["follower_1"], RuntimeError("following hidden")]),
        ):
            result = await tiktok.scrape_target_connections(base_page, "target_user", None, object())

        self.assertEqual(result["followers"], ["follower_1"])
        self.assertTrue(result["followers_available"])
        self.assertEqual(result["following"], [])
        self.assertFalse(result["following_available"])
        self.assertIn("following", result["errors"])

    async def test_account_follow_scrape_saves_only_available_lists(self) -> None:
        page = object()

        with patch.object(
            tiktok,
            "_scrape_connection_lists_best_effort",
            new=AsyncMock(
                return_value={
                    "followers": ["follower_1"],
                    "following": [],
                    "followers_available": True,
                    "following_available": False,
                    "errors": {"following": "hidden"},
                }
            ),
        ), patch("src.db.get_account_id", return_value=42), patch(
            "src.db.save_follower_snapshot"
        ) as save_snapshot, patch(
            "src.db.diff_follower_snapshots", return_value=[]
        ) as diff_snapshots, patch(
            "src.db.insert_follower_events"
        ) as insert_events:
            result = await tiktok.scrape_followers(page, "own_user", None, object())

        self.assertTrue(result["followers_available"])
        self.assertFalse(result["following_available"])
        self.assertEqual(save_snapshot.call_count, 1)
        self.assertEqual(save_snapshot.call_args.args[1:], (42, "followers", ["follower_1"]))
        self.assertEqual(diff_snapshots.call_count, 1)
        self.assertEqual(diff_snapshots.call_args.args[1:], (42, "followers"))
        insert_events.assert_not_called()


if __name__ == "__main__":
    unittest.main()
