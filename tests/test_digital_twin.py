from __future__ import annotations

import asyncio
import os
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.config import Config, DigitalTwinConfig
from src.db import (
    create_message_draft,
    get_connection,
    get_message_draft,
    init_db,
    replace_dm_participants,
    set_message_draft_editing,
    update_message_draft_review_message,
    upsert_account,
    upsert_dm_conversation,
    upsert_dm_message,
    upsert_target,
)
from src.digital_twin import (
    approve_draft,
    apply_edit_from_telegram_message,
    maybe_queue_story_initiation_draft,
    queue_inbound_reply_draft,
    select_send_route,
)


class DigitalTwinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_root = Path(self.temp_dir.name)
        self.data_dir = self.project_root / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._write_project_files()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_project_files(self) -> None:
        settings_text = textwrap.dedent(
            f"""
            app:
              data_dir: {self.data_dir}
              log_level: INFO
              dry_run: false

            accounts:
              - platform: instagram
                username: owner
                monitor_own_profile: true

            targets:
              instagram:
                defaults:
                  check_posts: true
                  check_stories: true
                  check_highlights: false
                  check_mentions: false
                  using_account: owner
                items:
                  - nina

            notifications:
              new_post: false
              new_story: false
              follower_gained: false
              follower_lost: false
              following_started: false
              following_stopped: false
              error_alerts: false
              daily_digest: false

            digital_twin:
              enabled: true
              reply_window_hours: 24
              default_model: gemini-1.5-flash
              style_example_count: 4
              profile_context_count: 4
              max_pending_drafts_per_target: 1
              initiation_cooldown_hours: 72
              story_keyword_rules: []
              appropriate_initiation:
                targets:
                  - platform: instagram
                    username: nina
                    priority: high
                    story_keywords: ["coffee"]
                    trigger_categories: ["story_keyword"]
                    cooldown_hours: 72
                    max_pending_drafts: 1
            """
        ).strip()
        env_text = textwrap.dedent(
            """
            TELEGRAM_BOT_TOKEN=test-token
            TELEGRAM_CHAT_ID=-1001
            TELEGRAM_DIGITAL_TWIN_THREAD_ID=99
            INSTAGRAM_PASSWORD_OWNER=secret
            GEMINI_API_KEY=test-gemini
            META_GRAPH_ACCESS_TOKEN=test-graph-token
            META_GRAPH_IG_USER_ID=123456789
            META_GRAPH_API_VERSION=v22.0
            META_GRAPH_BASE_URL=https://graph.instagram.com
            """
        ).strip()
        (self.project_root / "settings.yaml").write_text(settings_text, encoding="utf-8")
        (self.project_root / ".env").write_text(env_text, encoding="utf-8")

    def _build_dm_fixture(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            conn = init_db(self.data_dir / "social_scanner.db")
            account_id = upsert_account(conn, "instagram", "owner", "instagram_owner.json")
            target_id = upsert_target(conn, "instagram", "nina")
            conversation_row_id = upsert_dm_conversation(
                conn,
                account_id,
                {
                    "platform": "instagram",
                    "conversation_id": "thread-1",
                    "conversation_type": "direct",
                    "title": "Nina",
                    "conversation_url": "https://www.instagram.com/direct/t/thread-1/",
                    "last_message_preview": "Hey",
                    "last_message_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "participants": [],
                    "messages": [],
                },
            )
            replace_dm_participants(
                conn,
                conversation_row_id,
                [
                    {
                        "username": "nina",
                        "user_id": "recipient-1",
                        "display_name": "Nina",
                        "is_self": False,
                    },
                    {
                        "username": "owner",
                        "user_id": "viewer-1",
                        "display_name": "Owner",
                        "is_self": True,
                    },
                ],
            )
            upsert_dm_message(
                conn,
                conversation_row_id,
                "instagram",
                {
                    "message_id": "msg-self-1",
                    "message_key": "msg-self-1",
                    "sender_username": "owner",
                    "sender_user_id": "viewer-1",
                    "sender_display_name": "Owner",
                    "sent_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
                    "message_type": "text",
                    "text": "See you later",
                    "is_self": True,
                },
            )
            upsert_dm_message(
                conn,
                conversation_row_id,
                "instagram",
                {
                    "message_id": "msg-inbound-1",
                    "message_key": "msg-inbound-1",
                    "sender_username": "nina",
                    "sender_user_id": "recipient-1",
                    "sender_display_name": "Nina",
                    "sent_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
                    "message_type": "text",
                    "text": "Want to grab coffee later?",
                    "is_self": False,
                },
            )
            conn.close()

    def test_select_send_route_uses_meta_graph_inside_reply_window(self) -> None:
        cfg = Config(
            digital_twin=DigitalTwinConfig(reply_window_hours=24),
            meta_graph_access_token="token",
            meta_graph_ig_user_id="123",
        )
        route, reason = select_send_route(
            cfg,
            trigger_type="inbound_reply",
            latest_inbound_message={
                "sent_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
            },
            target_username="nina",
            recipient_id="recipient-1",
        )
        self.assertEqual(route, "meta_graph")
        self.assertIn("24-hour", reason)

    def test_select_send_route_falls_back_to_playwright_outside_reply_window(self) -> None:
        cfg = Config(
            digital_twin=DigitalTwinConfig(reply_window_hours=24),
            meta_graph_access_token="token",
            meta_graph_ig_user_id="123",
        )
        route, _ = select_send_route(
            cfg,
            trigger_type="inbound_reply",
            latest_inbound_message={
                "sent_at": (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat().replace("+00:00", "Z")
            },
            target_username="nina",
            recipient_id="recipient-1",
        )
        self.assertEqual(route, "playwright_stealth")

    def test_queue_inbound_reply_creates_review_draft_without_sending(self) -> None:
        self._build_dm_fixture()
        draft_payload = {
            "draft_text": "Coffee sounds fun. I can do later this afternoon.",
            "intent_label": "reply",
            "route_hint": "meta_graph",
            "style_notes": "short and warm",
            "context_sources": [{"source_kind": "message", "summary": "Recent inbound DM"}],
            "risk_flags": [],
        }

        with patch.dict(os.environ, {}, clear=True):
            with patch("src.digital_twin._generate_gemini_draft", new=AsyncMock(return_value=draft_payload)):
                with patch("src.digital_twin._post_draft_for_review", new=AsyncMock()) as review_mock:
                    with patch("src.digital_twin.execute_message_route", new=AsyncMock()) as send_mock:
                        draft_id = asyncio.run(
                            queue_inbound_reply_draft(
                                platform="instagram",
                                account_username="owner",
                                conversation_id="thread-1",
                                project_root=self.project_root,
                            )
                        )

        self.assertIsNotNone(draft_id)
        review_mock.assert_awaited_once()
        send_mock.assert_not_awaited()

        conn = get_connection(self.data_dir / "social_scanner.db")
        draft = get_message_draft(conn, int(draft_id))
        conn.close()

        self.assertIsNotNone(draft)
        assert draft is not None
        self.assertEqual(draft["review_status"], "pending_review")
        self.assertEqual(draft["send_status"], "not_sent")
        self.assertEqual(draft["route"], "meta_graph")

    def test_apply_edit_replaces_draft_text_and_returns_to_pending_review(self) -> None:
        self._build_dm_fixture()
        with patch.dict(os.environ, {}, clear=True):
            conn = get_connection(self.data_dir / "social_scanner.db")
            account_row = conn.execute(
                "SELECT id FROM accounts WHERE platform='instagram' AND username='owner'"
            ).fetchone()
            target_row = conn.execute(
                "SELECT id FROM targets WHERE platform='instagram' AND username='nina'"
            ).fetchone()
            conversation_row = conn.execute(
                "SELECT id FROM dm_conversations WHERE external_thread_id='thread-1'"
            ).fetchone()
            draft_id = create_message_draft(
                conn,
                platform="instagram",
                account_id=account_row["id"],
                account_username="owner",
                target_id=target_row["id"],
                target_username="nina",
                conversation_row_id=conversation_row["id"],
                conversation_id="thread-1",
                source_message_id="msg-inbound-1",
                source_story_id=None,
                trigger_type="inbound_reply",
                trigger_payload={"recipient_id": "recipient-1"},
                route="meta_graph",
                route_reason="Inside reply window.",
                prompt_version="test",
                gemini_context={},
                intent_label="reply",
                route_hint="meta_graph",
                style_notes="warm",
                risk_flags=[],
                context_sources=[],
                draft_text="Original draft",
                review_status="pending_review",
            )
            set_message_draft_editing(
                conn,
                draft_id,
                actor_user_id="42",
                actor_username="reviewer",
            )
            update_message_draft_review_message(
                conn,
                draft_id,
                telegram_chat_id="-1001",
                telegram_thread_id=99,
                telegram_message_id=123,
            )
            conn.close()

            with patch("src.digital_twin._post_draft_for_review", new=AsyncMock()) as review_mock:
                updated_id = asyncio.run(
                    apply_edit_from_telegram_message(
                        actor_user_id="42",
                        actor_username="reviewer",
                        edited_text="Updated edited draft",
                        chat_id="-1001",
                        thread_id=99,
                        project_root=self.project_root,
                    )
                )

        self.assertEqual(updated_id, draft_id)
        review_mock.assert_awaited_once()

        conn = get_connection(self.data_dir / "social_scanner.db")
        draft = get_message_draft(conn, draft_id)
        conn.close()

        self.assertEqual(draft["draft_text"], "Updated edited draft")
        self.assertEqual(draft["review_status"], "pending_review")
        self.assertIsNone(draft["editing_user_id"])

    def test_approve_draft_executes_send_route_and_marks_sent(self) -> None:
        self._build_dm_fixture()
        with patch.dict(os.environ, {}, clear=True):
            conn = get_connection(self.data_dir / "social_scanner.db")
            account_row = conn.execute(
                "SELECT id FROM accounts WHERE platform='instagram' AND username='owner'"
            ).fetchone()
            target_row = conn.execute(
                "SELECT id FROM targets WHERE platform='instagram' AND username='nina'"
            ).fetchone()
            conversation_row = conn.execute(
                "SELECT id FROM dm_conversations WHERE external_thread_id='thread-1'"
            ).fetchone()
            draft_id = create_message_draft(
                conn,
                platform="instagram",
                account_id=account_row["id"],
                account_username="owner",
                target_id=target_row["id"],
                target_username="nina",
                conversation_row_id=conversation_row["id"],
                conversation_id="thread-1",
                source_message_id="msg-inbound-1",
                source_story_id=None,
                trigger_type="inbound_reply",
                trigger_payload={"recipient_id": "recipient-1"},
                route="meta_graph",
                route_reason="Inside reply window.",
                prompt_version="test",
                gemini_context={},
                intent_label="reply",
                route_hint="meta_graph",
                style_notes="warm",
                risk_flags=[],
                context_sources=[],
                draft_text="Approved final draft",
                review_status="pending_review",
            )
            update_message_draft_review_message(
                conn,
                draft_id,
                telegram_chat_id="-1001",
                telegram_thread_id=99,
                telegram_message_id=456,
            )
            conn.close()

            with patch("src.digital_twin.disable_digital_twin_controls", new=AsyncMock()) as disable_mock:
                with patch(
                    "src.digital_twin.execute_message_route",
                    new=AsyncMock(
                        return_value={
                            "route": "meta_graph",
                            "conversation_id": "thread-1",
                        }
                    ),
                ) as send_mock:
                    with patch("src.digital_twin.send_digital_twin_result", new=AsyncMock()) as result_mock:
                        approved = asyncio.run(
                            approve_draft(
                                draft_id,
                                actor_user_id="42",
                                actor_username="reviewer",
                                project_root=self.project_root,
                            )
                        )

        self.assertTrue(approved)
        disable_mock.assert_awaited_once()
        send_mock.assert_awaited_once()
        result_mock.assert_awaited_once()

        conn = get_connection(self.data_dir / "social_scanner.db")
        draft = get_message_draft(conn, draft_id)
        conn.close()

        self.assertEqual(draft["review_status"], "sent")
        self.assertEqual(draft["send_status"], "sent")
        self.assertEqual(draft["approved_by_user_id"], "42")

    def test_story_keyword_trigger_creates_pending_review_draft_only(self) -> None:
        self._build_dm_fixture()
        draft_payload = {
            "draft_text": "That coffee spot looked great. Was it somewhere new?",
            "intent_label": "story_outreach",
            "route_hint": "playwright_stealth",
            "style_notes": "light and curious",
            "context_sources": [{"source_kind": "recent_story", "summary": "Story mentioned coffee"}],
            "risk_flags": [],
        }

        with patch.dict(os.environ, {}, clear=True):
            with patch("src.digital_twin._generate_gemini_draft", new=AsyncMock(return_value=draft_payload)):
                with patch("src.digital_twin._post_draft_for_review", new=AsyncMock()) as review_mock:
                    draft_id = asyncio.run(
                        maybe_queue_story_initiation_draft(
                            platform="instagram",
                            account_username="owner",
                            target_username="nina",
                            story_payload={
                                "story_id": "story-1",
                                "url": "https://instagram.example/story-1",
                                "metadata": {"caption": "Coffee before class"},
                            },
                            project_root=self.project_root,
                        )
                    )

        self.assertIsNotNone(draft_id)
        review_mock.assert_awaited_once()

        conn = get_connection(self.data_dir / "social_scanner.db")
        draft = get_message_draft(conn, int(draft_id))
        conn.close()

        self.assertEqual(draft["trigger_type"], "story_keyword")
        self.assertEqual(draft["route"], "playwright_stealth")
        self.assertEqual(draft["review_status"], "pending_review")
        self.assertEqual(draft["send_status"], "not_sent")


if __name__ == "__main__":
    unittest.main()
