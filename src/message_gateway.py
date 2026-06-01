"""Route-aware message execution for owned-account direct messages."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.config import load_config
from src.db import (
    get_connection,
    replace_dm_participants,
    upsert_account,
    upsert_dm_conversation,
    upsert_dm_message,
)
from src.dm_builder import build_dm_archive
from src.meta_graph import MetaGraphClient
from src.proxy_manager import ProxyManager


def _env_path(project_root: Path) -> Path:
    env_local = project_root / ".env.local"
    if env_local.exists():
        return env_local
    return project_root / ".env"


def prepare_send(*, project_root: Path) -> tuple[Any, Any, ProxyManager]:
    """Load config, open a DB connection, and build the proxy manager."""
    cfg = load_config(project_root / "settings.yaml", _env_path(project_root))
    conn = get_connection(cfg.db_path)
    proxy_mgr = ProxyManager(cfg.proxies, conn)
    return cfg, conn, proxy_mgr


async def _refresh_conversation_archive_with_runtime(
    *,
    cfg,
    conn,
    proxy_mgr: ProxyManager,
    platform: str,
    username: str,
    conversation_id: str,
) -> dict[str, Any]:
    from src.main import _ensure_logged_in, _run_with_session

    if platform == "instagram":
        from src.scrapers.instagram import scrape_dm_thread
    else:
        raise NotImplementedError(
            f"Conversation refresh is not implemented for {platform} yet."
        )

    async def _refresh_fn(page, runtime_cfg):
        page = await _ensure_logged_in(page, platform, username, runtime_cfg)
        refreshed = await scrape_dm_thread(page, username, conversation_id, runtime_cfg)
        if not refreshed:
            raise RuntimeError("The conversation could not be refreshed after send.")
        return refreshed

    refreshed = await _run_with_session(
        platform,
        username,
        _refresh_fn,
        cfg,
        conn,
        proxy_mgr,
        bot=None,
    )
    if not isinstance(refreshed, dict):
        raise RuntimeError("The conversation refresh did not return a usable payload.")
    return refreshed


async def refresh_conversation_archive(
    *,
    project_root: Path,
    platform: str,
    username: str,
    conversation_id: str,
) -> dict[str, Any]:
    """Refresh one archived conversation and persist it back into the DM tables."""
    cfg, conn, proxy_mgr = prepare_send(project_root=project_root)
    try:
        account_id = upsert_account(
            conn,
            platform,
            username,
            session_file=f"{platform}_{username}.json",
        )
        conversation = await _refresh_conversation_archive_with_runtime(
            cfg=cfg,
            conn=conn,
            proxy_mgr=proxy_mgr,
            platform=platform,
            username=username,
            conversation_id=conversation_id,
        )
        _persist_conversation(conn, account_id, platform, conversation)
        build_dm_archive(conn, account_id, cfg.accounts_dir)
        return conversation
    finally:
        conn.close()


async def send_via_playwright(
    *,
    project_root: Path,
    platform: str,
    username: str,
    text: str,
    conversation_id: str | None = None,
    target_username: str | None = None,
) -> dict[str, Any]:
    """Send a DM through the Playwright stealth browser path."""
    message_text = str(text or "").strip()
    if not message_text:
        raise ValueError("Message text is required.")

    cfg, conn, proxy_mgr = prepare_send(project_root=project_root)
    try:
        account_id = upsert_account(
            conn,
            platform,
            username,
            session_file=f"{platform}_{username}.json",
        )

        from src.main import _ensure_logged_in, _run_with_session

        if platform == "instagram":
            from src.scrapers.instagram import scrape_dm_thread, send_dm_text, send_new_dm_text
        else:
            raise NotImplementedError(
                f"Playwright sending is not implemented for {platform} yet."
            )

        async def _send_fn(page, runtime_cfg):
            page = await _ensure_logged_in(page, platform, username, runtime_cfg)
            resolved_conversation_id = conversation_id
            if resolved_conversation_id:
                await send_dm_text(page, username, resolved_conversation_id, message_text, runtime_cfg)
            else:
                if not target_username:
                    raise ValueError("target_username is required for outbound initiation sends.")
                init_result = await send_new_dm_text(
                    page,
                    own_username=username,
                    target_username=target_username,
                    text=message_text,
                    cfg=runtime_cfg,
                )
                resolved_conversation_id = str(init_result.get("conversation_id") or "").strip() or None
                if not resolved_conversation_id:
                    raise RuntimeError("The browser initiation path did not resolve a conversation_id.")

            refreshed = await scrape_dm_thread(page, username, resolved_conversation_id, runtime_cfg)
            if not refreshed:
                raise RuntimeError("The conversation could not be refreshed after send.")
            return refreshed

        conversation = await _run_with_session(
            platform,
            username,
            _send_fn,
            cfg,
            conn,
            proxy_mgr,
            bot=None,
        )
        if not isinstance(conversation, dict):
            raise RuntimeError("The browser send did not return a refreshed conversation.")

        _persist_conversation(conn, account_id, platform, conversation)
        build_dm_archive(conn, account_id, cfg.accounts_dir)

        return {
            "route": "playwright_stealth",
            "platform": platform,
            "username": username,
            "conversation_id": conversation.get("conversation_id") or conversation_id,
            "target_username": target_username,
            "message_count": len(conversation.get("messages") or []),
            "last_message_at": conversation.get("last_message_at"),
            "last_message_preview": conversation.get("last_message_preview"),
        }
    finally:
        conn.close()


async def send_via_meta_graph(
    *,
    project_root: Path,
    platform: str,
    username: str,
    conversation_id: str,
    recipient_id: str,
    text: str,
    reply_to_message_id: str | None = None,
) -> dict[str, Any]:
    """Send a DM through the Meta Graph API and then refresh the archive."""
    message_text = str(text or "").strip()
    if not message_text:
        raise ValueError("Message text is required.")
    if not conversation_id:
        raise ValueError("conversation_id is required for Meta Graph sends.")
    if not recipient_id:
        raise ValueError("recipient_id is required for Meta Graph sends.")

    cfg, conn, proxy_mgr = prepare_send(project_root=project_root)
    try:
        account_id = upsert_account(
            conn,
            platform,
            username,
            session_file=f"{platform}_{username}.json",
        )
        if platform != "instagram":
            raise NotImplementedError(
                f"Meta Graph sending is not implemented for {platform} yet."
            )

        client = MetaGraphClient(
            access_token=cfg.meta_graph_access_token,
            instagram_actor_id=cfg.meta_graph_ig_user_id,
            api_version=cfg.meta_graph_api_version,
            base_url=cfg.meta_graph_base_url,
            app_secret=cfg.meta_graph_app_secret,
        )
        graph_payload = await client.send_text_message(
            recipient_id=recipient_id,
            text=message_text,
            reply_to_message_id=reply_to_message_id,
        )

        conversation = await _refresh_conversation_archive_with_runtime(
            cfg=cfg,
            conn=conn,
            proxy_mgr=proxy_mgr,
            platform=platform,
            username=username,
            conversation_id=conversation_id,
        )
        _persist_conversation(conn, account_id, platform, conversation)
        build_dm_archive(conn, account_id, cfg.accounts_dir)

        return {
            "route": "meta_graph",
            "platform": platform,
            "username": username,
            "conversation_id": conversation_id,
            "graph_payload": graph_payload,
            "message_count": len(conversation.get("messages") or []),
            "last_message_at": conversation.get("last_message_at"),
            "last_message_preview": conversation.get("last_message_preview"),
        }
    finally:
        conn.close()


async def execute_message_route(
    *,
    project_root: Path,
    route: str,
    platform: str,
    account_username: str,
    text: str,
    conversation_id: str | None = None,
    target_username: str | None = None,
    recipient_id: str | None = None,
    reply_to_message_id: str | None = None,
) -> dict[str, Any]:
    """Execute a prepared send using the selected route."""
    route_key = str(route or "").strip().lower()
    if route_key == "meta_graph":
        return await send_via_meta_graph(
            project_root=project_root,
            platform=platform,
            username=account_username,
            conversation_id=str(conversation_id or ""),
            recipient_id=str(recipient_id or ""),
            text=text,
            reply_to_message_id=reply_to_message_id,
        )
    if route_key == "playwright_stealth":
        return await send_via_playwright(
            project_root=project_root,
            platform=platform,
            username=account_username,
            conversation_id=conversation_id,
            target_username=target_username,
            text=text,
        )
    raise RuntimeError(f"Route {route!r} is blocked or unsupported.")


async def send_owned_account_message(
    *,
    project_root: Path,
    platform: str,
    username: str,
    conversation_id: str,
    text: str,
) -> dict[str, Any]:
    """Preserve the existing manual viewer send path through the browser route."""
    return await send_via_playwright(
        project_root=project_root,
        platform=platform,
        username=username,
        conversation_id=conversation_id,
        text=text,
    )


def _persist_conversation(
    conn,
    account_id: int,
    platform: str,
    conversation: dict[str, Any],
) -> None:
    """Persist a refreshed conversation snapshot back into the DM tables."""
    conversation.setdefault("platform", platform)
    conversation_row_id = upsert_dm_conversation(conn, account_id, conversation)
    replace_dm_participants(
        conn,
        conversation_row_id,
        conversation.get("participants") or [],
    )

    for index, message in enumerate(conversation.get("messages") or []):
        message.setdefault("message_order", index)
        upsert_dm_message(
            conn,
            conversation_row_id,
            platform,
            message,
        )
