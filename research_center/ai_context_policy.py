from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

from .models import CommandRequest, SourceItem
from .source_text_cleaner import clean_source_text


POLICY_SCHEMA_VERSION = "ai_context_policy_v1"

RISK_TERMS = (
    "風險", "反證", "衰退", "下滑", "庫存", "毛利", "虧損", "訴訟", "違約", "降評",
    "制裁", "關稅", "戰爭", "匯率", "risk", "decline", "inventory", "lawsuit",
)
OFFICIAL_LEVELS = {"Level 1", "L1_official"}
MEDIA_LEVELS = {"Level 2", "Level 3", "L2_media", "L2_industry"}
COMMUNITY_LEVELS = {"Level 4", "L3_community", "L4_social"}


def build_evidence_selection_policy(request: CommandRequest) -> dict[str, Any]:
    command = request.command
    mode = request.mode or "normal"
    deep = mode == "deep"
    return {
        "schema_version": POLICY_SCHEMA_VERSION,
        "command": command,
        "mode": mode,
        "quality_first": True,
        "max_prompt_sources": _max_prompt_sources(command, deep),
        "min_prompt_sources": _min_prompt_sources(command, deep),
        "must_include": [
            "官方來源",
            "財報與營收資料",
            "重大訊息與法說會",
            "反證與風險來源",
            "高品質且高相關新聞",
        ],
        "downgraded": [
            "重複新聞",
            "低相關來源",
            "日期不可驗證來源",
            "論壇與社群資料",
        ],
        "source_rules": {
            "official": "官方來源優先入模。",
            "risk": "反證與風險來源必須優先入模。",
            "duplicates": "同事件去重，但保留官方來源與一筆媒體解讀。",
            "community": "論壇與社群只能作為情緒參考，不得單獨支撐高分。",
            "preserve_full_data": "未入模資料仍保存在完整報告 JSON 與來源清單。",
        },
    }


def select_sources_for_ai_input(
    request: CommandRequest,
    sources: list[SourceItem],
    *,
    max_sources: int | None = None,
) -> dict[str, Any]:
    policy = build_evidence_selection_policy(request)
    limit = max_sources or int(policy["max_prompt_sources"])
    ranked: list[tuple[int, int, SourceItem, list[str]]] = []
    seen_event_keys: set[str] = set()

    for index, source in enumerate(sources):
        reasons = _selection_reasons(source)
        score = _source_score(source, reasons)
        event_key = _event_key(source)
        if event_key in seen_event_keys and "官方來源" not in reasons and "反證或風險" not in reasons:
            score -= 35
            reasons.append("同事件重複")
        else:
            seen_event_keys.add(event_key)
        ranked.append((score, -index, source, reasons))

    selected_rows = sorted(ranked, key=lambda row: (row[0], row[1]), reverse=True)[:limit]
    selected_ids = {row[2].source_id for row in selected_rows}
    selected = [_source_entry(row[2], row[3], "入模") for row in selected_rows]
    omitted = [
        _source_entry(source, reasons, _omitted_reason(source, reasons, selected_ids))
        for _score, _index, source, reasons in ranked
        if source.source_id not in selected_ids
    ]
    audit = {
        "schema_version": "ai_input_source_selection_v1",
        "policy": policy,
        "input_source_count": len(sources),
        "selected_source_count": len(selected),
        "omitted_source_count": len(omitted),
        "selected_sources": selected,
        "omitted_sources": omitted,
        "coverage": source_coverage_counts([row[2] for row in selected_rows]),
        "all_source_coverage": source_coverage_counts(sources),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    return audit


def source_coverage_counts(sources: list[SourceItem]) -> dict[str, Any]:
    by_level: dict[str, int] = {}
    by_provider: dict[str, int] = {}
    official = 0
    media = 0
    community = 0
    risk = 0
    dated = 0
    explicit_dated = 0
    inferred_dated = 0
    fetched = 0
    for source in sources:
        level = source.source_level or "unknown"
        provider = source.provider or source.fetch_provider or "unknown"
        by_level[level] = by_level.get(level, 0) + 1
        by_provider[provider] = by_provider.get(provider, 0) + 1
        if level in OFFICIAL_LEVELS:
            official += 1
        if level in MEDIA_LEVELS:
            media += 1
        if level in COMMUNITY_LEVELS:
            community += 1
        if _has_risk_signal(source):
            risk += 1
        if source.published_date:
            dated += 1
            found_by = set(source.found_by or [])
            if "source_date:explicit" in found_by:
                explicit_dated += 1
            elif "source_date:inferred" in found_by:
                inferred_dated += 1
        if source.fetch_status in {"success", "ok"}:
            fetched += 1
    return {
        "total_sources": len(sources),
        "official_sources": official,
        "media_sources": media,
        "community_sources": community,
        "risk_or_counter_sources": risk,
        "dated_sources": dated,
        "explicit_dated_sources": explicit_dated,
        "inferred_dated_sources": inferred_dated,
        "undated_sources": max(0, len(sources) - dated),
        "fetched_sources": fetched,
        "by_source_level": by_level,
        "by_provider": by_provider,
    }


def compact_source_for_prompt(entry: dict[str, Any]) -> dict[str, Any]:
    source = dict(entry.get("source") or {})
    return {
        "來源編號": source.get("source_id"),
        "標題": source.get("title"),
        "連結": source.get("url"),
        "來源層級": source.get("source_level"),
        "發布日期": source.get("published_date") or "日期不可驗證",
        "資料工具": source.get("provider") or source.get("fetch_provider"),
        "入模原因": entry.get("reasons") or [],
        "摘要": source.get("snippet"),
    }


def compact_source_for_prompt(entry: dict[str, Any]) -> dict[str, Any]:
    source = dict(entry.get("source") or {})
    return {
        "來源編號": source.get("source_id"),
        "標題": clean_source_text(source.get("title")),
        "連結": source.get("url"),
        "來源層級": source.get("source_level"),
        "發布日期": source.get("published_date") or "日期不可驗證",
        "資料工具": source.get("provider") or source.get("fetch_provider"),
        "入模原因": entry.get("reasons") or [],
        "摘要": clean_source_text(source.get("snippet")),
    }


def _max_prompt_sources(command: str, deep: bool) -> int:
    if command == "research":
        return 70 if deep else 45
    if command == "value_scan":
        return 90 if deep else 60
    if command == "macro":
        return 80 if deep else 55
    if command in {"theme", "theme_radar", "theme_flow", "sector_strength"}:
        return 90 if deep else 65
    if command == "radar":
        return 40
    return 50


def _min_prompt_sources(command: str, deep: bool) -> int:
    if command == "research":
        return 14 if deep else 8
    if command == "value_scan":
        return 20 if deep else 12
    if command == "macro":
        return 16 if deep else 10
    if command in {"theme", "theme_radar", "theme_flow", "sector_strength"}:
        return 16 if deep else 10
    return 6


def _source_score(source: SourceItem, reasons: list[str]) -> int:
    score = 0
    level = source.source_level or ""
    if level in OFFICIAL_LEVELS:
        score += 100
    elif level in MEDIA_LEVELS:
        score += 65
    elif level in COMMUNITY_LEVELS:
        score += 10
    else:
        score += 35
    if "反證或風險" in reasons:
        score += 45
    if "已抓取正文" in reasons:
        score += 20
    if source.published_date:
        score += 12
    else:
        score -= 12
    if source.snippet:
        score += 6
    return score


def _selection_reasons(source: SourceItem) -> list[str]:
    reasons: list[str] = []
    if source.source_level in OFFICIAL_LEVELS:
        reasons.append("官方來源")
    if source.source_level in MEDIA_LEVELS:
        reasons.append("媒體或產業來源")
    if source.source_level in COMMUNITY_LEVELS:
        reasons.append("社群情緒來源")
    if _has_risk_signal(source):
        reasons.append("反證或風險")
    if source.fetch_status in {"success", "ok"}:
        reasons.append("已抓取正文")
    if source.published_date:
        reasons.append("日期可驗證")
    else:
        reasons.append("日期不可驗證")
    return reasons


def _source_entry(source: SourceItem, reasons: list[str], status: str) -> dict[str, Any]:
    source_dict = asdict(source)
    source_dict["title"] = clean_source_text(source_dict.get("title"))
    source_dict["snippet"] = clean_source_text(source_dict.get("snippet"))
    return {
        "status": status,
        "reasons": reasons,
        "source": source_dict,
    }


def _omitted_reason(source: SourceItem, reasons: list[str], selected_ids: set[str]) -> str:
    if source.source_id in selected_ids:
        return "入模"
    if "同事件重複" in reasons:
        return "同事件重複，完整來源仍保存"
    if "社群情緒來源" in reasons:
        return "社群或論壇來源，僅列補充"
    if "日期不可驗證" in reasons:
        return "日期不可驗證，列為補充"
    return "超出入模上限，完整來源仍保存"


def _has_risk_signal(source: SourceItem) -> bool:
    text = f"{source.title or ''} {source.snippet or ''}".lower()
    return any(term.lower() in text for term in RISK_TERMS)


def _event_key(source: SourceItem) -> str:
    title = str(source.title or "").strip().lower()
    if not title:
        return str(source.url or source.source_id)
    for token in ("｜", "|", "-", "－", "_"):
        title = title.split(token, 1)[0].strip()
    return title[:50] or str(source.url or source.source_id)
