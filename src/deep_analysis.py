"""Safe local deep-media analysis for one archived target using Ollama."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import yaml
from dotenv import load_dotenv
from loguru import logger

try:
    from PIL import Image
except ImportError:  # pragma: no cover - Pillow is available in the host env
    Image = None

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.telegram_bot import build_bot, send_deep_profile_summary
from src.utils import extract_video_frames as _utils_extract_frames
from src.utils import parse_iso_timestamp as _parse_iso_timestamp_util
from src.utils import probe_video_duration
from src.vector_store import chroma_backend_available, upsert_target_profile_document

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = PROJECT_ROOT.parent.parent
DEFAULT_PLATFORM = "instagram"
DEFAULT_USERNAME = "k.dvorakovaa"
DEFAULT_MODEL = "gemma4:latest"
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"
PROMPT_VERSION = "deep_media_safe_v4"
SCHEMA_VERSION = 1
MAX_OLLAMA_IMAGE_EDGE = 1280
CHECKPOINT_INTERVAL = 5
VIDEO_FRAME_COUNT = 5
ITEM_ARTIFACT_FILENAME = "analysis.json"
ITEM_REPORT_FILENAME = "analysis.md"
MARKDOWN_REPORT_FILENAME = "deep_analysis.md"
ENTITY_GRAPH_FILENAME = "deep_entities_graph.json"
SUPPORTED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
SUPPORTED_VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".mkv", ".webm"}
SOURCE_KIND_LABELS = {
    "posts": "posts",
    "mentions": "mentions",
    "reposts": "reposts",
    "stories": "stories",
    "story_highlights": "highlights",
}
EXCLUDED_INFERENCES = [
    "mental health or emotional diagnosis",
    "financial status or wealth tier",
    "biometric guesses such as age or gender",
    "relationship status",
    "private identity traits",
]
SAFE_SCHEMA = {
    "type": "object",
    "properties": {
        "visual_summary": {"type": "string"},
        "activities": {"type": "array", "items": {"type": "string"}},
        "visible_objects": {"type": "array", "items": {"type": "string"}},
        "visible_brands_or_logos": {"type": "array", "items": {"type": "string"}},
        "fashion_and_style_signals": {"type": "array", "items": {"type": "string"}},
        "explicit_location_signals": {"type": "array", "items": {"type": "string"}},
        "location_guesses": {"type": "array", "items": {"type": "string"}},
        "hobby_signals": {"type": "array", "items": {"type": "string"}},
        "recognizable_public_figures_or_performers": {"type": "array", "items": {"type": "string"}},
        "luxury_or_premium_indicators": {"type": "array", "items": {"type": "string"}},
        "social_context_visible": {
            "type": "object",
            "properties": {
                "category": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["category", "notes"],
        },
        "recurring_themes": {"type": "array", "items": {"type": "string"}},
        "media_tone": {"type": "array", "items": {"type": "string"}},
        "confidence_notes": {"type": "string"},
    },
    "required": [
        "visual_summary",
        "activities",
        "visible_objects",
        "visible_brands_or_logos",
        "fashion_and_style_signals",
        "explicit_location_signals",
        "location_guesses",
        "hobby_signals",
        "recognizable_public_figures_or_performers",
        "luxury_or_premium_indicators",
        "social_context_visible",
        "recurring_themes",
        "media_tone",
        "confidence_notes",
    ],
}


@dataclass(frozen=True)
class RuntimeConfig:
    project_root: Path
    workspace_root: Path
    data_dir: Path
    media_root: Path
    profile_dir: Path
    report_dir: Path
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_general_thread_id: int | None
    platform: str = DEFAULT_PLATFORM
    username: str = DEFAULT_USERNAME


def _report_path(runtime: RuntimeConfig) -> Path:
    return runtime.profile_dir / MARKDOWN_REPORT_FILENAME


def _entity_graph_path(runtime: RuntimeConfig) -> Path:
    return runtime.profile_dir / ENTITY_GRAPH_FILENAME


def _legacy_workspace_report_path(username: str = DEFAULT_USERNAME) -> Path:
    return WORKSPACE_ROOT / "profiles" / f"{username}.md"


def _remove_legacy_workspace_report(username: str = DEFAULT_USERNAME) -> None:
    legacy_path = _legacy_workspace_report_path(username)
    try:
        if legacy_path.exists():
            legacy_path.unlink()
    except Exception:
        logger.debug(f"[deep_analysis] Unable to remove legacy report path: {legacy_path}")


def _item_artifact_path(item_dir: Path) -> Path:
    return item_dir / ITEM_ARTIFACT_FILENAME


def _item_report_path(item_dir: Path) -> Path:
    return item_dir / ITEM_REPORT_FILENAME


def _env_path(project_root: Path) -> Path:
    env_local = project_root / ".env.local"
    if env_local.exists():
        return env_local
    return project_root / ".env"


def _resolve_config_relative_path(value: str | Path, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def _clean_env_value(value: str | None) -> str:
    if value is None:
        return ""
    cleaned = value.strip()
    if not cleaned or cleaned.startswith("#"):
        return ""
    return cleaned


def _load_optional_int_env(*env_names: str) -> int | None:
    for env_name in env_names:
        raw = _clean_env_value(os.environ.get(env_name))
        if not raw:
            continue
        try:
            return int(raw)
        except ValueError:
            logger.warning(f"[deep_analysis] Ignoring invalid integer env var {env_name}={raw!r}")
    return None


def _load_runtime_settings(project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    load_dotenv(_env_path(project_root), override=False)
    settings_path = project_root / "settings.yaml"
    config_dir = settings_path.parent
    raw = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
    app_raw = raw.get("app") or {}
    data_dir = _resolve_config_relative_path(
        os.environ.get("DATA_DIR", app_raw.get("data_dir", "./data")),
        config_dir,
    )
    return {
        "project_root": project_root,
        "workspace_root": WORKSPACE_ROOT,
        "data_dir": data_dir,
        "telegram_bot_token": _clean_env_value(os.environ.get("TELEGRAM_BOT_TOKEN")),
        "telegram_chat_id": _clean_env_value(os.environ.get("TELEGRAM_CHAT_ID")),
        "telegram_general_thread_id": _load_optional_int_env(
            "TELEGRAM_GENERAL_THREAD_ID",
            "TELEGRAM_STARTUP_THREAD_ID",
            "TELEGRAM_DIGEST_THREAD_ID",
        ),
    }


def _load_runtime(
    project_root: Path = PROJECT_ROOT,
    *,
    platform: str = DEFAULT_PLATFORM,
    username: str = DEFAULT_USERNAME,
) -> RuntimeConfig:
    settings = _load_runtime_settings(project_root)
    data_dir = settings["data_dir"]
    media_root = data_dir / "media" / platform / username
    profile_dir = data_dir / "profiles" / platform / username
    profile_dir.mkdir(parents=True, exist_ok=True)
    return RuntimeConfig(
        project_root=settings["project_root"],
        workspace_root=settings["workspace_root"],
        data_dir=data_dir,
        media_root=media_root,
        profile_dir=profile_dir,
        report_dir=profile_dir,
        telegram_bot_token=settings["telegram_bot_token"],
        telegram_chat_id=settings["telegram_chat_id"],
        telegram_general_thread_id=settings["telegram_general_thread_id"],
        platform=platform,
        username=username,
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_json_load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _read_text_if_exists(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
    except Exception:
        return None
    return text or None


def _stable_id(*parts: object) -> str:
    raw = "||".join(str(part or "") for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _short_text(value: str | None, limit: int = 1_600) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    collapsed = re.sub(r"\s+", " ", text)
    return collapsed[:limit]


def _extract_handles_from_text(value: str | None) -> list[str]:
    if not value:
        return []
    seen: set[str] = set()
    handles: list[str] = []
    for match in re.findall(r"@([A-Za-z0-9._]+)", value):
        normalized = f"@{match.strip()}"
        key = normalized.casefold()
        if not match or key in seen:
            continue
        seen.add(key)
        handles.append(normalized)
    return handles


def _parse_datetime(value: str | None) -> datetime | None:
    return _parse_iso_timestamp_util(value)


def _format_date_range(items: list[dict[str, Any]]) -> dict[str, str | None]:
    timestamps = [
        _parse_datetime(item.get("posted_at") or item.get("discovered_at") or item.get("analyzed_at"))
        for item in items
    ]
    timestamps = [item for item in timestamps if item is not None]
    if not timestamps:
        return {"earliest": None, "latest": None}
    return {
        "earliest": min(timestamps).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "latest": max(timestamps).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _display_source_kind(raw_kind: str) -> str:
    return SOURCE_KIND_LABELS.get(raw_kind, raw_kind or "unknown")


def _normalise_term(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    return text.strip(" -,:;")


def _term_counter(items: list[dict[str, Any]], field: str, limit: int = 8) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    display: dict[str, str] = {}
    for item in items:
        analysis = item.get("analysis") if isinstance(item.get("analysis"), dict) else {}
        values = analysis.get(field)
        if not isinstance(values, list):
            continue
        seen: set[str] = set()
        for raw in values:
            term = _normalise_term(raw)
            if not term:
                continue
            key = term.casefold()
            if key in seen:
                continue
            seen.add(key)
            counter[key] += 1
            display.setdefault(key, term)
    return [
        {"label": display[key], "count": count}
        for key, count in counter.most_common(limit)
    ]


def _social_context_counter(items: list[dict[str, Any]], limit: int = 6) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    for item in items:
        analysis = item.get("analysis") if isinstance(item.get("analysis"), dict) else {}
        context = analysis.get("social_context_visible")
        if not isinstance(context, dict):
            continue
        category = _normalise_term(context.get("category"))
        if category:
            counter[category] += 1
    return [{"label": key, "count": value} for key, value in counter.most_common(limit)]


def _source_breakdown(items: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    for item in items:
        counter[_display_source_kind(str(item.get("source_kind") or "unknown"))] += 1
    return [{"label": key, "count": value} for key, value in counter.most_common(limit)]


def _item_list_counter(items: list[dict[str, Any]], field: str, limit: int = 8) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    display: dict[str, str] = {}
    for item in items:
        values = item.get(field)
        if not isinstance(values, list):
            continue
        seen: set[str] = set()
        for raw in values:
            term = _normalise_term(raw)
            if not term:
                continue
            key = term.casefold()
            if key in seen:
                continue
            seen.add(key)
            counter[key] += 1
            display.setdefault(key, term)
    return [{"label": display[key], "count": count} for key, count in counter.most_common(limit)]


def _music_counter(items: list[dict[str, Any]], field: str, limit: int = 8) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    display: dict[str, str] = {}
    for item in items:
        tracks = item.get("music_tracks")
        if not isinstance(tracks, list):
            continue
        seen: set[str] = set()
        for track in tracks:
            if not isinstance(track, dict):
                continue
            term = _normalise_term(track.get(field))
            if not term:
                continue
            key = term.casefold()
            if key in seen:
                continue
            seen.add(key)
            counter[key] += 1
            display.setdefault(key, term)
    return [{"label": display[key], "count": count} for key, count in counter.most_common(limit)]


def _safe_analysis_prompt(entry: dict[str, Any]) -> str:
    music_tracks = entry.get("music_tracks") if isinstance(entry.get("music_tracks"), list) else []
    explicit_accounts = entry.get("explicit_account_mentions") if isinstance(entry.get("explicit_account_mentions"), list) else []
    sidecar_locations = entry.get("sidecar_locations") if isinstance(entry.get("sidecar_locations"), list) else []
    context_lines = [
        "Analyze this archived social-media media item and return structured JSON.",
        "Use only what is directly visible in the image or explicitly stated in the provided archive text.",
        "Do not infer or guess sensitive traits or private facts.",
        "If the source item contains multiple media files, treat the text as shared post-level context only.",
        "Do not assume every caption/detail applies to this specific image or frame unless it is visibly supported here.",
        "Focus on concrete visible hobbies, style, travel context, social setting, and attached music metadata when present.",
        "You may name a clearly identifiable public performer, act, team, or public figure only when strongly supported by visible stage visuals, signage, logos, or explicit archive metadata.",
        "Do not identify private individuals or perform face recognition.",
        "Do not estimate the person's wealth. You may note only visible luxury or premium context indicators.",
        "For places, provide a cautious best-effort location guess only when strong visual or archive cues support it.",
        "",
        "Forbidden inferences:",
        "- mental health or emotional diagnosis",
        "- wealth, income, or class tier",
        "- age or gender guesses",
        "- relationship status",
        "- religion, ethnicity, politics, sexuality, or health",
        "",
        "Return JSON with exactly these keys:",
        "- visual_summary: short visible summary",
        "- activities: short list of visible activities",
        "- visible_objects: short list of notable visible objects",
        "- visible_brands_or_logos: list only when a brand or logo is clearly visible",
        "- fashion_and_style_signals: visible clothing, styling, or aesthetic cues only",
        "- explicit_location_signals: visible landmarks, signage, text, or location labels only",
        "- location_guesses: cautious best-effort place guesses only when strong cues support them",
        "- hobby_signals: concrete hobbies, sports, or recurring interests supported by the media",
        "- recognizable_public_figures_or_performers: public figures or stage acts only when clearly supported",
        "- luxury_or_premium_indicators: concrete visible indicators of premium travel, venues, products, or settings",
        "- social_context_visible: object with category and notes",
        "- recurring_themes: short list of broad visible themes",
        "- media_tone: short list describing the presentation style",
        "- confidence_notes: mention ambiguity or low-confidence areas",
        "",
        "Archive context:",
        f"- platform: {entry.get('platform')}",
        f"- target_username: {entry.get('target_username')}",
        f"- source_kind: {entry.get('source_kind')}",
        f"- source_item_id: {entry.get('source_item_id')}",
        f"- source_media_type: {entry.get('source_media_type')}",
        f"- analysis_media_type: {entry.get('analysis_media_type')}",
        f"- source_media_count: {entry.get('source_media_count') or 1}",
        f"- text_context_scope: {entry.get('text_context_scope') or 'item_specific'}",
        f"- frame_index: {entry.get('frame_index') if entry.get('frame_index') is not None else 'n/a'}",
        f"- frame_offset_s: {entry.get('frame_offset_s') if entry.get('frame_offset_s') is not None else 'n/a'}",
        f"- posted_at: {entry.get('posted_at') or 'unknown'}",
        f"- collection_name: {entry.get('collection_name') or 'n/a'}",
        f"- source_relative_path: {entry.get('source_relative_path')}",
        f"- public text context: {_short_text(entry.get('text_context'), limit=1_200) or 'none'}",
        f"- attached music tracks: {json.dumps(music_tracks, ensure_ascii=False) if music_tracks else 'none'}",
        f"- explicit account mentions: {', '.join(explicit_accounts) if explicit_accounts else 'none'}",
        f"- explicit location metadata: {', '.join(sidecar_locations) if sidecar_locations else 'none'}",
        f"- video motion classification: {entry.get('video_motion_classification') or 'n/a'}",
        "",
        "If something is unclear, use an empty list, 'unclear', or a cautious note.",
    ]
    return "\n".join(context_lines)


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        pass

    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _normalise_analysis_payload(raw: dict[str, Any]) -> dict[str, Any]:
    def _list_field(name: str) -> list[str]:
        values = raw.get(name)
        if not isinstance(values, list):
            return []
        normalized = []
        seen: set[str] = set()
        for value in values:
            item = _normalise_term(value)
            if not item:
                continue
            key = item.casefold()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(item)
        return normalized[:10]

    social_context = raw.get("social_context_visible")
    if not isinstance(social_context, dict):
        social_context = {}

    category = _normalise_term(social_context.get("category")) or "unclear"
    notes = _short_text(social_context.get("notes"), limit=280)

    return {
        "visual_summary": _short_text(raw.get("visual_summary"), limit=320),
        "activities": _list_field("activities"),
        "visible_objects": _list_field("visible_objects"),
        "visible_brands_or_logos": _list_field("visible_brands_or_logos"),
        "fashion_and_style_signals": _list_field("fashion_and_style_signals"),
        "explicit_location_signals": _list_field("explicit_location_signals"),
        "location_guesses": _list_field("location_guesses"),
        "hobby_signals": _list_field("hobby_signals"),
        "recognizable_public_figures_or_performers": _list_field("recognizable_public_figures_or_performers"),
        "luxury_or_premium_indicators": _list_field("luxury_or_premium_indicators"),
        "social_context_visible": {
            "category": category,
            "notes": notes,
        },
        "recurring_themes": _list_field("recurring_themes"),
        "media_tone": _list_field("media_tone"),
        "confidence_notes": _short_text(raw.get("confidence_notes"), limit=280),
    }


def _prepare_image_bytes_for_ollama(
    image_bytes: bytes,
    *,
    max_edge: int = MAX_OLLAMA_IMAGE_EDGE,
) -> bytes:
    if not image_bytes or Image is None:
        return image_bytes

    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            image.load()
            width, height = image.size
            needs_resize = max(width, height) > max_edge
            processed = image
            if needs_resize:
                resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
                processed = image.copy()
                processed.thumbnail((max_edge, max_edge), resample)
            if processed.mode not in {"RGB", "L"}:
                processed = processed.convert("RGB")
            elif processed.mode == "L":
                processed = processed.convert("RGB")

            buffer = io.BytesIO()
            processed.save(buffer, format="JPEG", quality=85, optimize=True)
            prepared = buffer.getvalue()
            if needs_resize or len(prepared) < len(image_bytes):
                return prepared
    except Exception:
        return image_bytes

    return image_bytes


def _ollama_chat(
    *,
    base_url: str,
    model: str,
    prompt: str,
    image_bytes: bytes,
    timeout_s: int = 240,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "stream": False,
        "format": SAFE_SCHEMA,
        "options": {
            "temperature": 0.2,
        },
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a careful archive analyst. "
                    "Return JSON only. Do not make sensitive personal inferences."
                ),
            },
            {
                "role": "user",
                "content": prompt,
                "images": [base64.b64encode(image_bytes).decode("ascii")],
            },
        ],
    }
    response = requests.post(
        f"{base_url.rstrip('/')}/api/chat",
        json=payload,
        timeout=(10, timeout_s),
    )
    response.raise_for_status()
    body = response.json()
    message = body.get("message") if isinstance(body, dict) else {}
    content = message.get("content") if isinstance(message, dict) else ""
    return _extract_json_object(str(content or ""))


def _load_profile_index(profile_dir: Path) -> dict[str, dict[str, Any]]:
    media_index: dict[str, dict[str, Any]] = {}
    for filename, item_key, id_key in (
        ("posts.json", "items", "post_id"),
        ("reposts.json", "items", "post_id"),
        ("stories.json", "items", "story_id"),
    ):
        payload = _safe_json_load(profile_dir / filename)
        items = payload.get(item_key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            media_paths = item.get("media_paths")
            if not isinstance(media_paths, list):
                continue
            for relative_path in media_paths:
                if not isinstance(relative_path, str):
                    continue
                media_index[relative_path] = {
                    "source_item_id": item.get(id_key),
                    "posted_at": item.get("posted_at"),
                    "discovered_at": item.get("discovered_at"),
                    "caption": item.get("caption"),
                    "post_type": item.get("post_type"),
                    "source_media_count": len(media_paths),
                    "collection_name": item.get("collection_name"),
                    "content_scope": item.get("content_scope"),
                    "source_url": item.get("url"),
                }
    return media_index


def _sidecar_payloads_for_path(path: Path) -> tuple[dict[str, Any], str | None]:
    story_json = _safe_json_load(path.parent / "story.json")
    post_json = _safe_json_load(path.parent / "post.json")
    sidecar = story_json or post_json
    text_blob = _read_text_if_exists(path.parent / "caption.txt") or _read_text_if_exists(path.parent / "story_text.txt")
    return sidecar, text_blob


def _text_context_from_sidecars(
    *,
    bundle_meta: dict[str, Any],
    sidecar: dict[str, Any],
    text_blob: str | None,
) -> str:
    parts: list[str] = []
    for value in (
        bundle_meta.get("caption"),
        text_blob,
        sidecar.get("caption"),
        sidecar.get("story_text"),
        sidecar.get("story_caption"),
        sidecar.get("collection_name"),
        bundle_meta.get("collection_name"),
        bundle_meta.get("source_url"),
        sidecar.get("url"),
    ):
        snippet = _short_text(value, limit=600)
        if snippet and snippet not in parts:
            parts.append(snippet)

    metadata = sidecar.get("metadata")
    if isinstance(metadata, dict):
        for key in ("caption", "story_text", "text", "accessibility_caption", "link_url", "location_name"):
            snippet = _short_text(metadata.get(key), limit=400)
            if snippet and snippet not in parts:
                parts.append(snippet)

    return "\n".join(parts[:6])


def _extract_music_tracks(sidecar: dict[str, Any]) -> list[dict[str, str]]:
    metadata = sidecar.get("metadata") if isinstance(sidecar.get("metadata"), dict) else {}
    raw_tracks = metadata.get("music")
    if not isinstance(raw_tracks, list):
        return []

    tracks: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw_tracks:
        if not isinstance(item, dict):
            continue
        title = _short_text(item.get("title"), limit=140)
        artist = _short_text(item.get("artist"), limit=140)
        key = (title.casefold(), artist.casefold())
        if key in seen or (not title and not artist):
            continue
        seen.add(key)
        tracks.append({"title": title, "artist": artist})
    return tracks


def _extract_sidecar_locations(sidecar: dict[str, Any]) -> list[str]:
    metadata = sidecar.get("metadata") if isinstance(sidecar.get("metadata"), dict) else {}
    values: list[str] = []
    raw_locations = metadata.get("locations")
    if isinstance(raw_locations, list):
        for item in raw_locations:
            if isinstance(item, dict):
                for key in ("name", "title", "short_name", "address"):
                    snippet = _short_text(item.get(key), limit=160)
                    if snippet and snippet not in values:
                        values.append(snippet)
            elif isinstance(item, str):
                snippet = _short_text(item, limit=160)
                if snippet and snippet not in values:
                    values.append(snippet)

    for key in ("location_name", "venue_name"):
        snippet = _short_text(metadata.get(key), limit=160)
        if snippet and snippet not in values:
            values.append(snippet)
    return values[:8]


def _extract_explicit_account_mentions(
    *,
    caption: str | None,
    sidecar: dict[str, Any],
    target_username: str = DEFAULT_USERNAME,
) -> list[str]:
    mentions: list[str] = []
    seen: set[str] = set()

    for handle in _extract_handles_from_text(caption):
        key = handle.casefold()
        if key not in seen:
            seen.add(key)
            mentions.append(handle)

    metadata = sidecar.get("metadata") if isinstance(sidecar.get("metadata"), dict) else {}
    raw_mentions = metadata.get("mentions")
    if isinstance(raw_mentions, list):
        for item in raw_mentions:
            if isinstance(item, dict):
                handle = _short_text(item.get("username"), limit=80)
                if handle and not handle.startswith("@"):
                    handle = f"@{handle}"
            else:
                handle = _short_text(item, limit=80)
            if not handle:
                continue
            if not handle.startswith("@"):
                handle = f"@{handle}"
            key = handle.casefold()
            if key in seen:
                continue
            seen.add(key)
            mentions.append(handle)

    owner = metadata.get("owner") if isinstance(metadata.get("owner"), dict) else {}
    owner_username = _short_text(owner.get("username"), limit=80)
    if owner_username and owner_username.casefold() != target_username.casefold():
        handle = owner_username if owner_username.startswith("@") else f"@{owner_username}"
        key = handle.casefold()
        if key not in seen:
            mentions.append(handle)

    return mentions[:12]


def _media_has_audio(sidecar: dict[str, Any]) -> bool:
    saved_media = sidecar.get("saved_media")
    if isinstance(saved_media, list):
        for item in saved_media:
            if isinstance(item, dict) and item.get("has_audio") is True:
                return True
    return False


def _scan_media_files(
    media_root: Path,
    profile_index: dict[str, dict[str, Any]],
    *,
    platform: str = DEFAULT_PLATFORM,
    target_username: str = DEFAULT_USERNAME,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    data_root = media_root.parents[2]
    for path in sorted(media_root.rglob("*")):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix not in SUPPORTED_IMAGE_SUFFIXES | SUPPORTED_VIDEO_SUFFIXES:
            continue

        relative_path = Path("media") / path.relative_to(data_root / "media")
        relative_path_str = str(relative_path)
        parts = relative_path.parts
        bundle_meta = profile_index.get(relative_path_str, {})
        sidecar, text_blob = _sidecar_payloads_for_path(path)
        source_kind = parts[3] if len(parts) > 3 else "unknown"
        source_media_count = bundle_meta.get("source_media_count")
        if not isinstance(source_media_count, int) or source_media_count < 1:
            sidecar_media_paths = sidecar.get("media_paths")
            if isinstance(sidecar_media_paths, list) and sidecar_media_paths:
                source_media_count = len([item for item in sidecar_media_paths if isinstance(item, str)])
            else:
                source_media_count = 1
        text_context_scope = "shared_post_context" if source_media_count > 1 else "item_specific"
        source_item_id = (
            bundle_meta.get("source_item_id")
            or (parts[-2] if len(parts) >= 7 else path.stem)
        )
        source_media_type = "video" if suffix in SUPPORTED_VIDEO_SUFFIXES else "image"
        entries.append(
            {
                "platform": platform,
                "target_username": target_username,
                "source_kind": source_kind,
                "source_item_id": source_item_id,
                "source_relative_path": relative_path_str,
                "source_media_type": source_media_type,
                "post_type": bundle_meta.get("post_type") or sidecar.get("post_type"),
                "source_media_count": source_media_count,
                "text_context_scope": text_context_scope,
                "posted_at": bundle_meta.get("posted_at"),
                "discovered_at": bundle_meta.get("discovered_at"),
                "collection_name": bundle_meta.get("collection_name"),
                "content_scope": bundle_meta.get("content_scope"),
                "source_url": bundle_meta.get("source_url"),
                "caption": bundle_meta.get("caption"),
                "text_context": _text_context_from_sidecars(
                    bundle_meta=bundle_meta,
                    sidecar=sidecar,
                    text_blob=text_blob,
                ),
                "music_tracks": _extract_music_tracks(sidecar),
                "explicit_account_mentions": _extract_explicit_account_mentions(
                    caption=bundle_meta.get("caption") or sidecar.get("caption") or text_blob,
                    sidecar=sidecar,
                    target_username=target_username,
                ),
                "sidecar_locations": _extract_sidecar_locations(sidecar),
                "has_audio": _media_has_audio(sidecar),
                "source_sha256": _sha256_file(path),
                "item_dir": path.parent,
                "absolute_path": path,
                "sidecar": sidecar,
            }
        )
    return entries


def _iter_nested_string_fields(value: Any, prefix: str = "") -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            next_prefix = f"{prefix}.{key}" if prefix else str(key)
            values.extend(_iter_nested_string_fields(nested, next_prefix))
        return values
    if isinstance(value, list):
        for index, nested in enumerate(value):
            next_prefix = f"{prefix}[{index}]"
            values.extend(_iter_nested_string_fields(nested, next_prefix))
        return values
    if isinstance(value, str):
        return [(prefix, value)]
    return values


def _candidate_mention_paths_from_sidecar(
    sidecar: dict[str, Any],
    *,
    valid_paths: set[str],
) -> list[str]:
    if not isinstance(sidecar, dict) or not valid_paths:
        return []

    candidates: list[str] = []
    seen: set[str] = set()
    for field_path, raw_value in _iter_nested_string_fields(sidecar):
        if not any(token in field_path.casefold() for token in ("tag", "mention")):
            continue
        candidate = raw_value.strip()
        if candidate in valid_paths and candidate not in seen:
            seen.add(candidate)
            candidates.append(candidate)
    return candidates


def _select_mention_entries(media_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in media_entries:
        if entry.get("source_kind") != "mentions":
            continue
        group_key = str(entry.get("source_item_id") or entry.get("source_relative_path") or "")
        grouped.setdefault(group_key, []).append(entry)

    decisions: dict[str, tuple[str, str | None]] = {}
    for group_key, group in grouped.items():
        if len(group) == 1:
            decisions[group_key] = (str(group[0]["source_relative_path"]), "single_media_mention")
            continue

        valid_paths = {
            str(entry.get("source_relative_path") or "")
            for entry in group
            if entry.get("source_relative_path")
        }
        sidecar = group[0].get("sidecar") if isinstance(group[0].get("sidecar"), dict) else {}
        ordered_paths = [
            path
            for path in (sidecar.get("media_paths") if isinstance(sidecar.get("media_paths"), list) else [])
            if isinstance(path, str) and path in valid_paths
        ]
        if not ordered_paths:
            ordered_paths = sorted(valid_paths)

        candidate_paths = _candidate_mention_paths_from_sidecar(
            sidecar,
            valid_paths=valid_paths,
        )
        if len(candidate_paths) == 1:
            decisions[group_key] = (candidate_paths[0], "sidecar_tagged_media_path")
        else:
            decisions[group_key] = (
                ordered_paths[0],
                "fallback_first_media_no_tag_coordinates",
            )

    selected_entries: list[dict[str, Any]] = []
    emitted_mentions: set[str] = set()
    for entry in media_entries:
        if entry.get("source_kind") != "mentions":
            selected_entries.append({**entry, "mention_selection_reason": None})
            continue

        group_key = str(entry.get("source_item_id") or entry.get("source_relative_path") or "")
        decision = decisions.get(group_key)
        if not decision:
            continue
        selected_path, reason = decision
        if group_key in emitted_mentions:
            continue
        if str(entry.get("source_relative_path") or "") != selected_path:
            continue
        emitted_mentions.add(group_key)
        selected_entries.append({**entry, "mention_selection_reason": reason})

    return selected_entries


def _prepared_entry_count_for_media_entries(media_entries: list[dict[str, Any]]) -> int:
    return sum(
        VIDEO_FRAME_COUNT if item.get("source_media_type") == "video" else 1
        for item in media_entries
    )


def _group_sort_key(group: dict[str, Any]) -> tuple[float, str]:
    representative = group.get("representative_entry") if isinstance(group.get("representative_entry"), dict) else {}
    parsed = _parse_datetime(
        representative.get("posted_at")
        or representative.get("discovered_at")
    )
    timestamp = parsed.timestamp() if parsed else float("-inf")
    return (timestamp, str(group.get("item_relative_dir") or ""))


def _build_item_groups(
    all_media_entries: list[dict[str, Any]],
    selected_media_entries: list[dict[str, Any]],
    *,
    data_dir: Path,
) -> list[dict[str, Any]]:
    archive_by_dir: dict[Path, list[dict[str, Any]]] = {}
    for entry in all_media_entries:
        item_dir = entry.get("item_dir")
        if isinstance(item_dir, Path):
            archive_by_dir.setdefault(item_dir, []).append(entry)

    selected_by_dir: dict[Path, list[dict[str, Any]]] = {}
    for entry in selected_media_entries:
        item_dir = entry.get("item_dir")
        if isinstance(item_dir, Path):
            selected_by_dir.setdefault(item_dir, []).append(entry)

    groups: list[dict[str, Any]] = []
    for item_dir, selected_entries in selected_by_dir.items():
        archive_entries = archive_by_dir.get(item_dir, [])
        representative_entry = selected_entries[0]
        groups.append(
            {
                "item_dir": item_dir,
                "item_relative_dir": str(item_dir.relative_to(data_dir)),
                "representative_entry": representative_entry,
                "archive_media_entries": sorted(
                    archive_entries,
                    key=lambda item: str(item.get("source_relative_path") or ""),
                ),
                "selected_media_entries": sorted(
                    selected_entries,
                    key=lambda item: str(item.get("source_relative_path") or ""),
                ),
                "prepared_entry_count": _prepared_entry_count_for_media_entries(selected_entries),
            }
        )

    groups.sort(key=_group_sort_key)
    return groups


def _selected_source_hashes(media_entries: list[dict[str, Any]]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for entry in media_entries:
        relative_path = str(entry.get("source_relative_path") or "")
        if not relative_path:
            continue
        hashes[relative_path] = str(entry.get("source_sha256") or "")
    return hashes


def _load_item_artifact(item_dir: Path) -> dict[str, Any]:
    payload = _safe_json_load(_item_artifact_path(item_dir))
    if payload.get("analysis_type") != "safe_deep_media_item":
        return {}
    return payload


def _item_artifact_matches_current_sources(
    payload: dict[str, Any],
    item_group: dict[str, Any],
) -> bool:
    if not payload:
        return False
    if payload.get("prompt_version") != PROMPT_VERSION:
        return False
    expected_hashes = _selected_source_hashes(item_group.get("selected_media_entries") or [])
    payload_hashes = payload.get("selected_source_hashes")
    if not isinstance(payload_hashes, dict):
        return False
    normalized_hashes = {
        str(path): str(value or "")
        for path, value in payload_hashes.items()
        if path
    }
    if normalized_hashes != expected_hashes:
        return False
    if str(payload.get("item_relative_dir") or "") != str(item_group.get("item_relative_dir") or ""):
        return False
    return True


def _item_artifact_is_complete_and_current(
    payload: dict[str, Any],
    item_group: dict[str, Any],
) -> bool:
    if not _item_artifact_matches_current_sources(payload, item_group):
        return False
    if payload.get("is_partial_run"):
        return False
    items = payload.get("items")
    if not isinstance(items, list):
        return False
    prepared_entry_count = int(payload.get("prepared_entry_count") or 0)
    if prepared_entry_count and len(items) < prepared_entry_count:
        return False
    return True


# Alias — delegates to src.utils.probe_video_duration
_probe_video_duration = probe_video_duration


def _frame_signature(image_bytes: bytes) -> list[int] | None:
    if not image_bytes or Image is None:
        return None
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            image = image.convert("RGB").resize((16, 16))
            pixels = list(image.getdata())
    except Exception:
        return None

    if not pixels:
        return None
    flattened: list[int] = []
    for red, green, blue in pixels:
        flattened.extend([red, green, blue])
    return flattened


def _hamming_distance(left: list[int] | None, right: list[int] | None) -> float:
    if left is None or right is None:
        return 255.0
    if len(left) != len(right) or not left:
        return 255.0
    return sum(abs(a - b) for a, b in zip(left, right)) / len(left)


def _classify_video_motion(frames: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    if len(frames) <= 1:
        return "single_frame_video", frames[:1]

    signatures = [_frame_signature(frame.get("image_bytes", b"")) for frame in frames]
    distances = [
        _hamming_distance(signatures[index], signatures[index + 1])
        for index in range(len(signatures) - 1)
    ]
    max_distance = max(distances) if distances else 0
    if max_distance <= 3.0:
        first_frame = dict(frames[0])
        first_frame["frame_index"] = 1
        return "static_video_single_frame", [first_frame]
    return "dynamic_video_multi_frame", frames


def _extract_video_frames(video_path: Path, frame_count: int = VIDEO_FRAME_COUNT) -> list[dict[str, Any]]:
    """
    Extract frame_count evenly-spaced frames from video_path.
    Returns list of dicts with frame_index, frame_offset_s, and image_bytes keys.
    Delegates extraction to src.utils.extract_video_frames; wraps bytes into dict shape.
    """
    duration_s = probe_video_duration(video_path)
    offsets = [
        duration_s * (0.05 + (0.9 * index / max(frame_count - 1, 1)))
        for index in range(frame_count)
    ]
    raw_frames = _utils_extract_frames(video_path, n=frame_count)
    candidate_frames = [
        {
            "frame_index": index + 1,
            "frame_offset_s": round(offsets[index], 3),
            "image_bytes": frame_bytes,
        }
        for index, frame_bytes in enumerate(raw_frames)
    ]
    motion_classification, selected_frames = _classify_video_motion(candidate_frames)
    for frame in selected_frames:
        frame["video_motion_classification"] = motion_classification
    return selected_frames


def _expand_analysis_entries(media_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for media_entry in media_entries:
        absolute_path = media_entry["absolute_path"]
        source_sha256 = str(media_entry.get("source_sha256") or _sha256_file(absolute_path))
        if media_entry["source_media_type"] == "image":
            expanded.append(
                {
                    **media_entry,
                    "entry_id": _stable_id("image", media_entry["source_relative_path"]),
                    "analysis_media_type": "image",
                    "frame_index": None,
                    "frame_offset_s": None,
                    "video_motion_classification": None,
                    "source_sha256": source_sha256,
                    # Store path instead of bytes — loaded lazily in the analysis loop
                    # so only one image occupies memory at a time.
                    "_image_path": absolute_path,
                }
            )
            continue

        for frame in _extract_video_frames(absolute_path, frame_count=VIDEO_FRAME_COUNT):
            expanded.append(
                {
                    **media_entry,
                    "entry_id": _stable_id(
                        "video_frame",
                        media_entry["source_relative_path"],
                        frame["frame_index"],
                    ),
                    "analysis_media_type": "video_frame",
                    "frame_index": frame["frame_index"],
                    "frame_offset_s": frame["frame_offset_s"],
                    "video_motion_classification": frame.get("video_motion_classification"),
                    "source_sha256": source_sha256,
                    "image_bytes": frame["image_bytes"],
                }
            )
    expanded.sort(
        key=lambda item: (
            str(item.get("source_relative_path") or ""),
            int(item.get("frame_index") or 0),
        )
    )
    return expanded


def _reuse_cached_entry(
    *,
    existing_by_id: dict[str, dict[str, Any]],
    entry: dict[str, Any],
    model: str,
    force: bool,
) -> dict[str, Any] | None:
    if force:
        return None
    cached = existing_by_id.get(entry["entry_id"])
    if not cached:
        return None
    if cached.get("prompt_version") != PROMPT_VERSION:
        return None
    if cached.get("model") != model:
        return None
    if cached.get("source_sha256") != entry.get("source_sha256"):
        return None
    return cached


def _build_analyzed_entry(
    entry: dict[str, Any],
    *,
    model: str,
    normalized_analysis: dict[str, Any],
    item_group: dict[str, Any],
) -> dict[str, Any]:
    item_dir = item_group["item_dir"]
    return {
        "entry_id": entry["entry_id"],
        "platform": entry["platform"],
        "target_username": entry["target_username"],
        "source_kind": entry["source_kind"],
        "source_item_id": entry["source_item_id"],
        "source_relative_path": entry["source_relative_path"],
        "source_media_type": entry["source_media_type"],
        "analysis_media_type": entry["analysis_media_type"],
        "post_type": entry.get("post_type"),
        "source_media_count": entry.get("source_media_count") or 1,
        "text_context_scope": entry.get("text_context_scope") or "item_specific",
        "mention_selection_reason": entry.get("mention_selection_reason"),
        "posted_at": entry.get("posted_at"),
        "discovered_at": entry.get("discovered_at"),
        "collection_name": entry.get("collection_name"),
        "content_scope": entry.get("content_scope"),
        "source_url": entry.get("source_url"),
        "caption": _short_text(entry.get("caption"), limit=500),
        "text_context": _short_text(entry.get("text_context"), limit=1_600),
        "frame_index": entry.get("frame_index"),
        "frame_offset_s": entry.get("frame_offset_s"),
        "video_motion_classification": entry.get("video_motion_classification"),
        "music_tracks": entry.get("music_tracks") if isinstance(entry.get("music_tracks"), list) else [],
        "explicit_account_mentions": entry.get("explicit_account_mentions") if isinstance(entry.get("explicit_account_mentions"), list) else [],
        "sidecar_locations": entry.get("sidecar_locations") if isinstance(entry.get("sidecar_locations"), list) else [],
        "has_audio": bool(entry.get("has_audio")),
        "source_sha256": entry.get("source_sha256"),
        "item_relative_dir": str(item_group.get("item_relative_dir") or ""),
        "item_artifact_relative_path": str(_item_artifact_path(item_dir).relative_to(PROJECT_ROOT)),
        "item_report_relative_path": str(_item_report_path(item_dir).relative_to(PROJECT_ROOT)),
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "analyzed_at": _now_iso(),
        "analysis": normalized_analysis,
    }


def _summarise_items(
    items: list[dict[str, Any]],
    selected_media_entries: list[dict[str, Any]],
    *,
    archive_media_file_count: int,
) -> dict[str, Any]:
    date_range = _format_date_range(items)
    unique_videos = {
        item["source_relative_path"]
        for item in items
        if item.get("source_media_type") == "video"
    }
    travel_related_items = sum(
        1
        for item in items
        if (
            (item.get("analysis") or {}).get("location_guesses")
            or (item.get("analysis") or {}).get("explicit_location_signals")
        )
    )
    music_attached_items = sum(1 for item in items if item.get("music_tracks"))
    premium_context_items = sum(
        1
        for item in items
        if (item.get("analysis") or {}).get("luxury_or_premium_indicators")
    )
    static_video_entries = sum(
        1 for item in items if item.get("video_motion_classification") == "static_video_single_frame"
    )
    dynamic_video_entries = sum(
        1 for item in items if item.get("video_motion_classification") == "dynamic_video_multi_frame"
    )
    overview_parts = [
        f"Analyzed {len(items)} visual entries from {len(selected_media_entries)} selected media files.",
    ]
    if archive_media_file_count != len(selected_media_entries):
        overview_parts.append(
            f"The archive scan covered {archive_media_file_count} files before collapsing multi-item mention posts to one selected media file each."
        )
    if date_range["earliest"] and date_range["latest"]:
        overview_parts.append(
            f"Archive coverage spans {date_range['earliest'][:10]} to {date_range['latest'][:10]}."
        )

    activities = _term_counter(items, "activities", limit=4)
    themes = _term_counter(items, "recurring_themes", limit=4)
    if activities:
        overview_parts.append(
            "Most common visible activities: " + ", ".join(entry["label"] for entry in activities[:4]) + "."
        )
    if themes:
        overview_parts.append(
            "Recurring visual themes: " + ", ".join(entry["label"] for entry in themes[:4]) + "."
        )
    if music_attached_items:
        overview_parts.append(f"Attached music metadata appeared in {music_attached_items} analyzed entries.")
    if travel_related_items:
        overview_parts.append(f"Travel or place-related cues appeared in {travel_related_items} analyzed entries.")

    return {
        "overview": " ".join(overview_parts),
        "scanned_media_files": archive_media_file_count,
        "selected_source_media_files": len(selected_media_entries),
        "source_images": sum(1 for item in selected_media_entries if item.get("source_media_type") == "image"),
        "source_videos": sum(1 for item in selected_media_entries if item.get("source_media_type") == "video"),
        "analyzed_entries": len(items),
        "video_frames_analyzed": sum(1 for item in items if item.get("analysis_media_type") == "video_frame"),
        "unique_videos_represented": len(unique_videos),
        "travel_related_items": travel_related_items,
        "music_attached_items": music_attached_items,
        "premium_context_items": premium_context_items,
        "static_video_entries": static_video_entries,
        "dynamic_video_entries": dynamic_video_entries,
        "date_range": date_range,
        "source_breakdown": _source_breakdown(items),
        "top_activities": activities,
        "top_hobbies": _term_counter(items, "hobby_signals"),
        "top_objects": _term_counter(items, "visible_objects"),
        "top_brands_or_logos": _term_counter(items, "visible_brands_or_logos"),
        "top_style_signals": _term_counter(items, "fashion_and_style_signals"),
        "top_location_signals": _term_counter(items, "explicit_location_signals"),
        "top_location_guesses": _term_counter(items, "location_guesses"),
        "top_public_figures": _term_counter(items, "recognizable_public_figures_or_performers"),
        "top_luxury_or_premium_indicators": _term_counter(items, "luxury_or_premium_indicators"),
        "top_music_artists": _music_counter(items, "artist"),
        "top_music_tracks": _music_counter(items, "title"),
        "top_explicit_accounts": _item_list_counter(items, "explicit_account_mentions"),
        "top_recurring_themes": _term_counter(items, "recurring_themes"),
        "top_media_tones": _term_counter(items, "media_tone"),
        "social_context_breakdown": _social_context_counter(items),
    }


def _sorted_items_for_report(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def _key(item: dict[str, Any]) -> tuple[float, str]:
        parsed = _parse_datetime(item.get("posted_at") or item.get("discovered_at") or item.get("analyzed_at"))
        timestamp = parsed.timestamp() if parsed else float("-inf")
        return (timestamp, str(item.get("source_relative_path") or ""))

    return sorted(items, key=_key, reverse=True)


def _mention_selection_note(reason: str | None) -> str | None:
    if reason == "single_media_mention":
        return "Only one media file was archived for this mention, so that file was analyzed."
    if reason == "sidecar_tagged_media_path":
        return "The archive sidecar identified a single tagged media file, so only that mention asset was analyzed."
    if reason == "fallback_first_media_no_tag_coordinates":
        return "The archive did not preserve per-slide tag coordinates, so the first saved media file for this mention post was selected."
    return None


def _append_ranked_markdown_section(
    lines: list[str],
    title: str,
    values: list[dict[str, Any]],
) -> None:
    if not values:
        return
    lines.extend(["", f"## {title}", ""])
    for value in values:
        lines.append(f"- {value['label']} ({value['count']})")


def _write_item_markdown_report(report_path: Path, payload: dict[str, Any]) -> None:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    items = _sorted_items_for_report(payload.get("items") if isinstance(payload.get("items"), list) else [])
    representative = payload.get("representative_entry") if isinstance(payload.get("representative_entry"), dict) else {}
    shared_caption_items: set[str] = set()

    lines = [
        f"# Safe Item Analysis: `{payload.get('source_item_id')}`",
        "",
        "> This report is limited to visible or explicitly stated content from the local archive.",
        "> It excludes sensitive personal inference such as mental-health, finance, biometrics, and relationship-status guesses.",
        "",
        "## Item Context",
        "",
        f"- Platform: `{payload.get('platform')}`",
        f"- Target username: `{payload.get('username')}`",
        f"- Source kind: `{_display_source_kind(str(payload.get('source_kind') or 'unknown'))}`",
        f"- Item folder: `{payload.get('item_relative_dir')}`",
        f"- Generated at: `{payload.get('generated_at')}`",
        f"- Model: `{payload.get('model')}`",
        f"- Archived media files scanned in this item: `{summary.get('scanned_media_files', 0)}`",
        f"- Selected source media files: `{summary.get('selected_source_media_files', 0)}`",
        f"- Visual entries analyzed: `{summary.get('analyzed_entries', 0)}`",
        f"- Source images: `{summary.get('source_images', 0)}`",
        f"- Source videos: `{summary.get('source_videos', 0)}`",
        f"- Video frames analyzed: `{summary.get('video_frames_analyzed', 0)}`",
        f"- Music-tagged entries: `{summary.get('music_attached_items', 0)}`",
        f"- Travel-related entries: `{summary.get('travel_related_items', 0)}`",
        f"- Premium-context entries: `{summary.get('premium_context_items', 0)}`",
    ]
    if representative.get("posted_at") or representative.get("discovered_at"):
        lines.append(
            f"- Posted at: `{representative.get('posted_at') or representative.get('discovered_at')}`"
        )
    if representative.get("source_url"):
        lines.append(f"- Source URL: {representative.get('source_url')}")
    if payload.get("is_partial_run"):
        lines.append(
            f"- Partial run: `{summary.get('analyzed_entries', 0)}` of `{payload.get('prepared_entry_count', 0)}` prepared entries analyzed"
        )

    overview = summary.get("overview")
    if overview:
        lines.extend(["", "## High-Level Summary", "", overview])

    _append_ranked_markdown_section(lines, "Top Activities", summary.get("top_activities") or [])
    _append_ranked_markdown_section(lines, "Top Hobbies", summary.get("top_hobbies") or [])
    _append_ranked_markdown_section(lines, "Visible Objects", summary.get("top_objects") or [])
    _append_ranked_markdown_section(lines, "Visible Brands Or Logos", summary.get("top_brands_or_logos") or [])
    _append_ranked_markdown_section(lines, "Style Signals", summary.get("top_style_signals") or [])
    _append_ranked_markdown_section(lines, "Explicit Location Signals", summary.get("top_location_signals") or [])
    _append_ranked_markdown_section(lines, "Location Guesses", summary.get("top_location_guesses") or [])
    _append_ranked_markdown_section(lines, "Recognizable Public Figures Or Performers", summary.get("top_public_figures") or [])
    _append_ranked_markdown_section(lines, "Music Artists", summary.get("top_music_artists") or [])
    _append_ranked_markdown_section(lines, "Music Tracks", summary.get("top_music_tracks") or [])
    _append_ranked_markdown_section(lines, "Explicit Account Mentions", summary.get("top_explicit_accounts") or [])
    _append_ranked_markdown_section(lines, "Luxury Or Premium Indicators", summary.get("top_luxury_or_premium_indicators") or [])
    _append_ranked_markdown_section(lines, "Recurring Themes", summary.get("top_recurring_themes") or [])
    _append_ranked_markdown_section(lines, "Media Tones", summary.get("top_media_tones") or [])
    _append_ranked_markdown_section(lines, "Social Context Visible", summary.get("social_context_breakdown") or [])

    lines.extend(
        [
            "",
            "## Evidence",
            "",
            f"Structured artifact: `{payload.get('artifact_relative_path')}`",
        ]
    )
    for item in items:
        analysis = item.get("analysis") if isinstance(item.get("analysis"), dict) else {}
        social_context = analysis.get("social_context_visible") if isinstance(analysis.get("social_context_visible"), dict) else {}
        shared_text_context = (
            str(item.get("text_context_scope") or "") == "shared_post_context"
            or int(item.get("source_media_count") or 1) > 1
        )
        source_item_key = str(item.get("source_item_id") or item.get("source_relative_path") or "")
        lines.extend(
            [
                "",
                f"### `{item.get('source_relative_path')}`",
                "",
                f"- Analysis type: `{item.get('analysis_media_type')}`",
            ]
        )
        if item.get("video_motion_classification"):
            lines.append(f"- Video motion classification: `{item.get('video_motion_classification')}`")
        if item.get("frame_index") is not None:
            lines.append(
                f"- Frame slice: `#{item.get('frame_index')}` at `{item.get('frame_offset_s')}s`"
            )
        if item.get("mention_selection_reason"):
            selection_note = _mention_selection_note(str(item.get("mention_selection_reason")))
            if selection_note:
                lines.append(f"- Mention selection: {selection_note}")
        if item.get("caption"):
            if shared_text_context:
                if source_item_key not in shared_caption_items:
                    lines.append(f"- Shared post caption excerpt: {item.get('caption')}")
                    shared_caption_items.add(source_item_key)
            else:
                lines.append(f"- Caption excerpt: {item.get('caption')}")
        if analysis.get("visual_summary"):
            lines.append(f"- Visible summary: {analysis.get('visual_summary')}")
        if item.get("music_tracks"):
            music_values = []
            for track in item.get("music_tracks") or []:
                if not isinstance(track, dict):
                    continue
                title = track.get("title")
                artist = track.get("artist")
                if title and artist:
                    music_values.append(f"{title} — {artist}")
                elif title:
                    music_values.append(str(title))
                elif artist:
                    music_values.append(str(artist))
            if music_values:
                lines.append(f"- Attached music: {', '.join(music_values)}")
        if item.get("explicit_account_mentions"):
            lines.append(f"- Explicit account mentions: {', '.join(item.get('explicit_account_mentions') or [])}")
        if analysis.get("activities"):
            lines.append(f"- Activities: {', '.join(analysis.get('activities') or [])}")
        if analysis.get("hobby_signals"):
            lines.append(f"- Hobbies: {', '.join(analysis.get('hobby_signals') or [])}")
        if analysis.get("visible_brands_or_logos"):
            lines.append(f"- Brands/logos: {', '.join(analysis.get('visible_brands_or_logos') or [])}")
        if analysis.get("explicit_location_signals"):
            lines.append(f"- Explicit location signals: {', '.join(analysis.get('explicit_location_signals') or [])}")
        if analysis.get("location_guesses"):
            lines.append(f"- Location guesses: {', '.join(analysis.get('location_guesses') or [])}")
        if analysis.get("recognizable_public_figures_or_performers"):
            lines.append(
                "- Recognizable public figures or performers: "
                + ", ".join(analysis.get("recognizable_public_figures_or_performers") or [])
            )
        if analysis.get("luxury_or_premium_indicators"):
            lines.append(
                "- Luxury or premium indicators: "
                + ", ".join(analysis.get("luxury_or_premium_indicators") or [])
            )
        if social_context.get("category"):
            lines.append(f"- Social context: {social_context.get('category')}")
        if analysis.get("recurring_themes"):
            lines.append(f"- Themes: {', '.join(analysis.get('recurring_themes') or [])}")
        if analysis.get("media_tone"):
            lines.append(f"- Tone: {', '.join(analysis.get('media_tone') or [])}")
        if analysis.get("confidence_notes"):
            lines.append(f"- Confidence notes: {analysis.get('confidence_notes')}")

    report_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def _write_markdown_report(report_path: Path, payload: dict[str, Any]) -> None:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    items = _sorted_items_for_report(payload.get("items") if isinstance(payload.get("items"), list) else [])
    shared_caption_items: set[str] = set()

    lines = [
        f"# Safe Deep Profile: `{payload.get('username')}`",
        "",
        "> This report is limited to visible or explicitly stated content from the local archive.",
        "> It excludes sensitive personal inference such as mental-health, finance, biometrics, and relationship-status guesses.",
        "",
        "## Archive Coverage",
        "",
        f"- Generated at: `{payload.get('generated_at')}`",
        f"- Model: `{payload.get('model')}`",
        f"- Archived media files scanned: `{summary.get('scanned_media_files', 0)}`",
        f"- Selected source media files: `{summary.get('selected_source_media_files', 0)}`",
        f"- Item folders total: `{summary.get('item_folders_total', 0)}`",
        f"- Item folders with artifacts: `{summary.get('item_folders_with_artifacts', 0)}`",
        f"- Item folders complete: `{summary.get('item_folders_complete', 0)}`",
        f"- Visual entries analyzed: `{summary.get('analyzed_entries', 0)}`",
        f"- Source images: `{summary.get('source_images', 0)}`",
        f"- Source videos: `{summary.get('source_videos', 0)}`",
        f"- Video frames analyzed: `{summary.get('video_frames_analyzed', 0)}`",
        f"- Music-tagged entries: `{summary.get('music_attached_items', 0)}`",
        f"- Travel-related entries: `{summary.get('travel_related_items', 0)}`",
        f"- Premium-context entries: `{summary.get('premium_context_items', 0)}`",
    ]

    date_range = summary.get("date_range") if isinstance(summary.get("date_range"), dict) else {}
    if date_range.get("earliest") and date_range.get("latest"):
        lines.append(
            f"- Coverage window: `{date_range['earliest']}` to `{date_range['latest']}`"
        )

    overview = summary.get("overview")
    if overview:
        lines.extend(["", "## High-Level Summary", "", overview])

    _append_ranked_markdown_section(lines, "Source Breakdown", summary.get("source_breakdown") or [])
    _append_ranked_markdown_section(lines, "Top Activities", summary.get("top_activities") or [])
    _append_ranked_markdown_section(lines, "Top Hobbies", summary.get("top_hobbies") or [])
    _append_ranked_markdown_section(lines, "Visible Objects", summary.get("top_objects") or [])
    _append_ranked_markdown_section(lines, "Visible Brands Or Logos", summary.get("top_brands_or_logos") or [])
    _append_ranked_markdown_section(lines, "Style Signals", summary.get("top_style_signals") or [])
    _append_ranked_markdown_section(lines, "Explicit Location Signals", summary.get("top_location_signals") or [])
    _append_ranked_markdown_section(lines, "Location Guesses", summary.get("top_location_guesses") or [])
    _append_ranked_markdown_section(lines, "Recognizable Public Figures Or Performers", summary.get("top_public_figures") or [])
    _append_ranked_markdown_section(lines, "Music Artists", summary.get("top_music_artists") or [])
    _append_ranked_markdown_section(lines, "Music Tracks", summary.get("top_music_tracks") or [])
    _append_ranked_markdown_section(lines, "Explicit Account Mentions", summary.get("top_explicit_accounts") or [])
    _append_ranked_markdown_section(lines, "Luxury Or Premium Indicators", summary.get("top_luxury_or_premium_indicators") or [])
    _append_ranked_markdown_section(lines, "Recurring Themes", summary.get("top_recurring_themes") or [])
    _append_ranked_markdown_section(lines, "Media Tones", summary.get("top_media_tones") or [])
    _append_ranked_markdown_section(lines, "Social Context Visible", summary.get("social_context_breakdown") or [])

    lines.extend(
        [
            "",
            "## Representative Evidence",
            "",
            f"Full structured artifact: `{payload.get('artifact_relative_path')}`",
            f"Entity graph export: `{payload.get('graph_relative_path')}`",
        ]
    )
    for item in items[:18]:
        analysis = item.get("analysis") if isinstance(item.get("analysis"), dict) else {}
        social_context = analysis.get("social_context_visible") if isinstance(analysis.get("social_context_visible"), dict) else {}
        shared_text_context = (
            str(item.get("text_context_scope") or "") == "shared_post_context"
            or int(item.get("source_media_count") or 1) > 1
        )
        source_item_key = str(item.get("source_item_id") or item.get("source_relative_path") or "")
        lines.extend(
            [
                "",
                f"### `{item.get('source_item_id')}` · `{_display_source_kind(str(item.get('source_kind') or 'unknown'))}`",
                "",
                f"- Source path: `{item.get('source_relative_path')}`",
                f"- Posted at: `{item.get('posted_at') or item.get('discovered_at') or 'unknown'}`",
                f"- Analysis type: `{item.get('analysis_media_type')}`",
            ]
        )
        if item.get("video_motion_classification"):
            lines.append(f"- Video motion classification: `{item.get('video_motion_classification')}`")
        if item.get("frame_index") is not None:
            lines.append(
                f"- Frame slice: `#{item.get('frame_index')}` at `{item.get('frame_offset_s')}s`"
            )
        if item.get("mention_selection_reason"):
            selection_note = _mention_selection_note(str(item.get("mention_selection_reason")))
            if selection_note:
                lines.append(f"- Mention selection: {selection_note}")
        if item.get("caption"):
            if shared_text_context:
                if source_item_key not in shared_caption_items:
                    lines.append(f"- Shared post caption excerpt: {item.get('caption')}")
                    shared_caption_items.add(source_item_key)
            else:
                lines.append(f"- Caption excerpt: {item.get('caption')}")
        if analysis.get("visual_summary"):
            lines.append(f"- Visible summary: {analysis.get('visual_summary')}")
        if item.get("music_tracks"):
            music_values = []
            for track in item.get("music_tracks") or []:
                if not isinstance(track, dict):
                    continue
                title = track.get("title")
                artist = track.get("artist")
                if title and artist:
                    music_values.append(f"{title} — {artist}")
                elif title:
                    music_values.append(str(title))
                elif artist:
                    music_values.append(str(artist))
            if music_values:
                lines.append(f"- Attached music: {', '.join(music_values)}")
        if item.get("explicit_account_mentions"):
            lines.append(f"- Explicit account mentions: {', '.join(item.get('explicit_account_mentions') or [])}")
        if analysis.get("activities"):
            lines.append(f"- Activities: {', '.join(analysis.get('activities') or [])}")
        if analysis.get("hobby_signals"):
            lines.append(f"- Hobbies: {', '.join(analysis.get('hobby_signals') or [])}")
        if analysis.get("visible_brands_or_logos"):
            lines.append(f"- Brands/logos: {', '.join(analysis.get('visible_brands_or_logos') or [])}")
        if analysis.get("explicit_location_signals"):
            lines.append(f"- Explicit location signals: {', '.join(analysis.get('explicit_location_signals') or [])}")
        if analysis.get("location_guesses"):
            lines.append(f"- Location guesses: {', '.join(analysis.get('location_guesses') or [])}")
        if analysis.get("recognizable_public_figures_or_performers"):
            lines.append(
                "- Recognizable public figures or performers: "
                + ", ".join(analysis.get("recognizable_public_figures_or_performers") or [])
            )
        if analysis.get("luxury_or_premium_indicators"):
            lines.append(
                "- Luxury or premium indicators: "
                + ", ".join(analysis.get("luxury_or_premium_indicators") or [])
            )
        if social_context.get("category"):
            lines.append(f"- Social context: {social_context.get('category')}")
        if analysis.get("recurring_themes"):
            lines.append(f"- Themes: {', '.join(analysis.get('recurring_themes') or [])}")
        if analysis.get("media_tone"):
            lines.append(f"- Tone: {', '.join(analysis.get('media_tone') or [])}")
        if analysis.get("confidence_notes"):
            lines.append(f"- Confidence notes: {analysis.get('confidence_notes')}")

    report_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def _document_text_for_item(item: dict[str, Any]) -> str:
    analysis = item.get("analysis") if isinstance(item.get("analysis"), dict) else {}
    social_context = analysis.get("social_context_visible") if isinstance(analysis.get("social_context_visible"), dict) else {}
    mention_selection_note = _mention_selection_note(str(item.get("mention_selection_reason") or "")) if item.get("mention_selection_reason") else None
    sections = [
        f"Target: {item.get('target_username')}",
        f"Platform: {item.get('platform')}",
        f"Source kind: {item.get('source_kind')}",
        f"Source item id: {item.get('source_item_id')}",
        f"Analysis type: {item.get('analysis_media_type')}",
        f"Source path: {item.get('source_relative_path')}",
        f"Source media count: {item.get('source_media_count') or 1}",
        f"Text context scope: {item.get('text_context_scope') or 'item_specific'}",
        f"Video motion classification: {item.get('video_motion_classification') or 'n/a'}",
        f"Posted at: {item.get('posted_at') or item.get('discovered_at') or 'unknown'}",
        "Attached music: " + ", ".join(
            (
                f"{track.get('title')} — {track.get('artist')}"
                if isinstance(track, dict) and track.get("title") and track.get("artist")
                else str(track.get("title") or track.get("artist") or "")
            )
            for track in (item.get("music_tracks") or [])
            if isinstance(track, dict)
        ),
        "Explicit account mentions: " + ", ".join(item.get("explicit_account_mentions") or []),
        f"Text context: {_short_text(item.get('text_context'), 800)}",
        f"Visible summary: {analysis.get('visual_summary') or ''}",
        "Activities: " + ", ".join(analysis.get("activities") or []),
        "Hobbies: " + ", ".join(analysis.get("hobby_signals") or []),
        "Objects: " + ", ".join(analysis.get("visible_objects") or []),
        "Brands/logos: " + ", ".join(analysis.get("visible_brands_or_logos") or []),
        "Style: " + ", ".join(analysis.get("fashion_and_style_signals") or []),
        "Explicit location signals: " + ", ".join(analysis.get("explicit_location_signals") or []),
        "Location guesses: " + ", ".join(analysis.get("location_guesses") or []),
        "Recognizable public figures or performers: " + ", ".join(analysis.get("recognizable_public_figures_or_performers") or []),
        "Luxury or premium indicators: " + ", ".join(analysis.get("luxury_or_premium_indicators") or []),
        "Themes: " + ", ".join(analysis.get("recurring_themes") or []),
        "Media tone: " + ", ".join(analysis.get("media_tone") or []),
        f"Social context category: {social_context.get('category') or 'unclear'}",
        f"Social context notes: {social_context.get('notes') or ''}",
        f"Confidence notes: {analysis.get('confidence_notes') or ''}",
    ]
    if mention_selection_note:
        sections.append(f"Mention selection: {mention_selection_note}")
    return "\n".join(sections)


def _document_text_for_summary(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}

    def _line_for_list(label: str, values: list[dict[str, Any]]) -> str:
        formatted = ", ".join(
            f"{value['label']} ({value['count']})"
            for value in values[:8]
            if isinstance(value, dict) and value.get("label")
        )
        return f"{label}: {formatted}"

    sections = [
        f"Target: {payload.get('username')}",
        f"Platform: {payload.get('platform')}",
        f"Overview: {summary.get('overview') or ''}",
        f"Scanned media files: {summary.get('scanned_media_files', 0)}",
        f"Selected source media files: {summary.get('selected_source_media_files', 0)}",
        f"Item folders total: {summary.get('item_folders_total', 0)}",
        f"Item folders with artifacts: {summary.get('item_folders_with_artifacts', 0)}",
        f"Item folders complete: {summary.get('item_folders_complete', 0)}",
        f"Analyzed entries: {summary.get('analyzed_entries', 0)}",
        f"Source images: {summary.get('source_images', 0)}",
        f"Source videos: {summary.get('source_videos', 0)}",
        f"Video frames analyzed: {summary.get('video_frames_analyzed', 0)}",
        f"Music-tagged entries: {summary.get('music_attached_items', 0)}",
        f"Travel-related entries: {summary.get('travel_related_items', 0)}",
        f"Premium-context entries: {summary.get('premium_context_items', 0)}",
        _line_for_list("Source breakdown", summary.get("source_breakdown") or []),
        _line_for_list("Top activities", summary.get("top_activities") or []),
        _line_for_list("Top hobbies", summary.get("top_hobbies") or []),
        _line_for_list("Top objects", summary.get("top_objects") or []),
        _line_for_list("Top brands/logos", summary.get("top_brands_or_logos") or []),
        _line_for_list("Top style signals", summary.get("top_style_signals") or []),
        _line_for_list("Top location signals", summary.get("top_location_signals") or []),
        _line_for_list("Top location guesses", summary.get("top_location_guesses") or []),
        _line_for_list("Top public figures", summary.get("top_public_figures") or []),
        _line_for_list("Top music artists", summary.get("top_music_artists") or []),
        _line_for_list("Top music tracks", summary.get("top_music_tracks") or []),
        _line_for_list("Top explicit accounts", summary.get("top_explicit_accounts") or []),
        _line_for_list("Top luxury or premium indicators", summary.get("top_luxury_or_premium_indicators") or []),
        _line_for_list("Top recurring themes", summary.get("top_recurring_themes") or []),
        _line_for_list("Top media tones", summary.get("top_media_tones") or []),
        _line_for_list("Social context breakdown", summary.get("social_context_breakdown") or []),
    ]
    return "\n".join(sections)


def _write_artifact(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_entity_graph(
    items: list[dict[str, Any]],
    *,
    platform: str = DEFAULT_PLATFORM,
    username: str = DEFAULT_USERNAME,
) -> dict[str, Any]:
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[tuple[str, str, str], dict[str, Any]] = {}
    target_node_id = f"target:{platform}:{username}"
    nodes[target_node_id] = {
        "id": target_node_id,
        "label": username,
        "type": "target",
    }

    def _ensure_node(node_type: str, label: str) -> str:
        normalized = _normalise_term(label)
        key = f"{node_type}:{normalized.casefold()}"
        if key not in nodes:
            nodes[key] = {"id": key, "label": normalized, "type": node_type}
        return key

    def _increment_edge(to_node_id: str, relation: str, item: dict[str, Any]) -> None:
        edge_key = (target_node_id, to_node_id, relation)
        payload = edges.setdefault(
            edge_key,
            {
                "source": target_node_id,
                "target": to_node_id,
                "relation": relation,
                "count": 0,
                "examples": [],
            },
        )
        payload["count"] += 1
        source_path = str(item.get("source_relative_path") or "")
        if source_path and source_path not in payload["examples"] and len(payload["examples"]) < 5:
            payload["examples"].append(source_path)

    for item in items:
        analysis = item.get("analysis") if isinstance(item.get("analysis"), dict) else {}
        for handle in item.get("explicit_account_mentions") or []:
            node_id = _ensure_node("account", handle)
            _increment_edge(node_id, "mentions_account", item)
        for track in item.get("music_tracks") or []:
            if isinstance(track, dict):
                artist = _normalise_term(track.get("artist"))
                if artist:
                    node_id = _ensure_node("music_artist", artist)
                    _increment_edge(node_id, "attached_music_artist", item)
        for hobby in analysis.get("hobby_signals") or []:
            node_id = _ensure_node("hobby", hobby)
            _increment_edge(node_id, "hobby_signal", item)
        for figure in analysis.get("recognizable_public_figures_or_performers") or []:
            node_id = _ensure_node("public_figure", figure)
            _increment_edge(node_id, "public_figure_signal", item)
        for location in analysis.get("location_guesses") or []:
            node_id = _ensure_node("location_guess", location)
            _increment_edge(node_id, "location_guess", item)

    return {
        "generated_at": _now_iso(),
        "platform": platform,
        "username": username,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "nodes": sorted(nodes.values(), key=lambda item: (item["type"], item["label"].casefold())),
        "edges": sorted(edges.values(), key=lambda item: (-item["count"], item["relation"], item["target"])),
    }


def _build_item_payload(
    *,
    runtime: RuntimeConfig,
    item_group: dict[str, Any],
    model: str,
    base_url: str,
    items: list[dict[str, Any]],
    prepared_entry_count: int | None = None,
) -> dict[str, Any]:
    item_dir = item_group["item_dir"]
    representative = item_group.get("representative_entry") if isinstance(item_group.get("representative_entry"), dict) else {}
    artifact_path = _item_artifact_path(item_dir)
    report_path = _item_report_path(item_dir)
    selected_media_entries = item_group.get("selected_media_entries") or []
    archive_media_entries = item_group.get("archive_media_entries") or []
    prepared_entry_count = int(
        prepared_entry_count
        if prepared_entry_count is not None
        else item_group.get("prepared_entry_count") or _prepared_entry_count_for_media_entries(selected_media_entries)
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "analysis_type": "safe_deep_media_item",
        "generated_at": _now_iso(),
        "platform": runtime.platform,
        "username": runtime.username,
        "source_kind": representative.get("source_kind"),
        "source_item_id": representative.get("source_item_id"),
        "item_relative_dir": str(item_group.get("item_relative_dir") or ""),
        "artifact_relative_path": str(artifact_path.relative_to(runtime.project_root)),
        "report_relative_path": str(report_path.relative_to(runtime.project_root)),
        "model": model,
        "ollama_base_url": base_url,
        "prompt_version": PROMPT_VERSION,
        "selected_source_hashes": _selected_source_hashes(selected_media_entries),
        "selected_source_media_files": [
            {
                "source_relative_path": entry.get("source_relative_path"),
                "source_media_type": entry.get("source_media_type"),
                "mention_selection_reason": entry.get("mention_selection_reason"),
            }
            for entry in selected_media_entries
        ],
        "representative_entry": {
            "source_kind": representative.get("source_kind"),
            "source_item_id": representative.get("source_item_id"),
            "posted_at": representative.get("posted_at"),
            "discovered_at": representative.get("discovered_at"),
            "source_url": representative.get("source_url"),
            "caption": representative.get("caption"),
            "collection_name": representative.get("collection_name"),
        },
        "summary": _summarise_items(
            items,
            selected_media_entries,
            archive_media_file_count=len(archive_media_entries),
        ),
        "items": items,
        "prepared_entry_count": prepared_entry_count,
        "is_partial_run": bool(prepared_entry_count > len(items)),
        "safety": {
            "scope": "Visible or explicitly stated content only.",
            "excluded_inferences": EXCLUDED_INFERENCES,
        },
    }


def _write_item_artifacts(
    *,
    runtime: RuntimeConfig,
    item_group: dict[str, Any],
    model: str,
    base_url: str,
    items: list[dict[str, Any]],
    prepared_entry_count: int | None = None,
) -> dict[str, Any]:
    payload = _build_item_payload(
        runtime=runtime,
        item_group=item_group,
        model=model,
        base_url=base_url,
        items=items,
        prepared_entry_count=prepared_entry_count,
    )
    _write_artifact(_item_artifact_path(item_group["item_dir"]), payload)
    _write_item_markdown_report(_item_report_path(item_group["item_dir"]), payload)
    return payload


def _load_current_item_payloads(
    item_groups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for item_group in item_groups:
        payload = _load_item_artifact(item_group["item_dir"])
        if payload and _item_artifact_matches_current_sources(payload, item_group):
            payloads.append(payload)
    return payloads


def _flatten_items_from_payloads(item_payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for payload in item_payloads:
        payload_items = payload.get("items")
        if not isinstance(payload_items, list):
            continue
        for item in payload_items:
            if isinstance(item, dict):
                items.append(item)
    return items


def _analyze_item_group(
    *,
    runtime: RuntimeConfig,
    item_group: dict[str, Any],
    model: str,
    base_url: str,
    force: bool,
) -> dict[str, Any]:
    item_dir = item_group["item_dir"]
    existing_artifact = _load_item_artifact(item_dir)
    existing_items = existing_artifact.get("items") if isinstance(existing_artifact.get("items"), list) else []
    existing_by_id = {
        item.get("entry_id"): item
        for item in existing_items
        if isinstance(item, dict) and item.get("entry_id")
    }
    expanded_entries = _expand_analysis_entries(item_group["selected_media_entries"])
    actual_prepared_entry_count = len(expanded_entries)
    analyzed_items: list[dict[str, Any]] = []

    logger.info(
        "[deep_analysis] Processing item "
        f"{item_group.get('item_relative_dir')} with {len(expanded_entries)} analysis entries."
    )

    for index, entry in enumerate(expanded_entries, start=1):
        cached = _reuse_cached_entry(
            existing_by_id=existing_by_id,
            entry=entry,
            model=model,
            force=force,
        )
        if cached:
            logger.info(
                "[deep_analysis] Reusing cached entry "
                f"{index}/{len(expanded_entries)} for {entry['source_relative_path']}"
            )
            analyzed_items.append(cached)
        else:
            prompt = _safe_analysis_prompt(entry)
            logger.info(
                "[deep_analysis] Analyzing "
                f"{index}/{len(expanded_entries)} in {item_group.get('item_relative_dir')}: {entry['source_relative_path']}"
                + (
                    f" [frame {entry['frame_index']} @ {entry['frame_offset_s']}s]"
                    if entry.get("frame_index") is not None
                    else ""
                )
            )
            if "_image_path" in entry:
                image_bytes: bytes = Path(entry["_image_path"]).read_bytes()
            else:
                image_bytes = entry["image_bytes"]
            prepared_image_bytes = _prepare_image_bytes_for_ollama(image_bytes)
            raw_analysis = _ollama_chat(
                base_url=base_url,
                model=model,
                prompt=prompt,
                image_bytes=prepared_image_bytes,
            )
            normalized_analysis = _normalise_analysis_payload(raw_analysis)
            analyzed_items.append(
                _build_analyzed_entry(
                    entry,
                    model=model,
                    normalized_analysis=normalized_analysis,
                    item_group=item_group,
                )
            )

        if index == 1 or index % CHECKPOINT_INTERVAL == 0 or index == len(expanded_entries):
            _write_item_artifacts(
                runtime=runtime,
                item_group=item_group,
                model=model,
                base_url=base_url,
                items=analyzed_items,
                prepared_entry_count=actual_prepared_entry_count,
            )
            logger.info(
                "[deep_analysis] Checkpointed item artifact with "
                f"{len(analyzed_items)} analyzed entries for {item_group.get('item_relative_dir')}."
            )

    return _load_item_artifact(item_dir)


def _upsert_chroma_documents(payload: dict[str, Any], data_dir: Path) -> dict[str, Any]:
    document_specs: list[dict[str, Any]] = []
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        content = _document_text_for_item(item)
        if not str(content or "").strip():
            continue
        document_specs.append(
            {
                "platform": str(item.get("platform") or DEFAULT_PLATFORM),
                "target_username": str(item.get("target_username") or DEFAULT_USERNAME),
                "source_kind": "deep_media_analysis",
                "source_id": str(item.get("entry_id") or ""),
                "content": content,
                "metadata": {
                    "media_type": item.get("analysis_media_type"),
                    "source_kind": item.get("source_kind"),
                    "source_relative_path": item.get("source_relative_path"),
                    "source_item_id": item.get("source_item_id"),
                    "text_context_scope": item.get("text_context_scope") or "",
                    "mention_selection_reason": item.get("mention_selection_reason") or "",
                    "video_motion_classification": item.get("video_motion_classification") or "",
                    "music_track_count": len(item.get("music_tracks") or []),
                    "explicit_account_count": len(item.get("explicit_account_mentions") or []),
                    "posted_at": item.get("posted_at") or "",
                    "frame_index": item.get("frame_index") or "",
                    "analyzed_at": item.get("analyzed_at") or "",
                },
            }
        )

    summary_content = _document_text_for_summary(payload)
    if str(summary_content or "").strip():
        document_specs.append(
            {
                "platform": str(payload.get("platform") or DEFAULT_PLATFORM),
                "target_username": str(payload.get("username") or DEFAULT_USERNAME),
                "source_kind": "deep_media_summary",
                "source_id": "deep_analysis_json",
                "content": summary_content,
                "metadata": {
                    "generated_at": payload.get("generated_at") or "",
                    "model": payload.get("model") or "",
                    "artifact_relative_path": payload.get("artifact_relative_path") or "",
                    "graph_relative_path": payload.get("graph_relative_path") or "",
                },
            }
        )

    attempted = len(document_specs)
    if attempted == 0:
        return {"available": chroma_backend_available(), "attempted": 0, "upserted": 0}
    if not chroma_backend_available():
        return {"available": False, "attempted": attempted, "upserted": 0}

    upserted = 0
    for spec in document_specs:
        saved = upsert_target_profile_document(
            platform=spec["platform"],
            target_username=spec["target_username"],
            source_kind=spec["source_kind"],
            source_id=spec["source_id"],
            content=spec["content"],
            metadata=spec["metadata"],
            data_dir=data_dir,
        )
        if saved:
            upserted += 1

    return {"available": True, "attempted": attempted, "upserted": upserted}


async def _send_telegram_summary(
    runtime: RuntimeConfig,
    payload: dict[str, Any],
    *,
    skip_telegram: bool,
) -> None:
    if skip_telegram:
        logger.info("[deep_analysis] Telegram summary skipped by flag.")
        return
    if not runtime.telegram_bot_token or not runtime.telegram_chat_id:
        logger.warning("[deep_analysis] Telegram summary skipped because bot token or chat id is missing.")
        return
    bot = build_bot(runtime.telegram_bot_token)
    await send_deep_profile_summary(
        bot,
        runtime.telegram_chat_id,
        runtime.username,
        payload.get("summary") if isinstance(payload.get("summary"), dict) else {},
        platform=runtime.platform,
        report_path=str(_report_path(runtime)),
        artifact_path=str(runtime.profile_dir / "deep_analysis.json"),
        message_thread_id=runtime.telegram_general_thread_id,
    )


def _build_payload(
    *,
    runtime: RuntimeConfig,
    model: str,
    base_url: str,
    items: list[dict[str, Any]],
    scanned_media_entries: list[dict[str, Any]],
    archive_media_file_count: int,
    prepared_entry_count: int,
    run_limit: int | None,
    total_item_count: int | None = None,
    analyzed_item_count: int | None = None,
    completed_item_count: int | None = None,
) -> dict[str, Any]:
    artifact_relative_path = str((runtime.profile_dir / "deep_analysis.json").relative_to(runtime.project_root))
    report_relative_path = str(_report_path(runtime).relative_to(runtime.project_root))
    graph_relative_path = str(_entity_graph_path(runtime).relative_to(runtime.project_root))
    summary = _summarise_items(
        items,
        scanned_media_entries,
        archive_media_file_count=archive_media_file_count,
    )
    if total_item_count is not None:
        summary["item_folders_total"] = total_item_count
    if analyzed_item_count is not None:
        summary["item_folders_with_artifacts"] = analyzed_item_count
    if completed_item_count is not None:
        summary["item_folders_complete"] = completed_item_count
    if total_item_count:
        ready_count = completed_item_count if completed_item_count is not None else analyzed_item_count
        if ready_count is not None and ready_count < total_item_count:
            summary["overview"] = (
                f"{summary.get('overview') or ''} "
                f"Per-item analysis files are currently available for {ready_count} of {total_item_count} item folders."
            ).strip()
    return {
        "schema_version": SCHEMA_VERSION,
        "analysis_type": "safe_deep_media_profile",
        "generated_at": _now_iso(),
        "platform": runtime.platform,
        "username": runtime.username,
        "model": model,
        "ollama_base_url": base_url,
        "prompt_version": PROMPT_VERSION,
        "artifact_relative_path": artifact_relative_path,
        "report_relative_path": report_relative_path,
        "graph_relative_path": graph_relative_path,
        "bundle_source_root": str(Path("media") / runtime.platform / runtime.username),
        "summary": summary,
        "items": items,
        "prepared_entry_count": prepared_entry_count,
        "run_limit": run_limit,
        "is_partial_run": bool(prepared_entry_count > len(items)),
        "safety": {
            "scope": "Visible or explicitly stated content only.",
            "excluded_inferences": EXCLUDED_INFERENCES,
        },
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run an incremental safe deep-media analysis pass for archived targets."
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Ollama model name. Default: {DEFAULT_MODEL}")
    parser.add_argument(
        "--base-url",
        default=DEFAULT_OLLAMA_BASE_URL,
        help=f"Ollama base URL. Default: {DEFAULT_OLLAMA_BASE_URL}",
    )
    parser.add_argument(
        "--target",
        action="append",
        help=(
            "Limit analysis to one or more archived targets in platform:username format. "
            "Repeatable. When omitted, all discovered archived targets are analyzed."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional maximum number of item folders to analyze in this run.",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Skip Ollama and rebuild only the profile-level summary from existing per-item analysis.json files.",
    )
    parser.add_argument(
        "--pending-only",
        action="store_true",
        help="Analyze only item folders that are missing analysis files or whose media changed. This is the default behavior.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-run every item folder even when analysis files already exist.",
    )
    parser.add_argument(
        "--skip-chroma",
        action="store_true",
        help="Do not upsert the analysis documents into ChromaDB.",
    )
    parser.add_argument(
        "--skip-telegram",
        action="store_true",
        help="Do not send the final Telegram summary.",
    )
    return parser.parse_args(argv)


def _parse_target_selectors(raw_targets: list[str] | None) -> list[tuple[str, str]]:
    selectors: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in raw_targets or []:
        platform, sep, username = raw.partition(":")
        platform = platform.strip().lower()
        username = username.strip()
        if not sep or not platform or not username:
            raise ValueError(
                f"Invalid --target value {raw!r}. Use platform:username, for example instagram:k.dvorakovaa."
            )
        key = (platform, username)
        if key in seen:
            continue
        seen.add(key)
        selectors.append(key)
    return selectors


def _discover_archived_targets(project_root: Path = PROJECT_ROOT) -> list[tuple[str, str]]:
    settings = _load_runtime_settings(project_root)
    media_root = settings["data_dir"] / "media"
    discovered: list[tuple[str, str]] = []
    if not media_root.exists():
        return discovered

    for platform_dir in sorted(media_root.iterdir()):
        if not platform_dir.is_dir():
            continue
        for username_dir in sorted(platform_dir.iterdir()):
            if username_dir.is_dir():
                discovered.append((platform_dir.name, username_dir.name))
    return discovered


def _resolve_runtimes_for_args(args: argparse.Namespace, project_root: Path = PROJECT_ROOT) -> list[RuntimeConfig]:
    selectors = _parse_target_selectors(args.target)
    if not selectors:
        selectors = _discover_archived_targets(project_root)
    return [
        _load_runtime(project_root=project_root, platform=platform, username=username)
        for platform, username in selectors
    ]


def _run_for_runtime(runtime: RuntimeConfig, args: argparse.Namespace) -> int:
    if not runtime.media_root.exists():
        logger.error(f"[deep_analysis] Media root does not exist: {runtime.media_root}")
        return 1

    logger.info(
        "[deep_analysis] Starting safe deep analysis for "
        f"{runtime.platform}/{runtime.username} using {args.model} at {args.base_url}"
    )

    profile_index = _load_profile_index(runtime.profile_dir)
    all_media_entries = _scan_media_files(
        runtime.media_root,
        profile_index,
        platform=runtime.platform,
        target_username=runtime.username,
    )
    archive_media_file_count = len(all_media_entries)
    selected_media_entries = _select_mention_entries(all_media_entries)
    item_groups = _build_item_groups(
        all_media_entries,
        selected_media_entries,
        data_dir=runtime.data_dir,
    )
    prepared_entry_count = sum(int(group.get("prepared_entry_count") or 0) for group in item_groups)

    logger.info(
        "[deep_analysis] Discovered "
        f"{len(selected_media_entries)} selected source media files "
        f"(from {archive_media_file_count} scanned archive files) "
        f"across {len(item_groups)} item folders, "
        f"with {prepared_entry_count} total analysis entries."
    )

    artifact_path = runtime.profile_dir / "deep_analysis.json"
    report_path = _report_path(runtime)
    interrupted = False
    try:
        if args.summary_only:
            logger.info("[deep_analysis] Summary-only mode: skipping per-item Ollama analysis.")
        else:
            pending_groups = [
                group
                for group in item_groups
                if args.force or not _item_artifact_is_complete_and_current(
                    _load_item_artifact(group["item_dir"]),
                    group,
                )
            ]
            if args.limit is not None:
                pending_groups = pending_groups[: max(0, args.limit)]

            logger.info(
                "[deep_analysis] Pending item folders to analyze: "
                f"{len(pending_groups)}"
                + (" (force mode)" if args.force else "")
            )
            for index, item_group in enumerate(pending_groups, start=1):
                logger.info(
                    "[deep_analysis] Item "
                    f"{index}/{len(pending_groups)}: {item_group.get('item_relative_dir')}"
                )
                try:
                    _analyze_item_group(
                        runtime=runtime,
                        item_group=item_group,
                        model=args.model,
                        base_url=args.base_url,
                        force=args.force,
                    )
                except Exception as exc:
                    logger.error(
                        "[deep_analysis] Item analysis failed for "
                        f"{item_group.get('item_relative_dir')}: {exc}"
                    )
    except KeyboardInterrupt:
        interrupted = True
        logger.warning("[deep_analysis] Interrupted. Rebuilding the aggregate summary from saved item artifacts.")

    current_item_payloads: list[dict[str, Any]] = []
    complete_item_count = 0
    current_prepared_entry_count = 0
    for item_group in item_groups:
        payload = _load_item_artifact(item_group["item_dir"])
        if not payload or not _item_artifact_matches_current_sources(payload, item_group):
            continue
        current_item_payloads.append(payload)
        current_prepared_entry_count += int(payload.get("prepared_entry_count") or 0)
        if _item_artifact_is_complete_and_current(payload, item_group):
            complete_item_count += 1

    analyzed_items = _flatten_items_from_payloads(current_item_payloads)
    remaining_estimated_entries = sum(
        int(group.get("prepared_entry_count") or 0)
        for group in item_groups
        if not _item_artifact_matches_current_sources(_load_item_artifact(group["item_dir"]), group)
    )
    payload = _build_payload(
        runtime=runtime,
        model=args.model,
        base_url=args.base_url,
        items=analyzed_items,
        scanned_media_entries=selected_media_entries,
        archive_media_file_count=archive_media_file_count,
        prepared_entry_count=current_prepared_entry_count + remaining_estimated_entries,
        run_limit=args.limit if not args.summary_only else None,
        total_item_count=len(item_groups),
        analyzed_item_count=len(current_item_payloads),
        completed_item_count=complete_item_count,
    )
    entity_graph_payload = _build_entity_graph(
        analyzed_items,
        platform=runtime.platform,
        username=runtime.username,
    )

    _write_artifact(artifact_path, payload)
    _write_artifact(_entity_graph_path(runtime), entity_graph_payload)
    _write_markdown_report(report_path, payload)
    _remove_legacy_workspace_report(runtime.username)
    logger.info(f"[deep_analysis] Wrote artifact: {artifact_path}")
    logger.info(f"[deep_analysis] Wrote report:   {report_path}")

    if not args.skip_chroma:
        chroma_result = _upsert_chroma_documents(payload, runtime.data_dir)
        if not chroma_result.get("available"):
            logger.warning(
                "[deep_analysis] ChromaDB unavailable; skipped "
                f"{chroma_result.get('attempted', 0)} deep-analysis document upserts."
            )
        elif chroma_result.get("upserted") != chroma_result.get("attempted"):
            logger.warning(
                "[deep_analysis] ChromaDB upserts incomplete: "
                f"{chroma_result.get('upserted', 0)}/{chroma_result.get('attempted', 0)} documents saved."
            )
        else:
            logger.info(
                "[deep_analysis] Upserted "
                f"{chroma_result.get('upserted', 0)} deep-analysis documents into ChromaDB."
            )
    else:
        logger.info("[deep_analysis] ChromaDB upserts skipped by flag.")

    try:
        import asyncio

        asyncio.run(
            _send_telegram_summary(
                runtime,
                payload,
                skip_telegram=bool(args.skip_telegram or interrupted),
            )
        )
    except Exception as exc:
        logger.warning(f"[deep_analysis] Telegram summary failed: {exc}")

    logger.info("[deep_analysis] Finished.")
    return 130 if interrupted else 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logger.remove()
    logger.add(
        sys.stderr,
        level="INFO",
        format="[{time:YYYY-MM-DD HH:mm:ss}] [{level}] {message}",
        colorize=True,
    )

    runtimes = _resolve_runtimes_for_args(args)
    if not runtimes:
        logger.error("[deep_analysis] No archived targets were found under data/media.")
        return 1

    logger.info(
        "[deep_analysis] Selected archived targets: "
        + ", ".join(f"{runtime.platform}/{runtime.username}" for runtime in runtimes)
    )

    exit_code = 0
    for index, runtime in enumerate(runtimes, start=1):
        logger.info(
            "[deep_analysis] Target "
            f"{index}/{len(runtimes)}: {runtime.platform}/{runtime.username}"
        )
        exit_code = max(exit_code, _run_for_runtime(runtime, args))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
