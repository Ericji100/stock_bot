"""Small topic dedupe helpers for change-pack generation."""
from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any

from .topic_models import TopicActionType


def decide_topic_action_type(candidate: dict[str, Any], existing_profiles: list[Any]) -> tuple[TopicActionType, str | None]:
    """Return action type and matched theme_id for a candidate.

    The current policy is conservative: update close matches, create otherwise.
    It intentionally avoids automatic merge actions.
    """
    candidate_id = str(candidate.get("theme_id") or "").strip()
    candidate_name = str(candidate.get("theme_name") or "").strip()
    candidate_keywords = _keyword_set(candidate.get("keywords"))

    for profile in existing_profiles or []:
        profile_id = str(getattr(profile, "theme_id", "") or _get(profile, "theme_id") or "").strip()
        if candidate_id and profile_id and candidate_id == profile_id:
            return TopicActionType.UPDATE_THEME, profile_id

    best_id: str | None = None
    best_score = 0.0
    for profile in existing_profiles or []:
        profile_id = str(getattr(profile, "theme_id", "") or _get(profile, "theme_id") or "").strip()
        profile_name = str(getattr(profile, "theme_name", "") or _get(profile, "theme_name") or "").strip()
        profile_keywords = _keyword_set(getattr(profile, "keywords", None) or _get(profile, "keywords"))
        name_score = SequenceMatcher(None, candidate_name, profile_name).ratio() if candidate_name and profile_name else 0.0
        keyword_score = _jaccard(candidate_keywords, profile_keywords)
        score = max(name_score, keyword_score)
        if score > best_score:
            best_score = score
            best_id = profile_id

    if best_id and best_score >= 0.62:
        return TopicActionType.UPDATE_THEME, best_id
    return TopicActionType.CREATE_THEME, None


def _get(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, dict) else None


def _keyword_set(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {str(item).strip().lower() for item in value if str(item).strip()}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
