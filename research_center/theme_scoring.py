"""Theme relation scoring engine.

Scoring rules:
- Official evidence: 0-30 (L1=30, L2=20, L3=10)
- Mainstream media & industry: 0-20 (L2=20, L3=10)
- Product/supply chain relation: 0-20 (specific name=20, vague=10)
- Revenue/financial verifiability: 0-15 (with numbers=15, estimate=10, none=0)
- Market heat & group resonance: 0-10
- Counter-evidence penalty: up to -20
- Confidence levels:
  - >=75: high
  - 60-74: medium
  - 40-59: watch_only
  - <40: reject
- L3 community source cannot independently generate high confidence
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .theme_models import ThemeConfidence, ThemeEvidence, ThemeRelationType, ThemeSourceLevel


@dataclass
class ThemeScoreBreakdown:
    official_score: float = 0.0
    media_score: float = 0.0
    supply_chain_score: float = 0.0
    financial_score: float = 0.0
    heat_score: float = 0.0
    counter_penalty: float = 0.0
    total: float = 0.0
    confidence: ThemeConfidence = ThemeConfidence.MEDIUM
    relation_type: ThemeRelationType = ThemeRelationType.UNCLEAR


def theme_relation_score(
    evidence_list: list[ThemeEvidence | dict[str, Any]],
    supply_chain_hits: list[str] | None = None,
    financial_verifiable: bool = False,
    financial_has_numbers: bool = False,
    market_heat_score: float = 0.0,
    counter_evidence_count: int = 0,
    counter_evidence_strength: float = 0.0,
) -> ThemeScoreBreakdown:
    """Calculate theme relation score from evidence list and metadata."""

    official_score = 0.0
    media_score = 0.0
    supply_chain_score = 0.0
    financial_score = 0.0

    for ev in evidence_list:
        if isinstance(ev, dict):
            src_level = ThemeSourceLevel(ev.get("source_level", "L2_media"))
            contrib = float(ev.get("score_contribution", 0.0))
        else:
            src_level = ev.source_level
            contrib = ev.score_contribution

        if src_level == ThemeSourceLevel.L1_OFFICIAL:
            official_score = max(official_score, 30.0 * (contrib / 30.0) if contrib else 30.0)
        elif src_level == ThemeSourceLevel.L2_MEDIA:
            media_score = max(media_score, 20.0 * (contrib / 20.0) if contrib else 20.0)
        elif src_level == ThemeSourceLevel.L3_COMMUNITY:
            media_score = max(media_score, 10.0 * (contrib / 10.0) if contrib else 10.0)

    # Supply chain score
    if supply_chain_hits:
        has_specific = any(h.strip() for h in supply_chain_hits)
        supply_chain_score = 20.0 if has_specific else 10.0

    # Financial score
    if financial_verifiable:
        financial_score = 15.0 if financial_has_numbers else 10.0

    # Market heat
    heat_score = min(10.0, max(0.0, market_heat_score))

    # Counter evidence penalty
    counter_penalty = 0.0
    if counter_evidence_count > 0:
        counter_penalty = min(20.0, counter_evidence_count * counter_evidence_strength * 10.0)

    total = official_score + media_score + supply_chain_score + financial_score + heat_score - counter_penalty
    total = max(0.0, min(100.0, total))

    # Determine confidence
    confidence = ThemeConfidence.from_score(total)

    # L3 cannot independently produce high confidence
    has_l3_only = all(
        (ThemeSourceLevel(e.get("source_level", "L2_media")) == ThemeSourceLevel.L3_COMMUNITY
         if isinstance(e, dict) else e.source_level == ThemeSourceLevel.L3_COMMUNITY)
        for e in evidence_list
    )
    if has_l3_only and confidence == ThemeConfidence.HIGH:
        confidence = ThemeConfidence.MEDIUM

    # Determine relation type
    relation_type = ThemeRelationType.UNCLEAR
    if official_score >= 20 and supply_chain_score >= 15:
        relation_type = ThemeRelationType.DIRECT_PRODUCT
    elif supply_chain_score >= 15:
        relation_type = ThemeRelationType.SUPPLY_CHAIN
    elif official_score >= 20:
        relation_type = ThemeRelationType.BRAND_OWNER
    elif media_score >= 10:
        relation_type = ThemeRelationType.INDIRECT

    return ThemeScoreBreakdown(
        official_score=official_score,
        media_score=media_score,
        supply_chain_score=supply_chain_score,
        financial_score=financial_score,
        heat_score=heat_score,
        counter_penalty=counter_penalty,
        total=total,
        confidence=confidence,
        relation_type=relation_type,
    )


def freshness_score(last_updated: str | None, days_threshold: int = 30) -> float:
    """Calculate freshness score based on last update time."""
    if not last_updated:
        return 0.0
    try:
        from datetime import datetime
        from dateutil import parser
        dt = parser.parse(last_updated)
        age_days = (datetime.now().astimezone() - dt).days
        if age_days <= days_threshold:
            return 10.0
        if age_days < days_threshold * 2:
            return 5.0
        return 0.0
    except Exception:
        return 0.0


def confidence_label(confidence: ThemeConfidence) -> str:
    return {
        ThemeConfidence.HIGH: "高信心",
        ThemeConfidence.MEDIUM: "中等信心",
        ThemeConfidence.WATCH_ONLY: "觀望",
        ThemeConfidence.REJECT: "否決",
    }.get(confidence, "未知")
