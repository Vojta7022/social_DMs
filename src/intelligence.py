"""
intelligence.py — Lightweight local analytics for archived profile data.

This module is intentionally offline-friendly so it can run on macOS and
Raspberry Pi 4 without cloud services or heavyweight infrastructure.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
import re
from statistics import mean, median, pstdev
from typing import Any, Protocol
import unicodedata

REPOST_CLASSIFIER_VERSION = "repost_keyword_v1"
_REPOST_CATEGORY_KEYWORDS = {
    "hobbies": {
        "gym", "workout", "fitness", "football", "soccer", "basketball", "tennis",
        "run", "running", "travel", "trip", "music", "guitar", "piano", "book",
        "gaming", "game", "ski", "skating", "fashion", "makeup", "cooking", "car",
        "cars", "art", "photography",
    },
    "humor": {
        "meme", "funny", "lol", "lmao", "haha", "joke", "comedy", "laugh", "🤣", "😂",
    },
    "emotional": {
        "love", "heart", "miss", "cry", "sad", "happy", "healing", "anxiety", "depressed",
        "feel", "feeling", "broken", "grateful", "blessed", "lonely", "relationship",
    },
    "social_political": {
        "politics", "political", "government", "rights", "war", "peace", "news", "election",
        "society", "protest", "climate", "policy", "feminism", "activism", "justice",
    },
}


class _SentimentAnalyzer(Protocol):
    def polarity_scores(self, text: str) -> dict[str, float]:
        ...


def build_intelligence_bundle(
    *,
    platform: str,
    username: str,
    generated_at: str,
    bio: dict | None,
    posts: list[dict],
    reposts: list[dict] | None = None,
    stories: list[dict] | None = None,
    candidate_profiles: list[dict] | None = None,
    data_root: Any = None,
) -> dict:
    """Return local analytics derived from already archived content."""
    feed_posts = [post for post in posts if (post.get("content_scope") or "feed") == "feed"]
    repost_items = reposts or []
    story_items = stories or []
    sentiment_summary = _build_sentiment_summary(posts)
    repost_analytics = _build_repost_analytics(repost_items, generated_at)
    geoparsed_locations = _build_geoparsed_location_summary(
        username=username,
        bio=bio or {},
        posts=posts,
        stories=story_items,
    )
    linked_profiles = _build_cross_platform_links(
        platform=platform,
        username=username,
        bio=bio or {},
        candidate_profiles=candidate_profiles or [],
        data_root=data_root,
    )
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "platform": platform,
        "username": username,
        "next_post_prediction": _forecast_next_post(feed_posts, generated_at),
        "sentiment_overview": sentiment_summary,
        "repost_analytics": repost_analytics,
        "geoparsed_locations": geoparsed_locations,
        "linked_profiles": linked_profiles,
    }


def summarise_intelligence(bundle: dict) -> dict:
    """Return a compact top-level summary for profile.json."""
    forecast = bundle.get("next_post_prediction") or {}
    prediction = forecast.get("prediction") or {}
    basis = forecast.get("basis") or {}
    sentiment = bundle.get("sentiment_overview") or {}
    reposts = bundle.get("repost_analytics") or {}
    geoparsed = bundle.get("geoparsed_locations") or {}
    links = bundle.get("linked_profiles") or {}
    return {
        "next_post_prediction": {
            "status": forecast.get("status"),
            "predicted_at": prediction.get("predicted_at"),
            "window_start": prediction.get("window_start"),
            "window_end": prediction.get("window_end"),
            "confidence": forecast.get("confidence"),
            "post_count_used": basis.get("post_count_used"),
            "last_post_at": basis.get("last_post_at"),
        },
        "sentiment": {
            "status": sentiment.get("status"),
            "average_vibe_score": sentiment.get("average_vibe_score"),
            "posts_scored": sentiment.get("posts_scored"),
            "positive_posts": sentiment.get("positive_posts"),
            "neutral_posts": sentiment.get("neutral_posts"),
            "negative_posts": sentiment.get("negative_posts"),
        },
        "repost_analytics": {
            "status": reposts.get("status"),
            "total_reposts": reposts.get("total_reposts"),
            "average_vibe_score": reposts.get("average_vibe_score"),
            "active_window": reposts.get("active_window"),
            "top_categories": (reposts.get("top_categories") or [])[:3],
            "high_frequency_sources": (reposts.get("high_frequency_sources") or [])[:5],
            "deviation_flags": reposts.get("deviation_flag_count"),
        },
        "geoparsed_locations": {
            "status": geoparsed.get("status"),
            "top_locations": (geoparsed.get("top_locations") or [])[:5],
            "total_locations": geoparsed.get("total_locations"),
        },
        "linked_profiles": {
            "status": links.get("status"),
            "match_count": links.get("match_count"),
            "matches": (links.get("matches") or [])[:5],
        },
    }


def build_social_graph_bundle(
    *,
    platform: str,
    username: str,
    generated_at: str,
    followers: dict,
    following: dict,
) -> dict:
    """Return a NetworkX-compatible node-link graph export."""
    target_node_id = _graph_node_id(platform, username=username, user_id=None)
    nodes: list[dict[str, Any]] = [
        {
            "id": target_node_id,
            "platform": platform,
            "username": username,
            "user_id": None,
            "display_name": username,
            "kind": "target",
            "is_target_profile": True,
        }
    ]
    node_ids = {target_node_id}
    links: list[dict[str, Any]] = []

    def add_user_node(user: dict[str, Any], relation: str) -> str | None:
        if not isinstance(user, dict):
            return None
        user_username = user.get("username")
        if not user_username:
            return None
        node_id = _graph_node_id(platform, username=user_username, user_id=user.get("user_id"))
        if node_id not in node_ids:
            nodes.append(
                {
                    "id": node_id,
                    "platform": platform,
                    "username": user_username,
                    "user_id": user.get("user_id"),
                    "display_name": user.get("display_name") or user_username,
                    "profile_pic_url": user.get("profile_pic_url"),
                    "is_verified": bool(user.get("is_verified")),
                    "is_private": bool(user.get("is_private")),
                    "kind": relation,
                    "is_target_profile": False,
                }
            )
            node_ids.add(node_id)
        return node_id

    for user in followers.get("users") or []:
        node_id = add_user_node(user, "follower")
        if not node_id:
            continue
        links.append(
            {
                "source": node_id,
                "target": target_node_id,
                "relationship": "follows_target",
                "snapshot_at": followers.get("snapshot_at"),
            }
        )

    for user in following.get("users") or []:
        node_id = add_user_node(user, "following")
        if not node_id:
            continue
        links.append(
            {
                "source": target_node_id,
                "target": node_id,
                "relationship": "target_follows",
                "snapshot_at": following.get("snapshot_at"),
            }
        )

    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "platform": platform,
        "username": username,
        "format": "networkx_node_link",
        "directed": True,
        "multigraph": False,
        "graph": {
            "name": f"{platform}:{username}",
            "target_node_id": target_node_id,
            "followers_snapshot_at": followers.get("snapshot_at"),
            "following_snapshot_at": following.get("snapshot_at"),
        },
        "nodes": nodes,
        "links": links,
    }


def _forecast_next_post(posts: list[dict], generated_at: str) -> dict:
    """Predict the next likely feed-post window from historical timestamps."""
    timestamps = sorted(
        dt for dt in (_pick_post_timestamp(post) for post in posts) if dt is not None
    )
    if len(timestamps) < 2:
        return {
            "status": "insufficient_history",
            "generated_at": generated_at,
            "confidence": 0.0,
            "basis": {
                "post_count_used": len(timestamps),
                "first_post_at": _iso(timestamps[0]) if timestamps else None,
                "last_post_at": _iso(timestamps[-1]) if timestamps else None,
            },
            "prediction": {},
            "patterns": {},
            "notes": [
                "Need at least 2 feed posts with usable timestamps before forecasting."
            ],
        }

    intervals_hours = [
        max((curr - prev).total_seconds() / 3600.0, 0.0)
        for prev, curr in zip(timestamps, timestamps[1:])
    ]
    median_interval_hours = median(intervals_hours)
    mean_interval_hours = mean(intervals_hours)
    stdev_interval_hours = pstdev(intervals_hours) if len(intervals_hours) > 1 else 0.0
    mad_hours = median(
        [abs(interval - median_interval_hours) for interval in intervals_hours]
    )

    last_post_at = timestamps[-1]
    predicted_at = last_post_at + timedelta(hours=median_interval_hours)

    if len(intervals_hours) == 1:
        # With only one observed posting interval, keep the forecast deliberately broad.
        window_half_width_hours = max(
            12.0,
            min(96.0, (median_interval_hours * 0.75) if median_interval_hours else 24.0),
        )
    else:
        window_half_width_hours = max(
            6.0,
            min(72.0, mad_hours * 1.5 if mad_hours else 12.0),
        )
    window_start = predicted_at - timedelta(hours=window_half_width_hours)
    window_end = predicted_at + timedelta(hours=window_half_width_hours)

    hour_counter = Counter(ts.hour for ts in timestamps)
    weekday_counter = Counter(ts.strftime("%A") for ts in timestamps)

    mean_safe = mean_interval_hours if mean_interval_hours > 0 else 1.0
    relative_spread = stdev_interval_hours / mean_safe
    consistency_factor = 1.0 / (1.0 + relative_spread)
    if len(intervals_hours) == 1:
        confidence = round(max(0.1, min(0.45, 0.18 + (0.12 * consistency_factor))), 2)
        notes = [
            "Prediction is based on only two archived feed-post timestamps.",
            "Treat this as a low-confidence estimate until more post history is captured.",
            "All times are stored in UTC.",
        ]
    else:
        sample_factor = min(1.0, len(intervals_hours) / 12.0)
        confidence = round(
            max(0.05, min(0.95, 0.2 + (0.45 * sample_factor) + (0.35 * consistency_factor))),
            2,
        )
        notes = [
            "Prediction is based on archived feed-post timestamps only.",
            "All times are stored in UTC.",
        ]

    return {
        "status": "ready",
        "generated_at": generated_at,
        "confidence": confidence,
        "basis": {
            "post_count_used": len(timestamps),
            "first_post_at": _iso(timestamps[0]),
            "last_post_at": _iso(last_post_at),
        },
        "prediction": {
            "predicted_at": _iso(predicted_at),
            "window_start": _iso(window_start),
            "window_end": _iso(window_end),
            "expected_hour_utc": predicted_at.hour,
            "expected_weekday_utc": predicted_at.strftime("%A"),
        },
        "patterns": {
            "median_interval_hours": round(median_interval_hours, 2),
            "mean_interval_hours": round(mean_interval_hours, 2),
            "stddev_interval_hours": round(stdev_interval_hours, 2),
            "median_absolute_deviation_hours": round(mad_hours, 2),
            "most_common_hours_utc": [hour for hour, _ in hour_counter.most_common(3)],
            "most_common_weekdays_utc": [day for day, _ in weekday_counter.most_common(3)],
        },
        "notes": notes,
    }


def _pick_post_timestamp(post: dict) -> datetime | None:
    """Prefer posted_at; fall back to discovered_at when needed."""
    for key in ("posted_at", "discovered_at"):
        value = post.get(key)
        parsed = _parse_iso_datetime(value)
        if parsed is not None:
            return parsed
    return None


def _pick_repost_timestamp(repost: dict) -> datetime | None:
    """Prefer reposted_at, then original_posted_at, then posted/discovered timestamps."""
    for key in ("reposted_at", "original_posted_at", "posted_at", "discovered_at"):
        parsed = _parse_iso_datetime(repost.get(key))
        if parsed is not None:
            return parsed
    return None


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _within_days(value: datetime | None, reference: datetime, days: int) -> bool:
    if value is None:
        return False
    return value >= reference - timedelta(days=days)


def _repost_source_key(repost: dict) -> str:
    platform = str(repost.get("source_platform") or repost.get("platform") or "unknown").strip().lower()
    user_id = str(repost.get("source_user_id") or "").strip()
    username = str(repost.get("source_username") or repost.get("repost_source_account") or "").strip().lower()
    if user_id:
        return f"{platform}:id:{user_id}"
    if username:
        return f"{platform}:username:{username}"
    return f"{platform}:unknown"


def add_sentiment_to_posts(posts: list[dict]) -> list[dict]:
    """Annotate exported post items with local VADER sentiment data."""
    analyzer = _get_vader_analyzer()
    if analyzer is None:
        return [_with_sentiment(post, _dependency_missing_sentiment()) for post in posts]
    return [_with_sentiment(post, _score_post_sentiment(post, analyzer)) for post in posts]


def add_sentiment_to_reposts(reposts: list[dict]) -> list[dict]:
    """Annotate metadata-only repost items with local caption sentiment."""
    analyzer = _get_vader_analyzer()
    if analyzer is None:
        return [_with_sentiment(repost, _dependency_missing_sentiment()) for repost in reposts]
    return [_with_sentiment(repost, _score_repost_sentiment(repost, analyzer)) for repost in reposts]


def add_behavior_to_reposts(reposts: list[dict]) -> list[dict]:
    """Attach lightweight keyword-based behavior categories to repost items."""
    enriched: list[dict] = []
    for repost in reposts:
        annotated = dict(repost)
        category = _classify_repost_behavior(repost)
        annotated["behavior_category"] = category["behavior_category"]
        annotated["behavior_confidence"] = category["behavior_confidence"]
        annotated["classifier_version"] = category["classifier_version"]
        enriched.append(annotated)
    return enriched


def add_geoparsing_to_posts(posts: list[dict]) -> list[dict]:
    """Attach derived location candidates to archived post items."""
    enriched: list[dict] = []
    for post in posts:
        annotated = dict(post)
        candidates = []
        candidates.extend(
            _extract_text_location_candidates(
                text=post.get("caption"),
                source_type="caption_text",
                item_kind="post",
                item_id=post.get("post_id"),
                occurred_at=post.get("posted_at") or post.get("discovered_at"),
            )
        )
        annotated["geoparsed_locations"] = _aggregate_location_candidates(candidates)
        enriched.append(annotated)
    return enriched


def add_geoparsing_to_stories(stories: list[dict]) -> list[dict]:
    """Attach derived location candidates to archived story/highlight items."""
    enriched: list[dict] = []
    for story in stories:
        annotated = dict(story)
        metadata = story.get("metadata") if isinstance(story.get("metadata"), dict) else {}
        candidates = []
        candidates.extend(
            _extract_structured_story_location_candidates(
                metadata=metadata,
                item_id=story.get("story_id"),
                item_kind="story" if story.get("content_scope") != "highlight" else "highlight",
                occurred_at=story.get("posted_at") or story.get("discovered_at"),
            )
        )
        candidates.extend(
            _extract_text_location_candidates(
                text=metadata.get("caption") or metadata.get("accessibility_caption"),
                source_type="story_text",
                item_kind="story" if story.get("content_scope") != "highlight" else "highlight",
                item_id=story.get("story_id"),
                occurred_at=story.get("posted_at") or story.get("discovered_at"),
            )
        )
        for hashtag in metadata.get("hashtags") or []:
            candidates.extend(
                _extract_hashtag_location_candidates(
                    hashtag,
                    source_type="story_hashtag",
                    item_kind="story" if story.get("content_scope") != "highlight" else "highlight",
                    item_id=story.get("story_id"),
                    occurred_at=story.get("posted_at") or story.get("discovered_at"),
                )
            )
        annotated["geoparsed_locations"] = _aggregate_location_candidates(candidates)
        enriched.append(annotated)
    return enriched


def _with_sentiment(post: dict, sentiment: dict) -> dict:
    annotated = dict(post)
    annotated["sentiment"] = sentiment
    annotated["vibe_score"] = sentiment.get("vibe_score")
    annotated["vibe_label"] = sentiment.get("label")
    return annotated


def _dependency_missing_sentiment() -> dict:
    return {
        "status": "dependency_missing",
        "vibe_score": None,
        "label": None,
        "caption": None,
        "comments": None,
        "summary": {
            "scored_segments": 0,
            "comment_count_scored": 0,
        },
        "notes": [
            "Install vaderSentiment to enable local caption/comment sentiment analysis."
        ],
    }


def _score_post_sentiment(post: dict, analyzer: _SentimentAnalyzer) -> dict:
    caption_text = _clean_text(post.get("caption"))
    comment_texts = [
        text
        for text in (
            _clean_text(comment.get("text"))
            for comment in (post.get("comments") or [])
            if isinstance(comment, dict)
        )
        if text
    ]

    caption_scores = analyzer.polarity_scores(caption_text) if caption_text else None
    comment_scores = [analyzer.polarity_scores(text) for text in comment_texts]

    weighted_compounds: list[float] = []
    if caption_scores:
        weighted_compounds.extend([float(caption_scores.get("compound", 0.0))] * 2)
    weighted_compounds.extend(float(score.get("compound", 0.0)) for score in comment_scores)

    if not weighted_compounds:
        return {
            "status": "no_text",
            "vibe_score": None,
            "label": None,
            "caption": None,
            "comments": None,
            "summary": {
                "scored_segments": 0,
                "comment_count_scored": 0,
            },
            "notes": [
                "No caption or comment text was available to score."
            ],
        }

    vibe_score = round(sum(weighted_compounds) / len(weighted_compounds), 4)
    comment_compounds = [float(score.get("compound", 0.0)) for score in comment_scores]
    return {
        "status": "ready",
        "vibe_score": vibe_score,
        "label": _vibe_label(vibe_score),
        "caption": _score_summary(caption_scores),
        "comments": {
            "count_scored": len(comment_scores),
            "average_compound": round(sum(comment_compounds) / len(comment_compounds), 4)
            if comment_compounds else None,
            "most_positive_compound": round(max(comment_compounds), 4) if comment_compounds else None,
            "most_negative_compound": round(min(comment_compounds), 4) if comment_compounds else None,
        },
        "summary": {
            "scored_segments": len(weighted_compounds),
            "comment_count_scored": len(comment_scores),
        },
        "notes": [
            "Caption compound is weighted 2x relative to each archived comment.",
            "Scores use local VADER sentiment analysis only.",
        ],
    }


def _score_repost_sentiment(repost: dict, analyzer: _SentimentAnalyzer) -> dict:
    """Score metadata-only repost sentiment from the archived original caption only."""
    caption_text = _clean_text(repost.get("original_caption") if "original_caption" in repost else repost.get("caption"))
    if not caption_text:
        return {
            "status": "no_text",
            "vibe_score": None,
            "label": None,
            "caption": None,
            "comments": None,
            "summary": {
                "scored_segments": 0,
                "comment_count_scored": 0,
            },
            "notes": [
                "No original caption text was available to score for this repost.",
            ],
        }

    caption_scores = analyzer.polarity_scores(caption_text)
    vibe_score = round(float(caption_scores.get("compound", 0.0)), 4)
    return {
        "status": "ready",
        "vibe_score": vibe_score,
        "label": _vibe_label(vibe_score),
        "caption": _score_summary(caption_scores),
        "comments": None,
        "summary": {
            "scored_segments": 1,
            "comment_count_scored": 0,
        },
        "notes": [
            "Repost vibe is scored from the archived original caption only.",
            "Scores use local VADER sentiment analysis only.",
        ],
    }


def _classify_repost_behavior(repost: dict) -> dict:
    """Return a lightweight keyword-based category for a repost caption."""
    caption_text = _clean_text(repost.get("original_caption") if "original_caption" in repost else repost.get("caption"))
    tokens = _tokenize_text(caption_text)
    token_set = set(tokens)
    hashtags = {token.lstrip("#") for token in _HASHTAG_RE.findall(caption_text)}
    token_set.update(hashtags)

    scores: dict[str, int] = {}
    for category, keywords in _REPOST_CATEGORY_KEYWORDS.items():
        score = sum(1 for keyword in keywords if keyword in token_set or keyword in caption_text.lower())
        if score:
            scores[category] = score

    if not scores:
        return {
            "behavior_category": "other",
            "behavior_confidence": 0.35 if caption_text else 0.0,
            "classifier_version": REPOST_CLASSIFIER_VERSION,
        }

    sorted_scores = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    top_category, top_score = sorted_scores[0]
    runner_up = sorted_scores[1][1] if len(sorted_scores) > 1 else 0
    confidence = 0.5 + min(0.45, ((top_score - runner_up) * 0.15) + (top_score * 0.05))
    return {
        "behavior_category": top_category,
        "behavior_confidence": round(min(confidence, 0.95), 2),
        "classifier_version": REPOST_CLASSIFIER_VERSION,
    }


def _score_summary(scores: dict[str, float] | None) -> dict[str, float] | None:
    if not scores:
        return None
    return {
        "compound": round(float(scores.get("compound", 0.0)), 4),
        "positive": round(float(scores.get("pos", 0.0)), 4),
        "neutral": round(float(scores.get("neu", 0.0)), 4),
        "negative": round(float(scores.get("neg", 0.0)), 4),
    }


def _vibe_label(score: float | None) -> str | None:
    if score is None:
        return None
    if score >= 0.2:
        return "positive"
    if score <= -0.2:
        return "negative"
    return "neutral"


def _build_sentiment_summary(posts: list[dict]) -> dict:
    scored_posts = []
    for post in posts:
        sentiment = post.get("sentiment")
        if isinstance(sentiment, dict) and sentiment.get("status") == "ready":
            vibe_score = sentiment.get("vibe_score")
            if isinstance(vibe_score, (int, float)):
                scored_posts.append((post, float(vibe_score)))

    if not posts:
        return {
            "status": "no_posts",
            "average_vibe_score": None,
            "posts_scored": 0,
            "positive_posts": 0,
            "neutral_posts": 0,
            "negative_posts": 0,
            "most_positive_post_id": None,
            "most_negative_post_id": None,
        }

    dependency_missing = any(
        isinstance(post.get("sentiment"), dict) and post["sentiment"].get("status") == "dependency_missing"
        for post in posts
    )
    if dependency_missing and not scored_posts:
        return {
            "status": "dependency_missing",
            "average_vibe_score": None,
            "posts_scored": 0,
            "positive_posts": 0,
            "neutral_posts": 0,
            "negative_posts": 0,
            "most_positive_post_id": None,
            "most_negative_post_id": None,
        }

    if not scored_posts:
        return {
            "status": "no_text",
            "average_vibe_score": None,
            "posts_scored": 0,
            "positive_posts": 0,
            "neutral_posts": 0,
            "negative_posts": 0,
            "most_positive_post_id": None,
            "most_negative_post_id": None,
        }

    labels = [_vibe_label(score) for _, score in scored_posts]
    most_positive_post, positive_score = max(scored_posts, key=lambda entry: entry[1])
    most_negative_post, negative_score = min(scored_posts, key=lambda entry: entry[1])
    return {
        "status": "ready",
        "average_vibe_score": round(sum(score for _, score in scored_posts) / len(scored_posts), 4),
        "posts_scored": len(scored_posts),
        "positive_posts": sum(1 for label in labels if label == "positive"),
        "neutral_posts": sum(1 for label in labels if label == "neutral"),
        "negative_posts": sum(1 for label in labels if label == "negative"),
        "most_positive_post_id": most_positive_post.get("post_id"),
        "most_positive_score": round(positive_score, 4),
        "most_negative_post_id": most_negative_post.get("post_id"),
        "most_negative_score": round(negative_score, 4),
    }


def _build_repost_analytics(reposts: list[dict], generated_at: str) -> dict:
    if not reposts:
        return {
            "status": "no_reposts",
            "total_reposts": 0,
            "average_vibe_score": None,
            "top_categories": [],
            "high_frequency_sources": [],
            "top_sources": [],
            "rolling_windows": {},
            "deviations": [],
            "deviation_flag_count": 0,
            "active_window": "all_time",
        }

    generated_dt = _parse_iso_datetime(generated_at) or datetime.now(timezone.utc)
    repost_entries = []
    for repost in reposts:
        timestamp = _pick_repost_timestamp(repost)
        repost_entries.append({
            "item": repost,
            "timestamp": timestamp,
            "source_key": _repost_source_key(repost),
        })

    category_breakdown = _repost_category_breakdown(reposts)
    active_window = _select_repost_active_window(repost_entries, generated_dt)
    top_sources, high_frequency_sources = _repost_source_summary(
        repost_entries,
        generated_dt,
        active_window=active_window,
    )
    rolling_windows = _repost_window_summary(repost_entries, generated_dt)
    deviations = _detect_repost_pattern_deviations(repost_entries, generated_dt)

    scored = [
        float(repost.get("vibe_score"))
        for repost in reposts
        if isinstance(repost.get("vibe_score"), (int, float))
    ]
    return {
        "status": "ready",
        "total_reposts": len(reposts),
        "average_vibe_score": round(sum(scored) / len(scored), 4) if scored else None,
        "scored_reposts": len(scored),
        "top_categories": category_breakdown[:5],
        "high_frequency_sources": high_frequency_sources[:10],
        "top_sources": top_sources[:10],
        "rolling_windows": rolling_windows,
        "deviations": deviations,
        "deviation_flag_count": sum(1 for entry in deviations if entry.get("flagged")),
        "active_window": active_window,
    }


def _repost_category_breakdown(reposts: list[dict]) -> list[dict]:
    counts = Counter(
        str(repost.get("behavior_category") or "other")
        for repost in reposts
    )
    total = max(len(reposts), 1)
    return [
        {
            "category": category,
            "count": count,
            "share": round(count / total, 4),
        }
        for category, count in counts.most_common()
    ]


def _select_repost_active_window(repost_entries: list[dict], generated_dt: datetime) -> str:
    last_30 = sum(1 for entry in repost_entries if _within_days(entry.get("timestamp"), generated_dt, 30))
    if last_30 >= 5:
        return "30d"
    last_90 = sum(1 for entry in repost_entries if _within_days(entry.get("timestamp"), generated_dt, 90))
    if last_90 >= 5:
        return "90d"
    return "all_time"


def _repost_source_summary(
    repost_entries: list[dict],
    generated_dt: datetime,
    *,
    active_window: str,
) -> tuple[list[dict], list[dict]]:
    grouped: dict[str, dict[str, Any]] = {}
    windows = {
        "30d": lambda dt: _within_days(dt, generated_dt, 30),
        "90d": lambda dt: _within_days(dt, generated_dt, 90),
        "all_time": lambda dt: True,
    }
    totals = {
        "30d": sum(1 for entry in repost_entries if windows["30d"](entry.get("timestamp"))),
        "90d": sum(1 for entry in repost_entries if windows["90d"](entry.get("timestamp"))),
        "all_time": len(repost_entries),
    }

    for entry in repost_entries:
        repost = entry["item"]
        source_key = entry["source_key"]
        if source_key.endswith(":unknown"):
            continue
        source = grouped.setdefault(
            source_key,
            {
                "source_username": repost.get("source_username") or repost.get("repost_source_account"),
                "source_display_name": repost.get("source_display_name"),
                "source_platform": repost.get("source_platform") or repost.get("platform"),
                "source_user_id": repost.get("source_user_id"),
                "source_url": repost.get("source_url") or repost.get("repost_source_url"),
                "count_all_time": 0,
                "windows": {},
            },
        )
        source["count_all_time"] += 1

    for label, predicate in windows.items():
        window_counts = Counter(
            entry["source_key"]
            for entry in repost_entries
            if predicate(entry.get("timestamp"))
        )
        total = max(totals[label], 1)
        for source_key, source in grouped.items():
            count = int(window_counts.get(source_key, 0))
            source["windows"][label] = {
                "count": count,
                "share": round(count / total, 4) if totals[label] else 0.0,
            }

    sources = sorted(
        grouped.values(),
        key=lambda item: (-item["windows"][active_window]["count"], -item["count_all_time"], str(item.get("source_username") or "").lower()),
    )
    high_frequency = [
        {
            **source,
            "high_frequency": (
                source["windows"][active_window]["count"] >= 3
                and source["windows"][active_window]["share"] >= 0.15
            ),
        }
        for source in sources
    ]
    return sources, [source for source in high_frequency if source["high_frequency"]]


def _repost_window_summary(repost_entries: list[dict], generated_dt: datetime) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for label, days in (("7d", 7), ("30d", 30), ("90d", 90)):
        items = [
            entry["item"]
            for entry in repost_entries
            if _within_days(entry.get("timestamp"), generated_dt, days)
        ]
        vibe_scores = [
            float(item.get("vibe_score"))
            for item in items
            if isinstance(item.get("vibe_score"), (int, float))
        ]
        result[label] = {
            "count": len(items),
            "average_vibe_score": round(sum(vibe_scores) / len(vibe_scores), 4) if vibe_scores else None,
        }
    return result


def _detect_repost_pattern_deviations(repost_entries: list[dict], generated_dt: datetime) -> list[dict]:
    return [
        _detect_repost_volume_spike(repost_entries, generated_dt),
        _detect_repost_vibe_shift(repost_entries, generated_dt),
        _detect_repost_source_shift(repost_entries, generated_dt),
    ]


def _detect_repost_volume_spike(repost_entries: list[dict], generated_dt: datetime) -> dict:
    recent_start = generated_dt - timedelta(days=7)
    baseline_start = generated_dt - timedelta(days=37)
    baseline_end = recent_start
    recent_count = sum(
        1 for entry in repost_entries
        if entry.get("timestamp") and entry["timestamp"] >= recent_start
    )
    baseline_daily: list[int] = []
    for day_offset in range(30):
        day_start = baseline_start + timedelta(days=day_offset)
        day_end = day_start + timedelta(days=1)
        baseline_daily.append(
            sum(
                1 for entry in repost_entries
                if entry.get("timestamp") and day_start <= entry["timestamp"] < day_end
            )
        )

    mean_daily = mean(baseline_daily) if baseline_daily else 0.0
    stddev_daily = pstdev(baseline_daily) if len(baseline_daily) > 1 else 0.0
    threshold = (mean_daily * 7) + (2 * stddev_daily * 7)
    flagged = recent_count >= 3 and recent_count > threshold
    return {
        "kind": "volume_spike",
        "flagged": flagged,
        "recent_7d_count": recent_count,
        "baseline_30d_daily_mean": round(mean_daily, 4),
        "baseline_30d_daily_stddev": round(stddev_daily, 4),
        "threshold_7d": round(threshold, 4),
        "window_start": _iso(recent_start),
        "baseline_start": _iso(baseline_start),
        "baseline_end": _iso(baseline_end),
    }


def _detect_repost_vibe_shift(repost_entries: list[dict], generated_dt: datetime) -> dict:
    recent_start = generated_dt - timedelta(days=14)
    baseline_start = generated_dt - timedelta(days=104)
    recent_scores = [
        float(entry["item"].get("vibe_score"))
        for entry in repost_entries
        if entry.get("timestamp")
        and entry["timestamp"] >= recent_start
        and isinstance(entry["item"].get("vibe_score"), (int, float))
    ]
    baseline_scores = [
        float(entry["item"].get("vibe_score"))
        for entry in repost_entries
        if entry.get("timestamp")
        and baseline_start <= entry["timestamp"] < recent_start
        and isinstance(entry["item"].get("vibe_score"), (int, float))
    ]
    if len(recent_scores) < 5 or len(baseline_scores) < 5:
        return {
            "kind": "vibe_shift",
            "flagged": False,
            "status": "insufficient_history",
            "recent_count": len(recent_scores),
            "baseline_count": len(baseline_scores),
        }

    recent_avg = sum(recent_scores) / len(recent_scores)
    baseline_avg = sum(baseline_scores) / len(baseline_scores)
    shift = recent_avg - baseline_avg
    return {
        "kind": "vibe_shift",
        "flagged": abs(shift) >= 0.25,
        "status": "ready",
        "recent_count": len(recent_scores),
        "baseline_count": len(baseline_scores),
        "recent_average_vibe": round(recent_avg, 4),
        "baseline_average_vibe": round(baseline_avg, 4),
        "difference": round(shift, 4),
        "window_start": _iso(recent_start),
        "baseline_start": _iso(baseline_start),
    }


def _detect_repost_source_shift(repost_entries: list[dict], generated_dt: datetime) -> dict:
    recent_entries = [
        entry for entry in repost_entries
        if _within_days(entry.get("timestamp"), generated_dt, 30)
        and not entry["source_key"].endswith(":unknown")
    ]
    if len(recent_entries) < 5:
        return {
            "kind": "source_shift",
            "flagged": False,
            "status": "insufficient_history",
            "recent_30d_count": len(recent_entries),
        }
    counts = Counter(entry["source_key"] for entry in recent_entries)
    top_source, top_count = counts.most_common(1)[0]
    share = top_count / len(recent_entries)
    sample = next((entry["item"] for entry in recent_entries if entry["source_key"] == top_source), {})
    return {
        "kind": "source_shift",
        "flagged": share > 0.30,
        "status": "ready",
        "recent_30d_count": len(recent_entries),
        "source_username": sample.get("source_username") or sample.get("repost_source_account"),
        "source_platform": sample.get("source_platform") or sample.get("platform"),
        "source_share": round(share, 4),
        "source_count": top_count,
    }


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split()).strip()


def _tokenize_text(value: str) -> list[str]:
    if not value:
        return []
    return [token for token in re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿĀ-ž0-9_#]+", value.lower()) if token]


def _get_vader_analyzer() -> _SentimentAnalyzer | None:
    try:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    except Exception:
        return None
    return SentimentIntensityAnalyzer()


def _graph_node_id(platform: str, *, username: str, user_id: Any) -> str:
    if user_id:
        return f"{platform}:user_id:{user_id}"
    return f"{platform}:username:{username}"


_LOCATION_PREPOSITION_RE = re.compile(
    r"(?i)\b(?:in|at|from|near|around|inside|outside|to|into|v|ve|na|do|z|ze|u|pod|nad)\b\s+"
    r"([A-Za-zÀ-ÖØ-öø-ÿĀ-ž][A-Za-zÀ-ÖØ-öø-ÿĀ-ž'’.-]*(?:\s+[A-Za-zÀ-ÖØ-öø-ÿĀ-ž][A-Za-zÀ-ÖØ-öø-ÿĀ-ž'’.-]*){0,2})"
)
_HASHTAG_RE = re.compile(r"#([A-Za-zÀ-ÖØ-öø-ÿĀ-ž0-9_]{3,})")
_URL_RE = re.compile(r"(?:(?:https?://)?(?:www\.)?)(instagram\.com|instagr\.am|tiktok\.com|facebook\.com|fb\.com)/([^\s/?#]+(?:/[^\s?#]+)?)", re.I)
_PLATFORM_HANDLE_PATTERNS = {
    "instagram": re.compile(r"(?i)\b(?:instagram|insta|ig)\b[\s:/-]*@?([A-Za-z0-9._]{2,})"),
    "tiktok": re.compile(r"(?i)\b(?:tiktok|tt)\b[\s:/-]*@?([A-Za-z0-9._]{2,})"),
    "facebook": re.compile(r"(?i)\b(?:facebook|fb)\b[\s:/-]*@?([A-Za-z0-9._]{2,})"),
}
_PLACE_STOPWORDS = {
    "summer", "winter", "spring", "autumn", "vibes", "friends", "family", "school",
    "gym", "home", "work", "party", "vacation", "holiday", "beach", "night", "today",
    "tonight", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
}
_KNOWN_LOCATION_KEYS = {
    "prague", "praha", "brno", "ostrava", "plzen", "pilsen", "liberec", "olomouc",
    "pardubice", "hradec kralove", "ceske budejovice", "karlovy vary",
    "czech republic", "czechrepublic", "ceskarepublika", "slovakia", "bratislava",
    "vienna", "paris", "rome", "milan", "italy", "venice", "florence", "sicily",
    "london", "england", "berlin", "munich", "germany", "amsterdam", "netherlands",
    "barcelona", "madrid", "spain", "mallorca", "ibiza", "greece", "athens", "crete",
    "croatia", "split", "dubrovnik", "zadar", "usa", "new york", "newyork",
    "los angeles", "losangeles", "california", "bali", "thailand", "phuket",
    "singapore", "tokyo", "japan", "austria", "france",
}


def _build_geoparsed_location_summary(
    *,
    username: str,
    bio: dict,
    posts: list[dict],
    stories: list[dict],
) -> dict:
    all_candidates: list[dict] = []
    for post in posts:
        all_candidates.extend(_expand_location_candidates(post.get("geoparsed_locations") or []))
    for story in stories:
        all_candidates.extend(_expand_location_candidates(story.get("geoparsed_locations") or []))
    bio_candidates = _extract_text_location_candidates(
        text=bio.get("bio_text"),
        source_type="bio_text",
        item_kind="profile",
        item_id=username,
        occurred_at=bio.get("snapshot_at"),
    )
    all_candidates.extend(_aggregate_location_candidates(bio_candidates))

    if not all_candidates:
        return {
            "status": "no_signals",
            "top_locations": [],
            "total_locations": 0,
            "source_counts": {},
            "scanned_posts": len(posts),
            "scanned_stories": len(stories),
        }

    aggregated = _aggregate_location_candidates(all_candidates)
    source_counts = Counter()
    for candidate in aggregated:
        for source in candidate.get("source_types") or []:
            source_counts[source] += 1
    return {
        "status": "ready",
        "top_locations": aggregated[:10],
        "total_locations": len(aggregated),
        "source_counts": dict(source_counts),
        "scanned_posts": len(posts),
        "scanned_stories": len(stories),
    }


def _expand_location_candidates(entries: list[dict]) -> list[dict]:
    expanded: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("source_type"):
            expanded.append(entry)
            continue

        source_types = entry.get("source_types") or [None]
        evidences = entry.get("evidence") or [entry.get("name")]
        items = entry.get("items") or [{"item_kind": None, "item_id": None, "occurred_at": None}]
        for source_type in source_types:
            for item in items:
                expanded.append(
                    {
                        "name": entry.get("name"),
                        "normalized_name": entry.get("normalized_name"),
                        "confidence": entry.get("max_confidence"),
                        "source_type": source_type,
                        "item_kind": item.get("item_kind") if isinstance(item, dict) else None,
                        "item_id": item.get("item_id") if isinstance(item, dict) else None,
                        "occurred_at": item.get("occurred_at") if isinstance(item, dict) else None,
                        "evidence": evidences[0] if evidences else entry.get("name"),
                        "metadata": entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {},
                    }
                )
    return expanded


def _extract_text_location_candidates(
    *,
    text: Any,
    source_type: str,
    item_kind: str,
    item_id: Any,
    occurred_at: Any,
) -> list[dict]:
    cleaned = _clean_text(text)
    if not cleaned:
        return []

    candidates: list[dict] = []
    for match in _LOCATION_PREPOSITION_RE.finditer(cleaned):
        phrase = _trim_location_phrase(match.group(1).strip(" .,!?:;"))
        normalized = _normalize_location_key(phrase)
        if not normalized or normalized in _PLACE_STOPWORDS:
            continue
        if not (_phrase_has_capitalized_token(phrase) or normalized in _KNOWN_LOCATION_KEYS):
            continue
        confidence = 0.62 if normalized in _KNOWN_LOCATION_KEYS else 0.5
        candidates.append(
            _location_candidate(
                name=_titleish_location_name(phrase),
                confidence=confidence,
                source_type=source_type,
                item_kind=item_kind,
                item_id=item_id,
                occurred_at=occurred_at,
                evidence=phrase,
            )
        )

    for hashtag in _HASHTAG_RE.findall(cleaned):
        candidates.extend(
            _extract_hashtag_location_candidates(
                hashtag=hashtag,
                source_type=f"{source_type}_hashtag",
                item_kind=item_kind,
                item_id=item_id,
                occurred_at=occurred_at,
            )
        )
    return candidates


def _extract_hashtag_location_candidates(
    hashtag: Any,
    *,
    source_type: str,
    item_kind: str,
    item_id: Any,
    occurred_at: Any,
) -> list[dict]:
    if not isinstance(hashtag, str):
        return []
    token = hashtag.lstrip("#").replace("_", " ").strip()
    normalized = _normalize_location_key(token)
    if not normalized or normalized in _PLACE_STOPWORDS:
        return []
    if normalized not in _KNOWN_LOCATION_KEYS and not _phrase_has_capitalized_token(token):
        return []
    confidence = 0.68 if normalized in _KNOWN_LOCATION_KEYS else 0.48
    return [
        _location_candidate(
            name=_titleish_location_name(token),
            confidence=confidence,
            source_type=source_type,
            item_kind=item_kind,
            item_id=item_id,
            occurred_at=occurred_at,
            evidence=f"#{hashtag.lstrip('#')}",
        )
    ]


def _extract_structured_story_location_candidates(
    *,
    metadata: dict,
    item_id: Any,
    item_kind: str,
    occurred_at: Any,
) -> list[dict]:
    candidates: list[dict] = []
    for entry in metadata.get("locations") or []:
        if not isinstance(entry, dict):
            continue
        raw_name = (
            entry.get("name")
            or entry.get("short_name")
            or entry.get("title")
            or entry.get("city")
            or entry.get("address")
        )
        if not isinstance(raw_name, str) or not raw_name.strip():
            continue
        candidates.append(
            _location_candidate(
                name=raw_name.strip(),
                confidence=0.97,
                source_type="story_location_tag",
                item_kind=item_kind,
                item_id=item_id,
                occurred_at=occurred_at,
                evidence=raw_name.strip(),
                metadata={
                    "facebook_places_id": entry.get("facebook_places_id"),
                    "address": entry.get("address"),
                    "city": entry.get("city"),
                    "lat": entry.get("lat"),
                    "lng": entry.get("lng"),
                },
            )
        )
    return candidates


def _aggregate_location_candidates(candidates: list[dict]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        normalized = candidate.get("normalized_name")
        if not normalized:
            continue
        group = grouped.setdefault(
            normalized,
            {
                "name": candidate.get("name"),
                "normalized_name": normalized,
                "occurrences": 0,
                "max_confidence": 0.0,
                "first_seen_at": candidate.get("occurred_at"),
                "last_seen_at": candidate.get("occurred_at"),
                "source_types": [],
                "evidence": [],
                "items": [],
                "metadata": {},
            },
        )
        group["occurrences"] += 1
        confidence = float(candidate.get("confidence") or 0.0)
        if confidence >= group["max_confidence"]:
            group["max_confidence"] = round(confidence, 2)
            if candidate.get("name"):
                group["name"] = candidate["name"]
        source_type = candidate.get("source_type")
        if source_type and source_type not in group["source_types"]:
            group["source_types"].append(source_type)
        evidence = candidate.get("evidence")
        if evidence and evidence not in group["evidence"]:
            group["evidence"].append(evidence)
        item_ref = {
            "item_kind": candidate.get("item_kind"),
            "item_id": candidate.get("item_id"),
            "occurred_at": candidate.get("occurred_at"),
        }
        if item_ref not in group["items"]:
            group["items"].append(item_ref)
        seen_at = candidate.get("occurred_at")
        if seen_at and (group["first_seen_at"] is None or seen_at < group["first_seen_at"]):
            group["first_seen_at"] = seen_at
        if seen_at and (group["last_seen_at"] is None or seen_at > group["last_seen_at"]):
            group["last_seen_at"] = seen_at
        metadata = candidate.get("metadata")
        if isinstance(metadata, dict):
            for key, value in metadata.items():
                if value not in (None, "", []):
                    group["metadata"].setdefault(key, value)

    return sorted(
        grouped.values(),
        key=lambda item: (-item["occurrences"], -item["max_confidence"], str(item["name"]).lower()),
    )


def _location_candidate(
    *,
    name: str,
    confidence: float,
    source_type: str,
    item_kind: str,
    item_id: Any,
    occurred_at: Any,
    evidence: str,
    metadata: dict | None = None,
) -> dict:
    return {
        "name": name,
        "normalized_name": _normalize_location_key(name),
        "confidence": round(confidence, 2),
        "source_type": source_type,
        "item_kind": item_kind,
        "item_id": str(item_id) if item_id is not None else None,
        "occurred_at": occurred_at,
        "evidence": evidence,
        "metadata": metadata or {},
    }


def _normalize_location_key(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKD", value)
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    normalized = normalized.lower()
    normalized = re.sub(r"[^a-z0-9\s-]", " ", normalized)
    normalized = re.sub(r"[\s_-]+", " ", normalized).strip()
    return normalized


def _phrase_has_capitalized_token(phrase: str) -> bool:
    return any(token[:1].isupper() for token in phrase.split() if token)


def _titleish_location_name(value: str) -> str:
    parts = [part for part in re.split(r"\s+", value.strip()) if part]
    return " ".join(part[:1].upper() + part[1:] if not part[:1].isupper() else part for part in parts)


def _trim_location_phrase(value: str) -> str:
    parts = [part for part in re.split(r"\s+", value.strip()) if part]
    kept: list[str] = []
    for part in parts:
        normalized = _normalize_location_key(part)
        if kept and not (part[:1].isupper() or normalized in _KNOWN_LOCATION_KEYS):
            break
        kept.append(part)
    return " ".join(kept)


def _build_cross_platform_links(
    *,
    platform: str,
    username: str,
    bio: dict,
    candidate_profiles: list[dict],
    data_root: Any = None,
) -> dict:
    current_bio = _clean_text((bio or {}).get("bio_text"))
    current_display_name = _clean_text((bio or {}).get("display_name")) or username
    current_normalized_username = _normalize_username_key(username)
    current_links = _extract_social_links(current_bio)
    current_handles = _extract_platform_handles(current_bio)
    matches: list[dict] = []

    for candidate in candidate_profiles:
        candidate_platform = candidate.get("platform")
        candidate_username = candidate.get("username")
        if candidate_platform == platform or not candidate_username:
            continue

        score = 0.0
        reasons: list[str] = []
        evidence: dict[str, Any] = {}
        candidate_bio = _clean_text(candidate.get("bio_snapshot"))
        candidate_links = _extract_social_links(candidate_bio)
        candidate_handles = _extract_platform_handles(candidate_bio)

        candidate_normalized_username = _normalize_username_key(candidate_username)
        if current_normalized_username and current_normalized_username == candidate_normalized_username:
            score = max(score, 0.88)
            reasons.append("normalized_username_exact")

        # Fuzzy username matching across all normalized forms
        current_forms = _username_forms(username)
        candidate_forms = _username_forms(candidate_username)

        # Exact match on any normalized form pair (e.g. stripped or trailing-repeat-collapsed)
        if not any(r == "normalized_username_exact" for r in reasons):
            for cf in current_forms:
                for cdf in candidate_forms:
                    if cf and cdf and cf == cdf and cf != current_normalized_username:
                        score = max(score, 0.82)
                        reasons.append("fuzzy_username_form_exact")
                        evidence["current_username_form"] = cf
                        evidence["candidate_username_form"] = cdf
                        break

        # Similarity across the best-matching form pair
        best_username_sim = max(
            SequenceMatcher(None, cf, cdf).ratio()
            for cf in current_forms
            for cdf in candidate_forms
        )
        display_similarity = SequenceMatcher(
            None,
            _normalize_location_key(current_display_name),
            _normalize_location_key(candidate.get("display_name") or candidate_username),
        ).ratio()
        if best_username_sim >= 0.78 and display_similarity >= 0.60:
            score = max(score, 0.70)
            reasons.append("username_display_similarity")
            evidence["username_similarity"] = round(best_username_sim, 2)

        # Display-name-only match (no username signal needed if names are very close)
        if display_similarity >= 0.85 and score < 0.70:
            score = max(score, 0.65)
            reasons.append("display_name_similarity")
            evidence["display_similarity"] = round(display_similarity, 2)

        current_link = _matching_social_link(current_links, candidate_platform, candidate_username)
        if current_link:
            score = max(score, 0.98)
            reasons.append("current_bio_links_to_candidate")
            evidence["current_bio_link"] = current_link["url"]

        reciprocal_link = _matching_social_link(candidate_links, platform, username)
        if reciprocal_link:
            score = max(score, 0.94)
            reasons.append("candidate_bio_links_back")
            evidence["candidate_bio_link"] = reciprocal_link["url"]

        current_handle = _matching_platform_handle(current_handles, candidate_platform, candidate_username)
        if current_handle:
            score = max(score, 0.84)
            reasons.append("current_bio_mentions_candidate_handle")
            evidence["current_bio_handle"] = current_handle

        reciprocal_handle = _matching_platform_handle(candidate_handles, platform, username)
        if reciprocal_handle:
            score = max(score, 0.8)
            reasons.append("candidate_bio_mentions_current_handle")
            evidence["candidate_bio_handle"] = reciprocal_handle

        if score < 0.55:
            continue

        matches.append(
            {
                "platform": candidate_platform,
                "username": candidate_username,
                "display_name": candidate.get("display_name") or candidate_username,
                "confidence": round(score, 2),
                "confidence_label": _confidence_label(score),
                "reasons": sorted(set(reasons)),
                "evidence": evidence,
            }
        )

    matches.sort(key=lambda item: (-item["confidence"], item["platform"], item["username"]))
    deduped: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for match in matches:
        key = (match["platform"], match["username"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(match)

    # Enrich with facial recognition scores where avatars exist
    if data_root is not None and deduped:
        try:
            from pathlib import Path as _Path
            from src.face_matcher import enrich_matches_with_face_scores
            enrich_matches_with_face_scores(
                deduped, platform, username, _Path(data_root)
            )
            # Re-sort after potential confidence boosts
            deduped.sort(key=lambda item: (-item["confidence"], item["platform"], item["username"]))
        except Exception:
            pass  # face matching is optional; never block intelligence build

    return {
        "status": "ready" if deduped else "no_matches",
        "match_count": len(deduped),
        "matches": deduped,
        "evaluated_profiles": len([candidate for candidate in candidate_profiles if candidate.get("platform") != platform]),
    }


def _normalize_username_key(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _strip_username(value: Any) -> str:
    """Lowercase and strip all separator characters (., _, -, space)."""
    if not isinstance(value, str):
        return ""
    return re.sub(r"[._\-\s]", "", value.lower())


def _normalize_trailing_repeat(value: str) -> str:
    """
    Collapse trailing repeated characters that platforms use to fake uniqueness.
    E.g. 'vorlovaaa' → 'vorlova', 'dolezalovaae' → 'dolezalova'.
    Strategy:
      1. If the string ends with 2+ identical chars, strip the extras.
      2. If the string ends with a single 'e' and the char before it is also
         the last char of the root (common TikTok convention), strip it.
    """
    if not value:
        return value
    # Strip trailing repeated single char (aaa → a, eee → e)
    m = re.search(r"(.)\1+$", value)
    if m:
        value = value[:m.start()] + m.group(1)
    # Strip single trailing 'e' if it looks like a suffix  (dolezalovaae → after
    # step above gives dolezalovae → strip trailing 'e' → dolezalova)
    if len(value) > 3 and value.endswith("e") and value[-2] != "e":
        value = value[:-1]
    return value


def _username_forms(username: str) -> list[str]:
    """Return the distinct normalized forms of a username for fuzzy comparison."""
    raw = username.lower()
    stripped = _strip_username(username)
    normalized = _normalize_trailing_repeat(stripped)
    forms = []
    for f in (raw, stripped, normalized):
        if f and f not in forms:
            forms.append(f)
    return forms


def _extract_social_links(text: str) -> list[dict]:
    results: list[dict] = []
    for domain, path in _URL_RE.findall(text or ""):
        platform = _platform_for_domain(domain)
        if not platform:
            continue
        username = _username_from_social_path(platform, path)
        if not username:
            continue
        url = f"https://{domain}/{path}"
        results.append({"platform": platform, "username": username, "url": url})
    return results


def _extract_platform_handles(text: str) -> dict[str, list[str]]:
    results = {platform: [] for platform in _PLATFORM_HANDLE_PATTERNS}
    for platform, pattern in _PLATFORM_HANDLE_PATTERNS.items():
        for username in pattern.findall(text or ""):
            if username not in results[platform]:
                results[platform].append(username)
    return results


def _matching_social_link(links: list[dict], platform: str, username: str) -> dict | None:
    target_key = _normalize_username_key(username)
    for link in links:
        if link.get("platform") != platform:
            continue
        if _normalize_username_key(link.get("username")) == target_key:
            return link
    return None


def _matching_platform_handle(handles: dict[str, list[str]], platform: str, username: str) -> str | None:
    target_key = _normalize_username_key(username)
    for handle in handles.get(platform) or []:
        if _normalize_username_key(handle) == target_key:
            return handle
    return None


def _platform_for_domain(domain: str) -> str | None:
    lowered = domain.lower()
    if lowered in {"instagram.com", "instagr.am"}:
        return "instagram"
    if lowered == "tiktok.com":
        return "tiktok"
    if lowered in {"facebook.com", "fb.com"}:
        return "facebook"
    return None


def _username_from_social_path(platform: str, path: str) -> str | None:
    cleaned = str(path or "").strip("/ ")
    if not cleaned:
        return None
    first = cleaned.split("/", 1)[0]
    if platform == "tiktok":
        first = first.lstrip("@")
    if platform == "instagram" and first in {"stories", "reel", "p", "tv"}:
        return None
    if platform == "facebook" and first in {"profile.php", "share", "watch"}:
        return None
    return first or None


def _confidence_label(score: float) -> str:
    if score >= 0.9:
        return "very_high"
    if score >= 0.78:
        return "high"
    if score >= 0.65:
        return "medium"
    return "low"


# ─── Social Proximity Scoring ─────────────────────────────────────────────────

def compute_proximity_scores(conn, target_id: int) -> dict[str, int]:
    """
    Compute a proximity score for each user that appears in the target's
    follower/following snapshots or comment history.

    Scoring rules:
        +3 mutual follow (appears in both followers and following)
        +2 per additional snapshot appearance beyond the first
        +2 if the target follows them (stronger signal)
        +2 if the user has commented on any of the target's posts
    Returns {username: score} sorted highest-first.
    """
    import json as _json
    from collections import defaultdict

    scores: dict[str, int] = defaultdict(int)
    follower_users: set[str] = set()
    following_users: set[str] = set()

    # Fetch all snapshot entries for the target
    rows = conn.execute("""
        SELECT snapshot_type, username
        FROM target_follow_snapshots
        WHERE target_id=? AND username IS NOT NULL
    """, (target_id,)).fetchall()

    snapshot_counts: dict[str, dict[str, int]] = {
        "followers": defaultdict(int),
        "following": defaultdict(int),
    }
    for row in rows:
        stype = row["snapshot_type"]
        uname = row["username"]
        if stype in snapshot_counts:
            snapshot_counts[stype][uname] += 1
            if stype == "followers":
                follower_users.add(uname)
            else:
                following_users.add(uname)

    all_users = follower_users | following_users

    for uname in all_users:
        # Mutual follow
        if uname in follower_users and uname in following_users:
            scores[uname] += 3
        # Target follows them (strong signal)
        if uname in following_users:
            scores[uname] += 2
        # Extra appearances in snapshots (persistence across time)
        extra_follower = max(snapshot_counts["followers"].get(uname, 0) - 1, 0)
        extra_following = max(snapshot_counts["following"].get(uname, 0) - 1, 0)
        scores[uname] += extra_follower * 2 + extra_following * 2

    # Comments on target's posts
    comment_rows = conn.execute("""
        SELECT DISTINCT pc.username
        FROM post_comments pc
        JOIN posts p ON p.id = pc.post_id
        WHERE p.target_id=? AND pc.username IS NOT NULL
    """, (target_id,)).fetchall()
    for row in comment_rows:
        uname = row["username"]
        if uname:
            scores[uname] += 2

    return dict(sorted(scores.items(), key=lambda kv: kv[1], reverse=True))


# ─── Vibe Shift Detection ─────────────────────────────────────────────────────

def check_vibe_shift(
    conn,
    target_id: int,
    threshold: float = 2.5,
) -> tuple[bool, float, str]:
    """
    Detect a significant mood shift in a target's recent repost AI analyses.

    Algorithm:
        1. Fetch last 14 analyses with mood_score (1-10 scale).
        2. Split into oldest 7 and newest 7.
        3. Compute delta = avg(newest7) - avg(oldest7).
        4. If abs(delta) > threshold, return (True, delta, direction).

    Returns (shifted, delta, direction) where direction is 'positive' or 'negative'.
    """
    import json as _json

    rows = conn.execute("""
        SELECT ai_analysis_json, COALESCE(reposted_at, discovered_at) AS ts
        FROM repost_history
        WHERE target_id=? AND ai_analysis_json IS NOT NULL
        ORDER BY ts DESC
        LIMIT 14
    """, (target_id,)).fetchall()

    if len(rows) < 8:
        return False, 0.0, ""

    scores: list[float] = []
    for row in rows:
        try:
            analysis = _json.loads(row["ai_analysis_json"])
            score = float(analysis.get("mood_score") or 0)
            if 1.0 <= score <= 10.0:
                scores.append(score)
        except Exception:
            continue

    if len(scores) < 8:
        return False, 0.0, ""

    # Rows are newest-first, so oldest = scores[-7:], newest = scores[:7]
    oldest_avg = sum(scores[-7:]) / 7
    newest_avg = sum(scores[:7]) / 7
    delta = newest_avg - oldest_avg

    if abs(delta) >= threshold:
        direction = "positive" if delta > 0 else "negative"
        return True, delta, direction

    return False, delta, ""
