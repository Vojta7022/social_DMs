"""
comment_utils.py — Helpers for storing and exporting threaded post comments.

The database stores comments as a flat table, but later visualizations need to
reconstruct the original discussion tree. These helpers assign stable thread
metadata to flat comment records and can rebuild a nested reply tree on demand.
"""

from __future__ import annotations

from typing import Any


def _clean_comment_id(value: Any) -> str | None:
    """Normalize comment identifiers to compact strings."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: Any) -> int | None:
    """Best-effort integer coercion for stored comment metadata."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _node_key(comment: dict[str, Any], index: int) -> str:
    """Return a stable internal key for a possibly-id-less comment record."""
    comment_id = _clean_comment_id(comment.get("comment_id"))
    if comment_id:
        return f"id:{comment_id}"
    return f"row:{index}"


def _sort_comments_for_tree(comments: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    """
    Order comment records for deterministic tree construction.
    Prefer existing explicit ordering fields when present, then fall back to the
    original input order.
    """
    keyed = list(enumerate(comments))

    def _sort_key(item: tuple[int, dict[str, Any]]) -> tuple[int, Any, Any, int]:
        index, comment = item
        comment_order = _int_or_none(comment.get("comment_order"))
        thread_path = comment.get("thread_path")
        if comment_order is not None:
            return (0, comment_order, thread_path or "", index)
        if isinstance(thread_path, str) and thread_path.strip():
            return (1, thread_path, "", index)
        return (2, index, "", index)

    return sorted(keyed, key=_sort_key)


def normalize_comment_records(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Return flat comment records with stable hierarchy fields filled in.

    Output fields added/overwritten:
    - parent_comment_id: normalized string or None
    - depth: zero-based nesting depth
    - thread_path: lexicographically sortable path such as 0001.0002
    - comment_order: depth-first display order
    """
    if not comments:
        return []

    ordered = _sort_comments_for_tree(comments)
    id_to_key: dict[str, str] = {}
    by_key: dict[str, dict[str, Any]] = {}
    children: dict[str, list[str]] = {}
    root_keys: list[str] = []

    for index, comment in ordered:
        record = dict(comment)
        record["comment_id"] = _clean_comment_id(record.get("comment_id"))
        record["parent_comment_id"] = _clean_comment_id(record.get("parent_comment_id"))
        key = _node_key(record, index)
        by_key[key] = record
        children.setdefault(key, [])
        if record["comment_id"]:
            id_to_key.setdefault(record["comment_id"], key)

    for index, comment in ordered:
        key = _node_key(comment, index)
        parent_id = by_key[key].get("parent_comment_id")
        parent_key = id_to_key.get(parent_id) if isinstance(parent_id, str) else None

        if parent_key and parent_key != key:
            siblings = children.setdefault(parent_key, [])
            if key not in siblings:
                siblings.append(key)
            continue

        if key not in root_keys:
            root_keys.append(key)

    normalised: list[dict[str, Any]] = []
    visited: set[str] = set()
    comment_order = 1

    def _visit(key: str, path_parts: list[int]) -> None:
        nonlocal comment_order
        if key in visited:
            return
        visited.add(key)

        record = dict(by_key[key])
        record["depth"] = len(path_parts) - 1
        record["thread_path"] = ".".join(f"{part:04d}" for part in path_parts)
        record["comment_order"] = comment_order
        comment_order += 1
        normalised.append(record)

        for child_index, child_key in enumerate(children.get(key, []), start=1):
            _visit(child_key, path_parts + [child_index])

    for root_index, key in enumerate(root_keys, start=1):
        _visit(key, [root_index])

    # Guard against malformed/cyclic parent references by emitting anything left.
    for fallback_index, (original_index, comment) in enumerate(ordered, start=len(root_keys) + 1):
        key = _node_key(comment, original_index)
        if key not in visited:
            _visit(key, [fallback_index])

    return normalised


def build_comment_tree(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return nested comment threads with `replies` arrays."""
    normalised = normalize_comment_records(comments)
    if not normalised:
        return []

    id_to_key: dict[str, str] = {}
    by_key: dict[str, dict[str, Any]] = {}
    roots: list[str] = []

    for index, comment in enumerate(normalised):
        key = _node_key(comment, index)
        node = dict(comment)
        node["replies"] = []
        by_key[key] = node
        comment_id = _clean_comment_id(comment.get("comment_id"))
        if comment_id:
            id_to_key.setdefault(comment_id, key)

    for index, comment in enumerate(normalised):
        key = _node_key(comment, index)
        parent_id = _clean_comment_id(comment.get("parent_comment_id"))
        parent_key = id_to_key.get(parent_id) if parent_id else None
        if parent_key and parent_key != key:
            by_key[parent_key]["replies"].append(by_key[key])
        else:
            roots.append(key)

    return [by_key[key] for key in roots]
