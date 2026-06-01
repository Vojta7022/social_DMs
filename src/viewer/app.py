"""FastAPI app for browsing the local social-scanner archive."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode

from fastapi import Body, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2.runtime import Undefined
from markupsafe import Markup, escape
from src.utils import platform_profile_url as _shared_platform_profile_url

from .catalog import ArchiveCatalog
import src.db as db_module

VIEWER_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = VIEWER_ROOT.parent.parent
DEFAULT_DATA_ROOT = PROJECT_ROOT / "data"
TEMPLATES_DIR = VIEWER_ROOT / "templates"
STATIC_DIR = VIEWER_ROOT / "static"
VALID_TABS = {"overview", "posts", "mentions", "reposts", "stories", "highlights", "connections", "deep_profile"}
INSTAGRAM_USERNAME_RE = re.compile(r"(?<![\w.])@([A-Za-z0-9._]+)")
PROFILE_SORT_KEYS = {"name", "followers", "following", "archived_posts", "last_activity", "last_export", "platform"}
PROFILE_SCOPE_KEYS = {"all", "own", "tracked"}
PROFILE_CONTENT_KEYS = {"all", "with_any", "with_posts", "with_mentions", "with_stories", "with_highlights"}
MEDIA_SORT_KEYS = {"newest", "oldest", "likes", "comments", "views", "vibe", "files", "title"}
MEDIA_FILTER_KEYS = {"all", "with_caption", "with_comments", "photos_only", "videos_only", "with_audio", "multi_file"}
ACCOUNT_SORT_KEYS = {"name", "conversations", "messages", "attachments", "last_message"}


def create_app(data_root: Optional[Path] = None) -> FastAPI:
    """Create the read-only archive viewer app."""
    resolved_data_root = data_root or DEFAULT_DATA_ROOT
    catalog = ArchiveCatalog(resolved_data_root)
    _db_path = resolved_data_root / "social_scanner.db"

    def _get_db():
        return db_module.get_connection(_db_path)

    app = FastAPI(
        title="social-scanner archive viewer",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.catalog = catalog

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.auto_reload = True
    templates.env.cache = {}
    _install_template_helpers(templates)
    app.state.templates = templates

    async def _send_conversation_message(
        platform: str,
        username: str,
        conversation_id: str,
        text: str,
    ) -> dict[str, Any]:
        account, conversation = catalog.get_conversation(platform, username, conversation_id)
        if not account:
            raise HTTPException(status_code=404, detail="Account archive not found")
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")

        from src.message_gateway import send_owned_account_message

        try:
            return await send_owned_account_message(
                project_root=PROJECT_ROOT,
                platform=platform,
                username=username,
                conversation_id=conversation_id,
                text=text,
            )
        except NotImplementedError as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    app.mount(
        "/media",
        StaticFiles(directory=str(catalog.media_root), check_dir=False),
        name="media",
    )
    app.mount(
        "/archive",
        StaticFiles(directory=str(resolved_data_root), check_dir=False),
        name="archive",
    )
    app.mount(
        "/assets",
        StaticFiles(directory=str(STATIC_DIR), check_dir=False),
        name="assets",
    )

    @app.get("/", response_class=HTMLResponse, name="dashboard")
    async def dashboard(
        request: Request,
        platform: Optional[str] = Query(default=None),
        q: Optional[str] = Query(default=None),
        scope: str = Query(default="all"),
        content: str = Query(default="all"),
        sort_profiles: str = Query(default="name"),
        profile_dir: str = Query(default="asc"),
    ) -> HTMLResponse:
        if scope not in PROFILE_SCOPE_KEYS:
            scope = "all"
        if content not in PROFILE_CONTENT_KEYS:
            content = "all"
        if sort_profiles not in PROFILE_SORT_KEYS:
            sort_profiles = "name"
        if profile_dir not in {"asc", "desc"}:
            profile_dir = "asc"

        profiles = catalog.list_profiles(platform=platform, search=q)
        profiles = _filter_profile_summaries(profiles, scope=scope, content=content)
        profiles = _sort_profile_summaries(profiles, sort_key=sort_profiles, direction=profile_dir)
        item_matches = catalog.search_items(platform=platform, search=q)
        context = {
            "request": request,
            "page_title": "Archive Dashboard",
            "site_nav": _site_nav(request, "profiles"),
            "active_section": "profiles",
            "profiles": profiles,
            "item_matches": item_matches,
            "platforms": catalog.available_platforms(),
            "selected_platform": platform or "",
            "search_query": q or "",
            "selected_scope": scope,
            "selected_content": content,
            "selected_profile_sort": sort_profiles,
            "selected_profile_dir": profile_dir,
            "profile_scope_options": [
                ("all", "All accounts"),
                ("own", "Own accounts"),
                ("tracked", "Tracked only"),
            ],
            "profile_content_options": [
                ("all", "Any content state"),
                ("with_any", "Has any archive"),
                ("with_posts", "Has posts"),
                ("with_mentions", "Has mentions"),
                ("with_stories", "Has stories"),
                ("with_highlights", "Has highlights"),
            ],
            "profile_sort_options": [
                ("name", "Name"),
                ("followers", "Followers"),
                ("following", "Following"),
                ("archived_posts", "Archived posts"),
                ("last_activity", "Last posted/activity"),
                ("last_export", "Last export"),
                ("platform", "Platform"),
            ],
            "stats": {
                "profiles": len(profiles),
                "posts": sum(profile["counts"]["posts"] for profile in profiles),
                "mentions": sum(profile["counts"]["mentions"] for profile in profiles),
                "stories": sum(profile["counts"]["stories"] for profile in profiles),
                "highlights": sum(profile["counts"]["highlights"] for profile in profiles),
            },
        }
        return templates.TemplateResponse(request, "dashboard.html", context)

    @app.get("/api/profiles", response_class=JSONResponse, name="api_profiles")
    async def api_profiles(
        platform: Optional[str] = Query(default=None),
        q: Optional[str] = Query(default=None),
        scope: str = Query(default="all"),
        content: str = Query(default="all"),
        sort_profiles: str = Query(default="name"),
        profile_dir: str = Query(default="asc"),
    ) -> JSONResponse:
        if scope not in PROFILE_SCOPE_KEYS:
            scope = "all"
        if content not in PROFILE_CONTENT_KEYS:
            content = "all"
        if sort_profiles not in PROFILE_SORT_KEYS:
            sort_profiles = "name"
        if profile_dir not in {"asc", "desc"}:
            profile_dir = "asc"
        profiles = catalog.list_profiles(platform=platform, search=q)
        profiles = _filter_profile_summaries(profiles, scope=scope, content=content)
        profiles = _sort_profile_summaries(profiles, sort_key=sort_profiles, direction=profile_dir)
        item_matches = catalog.search_items(platform=platform, search=q)
        return JSONResponse(
            {
                "profiles": profiles,
                "item_matches": item_matches,
                "platforms": catalog.available_platforms(),
            }
        )

    @app.get("/accounts", response_class=HTMLResponse, name="accounts_dashboard")
    async def accounts_dashboard(
        request: Request,
        platform: Optional[str] = Query(default=None),
        q: Optional[str] = Query(default=None),
        sort_accounts: str = Query(default="last_message"),
        account_dir: str = Query(default="desc"),
    ) -> HTMLResponse:
        if sort_accounts not in ACCOUNT_SORT_KEYS:
            sort_accounts = "last_message"
        if account_dir not in {"asc", "desc"}:
            account_dir = "desc"

        accounts = catalog.list_accounts(platform=platform, search=q)
        accounts = _sort_account_summaries(accounts, sort_key=sort_accounts, direction=account_dir)
        recent_conversations = catalog.list_conversations(platform=platform, search=q, limit=18)
        context = {
            "request": request,
            "page_title": "Account Archives",
            "site_nav": _site_nav(request, "accounts"),
            "active_section": "accounts",
            "accounts": accounts,
            "recent_conversations": recent_conversations[:8],
            "platforms": catalog.available_account_platforms(),
            "selected_platform": platform or "",
            "search_query": q or "",
            "selected_account_sort": sort_accounts,
            "selected_account_dir": account_dir,
            "account_sort_options": [
                ("last_message", "Latest conversation"),
                ("name", "Name"),
                ("conversations", "Conversation count"),
                ("messages", "Message count"),
                ("attachments", "Attachment count"),
            ],
            "stats": {
                "accounts": len(accounts),
                "conversations": sum(account["archive_counts"].get("conversations", 0) for account in accounts),
                "messages": sum(account["archive_counts"].get("messages", 0) for account in accounts),
                "attachments": sum(account["archive_counts"].get("attachments", 0) for account in accounts),
            },
        }
        return templates.TemplateResponse(request, "accounts_dashboard.html", context)

    @app.get("/accounts/{platform}/{username}", response_class=HTMLResponse, name="account_page")
    async def account_page(
        request: Request,
        platform: str,
        username: str,
        tab: str = Query(default="overview"),
    ) -> HTMLResponse:
        if tab not in {"overview", "messages"}:
            tab = "overview"
        account = catalog.get_account(platform, username)
        if not account:
            raise HTTPException(status_code=404, detail="Account archive not found")

        tabs = [
            {
                "key": "overview",
                "label": "Overview",
                "url": _append_query(
                    request.url_for("account_page", platform=platform, username=username),
                    {"tab": "overview"},
                ),
            },
            {
                "key": "messages",
                "label": "Messages",
                "url": _append_query(
                    request.url_for("account_page", platform=platform, username=username),
                    {"tab": "messages"},
                ),
            },
        ]
        context = {
            "request": request,
            "page_title": f"{account['username']} · account archive",
            "site_nav": _site_nav(request, "accounts"),
            "active_section": "accounts",
            "account": account,
            "active_tab": tab,
            "tabs": tabs,
        }
        return templates.TemplateResponse(request, "account.html", context)

    @app.get("/messages", response_class=HTMLResponse, name="messages_dashboard")
    async def messages_dashboard(
        request: Request,
        platform: Optional[str] = Query(default=None),
        q: Optional[str] = Query(default=None),
    ) -> HTMLResponse:
        conversations = catalog.list_conversations(platform=platform, search=q, limit=200)
        context = {
            "request": request,
            "page_title": "Message Inbox",
            "site_nav": _site_nav(request, "messages"),
            "active_section": "messages",
            "conversations": conversations,
            "platforms": catalog.available_account_platforms(),
            "selected_platform": platform or "",
            "search_query": q or "",
            "stats": {
                "conversations": len(conversations),
                "messages": sum(int(conversation.get("message_count") or 0) for conversation in conversations),
                "attachments": sum(int(conversation.get("attachment_count") or 0) for conversation in conversations),
            },
        }
        return templates.TemplateResponse(request, "messages_dashboard.html", context)

    @app.get(
        "/accounts/{platform}/{username}/conversations/{conversation_id}",
        response_class=HTMLResponse,
        name="conversation_page",
    )
    async def conversation_page(
        request: Request,
        platform: str,
        username: str,
        conversation_id: str,
        notice: Optional[str] = Query(default=None),
        error: Optional[str] = Query(default=None),
    ) -> HTMLResponse:
        account, conversation = catalog.get_conversation(platform, username, conversation_id)
        if not account:
            raise HTTPException(status_code=404, detail="Account archive not found")
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")

        primary_profile = (
            conversation.get("primary_profile")
            if isinstance(conversation.get("primary_profile"), dict)
            else {}
        )
        primary_username = str(primary_profile.get("username") or "").strip()
        related_conversations = []
        if primary_username:
            related_conversations = [
                item
                for item in catalog.find_conversations_for_profile(platform, primary_username, limit=12)
                if item.get("conversation_id") != conversation_id
            ]

        context = {
            "request": request,
            "page_title": f"{conversation['display_title']} · messages",
            "site_nav": _site_nav(request, "messages"),
            "active_section": "messages",
            "account": account,
            "conversation": conversation,
            "composer_enabled": platform == "instagram",
            "conversation_sidebar": account.get("conversations", [])[:24],
            "related_conversations": related_conversations,
            "conversation_notice": _conversation_notice_text(notice),
            "conversation_error": _conversation_error_text(error),
            "back_to_account_url": _append_query(
                request.url_for("account_page", platform=platform, username=username),
                {"tab": "messages"},
            ),
        }
        return templates.TemplateResponse(request, "conversation.html", context)

    @app.post(
        "/accounts/{platform}/{username}/conversations/{conversation_id}/send",
        name="conversation_send",
    )
    async def conversation_send(
        request: Request,
        platform: str,
        username: str,
        conversation_id: str,
    ) -> RedirectResponse:
        redirect_url = str(
            request.url_for(
                "conversation_page",
                platform=platform,
                username=username,
                conversation_id=conversation_id,
            )
        )
        payload = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        text = str((payload.get("message_text") or [""])[-1]).strip()
        if not text:
            return RedirectResponse(
                _append_query(redirect_url, {"error": "empty_message"}),
                status_code=status.HTTP_303_SEE_OTHER,
            )

        try:
            await _send_conversation_message(platform, username, conversation_id, text)
        except HTTPException as exc:
            error_key = "unsupported_platform" if exc.status_code == 501 else "send_failed"
            return RedirectResponse(
                _append_query(redirect_url, {"error": error_key}),
                status_code=status.HTTP_303_SEE_OTHER,
            )

        return RedirectResponse(
            _append_query(redirect_url, {"notice": "sent"}),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.get("/profiles/{platform}/{username}", response_class=HTMLResponse, name="profile_page")
    async def profile_page(
        request: Request,
        platform: str,
        username: str,
        tab: str = Query(default="overview"),
        sort: str = Query(default="newest"),
        dir: str = Query(default="desc"),
        media_filter: str = Query(default="all"),
    ) -> HTMLResponse:
        if tab not in VALID_TABS:
            tab = "overview"
        if sort not in MEDIA_SORT_KEYS:
            sort = "newest"
        if dir not in {"asc", "desc"}:
            dir = "desc"
        if media_filter not in MEDIA_FILTER_KEYS:
            media_filter = "all"
        profile = catalog.get_profile(platform, username)
        if not profile:
            raise HTTPException(status_code=404, detail="Profile bundle not found")

        # Inject DB-persisted cross-platform link state into the bundle
        _conn = _get_db()
        try:
            profile["approved_links"] = db_module.get_approved_cross_platform_links(
                _conn, platform, username
            )
            _rejected_keys = db_module.get_rejected_cross_platform_link_keys(
                _conn, platform, username
            )
        finally:
            _conn.close()
        # Filter rejected/already-approved suggestions out of intelligence matches
        intel = profile.get("intelligence") or {}
        linked = intel.get("linked_profiles") or {}
        raw_matches = linked.get("matches") or []
        _approved_keys = {(a["target_platform"], a["target_username"]) for a in profile["approved_links"]}
        linked["matches"] = [
            m for m in raw_matches
            if (m.get("platform"), m.get("username")) not in _rejected_keys
            and (m.get("platform"), m.get("username")) not in _approved_keys
        ]

        profile["external_candidates"] = []  # populated on-demand via POST /cross-platform-search

        media_tabs = {"overview", "posts", "mentions", "reposts", "stories", "highlights"}
        if tab in media_tabs:
            if tab == "overview":
                profile["overview_items"] = _sort_media_items(
                    _filter_media_items(profile["overview_items"], media_filter=media_filter),
                    sort_key=sort,
                    direction=dir,
                )
            else:
                profile["collections"][tab] = _sort_media_items(
                    _filter_media_items(profile["collections"][tab], media_filter=media_filter),
                    sort_key=sort,
                    direction=dir,
                )

        tab_urls = []
        for tab_key, tab_label in [
            ("overview", "Overview"),
            ("posts", "Posts"),
            ("mentions", "Mentions"),
            ("reposts", "Reposts"),
            ("stories", "Stories"),
            ("highlights", "Highlights"),
            ("connections", "Connections"),
            ("deep_profile", "Deep Profile"),
        ]:
            query = {"tab": tab_key}
            if tab_key in media_tabs:
                query.update({"sort": sort, "dir": dir, "media_filter": media_filter})
            tab_urls.append(
                {
                    "key": tab_key,
                    "label": tab_label,
                    "url": _append_query(
                        request.url_for("profile_page", platform=profile["platform"], username=profile["username"]),
                        query,
                    ),
                }
            )

        item_query_string = _query_string(
            {
                "tab": tab,
                "sort": sort if tab in media_tabs else None,
                "dir": dir if tab in media_tabs else None,
                "media_filter": media_filter if tab in media_tabs else None,
            }
        )

        context = {
            "request": request,
            "page_title": f"{profile['display_name']} · {profile['platform']}",
            "site_nav": _site_nav(request, "profiles"),
            "active_section": "profiles",
            "profile": profile,
            "related_conversations": catalog.find_conversations_for_profile(platform, username, limit=8),
            "linked_account_url": (
                str(request.url_for("account_page", platform=profile["platform"], username=profile["username"]))
                if profile.get("is_own_account")
                else None
            ),
            "active_tab": tab,
            "tabs": tab_urls,
            "selected_sort": sort,
            "selected_dir": dir,
            "selected_media_filter": media_filter,
            "media_sort_options": [
                ("newest", "Newest first"),
                ("oldest", "Oldest first"),
                ("likes", "Most likes"),
                ("comments", "Most comments"),
                ("views", "Most views"),
                ("vibe", "Highest vibe"),
                ("files", "Most files"),
                ("title", "Title A-Z"),
            ],
            "media_filter_options": [
                ("all", "All media"),
                ("with_caption", "With caption"),
                ("with_comments", "With comments"),
                ("photos_only", "Photos only"),
                ("videos_only", "Videos only"),
                ("with_audio", "Video with audio"),
                ("multi_file", "Multi-file items"),
            ],
            "item_query_string": item_query_string,
        }
        return templates.TemplateResponse(request, "profile.html", context)

    @app.get(
        "/api/profiles/{platform}/{username}/deep-profile",
        response_class=JSONResponse,
        name="api_profile_deep_profile",
    )
    async def api_profile_deep_profile(platform: str, username: str) -> JSONResponse:
        profile = catalog.get_profile(platform, username)
        if not profile:
            raise HTTPException(status_code=404, detail="Profile bundle not found")
        deep_profile = profile.get("deep_profile") if isinstance(profile.get("deep_profile"), dict) else {}
        return JSONResponse(deep_profile)

    @app.get(
        "/profiles/{platform}/{username}/items/{kind}/{item_id}",
        response_class=HTMLResponse,
        name="item_detail",
    )
    async def item_detail(
        request: Request,
        platform: str,
        username: str,
        kind: str,
        item_id: str,
        tab: Optional[str] = Query(default=None),
        sort: Optional[str] = Query(default=None),
        dir: Optional[str] = Query(default=None),
        media_filter: Optional[str] = Query(default=None),
    ) -> HTMLResponse:
        if kind not in {"posts", "mentions", "reposts", "stories", "highlights"}:
            raise HTTPException(status_code=404, detail="Unknown item kind")
        profile, item = catalog.get_item_detail(platform, username, kind, item_id)
        if not profile:
            raise HTTPException(status_code=404, detail="Profile bundle not found")
        if not item:
            raise HTTPException(status_code=404, detail="Archived item not found")

        if tab not in VALID_TABS:
            tab = kind
        if sort not in MEDIA_SORT_KEYS:
            sort = None
        if dir not in {"asc", "desc"}:
            dir = None
        if media_filter not in MEDIA_FILTER_KEYS:
            media_filter = None
        back_to_url = _append_query(
            request.url_for("profile_page", platform=profile["platform"], username=profile["username"]),
            {
                "tab": tab,
                "sort": sort,
                "dir": dir,
                "media_filter": media_filter,
            },
        )

        context = {
            "request": request,
            "page_title": f"{profile['username']} · {item['item_id']}",
            "site_nav": _site_nav(request, "profiles"),
            "active_section": "profiles",
            "profile": profile,
            "item": item,
            "back_to_url": back_to_url,
        }
        return templates.TemplateResponse(request, "item_detail.html", context)

    @app.get("/api/accounts", response_class=JSONResponse, name="api_accounts")
    async def api_accounts(
        platform: Optional[str] = Query(default=None),
        q: Optional[str] = Query(default=None),
        sort_accounts: str = Query(default="last_message"),
        account_dir: str = Query(default="desc"),
    ) -> JSONResponse:
        if sort_accounts not in ACCOUNT_SORT_KEYS:
            sort_accounts = "last_message"
        if account_dir not in {"asc", "desc"}:
            account_dir = "desc"
        accounts = catalog.list_accounts(platform=platform, search=q)
        accounts = _sort_account_summaries(accounts, sort_key=sort_accounts, direction=account_dir)
        return JSONResponse(
            {
                "accounts": accounts,
                "platforms": catalog.available_account_platforms(),
            }
        )

    @app.get(
        "/api/accounts/{platform}/{username}",
        response_class=JSONResponse,
        name="api_account_detail",
    )
    async def api_account_detail(platform: str, username: str) -> JSONResponse:
        account = catalog.get_account(platform, username)
        if not account:
            raise HTTPException(status_code=404, detail="Account archive not found")
        return JSONResponse(account)

    @app.get(
        "/api/accounts/{platform}/{username}/conversations/{conversation_id}",
        response_class=JSONResponse,
        name="api_conversation_detail",
    )
    async def api_conversation_detail(
        platform: str,
        username: str,
        conversation_id: str,
    ) -> JSONResponse:
        account, conversation = catalog.get_conversation(platform, username, conversation_id)
        if not account:
            raise HTTPException(status_code=404, detail="Account archive not found")
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return JSONResponse(conversation)

    @app.post(
        "/api/accounts/{platform}/{username}/conversations/{conversation_id}/send",
        response_class=JSONResponse,
        name="api_conversation_send",
    )
    async def api_conversation_send(
        platform: str,
        username: str,
        conversation_id: str,
        payload: dict[str, Any] = Body(default_factory=dict),
    ) -> JSONResponse:
        text = str(payload.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="Message text is required")
        result = await _send_conversation_message(platform, username, conversation_id, text)
        return JSONResponse({"status": "ok", **result})

    @app.get(
        "/api/profiles/{platform}/{username}",
        response_class=JSONResponse,
        name="api_profile_detail",
    )
    async def api_profile_detail(platform: str, username: str) -> JSONResponse:
        profile = catalog.get_profile(platform, username)
        if not profile:
            raise HTTPException(status_code=404, detail="Profile bundle not found")
        return JSONResponse(profile)

    @app.get(
        "/api/profiles/{platform}/{username}/graph",
        response_class=JSONResponse,
        name="api_profile_graph",
    )
    async def api_profile_graph(platform: str, username: str) -> JSONResponse:
        profile = catalog.get_profile(platform, username)
        if not profile:
            raise HTTPException(status_code=404, detail="Profile bundle not found")
        graph = profile.get("graph") if isinstance(profile.get("graph"), dict) else {}
        raw_graph = graph.get("raw") if isinstance(graph.get("raw"), dict) else {}
        return JSONResponse(raw_graph)

    # ── Cross-platform link endpoints ────────────────────────────────────────

    @app.post(
        "/api/profiles/{platform}/{username}/cross-platform-search",
        response_class=JSONResponse,
        name="api_cross_platform_search",
    )
    async def api_cross_platform_search(
        platform: str,
        username: str,
        payload: dict[str, Any] = Body(default_factory=dict),
    ) -> JSONResponse:
        """
        Live cross-platform search: navigates to each other platform with an
        authenticated browser and searches for real accounts matching display_name.
        """
        display_name = str(payload.get("display_name") or "").strip()
        if not display_name:
            raise HTTPException(status_code=400, detail="display_name is required")

        from src.platform_search import run_cross_platform_search

        try:
            results = await run_cross_platform_search(
                project_root=PROJECT_ROOT,
                source_platform=platform,
                source_username=username,
                display_name=display_name,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        # Filter out accounts already approved or rejected
        conn = _get_db()
        try:
            _rejected = db_module.get_rejected_cross_platform_link_keys(conn, platform, username)
            _approved = {
                (a["target_platform"], a["target_username"])
                for a in db_module.get_approved_cross_platform_links(conn, platform, username)
            }
        finally:
            conn.close()

        filtered = [
            r for r in results
            if (r["platform"], r["username"]) not in _rejected
            and (r["platform"], r["username"]) not in _approved
        ]
        return JSONResponse({"results": filtered})

    @app.get(
        "/api/profiles/{platform}/{username}/cross-platform-links",
        response_class=JSONResponse,
        name="api_cross_platform_links",
    )
    async def api_cross_platform_links(platform: str, username: str) -> JSONResponse:
        conn = _get_db()
        try:
            approved = db_module.get_approved_cross_platform_links(conn, platform, username)
            pending = db_module.get_cross_platform_links(conn, platform, username)
            pending = [r for r in pending if r["status"] == "pending"]
        finally:
            conn.close()
        return JSONResponse({"approved": approved, "pending": pending})

    @app.post(
        "/api/profiles/{platform}/{username}/cross-platform-links/approve",
        response_class=JSONResponse,
        name="api_cross_platform_links_approve",
    )
    async def api_cross_platform_links_approve(
        platform: str,
        username: str,
        payload: dict[str, Any] = Body(default_factory=dict),
    ) -> JSONResponse:
        target_platform = str(payload.get("target_platform") or "").strip()
        target_username = str(payload.get("target_username") or "").strip()
        if not target_platform or not target_username:
            raise HTTPException(status_code=400, detail="target_platform and target_username required")
        confidence = payload.get("confidence")
        reasons = payload.get("reasons") if isinstance(payload.get("reasons"), list) else None
        evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else None

        conn = _get_db()
        try:
            link_id = db_module.approve_cross_platform_link(
                conn, platform, username, target_platform, target_username,
                confidence=float(confidence) if confidence is not None else None,
                reasons=reasons,
                evidence=evidence,
            )
            # Auto-add target to tracking list if not already there
            existing = conn.execute(
                "SELECT id FROM targets WHERE platform=? AND username=?",
                (target_platform, target_username),
            ).fetchone()
            added_to_tracking = False
            if not existing:
                db_module.upsert_target(conn, target_platform, target_username)
                added_to_tracking = True
        finally:
            conn.close()
        return JSONResponse({"status": "approved", "id": link_id, "added_to_tracking": added_to_tracking})

    @app.post(
        "/api/profiles/{platform}/{username}/cross-platform-links/reject",
        response_class=JSONResponse,
        name="api_cross_platform_links_reject",
    )
    async def api_cross_platform_links_reject(
        platform: str,
        username: str,
        payload: dict[str, Any] = Body(default_factory=dict),
    ) -> JSONResponse:
        target_platform = str(payload.get("target_platform") or "").strip()
        target_username = str(payload.get("target_username") or "").strip()
        if not target_platform or not target_username:
            raise HTTPException(status_code=400, detail="target_platform and target_username required")

        conn = _get_db()
        try:
            link_id = db_module.reject_cross_platform_link(
                conn, platform, username, target_platform, target_username,
            )
        finally:
            conn.close()
        return JSONResponse({"status": "rejected", "id": link_id})

    return app


def _install_template_helpers(templates: Jinja2Templates) -> None:
    env = templates.env
    env.filters["datetime"] = format_datetime
    env.filters["human_number"] = format_number
    env.filters["filesize"] = format_bytes
    env.filters["pretty_json"] = pretty_json
    env.filters["nl2br"] = nl2br
    env.filters["instagram_links"] = instagram_links
    env.filters["tojson"] = lambda v: json.dumps(v, ensure_ascii=False, separators=(",", ":")).replace("</", r"<\/")
    env.globals["now_utc"] = lambda: datetime.now(timezone.utc)
    env.globals["instagram_profile_url"] = instagram_profile_url
    env.globals["platform_profile_url"] = platform_profile_url
    env.globals["asset_version"] = asset_version


def format_datetime(value: Any) -> str:
    if isinstance(value, Undefined):
        return "—"
    if not isinstance(value, str) or not value:
        return "—"
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def format_number(value: Any) -> str:
    if isinstance(value, Undefined):
        return "—"
    if value in (None, ""):
        return "—"
    try:
        number = int(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:,}"


def format_bytes(value: Any) -> str:
    if isinstance(value, Undefined):
        return "—"
    if value in (None, ""):
        return "—"
    try:
        size = float(value)
    except (TypeError, ValueError):
        return str(value)
    units = ["B", "KB", "MB", "GB", "TB"]
    unit = 0
    while size >= 1024 and unit < len(units) - 1:
        size /= 1024
        unit += 1
    return f"{size:.1f} {units[unit]}"


def pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def nl2br(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.replace("\n", "<br>")


def instagram_profile_url(username: Any) -> str:
    if not isinstance(username, str) or not username.strip():
        return "https://www.instagram.com/"
    clean = username.strip().lstrip("@")
    return f"https://www.instagram.com/{clean}/"


def platform_profile_url(platform: Any, username: Any) -> str:
    if not isinstance(username, str) or not username.strip():
        return "https://www.instagram.com/"
    shared = _shared_platform_profile_url(str(platform or ""), username)
    return shared or instagram_profile_url(username)


def instagram_links(value: Any) -> Markup:
    if not isinstance(value, str):
        return Markup("")

    escaped_text = str(escape(value))

    def _replace(match: re.Match[str]) -> str:
        username = match.group(1)
        return (
            f'<a class="inline-link" href="{instagram_profile_url(username)}" '
            f'target="_blank" rel="noreferrer">@{username}</a>'
        )

    linked = INSTAGRAM_USERNAME_RE.sub(_replace, escaped_text).replace("\n", "<br>")
    return Markup(linked)


def asset_version(name: str) -> int:
    """Return a simple static asset version from the file mtime."""
    path = STATIC_DIR / name
    try:
        return int(path.stat().st_mtime)
    except OSError:
        return 0


def _query_string(params: dict[str, Any]) -> str:
    clean = {
        key: value
        for key, value in params.items()
        if value not in (None, "", [])
    }
    return urlencode(clean, doseq=True)


def _append_query(base_url: str, params: dict[str, Any]) -> str:
    query = _query_string(params)
    return f"{base_url}?{query}" if query else base_url


def _site_nav(request: Request, active_section: str) -> list[dict[str, str | bool]]:
    return [
        {
            "key": "profiles",
            "label": "Profiles",
            "url": str(request.url_for("dashboard")),
            "active": active_section == "profiles",
        },
        {
            "key": "accounts",
            "label": "Accounts",
            "url": str(request.url_for("accounts_dashboard")),
            "active": active_section == "accounts",
        },
        {
            "key": "messages",
            "label": "Messages",
            "url": str(request.url_for("messages_dashboard")),
            "active": active_section == "messages",
        },
    ]


def _conversation_notice_text(value: Any) -> str | None:
    if value == "sent":
        return "Message sent. The conversation archive was refreshed right after."
    return None


def _conversation_error_text(value: Any) -> str | None:
    if value == "empty_message":
        return "Type a message before sending."
    if value == "unsupported_platform":
        return "Sending from the viewer is currently available for Instagram threads only."
    if value == "send_failed":
        return "The message could not be sent from the current browser session."
    return None


def _profile_summary_count(profile: dict[str, Any], key: str) -> int:
    raw_count = profile.get("counts", {}).get(key, 0)
    if key == "followers":
        raw_count = raw_count or profile.get("bio_follower_count") or 0
    if key == "following":
        raw_count = raw_count or profile.get("bio_following_count") or 0
    try:
        return int(raw_count or 0)
    except (TypeError, ValueError):
        return 0


def _filter_profile_summaries(
    profiles: list[dict[str, Any]],
    *,
    scope: str,
    content: str,
) -> list[dict[str, Any]]:
    filtered = profiles
    if scope == "own":
        filtered = [profile for profile in filtered if profile.get("is_own_account")]
    elif scope == "tracked":
        filtered = [profile for profile in filtered if not profile.get("is_own_account")]

    if content == "with_any":
        filtered = [profile for profile in filtered if any(_profile_summary_count(profile, key) for key in ("posts", "mentions", "reposts", "stories", "highlights"))]
    elif content == "with_posts":
        filtered = [profile for profile in filtered if _profile_summary_count(profile, "posts") > 0]
    elif content == "with_mentions":
        filtered = [profile for profile in filtered if _profile_summary_count(profile, "mentions") > 0]
    elif content == "with_stories":
        filtered = [profile for profile in filtered if _profile_summary_count(profile, "stories") > 0]
    elif content == "with_highlights":
        filtered = [profile for profile in filtered if _profile_summary_count(profile, "highlights") > 0]
    return filtered


def _sort_profile_summaries(
    profiles: list[dict[str, Any]],
    *,
    sort_key: str,
    direction: str,
) -> list[dict[str, Any]]:
    reverse = direction == "desc"

    def key(profile: dict[str, Any]) -> Any:
        if sort_key == "followers":
            return _profile_summary_count(profile, "followers")
        if sort_key == "following":
            return _profile_summary_count(profile, "following")
        if sort_key == "archived_posts":
            return _profile_summary_count(profile, "posts")
        if sort_key == "last_activity":
            return format_datetime_sort_key(profile.get("last_activity_at"))
        if sort_key == "last_export":
            return format_datetime_sort_key(profile.get("generated_at"))
        if sort_key == "platform":
            return (str(profile.get("platform") or "").lower(), str(profile.get("display_name") or "").lower())
        return (str(profile.get("display_name") or "").lower(), str(profile.get("username") or "").lower())

    return sorted(profiles, key=key, reverse=reverse)


def _sort_account_summaries(
    accounts: list[dict[str, Any]],
    *,
    sort_key: str,
    direction: str,
) -> list[dict[str, Any]]:
    reverse = direction == "desc"

    def key(account: dict[str, Any]) -> Any:
        archive_counts = account.get("archive_counts") or {}
        if sort_key == "conversations":
            return int(archive_counts.get("conversations") or 0)
        if sort_key == "messages":
            return int(archive_counts.get("messages") or 0)
        if sort_key == "attachments":
            return int(archive_counts.get("attachments") or 0)
        if sort_key == "last_message":
            return format_datetime_sort_key(account.get("last_message_at"))
        return (str(account.get("platform") or "").lower(), str(account.get("username") or "").lower())

    return sorted(accounts, key=key, reverse=reverse)


def _filter_media_items(
    items: list[dict[str, Any]],
    *,
    media_filter: str,
) -> list[dict[str, Any]]:
    if media_filter == "all":
        return list(items)

    def has_photo(item: dict[str, Any]) -> bool:
        return any(asset.get("media_type") == "photo" for asset in item.get("media_assets") or [])

    def has_video(item: dict[str, Any]) -> bool:
        return any(asset.get("media_type") == "video" for asset in item.get("media_assets") or [])

    def has_audio(item: dict[str, Any]) -> bool:
        return any(asset.get("media_type") == "video" and asset.get("has_audio") for asset in item.get("media_assets") or [])

    if media_filter == "with_caption":
        return [item for item in items if item.get("caption")]
    if media_filter == "with_comments":
        return [item for item in items if (item.get("metrics") or {}).get("comments") not in (None, 0)]
    if media_filter == "photos_only":
        return [item for item in items if has_photo(item) and not has_video(item)]
    if media_filter == "videos_only":
        return [item for item in items if has_video(item)]
    if media_filter == "with_audio":
        return [item for item in items if has_audio(item)]
    if media_filter == "multi_file":
        return [item for item in items if len(item.get("media_assets") or []) > 1]
    return list(items)


def _sort_media_items(
    items: list[dict[str, Any]],
    *,
    sort_key: str,
    direction: str,
) -> list[dict[str, Any]]:
    reverse = direction == "desc"

    def key(item: dict[str, Any]) -> Any:
        metrics = item.get("metrics") or {}
        if sort_key == "likes":
            return int(metrics.get("likes") or 0)
        if sort_key == "comments":
            return int(metrics.get("comments") or 0)
        if sort_key == "views":
            return int(metrics.get("views") or 0)
        if sort_key == "vibe":
            value = item.get("vibe_score")
            return float(value) if isinstance(value, (int, float)) else float("-inf")
        if sort_key == "files":
            return len(item.get("media_assets") or [])
        if sort_key == "title":
            return str(item.get("title") or "").lower()
        timestamp = item.get("posted_at") or item.get("discovered_at")
        return format_datetime_sort_key(timestamp)

    if sort_key == "oldest":
        return sorted(items, key=lambda item: format_datetime_sort_key(item.get("posted_at") or item.get("discovered_at")))
    return sorted(items, key=key, reverse=reverse)


def format_datetime_sort_key(value: Any) -> float:
    if not isinstance(value, str) or not value:
        return float("-inf")
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return float("-inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).timestamp()


app = create_app()
