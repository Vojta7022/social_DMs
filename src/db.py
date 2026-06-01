"""
db.py — SQLite schema and query helpers.

All timestamps are stored as ISO 8601 UTC strings (e.g. "2026-03-29T12:00:00Z").
WAL mode is enabled for safe concurrent reads while the scheduler writes.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from src.comment_utils import normalize_comment_records


# ─── Schema DDL ──────────────────────────────────────────────────────────────

_SCHEMA = """
-- Own login accounts (the identities used to scrape)
CREATE TABLE IF NOT EXISTS accounts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    platform     TEXT NOT NULL CHECK(platform IN ('instagram','tiktok','facebook')),
    username     TEXT NOT NULL,
    session_file TEXT NOT NULL,
    proxy_id     INTEGER REFERENCES proxies(id),
    is_active    INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE(platform, username)
);

-- Accounts being monitored
CREATE TABLE IF NOT EXISTS targets (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    platform         TEXT NOT NULL CHECK(platform IN ('instagram','tiktok','facebook')),
    username         TEXT NOT NULL,
    display_name     TEXT,
    bio_snapshot     TEXT,
    follower_count   INTEGER,
    following_count  INTEGER,
    post_count       INTEGER,
    profile_pic_url  TEXT,
    cover_photo_url  TEXT,
    is_active        INTEGER NOT NULL DEFAULT 1,
    last_scraped_at  TEXT,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE(platform, username)
);

-- Posts / reels discovered on target profiles
CREATE TABLE IF NOT EXISTS posts (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id             INTEGER NOT NULL REFERENCES targets(id),
    platform              TEXT NOT NULL,
    post_id               TEXT NOT NULL,
    post_type             TEXT NOT NULL CHECK(post_type IN ('photo','video','reel','carousel','repost')),
    content_scope         TEXT NOT NULL DEFAULT 'feed',
    collection_name       TEXT,
    url                   TEXT NOT NULL,
    caption               TEXT,
    like_count            INTEGER,
    comment_count         INTEGER,
    share_count           INTEGER,
    view_count            INTEGER,
    is_repost             INTEGER NOT NULL DEFAULT 0,
    repost_source_url     TEXT,
    repost_source_account TEXT,
    posted_at             TEXT,
    discovered_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    raw_metadata_json     TEXT,
    notified              INTEGER NOT NULL DEFAULT 0,
    UNIQUE(target_id, platform, post_id, content_scope)
);

-- Metadata-only repost history captured from repost/share surfaces
CREATE TABLE IF NOT EXISTS repost_history (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id           INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
    platform            TEXT NOT NULL,
    repost_id           TEXT NOT NULL,
    source_platform     TEXT,
    source_username     TEXT,
    source_user_id      TEXT,
    source_display_name TEXT,
    source_url          TEXT,
    repost_url          TEXT,
    original_caption    TEXT,
    reposted_at         TEXT,
    original_posted_at  TEXT,
    discovered_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    behavior_category   TEXT,
    behavior_confidence REAL,
    classifier_version  TEXT,
    vibe_score          REAL,
    vibe_label          TEXT,
    raw_metadata_json   TEXT,
    UNIQUE(target_id, platform, repost_id)
);
CREATE INDEX IF NOT EXISTS idx_repost_history_target_time
    ON repost_history(target_id, reposted_at, discovered_at);
CREATE INDEX IF NOT EXISTS idx_repost_history_target_source
    ON repost_history(target_id, source_username);
CREATE INDEX IF NOT EXISTS idx_repost_history_target_category
    ON repost_history(target_id, behavior_category);

-- Stories discovered on target profiles
CREATE TABLE IF NOT EXISTS stories (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id     INTEGER NOT NULL REFERENCES targets(id),
    platform      TEXT NOT NULL,
    story_id      TEXT NOT NULL,
    story_type    TEXT NOT NULL CHECK(story_type IN ('image','video','text')),
    content_scope TEXT NOT NULL DEFAULT 'story',
    collection_name TEXT,
    url           TEXT,
    expires_at    TEXT,
    posted_at     TEXT,
    metadata_json TEXT,
    discovered_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    notified      INTEGER NOT NULL DEFAULT 0,
    UNIQUE(platform, story_id)
);
CREATE INDEX IF NOT EXISTS idx_posts_target_discovered
    ON posts(target_id, discovered_at DESC);
CREATE INDEX IF NOT EXISTS idx_posts_target_notified
    ON posts(notified, discovered_at ASC);
CREATE INDEX IF NOT EXISTS idx_stories_target_discovered
    ON stories(target_id, discovered_at DESC);
CREATE INDEX IF NOT EXISTS idx_stories_notified
    ON stories(notified, discovered_at ASC);

-- Point-in-time snapshots of follower/following lists for own accounts
CREATE TABLE IF NOT EXISTS follower_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    INTEGER NOT NULL REFERENCES accounts(id),
    snapshot_type TEXT NOT NULL CHECK(snapshot_type IN ('followers','following')),
    username      TEXT NOT NULL,
    display_name  TEXT,
    user_id       TEXT,
    profile_pic_url TEXT,
    is_verified   INTEGER NOT NULL DEFAULT 0,
    is_private    INTEGER NOT NULL DEFAULT 0,
    snapshot_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_follower_snapshots_account
    ON follower_snapshots(account_id, snapshot_type, snapshot_at);

-- Derived delta events (new follower, unfollower, etc.)
CREATE TABLE IF NOT EXISTS follower_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL REFERENCES accounts(id),
    event_type      TEXT NOT NULL CHECK(event_type IN (
                        'gained_follower','lost_follower',
                        'started_following','stopped_following'
                    )),
    target_username TEXT NOT NULL,
    detected_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    notified        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_follower_events_account
    ON follower_events(account_id, detected_at);

-- Point-in-time snapshots of follower/following lists for monitored targets
CREATE TABLE IF NOT EXISTS target_follow_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id     INTEGER NOT NULL REFERENCES targets(id),
    snapshot_type TEXT NOT NULL CHECK(snapshot_type IN ('followers','following')),
    username      TEXT NOT NULL,
    display_name  TEXT,
    user_id       TEXT,
    profile_pic_url TEXT,
    is_verified   INTEGER NOT NULL DEFAULT 0,
    is_private    INTEGER NOT NULL DEFAULT 0,
    snapshot_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_target_follow_snapshots_target
    ON target_follow_snapshots(target_id, snapshot_type, snapshot_at);

-- Derived delta events (new follower, unfollower, etc.) for monitored targets
CREATE TABLE IF NOT EXISTS target_follow_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id       INTEGER NOT NULL REFERENCES targets(id),
    event_type      TEXT NOT NULL CHECK(event_type IN (
                        'gained_follower','lost_follower',
                        'started_following','stopped_following'
                    )),
    target_username TEXT NOT NULL,
    detected_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    notified        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_target_follow_events_target
    ON target_follow_events(target_id, detected_at);

-- Downloaded media files on disk
CREATE TABLE IF NOT EXISTS media_files (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id       INTEGER REFERENCES posts(id),
    story_id      INTEGER REFERENCES stories(id),
    file_path     TEXT NOT NULL UNIQUE,
    file_type     TEXT NOT NULL CHECK(file_type IN ('photo','video','screenshot','thumbnail')),
    sha256        TEXT,
    file_size     INTEGER,
    width         INTEGER,
    height        INTEGER,
    duration_s    REAL,
    downloaded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_media_files_sha256 ON media_files(sha256);

-- Manually approved / rejected cross-platform profile links
CREATE TABLE IF NOT EXISTS cross_platform_links (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source_platform  TEXT NOT NULL,
    source_username  TEXT NOT NULL,
    target_platform  TEXT NOT NULL,
    target_username  TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending'
                         CHECK(status IN ('approved','rejected','pending')),
    confidence       REAL,
    reasons          TEXT,  -- JSON array of strings
    evidence         TEXT,  -- JSON object
    face_score       REAL,  -- facial-recognition similarity (0-1), null if not run
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE(source_platform, source_username, target_platform, target_username)
);
CREATE INDEX IF NOT EXISTS idx_cross_platform_links_source
    ON cross_platform_links(source_platform, source_username, status);

-- Account-level DM conversations captured from owned accounts
CREATE TABLE IF NOT EXISTS dm_conversations (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id           INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    platform             TEXT NOT NULL,
    external_thread_id   TEXT NOT NULL,
    conversation_type    TEXT NOT NULL DEFAULT 'direct',
    title                TEXT,
    subtitle             TEXT,
    conversation_url     TEXT,
    last_message_preview TEXT,
    last_message_at      TEXT,
    discovered_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    last_scraped_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    is_group             INTEGER NOT NULL DEFAULT 0,
    is_broadcast_channel INTEGER NOT NULL DEFAULT 0,
    metadata_json        TEXT,
    UNIQUE(account_id, platform, external_thread_id)
);
CREATE INDEX IF NOT EXISTS idx_dm_conversations_account
    ON dm_conversations(account_id, platform, last_message_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS dm_participants (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id   INTEGER NOT NULL REFERENCES dm_conversations(id) ON DELETE CASCADE,
    username          TEXT,
    user_id           TEXT,
    display_name      TEXT,
    profile_url       TEXT,
    profile_pic_url   TEXT,
    is_self           INTEGER NOT NULL DEFAULT 0,
    participant_order INTEGER,
    metadata_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_dm_participants_conversation
    ON dm_participants(conversation_id, participant_order, id);

CREATE TABLE IF NOT EXISTS dm_messages (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id      INTEGER NOT NULL REFERENCES dm_conversations(id) ON DELETE CASCADE,
    platform             TEXT NOT NULL,
    external_message_id  TEXT,
    message_key          TEXT NOT NULL,
    sender_username      TEXT,
    sender_user_id       TEXT,
    sender_display_name  TEXT,
    sent_at              TEXT,
    sent_at_label        TEXT,
    message_type         TEXT NOT NULL DEFAULT 'text',
    text                 TEXT,
    status_text          TEXT,
    reply_to_message_id  TEXT,
    message_order        INTEGER,
    is_self              INTEGER NOT NULL DEFAULT 0,
    raw_payload_json     TEXT,
    discovered_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    last_seen_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE(conversation_id, message_key)
);
CREATE INDEX IF NOT EXISTS idx_dm_messages_conversation
    ON dm_messages(conversation_id, message_order, sent_at, id);

CREATE TABLE IF NOT EXISTS dm_media_files (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id       INTEGER NOT NULL REFERENCES dm_messages(id) ON DELETE CASCADE,
    attachment_id    TEXT,
    attachment_order INTEGER NOT NULL DEFAULT 0,
    file_path        TEXT NOT NULL UNIQUE,
    file_type        TEXT NOT NULL DEFAULT 'unknown',
    mime_type        TEXT,
    sha256           TEXT,
    file_size        INTEGER,
    width            INTEGER,
    height           INTEGER,
    duration_s       REAL,
    source_url       TEXT,
    preview_text     TEXT,
    metadata_json    TEXT,
    downloaded_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_dm_media_files_message
    ON dm_media_files(message_id, attachment_order, id);

-- Comments captured for a post archive
CREATE TABLE IF NOT EXISTS post_comments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id             INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    platform            TEXT NOT NULL,
    external_comment_id TEXT,
    comment_key         TEXT,
    parent_comment_id   TEXT,
    root_comment_id     TEXT,
    reply_to_comment_id TEXT,
    username            TEXT,
    user_id             TEXT,
    display_name        TEXT,
    profile_pic_url     TEXT,
    is_verified         INTEGER NOT NULL DEFAULT 0,
    text                TEXT NOT NULL,
    commented_at        TEXT,
    commented_at_label  TEXT,
    like_count          INTEGER,
    reply_count         INTEGER,
    depth               INTEGER NOT NULL DEFAULT 0,
    thread_path         TEXT,
    comment_order       INTEGER,
    discovered_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    first_seen_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    last_seen_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    removed_at          TEXT,
    is_current          INTEGER NOT NULL DEFAULT 1,
    raw_payload_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_post_comments_post_id ON post_comments(post_id);
CREATE INDEX IF NOT EXISTS idx_post_comments_thread_path
    ON post_comments(post_id, thread_path);
CREATE UNIQUE INDEX IF NOT EXISTS idx_post_comments_external
    ON post_comments(post_id, external_comment_id);
CREATE INDEX IF NOT EXISTS idx_post_comments_comment_key
    ON post_comments(post_id, comment_key);
CREATE INDEX IF NOT EXISTS idx_post_comments_current
    ON post_comments(post_id, is_current, comment_order, commented_at, id);

-- Proxy pool (mirrors settings.yaml but persists failure state)
CREATE TABLE IF NOT EXISTS proxies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    url           TEXT NOT NULL UNIQUE,
    is_active     INTEGER NOT NULL DEFAULT 1,
    last_used_at  TEXT,
    failure_count INTEGER NOT NULL DEFAULT 0
);

-- Audit log of every scrape job execution
CREATE TABLE IF NOT EXISTS scrape_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type    TEXT NOT NULL,
    platform    TEXT,
    account_id  INTEGER REFERENCES accounts(id),
    target_id   INTEGER REFERENCES targets(id),
    status      TEXT NOT NULL CHECK(status IN ('success','error','skipped')),
    detail      TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_scrape_log_started ON scrape_log(started_at);
"""


# ─── Connection ──────────────────────────────────────────────────────────────

def init_db(db_path: Path) -> sqlite3.Connection:
    """Create the DB file, apply schema DDL, enable WAL mode. Return a connection."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    _ensure_runtime_migrations(conn)
    conn.commit()
    return conn


def get_connection(db_path: Path) -> sqlite3.Connection:
    """Return a new connection with Row factory and WAL mode."""
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _ensure_runtime_migrations(conn)
    return conn


def _ensure_runtime_migrations(conn: sqlite3.Connection) -> None:
    """Apply additive schema migrations for existing SQLite files."""
    _ensure_column(conn, "posts", "content_scope", "TEXT NOT NULL DEFAULT 'feed'")
    _ensure_column(conn, "posts", "collection_name", "TEXT")
    _ensure_column(conn, "stories", "content_scope", "TEXT NOT NULL DEFAULT 'story'")
    _ensure_column(conn, "stories", "collection_name", "TEXT")
    _ensure_column(conn, "stories", "posted_at", "TEXT")
    _ensure_column(conn, "stories", "metadata_json", "TEXT")
    _ensure_column(conn, "follower_snapshots", "user_id", "TEXT")
    _ensure_column(conn, "follower_snapshots", "profile_pic_url", "TEXT")
    _ensure_column(conn, "follower_snapshots", "is_verified", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "follower_snapshots", "is_private", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "target_follow_snapshots", "display_name", "TEXT")
    _ensure_column(conn, "target_follow_snapshots", "user_id", "TEXT")
    _ensure_column(conn, "target_follow_snapshots", "profile_pic_url", "TEXT")
    _ensure_column(conn, "target_follow_snapshots", "is_verified", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "target_follow_snapshots", "is_private", "INTEGER NOT NULL DEFAULT 0")
    _ensure_posts_scope_uniqueness(conn)
    # Performance indexes for large datasets (36 GB+).
    # CREATE INDEX IF NOT EXISTS is idempotent — safe to run on every startup.
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_posts_target_discovered
            ON posts(target_id, discovered_at DESC);
        CREATE INDEX IF NOT EXISTS idx_posts_target_notified
            ON posts(notified, discovered_at ASC);
        CREATE INDEX IF NOT EXISTS idx_stories_target_discovered
            ON stories(target_id, discovered_at DESC);
        CREATE INDEX IF NOT EXISTS idx_stories_notified
            ON stories(notified, discovered_at ASC);
    """)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS repost_history (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            target_id           INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
            platform            TEXT NOT NULL,
            repost_id           TEXT NOT NULL,
            source_platform     TEXT,
            source_username     TEXT,
            source_user_id      TEXT,
            source_display_name TEXT,
            source_url          TEXT,
            repost_url          TEXT,
            original_caption    TEXT,
            reposted_at         TEXT,
            original_posted_at  TEXT,
            discovered_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            behavior_category   TEXT,
            behavior_confidence REAL,
            classifier_version  TEXT,
            vibe_score          REAL,
            vibe_label          TEXT,
            raw_metadata_json   TEXT,
            UNIQUE(target_id, platform, repost_id)
        );
        CREATE INDEX IF NOT EXISTS idx_repost_history_target_time
            ON repost_history(target_id, reposted_at, discovered_at);
        CREATE INDEX IF NOT EXISTS idx_repost_history_target_source
            ON repost_history(target_id, source_username);
        CREATE INDEX IF NOT EXISTS idx_repost_history_target_category
            ON repost_history(target_id, behavior_category);
        CREATE TABLE IF NOT EXISTS target_follow_snapshots (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            target_id     INTEGER NOT NULL REFERENCES targets(id),
            snapshot_type TEXT NOT NULL CHECK(snapshot_type IN ('followers','following')),
            username      TEXT NOT NULL,
            display_name  TEXT,
            user_id       TEXT,
            profile_pic_url TEXT,
            is_verified   INTEGER NOT NULL DEFAULT 0,
            is_private    INTEGER NOT NULL DEFAULT 0,
            snapshot_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_target_follow_snapshots_target
            ON target_follow_snapshots(target_id, snapshot_type, snapshot_at);
        CREATE TABLE IF NOT EXISTS target_follow_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            target_id       INTEGER NOT NULL REFERENCES targets(id),
            event_type      TEXT NOT NULL CHECK(event_type IN (
                                'gained_follower','lost_follower',
                                'started_following','stopped_following'
                            )),
            target_username TEXT NOT NULL,
            detected_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            notified        INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_target_follow_events_target
            ON target_follow_events(target_id, detected_at);
        CREATE TABLE IF NOT EXISTS post_comments (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id             INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
            platform            TEXT NOT NULL,
            external_comment_id TEXT,
            comment_key         TEXT,
            parent_comment_id   TEXT,
            root_comment_id     TEXT,
            reply_to_comment_id TEXT,
            username            TEXT,
            user_id             TEXT,
            display_name        TEXT,
            profile_pic_url     TEXT,
            is_verified         INTEGER NOT NULL DEFAULT 0,
            text                TEXT NOT NULL,
            commented_at        TEXT,
            commented_at_label  TEXT,
            like_count          INTEGER,
            reply_count         INTEGER,
            depth               INTEGER NOT NULL DEFAULT 0,
            thread_path         TEXT,
            comment_order       INTEGER,
            discovered_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            first_seen_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            last_seen_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            removed_at          TEXT,
            is_current          INTEGER NOT NULL DEFAULT 1,
            raw_payload_json    TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_post_comments_post_id ON post_comments(post_id);
        CREATE INDEX IF NOT EXISTS idx_post_comments_thread_path
            ON post_comments(post_id, thread_path);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_post_comments_external
            ON post_comments(post_id, external_comment_id);
        CREATE TABLE IF NOT EXISTS dm_conversations (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id           INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            platform             TEXT NOT NULL,
            external_thread_id   TEXT NOT NULL,
            conversation_type    TEXT NOT NULL DEFAULT 'direct',
            title                TEXT,
            subtitle             TEXT,
            conversation_url     TEXT,
            last_message_preview TEXT,
            last_message_at      TEXT,
            discovered_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            last_scraped_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            is_group             INTEGER NOT NULL DEFAULT 0,
            is_broadcast_channel INTEGER NOT NULL DEFAULT 0,
            metadata_json        TEXT,
            UNIQUE(account_id, platform, external_thread_id)
        );
        CREATE INDEX IF NOT EXISTS idx_dm_conversations_account
            ON dm_conversations(account_id, platform, last_message_at DESC, id DESC);
        CREATE TABLE IF NOT EXISTS dm_participants (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id   INTEGER NOT NULL REFERENCES dm_conversations(id) ON DELETE CASCADE,
            username          TEXT,
            user_id           TEXT,
            display_name      TEXT,
            profile_url       TEXT,
            profile_pic_url   TEXT,
            is_self           INTEGER NOT NULL DEFAULT 0,
            participant_order INTEGER,
            metadata_json     TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_dm_participants_conversation
            ON dm_participants(conversation_id, participant_order, id);
        CREATE TABLE IF NOT EXISTS dm_messages (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id      INTEGER NOT NULL REFERENCES dm_conversations(id) ON DELETE CASCADE,
            platform             TEXT NOT NULL,
            external_message_id  TEXT,
            message_key          TEXT NOT NULL,
            sender_username      TEXT,
            sender_user_id       TEXT,
            sender_display_name  TEXT,
            sent_at              TEXT,
            sent_at_label        TEXT,
            message_type         TEXT NOT NULL DEFAULT 'text',
            text                 TEXT,
            status_text          TEXT,
            reply_to_message_id  TEXT,
            message_order        INTEGER,
            is_self              INTEGER NOT NULL DEFAULT 0,
            raw_payload_json     TEXT,
            discovered_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            last_seen_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            UNIQUE(conversation_id, message_key)
        );
        CREATE INDEX IF NOT EXISTS idx_dm_messages_conversation
            ON dm_messages(conversation_id, message_order, sent_at, id);
        CREATE TABLE IF NOT EXISTS dm_media_files (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id       INTEGER NOT NULL REFERENCES dm_messages(id) ON DELETE CASCADE,
            attachment_id    TEXT,
            attachment_order INTEGER NOT NULL DEFAULT 0,
            file_path        TEXT NOT NULL UNIQUE,
            file_type        TEXT NOT NULL DEFAULT 'unknown',
            mime_type        TEXT,
            sha256           TEXT,
            file_size        INTEGER,
            width            INTEGER,
            height           INTEGER,
            duration_s       REAL,
            source_url       TEXT,
            preview_text     TEXT,
            metadata_json    TEXT,
            downloaded_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_dm_media_files_message
            ON dm_media_files(message_id, attachment_order, id);
    """)
    _ensure_column(conn, "post_comments", "depth", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "post_comments", "thread_path", "TEXT")
    _ensure_column(conn, "post_comments", "comment_order", "INTEGER")
    _ensure_column(conn, "post_comments", "comment_key", "TEXT")
    _ensure_column(conn, "post_comments", "root_comment_id", "TEXT")
    _ensure_column(conn, "post_comments", "reply_to_comment_id", "TEXT")
    _ensure_column(conn, "post_comments", "commented_at_label", "TEXT")
    _ensure_column(conn, "post_comments", "first_seen_at", "TEXT")
    _ensure_column(conn, "post_comments", "last_seen_at", "TEXT")
    _ensure_column(conn, "post_comments", "removed_at", "TEXT")
    _ensure_column(conn, "post_comments", "is_current", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "post_comments", "raw_payload_json", "TEXT")
    _ensure_column(conn, "posts", "raw_metadata_json", "TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_post_comments_comment_key ON post_comments(post_id, comment_key)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_post_comments_current ON post_comments(post_id, is_current, comment_order, commented_at, id)"
    )
    _backfill_post_comment_retention_metadata(conn)
    _backfill_repost_history_from_posts(conn)

    # Persistent scanner settings (intervals, enabled flags, batch state)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS scanner_config (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        );
    """)
    _seed_scanner_config_defaults(conn)

    # Phase 12 — interactive commands
    _ensure_column(conn, "targets", "is_paused", "INTEGER NOT NULL DEFAULT 0")

    # Phase 14 — Gemini AI analysis for reposts
    _ensure_column(conn, "repost_history", "ai_analysis_json", "TEXT")

    # Phase 15 — social proximity scoring
    _ensure_column(conn, "targets", "proximity_json", "TEXT")
    _ensure_column(conn, "targets", "cover_photo_url", "TEXT")

    # Phase 13 — profile and avatar history tables
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS target_profile_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            target_id   INTEGER NOT NULL REFERENCES targets(id),
            field       TEXT NOT NULL,
            old_value   TEXT,
            new_value   TEXT NOT NULL,
            detected_at TEXT NOT NULL,
            notified    INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_tph_target_id
            ON target_profile_history(target_id);

        CREATE TABLE IF NOT EXISTS target_avatar_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            target_id   INTEGER NOT NULL REFERENCES targets(id),
            asset_kind  TEXT NOT NULL DEFAULT 'avatar',
            file_path   TEXT NOT NULL,
            sha256      TEXT NOT NULL,
            detected_at TEXT NOT NULL,
            notified    INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_tah_target_id
            ON target_avatar_history(target_id);
    """)
    _ensure_column(conn, "target_avatar_history", "asset_kind", "TEXT NOT NULL DEFAULT 'avatar'")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tah_target_asset ON target_avatar_history(target_id, asset_kind, detected_at)"
    )

    # Phase 15 — vibe shift alert log
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS vibe_shift_alerts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            target_id   INTEGER NOT NULL REFERENCES targets(id),
            delta       REAL NOT NULL,
            direction   TEXT NOT NULL,
            description TEXT,
            detected_at TEXT NOT NULL,
            notified    INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_vsa_target_id
            ON vibe_shift_alerts(target_id, detected_at);
    """)

    # Phase 16 — digital twin draft queue + audit trail
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS message_drafts (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            platform             TEXT NOT NULL,
            account_id           INTEGER REFERENCES accounts(id),
            account_username     TEXT NOT NULL,
            target_id            INTEGER REFERENCES targets(id),
            target_username      TEXT NOT NULL,
            conversation_row_id  INTEGER REFERENCES dm_conversations(id),
            conversation_id      TEXT,
            source_message_id    TEXT,
            source_story_id      TEXT,
            trigger_type         TEXT NOT NULL,
            trigger_payload_json TEXT,
            route                TEXT NOT NULL DEFAULT 'blocked',
            route_reason         TEXT,
            prompt_version       TEXT,
            gemini_context_json  TEXT,
            intent_label         TEXT,
            route_hint           TEXT,
            style_notes          TEXT,
            risk_flags_json      TEXT,
            context_sources_json TEXT,
            draft_text           TEXT NOT NULL,
            approved_text        TEXT,
            review_status        TEXT NOT NULL DEFAULT 'pending_review',
            send_status          TEXT NOT NULL DEFAULT 'not_sent',
            telegram_chat_id     TEXT,
            telegram_thread_id   INTEGER,
            telegram_message_id  INTEGER,
            editing_user_id      TEXT,
            editing_username     TEXT,
            approved_by_user_id  TEXT,
            approved_by_username TEXT,
            rejected_by_user_id  TEXT,
            rejected_by_username TEXT,
            last_error           TEXT,
            created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            updated_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            reviewed_at          TEXT,
            sent_at              TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_message_drafts_review
            ON message_drafts(review_status, created_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_message_drafts_send
            ON message_drafts(send_status, created_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_message_drafts_target
            ON message_drafts(platform, target_username, created_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_message_drafts_source
            ON message_drafts(platform, account_username, conversation_id, source_message_id, trigger_type);

        CREATE TABLE IF NOT EXISTS message_draft_events (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            draft_id       INTEGER NOT NULL REFERENCES message_drafts(id) ON DELETE CASCADE,
            event_type     TEXT NOT NULL,
            actor_user_id  TEXT,
            actor_username TEXT,
            payload_json   TEXT,
            created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_message_draft_events_draft
            ON message_draft_events(draft_id, created_at, id);
    """)


def _seed_scanner_config_defaults(conn: sqlite3.Connection) -> None:
    """Insert default scanner_config entries only when a key doesn't exist yet."""
    defaults = {
        # Global on/off — '0' means paused on boot, '1' means auto-resume
        "is_scanning": "0",
        # Per-job scheduler run intervals (minutes between each batch run)
        "stories_interval_minutes": "360",       # 6h
        "posts_interval_minutes": "1440",        # 24h (batch covers 7-day cycle)
        "highlights_interval_minutes": "1440",   # 24h
        "mentions_interval_minutes": "1440",     # 24h (batch covers 7-day cycle)
        "followers_interval_minutes": "2880",    # 48h
        "reposts_interval_minutes": "1440",      # 24h
        # Per-job enabled flags ('0' or '1')
        "stories_enabled": "1",
        "posts_enabled": "1",
        "highlights_enabled": "1",
        "mentions_enabled": "1",
        "followers_enabled": "1",
        "reposts_enabled": "0",     # off by default per user request
        # Per-job batch sizes (targets per scheduler run; 0 = no batching = all targets)
        "stories_batch_size": "50",
        "posts_batch_size": "30",
        "highlights_batch_size": "50",
        "mentions_batch_size": "30",
        "followers_batch_size": "100",
        "reposts_batch_size": "30",
        # Current batch rotation index per job (incremented after each run)
        "stories_batch_index": "0",
        "posts_batch_index": "0",
        "highlights_batch_index": "0",
        "mentions_batch_index": "0",
        "followers_batch_index": "0",
        "reposts_batch_index": "0",
    }
    for key, value in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO scanner_config (key, value) VALUES (?, ?)",
            (key, value),
        )
    conn.commit()


# ─── Scanner config helpers ───────────────────────────────────────────────────

def get_config(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM scanner_config WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_config(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO scanner_config (key, value, updated_at)
        VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (key, value),
    )
    conn.commit()


def get_all_config(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM scanner_config ORDER BY key").fetchall()
    return {r["key"]: r["value"] for r in rows}


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _ensure_posts_scope_uniqueness(conn: sqlite3.Connection) -> None:
    """Rebuild posts when needed so uniqueness is tracked per target and scope."""
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(posts)").fetchall()
    }
    row = conn.execute("""
        SELECT sql
        FROM sqlite_master
        WHERE type='table' AND name='posts'
    """).fetchone()
    table_sql = str(row["sql"] or "") if row else ""
    normalised_sql = re.sub(r"\s+", " ", table_sql).upper()
    if "UNIQUE(TARGET_ID, PLATFORM, POST_ID, CONTENT_SCOPE)" in normalised_sql:
        return

    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        raw_metadata_select = "raw_metadata_json" if "raw_metadata_json" in columns else "NULL"
        conn.execute("DROP TABLE IF EXISTS posts_new")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS posts_new (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                target_id             INTEGER NOT NULL REFERENCES targets(id),
                platform              TEXT NOT NULL,
                post_id               TEXT NOT NULL,
                post_type             TEXT NOT NULL CHECK(post_type IN ('photo','video','reel','carousel','repost')),
                content_scope         TEXT NOT NULL DEFAULT 'feed',
                collection_name       TEXT,
                url                   TEXT NOT NULL,
                caption               TEXT,
                like_count            INTEGER,
                comment_count         INTEGER,
                share_count           INTEGER,
                view_count            INTEGER,
                is_repost             INTEGER NOT NULL DEFAULT 0,
                repost_source_url     TEXT,
                repost_source_account TEXT,
                posted_at             TEXT,
                discovered_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                raw_metadata_json     TEXT,
                notified              INTEGER NOT NULL DEFAULT 0,
                UNIQUE(target_id, platform, post_id, content_scope)
            )
        """)
        conn.execute("""
            INSERT INTO posts_new (
                id, target_id, platform, post_id, post_type, content_scope, collection_name, url,
                caption, like_count, comment_count, share_count, view_count,
                is_repost, repost_source_url, repost_source_account, posted_at, discovered_at,
                raw_metadata_json, notified
            )
            SELECT
                id, target_id, platform, post_id, post_type, content_scope, collection_name, url,
                caption, like_count, comment_count, share_count, view_count,
                is_repost,
                repost_source_url,
                repost_source_account,
                posted_at,
                COALESCE(discovered_at, strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                {raw_metadata_select},
                COALESCE(notified, 0)
            FROM posts
        """.format(raw_metadata_select=raw_metadata_select))
        conn.execute("DROP TABLE posts")
        conn.execute("ALTER TABLE posts_new RENAME TO posts")
        conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _stable_comment_key(
    external_comment_id: str | None,
    parent_comment_id: str | None,
    username: str | None,
    user_id: str | None,
    text: str | None,
    commented_at: str | None,
) -> str | None:
    """Return a stable comment identity usable across refresh runs."""
    normalized_external = str(external_comment_id or "").strip()
    if normalized_external:
        return f"id:{normalized_external}"

    payload = "|".join(
        [
            str(parent_comment_id or "").strip(),
            str(username or "").strip().casefold(),
            str(user_id or "").strip(),
            str(commented_at or "").strip(),
            str(text or "").strip(),
        ]
    )
    if not payload.strip("|"):
        return None
    return f"hash:{hashlib.sha1(payload.encode('utf-8')).hexdigest()}"


def _backfill_post_comment_retention_metadata(conn: sqlite3.Connection) -> None:
    """Populate retention columns for pre-existing comment rows after migration."""
    rows = conn.execute(
        """
        SELECT id, external_comment_id, parent_comment_id, username, user_id, text,
               commented_at, discovered_at, first_seen_at, last_seen_at, is_current, comment_key
        FROM post_comments
        """
    ).fetchall()
    updates: list[tuple[str | None, str | None, str | None, int, int]] = []
    for row in rows:
        comment_key = row["comment_key"] or _stable_comment_key(
            row["external_comment_id"],
            row["parent_comment_id"],
            row["username"],
            row["user_id"],
            row["text"],
            row["commented_at"],
        )
        discovered_at = row["discovered_at"] or _now()
        first_seen_at = row["first_seen_at"] or discovered_at
        last_seen_at = row["last_seen_at"] or discovered_at
        is_current = int(bool(row["is_current"])) if row["is_current"] is not None else 1
        updates.append((comment_key, first_seen_at, last_seen_at, is_current, row["id"]))

    if updates:
        conn.executemany(
            """
            UPDATE post_comments
            SET comment_key=?, first_seen_at=?, last_seen_at=?, is_current=?
            WHERE id=?
            """,
            updates,
        )
        conn.commit()


def _backfill_repost_history_from_posts(conn: sqlite3.Connection) -> None:
    """Seed repost_history from legacy posts rows so existing archives remain visible."""
    repost_rows = conn.execute("""
        SELECT
            p.target_id,
            p.platform,
            p.post_id,
            p.url,
            p.caption,
            p.repost_source_url,
            p.repost_source_account,
            p.posted_at,
            p.discovered_at
        FROM posts p
        WHERE p.content_scope='reposts'
          AND NOT EXISTS (
              SELECT 1
              FROM repost_history rh
              WHERE rh.target_id=p.target_id
                AND rh.platform=p.platform
                AND rh.repost_id=p.post_id
          )
    """).fetchall()
    if not repost_rows:
        return

    for row in repost_rows:
        raw_metadata = {
            "migrated_from_posts": True,
            "legacy_post_id": row["post_id"],
            "legacy_repost_source_url": row["repost_source_url"],
        }
        conn.execute("""
            INSERT INTO repost_history (
                target_id, platform, repost_id, source_platform, source_username,
                source_url, repost_url, original_caption, reposted_at,
                original_posted_at, discovered_at, raw_metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            row["target_id"],
            row["platform"],
            row["post_id"],
            row["platform"],
            row["repost_source_account"],
            row["repost_source_url"],
            row["url"],
            row["caption"],
            None,
            row["posted_at"],
            row["discovered_at"] or _now(),
            _json_dumps(raw_metadata),
        ))
    conn.commit()


# ─── Timestamp helper ────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _normalise_snapshot_entries(users: list[str | dict]) -> list[dict]:
    """Return stable, deduplicated snapshot entries with optional metadata."""
    deduped: dict[str, dict] = {}
    for entry in users:
        if isinstance(entry, dict):
            username = str(entry.get("username") or "").strip()
            user_id = str(entry.get("user_id") or "").strip() or None
            display_name = entry.get("display_name")
            profile_pic_url = entry.get("profile_pic_url")
            is_verified = bool(entry.get("is_verified"))
            is_private = bool(entry.get("is_private"))
        else:
            username = str(entry).strip()
            user_id = None
            display_name = None
            profile_pic_url = None
            is_verified = False
            is_private = False
        if not username:
            continue
        identity = f"id:{user_id}" if user_id else f"username:{username.casefold()}"
        current = deduped.get(identity) or {}
        deduped[identity] = {
            "username": username,
            "display_name": display_name or current.get("display_name"),
            "user_id": user_id or current.get("user_id"),
            "profile_pic_url": profile_pic_url or current.get("profile_pic_url"),
            "is_verified": is_verified or bool(current.get("is_verified")),
            "is_private": is_private if is_private else bool(current.get("is_private")),
        }
    return sorted(
        deduped.values(),
        key=lambda item: (
            str(item.get("username") or "").casefold(),
            str(item.get("user_id") or ""),
        ),
    )


def _snapshot_identity(username: str | None, user_id: str | None) -> str | None:
    """Return a stable snapshot identity, preferring numeric user id over username."""
    normalized_user_id = str(user_id or "").strip()
    if normalized_user_id:
        return f"id:{normalized_user_id}"

    normalized_username = str(username or "").strip()
    if normalized_username:
        return f"username:{normalized_username.casefold()}"
    return None


def _json_dumps(value) -> str | None:
    """Serialize structured payloads to JSON while accepting strings."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _stable_dm_message_key(message: dict) -> str:
    """Build a deterministic message key when platforms do not expose one cleanly."""
    for field in ("message_key", "message_id", "external_message_id", "offline_threading_id"):
        value = str(message.get(field) or "").strip()
        if value:
            return value

    payload = {
        "sender_user_id": message.get("sender_user_id"),
        "sender_username": message.get("sender_username"),
        "sent_at": message.get("sent_at"),
        "text": message.get("text"),
        "message_type": message.get("message_type"),
        "attachments": message.get("attachments") or [],
    }
    digest = hashlib.sha1(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"synthetic:{digest}"


def _normalise_dm_participants(participants: list[dict]) -> list[dict]:
    """Return participants with stable ordering and duplicate collapse."""
    deduped: dict[str, dict] = {}
    ordered_keys: list[str] = []
    for index, participant in enumerate(participants or []):
        username = str(participant.get("username") or "").strip()
        user_id = str(participant.get("user_id") or "").strip()
        profile_url = str(participant.get("profile_url") or "").strip()
        identity = "||".join([username, user_id, profile_url])
        if not identity.strip("|"):
            identity = f"row:{index}"
        if identity not in deduped:
            ordered_keys.append(identity)
            deduped[identity] = {
                "username": username or None,
                "user_id": user_id or None,
                "display_name": participant.get("display_name"),
                "profile_url": profile_url or None,
                "profile_pic_url": participant.get("profile_pic_url"),
                "is_self": bool(participant.get("is_self")),
                "participant_order": participant.get("participant_order", index),
                "metadata": participant.get("metadata"),
            }
            continue

        current = deduped[identity]
        for field in ("display_name", "profile_pic_url"):
            if not current.get(field) and participant.get(field):
                current[field] = participant.get(field)
        if participant.get("is_self"):
            current["is_self"] = True

    return [deduped[key] for key in ordered_keys]


# ─── Accounts ────────────────────────────────────────────────────────────────

def upsert_account(conn: sqlite3.Connection, platform: str, username: str,
                   session_file: str, proxy_id: int | None = None) -> int:
    """Insert or update an account row. Return accounts.id."""
    conn.execute("""
        INSERT INTO accounts (platform, username, session_file, proxy_id)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(platform, username) DO UPDATE SET
            session_file = excluded.session_file,
            proxy_id     = excluded.proxy_id
    """, (platform, username, session_file, proxy_id))
    conn.commit()
    row = conn.execute(
        "SELECT id FROM accounts WHERE platform=? AND username=?", (platform, username)
    ).fetchone()
    return row["id"]


def get_account_id(conn: sqlite3.Connection, platform: str, username: str) -> int:
    """Return accounts.id for given platform + username. Raise if not found."""
    row = conn.execute(
        "SELECT id FROM accounts WHERE platform=? AND username=?", (platform, username)
    ).fetchone()
    if not row:
        raise ValueError(f"Account not found in DB: {platform}/{username}")
    return row["id"]


# ─── Targets ─────────────────────────────────────────────────────────────────

def upsert_target(conn: sqlite3.Connection, platform: str, username: str,
                  **fields) -> int:
    """Insert or update a target row. Return targets.id."""
    allowed = {"display_name", "bio_snapshot", "follower_count", "following_count",
               "post_count", "profile_pic_url", "cover_photo_url", "last_scraped_at"}
    updates = {k: v for k, v in fields.items() if k in allowed}

    conn.execute("""
        INSERT INTO targets (platform, username)
        VALUES (?, ?)
        ON CONFLICT(platform, username) DO NOTHING
    """, (platform, username))

    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE targets SET {set_clause} WHERE platform=? AND username=?",
            list(updates.values()) + [platform, username],
        )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM targets WHERE platform=? AND username=?", (platform, username)
    ).fetchone()
    return row["id"]


def get_target_id(conn: sqlite3.Connection, platform: str, username: str) -> int:
    """Return targets.id. Raise if not found."""
    row = conn.execute(
        "SELECT id FROM targets WHERE platform=? AND username=?", (platform, username)
    ).fetchone()
    if not row:
        raise ValueError(f"Target not found in DB: {platform}/{username}")
    return row["id"]


def get_target_row(conn: sqlite3.Connection, platform: str, username: str) -> sqlite3.Row | None:
    """Return the full targets row for a platform/username pair."""
    return conn.execute(
        "SELECT * FROM targets WHERE platform=? AND username=?",
        (platform, username),
    ).fetchone()


def get_account_row(conn: sqlite3.Connection, platform: str, username: str) -> sqlite3.Row | None:
    """Return the full accounts row for a platform/username pair."""
    return conn.execute(
        "SELECT * FROM accounts WHERE platform=? AND username=?",
        (platform, username),
    ).fetchone()


# ─── Posts ───────────────────────────────────────────────────────────────────

def _default_post_raw_metadata(post: dict) -> dict:
    """Preserve the full discovered post payload minus derived archive trees."""
    raw = dict(post)
    raw.pop("raw_metadata", None)
    raw.pop("comment_tree", None)
    raw.pop("comment_history", None)
    raw.pop("comment_history_tree", None)
    return raw


def _post_raw_metadata(post: dict):
    """Combine explicit raw payloads with the normalised post fields."""
    raw = _default_post_raw_metadata(post)
    explicit = post.get("raw_metadata")
    if explicit is None:
        return raw
    if isinstance(explicit, dict):
        raw.update(explicit)
        return raw
    return explicit

def insert_post(conn: sqlite3.Connection, target_id: int,
                post: dict) -> int | None:
    """Insert a new post row. Return new row id, or None if duplicate."""
    raw_metadata = _post_raw_metadata(post)
    try:
        cursor = conn.execute("""
            INSERT INTO posts (
                target_id, platform, post_id, post_type, content_scope, collection_name, url, caption,
                like_count, comment_count, share_count, view_count,
                is_repost, repost_source_url, repost_source_account, posted_at, raw_metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            target_id,
            post["platform"],
            post["post_id"],
            post["post_type"],
            post.get("content_scope", "feed"),
            post.get("collection_name"),
            post["url"],
            post.get("caption"),
            post.get("like_count"),
            post.get("comment_count"),
            post.get("share_count"),
            post.get("view_count"),
            int(post.get("is_repost", False)),
            post.get("repost_source_url"),
            post.get("repost_source_account"),
            post.get("posted_at"),
            _json_dumps(raw_metadata),
        ))
        conn.commit()
        return cursor.lastrowid
    except sqlite3.IntegrityError:
        return None  # duplicate


def update_post(conn: sqlite3.Connection, post_row_id: int, post: dict) -> None:
    """Refresh mutable metadata for an existing post row."""
    allowed_fields = [
        "post_type",
        "content_scope",
        "collection_name",
        "url",
        "caption",
        "like_count",
        "comment_count",
        "share_count",
        "view_count",
        "is_repost",
        "repost_source_url",
        "repost_source_account",
        "posted_at",
    ]
    updates = {
        field: post.get(field)
        for field in allowed_fields
        if field in post
    }
    if "is_repost" in updates:
        updates["is_repost"] = int(bool(updates["is_repost"]))
    raw_metadata = _post_raw_metadata(post)
    updates["raw_metadata_json"] = _json_dumps(raw_metadata)
    if not updates:
        return

    assignments = ", ".join(f"{field}=?" for field in updates)
    conn.execute(
        f"UPDATE posts SET {assignments} WHERE id=?",
        [*updates.values(), post_row_id],
    )
    conn.commit()


def get_post_row_id(
    conn: sqlite3.Connection,
    target_id: int,
    platform: str,
    post_id: str,
    content_scope: str = "feed",
) -> int | None:
    """Return an existing posts.id for a target/scope combination, or None if missing."""
    row = conn.execute(
        """
        SELECT id
        FROM posts
        WHERE target_id=? AND platform=? AND post_id=? AND content_scope=?
        """,
        (target_id, platform, post_id, content_scope),
    ).fetchone()
    return row["id"] if row else None


def get_existing_post_ids_for_target(
    conn: sqlite3.Connection,
    target_id: int,
    *,
    content_scope: str | None = None,
    require_media: bool = False,
) -> set[str]:
    """Return known post_ids for one target, optionally only rows that already have media."""
    sql = """
        SELECT DISTINCT p.post_id
        FROM posts p
        WHERE p.target_id=?
    """
    params: list[object] = [target_id]
    if content_scope is not None:
        sql += " AND p.content_scope=?"
        params.append(content_scope)
    if require_media:
        sql += " AND EXISTS (SELECT 1 FROM media_files mf WHERE mf.post_id=p.id)"

    rows = conn.execute(sql, params).fetchall()
    return {
        str(row["post_id"])
        for row in rows
        if row["post_id"] is not None
    }


def post_has_media(conn: sqlite3.Connection, post_row_id: int) -> bool:
    """Return True if any media_files rows already reference this post."""
    row = conn.execute(
        "SELECT 1 FROM media_files WHERE post_id=? LIMIT 1",
        (post_row_id,),
    ).fetchone()
    return row is not None


def get_post_media_records(conn: sqlite3.Connection, post_row_id: int) -> list[sqlite3.Row]:
    """Return all media_files rows for a post, ordered by insertion."""
    return conn.execute(
        """
        SELECT id, file_path, sha256, file_type, file_size, width, height, duration_s, downloaded_at
        FROM media_files
        WHERE post_id=?
        ORDER BY id
        """,
        (post_row_id,),
    ).fetchall()


def get_post_media_count(conn: sqlite3.Connection, post_row_id: int) -> int:
    """Return the number of media_files rows associated with a post."""
    row = conn.execute(
        "SELECT COUNT(*) AS count FROM media_files WHERE post_id=?",
        (post_row_id,),
    ).fetchone()
    return int(row["count"]) if row else 0


def delete_post_media(conn: sqlite3.Connection, post_row_id: int) -> list[str]:
    """Delete all media_files rows for a post and return their file paths."""
    rows = get_post_media_records(conn, post_row_id)
    file_paths = [row["file_path"] for row in rows]
    conn.execute("DELETE FROM media_files WHERE post_id=?", (post_row_id,))
    conn.commit()
    return file_paths


def replace_post_comments(
    conn: sqlite3.Connection,
    post_row_id: int,
    platform: str,
    comments: list[dict],
) -> None:
    """Merge a fresh comment snapshot while retaining historical comment rows."""
    snapshot_at = _now()
    normalised_comments = normalize_comment_records(comments)
    seen_comment_keys: set[str] = set()
    for comment in normalised_comments:
        text = str(comment.get("text") or "")
        external_comment_id = comment.get("comment_id") or None
        if not text and not external_comment_id:
            continue
        comment_key = _stable_comment_key(
            external_comment_id,
            comment.get("parent_comment_id") or None,
            comment.get("username"),
            comment.get("user_id"),
            text,
            comment.get("commented_at"),
        )
        if not comment_key:
            continue
        seen_comment_keys.add(comment_key)

        existing_row = conn.execute(
            """
            SELECT id
            FROM post_comments
            WHERE post_id=?
              AND (
                comment_key=?
                OR (? IS NOT NULL AND external_comment_id=?)
              )
            ORDER BY id ASC
            LIMIT 1
            """,
            (post_row_id, comment_key, external_comment_id, external_comment_id),
        ).fetchone()

        raw_payload_json = _json_dumps(comment.get("raw_payload"))
        payload = (
            platform,
            external_comment_id,
            comment_key,
            comment.get("parent_comment_id") or None,
            comment.get("root_comment_id") or None,
            comment.get("reply_to_comment_id") or None,
            comment.get("username"),
            comment.get("user_id"),
            comment.get("display_name"),
            comment.get("profile_pic_url"),
            int(bool(comment.get("is_verified"))),
            text,
            comment.get("commented_at"),
            comment.get("commented_at_label"),
            comment.get("like_count"),
            comment.get("reply_count"),
            comment.get("depth"),
            comment.get("thread_path"),
            comment.get("comment_order"),
            snapshot_at,
        )

        if existing_row:
            conn.execute(
                """
                UPDATE post_comments
                SET platform=?,
                    external_comment_id=?,
                    comment_key=?,
                    parent_comment_id=?,
                    root_comment_id=?,
                    reply_to_comment_id=?,
                    username=?,
                    user_id=?,
                    display_name=?,
                    profile_pic_url=?,
                    is_verified=?,
                    text=?,
                    commented_at=?,
                    commented_at_label=?,
                    like_count=?,
                    reply_count=?,
                    depth=?,
                    thread_path=?,
                    comment_order=?,
                    raw_payload_json=?,
                    last_seen_at=?,
                    removed_at=NULL,
                    is_current=1
                WHERE id=?
                """,
                (*payload[:-1], raw_payload_json, payload[-1], existing_row["id"]),
            )
        else:
            conn.execute(
                """
                INSERT INTO post_comments (
                    post_id, platform, external_comment_id, comment_key, parent_comment_id,
                    root_comment_id, reply_to_comment_id,
                    username, user_id, display_name, profile_pic_url,
                    is_verified, text, commented_at, commented_at_label,
                    like_count, reply_count, depth, thread_path, comment_order,
                    first_seen_at, last_seen_at, is_current, raw_payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    post_row_id,
                    *payload[:-1],
                    snapshot_at,
                    snapshot_at,
                    raw_payload_json,
                ),
            )

    current_rows = conn.execute(
        """
        SELECT id, comment_key
        FROM post_comments
        WHERE post_id=? AND is_current=1
        """,
        (post_row_id,),
    ).fetchall()
    rows_to_archive = [
        row["id"]
        for row in current_rows
        if row["comment_key"] and row["comment_key"] not in seen_comment_keys
    ]
    if rows_to_archive:
        placeholders = ", ".join("?" for _ in rows_to_archive)
        conn.execute(
            f"""
            UPDATE post_comments
            SET is_current=0,
                removed_at=COALESCE(removed_at, ?)
            WHERE id IN ({placeholders})
            """,
            [snapshot_at, *rows_to_archive],
        )
    elif not seen_comment_keys:
        conn.execute(
            """
            UPDATE post_comments
            SET is_current=0,
                removed_at=COALESCE(removed_at, ?)
            WHERE post_id=? AND is_current=1
            """,
            (snapshot_at, post_row_id),
        )
    conn.commit()


def get_post_comments(
    conn: sqlite3.Connection,
    post_row_id: int,
    *,
    current_only: bool = True,
) -> list[sqlite3.Row]:
    """Return archived comments for a post, optionally limited to current comments."""
    where_clause = "WHERE post_id=?"
    params: list[object] = [post_row_id]
    if current_only:
        where_clause += " AND is_current=1"
    return conn.execute("""
        SELECT *
        FROM post_comments
        {where_clause}
        ORDER BY comment_order ASC NULLS LAST, commented_at ASC NULLS LAST, id ASC
    """.format(where_clause=where_clause), params).fetchall()


def get_post_comment_history(conn: sqlite3.Connection, post_row_id: int) -> list[sqlite3.Row]:
    """Return all archived comment rows for a post, including removed comments."""
    return conn.execute("""
        SELECT *
        FROM post_comments
        WHERE post_id=?
        ORDER BY
            COALESCE(first_seen_at, discovered_at) ASC NULLS LAST,
            comment_order ASC NULLS LAST,
            commented_at ASC NULLS LAST,
            id ASC
    """, (post_row_id,)).fetchall()


def get_post_media_files(conn: sqlite3.Connection, post_row_id: int) -> list[sqlite3.Row]:
    """Return photo/video media rows for a post, excluding thumbnails."""
    return conn.execute("""
        SELECT id, file_path, file_type, downloaded_at
        FROM media_files
        WHERE post_id=? AND file_type IN ('photo', 'video')
        ORDER BY id ASC
    """, (post_row_id,)).fetchall()


def get_story_media_files(conn: sqlite3.Connection, story_row_id: int) -> list[sqlite3.Row]:
    """Return media rows for a story, excluding thumbnails."""
    return conn.execute("""
        SELECT id, file_path, file_type, file_size, width, height, duration_s, downloaded_at
        FROM media_files
        WHERE story_id=? AND file_type IN ('photo', 'video', 'screenshot')
        ORDER BY id ASC
    """, (story_row_id,)).fetchall()


# ─── Repost history ──────────────────────────────────────────────────────────

def insert_repost_history(conn: sqlite3.Connection, target_id: int, repost: dict) -> int | None:
    """Insert a metadata-only repost history row. Return row id, or None if duplicate."""
    values = _repost_history_values(target_id, repost)
    try:
        cursor = conn.execute("""
            INSERT INTO repost_history (
                target_id, platform, repost_id, source_platform, source_username,
                source_user_id, source_display_name, source_url, repost_url,
                original_caption, reposted_at, original_posted_at, discovered_at,
                behavior_category, behavior_confidence, classifier_version,
                vibe_score, vibe_label, raw_metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, values)
        conn.commit()
        return cursor.lastrowid
    except sqlite3.IntegrityError:
        return None


def update_repost_history(conn: sqlite3.Connection, repost_row_id: int, repost: dict) -> None:
    """Refresh mutable metadata for an existing repost_history row."""
    fields = _repost_history_update_fields(repost)
    if not fields:
        return

    assignments = ", ".join(f"{field}=?" for field in fields)
    conn.execute(
        f"UPDATE repost_history SET {assignments} WHERE id=?",
        [*fields.values(), repost_row_id],
    )
    conn.commit()


def get_repost_history_row_id(
    conn: sqlite3.Connection,
    target_id: int,
    platform: str,
    repost_id: str,
) -> int | None:
    """Return the existing repost_history id for a target/platform/repost_id tuple."""
    row = conn.execute("""
        SELECT id
        FROM repost_history
        WHERE target_id=? AND platform=? AND repost_id=?
    """, (target_id, platform, repost_id)).fetchone()
    return row["id"] if row else None


def get_existing_repost_ids_for_target(conn: sqlite3.Connection, target_id: int) -> set[str]:
    """Return repost ids already known for a target."""
    rows = conn.execute(
        "SELECT repost_id FROM repost_history WHERE target_id=?",
        (target_id,),
    ).fetchall()
    return {
        str(row["repost_id"])
        for row in rows
        if row["repost_id"] is not None
    }


def get_repost_history_for_target(conn: sqlite3.Connection, target_id: int) -> list[sqlite3.Row]:
    """Return metadata-only repost history rows for one target, newest first."""
    return conn.execute("""
        SELECT *
        FROM repost_history
        WHERE target_id=?
        ORDER BY COALESCE(reposted_at, original_posted_at, discovered_at) DESC, id DESC
    """, (target_id,)).fetchall()


def _repost_history_values(target_id: int, repost: dict) -> tuple[object, ...]:
    repost_id = str(repost.get("repost_id") or repost.get("post_id") or "").strip()
    if not repost_id:
        raise ValueError("Repost history rows require repost_id or post_id.")

    raw_metadata = repost.get("raw_metadata")
    if raw_metadata is None:
        raw_metadata = _default_repost_raw_metadata(repost)

    return (
        target_id,
        repost["platform"],
        repost_id,
        repost.get("source_platform") or repost.get("platform"),
        repost.get("source_username"),
        repost.get("source_user_id"),
        repost.get("source_display_name"),
        repost.get("source_url"),
        repost.get("repost_url") or repost.get("url"),
        repost.get("original_caption") if "original_caption" in repost else repost.get("caption"),
        repost.get("reposted_at"),
        repost.get("original_posted_at") if "original_posted_at" in repost else repost.get("posted_at"),
        repost.get("discovered_at") or _now(),
        repost.get("behavior_category"),
        repost.get("behavior_confidence"),
        repost.get("classifier_version"),
        repost.get("vibe_score"),
        repost.get("vibe_label"),
        _json_dumps(raw_metadata),
    )


def _repost_history_update_fields(repost: dict) -> dict[str, object]:
    raw_metadata = repost.get("raw_metadata")
    if raw_metadata is None:
        raw_metadata = _default_repost_raw_metadata(repost)

    fields = {
        "source_platform": repost.get("source_platform") or repost.get("platform"),
        "source_username": repost.get("source_username"),
        "source_user_id": repost.get("source_user_id"),
        "source_display_name": repost.get("source_display_name"),
        "source_url": repost.get("source_url"),
        "repost_url": repost.get("repost_url") or repost.get("url"),
        "original_caption": repost.get("original_caption") if "original_caption" in repost else repost.get("caption"),
        "reposted_at": repost.get("reposted_at"),
        "original_posted_at": repost.get("original_posted_at") if "original_posted_at" in repost else repost.get("posted_at"),
        "behavior_category": repost.get("behavior_category"),
        "behavior_confidence": repost.get("behavior_confidence"),
        "classifier_version": repost.get("classifier_version"),
        "vibe_score": repost.get("vibe_score"),
        "vibe_label": repost.get("vibe_label"),
        "raw_metadata_json": _json_dumps(raw_metadata),
    }
    return fields


def _default_repost_raw_metadata(repost: dict) -> dict:
    raw = dict(repost)
    raw.pop("media_items", None)
    raw.pop("comments", None)
    raw.pop("comment_tree", None)
    return raw


def get_story_media_records(conn: sqlite3.Connection, story_row_id: int) -> list[sqlite3.Row]:
    """Return all media_files rows for a story, ordered by insertion."""
    return conn.execute("""
        SELECT id, file_path, sha256, file_type, file_size, width, height, duration_s, downloaded_at
        FROM media_files
        WHERE story_id=?
        ORDER BY id ASC
    """, (story_row_id,)).fetchall()


# ─── Stories ─────────────────────────────────────────────────────────────────

def _serialise_story_metadata(value) -> str | None:
    """Store story metadata dicts as JSON while accepting pre-serialised strings."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def insert_story(conn: sqlite3.Connection, target_id: int,
                 story: dict) -> int | None:
    """Insert a new story row. Return new row id, or None if duplicate."""
    try:
        cursor = conn.execute("""
            INSERT INTO stories (
                target_id, platform, story_id, story_type, content_scope,
                collection_name, url, expires_at, posted_at, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            target_id,
            story["platform"],
            story["story_id"],
            story["story_type"],
            story.get("content_scope", "story"),
            story.get("collection_name"),
            story.get("url"),
            story.get("expires_at"),
            story.get("posted_at"),
            _serialise_story_metadata(story.get("metadata")),
        ))
        conn.commit()
        return cursor.lastrowid
    except sqlite3.IntegrityError:
        return None


def update_story(conn: sqlite3.Connection, story_row_id: int, story: dict) -> None:
    """Refresh mutable metadata for an existing story row."""
    updates = {
        "story_type": story.get("story_type"),
        "content_scope": story.get("content_scope", "story"),
        "collection_name": story.get("collection_name"),
        "url": story.get("url"),
        "expires_at": story.get("expires_at"),
        "posted_at": story.get("posted_at"),
        "metadata_json": _serialise_story_metadata(story.get("metadata")),
    }
    assignments = ", ".join(f"{field}=?" for field in updates)
    conn.execute(
        f"UPDATE stories SET {assignments} WHERE id=?",
        [*updates.values(), story_row_id],
    )
    conn.commit()


def get_story_row_id(conn: sqlite3.Connection, platform: str, story_id: str) -> int | None:
    """Return an existing stories.id for platform/story_id, or None if missing."""
    row = conn.execute(
        "SELECT id FROM stories WHERE platform=? AND story_id=?",
        (platform, story_id),
    ).fetchone()
    return row["id"] if row else None


def get_existing_story_ids_for_target(
    conn: sqlite3.Connection,
    target_id: int,
    *,
    content_scope: str | None = None,
    require_media: bool = False,
) -> set[str]:
    """Return known story_ids for one target, optionally only rows that already have media."""
    sql = """
        SELECT DISTINCT s.story_id
        FROM stories s
        WHERE s.target_id=?
    """
    params: list[object] = [target_id]
    if content_scope is not None:
        sql += " AND s.content_scope=?"
        params.append(content_scope)
    if require_media:
        sql += " AND EXISTS (SELECT 1 FROM media_files mf WHERE mf.story_id=s.id)"

    rows = conn.execute(sql, params).fetchall()
    return {
        str(row["story_id"])
        for row in rows
        if row["story_id"] is not None
    }


def story_has_media(conn: sqlite3.Connection, story_row_id: int) -> bool:
    """Return True if any media_files rows already reference this story."""
    row = conn.execute(
        "SELECT 1 FROM media_files WHERE story_id=? LIMIT 1",
        (story_row_id,),
    ).fetchone()
    return row is not None


def get_story_media_count(conn: sqlite3.Connection, story_row_id: int) -> int:
    """Return the number of media_files rows associated with a story."""
    row = conn.execute(
        "SELECT COUNT(*) AS count FROM media_files WHERE story_id=?",
        (story_row_id,),
    ).fetchone()
    return int(row["count"]) if row else 0


# ─── Media files ─────────────────────────────────────────────────────────────

def sha256_exists(conn: sqlite3.Connection, sha256: str) -> bool:
    """Return True if any media_file row has this sha256 hash."""
    row = conn.execute(
        "SELECT 1 FROM media_files WHERE sha256=? LIMIT 1", (sha256,)
    ).fetchone()
    return row is not None


def media_sha_exists_for_owner(
    conn: sqlite3.Connection,
    sha256: str,
    *,
    post_id: int | None = None,
    story_id: int | None = None,
    file_type: str | None = None,
) -> bool:
    """Return True when the same media hash is already attached to this post/story."""
    if not sha256:
        return False

    if post_id is not None:
        sql = "SELECT 1 FROM media_files WHERE sha256=? AND post_id=?"
        params: list[object] = [sha256, post_id]
    elif story_id is not None:
        sql = "SELECT 1 FROM media_files WHERE sha256=? AND story_id=?"
        params = [sha256, story_id]
    else:
        return False

    if file_type:
        sql += " AND file_type=?"
        params.append(file_type)

    sql += " LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    return row is not None


def insert_media_file(conn: sqlite3.Connection, file_path: str, sha256: str,
                      **meta) -> int | None:
    """Insert a media_file row. Return new row id, or None on uniqueness conflicts."""
    allowed = {"post_id", "story_id", "file_type", "file_size", "width", "height", "duration_s"}
    filtered = {k: v for k, v in meta.items() if k in allowed}
    cols = ["file_path", "sha256"] + list(filtered.keys())
    vals = [file_path, sha256] + list(filtered.values())
    placeholders = ", ".join("?" for _ in vals)
    try:
        cursor = conn.execute(
            f"INSERT INTO media_files ({', '.join(cols)}) VALUES ({placeholders})",
            vals,
        )
        conn.commit()
        return cursor.lastrowid
    except sqlite3.IntegrityError:
        return None  # file_path duplicate


def update_media_file(conn: sqlite3.Connection, media_file_id: int, **fields) -> None:
    """Update mutable metadata for a media_files row."""
    allowed = {"file_path", "file_size", "width", "height", "duration_s", "file_type"}
    filtered = {k: v for k, v in fields.items() if k in allowed}
    if not filtered:
        return
    assignments = ", ".join(f"{field}=?" for field in filtered)
    conn.execute(
        f"UPDATE media_files SET {assignments} WHERE id=?",
        [*filtered.values(), media_file_id],
    )
    conn.commit()


# ─── Direct messages ─────────────────────────────────────────────────────────

def upsert_dm_conversation(
    conn: sqlite3.Connection,
    account_id: int,
    conversation: dict,
) -> int:
    """Insert or update one DM conversation row and return dm_conversations.id."""
    now = _now()
    payload = {
        "platform": conversation["platform"],
        "external_thread_id": conversation["conversation_id"],
        "conversation_type": conversation.get("conversation_type", "direct"),
        "title": conversation.get("title"),
        "subtitle": conversation.get("subtitle"),
        "conversation_url": conversation.get("conversation_url"),
        "last_message_preview": conversation.get("last_message_preview"),
        "last_message_at": conversation.get("last_message_at"),
        "is_group": int(bool(conversation.get("is_group"))),
        "is_broadcast_channel": int(bool(conversation.get("is_broadcast_channel"))),
        "metadata_json": _json_dumps(conversation.get("metadata")),
    }

    conn.execute(
        """
        INSERT INTO dm_conversations (
            account_id, platform, external_thread_id, conversation_type,
            title, subtitle, conversation_url, last_message_preview,
            last_message_at, last_scraped_at, is_group, is_broadcast_channel, metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(account_id, platform, external_thread_id) DO UPDATE SET
            conversation_type    = excluded.conversation_type,
            title                = excluded.title,
            subtitle             = excluded.subtitle,
            conversation_url     = excluded.conversation_url,
            last_message_preview = excluded.last_message_preview,
            last_message_at      = excluded.last_message_at,
            last_scraped_at      = excluded.last_scraped_at,
            is_group             = excluded.is_group,
            is_broadcast_channel = excluded.is_broadcast_channel,
            metadata_json        = excluded.metadata_json
        """,
        (
            account_id,
            payload["platform"],
            payload["external_thread_id"],
            payload["conversation_type"],
            payload["title"],
            payload["subtitle"],
            payload["conversation_url"],
            payload["last_message_preview"],
            payload["last_message_at"],
            now,
            payload["is_group"],
            payload["is_broadcast_channel"],
            payload["metadata_json"],
        ),
    )
    conn.commit()
    row = conn.execute(
        """
        SELECT id
        FROM dm_conversations
        WHERE account_id=? AND platform=? AND external_thread_id=?
        """,
        (account_id, payload["platform"], payload["external_thread_id"]),
    ).fetchone()
    return row["id"]


def replace_dm_participants(
    conn: sqlite3.Connection,
    conversation_row_id: int,
    participants: list[dict],
) -> None:
    """Replace the participant roster for a DM conversation."""
    conn.execute("DELETE FROM dm_participants WHERE conversation_id=?", (conversation_row_id,))

    rows = []
    for index, participant in enumerate(_normalise_dm_participants(participants)):
        rows.append(
            (
                conversation_row_id,
                participant.get("username"),
                participant.get("user_id"),
                participant.get("display_name"),
                participant.get("profile_url"),
                participant.get("profile_pic_url"),
                int(bool(participant.get("is_self"))),
                participant.get("participant_order", index),
                _json_dumps(participant.get("metadata")),
            )
        )

    if rows:
        conn.executemany(
            """
            INSERT INTO dm_participants (
                conversation_id, username, user_id, display_name, profile_url,
                profile_pic_url, is_self, participant_order, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    conn.commit()


def upsert_dm_message(
    conn: sqlite3.Connection,
    conversation_row_id: int,
    platform: str,
    message: dict,
) -> int:
    """Insert or update one DM message row and return dm_messages.id."""
    now = _now()
    message_key = _stable_dm_message_key(message)
    raw_payload = message.get("raw_payload")

    conn.execute(
        """
        INSERT INTO dm_messages (
            conversation_id, platform, external_message_id, message_key,
            sender_username, sender_user_id, sender_display_name, sent_at, sent_at_label,
            message_type, text, status_text, reply_to_message_id, message_order,
            is_self, raw_payload_json, discovered_at, last_seen_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(conversation_id, message_key) DO UPDATE SET
            external_message_id = excluded.external_message_id,
            sender_username     = excluded.sender_username,
            sender_user_id      = excluded.sender_user_id,
            sender_display_name = excluded.sender_display_name,
            sent_at             = excluded.sent_at,
            sent_at_label       = excluded.sent_at_label,
            message_type        = excluded.message_type,
            text                = excluded.text,
            status_text         = excluded.status_text,
            reply_to_message_id = excluded.reply_to_message_id,
            message_order       = excluded.message_order,
            is_self             = excluded.is_self,
            raw_payload_json    = excluded.raw_payload_json,
            last_seen_at        = excluded.last_seen_at
        """,
        (
            conversation_row_id,
            platform,
            message.get("message_id"),
            message_key,
            message.get("sender_username"),
            message.get("sender_user_id"),
            message.get("sender_display_name"),
            message.get("sent_at"),
            message.get("sent_at_label"),
            message.get("message_type", "text"),
            message.get("text"),
            message.get("status_text"),
            message.get("reply_to_message_id"),
            message.get("message_order"),
            int(bool(message.get("is_self"))),
            _json_dumps(raw_payload if raw_payload is not None else message),
            now,
            now,
        ),
    )
    conn.commit()
    row = conn.execute(
        """
        SELECT id
        FROM dm_messages
        WHERE conversation_id=? AND message_key=?
        """,
        (conversation_row_id, message_key),
    ).fetchone()
    return row["id"]


def replace_dm_message_media(
    conn: sqlite3.Connection,
    message_row_id: int,
    attachments: list[dict],
) -> None:
    """Replace the stored attachment descriptors for one DM message."""
    conn.execute("DELETE FROM dm_media_files WHERE message_id=?", (message_row_id,))

    rows = []
    for index, attachment in enumerate(attachments or []):
        file_path = attachment.get("file_path")
        if not file_path:
            continue
        rows.append(
            (
                message_row_id,
                attachment.get("attachment_id"),
                attachment.get("attachment_order", index),
                file_path,
                attachment.get("file_type", "unknown"),
                attachment.get("mime_type"),
                attachment.get("sha256"),
                attachment.get("file_size"),
                attachment.get("width"),
                attachment.get("height"),
                attachment.get("duration_s"),
                attachment.get("source_url"),
                attachment.get("preview_text"),
                _json_dumps(attachment.get("metadata")),
            )
        )

    if rows:
        conn.executemany(
            """
            INSERT INTO dm_media_files (
                message_id, attachment_id, attachment_order, file_path, file_type,
                mime_type, sha256, file_size, width, height, duration_s,
                source_url, preview_text, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    conn.commit()


def get_dm_conversations_for_account(conn: sqlite3.Connection, account_id: int) -> list[sqlite3.Row]:
    """Return DM conversations for an owned account, newest activity first."""
    return conn.execute(
        """
        SELECT *
        FROM dm_conversations
        WHERE account_id=?
        ORDER BY last_message_at DESC NULLS LAST, last_scraped_at DESC, id DESC
        """,
        (account_id,),
    ).fetchall()


def get_dm_participants(conn: sqlite3.Connection, conversation_row_id: int) -> list[sqlite3.Row]:
    """Return participants for one DM conversation."""
    return conn.execute(
        """
        SELECT *
        FROM dm_participants
        WHERE conversation_id=?
        ORDER BY participant_order ASC NULLS LAST, id ASC
        """,
        (conversation_row_id,),
    ).fetchall()


def get_dm_messages(conn: sqlite3.Connection, conversation_row_id: int) -> list[sqlite3.Row]:
    """Return ordered DM messages for one conversation."""
    return conn.execute(
        """
        SELECT *
        FROM dm_messages
        WHERE conversation_id=?
        ORDER BY message_order ASC NULLS LAST, sent_at ASC NULLS LAST, id ASC
        """,
        (conversation_row_id,),
    ).fetchall()


def get_dm_media_records(conn: sqlite3.Connection, message_row_id: int) -> list[sqlite3.Row]:
    """Return saved DM attachments for one message."""
    return conn.execute(
        """
        SELECT *
        FROM dm_media_files
        WHERE message_id=?
        ORDER BY attachment_order ASC, id ASC
        """,
        (message_row_id,),
    ).fetchall()


def get_dm_conversation_row(
    conn: sqlite3.Connection,
    platform: str,
    account_username: str,
    conversation_id: str,
) -> sqlite3.Row | None:
    """Return one DM conversation row joined with the owning account."""
    return conn.execute(
        """
        SELECT
            dc.*,
            a.username AS account_username,
            a.platform AS account_platform,
            a.id AS account_row_id
        FROM dm_conversations dc
        JOIN accounts a ON a.id = dc.account_id
        WHERE dc.platform=? AND a.username=? AND dc.external_thread_id=?
        LIMIT 1
        """,
        (platform, account_username, conversation_id),
    ).fetchone()


def get_latest_inbound_dm_message(
    conn: sqlite3.Connection,
    conversation_row_id: int,
) -> sqlite3.Row | None:
    """Return the newest inbound DM message for one conversation."""
    return conn.execute(
        """
        SELECT *
        FROM dm_messages
        WHERE conversation_id=? AND is_self=0
        ORDER BY sent_at DESC NULLS LAST, id DESC
        LIMIT 1
        """,
        (conversation_row_id,),
    ).fetchone()


def get_recent_self_dm_messages(
    conn: sqlite3.Connection,
    *,
    platform: str,
    account_username: str,
    target_username: str | None = None,
    limit: int = 50,
) -> list[sqlite3.Row]:
    """Return recent sent messages for style retrieval, optionally focused on one target."""
    params: list[object] = [platform, account_username]
    target_sql = ""
    if target_username:
        target_sql = """
            AND EXISTS (
                SELECT 1
                FROM dm_participants dp
                WHERE dp.conversation_id=dc.id
                  AND lower(COALESCE(dp.username, ''))=lower(?)
            )
        """
        params.append(target_username)
    params.append(limit)
    return conn.execute(
        f"""
        SELECT
            dm.*,
            dc.external_thread_id AS conversation_id,
            dc.title AS conversation_title
        FROM dm_messages dm
        JOIN dm_conversations dc ON dc.id = dm.conversation_id
        JOIN accounts a ON a.id = dc.account_id
        WHERE dm.platform=?
          AND a.username=?
          AND dm.is_self=1
          {target_sql}
        ORDER BY dm.sent_at DESC NULLS LAST, dm.id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()


# ─── Follower snapshots & events ─────────────────────────────────────────────

def save_follower_snapshot(conn: sqlite3.Connection, account_id: int,
                           snapshot_type: str, users: list[str | dict]) -> None:
    """Bulk-insert a fresh follower/following snapshot."""
    now = _now()
    normalised = _normalise_snapshot_entries(users)
    rows = [
        (
            account_id,
            snapshot_type,
            entry["username"],
            entry.get("display_name"),
            entry.get("user_id"),
            entry.get("profile_pic_url"),
            1 if entry.get("is_verified") else 0,
            1 if entry.get("is_private") else 0,
            now,
        )
        for entry in normalised
    ] or [(account_id, snapshot_type, "", None, None, None, 0, 0, now)]
    conn.executemany("""
        INSERT INTO follower_snapshots (
            account_id, snapshot_type, username, display_name, user_id,
            profile_pic_url, is_verified, is_private, snapshot_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)
    conn.commit()


def diff_follower_snapshots(conn: sqlite3.Connection, account_id: int,
                             snapshot_type: str) -> list[dict]:
    """
    Compare the two most-recent snapshots for this account + type.
    Return a list of event dicts: {event_type, target_username}.
    snapshot_type 'followers' → event_types gained_follower / lost_follower
    snapshot_type 'following' → event_types started_following / stopped_following
    """
    rows = conn.execute("""
        SELECT DISTINCT snapshot_at
        FROM follower_snapshots
        WHERE account_id=? AND snapshot_type=?
        ORDER BY snapshot_at DESC
        LIMIT 2
    """, (account_id, snapshot_type)).fetchall()

    if len(rows) < 2:
        return []

    latest_at, previous_at = rows[0]["snapshot_at"], rows[1]["snapshot_at"]

    def get_map(at: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for row in conn.execute("""
                SELECT username, user_id FROM follower_snapshots
                WHERE account_id=? AND snapshot_type=? AND snapshot_at=?
            """, (account_id, snapshot_type, at)).fetchall():
            identity = _snapshot_identity(row["username"], row["user_id"])
            if identity:
                result[identity] = row["username"]
        return result

    latest = get_map(latest_at)
    previous = get_map(previous_at)

    gained = latest.keys() - previous.keys()
    lost = previous.keys() - latest.keys()

    if snapshot_type == "followers":
        gained_type, lost_type = "gained_follower", "lost_follower"
    else:
        gained_type, lost_type = "started_following", "stopped_following"

    events = []
    for identity in sorted(gained, key=str.casefold):
        username = latest.get(identity)
        if username:
            events.append({"event_type": gained_type, "target_username": username})
    for identity in sorted(lost, key=str.casefold):
        username = previous.get(identity)
        if username:
            events.append({"event_type": lost_type, "target_username": username})
    return events


def insert_follower_events(conn: sqlite3.Connection, account_id: int,
                           events: list[dict]) -> None:
    """Bulk-insert derived follower_events rows."""
    now = _now()
    conn.executemany("""
        INSERT INTO follower_events (account_id, event_type, target_username, detected_at)
        VALUES (?, ?, ?, ?)
    """, [(account_id, e["event_type"], e["target_username"], now) for e in events])
    conn.commit()


def get_latest_follower_snapshot(
    conn: sqlite3.Connection,
    account_id: int,
    snapshot_type: str,
) -> dict:
    """Return the latest stored follower/following snapshot for an own account."""
    row = conn.execute("""
        SELECT snapshot_at
        FROM follower_snapshots
        WHERE account_id=? AND snapshot_type=?
        ORDER BY snapshot_at DESC
        LIMIT 1
    """, (account_id, snapshot_type)).fetchone()

    if not row:
        return {
            "snapshot_at": None,
            "count": 0,
            "users": [],
        }

    users = [
        {
            "username": result["username"],
            "display_name": result["display_name"],
            "user_id": result["user_id"],
            "profile_pic_url": result["profile_pic_url"],
            "is_verified": bool(result["is_verified"]),
            "is_private": bool(result["is_private"]),
        }
        for result in conn.execute("""
            SELECT username, display_name, user_id, profile_pic_url, is_verified, is_private
            FROM follower_snapshots
            WHERE account_id=? AND snapshot_type=? AND snapshot_at=? AND username <> ''
            ORDER BY username COLLATE NOCASE ASC
        """, (account_id, snapshot_type, row["snapshot_at"])).fetchall()
    ]
    return {
        "snapshot_at": row["snapshot_at"],
        "count": len(users),
        "users": users,
    }


def save_target_follow_snapshot(conn: sqlite3.Connection, target_id: int,
                                snapshot_type: str, users: list[str | dict]) -> None:
    """Bulk-insert a fresh follower/following snapshot for a monitored target."""
    now = _now()
    normalised = _normalise_snapshot_entries(users)
    rows = [
        (
            target_id,
            snapshot_type,
            entry["username"],
            entry.get("display_name"),
            entry.get("user_id"),
            entry.get("profile_pic_url"),
            1 if entry.get("is_verified") else 0,
            1 if entry.get("is_private") else 0,
            now,
        )
        for entry in normalised
    ] or [(target_id, snapshot_type, "", None, None, None, 0, 0, now)]
    conn.executemany("""
        INSERT INTO target_follow_snapshots (
            target_id, snapshot_type, username, display_name, user_id,
            profile_pic_url, is_verified, is_private, snapshot_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)
    conn.commit()


def diff_target_follow_snapshots(conn: sqlite3.Connection, target_id: int,
                                 snapshot_type: str) -> list[dict]:
    """Compare the two most-recent snapshots for this monitored target + type."""
    rows = conn.execute("""
        SELECT DISTINCT snapshot_at
        FROM target_follow_snapshots
        WHERE target_id=? AND snapshot_type=?
        ORDER BY snapshot_at DESC
        LIMIT 2
    """, (target_id, snapshot_type)).fetchall()

    if len(rows) < 2:
        return []

    latest_at, previous_at = rows[0]["snapshot_at"], rows[1]["snapshot_at"]

    def get_map(at: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for row in conn.execute("""
                SELECT username, user_id FROM target_follow_snapshots
                WHERE target_id=? AND snapshot_type=? AND snapshot_at=?
            """, (target_id, snapshot_type, at)).fetchall():
            identity = _snapshot_identity(row["username"], row["user_id"])
            if identity:
                result[identity] = row["username"]
        return result

    latest = get_map(latest_at)
    previous = get_map(previous_at)

    gained = latest.keys() - previous.keys()
    lost = previous.keys() - latest.keys()

    if snapshot_type == "followers":
        gained_type, lost_type = "gained_follower", "lost_follower"
    else:
        gained_type, lost_type = "started_following", "stopped_following"

    events = []
    for identity in sorted(gained, key=str.casefold):
        username = latest.get(identity)
        if username:
            events.append({"event_type": gained_type, "target_username": username})
    for identity in sorted(lost, key=str.casefold):
        username = previous.get(identity)
        if username:
            events.append({"event_type": lost_type, "target_username": username})
    return events


def insert_target_follow_events(conn: sqlite3.Connection, target_id: int,
                                events: list[dict]) -> None:
    """Bulk-insert derived target_follow_events rows."""
    now = _now()
    conn.executemany("""
        INSERT INTO target_follow_events (target_id, event_type, target_username, detected_at)
        VALUES (?, ?, ?, ?)
    """, [(target_id, event["event_type"], event["target_username"], now) for event in events])
    conn.commit()


# ─── Notification queue ──────────────────────────────────────────────────────

def get_unnotified_posts(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT p.*, t.username AS target_username
        FROM posts p JOIN targets t ON t.id = p.target_id
        WHERE p.notified=0
        ORDER BY p.discovered_at ASC
        LIMIT ?
    """, (limit,)).fetchall()


def get_unnotified_stories(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT s.*, t.username AS target_username
        FROM stories s JOIN targets t ON t.id = s.target_id
        WHERE s.notified=0
        ORDER BY s.discovered_at ASC
        LIMIT ?
    """, (limit,)).fetchall()


def get_unnotified_follower_events(conn: sqlite3.Connection,
                                   limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT fe.*, a.username AS account_username, a.username AS subject_username, a.platform,
               'account' AS subject_kind
        FROM follower_events fe JOIN accounts a ON a.id = fe.account_id
        WHERE fe.notified=0
        ORDER BY fe.detected_at ASC
        LIMIT ?
    """, (limit,)).fetchall()


def get_latest_target_follow_snapshot(
    conn: sqlite3.Connection,
    target_id: int,
    snapshot_type: str,
) -> dict:
    """Return the latest stored follower/following snapshot for a monitored target."""
    row = conn.execute("""
        SELECT snapshot_at
        FROM target_follow_snapshots
        WHERE target_id=? AND snapshot_type=?
        ORDER BY snapshot_at DESC
        LIMIT 1
    """, (target_id, snapshot_type)).fetchone()

    if not row:
        return {
            "snapshot_at": None,
            "count": 0,
            "users": [],
        }

    users = [
        {
            "username": result["username"],
            "display_name": result["display_name"],
            "user_id": result["user_id"],
            "profile_pic_url": result["profile_pic_url"],
            "is_verified": bool(result["is_verified"]),
            "is_private": bool(result["is_private"]),
        }
        for result in conn.execute("""
            SELECT username, display_name, user_id, profile_pic_url, is_verified, is_private
            FROM target_follow_snapshots
            WHERE target_id=? AND snapshot_type=? AND snapshot_at=? AND username <> ''
            ORDER BY username COLLATE NOCASE ASC
        """, (target_id, snapshot_type, row["snapshot_at"])).fetchall()
    ]
    return {
        "snapshot_at": row["snapshot_at"],
        "count": len(users),
        "users": users,
    }


def _build_follow_snapshot_history(rows: list[sqlite3.Row]) -> dict:
    """Convert raw snapshot rows into snapshot timeline plus all-time user history."""
    snapshots_by_at: dict[str, dict] = {}
    snapshot_identities: dict[str, set[str]] = {}
    all_time_users: dict[str, dict] = {}

    for row in rows:
        snapshot_at = row["snapshot_at"]
        snapshot = snapshots_by_at.setdefault(
            snapshot_at,
            {
                "snapshot_at": snapshot_at,
                "count": 0,
                "usernames": [],
                "users": [],
            },
        )
        username = str(row["username"] or "").strip()
        if not username:
            continue

        identity = _snapshot_identity(username, row["user_id"])
        if not identity:
            continue
        snapshot_identities.setdefault(snapshot_at, set()).add(identity)

        user_payload = {
            "username": username,
            "display_name": row["display_name"],
            "user_id": row["user_id"],
            "profile_pic_url": row["profile_pic_url"],
            "is_verified": bool(row["is_verified"]),
            "is_private": bool(row["is_private"]),
        }
        snapshot["users"].append(user_payload)
        snapshot["usernames"].append(username)

        history_entry = all_time_users.setdefault(
            identity,
            {
                **user_payload,
                "first_seen_at": snapshot_at,
                "last_seen_at": snapshot_at,
                "times_seen": 0,
                "currently_present": False,
            },
        )
        history_entry["times_seen"] += 1
        history_entry["last_seen_at"] = snapshot_at
        for field in ("display_name", "user_id", "profile_pic_url"):
            if user_payload.get(field):
                history_entry[field] = user_payload[field]
        history_entry["is_verified"] = bool(user_payload["is_verified"] or history_entry.get("is_verified"))
        history_entry["is_private"] = bool(user_payload["is_private"] or history_entry.get("is_private"))

    snapshots = [
        snapshots_by_at[snapshot_at]
        for snapshot_at in sorted(snapshots_by_at)
    ]
    for snapshot in snapshots:
        snapshot["count"] = len(snapshot["users"])
    latest_identities = snapshot_identities.get(snapshots[-1]["snapshot_at"], set()) if snapshots else set()
    for entry in all_time_users.values():
        identity = _snapshot_identity(entry.get("username"), entry.get("user_id"))
        entry["currently_present"] = bool(identity in latest_identities) if identity else False

    users = sorted(
        all_time_users.values(),
        key=lambda item: (
            str(item.get("username") or "").casefold(),
            str(item.get("user_id") or ""),
        ),
    )
    return {
        "snapshots": snapshots,
        "users": users,
    }


def get_follower_snapshot_history(
    conn: sqlite3.Connection,
    account_id: int,
    snapshot_type: str,
) -> dict:
    """Return full follower/following snapshot history for an own account."""
    rows = conn.execute(
        """
        SELECT snapshot_at, username, display_name, user_id, profile_pic_url, is_verified, is_private
        FROM follower_snapshots
        WHERE account_id=? AND snapshot_type=?
        ORDER BY snapshot_at ASC, username COLLATE NOCASE ASC, id ASC
        """,
        (account_id, snapshot_type),
    ).fetchall()
    return _build_follow_snapshot_history(rows)


def get_target_follow_snapshot_history(
    conn: sqlite3.Connection,
    target_id: int,
    snapshot_type: str,
) -> dict:
    """Return full follower/following snapshot history for a monitored target."""
    rows = conn.execute(
        """
        SELECT snapshot_at, username, display_name, user_id, profile_pic_url, is_verified, is_private
        FROM target_follow_snapshots
        WHERE target_id=? AND snapshot_type=?
        ORDER BY snapshot_at ASC, username COLLATE NOCASE ASC, id ASC
        """,
        (target_id, snapshot_type),
    ).fetchall()
    return _build_follow_snapshot_history(rows)


def get_unnotified_target_follow_events(conn: sqlite3.Connection,
                                        limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT
            tfe.*,
            t.username AS tracked_username,
            t.username AS subject_username,
            t.platform,
            'target' AS subject_kind
        FROM target_follow_events tfe
        JOIN targets t ON t.id = tfe.target_id
        WHERE tfe.notified=0
        ORDER BY tfe.detected_at ASC
        LIMIT ?
    """, (limit,)).fetchall()


def mark_notified(conn: sqlite3.Connection, table: str, ids: list[int]) -> None:
    """Set notified=1 on given row ids in named table."""
    if not ids:
        return
    placeholders = ", ".join("?" for _ in ids)
    conn.execute(f"UPDATE {table} SET notified=1 WHERE id IN ({placeholders})", ids)
    conn.commit()


def mark_unnotified(conn: sqlite3.Connection, table: str, ids: list[int]) -> None:
    """Set notified=0 on given row ids in named table."""
    if not ids:
        return
    placeholders = ", ".join("?" for _ in ids)
    conn.execute(f"UPDATE {table} SET notified=0 WHERE id IN ({placeholders})", ids)
    conn.commit()


# ─── Proxies ─────────────────────────────────────────────────────────────────

def upsert_proxy(conn: sqlite3.Connection, url: str) -> int:
    """Insert proxy if not exists. Return proxies.id."""
    conn.execute(
        "INSERT OR IGNORE INTO proxies (url) VALUES (?)", (url,)
    )
    conn.commit()
    return conn.execute("SELECT id FROM proxies WHERE url=?", (url,)).fetchone()["id"]


def get_active_proxies(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM proxies WHERE is_active=1 ORDER BY last_used_at ASC NULLS FIRST"
    ).fetchall()


def update_proxy_failure(conn: sqlite3.Connection, url: str) -> int:
    """Increment failure count. Return new count."""
    conn.execute(
        "UPDATE proxies SET failure_count = failure_count + 1 WHERE url=?", (url,)
    )
    conn.commit()
    return conn.execute(
        "SELECT failure_count FROM proxies WHERE url=?", (url,)
    ).fetchone()["failure_count"]


def disable_proxy(conn: sqlite3.Connection, url: str) -> None:
    conn.execute("UPDATE proxies SET is_active=0 WHERE url=?", (url,))
    conn.commit()


def reset_proxy(conn: sqlite3.Connection, url: str) -> None:
    """Mark proxy healthy after a successful request."""
    conn.execute(
        "UPDATE proxies SET failure_count=0, last_used_at=?, is_active=1 WHERE url=?",
        (_now(), url),
    )
    conn.commit()


# ─── Scrape log ──────────────────────────────────────────────────────────────

def log_scrape(conn: sqlite3.Connection, job_type: str, status: str,
               started_at: str, **kwargs) -> None:
    """Insert an audit row for a completed scrape job."""
    allowed = {"platform", "account_id", "target_id", "detail"}
    filtered = {k: v for k, v in kwargs.items() if k in allowed}
    cols = ["job_type", "status", "started_at"] + list(filtered.keys())
    vals = [job_type, status, started_at] + list(filtered.values())
    conn.execute(
        f"INSERT INTO scrape_log ({', '.join(cols)}) VALUES ({', '.join('?' for _ in vals)})",
        vals,
    )
    conn.commit()


def has_successful_scrape(conn: sqlite3.Connection, job_type: str,
                          *, target_id: int | None = None,
                          account_id: int | None = None,
                          platform: str | None = None) -> bool:
    """Return True if a matching scrape_log row exists with status='success'."""
    clauses = ["job_type=?", "status='success'"]
    params: list[object] = [job_type]
    if target_id is not None:
        clauses.append("target_id=?")
        params.append(target_id)
    if account_id is not None:
        clauses.append("account_id=?")
        params.append(account_id)
    if platform is not None:
        clauses.append("platform=?")
        params.append(platform)

    row = conn.execute(
        f"SELECT 1 FROM scrape_log WHERE {' AND '.join(clauses)} LIMIT 1",
        params,
    ).fetchone()
    return row is not None


# ─── Daily digest ────────────────────────────────────────────────────────────

def get_daily_digest_data(conn: sqlite3.Connection) -> dict:
    """Aggregate counts for the past 24 hours for the daily digest message."""
    since = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    new_posts = conn.execute(
        "SELECT COUNT(*) AS cnt FROM posts WHERE discovered_at >= ?", (since,)
    ).fetchone()["cnt"]

    new_stories = conn.execute(
        "SELECT COUNT(*) AS cnt FROM stories WHERE discovered_at >= ?", (since,)
    ).fetchone()["cnt"]

    follower_events = conn.execute("""
        SELECT event_type, COUNT(*) AS cnt
        FROM (
            SELECT event_type, detected_at FROM follower_events
            UNION ALL
            SELECT event_type, detected_at FROM target_follow_events
        )
        WHERE detected_at >= ?
        GROUP BY event_type
    """, (since,)).fetchall()

    event_counts = {r["event_type"]: r["cnt"] for r in follower_events}

    new_avatar_changes = conn.execute(
        "SELECT COUNT(*) AS cnt FROM target_avatar_history WHERE detected_at >= ?", (since,)
    ).fetchone()["cnt"]

    new_profile_changes = conn.execute(
        "SELECT COUNT(*) AS cnt FROM target_profile_history WHERE detected_at >= ?", (since,)
    ).fetchone()["cnt"]

    new_vibe_shifts = conn.execute(
        "SELECT COUNT(*) AS cnt FROM vibe_shift_alerts WHERE detected_at >= ?", (since,)
    ).fetchone()["cnt"]

    top_targets_rows = conn.execute("""
        SELECT t.username, t.platform, COUNT(p.id) AS post_count
        FROM posts p
        JOIN targets t ON t.id = p.target_id
        WHERE p.discovered_at >= ?
        GROUP BY t.id
        ORDER BY post_count DESC
        LIMIT 3
    """, (since,)).fetchall()
    top_targets = [
        {"username": r["username"], "platform": r["platform"], "post_count": r["post_count"]}
        for r in top_targets_rows
    ]

    return {
        "since": since,
        "new_posts": new_posts,
        "new_stories": new_stories,
        "gained_followers": event_counts.get("gained_follower", 0),
        "lost_followers": event_counts.get("lost_follower", 0),
        "started_following": event_counts.get("started_following", 0),
        "stopped_following": event_counts.get("stopped_following", 0),
        "avatar_changes": new_avatar_changes,
        "profile_changes": new_profile_changes,
        "vibe_shifts": new_vibe_shifts,
        "top_targets": top_targets,
    }


# ─── Pause / resume targets ───────────────────────────────────────────────────

def set_target_paused(
    conn: sqlite3.Connection,
    username: str,
    is_paused: bool,
    platform: str | None = None,
) -> bool:
    """Set is_paused for a target by username, optionally scoped to one platform."""
    if platform:
        cur = conn.execute(
            "UPDATE targets SET is_paused=? WHERE platform=? AND username=?",
            (1 if is_paused else 0, platform, username),
        )
    else:
        cur = conn.execute(
            "UPDATE targets SET is_paused=? WHERE username=?",
            (1 if is_paused else 0, username),
        )
    conn.commit()
    return cur.rowcount > 0


def get_target_is_paused(conn: sqlite3.Connection, target_id: int) -> bool:
    row = conn.execute(
        "SELECT is_paused FROM targets WHERE id=?", (target_id,)
    ).fetchone()
    return bool(row["is_paused"]) if row else False


# ─── Status helpers ───────────────────────────────────────────────────────────

def get_all_targets_with_stats(conn: sqlite3.Connection) -> list[dict]:
    """Return all targets with post count, story count, last scrape time, and pause state."""
    rows = conn.execute("""
        SELECT
            t.id, t.platform, t.username, t.is_paused, t.is_active,
            t.follower_count, t.following_count,
            (SELECT COUNT(*) FROM posts p WHERE p.target_id=t.id) AS post_count,
            (SELECT COUNT(*) FROM stories s WHERE s.target_id=t.id) AS story_count,
            (SELECT MAX(sl.finished_at) FROM scrape_log sl WHERE sl.target_id=t.id) AS last_scraped_at
        FROM targets t
        ORDER BY t.platform, t.username
    """).fetchall()
    return [dict(r) for r in rows]


# ─── Cross-platform link helpers ─────────────────────────────────────────────

def upsert_cross_platform_link(
    conn: sqlite3.Connection,
    *,
    source_platform: str,
    source_username: str,
    target_platform: str,
    target_username: str,
    status: str = "pending",
    confidence: float | None = None,
    reasons: list | None = None,
    evidence: dict | None = None,
    face_score: float | None = None,
) -> int:
    """Insert or update a cross-platform link record. Return its id."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        """
        INSERT INTO cross_platform_links
            (source_platform, source_username, target_platform, target_username,
             status, confidence, reasons, evidence, face_score, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_platform, source_username, target_platform, target_username)
        DO UPDATE SET
            status     = excluded.status,
            confidence = COALESCE(excluded.confidence, confidence),
            reasons    = COALESCE(excluded.reasons, reasons),
            evidence   = COALESCE(excluded.evidence, evidence),
            face_score = COALESCE(excluded.face_score, face_score),
            updated_at = excluded.updated_at
        """,
        (
            source_platform, source_username, target_platform, target_username,
            status,
            confidence,
            json.dumps(reasons) if reasons is not None else None,
            json.dumps(evidence) if evidence is not None else None,
            face_score,
            now, now,
        ),
    )
    conn.commit()
    row = conn.execute(
        """SELECT id FROM cross_platform_links
           WHERE source_platform=? AND source_username=?
             AND target_platform=? AND target_username=?""",
        (source_platform, source_username, target_platform, target_username),
    ).fetchone()
    return row["id"]


def get_cross_platform_links(
    conn: sqlite3.Connection,
    platform: str,
    username: str,
) -> list[dict]:
    """Return all non-rejected links for a profile (approved + pending)."""
    rows = conn.execute(
        """
        SELECT * FROM cross_platform_links
        WHERE source_platform=? AND source_username=?
          AND status != 'rejected'
        ORDER BY status DESC, confidence DESC
        """,
        (platform, username),
    ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["reasons"] = json.loads(d["reasons"]) if d.get("reasons") else []
        d["evidence"] = json.loads(d["evidence"]) if d.get("evidence") else {}
        result.append(d)
    return result


def get_approved_cross_platform_links(
    conn: sqlite3.Connection,
    platform: str,
    username: str,
) -> list[dict]:
    """Return only approved links for a profile."""
    rows = conn.execute(
        """
        SELECT * FROM cross_platform_links
        WHERE source_platform=? AND source_username=? AND status='approved'
        ORDER BY target_platform, target_username
        """,
        (platform, username),
    ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["reasons"] = json.loads(d["reasons"]) if d.get("reasons") else []
        d["evidence"] = json.loads(d["evidence"]) if d.get("evidence") else {}
        result.append(d)
    return result


def get_rejected_cross_platform_link_keys(
    conn: sqlite3.Connection,
    platform: str,
    username: str,
) -> set[tuple[str, str]]:
    """Return set of (target_platform, target_username) for rejected links."""
    rows = conn.execute(
        """
        SELECT target_platform, target_username FROM cross_platform_links
        WHERE source_platform=? AND source_username=? AND status='rejected'
        """,
        (platform, username),
    ).fetchall()
    return {(row["target_platform"], row["target_username"]) for row in rows}


def approve_cross_platform_link(
    conn: sqlite3.Connection,
    source_platform: str,
    source_username: str,
    target_platform: str,
    target_username: str,
    confidence: float | None = None,
    reasons: list | None = None,
    evidence: dict | None = None,
) -> int:
    """Approve a cross-platform link (creates if absent). Return its id."""
    return upsert_cross_platform_link(
        conn,
        source_platform=source_platform,
        source_username=source_username,
        target_platform=target_platform,
        target_username=target_username,
        status="approved",
        confidence=confidence,
        reasons=reasons,
        evidence=evidence,
    )


def reject_cross_platform_link(
    conn: sqlite3.Connection,
    source_platform: str,
    source_username: str,
    target_platform: str,
    target_username: str,
) -> int:
    """Reject a cross-platform link (creates if absent). Return its id."""
    return upsert_cross_platform_link(
        conn,
        source_platform=source_platform,
        source_username=source_username,
        target_platform=target_platform,
        target_username=target_username,
        status="rejected",
    )


def get_db_stats(conn: sqlite3.Connection) -> dict:
    """Return row counts for every table — used by /status command."""
    tables = [
        "accounts", "targets", "posts", "stories", "repost_history",
        "follower_snapshots", "follower_events",
        "target_follow_snapshots", "target_follow_events",
        "media_files", "post_comments", "scrape_log",
        "target_profile_history", "target_avatar_history", "vibe_shift_alerts",
        "message_drafts", "message_draft_events",
    ]
    result = {}
    for table in tables:
        try:
            cnt = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
            result[table] = cnt
        except Exception:
            result[table] = -1
    return result


# ─── Profile history helpers ──────────────────────────────────────────────────

def get_latest_profile_field(conn: sqlite3.Connection, target_id: int, field: str) -> str | None:
    """Return the most recently recorded value for a profile field, or None."""
    row = conn.execute(
        """
        SELECT new_value FROM target_profile_history
        WHERE target_id=? AND field=?
        ORDER BY detected_at DESC LIMIT 1
        """,
        (target_id, field),
    ).fetchone()
    return row["new_value"] if row else None


def insert_profile_change(
    conn: sqlite3.Connection,
    target_id: int,
    field: str,
    old_value: str | None,
    new_value: str,
    detected_at: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO target_profile_history (target_id, field, old_value, new_value, detected_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (target_id, field, old_value, new_value, detected_at),
    )
    conn.commit()
    return cur.lastrowid


def get_unnotified_profile_changes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT tph.*, t.username AS target_username, t.platform
        FROM target_profile_history tph
        JOIN targets t ON t.id = tph.target_id
        WHERE tph.notified = 0
        ORDER BY tph.detected_at
    """).fetchall()


def mark_profile_change_notified(conn: sqlite3.Connection, change_id: int) -> None:
    conn.execute("UPDATE target_profile_history SET notified=1 WHERE id=?", (change_id,))
    conn.commit()


# ─── Avatar history helpers ───────────────────────────────────────────────────

def get_latest_avatar_sha256(
    conn: sqlite3.Connection,
    target_id: int,
    *,
    asset_kind: str = "avatar",
) -> str | None:
    row = conn.execute(
        """
        SELECT sha256 FROM target_avatar_history
        WHERE target_id=? AND asset_kind=? ORDER BY detected_at DESC LIMIT 1
        """,
        (target_id, asset_kind),
    ).fetchone()
    return row["sha256"] if row else None


def insert_avatar_record(
    conn: sqlite3.Connection,
    target_id: int,
    file_path: str,
    sha256: str,
    detected_at: str,
    *,
    asset_kind: str = "avatar",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO target_avatar_history (target_id, asset_kind, file_path, sha256, detected_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (target_id, asset_kind, file_path, sha256, detected_at),
    )
    conn.commit()
    return cur.lastrowid


def get_unnotified_avatar_changes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT tah.*, t.username AS target_username, t.platform
        FROM target_avatar_history tah
        JOIN targets t ON t.id = tah.target_id
        WHERE tah.notified = 0
        ORDER BY tah.detected_at
    """).fetchall()


def mark_avatar_change_notified(conn: sqlite3.Connection, change_id: int) -> None:
    conn.execute("UPDATE target_avatar_history SET notified=1 WHERE id=?", (change_id,))
    conn.commit()


# ─── Repost AI analysis helpers ───────────────────────────────────────────────

def update_repost_ai_analysis(
    conn: sqlite3.Connection, repost_history_id: int, analysis_json: str
) -> None:
    conn.execute(
        "UPDATE repost_history SET ai_analysis_json=? WHERE id=?",
        (analysis_json, repost_history_id),
    )
    conn.commit()


def get_repost_mood_history(
    conn: sqlite3.Connection, target_id: int, limit: int = 14
) -> list[dict]:
    """Return the most recent AI analyses for a target that contain a mood_score."""
    rows = conn.execute(
        """
        SELECT id, ai_analysis_json, reposted_at, discovered_at
        FROM repost_history
        WHERE target_id=? AND ai_analysis_json IS NOT NULL
        ORDER BY COALESCE(reposted_at, discovered_at) DESC
        LIMIT ?
        """,
        (target_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ─── Vibe shift alert helpers ─────────────────────────────────────────────────

def insert_vibe_shift_alert(
    conn: sqlite3.Connection,
    target_id: int,
    delta: float,
    direction: str,
    description: str | None,
    detected_at: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO vibe_shift_alerts (target_id, delta, direction, description, detected_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (target_id, delta, direction, description, detected_at),
    )
    conn.commit()
    return cur.lastrowid


def get_unnotified_vibe_shifts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT vsa.*, t.username AS target_username, t.platform
        FROM vibe_shift_alerts vsa
        JOIN targets t ON t.id = vsa.target_id
        WHERE vsa.notified = 0
        ORDER BY vsa.detected_at
    """).fetchall()


def mark_vibe_shift_notified(conn: sqlite3.Connection, alert_id: int) -> None:
    conn.execute("UPDATE vibe_shift_alerts SET notified=1 WHERE id=?", (alert_id,))
    conn.commit()


# ─── Social proximity helpers ─────────────────────────────────────────────────

def update_target_proximity(
    conn: sqlite3.Connection, target_id: int, proximity_json: str
) -> None:
    conn.execute(
        "UPDATE targets SET proximity_json=? WHERE id=?",
        (proximity_json, target_id),
    )
    conn.commit()


def get_proximity_scores(conn: sqlite3.Connection, target_id: int) -> dict:
    row = conn.execute(
        "SELECT proximity_json FROM targets WHERE id=?", (target_id,)
    ).fetchone()
    if row and row["proximity_json"]:
        try:
            return json.loads(row["proximity_json"])
        except Exception:
            return {}
    return {}


# ─── Digital twin draft helpers ──────────────────────────────────────────────

def create_message_draft(
    conn: sqlite3.Connection,
    *,
    platform: str,
    account_id: int | None,
    account_username: str,
    target_id: int | None,
    target_username: str,
    conversation_row_id: int | None,
    conversation_id: str | None,
    source_message_id: str | None,
    source_story_id: str | None,
    trigger_type: str,
    trigger_payload: dict | None,
    route: str,
    route_reason: str | None,
    prompt_version: str | None,
    gemini_context: dict | None,
    intent_label: str | None,
    route_hint: str | None,
    style_notes: str | None,
    risk_flags: list[str] | dict | None,
    context_sources: list[dict] | None,
    draft_text: str,
    review_status: str = "pending_review",
    send_status: str = "not_sent",
) -> int:
    """Insert one digital-twin draft row and return its id."""
    now = _now()
    cursor = conn.execute(
        """
        INSERT INTO message_drafts (
            platform, account_id, account_username, target_id, target_username,
            conversation_row_id, conversation_id, source_message_id, source_story_id,
            trigger_type, trigger_payload_json, route, route_reason, prompt_version,
            gemini_context_json, intent_label, route_hint, style_notes,
            risk_flags_json, context_sources_json, draft_text,
            review_status, send_status, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            platform,
            account_id,
            account_username,
            target_id,
            target_username,
            conversation_row_id,
            conversation_id,
            source_message_id,
            source_story_id,
            trigger_type,
            _json_dumps(trigger_payload),
            route,
            route_reason,
            prompt_version,
            _json_dumps(gemini_context),
            intent_label,
            route_hint,
            style_notes,
            _json_dumps(risk_flags),
            _json_dumps(context_sources),
            draft_text,
            review_status,
            send_status,
            now,
            now,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def append_message_draft_event(
    conn: sqlite3.Connection,
    draft_id: int,
    event_type: str,
    *,
    actor_user_id: str | None = None,
    actor_username: str | None = None,
    payload: dict | None = None,
) -> int:
    """Append an immutable audit event for one draft."""
    cursor = conn.execute(
        """
        INSERT INTO message_draft_events (
            draft_id, event_type, actor_user_id, actor_username, payload_json
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            draft_id,
            event_type,
            actor_user_id,
            actor_username,
            _json_dumps(payload),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def get_message_draft(conn: sqlite3.Connection, draft_id: int) -> sqlite3.Row | None:
    """Return one draft row joined with lightweight account/target metadata."""
    return conn.execute(
        """
        SELECT
            md.*,
            a.platform AS account_platform,
            t.platform AS target_platform
        FROM message_drafts md
        LEFT JOIN accounts a ON a.id = md.account_id
        LEFT JOIN targets t ON t.id = md.target_id
        WHERE md.id=?
        LIMIT 1
        """,
        (draft_id,),
    ).fetchone()


def get_message_draft_events(conn: sqlite3.Connection, draft_id: int) -> list[sqlite3.Row]:
    """Return the audit trail for one draft."""
    return conn.execute(
        """
        SELECT *
        FROM message_draft_events
        WHERE draft_id=?
        ORDER BY created_at ASC, id ASC
        """,
        (draft_id,),
    ).fetchall()


def get_pending_message_drafts(
    conn: sqlite3.Connection,
    *,
    limit: int = 20,
    statuses: tuple[str, ...] = ("pending_review", "blocked"),
) -> list[sqlite3.Row]:
    """Return the newest reviewable or blocked drafts."""
    placeholders = ",".join("?" for _ in statuses)
    params: list[object] = [*statuses, limit]
    return conn.execute(
        f"""
        SELECT *
        FROM message_drafts
        WHERE review_status IN ({placeholders})
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()


def get_editing_message_draft_for_reviewer(
    conn: sqlite3.Connection,
    *,
    actor_user_id: str,
    chat_id: str | None = None,
    thread_id: int | None = None,
) -> sqlite3.Row | None:
    """Return the newest draft currently waiting for edited text from one reviewer."""
    params: list[object] = [actor_user_id]
    chat_filter = ""
    if chat_id is not None:
        chat_filter += " AND COALESCE(telegram_chat_id, '')=?"
        params.append(str(chat_id))
    if thread_id is not None:
        chat_filter += " AND telegram_thread_id=?"
        params.append(thread_id)
    return conn.execute(
        f"""
        SELECT *
        FROM message_drafts
        WHERE review_status='editing'
          AND editing_user_id=?
          {chat_filter}
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
        """,
        params,
    ).fetchone()


def list_active_message_drafts_for_target(
    conn: sqlite3.Connection,
    *,
    platform: str,
    target_username: str,
) -> list[sqlite3.Row]:
    """Return pending/editing/approved-but-unsent drafts for one target."""
    return conn.execute(
        """
        SELECT *
        FROM message_drafts
        WHERE platform=?
          AND lower(target_username)=lower(?)
          AND review_status IN ('pending_review', 'editing', 'approved', 'blocked')
        ORDER BY created_at DESC, id DESC
        """,
        (platform, target_username),
    ).fetchall()


def get_recent_message_draft_for_target(
    conn: sqlite3.Connection,
    *,
    platform: str,
    target_username: str,
    since_iso: str,
) -> sqlite3.Row | None:
    """Return the newest draft for a target inside a cooldown window."""
    return conn.execute(
        """
        SELECT *
        FROM message_drafts
        WHERE platform=?
          AND lower(target_username)=lower(?)
          AND created_at>=?
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (platform, target_username, since_iso),
    ).fetchone()


def find_message_draft_by_source(
    conn: sqlite3.Connection,
    *,
    platform: str,
    account_username: str,
    conversation_id: str | None,
    source_message_id: str | None,
    trigger_type: str,
) -> sqlite3.Row | None:
    """Return an existing draft created from the same source event, if any."""
    return conn.execute(
        """
        SELECT *
        FROM message_drafts
        WHERE platform=?
          AND account_username=?
          AND COALESCE(conversation_id, '')=COALESCE(?, '')
          AND COALESCE(source_message_id, '')=COALESCE(?, '')
          AND trigger_type=?
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (platform, account_username, conversation_id, source_message_id, trigger_type),
    ).fetchone()


def update_message_draft_review_message(
    conn: sqlite3.Connection,
    draft_id: int,
    *,
    telegram_chat_id: str | None,
    telegram_thread_id: int | None,
    telegram_message_id: int | None,
) -> None:
    """Persist where the draft review card lives in Telegram."""
    now = _now()
    conn.execute(
        """
        UPDATE message_drafts
        SET telegram_chat_id=?, telegram_thread_id=?, telegram_message_id=?, updated_at=?
        WHERE id=?
        """,
        (telegram_chat_id, telegram_thread_id, telegram_message_id, now, draft_id),
    )
    conn.commit()


def set_message_draft_editing(
    conn: sqlite3.Connection,
    draft_id: int,
    *,
    actor_user_id: str,
    actor_username: str | None,
) -> None:
    """Move a draft into the edit-waiting state for one reviewer."""
    now = _now()
    conn.execute(
        """
        UPDATE message_drafts
        SET review_status='editing',
            editing_user_id=?,
            editing_username=?,
            updated_at=?
        WHERE id=?
        """,
        (actor_user_id, actor_username, now, draft_id),
    )
    conn.commit()


def save_message_draft_edited_text(
    conn: sqlite3.Connection,
    draft_id: int,
    *,
    edited_text: str,
) -> None:
    """Replace the draft body with reviewer-edited text and return to pending review."""
    now = _now()
    conn.execute(
        """
        UPDATE message_drafts
        SET draft_text=?,
            approved_text=NULL,
            review_status='pending_review',
            editing_user_id=NULL,
            editing_username=NULL,
            updated_at=?
        WHERE id=?
        """,
        (edited_text, now, draft_id),
    )
    conn.commit()


def mark_message_draft_approved(
    conn: sqlite3.Connection,
    draft_id: int,
    *,
    approved_text: str,
    actor_user_id: str,
    actor_username: str | None,
) -> None:
    """Mark a draft as approved and ready for immediate send execution."""
    now = _now()
    conn.execute(
        """
        UPDATE message_drafts
        SET approved_text=?,
            review_status='approved',
            send_status='queued',
            approved_by_user_id=?,
            approved_by_username=?,
            reviewed_at=?,
            editing_user_id=NULL,
            editing_username=NULL,
            updated_at=?
        WHERE id=?
        """,
        (approved_text, actor_user_id, actor_username, now, now, draft_id),
    )
    conn.commit()


def mark_message_draft_rejected(
    conn: sqlite3.Connection,
    draft_id: int,
    *,
    actor_user_id: str,
    actor_username: str | None,
) -> None:
    """Mark a draft as rejected/dropped."""
    now = _now()
    conn.execute(
        """
        UPDATE message_drafts
        SET review_status='rejected',
            send_status='dropped',
            rejected_by_user_id=?,
            rejected_by_username=?,
            reviewed_at=?,
            editing_user_id=NULL,
            editing_username=NULL,
            updated_at=?
        WHERE id=?
        """,
        (actor_user_id, actor_username, now, now, draft_id),
    )
    conn.commit()


def mark_message_draft_sent(
    conn: sqlite3.Connection,
    draft_id: int,
    *,
    sent_at: str | None = None,
) -> None:
    """Mark a draft as successfully delivered."""
    final_sent_at = sent_at or _now()
    conn.execute(
        """
        UPDATE message_drafts
        SET review_status='sent',
            send_status='sent',
            sent_at=?,
            last_error=NULL,
            updated_at=?
        WHERE id=?
        """,
        (final_sent_at, final_sent_at, draft_id),
    )
    conn.commit()


def mark_message_draft_send_failed(
    conn: sqlite3.Connection,
    draft_id: int,
    *,
    error_text: str,
) -> None:
    """Mark a draft as approved but failed during live send execution."""
    now = _now()
    conn.execute(
        """
        UPDATE message_drafts
        SET review_status='approved',
            send_status='send_failed',
            last_error=?,
            updated_at=?
        WHERE id=?
        """,
        (error_text, now, draft_id),
    )
    conn.commit()
