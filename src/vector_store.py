"""
vector_store.py — ChromaDB persistence for AI-generated personality insights.

Stores one document per (target, analysis timestamp) so historical analyses
accumulate and can be queried semantically across all monitored targets.

Collection: personality_insights
Document:   stringified JSON of the full Gemini analysis
Metadata:   {target_id, target_username, detected_at}
"""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
from types import ModuleType

from loguru import logger

_CHROMADB_MODULE: ModuleType | None = None
_CHROMADB_IMPORT_ATTEMPTED = False
_CHROMADB_UNAVAILABLE_LOGGED = False


def _chroma_dir(data_dir: Path) -> str:
    path = data_dir / "chroma"
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _attempt_chromadb_import() -> ModuleType:
    import chromadb  # type: ignore

    return chromadb


def _load_chromadb_module() -> ModuleType | None:
    """Best-effort ChromaDB import with once-per-process warning noise control."""
    global _CHROMADB_MODULE, _CHROMADB_IMPORT_ATTEMPTED, _CHROMADB_UNAVAILABLE_LOGGED

    if _CHROMADB_MODULE is not None:
        return _CHROMADB_MODULE
    if _CHROMADB_IMPORT_ATTEMPTED:
        return None

    _CHROMADB_IMPORT_ATTEMPTED = True
    try:
        _CHROMADB_MODULE = _attempt_chromadb_import()
    except ImportError as exc:
        if not _CHROMADB_UNAVAILABLE_LOGGED:
            logger.warning(f"[vector_store] ChromaDB unavailable; vector-store features disabled: {exc}")
            _CHROMADB_UNAVAILABLE_LOGGED = True
        _CHROMADB_MODULE = None
    return _CHROMADB_MODULE


def chroma_backend_available() -> bool:
    """Return True when ChromaDB can be imported in the current Python env."""
    return _load_chromadb_module() is not None


def get_named_collection(data_dir: Path, name: str):
    """Open (or create) one persistent ChromaDB collection by name."""
    chromadb = _load_chromadb_module()
    if chromadb is None:
        return None

    client = chromadb.PersistentClient(path=_chroma_dir(data_dir))
    collection = client.get_or_create_collection(
        name=name,
        metadata={"hnsw:space": "cosine"},
    )
    return collection


def get_collection(data_dir: Path):
    """Compatibility wrapper for the original personality-insights collection."""
    return get_named_collection(data_dir, "personality_insights")


def _stable_id(prefix: str, *parts: object) -> str:
    raw = "||".join(str(part or "") for part in parts)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    return f"{prefix}_{digest}"


def upsert_insight(
    target_id: int,
    target_username: str,
    analysis_dict: dict,
    detected_at: str,
    data_dir: Path,
) -> bool:
    """
    Upsert one Gemini analysis into ChromaDB.
    The document ID is unique per target per timestamp so repeated analyses accumulate.
    """
    try:
        collection = get_collection(data_dir)
        if collection is None:
            return False
        doc_id = f"{target_id}_{detected_at.replace(':', '-').replace('T', '_')}"
        document = json.dumps(analysis_dict, ensure_ascii=False)
        collection.upsert(
            ids=[doc_id],
            documents=[document],
            metadatas=[{
                "target_id": target_id,
                "target_username": target_username,
                "detected_at": detected_at,
            }],
        )
        logger.debug(f"[vector_store] Upserted insight for {target_username} at {detected_at}")
        return True
    except Exception as e:
        logger.warning(f"[vector_store] upsert_insight failed: {e}")
        return False


def query_insights(
    query_text: str,
    data_dir: Path,
    n_results: int = 5,
    target_username: str | None = None,
) -> list[dict]:
    """
    Semantic search over stored personality insights.
    Returns a list of dicts with keys: id, document, metadata, distance.
    Optionally filter by target_username.
    """
    try:
        collection = get_collection(data_dir)
        if collection is None:
            return []
        where = {"target_username": target_username} if target_username else None
        results = collection.query(
            query_texts=[query_text],
            n_results=min(n_results, collection.count() or 1),
            where=where,
            include=["documents", "metadatas", "distances"],
        )

        output = []
        ids = results.get("ids", [[]])[0]
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        dists = results.get("distances", [[]])[0]

        for doc_id, doc, meta, dist in zip(ids, docs, metas, dists):
            output.append({
                "id": doc_id,
                "document": doc,
                "metadata": meta,
                "distance": dist,
            })
        return output

    except Exception as e:
        logger.warning(f"[vector_store] query_insights failed: {e}")
        return []


def upsert_style_example(
    *,
    platform: str,
    account_username: str,
    target_username: str | None,
    conversation_id: str | None,
    message_key: str,
    text: str,
    sent_at: str | None,
    data_dir: Path,
) -> bool:
    """Store one self-authored message example for later style retrieval."""
    if not str(text or "").strip():
        return False

    try:
        collection = get_named_collection(data_dir, "user_style_examples")
        if collection is None:
            return False
        doc_id = _stable_id(
            "style",
            platform,
            account_username,
            target_username,
            conversation_id,
            message_key,
        )
        collection.upsert(
            ids=[doc_id],
            documents=[str(text).strip()],
            metadatas=[{
                "platform": platform,
                "account_username": account_username,
                "target_username": target_username or "",
                "conversation_id": conversation_id or "",
                "message_key": message_key,
                "sent_at": sent_at or "",
            }],
        )
        return True
    except Exception as exc:
        logger.warning(f"[vector_store] upsert_style_example failed: {exc}")
        return False


def query_style_examples(
    *,
    query_text: str,
    data_dir: Path,
    account_username: str | None = None,
    target_username: str | None = None,
    n_results: int = 5,
) -> list[dict]:
    """Semantic search over stored user writing-style examples."""
    try:
        collection = get_named_collection(data_dir, "user_style_examples")
        if collection is None:
            return []
        if collection.count() == 0:
            return []

        where: dict[str, object] | None = None
        if target_username:
            where = {"target_username": target_username}
        elif account_username:
            where = {"account_username": account_username}

        results = collection.query(
            query_texts=[query_text],
            n_results=min(n_results, collection.count()),
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        return _normalise_query_results(results)
    except Exception as exc:
        logger.warning(f"[vector_store] query_style_examples failed: {exc}")
        return []


def upsert_target_profile_document(
    *,
    platform: str,
    target_username: str,
    source_kind: str,
    source_id: str,
    content: str,
    metadata: dict | None,
    data_dir: Path,
) -> bool:
    """Store one OSINT/profile document chunk for a monitored target."""
    if not str(content or "").strip():
        return False
    try:
        collection = get_named_collection(data_dir, "target_osint_profiles")
        if collection is None:
            return False
        doc_id = _stable_id("osint", platform, target_username, source_kind, source_id)
        payload = {
            "platform": platform,
            "target_username": target_username,
            "source_kind": source_kind,
            "source_id": source_id,
        }
        if metadata:
            payload.update(metadata)
        collection.upsert(
            ids=[doc_id],
            documents=[str(content).strip()],
            metadatas=[payload],
        )
        return True
    except Exception as exc:
        logger.warning(f"[vector_store] upsert_target_profile_document failed: {exc}")
        return False


def query_target_profiles(
    *,
    query_text: str,
    data_dir: Path,
    platform: str | None = None,
    target_username: str | None = None,
    n_results: int = 6,
) -> list[dict]:
    """Semantic search over stored OSINT/profile documents for one target."""
    try:
        collection = get_named_collection(data_dir, "target_osint_profiles")
        if collection is None:
            return []
        if collection.count() == 0:
            return []

        where: dict[str, object] | None = None
        if platform and target_username:
            where = {"$and": [{"platform": platform}, {"target_username": target_username}]}
        elif target_username:
            where = {"target_username": target_username}
        elif platform:
            where = {"platform": platform}

        results = collection.query(
            query_texts=[query_text],
            n_results=min(n_results, collection.count()),
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        return _normalise_query_results(results)
    except Exception as exc:
        logger.warning(f"[vector_store] query_target_profiles failed: {exc}")
        return []


def _normalise_query_results(results: dict) -> list[dict]:
    """Flatten Chroma query responses into a stable list of result dicts."""
    output = []
    ids = results.get("ids", [[]])[0]
    docs = results.get("documents", [[]])[0]
    metas = results.get("metadatas", [[]])[0]
    dists = results.get("distances", [[]])[0]
    for doc_id, doc, meta, dist in zip(ids, docs, metas, dists):
        output.append({
            "id": doc_id,
            "document": doc,
            "metadata": meta or {},
            "distance": dist,
        })
    return output
