"""
face_matcher.py — Facial similarity scoring between archived profile avatars.

Uses DeepFace for embedding-based comparison. Falls back gracefully if deepface
is not installed or no avatar images are available.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _avatar_paths(platform: str, username: str, data_root: Path) -> list[Path]:
    """Return all avatar image files for a profile, newest first."""
    avatar_dir = data_root / "media" / platform / username / "avatars"
    if not avatar_dir.exists():
        return []
    images = sorted(
        [p for p in avatar_dir.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png"}],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return images


def _best_avatar(platform: str, username: str, data_root: Path) -> Path | None:
    """Return the most recent avatar file, or None."""
    paths = _avatar_paths(platform, username, data_root)
    return paths[0] if paths else None


def compare_faces(path_a: Path, path_b: Path) -> float | None:
    """
    Compare two face images using DeepFace.
    Returns a similarity score in [0, 1] (1 = identical), or None on failure.
    """
    try:
        from deepface import DeepFace  # type: ignore
    except ImportError:
        logger.debug("deepface not installed; face matching unavailable")
        return None

    try:
        result: Any = DeepFace.verify(
            img1_path=str(path_a),
            img2_path=str(path_b),
            model_name="Facenet512",
            detector_backend="retinaface",
            enforce_detection=False,
            silent=True,
        )
        # DeepFace.verify returns distance; convert to similarity via sigmoid-style mapping.
        # distance=0 → similarity=1; distance increases → similarity drops toward 0.
        distance: float = float(result.get("distance", 1.0))
        # Normalise: Facenet512 cosine threshold ≈ 0.30; map [0, 0.6] → [1, 0]
        similarity = max(0.0, min(1.0, 1.0 - distance / 0.6))
        return round(similarity, 3)
    except Exception as exc:
        logger.debug("DeepFace.verify failed for %s vs %s: %s", path_a.name, path_b.name, exc)
        return None


def score_face_match(
    platform_a: str,
    username_a: str,
    platform_b: str,
    username_b: str,
    data_root: Path,
) -> float | None:
    """
    Return face similarity score between the two profiles' most recent avatars,
    or None if avatars are unavailable or comparison fails.
    """
    path_a = _best_avatar(platform_a, username_a, data_root)
    path_b = _best_avatar(platform_b, username_b, data_root)
    if not path_a or not path_b:
        return None
    return compare_faces(path_a, path_b)


def enrich_matches_with_face_scores(
    matches: list[dict],
    source_platform: str,
    source_username: str,
    data_root: Path,
    face_threshold: float = 0.55,
) -> list[dict]:
    """
    Add a face_score to each match dict and boost confidence for strong face matches.
    Mutates and returns the list in-place.
    """
    for match in matches:
        target_platform = match.get("platform", "")
        target_username = match.get("username", "")
        score = score_face_match(
            source_platform, source_username,
            target_platform, target_username,
            data_root,
        )
        if score is None:
            continue
        match.setdefault("evidence", {})["face_score"] = score
        if score >= face_threshold:
            match["confidence"] = round(min(1.0, match.get("confidence", 0.0) + 0.20), 2)
            reasons: list[str] = match.get("reasons") or []
            if "face_match" not in reasons:
                reasons.append("face_match")
            match["reasons"] = sorted(set(reasons))
    return matches
