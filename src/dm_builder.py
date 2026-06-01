"""
dm_builder.py — Export account-level DM archives into structured JSON bundles.

Files are written to:
    data/accounts/{platform}/{username}/account.json
    data/accounts/{platform}/{username}/dms/index.json
    data/accounts/{platform}/{username}/dms/conversations/{conversation_id}/conversation.json
    data/accounts/{platform}/{username}/dms/conversations/{conversation_id}/messages.json

All media paths remain relative to data/ so later visualizations can rehydrate the
message timeline and inline attachments in their original positions.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from src.db import (
    get_dm_conversations_for_account,
    get_dm_media_records,
    get_dm_messages,
    get_dm_participants,
)

SCHEMA_VERSION = 1


def build_dm_archive(conn: sqlite3.Connection, account_id: int, accounts_dir: Path) -> Path | None:
    """Build the DM export bundle for one owned account."""
    try:
        account_row = conn.execute(
            "SELECT id, platform, username FROM accounts WHERE id=?",
            (account_id,),
        ).fetchone()
        if not account_row:
            logger.warning(f"[dm_builder] Account id={account_id} not found")
            return None

        platform = account_row["platform"]
        username = account_row["username"]
        safe_username = re.sub(r"[^\w.-]", "_", username)
        root_dir = accounts_dir / platform / safe_username
        dms_dir = root_dir / "dms"
        conversations_dir = dms_dir / "conversations"
        conversations_dir.mkdir(parents=True, exist_ok=True)

        generated_at = _now()
        conversation_rows = get_dm_conversations_for_account(conn, account_id)
        index_items: list[dict] = []
        total_messages = 0
        total_attachments = 0

        for conversation_row in conversation_rows:
            external_thread_id = conversation_row["external_thread_id"]
            safe_thread_id = re.sub(r"[^\w.-]", "_", external_thread_id)
            conversation_dir = conversations_dir / safe_thread_id
            conversation_dir.mkdir(parents=True, exist_ok=True)

            participant_rows = get_dm_participants(conn, conversation_row["id"])
            message_rows = get_dm_messages(conn, conversation_row["id"])
            participants = [_participant_payload(row) for row in participant_rows]
            messages = []
            for message_row in message_rows:
                media_rows = get_dm_media_records(conn, message_row["id"])
                attachments = [_attachment_payload(row) for row in media_rows]
                total_attachments += len(attachments)
                messages.append(_message_payload(message_row, attachments))

            total_messages += len(messages)

            conversation_payload = {
                "schema_version": SCHEMA_VERSION,
                "generated_at": generated_at,
                "platform": platform,
                "account_username": username,
                "conversation_id": external_thread_id,
                "conversation_type": conversation_row["conversation_type"],
                "title": conversation_row["title"],
                "subtitle": conversation_row["subtitle"],
                "conversation_url": conversation_row["conversation_url"],
                "is_group": bool(conversation_row["is_group"]),
                "is_broadcast_channel": bool(conversation_row["is_broadcast_channel"]),
                "last_message_preview": conversation_row["last_message_preview"],
                "last_message_at": conversation_row["last_message_at"],
                "discovered_at": conversation_row["discovered_at"],
                "last_scraped_at": conversation_row["last_scraped_at"],
                "metadata": _json_loads(conversation_row["metadata_json"]),
                "participants": participants,
                "message_count": len(messages),
                "attachment_count": sum(len(message["attachments"]) for message in messages),
                "files": {
                    "messages": "messages.json",
                },
            }
            messages_payload = {
                "schema_version": SCHEMA_VERSION,
                "generated_at": generated_at,
                "platform": platform,
                "account_username": username,
                "conversation_id": external_thread_id,
                "items": messages,
            }

            _write_json(conversation_dir / "conversation.json", conversation_payload)
            _write_json(conversation_dir / "messages.json", messages_payload)

            index_items.append(
                {
                    "conversation_id": external_thread_id,
                    "conversation_type": conversation_row["conversation_type"],
                    "title": conversation_row["title"],
                    "subtitle": conversation_row["subtitle"],
                    "is_group": bool(conversation_row["is_group"]),
                    "is_broadcast_channel": bool(conversation_row["is_broadcast_channel"]),
                    "participant_usernames": [
                        participant["username"]
                        for participant in participants
                        if participant.get("username")
                    ],
                    "message_count": len(messages),
                    "attachment_count": sum(len(message["attachments"]) for message in messages),
                    "last_message_preview": conversation_row["last_message_preview"],
                    "last_message_at": conversation_row["last_message_at"],
                    "last_scraped_at": conversation_row["last_scraped_at"],
                    "conversation_path": f"conversations/{safe_thread_id}/conversation.json",
                    "messages_path": f"conversations/{safe_thread_id}/messages.json",
                }
            )

        account_payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated_at,
            "platform": platform,
            "username": username,
            "archive_counts": {
                "conversations": len(index_items),
                "messages": total_messages,
                "attachments": total_attachments,
            },
            "files": {
                "dms_index": "dms/index.json",
            },
        }
        index_payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated_at,
            "platform": platform,
            "account_username": username,
            "conversation_count": len(index_items),
            "message_count": total_messages,
            "attachment_count": total_attachments,
            "items": index_items,
        }

        _write_json(root_dir / "account.json", account_payload)
        _write_json(dms_dir / "index.json", index_payload)

        logger.info(f"[dm_builder] Written DM archive for {platform}/{username}")
        return dms_dir / "index.json"
    except Exception as exc:
        logger.error(f"[dm_builder] Failed building archive for account_id={account_id}: {exc}")
        return None


def _attachment_payload(row: sqlite3.Row) -> dict:
    file_path = row["file_path"]
    return {
        "attachment_id": row["attachment_id"],
        "attachment_order": row["attachment_order"],
        "file_type": row["file_type"],
        "mime_type": row["mime_type"],
        "file_path": _relative_to_data_root(file_path),
        "filename": Path(file_path).name,
        "sha256": row["sha256"],
        "file_size": row["file_size"],
        "width": row["width"],
        "height": row["height"],
        "duration_s": row["duration_s"],
        "source_url": row["source_url"],
        "preview_text": row["preview_text"],
        "downloaded_at": row["downloaded_at"],
        "metadata": _json_loads(row["metadata_json"]),
    }


def _message_payload(row: sqlite3.Row, attachments: list[dict]) -> dict:
    return {
        "message_id": row["external_message_id"] or row["message_key"],
        "message_key": row["message_key"],
        "sender": {
            "username": row["sender_username"],
            "user_id": row["sender_user_id"],
            "display_name": row["sender_display_name"],
            "is_self": bool(row["is_self"]),
        },
        "sent_at": row["sent_at"],
        "sent_at_label": row["sent_at_label"],
        "message_type": row["message_type"],
        "text": row["text"],
        "status_text": row["status_text"],
        "reply_to_message_id": row["reply_to_message_id"],
        "message_order": row["message_order"],
        "discovered_at": row["discovered_at"],
        "last_seen_at": row["last_seen_at"],
        "attachments": attachments,
        "raw_payload": _json_loads(row["raw_payload_json"]),
    }


def _participant_payload(row: sqlite3.Row) -> dict:
    return {
        "username": row["username"],
        "user_id": row["user_id"],
        "display_name": row["display_name"],
        "profile_url": row["profile_url"],
        "profile_pic_url": row["profile_pic_url"],
        "is_self": bool(row["is_self"]),
        "participant_order": row["participant_order"],
        "metadata": _json_loads(row["metadata_json"]),
    }


def _relative_to_data_root(file_path: str) -> str:
    path = Path(file_path)
    parts = path.parts
    if "media" in parts:
        return str(Path(*parts[parts.index("media"):]))
    if "accounts" in parts:
        return str(Path(*parts[parts.index("accounts"):]))
    if "data" in parts:
        return str(Path(*parts[parts.index("data") + 1:]))
    return str(path)


def _json_loads(value):
    if not value:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return value


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
