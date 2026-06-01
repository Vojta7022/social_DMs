"""Human-in-the-loop digital twin drafting and review orchestration."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from src.config import Config, InitiationRuleConfig, load_config
from src.db import (
    append_message_draft_event,
    create_message_draft,
    find_message_draft_by_source,
    get_connection,
    get_dm_conversation_row,
    get_dm_messages,
    get_dm_participants,
    get_editing_message_draft_for_reviewer,
    get_latest_inbound_dm_message,
    get_message_draft,
    get_pending_message_drafts,
    get_recent_message_draft_for_target,
    get_recent_self_dm_messages,
    get_target_row,
    list_active_message_drafts_for_target,
    mark_message_draft_approved,
    mark_message_draft_rejected,
    mark_message_draft_send_failed,
    mark_message_draft_sent,
    save_message_draft_edited_text,
    set_message_draft_editing,
    update_message_draft_review_message,
)
from src.message_gateway import execute_message_route
from src.telegram_bot import (
    build_bot,
    disable_digital_twin_controls,
    send_digital_twin_draft,
    send_digital_twin_edit_prompt,
    send_digital_twin_result,
)
from src.vector_store import (
    query_style_examples,
    query_target_profiles,
    upsert_style_example,
    upsert_target_profile_document,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROMPT_VERSION = "digital_twin_v1"
_JSON_REQUIRED_KEYS = {
    "draft_text",
    "intent_label",
    "route_hint",
    "style_notes",
    "context_sources",
    "risk_flags",
}


class DigitalTwinError(RuntimeError):
    """Raised when digital twin orchestration cannot proceed safely."""


def _env_path(project_root: Path) -> Path:
    env_local = project_root / ".env.local"
    if env_local.exists():
        return env_local
    return project_root / ".env"


def _load_runtime(project_root: Path = PROJECT_ROOT) -> tuple[Config, Any]:
    cfg = load_config(project_root / "settings.yaml", _env_path(project_root))
    conn = get_connection(cfg.db_path)
    return cfg, conn


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    try:
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _record_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return row
    return dict(row)


def _json_loads(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return None


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _target_profile_dir(cfg: Config, platform: str, username: str) -> Path:
    return cfg.profiles_dir / platform / username


def _load_json_file(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _iter_initiation_rules(cfg: Config) -> list[InitiationRuleConfig]:
    rules = list(cfg.digital_twin.appropriate_initiation_rules)
    if cfg.digital_twin.story_keyword_rules:
        rules.extend(cfg.digital_twin.story_keyword_rules)
    return rules


def _find_initiation_rule(
    cfg: Config,
    *,
    platform: str,
    target_username: str,
) -> InitiationRuleConfig | None:
    for rule in _iter_initiation_rules(cfg):
        if rule.platform and rule.platform != platform:
            continue
        if rule.username and rule.username.casefold() != target_username.casefold():
            continue
        return rule
    return None


def _story_text_blob(story_payload: dict[str, Any]) -> str:
    metadata = _json_loads(story_payload.get("metadata_json") or story_payload.get("metadata")) or {}
    parts = [
        story_payload.get("caption"),
        story_payload.get("url"),
        story_payload.get("story_id"),
        metadata.get("caption"),
        metadata.get("story_caption"),
        metadata.get("text"),
        metadata.get("story_text"),
        metadata.get("sticker_text"),
        metadata.get("link_url"),
        metadata.get("music_title"),
        metadata.get("music_artist"),
    ]
    return "\n".join(str(part).strip() for part in parts if str(part or "").strip())


def _match_story_keywords(
    cfg: Config,
    *,
    platform: str,
    target_username: str,
    story_payload: dict[str, Any],
) -> tuple[InitiationRuleConfig | None, list[str]]:
    rule = _find_initiation_rule(cfg, platform=platform, target_username=target_username)
    if not rule:
        return None, []

    blob = _story_text_blob(story_payload).casefold()
    matches = [
        keyword
        for keyword in rule.story_keywords
        if keyword and keyword.casefold() in blob
    ]
    return rule, matches


def _pick_target_from_participants(participants: list[dict[str, Any]]) -> dict[str, Any] | None:
    for participant in participants:
        if participant.get("is_self"):
            continue
        if participant.get("username") or participant.get("user_id"):
            return participant
    return None


def _within_reply_window(cfg: Config, sent_at: str | None) -> bool:
    dt = _parse_iso(sent_at)
    if dt is None:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cfg.digital_twin.reply_window_hours)
    return dt >= cutoff


def select_send_route(
    cfg: Config,
    *,
    trigger_type: str,
    latest_inbound_message: dict[str, Any] | None,
    target_username: str | None,
    recipient_id: str | None,
) -> tuple[str, str]:
    """Choose the execution route using the approved dual-routing rules."""
    trigger_key = str(trigger_type or "").strip().lower()
    latest = latest_inbound_message or {}

    if trigger_key == "inbound_reply":
        if _within_reply_window(cfg, latest.get("sent_at")):
            if not cfg.meta_graph_access_token or not cfg.meta_graph_ig_user_id:
                return "blocked", "Inbound reply is inside the 24-hour window, but Meta Graph credentials are missing."
            if not recipient_id:
                return "blocked", "Inbound reply is inside the 24-hour window, but the recipient Meta user ID is missing."
            return "meta_graph", "Inbound reply is inside the configured 24-hour Meta Graph reply window."
        if target_username:
            return "playwright_stealth", "Reply window expired, so the send must use the Playwright stealth route."
        return "blocked", "Reply window expired and no target username is available for the browser route."

    if trigger_key in {"story_keyword", "story_reaction", "cold_initiation", "manual_outbound"}:
        if target_username:
            return "playwright_stealth", "Outbound initiation and story-triggered sends use the Playwright stealth route."
        return "blocked", "The browser initiation route needs a target username."

    if _within_reply_window(cfg, latest.get("sent_at")):
        if not cfg.meta_graph_access_token or not cfg.meta_graph_ig_user_id:
            return "blocked", "Meta Graph credentials are missing for an in-window reply."
        if not recipient_id:
            return "blocked", "Recipient Meta user ID is missing for an in-window reply."
        return "meta_graph", "Most recent inbound message is inside the Meta Graph reply window."
    if target_username:
        return "playwright_stealth", "No eligible in-window inbound reply was found, so the browser route is required."
    return "blocked", "The route could not be resolved safely."


def _sync_style_examples(
    conn,
    cfg: Config,
    *,
    platform: str,
    account_username: str,
    target_username: str,
) -> None:
    rows = get_recent_self_dm_messages(
        conn,
        platform=platform,
        account_username=account_username,
        target_username=target_username,
        limit=max(cfg.digital_twin.style_example_count * 4, 12),
    )
    if not rows:
        rows = get_recent_self_dm_messages(
            conn,
            platform=platform,
            account_username=account_username,
            target_username=None,
            limit=max(cfg.digital_twin.style_example_count * 4, 12),
        )

    for row in rows:
        payload = _record_dict(row)
        upsert_style_example(
            platform=platform,
            account_username=account_username,
            target_username=target_username,
            conversation_id=payload.get("conversation_id"),
            message_key=str(payload.get("message_key") or payload.get("external_message_id") or payload.get("id")),
            text=_clean_text(payload.get("text")),
            sent_at=payload.get("sent_at"),
            data_dir=cfg.data_dir,
        )


def _sync_target_osint_documents(
    cfg: Config,
    *,
    platform: str,
    target_username: str,
) -> None:
    root = _target_profile_dir(cfg, platform, target_username)
    if not root.exists():
        return

    profile_payload = _load_json_file(root / "profile.json")
    intelligence_payload = _load_json_file(root / "intelligence.json")
    posts_payload = _load_json_file(root / "posts.json")
    stories_payload = _load_json_file(root / "stories.json")

    if profile_payload:
        bio = profile_payload.get("bio") or {}
        summary = {
            "username": profile_payload.get("username"),
            "display_name": bio.get("display_name"),
            "bio_text": bio.get("bio_text"),
            "follower_count": bio.get("follower_count"),
            "following_count": bio.get("following_count"),
            "post_count": bio.get("post_count"),
        }
        upsert_target_profile_document(
            platform=platform,
            target_username=target_username,
            source_kind="profile_summary",
            source_id="profile_json",
            content=json.dumps(summary, ensure_ascii=False),
            metadata={"snapshot_at": bio.get("snapshot_at") or profile_payload.get("generated_at")},
            data_dir=cfg.data_dir,
        )

    if intelligence_payload:
        upsert_target_profile_document(
            platform=platform,
            target_username=target_username,
            source_kind="intelligence_summary",
            source_id="intelligence_json",
            content=json.dumps(intelligence_payload, ensure_ascii=False),
            metadata={"generated_at": intelligence_payload.get("generated_at")},
            data_dir=cfg.data_dir,
        )

    for index, item in enumerate((posts_payload.get("items") or [])[:6]):
        content = {
            "caption": item.get("caption"),
            "posted_at": item.get("posted_at"),
            "like_count": item.get("like_count"),
            "comment_count": item.get("comment_count"),
            "url": item.get("url"),
        }
        upsert_target_profile_document(
            platform=platform,
            target_username=target_username,
            source_kind="recent_post",
            source_id=str(item.get("post_id") or f"post_{index}"),
            content=json.dumps(content, ensure_ascii=False),
            metadata={"posted_at": item.get("posted_at")},
            data_dir=cfg.data_dir,
        )

    for index, item in enumerate((stories_payload.get("items") or [])[:6]):
        content = {
            "story_id": item.get("story_id"),
            "story_type": item.get("story_type"),
            "posted_at": item.get("posted_at"),
            "caption": (_json_loads(item.get("metadata")) or {}).get("caption"),
            "url": item.get("url"),
        }
        upsert_target_profile_document(
            platform=platform,
            target_username=target_username,
            source_kind="recent_story",
            source_id=str(item.get("story_id") or f"story_{index}"),
            content=json.dumps(content, ensure_ascii=False),
            metadata={"posted_at": item.get("posted_at")},
            data_dir=cfg.data_dir,
        )


def _query_context_chunks(
    cfg: Config,
    *,
    platform: str,
    account_username: str,
    target_username: str,
    latest_inbound_message: dict[str, Any] | None,
    story_payload: dict[str, Any] | None,
) -> tuple[list[dict], list[dict]]:
    inbound_text = _clean_text((latest_inbound_message or {}).get("text"))
    story_text = _story_text_blob(story_payload or {})
    query_text = "\n".join(
        part
        for part in [
            f"Draft a private social message to {target_username}.",
            inbound_text,
            story_text,
        ]
        if part
    ).strip() or f"Draft a private social message to {target_username}."

    style_results = query_style_examples(
        query_text=query_text,
        data_dir=cfg.data_dir,
        account_username=account_username,
        target_username=target_username,
        n_results=cfg.digital_twin.style_example_count,
    )
    if len(style_results) < cfg.digital_twin.style_example_count:
        fallback_results = query_style_examples(
            query_text=query_text,
            data_dir=cfg.data_dir,
            account_username=account_username,
            target_username=None,
            n_results=cfg.digital_twin.style_example_count,
        )
        seen_ids = {item["id"] for item in style_results}
        for item in fallback_results:
            if item["id"] in seen_ids:
                continue
            seen_ids.add(item["id"])
            style_results.append(item)
            if len(style_results) >= cfg.digital_twin.style_example_count:
                break

    profile_results = query_target_profiles(
        query_text=query_text,
        data_dir=cfg.data_dir,
        platform=platform,
        target_username=target_username,
        n_results=cfg.digital_twin.profile_context_count,
    )
    return style_results[: cfg.digital_twin.style_example_count], profile_results[: cfg.digital_twin.profile_context_count]


async def _generate_gemini_draft(
    cfg: Config,
    *,
    platform: str,
    account_username: str,
    target_username: str,
    trigger_type: str,
    route: str,
    latest_inbound_message: dict[str, Any] | None,
    story_payload: dict[str, Any] | None,
    style_results: list[dict],
    profile_results: list[dict],
) -> dict[str, Any]:
    """Call Gemini with retrieved style and OSINT context and return structured JSON."""
    if not cfg.gemini_api_key:
        raise DigitalTwinError("GEMINI_API_KEY is not configured.")

    try:
        import google.generativeai as genai  # type: ignore
    except ImportError as exc:
        raise DigitalTwinError("google-generativeai is not installed.") from exc

    genai.configure(api_key=cfg.gemini_api_key)
    model = genai.GenerativeModel(
        model_name=cfg.digital_twin.default_model,
        system_instruction=(
            "You draft private direct messages for a human reviewer. "
            "Match the user's style using only the supplied writing examples. "
            "Use only the supplied OSINT/profile facts. Do not invent facts, promises, or relationship history. "
            "Keep the draft concise, natural, and human. Return JSON only."
        ),
    )

    inbound_text = _clean_text((latest_inbound_message or {}).get("text"))
    story_text = _story_text_blob(story_payload or {})
    prompt = {
        "task": "Draft one private message for human review.",
        "platform": platform,
        "account_username": account_username,
        "target_username": target_username,
        "trigger_type": trigger_type,
        "selected_route": route,
        "latest_inbound_message": inbound_text or None,
        "story_trigger_context": story_text or None,
        "style_examples": [item.get("document") for item in style_results if item.get("document")],
        "target_osint_context": [item.get("document") for item in profile_results if item.get("document")],
        "response_schema": {
            "draft_text": "string",
            "intent_label": "string",
            "route_hint": "string",
            "style_notes": "string",
            "context_sources": [
                {"source_kind": "string", "summary": "string"}
            ],
            "risk_flags": ["string"],
        },
    }

    response = model.generate_content(
        json.dumps(prompt, ensure_ascii=False),
        generation_config=genai.GenerationConfig(
            response_mime_type="application/json",
            temperature=0.6,
        ),
    )
    try:
        payload = json.loads(_clean_text(response.text))
    except Exception as exc:
        raise DigitalTwinError("Gemini did not return valid JSON.") from exc

    missing = [key for key in _JSON_REQUIRED_KEYS if key not in payload]
    if missing:
        raise DigitalTwinError(f"Gemini response is missing required keys: {', '.join(sorted(missing))}")

    draft_text = _clean_text(payload.get("draft_text"))
    if not draft_text:
        raise DigitalTwinError("Gemini returned an empty draft_text.")

    payload["draft_text"] = draft_text
    payload["context_sources"] = payload.get("context_sources") or []
    payload["risk_flags"] = payload.get("risk_flags") or []
    return payload


def _bot_or_default(cfg: Config, bot: Any | None) -> Any:
    return bot or build_bot(cfg.telegram_bot_token)


async def _post_draft_for_review(
    cfg: Config,
    conn,
    *,
    draft_id: int,
    bot: Any | None = None,
) -> None:
    draft = get_message_draft(conn, draft_id)
    if not draft:
        raise DigitalTwinError(f"Draft {draft_id} no longer exists.")
    message = await send_digital_twin_draft(
        _bot_or_default(cfg, bot),
        cfg.notifications.chat_id,
        _record_dict(draft),
        message_thread_id=cfg.notifications.digital_twin_thread(),
    )
    if message is not None:
        update_message_draft_review_message(
            conn,
            draft_id,
            telegram_chat_id=str(message.chat_id),
            telegram_thread_id=cfg.notifications.digital_twin_thread(),
            telegram_message_id=message.message_id,
        )


def _build_context_snapshot(
    *,
    latest_inbound_message: dict[str, Any] | None,
    story_payload: dict[str, Any] | None,
    style_results: list[dict],
    profile_results: list[dict],
) -> dict[str, Any]:
    return {
        "latest_inbound_message": latest_inbound_message or {},
        "story_payload": story_payload or {},
        "style_examples": [
            {
                "id": item.get("id"),
                "distance": item.get("distance"),
                "document": item.get("document"),
                "metadata": item.get("metadata"),
            }
            for item in style_results
        ],
        "profile_context": [
            {
                "id": item.get("id"),
                "distance": item.get("distance"),
                "document": item.get("document"),
                "metadata": item.get("metadata"),
            }
            for item in profile_results
        ],
    }


async def queue_inbound_reply_draft(
    *,
    platform: str,
    account_username: str,
    conversation_id: str,
    bot: Any | None = None,
    source_message_id: str | None = None,
    project_root: Path = PROJECT_ROOT,
) -> int | None:
    """Generate and queue one AI reply draft for a DM conversation."""
    cfg, conn = _load_runtime(project_root)
    try:
        if not cfg.digital_twin.enabled:
            return None

        conversation_row = get_dm_conversation_row(conn, platform, account_username, conversation_id)
        if not conversation_row:
            return None

        participants = [_record_dict(row) for row in get_dm_participants(conn, conversation_row["id"])]
        primary_target = _pick_target_from_participants(participants)
        if not primary_target:
            logger.info(f"[digital_twin] No target participant resolved for {platform}/{account_username}/{conversation_id}")
            return None

        target_username = _clean_text(primary_target.get("username"))
        recipient_id = _clean_text(primary_target.get("user_id")) or None

        latest_inbound = get_latest_inbound_dm_message(conn, conversation_row["id"])
        latest_inbound_payload = _record_dict(latest_inbound) if latest_inbound else None
        if source_message_id:
            selected = conn.execute(
                """
                SELECT *
                FROM dm_messages
                WHERE conversation_id=?
                  AND (message_key=? OR external_message_id=?)
                ORDER BY id DESC
                LIMIT 1
                """,
                (conversation_row["id"], source_message_id, source_message_id),
            ).fetchone()
            if selected:
                latest_inbound_payload = _record_dict(selected)

        if not latest_inbound_payload or latest_inbound_payload.get("is_self"):
            return None

        source_key = str(
            latest_inbound_payload.get("external_message_id")
            or latest_inbound_payload.get("message_key")
            or latest_inbound_payload.get("id")
        )
        existing = find_message_draft_by_source(
            conn,
            platform=platform,
            account_username=account_username,
            conversation_id=conversation_id,
            source_message_id=source_key,
            trigger_type="inbound_reply",
        )
        if existing:
            return int(existing["id"])

        _sync_style_examples(
            conn,
            cfg,
            platform=platform,
            account_username=account_username,
            target_username=target_username,
        )
        _sync_target_osint_documents(cfg, platform=platform, target_username=target_username)
        style_results, profile_results = _query_context_chunks(
            cfg,
            platform=platform,
            account_username=account_username,
            target_username=target_username,
            latest_inbound_message=latest_inbound_payload,
            story_payload=None,
        )

        route, route_reason = select_send_route(
            cfg,
            trigger_type="inbound_reply",
            latest_inbound_message=latest_inbound_payload,
            target_username=target_username,
            recipient_id=recipient_id,
        )

        draft_payload = await _generate_gemini_draft(
            cfg,
            platform=platform,
            account_username=account_username,
            target_username=target_username,
            trigger_type="inbound_reply",
            route=route,
            latest_inbound_message=latest_inbound_payload,
            story_payload=None,
            style_results=style_results,
            profile_results=profile_results,
        )

        target_row = get_target_row(conn, platform, target_username)
        draft_id = create_message_draft(
            conn,
            platform=platform,
            account_id=conversation_row["account_row_id"],
            account_username=account_username,
            target_id=target_row["id"] if target_row else None,
            target_username=target_username,
            conversation_row_id=conversation_row["id"],
            conversation_id=conversation_id,
            source_message_id=source_key,
            source_story_id=None,
            trigger_type="inbound_reply",
            trigger_payload={
                "latest_inbound_message_id": source_key,
                "latest_inbound_sent_at": latest_inbound_payload.get("sent_at"),
                "recipient_id": recipient_id,
            },
            route=route,
            route_reason=route_reason,
            prompt_version=PROMPT_VERSION,
            gemini_context=_build_context_snapshot(
                latest_inbound_message=latest_inbound_payload,
                story_payload=None,
                style_results=style_results,
                profile_results=profile_results,
            ),
            intent_label=_clean_text(draft_payload.get("intent_label")) or "reply",
            route_hint=_clean_text(draft_payload.get("route_hint")),
            style_notes=_clean_text(draft_payload.get("style_notes")),
            risk_flags=draft_payload.get("risk_flags"),
            context_sources=draft_payload.get("context_sources"),
            draft_text=draft_payload["draft_text"],
            review_status="blocked" if route == "blocked" else "pending_review",
        )
        append_message_draft_event(
            conn,
            draft_id,
            "created",
            payload={
                "trigger_type": "inbound_reply",
                "route": route,
                "route_reason": route_reason,
                "target_username": target_username,
            },
        )
        await _post_draft_for_review(cfg, conn, draft_id=draft_id, bot=bot)
        return draft_id
    finally:
        conn.close()


async def maybe_queue_story_initiation_draft(
    *,
    platform: str,
    account_username: str,
    target_username: str,
    story_payload: dict[str, Any],
    bot: Any | None = None,
    project_root: Path = PROJECT_ROOT,
) -> int | None:
    """Create an outbound initiation draft when a story matches configured keywords."""
    cfg, conn = _load_runtime(project_root)
    try:
        if not cfg.digital_twin.enabled:
            return None

        rule, matched_keywords = _match_story_keywords(
            cfg,
            platform=platform,
            target_username=target_username,
            story_payload=story_payload,
        )
        if not rule or not matched_keywords:
            return None

        max_pending = rule.max_pending_drafts or cfg.digital_twin.max_pending_drafts_per_target
        active_drafts = list_active_message_drafts_for_target(
            conn,
            platform=platform,
            target_username=target_username,
        )
        if len(active_drafts) >= max_pending:
            logger.info(
                f"[digital_twin] Skipping story draft for {platform}/{target_username}: "
                f"{len(active_drafts)} active drafts already exist."
            )
            return None

        cooldown_hours = rule.cooldown_hours or cfg.digital_twin.initiation_cooldown_hours
        recent_cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=cooldown_hours)
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
        recent_draft = get_recent_message_draft_for_target(
            conn,
            platform=platform,
            target_username=target_username,
            since_iso=recent_cutoff,
        )
        if recent_draft:
            return None

        _sync_style_examples(
            conn,
            cfg,
            platform=platform,
            account_username=account_username,
            target_username=target_username,
        )
        _sync_target_osint_documents(cfg, platform=platform, target_username=target_username)
        style_results, profile_results = _query_context_chunks(
            cfg,
            platform=platform,
            account_username=account_username,
            target_username=target_username,
            latest_inbound_message=None,
            story_payload=story_payload,
        )

        route, route_reason = select_send_route(
            cfg,
            trigger_type="story_keyword",
            latest_inbound_message=None,
            target_username=target_username,
            recipient_id=None,
        )

        draft_payload = await _generate_gemini_draft(
            cfg,
            platform=platform,
            account_username=account_username,
            target_username=target_username,
            trigger_type="story_keyword",
            route=route,
            latest_inbound_message=None,
            story_payload=story_payload,
            style_results=style_results,
            profile_results=profile_results,
        )

        target_row = get_target_row(conn, platform, target_username)
        account_row = conn.execute(
            "SELECT id FROM accounts WHERE platform=? AND username=?",
            (platform, account_username),
        ).fetchone()
        draft_id = create_message_draft(
            conn,
            platform=platform,
            account_id=account_row["id"] if account_row else None,
            account_username=account_username,
            target_id=target_row["id"] if target_row else None,
            target_username=target_username,
            conversation_row_id=None,
            conversation_id=None,
            source_message_id=None,
            source_story_id=_clean_text(story_payload.get("story_id")) or None,
            trigger_type="story_keyword",
            trigger_payload={
                "matched_keywords": matched_keywords,
                "story_id": story_payload.get("story_id"),
                "story_url": story_payload.get("url"),
                "priority": rule.priority,
            },
            route=route,
            route_reason=route_reason,
            prompt_version=PROMPT_VERSION,
            gemini_context=_build_context_snapshot(
                latest_inbound_message=None,
                story_payload=story_payload,
                style_results=style_results,
                profile_results=profile_results,
            ),
            intent_label=_clean_text(draft_payload.get("intent_label")) or "story_outreach",
            route_hint=_clean_text(draft_payload.get("route_hint")),
            style_notes=_clean_text(draft_payload.get("style_notes")),
            risk_flags=draft_payload.get("risk_flags"),
            context_sources=draft_payload.get("context_sources"),
            draft_text=draft_payload["draft_text"],
            review_status="blocked" if route == "blocked" else "pending_review",
            send_status="not_sent",
        )
        append_message_draft_event(
            conn,
            draft_id,
            "created",
            payload={
                "trigger_type": "story_keyword",
                "matched_keywords": matched_keywords,
                "story_id": story_payload.get("story_id"),
                "route": route,
                "route_reason": route_reason,
            },
        )
        await _post_draft_for_review(cfg, conn, draft_id=draft_id, bot=bot)
        return draft_id
    finally:
        conn.close()


async def begin_edit_review(
    draft_id: int,
    *,
    actor_user_id: str,
    actor_username: str | None,
    bot: Any | None = None,
    project_root: Path = PROJECT_ROOT,
) -> bool:
    """Move a reviewable draft into the edit state and prompt for replacement text."""
    cfg, conn = _load_runtime(project_root)
    try:
        draft = get_message_draft(conn, draft_id)
        if not draft or draft["review_status"] not in {"pending_review", "blocked"}:
            return False
        set_message_draft_editing(
            conn,
            draft_id,
            actor_user_id=str(actor_user_id),
            actor_username=actor_username,
        )
        append_message_draft_event(
            conn,
            draft_id,
            "edit_started",
            actor_user_id=str(actor_user_id),
            actor_username=actor_username,
        )
        draft_payload = _record_dict(draft)
        if draft_payload.get("telegram_chat_id") and draft_payload.get("telegram_message_id"):
            await disable_digital_twin_controls(
                _bot_or_default(cfg, bot),
                draft_payload["telegram_chat_id"],
                int(draft_payload["telegram_message_id"]),
            )
        await send_digital_twin_edit_prompt(
            _bot_or_default(cfg, bot),
            cfg.notifications.chat_id,
            draft_id=draft_id,
            target_username=str(draft["target_username"]),
            message_thread_id=cfg.notifications.digital_twin_thread(),
        )
        return True
    finally:
        conn.close()


async def apply_edit_from_telegram_message(
    *,
    actor_user_id: str,
    actor_username: str | None,
    edited_text: str,
    chat_id: str,
    thread_id: int | None,
    bot: Any | None = None,
    project_root: Path = PROJECT_ROOT,
) -> int | None:
    """Consume one reviewer text message as the replacement draft body."""
    cfg, conn = _load_runtime(project_root)
    try:
        if not cfg.digital_twin.enabled:
            return None
        edited = _clean_text(edited_text)
        if not edited:
            return None
        draft = get_editing_message_draft_for_reviewer(
            conn,
            actor_user_id=str(actor_user_id),
            chat_id=chat_id,
            thread_id=thread_id,
        )
        if not draft:
            return None
        save_message_draft_edited_text(conn, draft["id"], edited_text=edited)
        append_message_draft_event(
            conn,
            draft["id"],
            "edited",
            actor_user_id=str(actor_user_id),
            actor_username=actor_username,
            payload={"draft_text": edited},
        )
        await _post_draft_for_review(cfg, conn, draft_id=int(draft["id"]), bot=bot)
        return int(draft["id"])
    finally:
        conn.close()


async def approve_draft(
    draft_id: int,
    *,
    actor_user_id: str,
    actor_username: str | None,
    bot: Any | None = None,
    project_root: Path = PROJECT_ROOT,
) -> bool:
    """Approve a draft, execute the selected route, and post the delivery result."""
    cfg, conn = _load_runtime(project_root)
    try:
        draft = get_message_draft(conn, draft_id)
        if not draft or draft["review_status"] not in {"pending_review", "blocked"}:
            return False

        draft_payload = _record_dict(draft)
        final_text = _clean_text(draft_payload.get("draft_text"))
        if not final_text:
            raise DigitalTwinError(f"Draft {draft_id} has no text to approve.")

        if draft_payload.get("telegram_chat_id") and draft_payload.get("telegram_message_id"):
            await disable_digital_twin_controls(
                _bot_or_default(cfg, bot),
                draft_payload["telegram_chat_id"],
                int(draft_payload["telegram_message_id"]),
            )

        mark_message_draft_approved(
            conn,
            draft_id,
            approved_text=final_text,
            actor_user_id=str(actor_user_id),
            actor_username=actor_username,
        )
        append_message_draft_event(
            conn,
            draft_id,
            "approved",
            actor_user_id=str(actor_user_id),
            actor_username=actor_username,
        )

        try:
            send_result = await execute_message_route(
                project_root=project_root,
                route=str(draft_payload.get("route") or "blocked"),
                platform=str(draft_payload["platform"]),
                account_username=str(draft_payload["account_username"]),
                conversation_id=_clean_text(draft_payload.get("conversation_id")) or None,
                target_username=_clean_text(draft_payload.get("target_username")) or None,
                text=final_text,
                recipient_id=_derive_recipient_id(draft_payload),
                reply_to_message_id=_clean_text(draft_payload.get("source_message_id")) or None,
            )
            mark_message_draft_sent(conn, draft_id)
            append_message_draft_event(
                conn,
                draft_id,
                "sent",
                actor_user_id=str(actor_user_id),
                actor_username=actor_username,
                payload=send_result,
            )
            await send_digital_twin_result(
                _bot_or_default(cfg, bot),
                cfg.notifications.chat_id,
                draft_id=draft_id,
                target_username=str(draft_payload["target_username"]),
                status="sent",
                detail=send_result,
                message_thread_id=cfg.notifications.digital_twin_thread(),
            )
            return True
        except Exception as exc:
            error_text = str(exc)
            logger.warning(f"[digital_twin] Draft {draft_id} send failed: {error_text}")
            mark_message_draft_send_failed(conn, draft_id, error_text=error_text)
            append_message_draft_event(
                conn,
                draft_id,
                "send_failed",
                actor_user_id=str(actor_user_id),
                actor_username=actor_username,
                payload={"error": error_text},
            )
            await send_digital_twin_result(
                _bot_or_default(cfg, bot),
                cfg.notifications.chat_id,
                draft_id=draft_id,
                target_username=str(draft_payload["target_username"]),
                status="send_failed",
                detail={"error": error_text},
                message_thread_id=cfg.notifications.digital_twin_thread(),
            )
            return False
    finally:
        conn.close()


async def reject_draft(
    draft_id: int,
    *,
    actor_user_id: str,
    actor_username: str | None,
    bot: Any | None = None,
    project_root: Path = PROJECT_ROOT,
) -> bool:
    """Reject a draft and record the decision in the audit trail."""
    cfg, conn = _load_runtime(project_root)
    try:
        draft = get_message_draft(conn, draft_id)
        if not draft or draft["review_status"] not in {"pending_review", "blocked", "editing"}:
            return False
        draft_payload = _record_dict(draft)
        if draft_payload.get("telegram_chat_id") and draft_payload.get("telegram_message_id"):
            await disable_digital_twin_controls(
                _bot_or_default(cfg, bot),
                draft_payload["telegram_chat_id"],
                int(draft_payload["telegram_message_id"]),
            )
        mark_message_draft_rejected(
            conn,
            draft_id,
            actor_user_id=str(actor_user_id),
            actor_username=actor_username,
        )
        append_message_draft_event(
            conn,
            draft_id,
            "rejected",
            actor_user_id=str(actor_user_id),
            actor_username=actor_username,
        )
        await send_digital_twin_result(
            _bot_or_default(cfg, bot),
            cfg.notifications.chat_id,
            draft_id=draft_id,
            target_username=str(draft["target_username"]),
            status="rejected",
            detail={},
            message_thread_id=cfg.notifications.digital_twin_thread(),
        )
        return True
    finally:
        conn.close()


def _derive_recipient_id(draft_payload: dict[str, Any]) -> str | None:
    trigger_payload = _json_loads(draft_payload.get("trigger_payload_json")) or {}
    recipient_id = _clean_text(trigger_payload.get("recipient_id"))
    if recipient_id:
        return recipient_id

    context = _json_loads(draft_payload.get("gemini_context_json")) or {}
    inbound = context.get("latest_inbound_message") or {}
    recipient_id = _clean_text(inbound.get("sender_user_id"))
    return recipient_id or None


def list_pending_drafts(
    *,
    project_root: Path = PROJECT_ROOT,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return pending or blocked drafts for Telegram status commands."""
    cfg, conn = _load_runtime(project_root)
    try:
        if not cfg.digital_twin.enabled:
            return []
        return [_record_dict(row) for row in get_pending_message_drafts(conn, limit=limit)]
    finally:
        conn.close()
