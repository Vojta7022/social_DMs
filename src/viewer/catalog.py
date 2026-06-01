"""Read-only catalog builder for the local archive viewer."""

from __future__ import annotations

import json
import math
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from src.utils import platform_profile_url as _shared_platform_profile_url

DEFAULT_PROFILE_FILES = {
    "posts": "posts.json",
    "reposts": "reposts.json",
    "stories": "stories.json",
    "connections": "connections.json",
    "intelligence": "intelligence.json",
    "graph": "graph.json",
    "deep_analysis": "deep_analysis.json",
}
DEFAULT_ACCOUNT_FILES = {
    "dms_index": "dms/index.json",
}

POST_KINDS = {"posts", "mentions", "reposts"}
STORY_KINDS = {"stories", "highlights"}


@dataclass(frozen=True)
class CachedBundle:
    signature: tuple[tuple[str, int, int], ...]
    payload: dict[str, Any]


def _platform_profile_url(platform: str, username: str) -> str | None:
    return _shared_platform_profile_url(platform, username)


class ArchiveCatalog:
    """Load profile export bundles and enrich them for HTML rendering."""

    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root
        self.profiles_root = data_root / "profiles"
        self.accounts_root = data_root / "accounts"
        self.media_root = data_root / "media"
        self._bundle_cache: dict[Path, CachedBundle] = {}
        self._account_cache: dict[Path, CachedBundle] = {}

    def list_profiles(
        self,
        platform: str | None = None,
        search: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return every available profile summary, filtered and sorted."""
        search_token = (search or "").strip().lower()
        summaries: list[dict[str, Any]] = []
        for bundle_dir in self._bundle_dirs():
            profile = self._load_bundle(bundle_dir)
            if not profile:
                continue
            if platform and profile["platform"] != platform:
                continue
            if search_token and not self._matches_search(profile, search_token):
                continue
            summaries.append(self._summary_view(profile))
        return sorted(
            summaries,
            key=lambda item: (
                item["platform"],
                item["display_name"].lower(),
                item["username"].lower(),
            ),
        )

    def available_platforms(self) -> list[str]:
        """Return platforms currently present in the archive."""
        platforms = {
            profile["platform"]
            for profile in (
                self._load_bundle(bundle_dir) for bundle_dir in self._bundle_dirs()
            )
            if profile
        }
        return sorted(platforms)

    def available_account_platforms(self) -> list[str]:
        """Return platforms currently present in owned-account archives."""
        platforms = {
            account["platform"]
            for account in (
                self._load_account_bundle(bundle_dir) for bundle_dir in self._account_dirs()
            )
            if account
        }
        return sorted(platforms)

    def list_accounts(
        self,
        platform: str | None = None,
        search: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return every available owned-account summary, filtered and sorted."""
        search_token = (search or "").strip().lower()
        summaries: list[dict[str, Any]] = []
        for bundle_dir in self._account_dirs():
            account = self._load_account_bundle(bundle_dir)
            if not account:
                continue
            if platform and account["platform"] != platform:
                continue
            if search_token and not self._matches_account_search(account, search_token):
                continue
            summaries.append(self._account_summary_view(account))
        return sorted(
            summaries,
            key=lambda item: (
                item["platform"],
                -self._timestamp_sort_key(item.get("last_message_at")),
                item["username"].lower(),
            ),
        )

    def search_items(
        self,
        platform: str | None = None,
        search: str | None = None,
        *,
        limit: int = 40,
    ) -> list[dict[str, Any]]:
        """Return archived items whose IDs or source URLs match the query."""
        search_token = (search or "").strip().lower()
        if not search_token:
            return []

        matches: list[dict[str, Any]] = []
        for bundle_dir in self._bundle_dirs():
            profile = self._load_bundle(bundle_dir)
            if not profile:
                continue
            if platform and profile["platform"] != platform:
                continue
            for kind, collection in profile.get("collections", {}).items():
                if not isinstance(collection, list):
                    continue
                for item in collection:
                    if not isinstance(item, dict):
                        continue
                    match_score = self._item_search_score(item, search_token)
                    if match_score is None:
                        continue
                    matches.append(self._item_search_view(profile, item, match_score))

        if any(int(item.get("match_score", 99)) == 0 for item in matches):
            matches = [item for item in matches if int(item.get("match_score", 99)) == 0]
        elif search_token.startswith("http") and any(int(item.get("match_score", 99)) == 1 for item in matches):
            matches = [item for item in matches if int(item.get("match_score", 99)) == 1]

        matches.sort(
            key=lambda item: (
                int(item.get("match_score", 99)),
                -self._timestamp_sort_key(item.get("posted_at") or item.get("discovered_at")),
                str(item.get("platform") or "").casefold(),
                str(item.get("username") or "").casefold(),
                str(item.get("kind") or "").casefold(),
            )
        )
        return matches[: max(1, limit)]

    def get_profile(self, platform: str, username: str) -> dict[str, Any] | None:
        """Return one fully normalized profile bundle."""
        for bundle_dir in self._bundle_dirs():
            profile = self._load_bundle(bundle_dir)
            if not profile:
                continue
            if profile["platform"] == platform and profile["username"] == username:
                return deepcopy(profile)
        return None

    def get_account(self, platform: str, username: str) -> dict[str, Any] | None:
        """Return one fully normalized owned-account DM bundle."""
        for bundle_dir in self._account_dirs():
            account = self._load_account_bundle(bundle_dir)
            if not account:
                continue
            if account["platform"] == platform and account["username"] == username:
                return deepcopy(account)
        return None

    def list_conversations(
        self,
        platform: str | None = None,
        search: str | None = None,
        *,
        account_username: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return normalized DM conversation summaries across account archives."""
        search_token = (search or "").strip().lower()
        account_token = (account_username or "").strip().lower()
        matches: list[dict[str, Any]] = []

        for bundle_dir in self._account_dirs():
            account = self._load_account_bundle(bundle_dir)
            if not account:
                continue
            if platform and account["platform"] != platform:
                continue
            if account_token and account["username"].lower() != account_token:
                continue
            for conversation in account.get("conversations", []):
                if search_token and not self._matches_conversation_search(account, conversation, search_token):
                    continue
                matches.append(deepcopy(conversation))

        matches.sort(
            key=lambda item: (
                -self._timestamp_sort_key(item.get("last_message_at")),
                str(item.get("account_username") or "").casefold(),
                str(item.get("display_title") or item.get("conversation_id") or "").casefold(),
            )
        )
        if limit is None:
            return matches
        return matches[: max(1, limit)]

    def find_conversations_for_profile(
        self,
        platform: str,
        username: str,
        *,
        limit: int | None = 12,
    ) -> list[dict[str, Any]]:
        """Return conversations whose participant list includes the given profile username."""
        username_token = str(username or "").strip().casefold()
        if not username_token:
            return []

        matches = [
            conversation
            for conversation in self.list_conversations(platform=platform, limit=None)
            if username_token in {
                str(participant or "").strip().casefold()
                for participant in conversation.get("participant_usernames", [])
            }
        ]
        if limit is None:
            return matches
        return matches[: max(1, limit)]

    def get_conversation(
        self,
        platform: str,
        username: str,
        conversation_id: str,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Return one account bundle plus a fully normalized DM conversation."""
        account = self.get_account(platform, username)
        if not account:
            return None, None

        summary = next(
            (
                item for item in account.get("conversations", [])
                if item.get("conversation_id") == conversation_id
            ),
            None,
        )
        if not summary:
            return account, None

        bundle_dir = Path(account.get("bundle_dir") or "")
        dms_index_file = str((account.get("files") or {}).get("dms_index") or "dms/index.json")
        dms_root = (bundle_dir / dms_index_file).parent
        conversation_payload = self._read_json(dms_root / summary["files"]["conversation"])
        messages_payload = self._read_json(dms_root / summary["files"]["messages"])
        if not isinstance(conversation_payload, dict):
            conversation_payload = {}
        if not isinstance(messages_payload, dict):
            messages_payload = {}

        conversation = self._normalize_dm_conversation_detail(
            account=account,
            summary=summary,
            conversation_payload=conversation_payload,
            messages_payload=messages_payload,
        )
        return account, conversation

    def get_item_detail(
        self,
        platform: str,
        username: str,
        kind: str,
        item_id: str,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Return a profile bundle plus one enriched item detail."""
        profile = self.get_profile(platform, username)
        if not profile:
            return None, None

        collection = profile["collections"].get(kind, [])
        for item in collection:
            if item["item_id"] == item_id:
                detail = deepcopy(item)
                sidecar = self._load_item_sidecar(detail)
                detail["sidecar"] = sidecar
                detail["raw_metadata"] = sidecar or detail["raw"]
                detail["text_blob"] = self._load_text_blob(detail)
                if sidecar:
                    detail["caption"] = sidecar.get("caption") or detail.get("caption")
                    detail["comments"] = sidecar.get("comments") or detail.get("comments", [])
                    detail["comment_tree"] = self._normalize_comment_tree(
                        sidecar.get("comment_tree") or detail.get("comment_tree", [])
                    )
                    detail["comment_history"] = (
                        sidecar.get("comment_history")
                        or detail.get("comment_history", [])
                    )
                    detail["comment_history_tree"] = self._normalize_comment_tree(
                        sidecar.get("comment_history_tree") or detail.get("comment_history_tree", [])
                    )
                    media_assets = self._media_assets_from_sidecar(sidecar)
                    if media_assets:
                        detail["media_assets"] = media_assets
                        detail["preview_media"] = self._preferred_preview_media(
                            media_assets,
                            metadata=sidecar.get("metadata"),
                            prefer_remote_image=detail["kind"] in STORY_KINDS,
                        )
                    detail["extra_media_assets"] = self._metadata_image_candidates(
                        sidecar,
                        existing_assets=detail.get("media_assets", []),
                    )
                else:
                    detail["extra_media_assets"] = []
                return profile, detail
        return profile, None

    def _bundle_dirs(self) -> list[Path]:
        if not self.profiles_root.exists():
            return []
        bundle_dirs: list[Path] = []
        for platform_dir in sorted(self.profiles_root.iterdir()):
            if not platform_dir.is_dir():
                continue
            for bundle_dir in sorted(platform_dir.iterdir()):
                if bundle_dir.is_dir():
                    bundle_dirs.append(bundle_dir)
        return bundle_dirs

    def _account_dirs(self) -> list[Path]:
        if not self.accounts_root.exists():
            return []
        bundle_dirs: list[Path] = []
        for platform_dir in sorted(self.accounts_root.iterdir()):
            if not platform_dir.is_dir():
                continue
            for bundle_dir in sorted(platform_dir.iterdir()):
                if bundle_dir.is_dir():
                    bundle_dirs.append(bundle_dir)
        return bundle_dirs

    def _load_bundle(self, bundle_dir: Path) -> dict[str, Any] | None:
        profile_path = bundle_dir / "profile.json"
        if not profile_path.exists():
            return None

        profile_payload = self._read_json(profile_path)
        if not isinstance(profile_payload, dict):
            return None

        files = dict(DEFAULT_PROFILE_FILES)
        profile_files = profile_payload.get("files")
        if isinstance(profile_files, dict):
            files.update(
                {
                    key: value
                    for key, value in profile_files.items()
                    if key in files and isinstance(value, str)
                }
            )

        file_paths = {
            "profile": profile_path,
            "posts": bundle_dir / files["posts"],
            "reposts": bundle_dir / files["reposts"],
            "stories": bundle_dir / files["stories"],
            "connections": bundle_dir / files["connections"],
            "intelligence": bundle_dir / files["intelligence"],
            "graph": bundle_dir / files["graph"],
            "deep_analysis": bundle_dir / files["deep_analysis"],
        }
        signature = self._signature(file_paths)
        cached = self._bundle_cache.get(bundle_dir)
        if cached and cached.signature == signature:
            return cached.payload

        posts_payload = self._read_json(file_paths["posts"])
        reposts_payload = self._read_json(file_paths["reposts"])
        stories_payload = self._read_json(file_paths["stories"])
        connections_payload = self._read_json(file_paths["connections"])
        intelligence_payload = self._read_json(file_paths["intelligence"])
        graph_payload = self._read_json(file_paths["graph"])
        deep_analysis_payload = self._read_json(file_paths["deep_analysis"])

        normalized = self._normalize_bundle(
            bundle_dir=bundle_dir,
            profile_payload=profile_payload,
            posts_payload=posts_payload,
            reposts_payload=reposts_payload,
            stories_payload=stories_payload,
            connections_payload=connections_payload,
            intelligence_payload=intelligence_payload,
            graph_payload=graph_payload,
            deep_analysis_payload=deep_analysis_payload,
        )
        self._bundle_cache[bundle_dir] = CachedBundle(signature=signature, payload=normalized)
        return normalized

    def _load_account_bundle(self, bundle_dir: Path) -> dict[str, Any] | None:
        account_path = bundle_dir / "account.json"
        if not account_path.exists():
            return None

        account_payload = self._read_json(account_path)
        if not isinstance(account_payload, dict):
            return None

        files = dict(DEFAULT_ACCOUNT_FILES)
        account_files = account_payload.get("files")
        if isinstance(account_files, dict):
            files.update(
                {
                    key: value
                    for key, value in account_files.items()
                    if key in files and isinstance(value, str)
                }
            )

        file_paths = {
            "account": account_path,
            "dms_index": bundle_dir / files["dms_index"],
        }
        signature = self._signature(file_paths)
        cached = self._account_cache.get(bundle_dir)
        if cached and cached.signature == signature:
            return cached.payload

        dms_index_payload = self._read_json(file_paths["dms_index"])
        normalized = self._normalize_account_bundle(
            bundle_dir=bundle_dir,
            account_payload=account_payload,
            dms_index_payload=dms_index_payload if isinstance(dms_index_payload, dict) else {},
            dms_index_file=files["dms_index"],
        )
        self._account_cache[bundle_dir] = CachedBundle(signature=signature, payload=normalized)
        return normalized

    def _normalize_bundle(
        self,
        bundle_dir: Path,
        profile_payload: dict[str, Any],
        posts_payload: Any,
        reposts_payload: Any,
        stories_payload: Any,
        connections_payload: Any,
        intelligence_payload: Any,
        graph_payload: Any,
        deep_analysis_payload: Any,
    ) -> dict[str, Any]:
        platform = str(profile_payload.get("platform") or bundle_dir.parent.name)
        username = str(profile_payload.get("username") or bundle_dir.name)
        bio = profile_payload.get("bio") if isinstance(profile_payload.get("bio"), dict) else {}

        posts_items = self._items(posts_payload)
        repost_items = self._items(reposts_payload)
        story_items = self._items(stories_payload)

        feed_posts = [
            self._normalize_post_item(platform, username, "posts", item)
            for item in posts_items
            if item.get("content_scope") not in {"mentions", "reposts"}
        ]
        mentions = [
            self._normalize_post_item(platform, username, "mentions", item)
            for item in posts_items
            if item.get("content_scope") == "mentions"
        ]
        if not repost_items:
            repost_items = [item for item in posts_items if item.get("content_scope") == "reposts"]
        reposts = [
            self._normalize_post_item(platform, username, "reposts", item)
            for item in repost_items
        ]
        stories = [
            self._normalize_story_item(platform, username, "stories", item)
            for item in story_items
            if item.get("content_scope") != "highlight"
        ]
        highlights = [
            self._normalize_story_item(platform, username, "highlights", item)
            for item in story_items
            if item.get("content_scope") == "highlight"
        ]

        collections = {
            "posts": feed_posts,
            "mentions": mentions,
            "reposts": reposts,
            "stories": stories,
            "highlights": highlights,
        }
        overview_items = self._overview_items(collections)
        connections = self._normalize_connections(
            connections_payload if isinstance(connections_payload, dict) else {}
        )
        graph = self._normalize_graph(graph_payload if isinstance(graph_payload, dict) else {})
        counts = {
            "posts": len(feed_posts),
            "mentions": len(mentions),
            "reposts": len(reposts),
            "stories": len(stories),
            "highlights": len(highlights),
        }
        counts["followers"] = connections["followers"].get("count", 0)
        counts["following"] = connections["following"].get("count", 0)

        return {
            "platform": platform,
            "username": username,
            "display_name": bio.get("display_name") or username,
            "generated_at": profile_payload.get("generated_at"),
            "is_own_account": bool(profile_payload.get("is_own_account")),
            "bio": bio,
            "followers": profile_payload.get("followers") if isinstance(profile_payload.get("followers"), dict) else {},
            "following": profile_payload.get("following") if isinstance(profile_payload.get("following"), dict) else {},
            "media_summary": profile_payload.get("media_summary") if isinstance(profile_payload.get("media_summary"), dict) else {},
            "archive_counts": profile_payload.get("archive_counts") if isinstance(profile_payload.get("archive_counts"), dict) else {},
            "bundle_dir": str(bundle_dir),
            "collections": collections,
            "overview_items": overview_items,
            "connections": connections,
            "graph": graph,
            "deep_profile": self._normalize_deep_profile(
                deep_analysis_payload if isinstance(deep_analysis_payload, dict) else {}
            ),
            "intelligence": self._normalize_intelligence(
                intelligence_payload if isinstance(intelligence_payload, dict) else {}
            ),
            "counts": counts,
            "files": profile_payload.get("files") if isinstance(profile_payload.get("files"), dict) else {},
            "raw_profile": profile_payload,
        }

    def _normalize_account_bundle(
        self,
        *,
        bundle_dir: Path,
        account_payload: dict[str, Any],
        dms_index_payload: dict[str, Any],
        dms_index_file: str,
    ) -> dict[str, Any]:
        platform = str(account_payload.get("platform") or bundle_dir.parent.name)
        username = str(account_payload.get("username") or bundle_dir.name)
        linked_profile = self.get_profile(platform, username)
        linked_bio = linked_profile.get("bio") if linked_profile else {}
        conversations = [
            self._normalize_dm_index_item(platform, username, item)
            for item in self._items(dms_index_payload)
        ]
        conversations.sort(
            key=lambda item: (
                -self._timestamp_sort_key(item.get("last_message_at")),
                str(item.get("display_title") or item.get("conversation_id") or "").casefold(),
            )
        )

        archive_counts = (
            account_payload.get("archive_counts")
            if isinstance(account_payload.get("archive_counts"), dict)
            else {}
        )
        last_message_at = conversations[0]["last_message_at"] if conversations else None
        last_scraped_at = max(
            (conversation.get("last_scraped_at") for conversation in conversations if conversation.get("last_scraped_at")),
            default=None,
            key=self._timestamp_sort_key,
        )

        counts = {
            "conversations": int(
                archive_counts.get("conversations", dms_index_payload.get("conversation_count", len(conversations))) or 0
            ),
            "messages": int(
                archive_counts.get("messages", dms_index_payload.get("message_count", 0)) or 0
            ),
            "attachments": int(
                archive_counts.get("attachments", dms_index_payload.get("attachment_count", 0)) or 0
            ),
        }

        return {
            "platform": platform,
            "username": username,
            "display_name": (
                linked_profile.get("display_name")
                if linked_profile
                else account_payload.get("display_name")
                or username
            ),
            "generated_at": account_payload.get("generated_at") or dms_index_payload.get("generated_at"),
            "bundle_dir": str(bundle_dir),
            "archive_counts": counts,
            "conversation_count": len(conversations),
            "conversations": conversations,
            "recent_conversations": conversations[:8],
            "last_message_at": last_message_at,
            "last_scraped_at": last_scraped_at,
            "bio_text": linked_bio.get("bio_text") if isinstance(linked_bio, dict) else None,
            "profile_pic_url": linked_bio.get("profile_pic_url") if isinstance(linked_bio, dict) else None,
            "bio_post_count": linked_bio.get("post_count") if isinstance(linked_bio, dict) else None,
            "bio_follower_count": linked_bio.get("follower_count") if isinstance(linked_bio, dict) else None,
            "bio_following_count": linked_bio.get("following_count") if isinstance(linked_bio, dict) else None,
            "linked_profile": {
                "available": bool(linked_profile),
                "display_name": linked_profile.get("display_name") if linked_profile else None,
                "detail_url": f"/profiles/{platform}/{username}" if linked_profile else None,
                "generated_at": linked_profile.get("generated_at") if linked_profile else None,
                "counts": linked_profile.get("counts") if linked_profile else {},
            },
            "files": {
                "dms_index": dms_index_file,
            },
            "raw_account": account_payload,
            "raw_index": dms_index_payload,
        }

    def _normalize_connections(self, payload: dict[str, Any]) -> dict[str, Any]:
        followers = self._normalize_connection_snapshot(payload.get("followers"))
        following = self._normalize_connection_snapshot(payload.get("following"))
        followers_history = self._normalize_connection_history(payload.get("followers_history"))
        following_history = self._normalize_connection_history(payload.get("following_history"))
        follower_delta_log = payload.get("follower_delta_log")
        following_delta_log = payload.get("following_delta_log")
        return {
            "followers": followers,
            "following": following,
            "followers_history": followers_history,
            "following_history": following_history,
            "follower_delta_log": follower_delta_log if isinstance(follower_delta_log, list) else [],
            "following_delta_log": following_delta_log if isinstance(following_delta_log, list) else [],
            "raw": payload,
        }

    def _normalize_intelligence(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {}

        forecast_payload = payload.get("next_post_prediction")
        if not isinstance(forecast_payload, dict):
            forecast_payload = {}
        prediction = forecast_payload.get("prediction")
        if not isinstance(prediction, dict):
            prediction = {}
        basis = forecast_payload.get("basis")
        if not isinstance(basis, dict):
            basis = {}
        patterns = forecast_payload.get("patterns")
        if not isinstance(patterns, dict):
            patterns = {}
        notes = forecast_payload.get("notes")
        if not isinstance(notes, list):
            notes = []

        sentiment = payload.get("sentiment")
        if not isinstance(sentiment, dict):
            sentiment = payload.get("sentiment_overview")
        if not isinstance(sentiment, dict):
            sentiment = {}

        geoparsed_locations = payload.get("geoparsed_locations")
        if not isinstance(geoparsed_locations, dict):
            geoparsed_locations = {}

        linked_profiles = payload.get("linked_profiles")
        if not isinstance(linked_profiles, dict):
            linked_profiles = {}

        repost_analytics = payload.get("repost_analytics")
        if not isinstance(repost_analytics, dict):
            repost_analytics = {}

        return {
            "schema_version": payload.get("schema_version"),
            "generated_at": payload.get("generated_at"),
            "platform": payload.get("platform"),
            "username": payload.get("username"),
            "next_post_prediction": {
                "status": forecast_payload.get("status"),
                "generated_at": forecast_payload.get("generated_at"),
                "predicted_at": forecast_payload.get("predicted_at") or prediction.get("predicted_at"),
                "window_start": forecast_payload.get("window_start") or prediction.get("window_start"),
                "window_end": forecast_payload.get("window_end") or prediction.get("window_end"),
                "expected_hour_utc": forecast_payload.get("expected_hour_utc") or prediction.get("expected_hour_utc"),
                "expected_weekday_utc": forecast_payload.get("expected_weekday_utc") or prediction.get("expected_weekday_utc"),
                "confidence": forecast_payload.get("confidence"),
                "post_count_used": forecast_payload.get("post_count_used") or basis.get("post_count_used"),
                "first_post_at": forecast_payload.get("first_post_at") or basis.get("first_post_at"),
                "last_post_at": forecast_payload.get("last_post_at") or basis.get("last_post_at"),
                "patterns": patterns,
                "notes": notes,
                "raw": forecast_payload,
            },
            "sentiment": sentiment,
            "sentiment_overview": sentiment,
            "geoparsed_locations": geoparsed_locations,
            "linked_profiles": linked_profiles,
            "repost_analytics": repost_analytics,
            "raw": payload,
        }

    def _normalize_deep_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            payload = {}

        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        raw_items = payload.get("items") if isinstance(payload.get("items"), list) else []
        items: list[dict[str, Any]] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            source_relative_path = self._relative_media_path(item.get("source_relative_path"))
            preview_asset = None
            if source_relative_path:
                preview_asset = self._archive_asset_from_path(
                    source_relative_path,
                    file_type=item.get("source_media_type"),
                )
            analysis = item.get("analysis") if isinstance(item.get("analysis"), dict) else {}
            social_context = (
                analysis.get("social_context_visible")
                if isinstance(analysis.get("social_context_visible"), dict)
                else {}
            )
            items.append(
                {
                    "entry_id": item.get("entry_id"),
                    "source_kind": item.get("source_kind"),
                    "source_kind_label": str(item.get("source_kind") or "").replace("_", " ").title(),
                    "source_item_id": item.get("source_item_id"),
                    "source_relative_path": source_relative_path,
                    "source_media_type": item.get("source_media_type"),
                    "analysis_media_type": item.get("analysis_media_type"),
                    "posted_at": item.get("posted_at"),
                    "discovered_at": item.get("discovered_at"),
                    "collection_name": item.get("collection_name"),
                    "content_scope": item.get("content_scope"),
                    "caption": item.get("caption"),
                    "text_context": item.get("text_context"),
                    "frame_index": item.get("frame_index"),
                    "frame_offset_s": item.get("frame_offset_s"),
                    "video_motion_classification": item.get("video_motion_classification"),
                    "music_tracks": item.get("music_tracks") if isinstance(item.get("music_tracks"), list) else [],
                    "explicit_account_mentions": item.get("explicit_account_mentions") if isinstance(item.get("explicit_account_mentions"), list) else [],
                    "preview_asset": preview_asset,
                    "analysis": {
                        "visual_summary": analysis.get("visual_summary"),
                        "activities": analysis.get("activities") if isinstance(analysis.get("activities"), list) else [],
                        "visible_objects": analysis.get("visible_objects") if isinstance(analysis.get("visible_objects"), list) else [],
                        "visible_brands_or_logos": analysis.get("visible_brands_or_logos") if isinstance(analysis.get("visible_brands_or_logos"), list) else [],
                        "fashion_and_style_signals": analysis.get("fashion_and_style_signals") if isinstance(analysis.get("fashion_and_style_signals"), list) else [],
                        "explicit_location_signals": analysis.get("explicit_location_signals") if isinstance(analysis.get("explicit_location_signals"), list) else [],
                        "location_guesses": analysis.get("location_guesses") if isinstance(analysis.get("location_guesses"), list) else [],
                        "hobby_signals": analysis.get("hobby_signals") if isinstance(analysis.get("hobby_signals"), list) else [],
                        "recognizable_public_figures_or_performers": analysis.get("recognizable_public_figures_or_performers") if isinstance(analysis.get("recognizable_public_figures_or_performers"), list) else [],
                        "luxury_or_premium_indicators": analysis.get("luxury_or_premium_indicators") if isinstance(analysis.get("luxury_or_premium_indicators"), list) else [],
                        "recurring_themes": analysis.get("recurring_themes") if isinstance(analysis.get("recurring_themes"), list) else [],
                        "media_tone": analysis.get("media_tone") if isinstance(analysis.get("media_tone"), list) else [],
                        "confidence_notes": analysis.get("confidence_notes"),
                        "social_context_visible": {
                            "category": social_context.get("category"),
                            "notes": social_context.get("notes"),
                        },
                    },
                    "analyzed_at": item.get("analyzed_at"),
                }
            )

        items.sort(
            key=lambda item: (
                self._timestamp_sort_key(item.get("posted_at") or item.get("discovered_at") or item.get("analyzed_at")),
                str(item.get("source_relative_path") or "").casefold(),
            ),
            reverse=True,
        )

        return {
            "available": bool(summary or items),
            "schema_version": payload.get("schema_version"),
            "analysis_type": payload.get("analysis_type"),
            "generated_at": payload.get("generated_at"),
            "platform": payload.get("platform"),
            "username": payload.get("username"),
            "model": payload.get("model"),
            "prompt_version": payload.get("prompt_version"),
            "artifact_relative_path": payload.get("artifact_relative_path"),
            "report_relative_path": payload.get("report_relative_path"),
            "graph_relative_path": payload.get("graph_relative_path"),
            "prepared_entry_count": payload.get("prepared_entry_count"),
            "run_limit": payload.get("run_limit"),
            "is_partial_run": bool(payload.get("is_partial_run")),
            "safety": payload.get("safety") if isinstance(payload.get("safety"), dict) else {},
            "summary": summary,
            "items": items,
            "raw": payload,
        }

    def _normalize_connection_snapshot(self, snapshot: Any) -> dict[str, Any]:
        if not isinstance(snapshot, dict):
            return {"snapshot_at": None, "count": 0, "usernames": [], "users": []}

        usernames = snapshot.get("usernames")
        if not isinstance(usernames, list):
            usernames = []
        users = snapshot.get("users")
        if not isinstance(users, list):
            users = [{"username": username} for username in usernames]
        normalized_users = []
        for user in users:
            if not isinstance(user, dict):
                continue
            username = user.get("username")
            if not username:
                continue
            normalized_users.append(
                {
                    "username": username,
                    "display_name": user.get("display_name"),
                    "user_id": user.get("user_id"),
                    "profile_pic_url": user.get("profile_pic_url"),
                    "is_verified": bool(user.get("is_verified")),
                    "is_private": bool(user.get("is_private")),
                }
            )
        if not usernames:
            usernames = [user["username"] for user in normalized_users]
        return {
            "snapshot_at": snapshot.get("snapshot_at"),
            "count": snapshot.get("count", len(normalized_users) or len(usernames)),
            "usernames": usernames,
            "users": normalized_users,
        }

    def _normalize_graph(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {
                "available": False,
                "node_count": 0,
                "link_count": 0,
                "follower_node_count": 0,
                "following_node_count": 0,
                "followers_snapshot_at": None,
                "following_snapshot_at": None,
                "generated_at": None,
                "format": None,
                "directed": None,
                "target_node_id": None,
                "sample_nodes": [],
                "raw": {},
            }

        graph_meta = payload.get("graph") if isinstance(payload.get("graph"), dict) else {}
        nodes = payload.get("nodes") if isinstance(payload.get("nodes"), list) else []
        links = payload.get("links") if isinstance(payload.get("links"), list) else []
        sample_nodes: list[dict[str, Any]] = []
        for node in nodes:
            if not isinstance(node, dict) or node.get("is_target_profile"):
                continue
            sample_nodes.append(
                {
                    "username": node.get("username"),
                    "display_name": node.get("display_name"),
                    "kind": node.get("kind"),
                    "user_id": node.get("user_id"),
                }
            )
            if len(sample_nodes) >= 8:
                break

        return {
            "available": bool(nodes or links),
            "node_count": len(nodes),
            "link_count": len(links),
            "follower_node_count": sum(
                1 for node in nodes if isinstance(node, dict) and node.get("kind") == "follower"
            ),
            "following_node_count": sum(
                1 for node in nodes if isinstance(node, dict) and node.get("kind") == "following"
            ),
            "followers_snapshot_at": graph_meta.get("followers_snapshot_at"),
            "following_snapshot_at": graph_meta.get("following_snapshot_at"),
            "generated_at": payload.get("generated_at"),
            "format": payload.get("format"),
            "directed": payload.get("directed"),
            "target_node_id": graph_meta.get("target_node_id"),
            "platform": payload.get("platform") or "",
            "sample_nodes": sample_nodes,
            "visualization": self._build_graph_visualization(nodes, links),
            "raw": payload,
        }

    def _build_graph_visualization(
        self,
        nodes: list[Any],
        links: list[Any],
    ) -> dict[str, Any]:
        width = 920
        height = 520
        center_x = width / 2
        center_y = height / 2
        radius = min(width, height) * 0.34

        target_nodes = [node for node in nodes if isinstance(node, dict) and node.get("is_target_profile")]
        follower_nodes = sorted(
            [
                node for node in nodes
                if isinstance(node, dict) and node.get("kind") == "follower"
            ],
            key=lambda item: str(item.get("username") or item.get("id") or "").casefold(),
        )
        following_nodes = sorted(
            [
                node for node in nodes
                if isinstance(node, dict) and node.get("kind") == "following"
            ],
            key=lambda item: str(item.get("username") or item.get("id") or "").casefold(),
        )
        other_nodes = sorted(
            [
                node for node in nodes
                if isinstance(node, dict)
                and not node.get("is_target_profile")
                and node.get("kind") not in {"follower", "following"}
            ],
            key=lambda item: str(item.get("username") or item.get("id") or "").casefold(),
        )

        positions: dict[str, tuple[float, float]] = {}
        rendered_nodes: list[dict[str, Any]] = []

        def assign_positions(
            group: list[dict[str, Any]],
            *,
            start_degrees: float,
            end_degrees: float,
            ring_radius: float,
        ) -> None:
            if not group:
                return
            if len(group) == 1:
                angle_values = [math.radians((start_degrees + end_degrees) / 2)]
            else:
                step = (end_degrees - start_degrees) / max(len(group) - 1, 1)
                angle_values = [
                    math.radians(start_degrees + (step * index))
                    for index in range(len(group))
                ]
            for node, angle in zip(group, angle_values):
                node_id = str(node.get("id") or "")
                if not node_id:
                    continue
                positions[node_id] = (
                    center_x + (math.cos(angle) * ring_radius),
                    center_y + (math.sin(angle) * ring_radius),
                )

        follower_ring = min(width, height) * 0.44
        following_ring = min(width, height) * 0.21
        other_ring = min(width, height) * 0.32
        n_followers = len(follower_nodes)
        follower_node_r = round(max(1.4, min(3.8, 1100.0 / max(n_followers, 1))), 2)

        assign_positions(follower_nodes, start_degrees=0, end_degrees=358, ring_radius=follower_ring)
        assign_positions(following_nodes, start_degrees=-35, end_degrees=35, ring_radius=following_ring)
        assign_positions(other_nodes, start_degrees=0, end_degrees=358, ring_radius=other_ring)

        for node in target_nodes:
            node_id = str(node.get("id") or "")
            if node_id:
                positions[node_id] = (center_x, center_y)

        palette = {
            "target": {"fill": "#35201d", "stroke": "#fff6ea", "radius": 10},
            "follower": {"fill": "#d9a24f", "stroke": "#fff5e6", "radius": follower_node_r},
            "following": {"fill": "#b6523a", "stroke": "#fff1ea", "radius": 4.5},
        }

        for node in nodes:
            if not isinstance(node, dict):
                continue
            node_id = str(node.get("id") or "")
            if not node_id or node_id not in positions:
                continue
            kind = "target" if node.get("is_target_profile") else str(node.get("kind") or "other")
            style = palette.get(kind, {"fill": "#7b6a5d", "stroke": "#fff8ef", "radius": 3.2})
            x, y = positions[node_id]
            label = node.get("display_name") or node.get("username") or node_id
            secondary = node.get("username") if node.get("display_name") else None
            rendered_nodes.append(
                {
                    "id": node_id,
                    "x": round(x, 2),
                    "y": round(y, 2),
                    "radius": style["radius"],
                    "fill": style["fill"],
                    "stroke": style["stroke"],
                    "kind": kind,
                    "label": label,
                    "secondary": secondary,
                    "username": node.get("username") or "",
                    "platform": node.get("platform") or "",
                }
            )

        rendered_edges: list[dict[str, Any]] = []
        for link in links:
            if not isinstance(link, dict):
                continue
            source = str(link.get("source") or "")
            target = str(link.get("target") or "")
            if source not in positions or target not in positions:
                continue
            x1, y1 = positions[source]
            x2, y2 = positions[target]
            rendered_edges.append(
                {
                    "x1": round(x1, 2),
                    "y1": round(y1, 2),
                    "x2": round(x2, 2),
                    "y2": round(y2, 2),
                    "kind": str(link.get("relationship") or ""),
                }
            )

        return {
            "width": width,
            "height": height,
            "nodes": rendered_nodes,
            "edges": rendered_edges,
        }

    def _normalize_connection_history(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {"source": None, "count": 0, "usernames": [], "snapshots": [], "users": []}

        snapshots = [
            self._normalize_connection_snapshot(snapshot)
            for snapshot in payload.get("snapshots", [])
            if isinstance(snapshot, dict)
        ]
        users = []
        for user in payload.get("users", []):
            if not isinstance(user, dict):
                continue
            username = user.get("username")
            if not username:
                continue
            users.append(
                {
                    "username": username,
                    "display_name": user.get("display_name"),
                    "user_id": user.get("user_id"),
                    "profile_pic_url": user.get("profile_pic_url"),
                    "is_verified": bool(user.get("is_verified")),
                    "is_private": bool(user.get("is_private")),
                    "first_seen_at": user.get("first_seen_at"),
                    "last_seen_at": user.get("last_seen_at"),
                    "times_seen": user.get("times_seen") or 0,
                    "currently_present": bool(user.get("currently_present")),
                }
            )

        return {
            "source": payload.get("source"),
            "count": payload.get("count", len(users)),
            "usernames": payload.get("usernames") if isinstance(payload.get("usernames"), list) else [user["username"] for user in users],
            "snapshots": snapshots,
            "users": users,
        }

    def _normalize_dm_index_item(
        self,
        platform: str,
        account_username: str,
        item: dict[str, Any],
    ) -> dict[str, Any]:
        participant_usernames = [
            str(username).strip()
            for username in item.get("participant_usernames", [])
            if isinstance(username, str) and str(username).strip()
        ]
        display_title = (
            item.get("title")
            or ", ".join(f"@{username}" for username in participant_usernames[:3])
            or str(item.get("conversation_id") or "Conversation")
        )
        participant_profiles = [
            self._profile_link_summary(platform, username)
            for username in participant_usernames[:4]
        ]
        participant_profiles = [profile for profile in participant_profiles if profile]
        primary_profile = next(
            (profile for profile in participant_profiles if profile.get("available")),
            participant_profiles[0] if participant_profiles else None,
        )
        return {
            "platform": platform,
            "account_username": account_username,
            "conversation_id": item.get("conversation_id"),
            "conversation_type": item.get("conversation_type") or "direct",
            "title": item.get("title"),
            "subtitle": item.get("subtitle"),
            "display_title": display_title,
            "is_group": bool(item.get("is_group")),
            "is_broadcast_channel": bool(item.get("is_broadcast_channel")),
            "participant_usernames": participant_usernames,
            "message_count": int(item.get("message_count") or 0),
            "attachment_count": int(item.get("attachment_count") or 0),
            "last_message_preview": item.get("last_message_preview"),
            "last_message_at": item.get("last_message_at"),
            "last_scraped_at": item.get("last_scraped_at"),
            "participant_profiles": participant_profiles,
            "primary_profile": primary_profile,
            "detail_url": f"/accounts/{platform}/{account_username}/conversations/{item.get('conversation_id')}",
            "account_url": f"/accounts/{platform}/{account_username}",
            "files": {
                "conversation": item.get("conversation_path") or "",
                "messages": item.get("messages_path") or "",
            },
            "raw": item,
        }

    def _normalize_dm_conversation_detail(
        self,
        *,
        account: dict[str, Any],
        summary: dict[str, Any],
        conversation_payload: dict[str, Any],
        messages_payload: dict[str, Any],
    ) -> dict[str, Any]:
        platform = str(account.get("platform") or "")
        participants = [
            self._normalize_dm_participant(platform, participant)
            for participant in conversation_payload.get("participants", [])
            if isinstance(participant, dict)
        ]
        participant_lookup = {
            str(participant.get("username") or "").strip().casefold(): participant
            for participant in participants
            if participant.get("username")
        }
        messages = [
            self._normalize_dm_message_item(
                platform,
                message,
                participant_lookup=participant_lookup,
            )
            for message in messages_payload.get("items", [])
            if isinstance(message, dict)
        ]
        self_participant = next((participant for participant in participants if participant.get("is_self")), None)
        other_participants = [participant for participant in participants if not participant.get("is_self")]
        linked_profiles = [participant["linked_profile"] for participant in other_participants if participant.get("linked_profile", {}).get("available")]
        primary_profile = linked_profiles[0] if linked_profiles else None

        return {
            **deepcopy(summary),
            "title": conversation_payload.get("title") or summary.get("title"),
            "subtitle": conversation_payload.get("subtitle") or summary.get("subtitle"),
            "display_title": (
                conversation_payload.get("title")
                or summary.get("display_title")
                or ", ".join(
                    participant.get("label") or participant.get("username") or ""
                    for participant in other_participants[:3]
                )
                or summary.get("conversation_id")
            ),
            "conversation_url": conversation_payload.get("conversation_url"),
            "last_message_preview": conversation_payload.get("last_message_preview") or summary.get("last_message_preview"),
            "last_message_at": conversation_payload.get("last_message_at") or summary.get("last_message_at"),
            "discovered_at": conversation_payload.get("discovered_at"),
            "last_scraped_at": conversation_payload.get("last_scraped_at") or summary.get("last_scraped_at"),
            "participants": participants,
            "participant_count": len(participants),
            "self_participant": self_participant,
            "other_participants": other_participants,
            "linked_profiles": linked_profiles,
            "primary_profile": primary_profile,
            "messages": messages,
            "message_count": len(messages),
            "attachment_count": sum(len(message.get("attachments") or []) for message in messages),
            "account": {
                "platform": account.get("platform"),
                "username": account.get("username"),
                "display_name": account.get("display_name"),
                "detail_url": f"/accounts/{account.get('platform')}/{account.get('username')}",
            },
            "raw_conversation": conversation_payload,
            "raw_messages": messages_payload,
        }

    def _normalize_dm_participant(self, platform: str, participant: dict[str, Any]) -> dict[str, Any]:
        username = participant.get("username")
        display_name = participant.get("display_name")
        label = display_name or (f"@{username}" if username else "Unknown participant")
        linked_profile = self.get_profile(platform, username) if username else None
        return {
            "username": username,
            "user_id": participant.get("user_id"),
            "display_name": display_name,
            "label": label,
            "profile_url": participant.get("profile_url"),
            "profile_pic_url": participant.get("profile_pic_url"),
            "is_self": bool(participant.get("is_self")),
            "participant_order": participant.get("participant_order"),
            "metadata": participant.get("metadata") if isinstance(participant.get("metadata"), dict) else {},
            "linked_profile": {
                "available": bool(linked_profile),
                "platform": platform,
                "username": username,
                "display_name": linked_profile.get("display_name") if linked_profile else None,
                "detail_url": f"/profiles/{platform}/{username}" if linked_profile else None,
                "counts": linked_profile.get("counts") if linked_profile else {},
                "bio_text": (linked_profile.get("bio") or {}).get("bio_text") if linked_profile else None,
                "profile_pic_url": (linked_profile.get("bio") or {}).get("profile_pic_url") if linked_profile else None,
            },
        }

    def _normalize_dm_message_item(
        self,
        platform: str,
        message: dict[str, Any],
        *,
        participant_lookup: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        sender = message.get("sender") if isinstance(message.get("sender"), dict) else {}
        attachments = []
        for attachment in message.get("attachments", []):
            if not isinstance(attachment, dict):
                continue
            asset = self._archive_asset_from_path(
                attachment.get("file_path"),
                file_type=attachment.get("file_type"),
                record=attachment,
            )
            if asset:
                attachments.append(asset)

        sender_label = (
            sender.get("display_name")
            or (f"@{sender.get('username')}" if sender.get("username") else None)
            or "Unknown sender"
        )
        sender_username = str(sender.get("username") or "").strip()
        participant_match = (
            (participant_lookup or {}).get(sender_username.casefold())
            if sender_username
            else None
        )
        sender_link = participant_match.get("linked_profile") if participant_match else None
        return {
            "message_id": message.get("message_id") or message.get("message_key"),
            "message_key": message.get("message_key"),
            "sender": {
                "username": sender_username or None,
                "user_id": sender.get("user_id"),
                "display_name": sender.get("display_name"),
                "label": sender_label,
                "is_self": bool(sender.get("is_self")),
                "profile_pic_url": (
                    participant_match.get("profile_pic_url")
                    if participant_match
                    else None
                ),
                "profile_url": (
                    participant_match.get("profile_url")
                    if participant_match
                    else _platform_profile_url(platform, sender_username)
                    if sender_username
                    else None
                ),
                "linked_profile": sender_link or self._profile_link_summary(platform, sender_username),
            },
            "sent_at": message.get("sent_at"),
            "sent_at_label": message.get("sent_at_label"),
            "message_type": message.get("message_type") or "text",
            "text": message.get("text"),
            "status_text": message.get("status_text"),
            "reply_to_message_id": message.get("reply_to_message_id"),
            "message_order": message.get("message_order"),
            "discovered_at": message.get("discovered_at"),
            "last_seen_at": message.get("last_seen_at"),
            "attachments": attachments,
            "attachment_count": len(attachments),
            "raw": message,
        }

    def _profile_link_summary(self, platform: str, username: str | None) -> dict[str, Any] | None:
        normalized_username = str(username or "").strip()
        if not normalized_username:
            return None

        linked_profile = self.get_profile(platform, normalized_username)
        if linked_profile:
            bio = linked_profile.get("bio") if isinstance(linked_profile.get("bio"), dict) else {}
            return {
                "available": True,
                "platform": platform,
                "username": normalized_username,
                "display_name": linked_profile.get("display_name") or normalized_username,
                "detail_url": f"/profiles/{platform}/{normalized_username}",
                "profile_pic_url": bio.get("profile_pic_url"),
                "bio_text": bio.get("bio_text"),
                "counts": linked_profile.get("counts") if isinstance(linked_profile.get("counts"), dict) else {},
            }

        return {
            "available": False,
            "platform": platform,
            "username": normalized_username,
            "display_name": f"@{normalized_username}",
            "detail_url": None,
            "profile_pic_url": None,
            "bio_text": None,
            "counts": {},
        }

    def _normalize_post_item(
        self,
        platform: str,
        username: str,
        kind: str,
        item: dict[str, Any],
    ) -> dict[str, Any]:
        item_id = str(item.get("post_id") or "")
        media_assets = self._media_assets_from_paths(item.get("media_paths"))
        caption = item.get("caption")
        comments = item.get("comments") if isinstance(item.get("comments"), list) else []
        comment_tree = self._normalize_comment_tree(
            item.get("comment_tree") if isinstance(item.get("comment_tree"), list) else []
        )
        comment_history = item.get("comment_history") if isinstance(item.get("comment_history"), list) else comments
        comment_history_tree = self._normalize_comment_tree(
            item.get("comment_history_tree") if isinstance(item.get("comment_history_tree"), list) else comment_tree
        )
        preview_text = caption or f"{kind[:-1].title()} {item_id}"
        return {
            "kind": kind,
            "item_id": item_id,
            "title": preview_text.strip()[:120] if isinstance(preview_text, str) else item_id,
            "caption": caption,
            "collection_name": item.get("collection_name"),
            "url": item.get("url"),
            "posted_at": item.get("posted_at"),
            "discovered_at": item.get("discovered_at"),
            "metrics": {
                "likes": item.get("like_count"),
                "comments": item.get("comment_count"),
                "views": item.get("view_count"),
                "shares": item.get("share_count"),
            },
            "is_repost": bool(item.get("is_repost")),
            "repost_source_url": item.get("repost_source_url"),
            "repost_source_account": item.get("repost_source_account"),
            "source_platform": item.get("source_platform"),
            "source_user_id": item.get("source_user_id"),
            "source_display_name": item.get("source_display_name"),
            "behavior_category": item.get("behavior_category"),
            "behavior_confidence": item.get("behavior_confidence"),
            "classifier_version": item.get("classifier_version"),
            "content_scope": item.get("content_scope"),
            "post_type": item.get("post_type"),
            "geoparsed_locations": item.get("geoparsed_locations") if isinstance(item.get("geoparsed_locations"), list) else [],
            "sentiment": item.get("sentiment") if isinstance(item.get("sentiment"), dict) else {},
            "vibe_score": item.get("vibe_score"),
            "vibe_label": item.get("vibe_label"),
            "media_assets": media_assets,
            "preview_media": self._preferred_preview_media(media_assets),
            "comments": comments,
            "comment_tree": comment_tree,
            "comment_history": comment_history,
            "comment_history_tree": comment_history_tree,
            "comments_supported": item.get("comments_supported"),
            "comments_snapshot_complete": item.get("comments_snapshot_complete"),
            "comments_unavailable_reason": item.get("comments_unavailable_reason"),
            "detail_url": f"/profiles/{platform}/{username}/items/{kind}/{item_id}",
            "raw": item,
        }

    def _normalize_story_item(
        self,
        platform: str,
        username: str,
        kind: str,
        item: dict[str, Any],
    ) -> dict[str, Any]:
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        item_id = str(item.get("story_id") or "")
        media_assets = self._media_assets_from_paths(item.get("media_paths"))
        preview_text = (
            metadata.get("caption")
            or item.get("collection_name")
            or metadata.get("accessibility_caption")
            or f"{kind[:-1].title()} {item_id}"
        )
        return {
            "kind": kind,
            "item_id": item_id,
            "title": preview_text.strip()[:120] if isinstance(preview_text, str) else item_id,
            "caption": metadata.get("caption"),
            "collection_name": item.get("collection_name"),
            "url": item.get("url"),
            "posted_at": item.get("posted_at"),
            "discovered_at": item.get("discovered_at"),
            "expires_at": item.get("expires_at"),
            "story_type": item.get("story_type"),
            "content_scope": item.get("content_scope"),
            "metadata": metadata,
            "geoparsed_locations": item.get("geoparsed_locations") if isinstance(item.get("geoparsed_locations"), list) else [],
            "sentiment": {},
            "vibe_score": None,
            "vibe_label": None,
            "media_assets": media_assets,
            "preview_media": self._preferred_preview_media(
                media_assets,
                metadata=metadata,
                prefer_remote_image=True,
            ),
            "comments": [],
            "comment_tree": [],
            "detail_url": f"/profiles/{platform}/{username}/items/{kind}/{item_id}",
            "raw": item,
        }

    def _overview_items(self, collections: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
        items = [
            item
            for key in ("posts", "mentions", "reposts", "stories", "highlights")
            for item in collections[key]
        ]
        items.sort(
            key=lambda item: (
                self._parse_datetime(item.get("posted_at"))
                or self._parse_datetime(item.get("discovered_at"))
                or datetime.min.replace(tzinfo=timezone.utc)
            ),
            reverse=True,
        )
        return items[:16]

    def _load_item_sidecar(self, item: dict[str, Any]) -> dict[str, Any] | None:
        media_assets = item.get("media_assets") or []
        if not media_assets:
            return None
        first_path = media_assets[0].get("relative_path")
        if not first_path:
            return None
        item_dir = (self.data_root / first_path).parent
        sidecar_name = "post.json" if item["kind"] in POST_KINDS else "story.json"
        sidecar_path = item_dir / sidecar_name
        if not sidecar_path.exists():
            return None
        sidecar = self._read_json(sidecar_path)
        return sidecar if isinstance(sidecar, dict) else None

    def _load_text_blob(self, item: dict[str, Any]) -> str | None:
        media_assets = item.get("media_assets") or []
        if not media_assets:
            return None
        first_path = media_assets[0].get("relative_path")
        if not first_path:
            return None
        item_dir = (self.data_root / first_path).parent
        filename = "caption.txt" if item["kind"] in POST_KINDS else "story_text.txt"
        text_path = item_dir / filename
        if not text_path.exists():
            return None
        try:
            return text_path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def _media_assets_from_sidecar(self, sidecar: dict[str, Any]) -> list[dict[str, Any]]:
        saved_media = sidecar.get("saved_media")
        if not isinstance(saved_media, list):
            return []
        assets = []
        for record in saved_media:
            if not isinstance(record, dict):
                continue
            relative_path = self._relative_media_path(record.get("file_path"))
            if not relative_path:
                continue
            assets.append(
                {
                    "relative_path": relative_path,
                    "url": self._media_url(relative_path),
                    "filename": record.get("filename") or Path(relative_path).name,
                    "media_type": record.get("file_type") or self._media_type_for_path(relative_path),
                    "file_size": record.get("file_size"),
                    "width": record.get("width"),
                    "height": record.get("height"),
                    "duration_s": record.get("duration_s"),
                    "has_audio": record.get("has_audio"),
                    "exists_on_disk": bool(record.get("exists_on_disk", (self.data_root / relative_path).exists())),
                }
            )
        return assets

    def _metadata_image_candidates(
        self,
        sidecar: dict[str, Any],
        *,
        existing_assets: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        metadata = sidecar.get("metadata")
        if not isinstance(metadata, dict):
            return []

        existing_urls = {
            asset.get("url")
            for asset in existing_assets
            if isinstance(asset, dict) and isinstance(asset.get("url"), str)
        }
        assets: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for candidate in metadata.get("media_candidates") or []:
            if not isinstance(candidate, dict) or candidate.get("kind") != "image":
                continue
            url = candidate.get("url")
            if not isinstance(url, str) or not url.startswith("http"):
                continue
            if url in existing_urls or url in seen_urls:
                continue
            seen_urls.add(url)
            assets.append(
                {
                    "relative_path": None,
                    "url": url,
                    "filename": Path(url.split("?", 1)[0]).name or "image",
                    "media_type": "photo",
                    "file_size": None,
                    "width": candidate.get("width"),
                    "height": candidate.get("height"),
                    "duration_s": None,
                    "has_audio": False,
                    "exists_on_disk": False,
                    "source": candidate.get("source") or "metadata_candidate",
                }
            )
        return assets

    def _media_assets_from_paths(self, media_paths: Any) -> list[dict[str, Any]]:
        assets = []
        if not isinstance(media_paths, list):
            return assets
        for path in media_paths:
            asset = self._archive_asset_from_path(path)
            if asset:
                assets.append(asset)
        return assets

    def _summary_view(self, profile: dict[str, Any]) -> dict[str, Any]:
        bio = profile.get("bio") or {}
        quick_badges = []
        for key in ("posts", "mentions", "reposts", "stories", "highlights"):
            count = profile["counts"].get(key, 0)
            if count:
                quick_badges.append({"key": key, "label": key.replace("_", " ").title(), "count": count})
        last_activity_at = self._latest_item_timestamp(profile.get("overview_items") or [])
        return {
            "platform": profile["platform"],
            "username": profile["username"],
            "display_name": profile["display_name"],
            "generated_at": profile.get("generated_at"),
            "is_own_account": profile.get("is_own_account", False),
            "bio_text": bio.get("bio_text"),
            "bio_post_count": bio.get("post_count"),
            "bio_follower_count": bio.get("follower_count"),
            "bio_following_count": bio.get("following_count"),
            "profile_pic_url": bio.get("profile_pic_url"),
            "last_activity_at": last_activity_at,
            "counts": profile["counts"],
            "quick_badges": quick_badges,
            "detail_url": f"/profiles/{profile['platform']}/{profile['username']}",
        }

    def _account_summary_view(self, account: dict[str, Any]) -> dict[str, Any]:
        recent_participants: list[str] = []
        for conversation in account.get("conversations", []):
            for username in conversation.get("participant_usernames", []):
                if username not in recent_participants:
                    recent_participants.append(username)
                if len(recent_participants) >= 5:
                    break
            if len(recent_participants) >= 5:
                break

        return {
            "platform": account["platform"],
            "username": account["username"],
            "display_name": account["display_name"],
            "generated_at": account.get("generated_at"),
            "archive_counts": account.get("archive_counts", {}),
            "conversation_count": account.get("conversation_count", 0),
            "last_message_at": account.get("last_message_at"),
            "last_scraped_at": account.get("last_scraped_at"),
            "bio_text": account.get("bio_text"),
            "profile_pic_url": account.get("profile_pic_url"),
            "bio_post_count": account.get("bio_post_count"),
            "bio_follower_count": account.get("bio_follower_count"),
            "bio_following_count": account.get("bio_following_count"),
            "linked_profile": account.get("linked_profile", {}),
            "recent_participants": recent_participants,
            "recent_conversations": account.get("recent_conversations", [])[:4],
            "detail_url": f"/accounts/{account['platform']}/{account['username']}",
            "profile_detail_url": f"/profiles/{account['platform']}/{account['username']}",
        }

    def _item_search_score(self, item: dict[str, Any], token: str) -> int | None:
        item_id = str(item.get("item_id") or "").strip().lower()
        source_url = str(item.get("url") or "").strip().lower()

        if not token:
            return None
        if item_id == token:
            return 0
        if source_url == token:
            return 1
        if token and item_id.startswith(token):
            return 2
        if token and token in item_id:
            return 3
        if token and token in source_url:
            return 4
        return None

    def _item_search_view(
        self,
        profile: dict[str, Any],
        item: dict[str, Any],
        match_score: int,
    ) -> dict[str, Any]:
        metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
        preview_media = item.get("preview_media") if isinstance(item.get("preview_media"), dict) else None
        return {
            "platform": profile.get("platform"),
            "username": profile.get("username"),
            "display_name": profile.get("display_name"),
            "kind": item.get("kind"),
            "item_id": item.get("item_id"),
            "title": item.get("title"),
            "caption": item.get("caption"),
            "url": item.get("url"),
            "posted_at": item.get("posted_at"),
            "discovered_at": item.get("discovered_at"),
            "content_scope": item.get("content_scope"),
            "media_count": len(item.get("media_assets") or []),
            "preview_media": preview_media,
            "likes": metrics.get("likes"),
            "comments": metrics.get("comments"),
            "views": metrics.get("views"),
            "detail_url": item.get("detail_url"),
            "match_score": match_score,
        }

    def _matches_search(self, profile: dict[str, Any], token: str) -> bool:
        haystack = " ".join(
            [
                str(profile.get("platform", "")),
                str(profile.get("username", "")),
                str(profile.get("display_name", "")),
                str((profile.get("bio") or {}).get("bio_text", "")),
            ]
        ).lower()
        return token in haystack

    def _matches_account_search(self, account: dict[str, Any], token: str) -> bool:
        haystack = [
            str(account.get("platform", "")),
            str(account.get("username", "")),
        ]
        for conversation in account.get("conversations", []):
            haystack.extend(
                [
                    str(conversation.get("display_title", "")),
                    str(conversation.get("last_message_preview", "")),
                    " ".join(conversation.get("participant_usernames", [])),
                ]
            )
        return token in " ".join(haystack).lower()

    def _matches_conversation_search(
        self,
        account: dict[str, Any],
        conversation: dict[str, Any],
        token: str,
    ) -> bool:
        haystack = " ".join(
            [
                str(account.get("platform", "")),
                str(account.get("username", "")),
                str(conversation.get("conversation_id", "")),
                str(conversation.get("display_title", "")),
                str(conversation.get("subtitle", "")),
                str(conversation.get("last_message_preview", "")),
                " ".join(conversation.get("participant_usernames", [])),
            ]
        ).lower()
        return token in haystack

    def _items(self, payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        items = payload.get("items")
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    def _signature(self, file_paths: dict[str, Path]) -> tuple[tuple[str, int, int], ...]:
        signature = []
        for name, path in sorted(file_paths.items()):
            if not path.exists():
                signature.append((name, -1, -1))
                continue
            stat = path.stat()
            signature.append((name, stat.st_mtime_ns, stat.st_size))
        return tuple(signature)

    def _read_json(self, path: Path) -> Any:
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _relative_data_path(self, value: Any) -> str | None:
        if not isinstance(value, str) or not value:
            return None
        path = Path(value)
        parts = list(path.parts)
        if "media" in parts:
            return str(Path(*parts[parts.index("media") :]))
        if "accounts" in parts:
            return str(Path(*parts[parts.index("accounts") :]))
        if value.startswith("media/"):
            return value
        if value.startswith("accounts/"):
            return value
        return None

    def _relative_media_path(self, value: Any) -> str | None:
        relative_path = self._relative_data_path(value)
        if isinstance(relative_path, str) and relative_path.startswith("media/"):
            return relative_path
        return None

    def _asset_url(self, relative_path: str) -> str:
        if relative_path.startswith("media/"):
            return f"/{relative_path}"
        return f"/archive/{relative_path}"

    def _media_url(self, relative_path: str) -> str:
        return self._asset_url(relative_path)

    def _archive_asset_from_path(
        self,
        value: Any,
        *,
        file_type: Any = None,
        record: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        relative_path = self._relative_data_path(value)
        if not relative_path:
            return None
        absolute_path = self.data_root / relative_path
        metadata = record or {}
        return {
            "relative_path": relative_path,
            "url": self._asset_url(relative_path),
            "filename": metadata.get("filename") or absolute_path.name,
            "media_type": file_type or self._media_type_for_path(relative_path),
            "file_size": metadata.get("file_size") if metadata.get("file_size") is not None else (absolute_path.stat().st_size if absolute_path.exists() else None),
            "width": metadata.get("width"),
            "height": metadata.get("height"),
            "duration_s": metadata.get("duration_s"),
            "has_audio": metadata.get("has_audio"),
            "exists_on_disk": absolute_path.exists(),
            "sha256": metadata.get("sha256"),
            "mime_type": metadata.get("mime_type"),
            "preview_text": metadata.get("preview_text"),
            "downloaded_at": metadata.get("downloaded_at"),
            "source_url": metadata.get("source_url"),
            "metadata": metadata.get("metadata") if isinstance(metadata.get("metadata"), dict) else {},
        }

    def _media_type_for_path(self, relative_path: str) -> str:
        suffix = Path(relative_path).suffix.lower()
        if suffix in {".mp4", ".mov", ".m4v", ".webm"}:
            return "video"
        return "photo"

    def _parse_datetime(self, value: Any) -> datetime | None:
        if not isinstance(value, str) or not value:
            return None
        normalized = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    def _timestamp_sort_key(self, value: Any) -> float:
        parsed = self._parse_datetime(value)
        if not parsed:
            return float("-inf")
        return parsed.astimezone(timezone.utc).timestamp()

    def _latest_item_timestamp(self, items: list[dict[str, Any]]) -> str | None:
        latest: datetime | None = None
        for item in items:
            candidate = self._parse_datetime(item.get("posted_at")) or self._parse_datetime(item.get("discovered_at"))
            if candidate and (latest is None or candidate > latest):
                latest = candidate
        if latest is None:
            return None
        return latest.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _preferred_preview_media(
        self,
        media_assets: list[dict[str, Any]],
        *,
        metadata: Any = None,
        prefer_remote_image: bool = False,
    ) -> dict[str, Any] | None:
        if prefer_remote_image:
            remote_image = self._first_metadata_image_candidate(metadata)
            if remote_image:
                return remote_image

        for asset in media_assets:
            if asset.get("media_type") == "photo":
                return asset
        return media_assets[0] if media_assets else None

    def _first_metadata_image_candidate(self, metadata: Any) -> dict[str, Any] | None:
        if not isinstance(metadata, dict):
            return None
        for candidate in metadata.get("media_candidates") or []:
            if not isinstance(candidate, dict) or candidate.get("kind") != "image":
                continue
            url = candidate.get("url")
            if not isinstance(url, str) or not url.startswith("http"):
                continue
            return {
                "relative_path": None,
                "url": url,
                "filename": Path(url.split("?", 1)[0]).name or "image",
                "media_type": "photo",
                "file_size": None,
                "width": candidate.get("width"),
                "height": candidate.get("height"),
                "duration_s": None,
                "has_audio": False,
                "exists_on_disk": False,
                "source": candidate.get("source") or "metadata_candidate",
            }
        return None

    def _normalize_comment_tree(self, nodes: Any) -> list[dict[str, Any]]:
        if not isinstance(nodes, list):
            return []

        normalized: list[dict[str, Any]] = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            children = self._normalize_comment_tree(
                node.get("children") if isinstance(node.get("children"), list) else node.get("replies")
            )
            normalized.append(
                {
                    **node,
                    "children": children,
                    "reply_count": node.get("reply_count", len(children)),
                }
            )
        return normalized
