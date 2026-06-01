"""Shared helpers for consistent human-readable terminal logging."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from loguru import logger

_SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9_./:@%+=,#~-]+$")


def build_log_message(component: str, event: str, **fields: Any) -> str:
    """Return a stable `event | key=value` log line."""
    parts = [f"[{component}] {event}"]
    for key, value in fields.items():
        if value in (None, "", [], {}, ()):
            continue
        parts.append(f"{key}={_format_value(value)}")
    return " | ".join(parts)


def log_event(component: str, event: str, *, level: str = "info", **fields: Any) -> None:
    """Emit a structured loguru message with a shared format."""
    log_method = getattr(logger, level)
    log_method(build_log_message(component, event, **fields))


def compact_url(url: str | None, *, max_length: int = 96) -> str | None:
    """Strip query noise from URLs so terminal logs stay scannable."""
    if not isinstance(url, str) or not url.strip():
        return None

    parsed = urlsplit(url.strip())
    compact = f"{parsed.scheme}://{parsed.netloc}{parsed.path}" if parsed.scheme else url.strip()
    if len(compact) <= max_length:
        return compact
    return f"{compact[: max_length - 1]}…"


def normalize_job_name(job_type: str | None) -> str | None:
    """Collapse bootstrap_* job names into their logical scrape kind."""
    if not isinstance(job_type, str) or not job_type:
        return job_type
    return job_type.removeprefix("bootstrap_")


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        raw = "true" if value else "false"
    elif isinstance(value, Path):
        raw = value.as_posix()
    elif isinstance(value, float):
        raw = f"{value:.2f}".rstrip("0").rstrip(".")
    elif isinstance(value, (list, tuple, set)):
        raw = ",".join(str(item) for item in value)
    elif isinstance(value, dict):
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    else:
        raw = str(value)

    raw = raw.replace("\n", " ").strip()
    if len(raw) > 220:
        raw = f"{raw[:217]}..."
    if not raw:
        return '""'
    if _SAFE_VALUE_RE.fullmatch(raw):
        return raw
    return json.dumps(raw, ensure_ascii=False)
