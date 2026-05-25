"""Theme domain models for the semi-automatic theme update system."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class ThemeConfidence(Enum):
    HIGH = "high"
    MEDIUM = "medium"
    WATCH_ONLY = "watch_only"
    REJECT = "reject"

    @classmethod
    def from_score(cls, score: float) -> ThemeConfidence:
        if score >= 75:
            return cls.HIGH
        if score >= 60:
            return cls.MEDIUM
        if score >= 40:
            return cls.WATCH_ONLY
        return cls.REJECT


class ThemeSourceLevel(Enum):
    L1_OFFICIAL = "L1_official"
    L2_MEDIA = "L2_media"
    L3_COMMUNITY = "L3_community"


class ThemeRelationType(Enum):
    DIRECT_PRODUCT = "direct_product"
    SUPPLY_CHAIN = "supply_chain"
    BRAND_OWNER = "brand_owner"
    INDIRECT = "indirect"
    UNCLEAR = "unclear"


class ThemeDraftStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    MERGED = "merged"


class ThemeAuditAction(Enum):
    CREATED = "created"
    APPROVED = "approved"
    REJECTED = "rejected"
    MERGED = "merged"
    UPDATED = "updated"


@dataclass
class ThemeEvidence:
    """Evidence for a theme, including source and scoring contribution."""
    source: str
    source_level: ThemeSourceLevel
    content: str
    url: str | None = None
    publish_date: str | None = None
    score_contribution: float = 0.0


@dataclass
class ThemeProfile:
    """Official theme profile stored in config/theme_profiles.json."""
    theme_id: str
    theme_name: str
    keywords: list[str] = field(default_factory=list)
    industries: list[str] = field(default_factory=list)
    supply_chain_role: str = ""
    confidence: ThemeConfidence = ThemeConfidence.MEDIUM
    source_level: ThemeSourceLevel = ThemeSourceLevel.L2_MEDIA
    status: str = "active"
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "theme_id": self.theme_id,
            "theme_name": self.theme_name,
            "keywords": self.keywords,
            "industries": self.industries,
            "supply_chain_role": self.supply_chain_role,
            "confidence": self.confidence.value,
            "source_level": self.source_level.value,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ThemeProfile:
        return cls(
            theme_id=data.get("theme_id", ""),
            theme_name=data.get("theme_name", ""),
            keywords=data.get("keywords", []),
            industries=data.get("industries", []),
            supply_chain_role=data.get("supply_chain_role", ""),
            confidence=ThemeConfidence(data.get("confidence", "medium")),
            source_level=ThemeSourceLevel(data.get("source_level", "L2_media")),
            status=data.get("status", "active"),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )


@dataclass
class SupplyChainNode:
    """Supply chain node for theme mapping."""
    node_id: str
    company_code: str
    company_name: str
    role: str
    upstream: list[str] = field(default_factory=list)
    downstream: list[str] = field(default_factory=list)
    product_keywords: list[str] = field(default_factory=list)


@dataclass
class CompanyThemeMapping:
    """Company to theme mapping stored in config/company_theme_map.json."""
    company_code: str
    company_name: str
    themes: list[str] = field(default_factory=list)
    primary_theme: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DynamicThemeCacheEntry:
    """Entry in the dynamic theme cache."""
    theme_name: str
    keywords: list[str]
    industries: list[str]
    supply_chain_role: str
    matched_companies: list[dict[str, Any]]
    evidence_list: list[dict[str, Any]]
    theme_relation_score: float
    confidence: str
    relation_type: str
    cache_time: str = ""


@dataclass
class ThemeDraft:
    """AI-generated theme draft that goes through approval before merging."""
    draft_id: str
    theme_name: str
    keywords: list[str] = field(default_factory=list)
    industries: list[str] = field(default_factory=list)
    supply_chain_role: str = ""
    candidate_stocks: list[dict[str, Any]] = field(default_factory=list)
    evidence_list: list[ThemeEvidence] = field(default_factory=list)
    counter_evidence: list[dict[str, Any]] = field(default_factory=list)
    risk_notes: list[str] = field(default_factory=list)
    missing_data: list[str] = field(default_factory=list)
    theme_relation_score: float = 0.0
    confidence: ThemeConfidence = ThemeConfidence.MEDIUM
    relation_type: ThemeRelationType = ThemeRelationType.UNCLEAR
    status: ThemeDraftStatus = ThemeDraftStatus.PENDING
    created_at: str = ""
    ai_model: str = ""
    raw_response_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "theme_name": self.theme_name,
            "keywords": self.keywords,
            "industries": self.industries,
            "supply_chain_role": self.supply_chain_role,
            "candidate_stocks": self.candidate_stocks,
            "evidence_list": [
                {
                    "source": e.source,
                    "source_level": e.source_level.value,
                    "content": e.content,
                    "url": e.url,
                    "publish_date": e.publish_date,
                    "score_contribution": e.score_contribution,
                }
                for e in self.evidence_list
            ],
            "counter_evidence": self.counter_evidence,
            "risk_notes": self.risk_notes,
            "missing_data": self.missing_data,
            "theme_relation_score": self.theme_relation_score,
            "confidence": self.confidence.value,
            "relation_type": self.relation_type.value,
            "status": self.status.value,
            "created_at": self.created_at,
            "ai_model": self.ai_model,
            "raw_response_id": self.raw_response_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ThemeDraft:
        evidence_list = []
        for e in data.get("evidence_list", []):
            if isinstance(e, dict):
                evidence_list.append(ThemeEvidence(
                    source=e.get("source", ""),
                    source_level=ThemeSourceLevel(e.get("source_level", "L2_media")),
                    content=e.get("content", ""),
                    url=e.get("url"),
                    publish_date=e.get("publish_date"),
                    score_contribution=e.get("score_contribution", 0.0),
                ))
            else:
                evidence_list.append(e)
        return cls(
            draft_id=data.get("draft_id", ""),
            theme_name=data.get("theme_name", ""),
            keywords=data.get("keywords", []),
            industries=data.get("industries", []),
            supply_chain_role=data.get("supply_chain_role", ""),
            candidate_stocks=data.get("candidate_stocks", []),
            evidence_list=evidence_list,
            counter_evidence=data.get("counter_evidence", []),
            risk_notes=data.get("risk_notes", []),
            missing_data=data.get("missing_data", []),
            theme_relation_score=data.get("theme_relation_score", 0.0),
            confidence=ThemeConfidence(data.get("confidence", "medium")),
            relation_type=ThemeRelationType(data.get("relation_type", "unclear")),
            status=ThemeDraftStatus(data.get("status", "pending")),
            created_at=data.get("created_at", ""),
            ai_model=data.get("ai_model", ""),
            raw_response_id=data.get("raw_response_id", ""),
        )

    @classmethod
    def new(cls, theme_name: str, ai_model: str = "gemini") -> ThemeDraft:
        now = datetime.now().astimezone().isoformat()
        return cls(
            draft_id=f"draft_{uuid.uuid4().hex[:12]}",
            theme_name=theme_name,
            created_at=now,
            ai_model=ai_model,
        )


@dataclass
class ThemeAuditEntry:
    """Audit log entry for theme draft actions."""
    audit_id: str
    action: ThemeAuditAction
    draft_id: str
    user_id: str
    timestamp: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "audit_id": self.audit_id,
            "action": self.action.value,
            "draft_id": self.draft_id,
            "user_id": self.user_id,
            "timestamp": self.timestamp,
            "details": self.details,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ThemeAuditEntry:
        return cls(
            audit_id=data.get("audit_id", ""),
            action=ThemeAuditAction(data.get("action", "created")),
            draft_id=data.get("draft_id", ""),
            user_id=data.get("user_id", ""),
            timestamp=data.get("timestamp", ""),
            details=data.get("details", {}),
        )

    @classmethod
    def create(cls, action: ThemeAuditAction, draft_id: str, user_id: str = "system", details: dict[str, Any] | None = None) -> ThemeAuditEntry:
        return cls(
            audit_id=f"audit_{uuid.uuid4().hex[:12]}",
            action=action,
            draft_id=draft_id,
            user_id=user_id,
            timestamp=datetime.now().astimezone().isoformat(),
            details=details or {},
        )