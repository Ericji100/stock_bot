from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any

BACKFILL_DAG_SCHEMA_VERSION = "backfill_dag_v1"
BACKFILL_DAG_EVENT_SCHEMA_VERSION = "backfill_dag_event_v1"
BACKFILL_DAG_EVENT_STATUSES = {"started", "completed", "skipped", "failed", "running"}


@dataclass(frozen=True)
class BackfillDagNode:
    node_id: str
    label: str
    depends_on: list[str] = field(default_factory=list)
    status: str = "pending"
    skip_reason: str | None = None
    failure_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BackfillDagEvent:
    node_id: str
    status: str
    message: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat(timespec="seconds"))
    failure_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = BACKFILL_DAG_EVENT_SCHEMA_VERSION


DEFAULT_BACKFILL_NODES = [
    BackfillDagNode("market_universe", "全市場基礎名單"),
    BackfillDagNode("technical_cache", "技術面快取", ["market_universe"]),
    BackfillDagNode("chip_cache", "籌碼快取", ["market_universe"]),
    BackfillDagNode("financial_cache", "財報 / 毛利率", ["market_universe"]),
    BackfillDagNode("curated_scan_cache", "精選選股快取", ["technical_cache", "chip_cache", "financial_cache"]),
    BackfillDagNode("research_feature_pack", "投研 Feature Pack", ["curated_scan_cache"]),
    BackfillDagNode("news_event_store", "新聞 / 事件庫", ["market_universe"]),
]


def build_backfill_dag(report_date: date | str | None = None, marker: dict[str, Any] | None = None) -> dict[str, Any]:
    marker = marker or {}
    health = marker.get("health") if isinstance(marker.get("health"), dict) else {}
    event_summary = summarize_backfill_events(marker.get("backfill_dag_events") or marker.get("node_events") or [])
    nodes = [_node_with_event_status(_node_with_marker_status(node, marker, health), event_summary) for node in DEFAULT_BACKFILL_NODES]
    return {
        "schema_version": BACKFILL_DAG_SCHEMA_VERSION,
        "report_date": report_date.isoformat() if isinstance(report_date, date) else report_date,
        "nodes": [asdict(node) for node in nodes],
        "ready_nodes": [node.node_id for node in nodes if node.status == "ready"],
        "blocked_nodes": [node.node_id for node in nodes if node.status == "blocked"],
        "event_summary": event_summary,
    }


def summarize_backfill_dag(dag: dict[str, Any]) -> dict[str, Any]:
    nodes = dag.get("nodes") or []
    counts: dict[str, int] = {}
    for node in nodes:
        status = str(node.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return {
        "schema_version": dag.get("schema_version") or BACKFILL_DAG_SCHEMA_VERSION,
        "report_date": dag.get("report_date"),
        "node_count": len(nodes),
        "status_counts": counts,
        "ready_nodes": [str(node.get("node_id")) for node in nodes if str(node.get("status")) == "ready"],
        "pending_nodes": [str(node.get("node_id")) for node in nodes if str(node.get("status")) in {"pending", "blocked"}],
        "blocked_nodes": [str(node.get("node_id")) for node in nodes if str(node.get("status")) == "blocked"],
        "event_summary": dag.get("event_summary") or {},
    }


def create_backfill_dag_event(
    node_id: str,
    status: str,
    *,
    message: str | None = None,
    failure_reason: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = str(status or "").strip().lower()
    if normalized not in BACKFILL_DAG_EVENT_STATUSES:
        normalized = "running"
    return asdict(
        BackfillDagEvent(
            node_id=str(node_id),
            status=normalized,
            message=message,
            failure_reason=failure_reason,
            metadata=metadata or {},
        )
    )


def summarize_backfill_events(events: list[dict[str, Any]] | None) -> dict[str, Any]:
    latest_by_node: dict[str, dict[str, Any]] = {}
    status_counts: dict[str, int] = {}
    for event in events or []:
        if not isinstance(event, dict):
            continue
        node_id = str(event.get("node_id") or "").strip()
        status = str(event.get("status") or "unknown").strip().lower()
        if not node_id:
            continue
        status_counts[status] = status_counts.get(status, 0) + 1
        latest_by_node[node_id] = event
    latest_status = {
        node_id: str(event.get("status") or "unknown")
        for node_id, event in latest_by_node.items()
    }
    return {
        "schema_version": BACKFILL_DAG_EVENT_SCHEMA_VERSION,
        "event_count": sum(status_counts.values()),
        "status_counts": status_counts,
        "latest_status_by_node": latest_status,
        "completed_nodes": [node_id for node_id, status in latest_status.items() if status == "completed"],
        "failed_nodes": [node_id for node_id, status in latest_status.items() if status == "failed"],
        "skipped_nodes": [node_id for node_id, status in latest_status.items() if status == "skipped"],
        "running_nodes": [node_id for node_id, status in latest_status.items() if status in {"started", "running"}],
        "latest_events": latest_by_node,
    }


def _node_with_marker_status(node: BackfillDagNode, marker: dict[str, Any], health: dict[str, Any]) -> BackfillDagNode:
    metadata: dict[str, Any] = {}
    status = "pending"
    skip_reason = None
    if node.node_id == "market_universe":
        count = int(marker.get("universe_count") or 0)
        metadata["universe_count"] = count
        status = "ready" if count > 0 else "pending"
    elif node.node_id == "chip_cache":
        coverage = float(health.get("chip_candidate_coverage_pct") or 0)
        metadata["chip_candidate_coverage_pct"] = coverage
        status = "ready" if health.get("chip_coverage_ok") else "pending"
    elif node.node_id == "curated_scan_cache":
        status = "ready" if health.get("curated_scan_ready") else "pending"
    elif node.node_id == "research_feature_pack":
        status = "ready" if health.get("backfill_ready_for_research") else "pending"
    elif node.node_id == "technical_cache":
        status = "ready" if health.get("technical_cache_ok") else "pending"
    elif node.node_id == "financial_cache":
        status = "ready" if health.get("screening_cache_ok") else "pending"
    elif node.node_id == "news_event_store":
        status = "ready" if marker.get("news_event_count") else "pending"
    if status != "ready":
        missing = [dep for dep in node.depends_on if dep not in _ready_ids(marker, health)]
        if missing:
            status = "blocked"
            skip_reason = f"waiting_for:{','.join(missing)}"
    return BackfillDagNode(node.node_id, node.label, node.depends_on, status, skip_reason, None, metadata)


def _node_with_event_status(node: BackfillDagNode, event_summary: dict[str, Any]) -> BackfillDagNode:
    latest = (event_summary.get("latest_events") or {}).get(node.node_id)
    if not isinstance(latest, dict):
        return node
    status = str(latest.get("status") or "").lower()
    metadata = dict(node.metadata)
    metadata["latest_event"] = latest
    if status == "completed":
        return BackfillDagNode(node.node_id, node.label, node.depends_on, "ready", node.skip_reason, None, metadata)
    if status == "failed":
        return BackfillDagNode(node.node_id, node.label, node.depends_on, "blocked", node.skip_reason, latest.get("failure_reason") or latest.get("message"), metadata)
    if status == "skipped":
        return BackfillDagNode(node.node_id, node.label, node.depends_on, "skipped", latest.get("message") or node.skip_reason, None, metadata)
    if status in {"started", "running"}:
        return BackfillDagNode(node.node_id, node.label, node.depends_on, "running", node.skip_reason, None, metadata)
    return node


def _ready_ids(marker: dict[str, Any], health: dict[str, Any]) -> set[str]:
    ready = set()
    if int(marker.get("universe_count") or 0) > 0:
        ready.add("market_universe")
    if health.get("technical_cache_ok"):
        ready.add("technical_cache")
    if health.get("chip_coverage_ok"):
        ready.add("chip_cache")
    if health.get("screening_cache_ok"):
        ready.add("financial_cache")
    if health.get("curated_scan_ready"):
        ready.add("curated_scan_cache")
    if health.get("backfill_ready_for_research"):
        ready.add("research_feature_pack")
    if marker.get("news_event_count"):
        ready.add("news_event_store")
    return ready
