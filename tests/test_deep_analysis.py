from __future__ import annotations

import io
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from src import deep_analysis


class DeepAnalysisTests(unittest.TestCase):
    def test_select_mention_entries_keeps_one_media_per_mention_post(self) -> None:
        entries = [
            {
                "source_kind": "mentions",
                "source_item_id": "MENTION1",
                "source_relative_path": "media/instagram/k.dvorakovaa/mentions/2025-01-01/MENTION1/photo_01.jpg",
                "sidecar": {
                    "media_paths": [
                        "media/instagram/k.dvorakovaa/mentions/2025-01-01/MENTION1/photo_01.jpg",
                        "media/instagram/k.dvorakovaa/mentions/2025-01-01/MENTION1/photo_02.jpg",
                    ]
                },
            },
            {
                "source_kind": "mentions",
                "source_item_id": "MENTION1",
                "source_relative_path": "media/instagram/k.dvorakovaa/mentions/2025-01-01/MENTION1/photo_02.jpg",
                "sidecar": {
                    "media_paths": [
                        "media/instagram/k.dvorakovaa/mentions/2025-01-01/MENTION1/photo_01.jpg",
                        "media/instagram/k.dvorakovaa/mentions/2025-01-01/MENTION1/photo_02.jpg",
                    ]
                },
            },
            {
                "source_kind": "posts",
                "source_item_id": "POST1",
                "source_relative_path": "media/instagram/k.dvorakovaa/posts/2025-01-01/POST1/photo_01.jpg",
                "sidecar": {},
            },
        ]

        selected = deep_analysis._select_mention_entries(entries)

        self.assertEqual(len(selected), 2)
        mention_entry = selected[0]
        self.assertEqual(
            mention_entry["source_relative_path"],
            "media/instagram/k.dvorakovaa/mentions/2025-01-01/MENTION1/photo_01.jpg",
        )
        self.assertEqual(
            mention_entry["mention_selection_reason"],
            "fallback_first_media_no_tag_coordinates",
        )
        self.assertIsNone(selected[1]["mention_selection_reason"])

    def test_markdown_report_labels_shared_caption_once(self) -> None:
        payload = {
            "username": "k.dvorakovaa",
            "generated_at": "2026-04-06T10:00:00Z",
            "model": "gemma4:latest",
            "artifact_relative_path": "profiles/instagram/k.dvorakovaa/deep_analysis.json",
            "summary": {
                "scanned_media_files": 3,
                "selected_source_media_files": 2,
                "analyzed_entries": 2,
                "source_images": 2,
                "source_videos": 0,
                "video_frames_analyzed": 0,
                "date_range": {
                    "earliest": "2026-04-01T10:00:00Z",
                    "latest": "2026-04-01T10:00:00Z",
                },
                "overview": "Analyzed 2 visual entries from 2 selected media files.",
                "source_breakdown": [],
                "top_activities": [],
                "top_objects": [],
                "top_brands_or_logos": [],
                "top_style_signals": [],
                "top_location_signals": [],
                "top_recurring_themes": [],
                "top_media_tones": [],
                "social_context_breakdown": [],
            },
            "items": [
                {
                    "source_item_id": "POST1",
                    "source_kind": "posts",
                    "source_relative_path": "media/instagram/k.dvorakovaa/posts/2026-04-01/POST1/photo_01.jpg",
                    "posted_at": "2026-04-01T10:00:00Z",
                    "analysis_media_type": "image",
                    "caption": "Shared carousel caption",
                    "text_context_scope": "shared_post_context",
                    "source_media_count": 2,
                    "mention_selection_reason": None,
                    "analysis": {
                        "visual_summary": "First photo summary.",
                        "activities": ["posing"],
                        "visible_objects": [],
                        "visible_brands_or_logos": [],
                        "fashion_and_style_signals": [],
                        "explicit_location_signals": [],
                        "social_context_visible": {"category": "solo", "notes": ""},
                        "recurring_themes": [],
                        "media_tone": [],
                        "confidence_notes": "High confidence.",
                    },
                },
                {
                    "source_item_id": "POST1",
                    "source_kind": "posts",
                    "source_relative_path": "media/instagram/k.dvorakovaa/posts/2026-04-01/POST1/photo_02.jpg",
                    "posted_at": "2026-04-01T10:00:00Z",
                    "analysis_media_type": "image",
                    "caption": "Shared carousel caption",
                    "text_context_scope": "shared_post_context",
                    "source_media_count": 2,
                    "mention_selection_reason": None,
                    "analysis": {
                        "visual_summary": "Second photo summary.",
                        "activities": ["walking"],
                        "visible_objects": [],
                        "visible_brands_or_logos": [],
                        "fashion_and_style_signals": [],
                        "explicit_location_signals": [],
                        "social_context_visible": {"category": "solo", "notes": ""},
                        "recurring_themes": [],
                        "media_tone": [],
                        "confidence_notes": "High confidence.",
                    },
                },
            ],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = Path(temp_dir) / "report.md"
            deep_analysis._write_markdown_report(report_path, payload)
            report_text = report_path.read_text(encoding="utf-8")

        self.assertEqual(report_text.count("Shared post caption excerpt"), 1)
        self.assertIn("Selected source media files", report_text)
        self.assertIn("First photo summary.", report_text)
        self.assertIn("Second photo summary.", report_text)

    def test_build_payload_marks_checkpoint_runs_as_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            runtime = deep_analysis.RuntimeConfig(
                project_root=base,
                workspace_root=base,
                data_dir=base / "data",
                media_root=base / "data" / "media",
                profile_dir=base / "data" / "profiles" / "instagram" / "k.dvorakovaa",
                report_dir=base / "profiles",
                telegram_bot_token="",
                telegram_chat_id="",
                telegram_general_thread_id=None,
            )
            runtime.profile_dir.mkdir(parents=True, exist_ok=True)
            runtime.report_dir.mkdir(parents=True, exist_ok=True)

            payload = deep_analysis._build_payload(
                runtime=runtime,
                model="gemma4:latest",
                base_url="http://localhost:11434",
                items=[],
                scanned_media_entries=[],
                archive_media_file_count=10,
                prepared_entry_count=25,
                run_limit=None,
            )

        self.assertTrue(payload["is_partial_run"])

    def test_build_payload_uses_runtime_target_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            runtime = deep_analysis.RuntimeConfig(
                project_root=base,
                workspace_root=base,
                data_dir=base / "data",
                media_root=base / "data" / "media" / "tiktok" / "alice.user",
                profile_dir=base / "data" / "profiles" / "tiktok" / "alice.user",
                report_dir=base / "data" / "profiles" / "tiktok" / "alice.user",
                telegram_bot_token="",
                telegram_chat_id="",
                telegram_general_thread_id=None,
                platform="tiktok",
                username="alice.user",
            )
            runtime.profile_dir.mkdir(parents=True, exist_ok=True)

            payload = deep_analysis._build_payload(
                runtime=runtime,
                model="gemma4:latest",
                base_url="http://localhost:11434",
                items=[],
                scanned_media_entries=[],
                archive_media_file_count=0,
                prepared_entry_count=0,
                run_limit=None,
            )

        self.assertEqual(payload["platform"], "tiktok")
        self.assertEqual(payload["username"], "alice.user")
        self.assertEqual(payload["bundle_source_root"], "media/tiktok/alice.user")

    def test_item_artifact_currentness_tracks_source_hashes_not_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            data_dir = base / "data"
            item_dir = data_dir / "media" / "instagram" / "k.dvorakovaa" / "posts" / "2026-04-01" / "POST1"
            item_dir.mkdir(parents=True, exist_ok=True)
            runtime = deep_analysis.RuntimeConfig(
                project_root=base,
                workspace_root=base,
                data_dir=data_dir,
                media_root=data_dir / "media",
                profile_dir=data_dir / "profiles" / "instagram" / "k.dvorakovaa",
                report_dir=data_dir / "profiles" / "instagram" / "k.dvorakovaa",
                telegram_bot_token="",
                telegram_chat_id="",
                telegram_general_thread_id=None,
            )
            runtime.profile_dir.mkdir(parents=True, exist_ok=True)

            selected_entry = {
                "platform": "instagram",
                "target_username": "k.dvorakovaa",
                "source_kind": "posts",
                "source_item_id": "POST1",
                "source_relative_path": "media/instagram/k.dvorakovaa/posts/2026-04-01/POST1/photo_01.jpg",
                "source_media_type": "image",
                "source_sha256": "hash-1",
                "item_dir": item_dir,
                "posted_at": "2026-04-01T10:00:00Z",
                "discovered_at": "2026-04-01T10:00:00Z",
                "collection_name": None,
                "caption": "Shared caption",
                "source_url": "https://example.test/post/POST1",
            }
            group = deep_analysis._build_item_groups(
                [selected_entry],
                [selected_entry],
                data_dir=data_dir,
            )[0]
            payload = deep_analysis._build_item_payload(
                runtime=runtime,
                item_group=group,
                model="gemma4:latest",
                base_url="http://localhost:11434",
                items=[
                    {
                        "entry_id": "entry-1",
                        "source_item_id": "POST1",
                        "source_relative_path": selected_entry["source_relative_path"],
                        "source_media_type": "image",
                        "analysis_media_type": "image",
                        "source_sha256": "hash-1",
                        "analysis": {
                            "visual_summary": "One photo.",
                            "activities": [],
                            "visible_objects": [],
                            "visible_brands_or_logos": [],
                            "fashion_and_style_signals": [],
                            "explicit_location_signals": [],
                            "social_context_visible": {"category": "solo", "notes": ""},
                            "recurring_themes": [],
                            "media_tone": [],
                            "confidence_notes": "",
                        },
                    }
                ],
            )

        self.assertTrue(deep_analysis._item_artifact_matches_current_sources(payload, group))
        self.assertTrue(deep_analysis._item_artifact_is_complete_and_current(payload, group))

        payload["model"] = "some-other-model"
        self.assertTrue(deep_analysis._item_artifact_is_complete_and_current(payload, group))

        payload["prompt_version"] = "older-version"
        self.assertFalse(deep_analysis._item_artifact_is_complete_and_current(payload, group))

        payload["prompt_version"] = deep_analysis.PROMPT_VERSION

        payload["is_partial_run"] = True
        self.assertFalse(deep_analysis._item_artifact_is_complete_and_current(payload, group))

    def test_classify_video_motion_collapses_static_frames(self) -> None:
        def _image_bytes(color: str) -> bytes:
            image = Image.new("RGB", (32, 32), color=color)
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG")
            return buffer.getvalue()

        static_frames = [
            {"frame_index": 1, "frame_offset_s": 0.0, "image_bytes": _image_bytes("red")},
            {"frame_index": 2, "frame_offset_s": 1.0, "image_bytes": _image_bytes("red")},
            {"frame_index": 3, "frame_offset_s": 2.0, "image_bytes": _image_bytes("red")},
        ]
        motion_classification, selected_frames = deep_analysis._classify_video_motion(static_frames)
        self.assertEqual(motion_classification, "static_video_single_frame")
        self.assertEqual(len(selected_frames), 1)

        dynamic_frames = [
            {"frame_index": 1, "frame_offset_s": 0.0, "image_bytes": _image_bytes("red")},
            {"frame_index": 2, "frame_offset_s": 1.0, "image_bytes": _image_bytes("green")},
            {"frame_index": 3, "frame_offset_s": 2.0, "image_bytes": _image_bytes("blue")},
        ]
        motion_classification, selected_frames = deep_analysis._classify_video_motion(dynamic_frames)
        self.assertEqual(motion_classification, "dynamic_video_multi_frame")
        self.assertEqual(len(selected_frames), 3)

    def test_item_markdown_report_is_written_per_media_folder(self) -> None:
        payload = {
            "platform": "instagram",
            "username": "k.dvorakovaa",
            "source_kind": "mentions",
            "source_item_id": "MENTION1",
            "item_relative_dir": "media/instagram/k.dvorakovaa/mentions/2026-04-01/MENTION1",
            "generated_at": "2026-04-06T10:00:00Z",
            "model": "gemma4:latest",
            "artifact_relative_path": "data/media/instagram/k.dvorakovaa/mentions/2026-04-01/MENTION1/analysis.json",
            "representative_entry": {
                "posted_at": "2026-04-01T10:00:00Z",
                "source_url": "https://example.test/post/MENTION1",
            },
            "prepared_entry_count": 1,
            "is_partial_run": False,
            "summary": {
                "scanned_media_files": 3,
                "selected_source_media_files": 1,
                "analyzed_entries": 1,
                "source_images": 1,
                "source_videos": 0,
                "video_frames_analyzed": 0,
                "overview": "Analyzed 1 visual entry from 1 selected media file.",
                "top_activities": [],
                "top_objects": [],
                "top_brands_or_logos": [],
                "top_style_signals": [],
                "top_location_signals": [],
                "top_recurring_themes": [],
                "top_media_tones": [],
                "social_context_breakdown": [],
            },
            "items": [
                {
                    "source_item_id": "MENTION1",
                    "source_relative_path": "media/instagram/k.dvorakovaa/mentions/2026-04-01/MENTION1/photo_01.jpg",
                    "analysis_media_type": "image",
                    "mention_selection_reason": "fallback_first_media_no_tag_coordinates",
                    "caption": "Shared mention caption",
                    "text_context_scope": "shared_post_context",
                    "source_media_count": 3,
                    "analysis": {
                        "visual_summary": "Visible summary.",
                        "activities": ["posing"],
                        "visible_objects": [],
                        "visible_brands_or_logos": [],
                        "fashion_and_style_signals": [],
                        "explicit_location_signals": [],
                        "social_context_visible": {"category": "group", "notes": ""},
                        "recurring_themes": [],
                        "media_tone": [],
                        "confidence_notes": "Cautious.",
                    },
                }
            ],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = Path(temp_dir) / "analysis.md"
            deep_analysis._write_item_markdown_report(report_path, payload)
            report_text = report_path.read_text(encoding="utf-8")

        self.assertIn("Safe Item Analysis", report_text)
        self.assertIn("MENTION1", report_text)
        self.assertIn("Mention selection:", report_text)
        self.assertIn("Structured artifact:", report_text)

    def test_resolve_runtimes_uses_discovered_targets_when_no_selector_is_passed(self) -> None:
        args = Namespace(target=None)
        runtime_one = deep_analysis.RuntimeConfig(
            project_root=Path("/tmp/project"),
            workspace_root=Path("/tmp"),
            data_dir=Path("/tmp/project/data"),
            media_root=Path("/tmp/project/data/media/instagram/alice"),
            profile_dir=Path("/tmp/project/data/profiles/instagram/alice"),
            report_dir=Path("/tmp/project/data/profiles/instagram/alice"),
            telegram_bot_token="",
            telegram_chat_id="",
            telegram_general_thread_id=None,
            platform="instagram",
            username="alice",
        )
        runtime_two = deep_analysis.RuntimeConfig(
            project_root=Path("/tmp/project"),
            workspace_root=Path("/tmp"),
            data_dir=Path("/tmp/project/data"),
            media_root=Path("/tmp/project/data/media/tiktok/bob"),
            profile_dir=Path("/tmp/project/data/profiles/tiktok/bob"),
            report_dir=Path("/tmp/project/data/profiles/tiktok/bob"),
            telegram_bot_token="",
            telegram_chat_id="",
            telegram_general_thread_id=None,
            platform="tiktok",
            username="bob",
        )

        with patch(
            "src.deep_analysis._discover_archived_targets",
            return_value=[("instagram", "alice"), ("tiktok", "bob")],
        ), patch(
            "src.deep_analysis._load_runtime",
            side_effect=[runtime_one, runtime_two],
        ) as load_runtime_mock:
            runtimes = deep_analysis._resolve_runtimes_for_args(args)

        self.assertEqual([(runtime.platform, runtime.username) for runtime in runtimes], [("instagram", "alice"), ("tiktok", "bob")])
        self.assertEqual(load_runtime_mock.call_count, 2)

    def test_upsert_chroma_documents_reports_skipped_when_backend_unavailable(self) -> None:
        payload = {
            "platform": "instagram",
            "username": "k.dvorakovaa",
            "generated_at": "2026-04-06T10:00:00Z",
            "model": "gemma4:latest",
            "artifact_relative_path": "profiles/instagram/k.dvorakovaa/deep_analysis.json",
            "graph_relative_path": "profiles/instagram/k.dvorakovaa/deep_entities_graph.json",
            "summary": {"overview": "Summary text."},
            "items": [
                {
                    "entry_id": "entry-1",
                    "platform": "instagram",
                    "target_username": "k.dvorakovaa",
                    "analysis_media_type": "image",
                    "source_kind": "story_highlights",
                    "source_relative_path": "media/instagram/k.dvorakovaa/story_highlights/2026-03-08/item/photo.jpg",
                    "source_item_id": "item-1",
                    "analysis": {
                        "visual_summary": "Visible summary.",
                        "activities": [],
                        "visible_objects": [],
                        "visible_brands_or_logos": [],
                        "fashion_and_style_signals": [],
                        "explicit_location_signals": [],
                        "location_guesses": [],
                        "hobby_signals": [],
                        "recognizable_public_figures_or_performers": [],
                        "luxury_or_premium_indicators": [],
                        "social_context_visible": {"category": "solo", "notes": ""},
                        "recurring_themes": [],
                        "media_tone": [],
                        "confidence_notes": "",
                    },
                }
            ],
        }

        with patch("src.deep_analysis.chroma_backend_available", return_value=False), patch(
            "src.deep_analysis.upsert_target_profile_document"
        ) as upsert_mock:
            result = deep_analysis._upsert_chroma_documents(payload, Path("/tmp/social-scanner-data"))

        self.assertEqual(result, {"available": False, "attempted": 2, "upserted": 0})
        upsert_mock.assert_not_called()

    def test_upsert_chroma_documents_counts_successful_writes(self) -> None:
        payload = {
            "platform": "instagram",
            "username": "k.dvorakovaa",
            "generated_at": "2026-04-06T10:00:00Z",
            "model": "gemma4:latest",
            "artifact_relative_path": "profiles/instagram/k.dvorakovaa/deep_analysis.json",
            "graph_relative_path": "profiles/instagram/k.dvorakovaa/deep_entities_graph.json",
            "summary": {"overview": "Summary text."},
            "items": [
                {
                    "entry_id": "entry-1",
                    "platform": "instagram",
                    "target_username": "k.dvorakovaa",
                    "analysis_media_type": "image",
                    "source_kind": "story_highlights",
                    "source_relative_path": "media/instagram/k.dvorakovaa/story_highlights/2026-03-08/item/photo_1.jpg",
                    "source_item_id": "item-1",
                    "analysis": {
                        "visual_summary": "Summary one.",
                        "activities": [],
                        "visible_objects": [],
                        "visible_brands_or_logos": [],
                        "fashion_and_style_signals": [],
                        "explicit_location_signals": [],
                        "location_guesses": [],
                        "hobby_signals": [],
                        "recognizable_public_figures_or_performers": [],
                        "luxury_or_premium_indicators": [],
                        "social_context_visible": {"category": "solo", "notes": ""},
                        "recurring_themes": [],
                        "media_tone": [],
                        "confidence_notes": "",
                    },
                },
                {
                    "entry_id": "entry-2",
                    "platform": "instagram",
                    "target_username": "k.dvorakovaa",
                    "analysis_media_type": "image",
                    "source_kind": "story_highlights",
                    "source_relative_path": "media/instagram/k.dvorakovaa/story_highlights/2026-03-08/item/photo_2.jpg",
                    "source_item_id": "item-2",
                    "analysis": {
                        "visual_summary": "Summary two.",
                        "activities": [],
                        "visible_objects": [],
                        "visible_brands_or_logos": [],
                        "fashion_and_style_signals": [],
                        "explicit_location_signals": [],
                        "location_guesses": [],
                        "hobby_signals": [],
                        "recognizable_public_figures_or_performers": [],
                        "luxury_or_premium_indicators": [],
                        "social_context_visible": {"category": "solo", "notes": ""},
                        "recurring_themes": [],
                        "media_tone": [],
                        "confidence_notes": "",
                    },
                },
            ],
        }

        with patch("src.deep_analysis.chroma_backend_available", return_value=True), patch(
            "src.deep_analysis.upsert_target_profile_document",
            side_effect=[True, False, True],
        ) as upsert_mock:
            result = deep_analysis._upsert_chroma_documents(payload, Path("/tmp/social-scanner-data"))

        self.assertEqual(result, {"available": True, "attempted": 3, "upserted": 2})
        self.assertEqual(upsert_mock.call_count, 3)


if __name__ == "__main__":
    unittest.main()
