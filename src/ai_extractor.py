"""
ai_extractor.py — Multimodal AI analysis of reposts using Google Gemini.

Flow for each detected repost:
  1. extract_repost_frames()     — pull N evenly-spaced JPEG frames from the video
  2. extract_audio_as_bytes()    — pull audio track from the video
  3. analyze_repost()            — send caption + frames + audio to Gemini 1.5 Flash
  4. store_ai_analysis()         — persist JSON result in repost_history
  5. (caller) delete temp files  — only metadata survives; no media is saved to disk

All functions return None / [] on failure without raising so that the scraper
pipeline continues even when the Gemini API is unavailable.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from loguru import logger
from src.utils import extract_video_frames, safe_unlink


# ─── Frame extraction ─────────────────────────────────────────────────────────

def extract_repost_frames(video_path: str | Path, n: int = 4) -> list[bytes]:
    """
    Extract N evenly-spaced JPEG frames from a video using ffmpeg.
    Returns a list of raw JPEG bytes (may be shorter than n if the video is short).
    Delegates to src.utils.extract_video_frames.
    """
    return extract_video_frames(Path(video_path), n=n)


# ─── Audio extraction ─────────────────────────────────────────────────────────

def extract_audio_as_bytes(video_path: str | Path, max_duration_s: int = 60) -> bytes | None:
    """
    Extract up to max_duration_s seconds of audio from a video as MP3 bytes.
    Returns None on failure.
    """
    video_path = Path(video_path)
    if not video_path.exists():
        return None

    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        result = subprocess.run(
            [
                "ffmpeg", "-v", "quiet",
                "-i", str(video_path),
                "-t", str(max_duration_s),
                "-vn",                  # strip video
                "-acodec", "libmp3lame",
                "-q:a", "7",            # low bitrate — only need intelligibility
                "-y", str(tmp_path),
            ],
            capture_output=True,
            timeout=60,
        )
        if result.returncode == 0 and tmp_path.exists() and tmp_path.stat().st_size > 0:
            audio_bytes = tmp_path.read_bytes()
            safe_unlink(tmp_path)
            return audio_bytes

        safe_unlink(tmp_path)
        return None

    except Exception as e:
        logger.warning(f"[ai_extractor] Audio extraction failed: {e}")
        return None


# ─── Gemini analysis ──────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """
You are an OSINT analyst. You will receive a social media repost: its caption, up to 4
video frames, and optionally an audio transcript. Your task is to return a JSON object
with the following keys:

{
  "mood_vibe": "<short string describing the emotional tone>",
  "mood_score": <integer 1-10, where 1=very negative, 10=very positive>,
  "hobbies": ["<hobby1>", "<hobby2>"],
  "financial_indicators": "<string or null — clues about wealth/poverty>",
  "age_gender_estimate": "<string or null — rough estimate based on visual/audio cues>",
  "relationship_clues": "<string or null — relationship status hints>",
  "intellectual_political_themes": "<string or null — topics, ideology, political leanings>",
  "overall_summary": "<2-3 sentence OSINT summary of the target based on this content>"
}

Return ONLY the JSON object, no markdown fences.
""".strip()


def analyze_repost(
    caption: str | None,
    frames: list[bytes],
    audio_bytes: bytes | None,
    api_key: str,
) -> dict | None:
    """
    Send caption + frames + audio to Gemini 1.5 Flash and return the parsed JSON dict.
    Returns None if the API key is missing, if no content is available, or on any error.
    """
    if not api_key:
        return None
    if not caption and not frames and not audio_bytes:
        return None

    try:
        import google.generativeai as genai  # type: ignore
        from google.generativeai.types import HarmBlockThreshold, HarmCategory  # type: ignore
    except ImportError:
        logger.warning("[ai_extractor] google-generativeai not installed; skipping analysis")
        return None

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(
            model_name="gemini-1.5-flash",
            system_instruction=_SYSTEM_PROMPT,
        )

        parts: list = []

        if caption:
            parts.append(f"Caption:\n{caption[:2000]}")

        for i, frame_bytes in enumerate(frames):
            parts.append({
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data": frame_bytes,
                }
            })

        if audio_bytes:
            parts.append({
                "inline_data": {
                    "mime_type": "audio/mpeg",
                    "data": audio_bytes,
                }
            })

        if not parts:
            return None

        safety_settings = {
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
        }

        response = model.generate_content(
            parts,
            generation_config=genai.GenerationConfig(
                response_mime_type="application/json",
                temperature=0.3,
            ),
            safety_settings=safety_settings,
        )

        text = response.text.strip()
        return json.loads(text)

    except Exception as e:
        logger.warning(f"[ai_extractor] Gemini API error: {e}")
        return None


# ─── DB persistence ───────────────────────────────────────────────────────────

def store_ai_analysis(conn, repost_history_id: int, analysis: dict) -> None:
    """Persist the Gemini JSON analysis into repost_history.ai_analysis_json."""
    from src.db import update_repost_ai_analysis
    update_repost_ai_analysis(conn, repost_history_id, json.dumps(analysis, ensure_ascii=False))


# ─── ChromaDB upsert (best-effort) ───────────────────────────────────────────

def push_to_vector_store(
    target_id: int,
    target_username: str,
    analysis: dict,
    detected_at: str,
    data_dir: Path,
) -> None:
    """Upsert the analysis into ChromaDB. No-op if chromadb is not installed."""
    try:
        from src.vector_store import upsert_insight
        upsert_insight(target_id, target_username, analysis, detected_at, data_dir)
    except ImportError:
        pass
    except Exception as e:
        logger.warning(f"[ai_extractor] ChromaDB upsert failed: {e}")
