from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CommandRequest:
    command: str
    raw_text: str
    target: str | None = None
    target_type: str | None = None
    market_scope: str | None = None
    theme_scope: str | None = None
    region_scope: str | None = None
    candidate_pool: str | None = None
    existing_theme_id: str | None = None
    source: str | None = None
    lookback_days: int | None = None
    mode: str = "normal"
    source_only: bool = False
    score: bool = False
    brief: bool = False
    top: int | None = None
    ai_model: str = "gemini"
    report_date: date | None = None
    output_formats: tuple[str, ...] = ("md", "html", "json")
    user_id: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class SourceItem:
    source_id: str
    title: str
    url: str
    source_level: str
    published_date: str | None = None
    snippet: str | None = None
    used_in_section: list[str] = field(default_factory=list)
    provider: str | None = None
    provider_detail: str | None = None
    fetch_provider: str | None = None
    fetch_status: str | None = None
    fetch_quality: str | None = None
    failure_reason: str | None = None
    found_by: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CandidateSnapshot:
    schema_version: str = "candidate_snapshot_v1"
    code: str = ""
    name: str = ""
    market: str | None = None
    symbol: str | None = None
    source_command: str | None = None
    source_strategy: str | None = None
    source_pool: str | None = None
    signal_date: str | None = None
    data_date: str | None = None
    signal_type: str | None = None
    signal_strength: float | None = None
    stage: str = "watch_only"
    technical_signals: list[dict[str, Any]] = field(default_factory=list)
    chip_signals: list[dict[str, Any]] = field(default_factory=list)
    revenue_signals: list[dict[str, Any]] = field(default_factory=list)
    theme_signals: list[dict[str, Any]] = field(default_factory=list)
    news_signals: list[dict[str, Any]] = field(default_factory=list)
    early_stage_flags: list[str] = field(default_factory=list)
    overheat_flags: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    local_scores: dict[str, Any] = field(default_factory=dict)
    evidence_refs: list[str] = field(default_factory=list)
    raw_snapshot_ref: str | None = None
    created_at: str | None = None


@dataclass(frozen=True)
class DataSourceSummary:
    schema_version: str = "data_source_summary_v1"
    data_type: str = "unknown"
    provider: str = "unknown"
    source_name: str | None = None
    source_path_or_url: str | None = None
    as_of_date: str | None = None
    fetch_time: str | None = None
    status: str = "unknown"
    row_count: int | None = None
    fallback_used: bool = False
    fallback_chain: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    warning_flags: list[str] = field(default_factory=list)
    freshness: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReportMetadata:
    schema_version: str = "report_metadata_v1"
    report_id: str | None = None
    report_type: str | None = None
    command: str | None = None
    target: str | None = None
    report_date: str | None = None
    data_date: str | None = None
    model: str | None = None
    mode: str | None = None
    source_pool: str | None = None
    ai_used: bool = False
    ai_status: str = "not_used"
    fallback_reason: str | None = None
    created_at: str | None = None


@dataclass(frozen=True)
class NewsEvent:
    schema_version: str = "news_event_v1"
    news_id: str | None = None
    title: str = ""
    published_at: str | None = None
    source: str | None = None
    url: str | None = None
    related_symbols: list[str] = field(default_factory=list)
    related_topics: list[str] = field(default_factory=list)
    event_type: str = "unknown"
    signal_role: str = "background_noise"
    heat_level: str | None = None
    is_catalyst: bool = False
    is_counter_evidence: bool = False
    is_overheat_risk: bool = False
    summary: str | None = None
    evidence_text: str | None = None
    confidence: str | None = None
    created_at: str | None = None


@dataclass(frozen=True)
class CommandResult:
    schema_version: str = "command_result_v1"
    command: str = ""
    args: list[str] = field(default_factory=list)
    status: str = "success"
    reason: str | None = None
    message: str | None = None
    data_date: str | None = None
    artifacts: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    runtime_seconds: float | None = None
    created_at: str | None = None


@dataclass(frozen=True)
class ReportArtifacts:
    report_id: str
    report_type: str
    markdown_path: Path
    html_path: Path
    json_path: Path
    sources_path: Path


@dataclass(frozen=True)
class ResearchCenterResult:
    status: str
    request: CommandRequest
    summary: str
    markdown: str
    report_json: dict[str, Any]
    sources: list[SourceItem]
    artifacts: ReportArtifacts
    ai_used: bool
    ai_model: str | None = None
    fallback_reason: str | None = None
    runtime_context: dict[str, Any] = field(default_factory=dict, repr=False)


class CommandParseError(ValueError):
    pass


class ResearchCenterError(RuntimeError):
    pass
