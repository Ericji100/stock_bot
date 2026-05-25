"""Topic domain models for the AI topic knowledge base maintenance system."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class TopicChangeStatus(Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    FAILED = "failed"


class TopicChangeMode(Enum):
    INITIAL = "initial"
    UPDATE = "update"
    ADJUST = "adjust"


class TopicActionType(Enum):
    CREATE_THEME = "create_theme"
    UPDATE_THEME = "update_theme"
    MERGE_THEME = "merge_theme"
    RENAME_THEME = "rename_theme"
    ADD_COMPANY_RELATION = "add_company_relation"
    UPDATE_COMPANY_RELATION = "update_company_relation"
    REMOVE_COMPANY_RELATION = "remove_company_relation"
    ADD_SUPPLY_CHAIN_NODE = "add_supply_chain_node"
    UPDATE_SUPPLY_CHAIN_NODE = "update_supply_chain_node"
    REMOVE_SUPPLY_CHAIN_NODE = "remove_supply_chain_node"


class TopicConfidence(Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class TopicSourceLevel(Enum):
    L1_OFFICIAL = "L1_official"
    L2_MEDIA = "L2_media"
    L3_COMMUNITY = "L3_community"


@dataclass
class TopicEvidence:
    source: str
    source_level: TopicSourceLevel
    content: str
    url: str | None = None
    publish_date: str | None = None
    score_contribution: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "source_level": self.source_level.value,
            "content": self.content,
            "url": self.url,
            "publish_date": self.publish_date,
            "score_contribution": self.score_contribution,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TopicEvidence:
        try:
            level = TopicSourceLevel(data.get("source_level", "L2_media"))
        except ValueError:
            level = TopicSourceLevel.L2_MEDIA
        return cls(
            source=data.get("source", ""),
            source_level=level,
            content=data.get("content", ""),
            url=data.get("url"),
            publish_date=data.get("publish_date"),
            score_contribution=float(data.get("score_contribution", 0.0)),
        )


@dataclass
class TopicChangeAction:
    action_type: TopicActionType
    theme_id: str
    theme_name: str
    keywords: list[str] = field(default_factory=list)
    industries: list[str] = field(default_factory=list)
    supply_chain_role: str = ""
    confidence: TopicConfidence = TopicConfidence.MEDIUM
    reason: str = ""
    evidence: list[TopicEvidence] = field(default_factory=list)
    target_theme_id: str | None = None
    company_code: str | None = None
    company_name: str | None = None
    node_id: str | None = None
    affected_companies: list[Any] = field(default_factory=list)
    company_relations: list[dict[str, Any]] = field(default_factory=list)
    risk_notes: list[str] = field(default_factory=list)
    missing_data: list[str] = field(default_factory=list)
    supply_chain_nodes: list[dict[str, Any]] = field(default_factory=list)
    counter_evidence: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_type": self.action_type.value,
            "theme_id": self.theme_id,
            "theme_name": self.theme_name,
            "keywords": self.keywords,
            "industries": self.industries,
            "supply_chain_role": self.supply_chain_role,
            "confidence": self.confidence.value,
            "reason": self.reason,
            "evidence": [e.to_dict() for e in self.evidence],
            "target_theme_id": self.target_theme_id,
            "company_code": self.company_code,
            "company_name": self.company_name,
            "node_id": self.node_id,
            "affected_companies": self.affected_companies,
            "company_relations": self.company_relations,
            "risk_notes": self.risk_notes,
            "missing_data": self.missing_data,
            "supply_chain_nodes": self.supply_chain_nodes,
            "counter_evidence": self.counter_evidence,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TopicChangeAction:
        try:
            at = TopicActionType(data.get("action_type", "create_theme"))
        except ValueError:
            at = TopicActionType.CREATE_THEME
        try:
            conf = TopicConfidence(data.get("confidence", "medium"))
        except ValueError:
            conf = TopicConfidence.MEDIUM
        return cls(
            action_type=at,
            theme_id=data.get("theme_id", ""),
            theme_name=data.get("theme_name", ""),
            keywords=data.get("keywords", []),
            industries=data.get("industries", []),
            supply_chain_role=data.get("supply_chain_role", ""),
            confidence=conf,
            reason=data.get("reason", ""),
            evidence=[TopicEvidence.from_dict(e) for e in data.get("evidence", [])],
            target_theme_id=data.get("target_theme_id"),
            company_code=data.get("company_code"),
            company_name=data.get("company_name"),
            node_id=data.get("node_id"),
            affected_companies=data.get("affected_companies", []),
            company_relations=data.get("company_relations", []),
            risk_notes=data.get("risk_notes", []),
            missing_data=data.get("missing_data", []),
            supply_chain_nodes=data.get("supply_chain_nodes", []),
            counter_evidence=data.get("counter_evidence", []),
            extra=data.get("extra", {}),
        )


@dataclass
class TopicChangePack:
    change_id: str
    parent_change_id: str | None
    mode: TopicChangeMode
    status: TopicChangeStatus
    model: str
    created_at: str
    updated_at: str
    summary: str
    confidence: str
    actions: list[TopicChangeAction] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    company_knowledge_updates: dict[str, Any] = field(default_factory=dict)
    adjustment_notes: str = ""
    adjustment_check: dict[str, Any] = field(default_factory=dict)
    raw_response_path: str = ""
    prompt_log_path: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_id": self.change_id,
            "parent_change_id": self.parent_change_id,
            "mode": self.mode.value,
            "status": self.status.value,
            "model": self.model,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "summary": self.summary,
            "confidence": self.confidence,
            "actions": [a.to_dict() for a in self.actions],
            "warnings": self.warnings,
            "sources": self.sources,
            "company_knowledge_updates": self.company_knowledge_updates,
            "adjustment_notes": self.adjustment_notes,
            "adjustment_check": self.adjustment_check,
            "raw_response_path": self.raw_response_path,
            "prompt_log_path": self.prompt_log_path,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TopicChangePack:
        try:
            mode = TopicChangeMode(data.get("mode", "update"))
        except ValueError:
            mode = TopicChangeMode.UPDATE
        try:
            status = TopicChangeStatus(data.get("status", "pending"))
        except ValueError:
            status = TopicChangeStatus.PENDING
        return cls(
            change_id=data.get("change_id", ""),
            parent_change_id=data.get("parent_change_id"),
            mode=mode,
            status=status,
            model=data.get("model", "gemini"),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            summary=data.get("summary", ""),
            confidence=data.get("confidence", "medium"),
            actions=[TopicChangeAction.from_dict(a) for a in data.get("actions", [])],
            warnings=data.get("warnings", []),
            sources=data.get("sources", []),
            company_knowledge_updates=(
                data.get("company_knowledge_updates", {})
                if isinstance(data.get("company_knowledge_updates", {}), dict)
                else {}
            ),
            adjustment_notes=data.get("adjustment_notes", ""),
            adjustment_check=data.get("adjustment_check", {}),
            raw_response_path=data.get("raw_response_path", ""),
            prompt_log_path=data.get("prompt_log_path", ""),
            extra=data.get("extra", {}),
        )


@dataclass
class TopicApplyResult:
    change_id: str
    success: bool
    created: int = 0
    updated: int = 0
    merged: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    backup_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_id": self.change_id,
            "success": self.success,
            "created": self.created,
            "updated": self.updated,
            "merged": self.merged,
            "skipped": self.skipped,
            "failed": self.failed,
            "errors": self.errors,
            "backup_path": self.backup_path,
        }


@dataclass
class TopicProfile:
    theme_id: str
    theme_name: str
    keywords: list[str] = field(default_factory=list)
    industries: list[str] = field(default_factory=list)
    supply_chain_role: str = ""
    confidence: str = "medium"
    source_level: str = "L2_media"
    status: str = "active"
    created_at: str = ""
    updated_at: str = ""
    risk_notes: list[str] = field(default_factory=list)
    missing_data: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "theme_id": self.theme_id,
            "theme_name": self.theme_name,
            "keywords": self.keywords,
            "industries": self.industries,
            "supply_chain_role": self.supply_chain_role,
            "confidence": self.confidence,
            "source_level": self.source_level,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "risk_notes": self.risk_notes,
            "missing_data": self.missing_data,
        }
        data.update(self.extra)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TopicProfile:
        known = {
            "theme_id",
            "theme_name",
            "keywords",
            "industries",
            "supply_chain_role",
            "confidence",
            "source_level",
            "status",
            "created_at",
            "updated_at",
            "risk_notes",
            "missing_data",
        }
        return cls(
            theme_id=data.get("theme_id", ""),
            theme_name=data.get("theme_name", ""),
            keywords=data.get("keywords", []),
            industries=data.get("industries", []),
            supply_chain_role=data.get("supply_chain_role", ""),
            confidence=data.get("confidence", "medium"),
            source_level=data.get("source_level", "L2_media"),
            status=data.get("status", "active"),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            risk_notes=data.get("risk_notes", []),
            missing_data=data.get("missing_data", []),
            extra={k: v for k, v in data.items() if k not in known},
        )


@dataclass
class TopicCompanyRelation:
    company_code: str
    company_name: str
    themes: list[str] = field(default_factory=list)
    primary_theme: str = ""
    evidence: list[Any] = field(default_factory=list)
    relation_strength: str = ""
    relation_type: str = ""
    role: str = ""
    products: list[str] = field(default_factory=list)
    customers: list[str] = field(default_factory=list)
    revenue_exposure: dict[str, Any] = field(default_factory=dict)
    benefit_logic: str = ""
    counter_evidence: list[Any] = field(default_factory=list)
    missing_data: list[str] = field(default_factory=list)
    updated_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "company_code": self.company_code,
            "company_name": self.company_name,
            "themes": self.themes,
            "primary_theme": self.primary_theme,
            "evidence": self.evidence,
        }
        if self.relation_strength:
            data["relation_strength"] = self.relation_strength
        if self.relation_type:
            data["relation_type"] = self.relation_type
        if self.role:
            data["role"] = self.role
        if self.products:
            data["products"] = self.products
        if self.customers:
            data["customers"] = self.customers
        if self.revenue_exposure:
            data["revenue_exposure"] = self.revenue_exposure
        if self.benefit_logic:
            data["benefit_logic"] = self.benefit_logic
        if self.counter_evidence:
            data["counter_evidence"] = self.counter_evidence
        if self.missing_data:
            data["missing_data"] = self.missing_data
        if self.updated_at:
            data["updated_at"] = self.updated_at
        data.update(self.extra)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TopicCompanyRelation:
        known = {
            "company_code",
            "company_name",
            "themes",
            "primary_theme",
            "evidence",
            "relation_strength",
            "relation_type",
            "role",
            "products",
            "customers",
            "revenue_exposure",
            "benefit_logic",
            "counter_evidence",
            "missing_data",
            "updated_at",
        }
        return cls(
            company_code=data.get("company_code", ""),
            company_name=data.get("company_name", ""),
            themes=data.get("themes", []),
            primary_theme=data.get("primary_theme", ""),
            evidence=data.get("evidence", []),
            relation_strength=data.get("relation_strength", ""),
            relation_type=data.get("relation_type", ""),
            role=data.get("role", ""),
            products=data.get("products", []),
            customers=data.get("customers", []),
            revenue_exposure=data.get("revenue_exposure", {}),
            benefit_logic=data.get("benefit_logic", ""),
            counter_evidence=data.get("counter_evidence", []),
            missing_data=data.get("missing_data", []),
            updated_at=data.get("updated_at", ""),
            extra={k: v for k, v in data.items() if k not in known},
        )


@dataclass
class TopicSupplyChainNode:
    node_id: str
    company_code: str
    company_name: str
    role: str = ""
    upstream: list[str] = field(default_factory=list)
    downstream: list[str] = field(default_factory=list)
    product_keywords: list[str] = field(default_factory=list)
    theme_id: str = ""
    confidence: str = ""
    source_level: str = ""
    evidence: list[Any] = field(default_factory=list)
    risk_notes: list[str] = field(default_factory=list)
    missing_data: list[str] = field(default_factory=list)
    layer: int | None = None
    customers: list[str] = field(default_factory=list)
    revenue_exposure: dict[str, Any] = field(default_factory=dict)
    benefit_logic: str = ""
    updated_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "node_id": self.node_id,
            "company_code": self.company_code,
            "company_name": self.company_name,
            "role": self.role,
            "upstream": self.upstream,
            "downstream": self.downstream,
            "product_keywords": self.product_keywords,
        }
        if self.theme_id:
            data["theme_id"] = self.theme_id
        if self.confidence:
            data["confidence"] = self.confidence
        if self.source_level:
            data["source_level"] = self.source_level
        if self.evidence:
            data["evidence"] = self.evidence
        if self.risk_notes:
            data["risk_notes"] = self.risk_notes
        if self.missing_data:
            data["missing_data"] = self.missing_data
        if self.layer is not None:
            data["layer"] = self.layer
        if self.customers:
            data["customers"] = self.customers
        if self.revenue_exposure:
            data["revenue_exposure"] = self.revenue_exposure
        if self.benefit_logic:
            data["benefit_logic"] = self.benefit_logic
        if self.updated_at:
            data["updated_at"] = self.updated_at
        data.update(self.extra)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TopicSupplyChainNode:
        known = {
            "node_id",
            "company_code",
            "company_name",
            "role",
            "upstream",
            "downstream",
            "product_keywords",
            "theme_id",
            "confidence",
            "source_level",
            "evidence",
            "risk_notes",
            "missing_data",
            "layer",
            "customers",
            "revenue_exposure",
            "benefit_logic",
            "updated_at",
        }
        return cls(
            node_id=data.get("node_id", ""),
            company_code=data.get("company_code", ""),
            company_name=data.get("company_name", ""),
            role=data.get("role", ""),
            upstream=data.get("upstream", []),
            downstream=data.get("downstream", []),
            product_keywords=data.get("product_keywords", []),
            theme_id=data.get("theme_id", ""),
            confidence=data.get("confidence", ""),
            source_level=data.get("source_level", ""),
            evidence=data.get("evidence", []),
            risk_notes=data.get("risk_notes", []),
            missing_data=data.get("missing_data", []),
            layer=data.get("layer"),
            customers=data.get("customers", []),
            revenue_exposure=data.get("revenue_exposure", {}),
            benefit_logic=data.get("benefit_logic", ""),
            updated_at=data.get("updated_at", ""),
            extra={k: v for k, v in data.items() if k not in known},
        )


@dataclass
class TopicAuditEntry:
    timestamp: str
    change_id: str
    action: str
    user_id: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "change_id": self.change_id,
            "action": self.action,
            "user_id": self.user_id,
            "details": self.details,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TopicAuditEntry:
        return cls(
            timestamp=data.get("timestamp", ""),
            change_id=data.get("change_id", ""),
            action=data.get("action", ""),
            user_id=data.get("user_id", ""),
            details=data.get("details", {}),
        )

    @classmethod
    def create(cls, action: str, change_id: str, user_id: str, details: dict[str, Any]) -> TopicAuditEntry:
        return cls(
            timestamp=datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"),
            change_id=change_id,
            action=action,
            user_id=user_id,
            details=details,
        )
