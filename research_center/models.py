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
    failure_reason: str | None = None
    found_by: list[str] = field(default_factory=list)


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
