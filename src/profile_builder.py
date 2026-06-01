"""
profile_builder.py — Aggregate DB data for a target account into profile files.

Files are written to:
    data/profiles/{platform}/{username}/profile.json
    data/profiles/{platform}/{username}/posts.json
    data/profiles/{platform}/{username}/reposts.json
    data/profiles/{platform}/{username}/stories.json
    data/profiles/{platform}/{username}/connections.json
    data/profiles/{platform}/{username}/intelligence.json
    data/profiles/{platform}/{username}/graph.json

All media_paths inside the JSON are relative to data/ so the files remain
portable when the Docker volume is remounted at a different host path.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from src.comment_utils import build_comment_tree, normalize_comment_records
from src.intelligence import (
    add_behavior_to_reposts,
    add_geoparsing_to_posts,
    add_geoparsing_to_stories,
    add_sentiment_to_reposts,
    add_sentiment_to_posts,
    build_intelligence_bundle,
    build_social_graph_bundle,
    summarise_intelligence,
)
from src.logging_utils import log_event

if TYPE_CHECKING:
    from src.config import TargetConfig

SCHEMA_VERSION = 12


# ─── Public API ──────────────────────────────────────────────────────────────

def build_profile(conn: sqlite3.Connection, target_id: int,
                  profiles_dir: Path) -> Path | None:
    """
    Aggregate all DB data for target_id and write the profile export bundle.
    Returns the path to the written file, or None on error.
    """
    try:
        target_row = conn.execute(
            "SELECT * FROM targets WHERE id=?", (target_id,)
        ).fetchone()
        if not target_row:
            logger.warning(f"[profile] Target id={target_id} not found in DB")
            return None

        platform = target_row["platform"]
        username = target_row["username"]
        account_id = _linked_account_id(conn, target_id)

        bio = _current_bio(target_row)
        post_index = add_geoparsing_to_posts(add_sentiment_to_posts(_collect_post_index(conn, target_id)))
        repost_index = add_behavior_to_reposts(add_sentiment_to_reposts(_collect_repost_index(conn, target_id)))
        story_index = add_geoparsing_to_stories(_collect_story_index(conn, target_id))
        follow_snapshots = _collect_follow_snapshots(conn, target_id, account_id, bio=bio)
        follow_histories = _collect_follow_histories(conn, target_id, account_id)
        follower_log = _collect_connection_log(
            conn,
            target_id,
            account_id,
            ("gained_follower", "lost_follower"),
        )
        following_log = _collect_connection_log(
            conn,
            target_id,
            account_id,
            ("started_following", "stopped_following"),
        )
        media_summary = _media_summary(conn, target_id)

        safe_username = re.sub(r"[^\w.-]", "_", username)
        out_dir = profiles_dir / platform / safe_username
        out_dir.mkdir(parents=True, exist_ok=True)

        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        posts_payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated_at,
            "platform": platform,
            "username": username,
            "items": post_index,
        }
        reposts_payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated_at,
            "platform": platform,
            "username": username,
            "items": repost_index,
        }
        stories_payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated_at,
            "platform": platform,
            "username": username,
            "items": story_index,
        }
        connections_payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated_at,
            "platform": platform,
            "username": username,
            "followers": follow_snapshots["followers"],
            "following": follow_snapshots["following"],
            "followers_history": follow_histories["followers"],
            "following_history": follow_histories["following"],
            "follower_delta_log": follower_log,
            "following_delta_log": following_log,
        }
        intelligence_payload = build_intelligence_bundle(
            platform=platform,
            username=username,
            generated_at=generated_at,
            bio=bio,
            posts=post_index,
            reposts=repost_index,
            stories=story_index,
            candidate_profiles=_candidate_profiles_for_linking(conn, platform, username),
            data_root=profiles_dir.parent,
        )
        graph_payload = build_social_graph_bundle(
            platform=platform,
            username=username,
            generated_at=generated_at,
            followers=follow_snapshots["followers"],
            following=follow_snapshots["following"],
        )

        _write_json(out_dir / "posts.json", posts_payload)
        _write_json(out_dir / "reposts.json", reposts_payload)
        _write_json(out_dir / "stories.json", stories_payload)
        _write_json(out_dir / "connections.json", connections_payload)
        _write_json(out_dir / "intelligence.json", intelligence_payload)
        _write_json(out_dir / "graph.json", graph_payload)

        profile = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated_at,
            "platform": platform,
            "username": username,
            "is_own_account": account_id is not None,
            "bio": bio,
            "followers": _snapshot_summary(follow_snapshots["followers"]),
            "following": _snapshot_summary(follow_snapshots["following"]),
            "media_summary": media_summary,
            "intelligence": summarise_intelligence(intelligence_payload),
            "archive_counts": {
                "posts": len(post_index),
                "reposts": len(repost_index),
                "stories": len(story_index),
            },
            "files": {
                "posts": "posts.json",
                "reposts": "reposts.json",
                "stories": "stories.json",
                "connections": "connections.json",
                "intelligence": "intelligence.json",
                "graph": "graph.json",
            },
        }

        out_path = out_dir / "profile.json"
        _write_json(out_path, profile)

        log_event(
            "profile",
            "export_written",
            platform=platform,
            target=username,
            path=out_path,
        )
        return out_path

    except Exception as e:
        log_event("profile", "export_error", level="error", target_id=target_id, error=e)
        return None


def rebuild_all_profiles(conn: sqlite3.Connection, profiles_dir: Path,
                          targets: "list[TargetConfig]") -> None:
    """Call build_profile for every active target in the config."""
    for target in targets:
        try:
            target_id = conn.execute(
                "SELECT id FROM targets WHERE platform=? AND username=? AND is_active=1",
                (target.platform, target.username),
            ).fetchone()
            if target_id:
                build_profile(conn, target_id["id"], profiles_dir)
        except Exception as e:
            log_event(
                "profile",
                "export_error",
                level="error",
                platform=target.platform,
                target=target.username,
                error=e,
            )


# ─── Internal helpers ────────────────────────────────────────────────────────

def _write_json(path: Path, payload: dict) -> None:
    """Write a UTF-8 JSON file with consistent formatting."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

def _current_bio(row: sqlite3.Row) -> dict:
    return {
        "display_name": row["display_name"],
        "bio_text": row["bio_snapshot"],
        "follower_count": row["follower_count"],
        "following_count": row["following_count"],
        "post_count": row["post_count"],
        "profile_pic_url": row["profile_pic_url"],
        "cover_photo_url": row["cover_photo_url"],
        "snapshot_at": row["last_scraped_at"],
    }


def _candidate_profiles_for_linking(
    conn: sqlite3.Connection,
    platform: str,
    username: str,
) -> list[dict]:
    """Return other archived profiles as candidates for cross-platform linking."""
    rows = conn.execute(
        """
        SELECT platform, username, display_name, bio_snapshot
        FROM targets
        WHERE is_active=1
        ORDER BY platform, username
        """
    ).fetchall()
    candidates = []
    for row in rows:
        if row["platform"] == platform and row["username"] == username:
            continue
        candidates.append(
            {
                "platform": row["platform"],
                "username": row["username"],
                "display_name": row["display_name"],
                "bio_snapshot": row["bio_snapshot"],
            }
        )
    return candidates


def _collect_post_index(
    conn: sqlite3.Connection,
    target_id: int,
    content_scope: str | None = None,
) -> list[dict]:
    """Return posts for target, newest first, with optional scope filtering."""
    if content_scope:
        posts = conn.execute("""
            SELECT *
            FROM posts
            WHERE target_id=? AND content_scope=?
            ORDER BY discovered_at DESC
        """, (target_id, content_scope)).fetchall()
    else:
        posts = conn.execute("""
            SELECT *
            FROM posts
            WHERE target_id=?
              AND content_scope!='reposts'
            ORDER BY discovered_at DESC
        """, (target_id,)).fetchall()

    result = []
    for p in posts:
        media_paths = _media_paths_for_post(conn, p["id"])
        comments = _comments_for_post(conn, p["id"], current_only=True)
        comment_history = _comments_for_post(conn, p["id"], current_only=False)
        raw_metadata = {}
        if p["raw_metadata_json"]:
            try:
                parsed = json.loads(p["raw_metadata_json"])
                if isinstance(parsed, dict):
                    raw_metadata = parsed
            except json.JSONDecodeError:
                raw_metadata = {}
        result.append({
            "post_id": p["post_id"],
            "post_type": p["post_type"],
            "content_scope": p["content_scope"],
            "collection_name": p["collection_name"],
            "url": p["url"],
            "caption": p["caption"],
            "posted_at": p["posted_at"],
            "discovered_at": p["discovered_at"],
            "like_count": p["like_count"],
            "comment_count": p["comment_count"],
            "view_count": p["view_count"],
            "share_count": p["share_count"],
            "is_repost": bool(p["is_repost"]),
            "repost_source_url": p["repost_source_url"],
            "repost_source_account": p["repost_source_account"],
            "comments_supported": raw_metadata.get("comments_supported"),
            "comments_snapshot_complete": raw_metadata.get("comments_snapshot_complete"),
            "comments_unavailable_reason": raw_metadata.get("comments_unavailable_reason"),
            "comments": comments,
            "comment_tree": build_comment_tree(comments),
            "comment_history": comment_history,
            "comment_history_tree": build_comment_tree(comment_history),
            "media_paths": media_paths,
            "raw_metadata": raw_metadata,
        })
    return result


def _collect_repost_index(conn: sqlite3.Connection, target_id: int) -> list[dict]:
    """Return metadata-only repost history rows for a target, newest first."""
    rows = conn.execute("""
        SELECT *
        FROM repost_history
        WHERE target_id=?
        ORDER BY COALESCE(reposted_at, original_posted_at, discovered_at) DESC, id DESC
    """, (target_id,)).fetchall()

    result = []
    for row in rows:
        raw_metadata = {}
        if row["raw_metadata_json"]:
            try:
                parsed = json.loads(row["raw_metadata_json"])
                if isinstance(parsed, dict):
                    raw_metadata = parsed
            except json.JSONDecodeError:
                raw_metadata = {}

        effective_posted_at = row["reposted_at"] or row["original_posted_at"] or row["discovered_at"]
        result.append({
            "post_id": row["repost_id"],
            "post_type": "repost",
            "content_scope": "reposts",
            "collection_name": None,
            "url": row["repost_url"] or row["source_url"],
            "caption": row["original_caption"],
            "posted_at": effective_posted_at,
            "reposted_at": row["reposted_at"],
            "original_posted_at": row["original_posted_at"],
            "discovered_at": row["discovered_at"],
            "like_count": raw_metadata.get("like_count"),
            "comment_count": raw_metadata.get("comment_count"),
            "view_count": raw_metadata.get("view_count"),
            "share_count": raw_metadata.get("share_count"),
            "is_repost": True,
            "repost_source_url": row["source_url"],
            "repost_source_account": row["source_username"],
            "source_platform": row["source_platform"],
            "source_user_id": row["source_user_id"],
            "source_display_name": row["source_display_name"],
            "comments": [],
            "comment_tree": [],
            "media_paths": [],
            "behavior_category": row["behavior_category"],
            "behavior_confidence": row["behavior_confidence"],
            "classifier_version": row["classifier_version"],
            "vibe_score": row["vibe_score"],
            "vibe_label": row["vibe_label"],
            "raw_metadata": raw_metadata,
        })
    return result


def _collect_story_index(conn: sqlite3.Connection, target_id: int) -> list[dict]:
    """Return all stories for target, newest first, with media paths."""
    stories = conn.execute("""
        SELECT * FROM stories WHERE target_id=? ORDER BY discovered_at DESC
    """, (target_id,)).fetchall()

    result = []
    for s in stories:
        media_paths = _media_paths_for_story(conn, s["id"])
        result.append({
            "story_id": s["story_id"],
            "story_type": s["story_type"],
            "content_scope": s["content_scope"],
            "collection_name": s["collection_name"],
            "url": s["url"],
            "posted_at": s["posted_at"],
            "discovered_at": s["discovered_at"],
            "expires_at": s["expires_at"],
            "metadata": _story_metadata_for_profile(s["metadata_json"]),
            "media_paths": media_paths,
        })
    return result


def _linked_account_id(conn: sqlite3.Connection, target_id: int) -> int | None:
    """Return the accounts.id when a target also belongs to an owned account."""
    row = conn.execute("""
        SELECT a.id
        FROM accounts a
        JOIN targets t ON t.platform=a.platform AND t.username=a.username
        WHERE t.id=?
    """, (target_id,)).fetchone()
    return row["id"] if row else None


def _collect_follow_snapshots(
    conn: sqlite3.Connection,
    target_id: int,
    account_id: int | None,
    *,
    bio: dict | None = None,
) -> dict:
    """Return the latest follower/following snapshots for a monitored target."""
    from src.db import get_latest_follower_snapshot, get_latest_target_follow_snapshot

    followers = get_latest_target_follow_snapshot(conn, target_id, "followers")
    following = get_latest_target_follow_snapshot(conn, target_id, "following")
    follower_count_fallback = _coerce_optional_int((bio or {}).get("follower_count"))
    following_count_fallback = _coerce_optional_int((bio or {}).get("following_count"))

    if followers["snapshot_at"] is None and account_id is not None:
        followers = _normalise_snapshot_payload(
            get_latest_follower_snapshot(conn, account_id, "followers"),
            fallback_count=follower_count_fallback,
        )
    else:
        followers = _normalise_snapshot_payload(followers, fallback_count=follower_count_fallback)

    if following["snapshot_at"] is None and account_id is not None:
        following = _normalise_snapshot_payload(
            get_latest_follower_snapshot(conn, account_id, "following"),
            fallback_count=following_count_fallback,
        )
    else:
        following = _normalise_snapshot_payload(following, fallback_count=following_count_fallback)

    return {
        "followers": followers,
        "following": following,
    }


def _collect_follow_histories(
    conn: sqlite3.Connection,
    target_id: int,
    account_id: int | None,
) -> dict:
    """Return all exported follower/following history for a monitored target."""
    from src.db import get_follower_snapshot_history, get_target_follow_snapshot_history

    followers = get_target_follow_snapshot_history(conn, target_id, "followers")
    follower_source = "target"
    if not followers.get("snapshots") and account_id is not None:
        followers = get_follower_snapshot_history(conn, account_id, "followers")
        follower_source = "account"

    following = get_target_follow_snapshot_history(conn, target_id, "following")
    following_source = "target"
    if not following.get("snapshots") and account_id is not None:
        following = get_follower_snapshot_history(conn, account_id, "following")
        following_source = "account"

    return {
        "followers": _normalise_snapshot_history_payload(followers, source=follower_source),
        "following": _normalise_snapshot_history_payload(following, source=following_source),
    }


def _coerce_optional_int(value: object) -> int | None:
    """Best-effort conversion of arbitrary values into an integer count."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalise_snapshot_payload(snapshot: dict, *, fallback_count: int | None = None) -> dict:
    """Normalise DB snapshot helpers into a stable export shape."""
    users = []
    for user in snapshot.get("users", []):
        if not isinstance(user, dict):
            continue
        username = user.get("username")
        if not username:
            continue
        users.append({
            "username": username,
            "display_name": user.get("display_name"),
            "user_id": user.get("user_id"),
            "profile_pic_url": user.get("profile_pic_url"),
            "is_verified": bool(user.get("is_verified")),
            "is_private": bool(user.get("is_private")),
        })

    usernames = snapshot.get("usernames")
    if usernames is None:
        usernames = [user["username"] for user in users]
    resolved_count = _coerce_optional_int(snapshot.get("count"))
    if resolved_count is None:
        resolved_count = len(usernames)
    if snapshot.get("snapshot_at") is None and fallback_count is not None:
        resolved_count = max(resolved_count, fallback_count)
    return {
        "snapshot_at": snapshot.get("snapshot_at"),
        "count": resolved_count,
        "usernames": usernames,
        "users": users,
    }


def _normalise_snapshot_history_payload(payload: dict, *, source: str) -> dict:
    """Normalise snapshot history payloads into a stable export shape."""
    snapshots = []
    for snapshot in payload.get("snapshots", []):
        if not isinstance(snapshot, dict):
            continue
        snapshots.append(_normalise_snapshot_payload(snapshot))

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
                "times_seen": _coerce_optional_int(user.get("times_seen")) or 0,
                "currently_present": bool(user.get("currently_present")),
            }
        )

    return {
        "source": source,
        "count": len(users),
        "usernames": [user["username"] for user in users],
        "snapshots": snapshots,
        "users": users,
    }


def _snapshot_summary(snapshot: dict) -> dict:
    """Return a compact summary for top-level profile.json."""
    return {
        "snapshot_at": snapshot.get("snapshot_at"),
        "count": snapshot.get("count", 0),
    }


def _collect_connection_log(
    conn: sqlite3.Connection,
    target_id: int,
    account_id: int | None,
    event_types: tuple[str, ...],
) -> list[dict]:
    """Return connection event history for a monitored target, with account fallback."""
    placeholders = ", ".join("?" for _ in event_types)
    events = conn.execute(f"""
        SELECT event_type, target_username, detected_at
        FROM target_follow_events
        WHERE target_id=? AND event_type IN ({placeholders})
        ORDER BY detected_at DESC
    """, (target_id, *event_types)).fetchall()

    if not events and account_id is not None:
        events = conn.execute(f"""
            SELECT event_type, target_username, detected_at
            FROM follower_events
            WHERE account_id=? AND event_type IN ({placeholders})
            ORDER BY detected_at DESC
        """, (account_id, *event_types)).fetchall()

    return [
        {
            "event_type": event["event_type"],
            "target_username": event["target_username"],
            "detected_at": event["detected_at"],
        }
        for event in events
    ]


def _media_paths_for_post(conn: sqlite3.Connection, post_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT file_path FROM media_files WHERE post_id=? ORDER BY id ASC", (post_id,)
    ).fetchall()
    return [_relative_media_path(r["file_path"]) for r in rows]


def _media_paths_for_story(conn: sqlite3.Connection, story_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT file_path FROM media_files WHERE story_id=? ORDER BY id ASC", (story_id,)
    ).fetchall()
    return [_relative_media_path(r["file_path"]) for r in rows]


def _comments_for_post(
    conn: sqlite3.Connection,
    post_id: int,
    *,
    current_only: bool = True,
) -> list[dict]:
    where_clause = "WHERE post_id=?"
    params: list[object] = [post_id]
    if current_only:
        where_clause += " AND is_current=1"
    rows = conn.execute("""
        SELECT
            external_comment_id,
            comment_key,
            parent_comment_id,
            username,
            user_id,
            display_name,
            profile_pic_url,
            is_verified,
            text,
            commented_at,
            like_count,
            reply_count,
            depth,
            thread_path,
            comment_order,
            first_seen_at,
            last_seen_at,
            removed_at,
            is_current
        FROM post_comments
        {where_clause}
        ORDER BY comment_order ASC NULLS LAST, commented_at ASC NULLS LAST, id ASC
    """.format(where_clause=where_clause), params).fetchall()
    return normalize_comment_records([
        {
            "comment_id": row["external_comment_id"],
            "comment_key": row["comment_key"],
            "parent_comment_id": row["parent_comment_id"],
            "username": row["username"],
            "user_id": row["user_id"],
            "display_name": row["display_name"],
            "profile_pic_url": row["profile_pic_url"],
            "is_verified": bool(row["is_verified"]),
            "text": row["text"],
            "commented_at": row["commented_at"],
            "like_count": row["like_count"],
            "reply_count": row["reply_count"],
            "depth": row["depth"],
            "thread_path": row["thread_path"],
            "comment_order": row["comment_order"],
            "first_seen_at": row["first_seen_at"],
            "last_seen_at": row["last_seen_at"],
            "removed_at": row["removed_at"],
            "is_current": bool(row["is_current"]),
        }
        for row in rows
    ])


def _story_metadata_for_profile(value: str | None) -> dict:
    """Parse story metadata JSON for profile output."""
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _relative_media_path(file_path: str) -> str:
    """
    Normalise an absolute or relative file_path to be relative to the data/ directory.
    Example: /app/data/media/ig/user/... → media/ig/user/...
    """
    p = Path(file_path)
    parts = p.parts
    try:
        # Find 'media' or 'profiles' segment and return from there
        for keyword in ("media", "profiles"):
            if keyword in parts:
                idx = parts.index(keyword)
                return str(Path(*parts[idx:]))
    except Exception:
        pass
    return file_path


def _media_summary(conn: sqlite3.Connection, target_id: int) -> dict:
    """Compute aggregate media statistics for this target."""
    total_posts = conn.execute(
        "SELECT COUNT(*) AS cnt FROM posts WHERE target_id=? AND content_scope!='reposts'", (target_id,)
    ).fetchone()["cnt"]

    total_stories = conn.execute(
        "SELECT COUNT(*) AS cnt FROM stories WHERE target_id=?", (target_id,)
    ).fetchone()["cnt"]

    # media_files linked via posts
    media_stats = conn.execute("""
        SELECT COUNT(*) AS cnt, SUM(file_size) AS total_size
        FROM media_files mf
        WHERE mf.post_id IN (SELECT id FROM posts WHERE target_id=? AND content_scope!='reposts')
           OR mf.story_id IN (SELECT id FROM stories WHERE target_id=?)
    """, (target_id, target_id)).fetchone()

    # First / last post dates
    date_range = conn.execute("""
        SELECT MIN(discovered_at) AS first_at, MAX(discovered_at) AS last_at
        FROM posts WHERE target_id=?
    """, (target_id,)).fetchone()

    return {
        "total_posts": total_posts,
        "total_stories_captured": total_stories,
        "total_mention_posts": conn.execute(
            "SELECT COUNT(*) AS cnt FROM posts WHERE target_id=? AND content_scope='mentions'",
            (target_id,),
        ).fetchone()["cnt"],
        "total_repost_posts": conn.execute(
            "SELECT COUNT(*) AS cnt FROM repost_history WHERE target_id=?",
            (target_id,),
        ).fetchone()["cnt"],
        "total_highlight_items": conn.execute(
            "SELECT COUNT(*) AS cnt FROM stories WHERE target_id=? AND content_scope='highlight'",
            (target_id,),
        ).fetchone()["cnt"],
        "total_media_files": media_stats["cnt"] or 0,
        "total_size_bytes": media_stats["total_size"] or 0,
        "first_post_at": date_range["first_at"],
        "last_post_at": date_range["last_at"],
    }
