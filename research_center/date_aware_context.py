from __future__ import annotations

import re
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .models import CommandRequest, SourceItem
from .news_models import NewsItem, apply_news_signal_tags
from .news_repository import NEWS_DB_PATH, NewsRepository
from .news_source_filter import is_irrelevant_market_source


POLICIES: dict[str, dict[str, Any]] = {
    "macro": {"windows": [7, 14, 30], "min_items": 8, "max_items": 24},
    "research": {"windows": [30, 90, 180], "min_items": 10, "max_items": 30},
    "theme": {"windows": [90, 180], "min_items": 12, "max_items": 36},
    "value_scan": {"windows": [90, 180], "min_items": 20, "max_items": 50},
    "topic_maintain_initial": {"windows": [180, 365], "min_items": 60, "max_items": 80},
    "topic_maintain_update": {"windows": [90, 180], "min_items": 30, "max_items": 60},
}

DEFAULT_POLICY = {"windows": [30, 90], "min_items": 10, "max_items": 30}

GENERAL_TAIWAN_MARKET_TERMS = (
    "台股",
    "台灣",
    "股票",
    "財經",
    "產業",
    "半導體",
    "AI",
    "金融",
    "匯率",
    "利率",
    "政策",
    "外資",
    "法人",
    "資金",
    "題材",
)


def analysis_date_for_request(request: CommandRequest) -> date:
    return request.report_date or date.today()


def policy_key_for_request(request: CommandRequest, structured_data: dict[str, Any] | None = None) -> str:
    if request.command == "topic_maintain":
        mode_hint = (structured_data or {}).get("topic_maintain_mode_hint")
        return "topic_maintain_initial" if mode_hint == "initial" else "topic_maintain_update"
    return request.command


def date_window_policy_for_request(request: CommandRequest, structured_data: dict[str, Any] | None = None) -> dict[str, Any]:
    key = policy_key_for_request(request, structured_data)
    policy = dict(POLICIES.get(key, DEFAULT_POLICY))
    policy["policy_key"] = key
    policy["analysis_date"] = analysis_date_for_request(request).isoformat()
    return policy


def parse_date_like(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    now = datetime.now()
    relative = _parse_relative_date(text, now)
    if relative:
        return relative
    match = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    match = re.search(r"(\d{1,2})[-/月](\d{1,2})", text)
    if match:
        try:
            return date(now.year, int(match.group(1)), int(match.group(2)))
        except ValueError:
            return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _parse_relative_date(text: str, now: datetime) -> date | None:
    lower = text.lower()
    if "昨天" in text or "yesterday" in lower:
        return (now - timedelta(days=1)).date()
    if "前天" in text:
        return (now - timedelta(days=2)).date()
    if "今天" in text or "今日" in text or "today" in lower:
        return now.date()
    match = re.search(r"(\d+)\s*(小時|hours?|hrs?)\s*(前|ago)", lower)
    if match:
        return (now - timedelta(hours=int(match.group(1)))).date()
    match = re.search(r"(\d+)\s*(天|days?)\s*(前|ago)", lower)
    if match:
        return (now - timedelta(days=int(match.group(1)))).date()
    return None


def attach_date_aware_context(
    request: CommandRequest,
    structured_data: dict[str, Any],
    db_path: Path | None = None,
    progress=None,
) -> dict[str, Any]:
    context = build_date_aware_context(request, structured_data, db_path=db_path)
    structured_data["analysis_date"] = context["analysis_date"]
    structured_data["date_window_policy"] = context["date_window_policy"]
    structured_data["saved_news_context"] = context["saved_news_context"]
    structured_data["date_aware_context"] = context
    if progress:
        news = context["saved_news_context"]
        progress(
            "日期感知資料："
            f"analysis_date={context['analysis_date']} "
            f"window={news.get('window_used_days')}d "
            f"news={news.get('usable_count')}"
        )
    return structured_data


def build_date_aware_context(
    request: CommandRequest,
    structured_data: dict[str, Any],
    db_path: Path | None = None,
) -> dict[str, Any]:
    policy = date_window_policy_for_request(request, structured_data)
    news = build_saved_news_context(request, structured_data, db_path=db_path, policy=policy)
    return {
        "analysis_date": policy["analysis_date"],
        "date_window_policy": policy,
        "saved_news_context": news,
        "usage_rules": [
            "優先使用 analysis_date 當天與更近期之前的資料。",
            "若指定 report_date，不得使用晚於該日期的新聞或來源。",
            "資料不足時才逐步擴大回看視窗；超過一年資料不作為主要依據。",
        ],
    }


def build_saved_news_context(
    request: CommandRequest,
    structured_data: dict[str, Any],
    db_path: Path | None = None,
    policy: dict[str, Any] | None = None,
    repository: NewsRepository | None = None,
) -> dict[str, Any]:
    policy = policy or date_window_policy_for_request(request, structured_data)
    analysis_date = parse_date_like(policy.get("analysis_date")) or analysis_date_for_request(request)
    windows = [int(v) for v in policy.get("windows", [])] or [30, 90]
    min_items = int(policy.get("min_items") or 10)
    max_items = int(policy.get("max_items") or 30)
    terms = extract_target_terms(request, structured_data)
    try:
        repo = repository or NewsRepository(db_path or NEWS_DB_PATH)
    except Exception as exc:
        return {
            "analysis_date": analysis_date.isoformat(),
            "policy_key": policy.get("policy_key"),
            "windows": windows,
            "window_used_days": windows[0],
            "min_items": min_items,
            "max_items": max_items,
            "usable_count": 0,
            "expanded": False,
            "excluded_after_analysis_date_count": 0,
            "target_terms": terms[:20],
            "items": [],
            "status": "unavailable",
            "error": str(exc),
        }
    max_window = max(windows)
    try:
        raw_items = repo.query_all_recent(hours=max_window * 24 + 48)
    except Exception as exc:
        return {
            "analysis_date": analysis_date.isoformat(),
            "policy_key": policy.get("policy_key"),
            "windows": windows,
            "window_used_days": windows[0],
            "min_items": min_items,
            "max_items": max_items,
            "usable_count": 0,
            "expanded": False,
            "excluded_after_analysis_date_count": 0,
            "target_terms": terms[:20],
            "items": [],
            "status": "unavailable",
            "error": str(exc),
        }
    excluded_future = 0
    selected: list[NewsItem] = []
    window_used = windows[-1]
    for window in windows:
        start_date = analysis_date - timedelta(days=window)
        candidates: list[NewsItem] = []
        for item in raw_items:
            published = parse_date_like(item.published_at) or parse_date_like(item.created_at)
            if not published:
                continue
            if published > analysis_date:
                excluded_future += 1
                continue
            if published < start_date:
                continue
            if is_irrelevant_market_source(item, request.command):
                continue
            if not _news_matches_terms(item, terms, request.command):
                continue
            candidates.append(item)
        candidates.sort(key=lambda item: _news_sort_key(item, analysis_date, terms), reverse=True)
        selected = candidates[:max_items]
        window_used = window
        if len(selected) >= min_items:
            break
    return {
        "analysis_date": analysis_date.isoformat(),
        "policy_key": policy.get("policy_key"),
        "windows": windows,
        "window_used_days": window_used,
        "min_items": min_items,
        "max_items": max_items,
        "usable_count": len(selected),
        "expanded": window_used != windows[0],
        "excluded_after_analysis_date_count": excluded_future,
        "target_terms": terms[:20],
        "items": [_news_item_to_context(item, analysis_date, terms) for item in selected],
    }


def extract_target_terms(request: CommandRequest, structured_data: dict[str, Any]) -> list[str]:
    terms: list[str] = []
    for value in (request.target, request.market_scope, request.theme_scope, request.candidate_pool):
        if value:
            terms.extend(_split_terms(str(value)))
    stock = structured_data.get("stock") or {}
    if isinstance(stock, dict):
        terms.extend(_split_terms(str(stock.get("code") or "")))
        terms.extend(_split_terms(str(stock.get("name") or "")))
    if request.command in {"macro", "topic_maintain", "value_scan"}:
        terms.extend(GENERAL_TAIWAN_MARKET_TERMS)
    if request.command == "theme":
        terms.extend(("題材", "供應鏈", "族群", "概念股"))
    if request.command == "theme_radar":
        terms.extend(("台股", "上市櫃", "題材", "族群", "類股輪動", "漲停", "量增", "供應鏈"))
    if request.command == "sector_strength":
        terms.extend(("台股", "上市櫃", "類股", "族群", "強勢股", "量增", "漲停"))
    deduped: list[str] = []
    seen: set[str] = set()
    for term in terms:
        clean = term.strip()
        if request.command in {"theme_radar", "sector_strength"} and clean.lower() in {"market", "latest"}:
            continue
        if len(clean) < 2 or clean.lower() in seen:
            continue
        seen.add(clean.lower())
        deduped.append(clean)
    return deduped


def _split_terms(value: str) -> list[str]:
    return [part for part in re.split(r"[\s,，、/|;；:：()（）\[\]【】]+", value) if part.strip()]


def _news_matches_terms(item: NewsItem, terms: list[str], command: str) -> bool:
    text = " ".join(
        [
            item.title or "",
            item.summary or "",
            item.full_text or "",
            item.category or "",
            " ".join(item.related_symbols or []),
            " ".join(item.related_topics or []),
        ]
    ).lower()
    if not terms:
        return True
    if any(term.lower() in text for term in terms):
        return True
    return command in {"macro", "topic_maintain"} and any(term.lower() in text for term in GENERAL_TAIWAN_MARKET_TERMS)


def _news_sort_key(item: NewsItem, analysis_date: date, terms: list[str]) -> tuple[int, int, int]:
    published = parse_date_like(item.published_at) or parse_date_like(item.created_at) or date.min
    age_days = max((analysis_date - published).days, 0)
    recency_score = max(365 - age_days, 0)
    text = f"{item.title} {item.summary} {item.full_text}".lower()
    relevance = sum(1 for term in terms if term.lower() in text)
    return (recency_score, int(item.importance_score or 0), relevance)


def _news_item_to_context(item: NewsItem, analysis_date: date, terms: list[str]) -> dict[str, Any]:
    item = apply_news_signal_tags(item)
    published = parse_date_like(item.published_at) or parse_date_like(item.created_at)
    return {
        "title": item.title,
        "url": item.url,
        "source": item.source,
        "published_at": item.published_at,
        "days_before_analysis_date": (analysis_date - published).days if published else None,
        "category": item.category,
        "related_symbols": item.related_symbols,
        "related_topics": item.related_topics,
        "importance_score": item.importance_score,
        "tags": item.tags,
        "news_signal_score": item.news_signal_score,
        "news_heat_risk_score": item.news_heat_risk_score,
        "news_signal_reason": item.news_signal_reason,
        "news_heat_risk_reason": item.news_heat_risk_reason,
        "summary": item.summary,
        "matched_terms": [term for term in terms if term.lower() in f"{item.title} {item.summary} {item.full_text}".lower()][:10],
    }


def augment_discovery_tasks_with_date_context(
    request: CommandRequest,
    structured_data: dict[str, Any],
    tasks: list[dict[str, Any]],
    max_added_per_task: int = 4,
) -> list[dict[str, Any]]:
    if not tasks:
        return tasks
    analysis_date = analysis_date_for_request(request)
    terms = _date_context_base_terms(request, structured_data)
    date_tokens = _date_query_tokens(analysis_date)
    for task in tasks:
        queries = task.setdefault("queries", [])
        added = 0
        base_terms = terms or [str(request.target or request.market_scope or request.theme_scope or "台股")]
        for term in base_terms[:2]:
            for token in date_tokens:
                if added >= max_added_per_task:
                    break
                queries.append(f"{term} {token}")
                added += 1
            if added >= max_added_per_task:
                break
        objective = str(task.get("objective") or "")
        task["objective"] = (
            f"{objective}\n\n日期規則：analysis_date={analysis_date.isoformat()}；"
            "優先搜尋該日與該日前近期資料；若資料不足，逐步擴大回看範圍；不得使用晚於 analysis_date 的資料。"
        ).strip()
    return tasks


def _date_query_tokens(analysis_date: date) -> list[str]:
    quarter = (analysis_date.month - 1) // 3 + 1
    return [
        analysis_date.isoformat(),
        f"{analysis_date.year}/{analysis_date.month:02d}/{analysis_date.day:02d}",
        f"{analysis_date.year}年{analysis_date.month}月",
        f"{analysis_date.year} Q{quarter}",
    ]


def _date_context_base_terms(request: CommandRequest, structured_data: dict[str, Any]) -> list[str]:
    if request.command == "theme_radar":
        return ["台股 題材 輪動", "上市櫃 漲停 量增 族群"]
    if request.command == "sector_strength":
        return ["台股 類股 強弱", "上市櫃 族群 輪動"]
    return extract_target_terms(request, structured_data)[:6]


def filter_and_sort_sources_for_analysis_date(
    sources: Iterable[SourceItem],
    request: CommandRequest,
) -> tuple[list[SourceItem], list[str]]:
    analysis_date = analysis_date_for_request(request)
    kept: list[SourceItem] = []
    dropped: list[str] = []
    for source in sources:
        if is_irrelevant_market_source(source, request.command):
            dropped.append(source.source_id)
            continue
        published = parse_date_like(source.published_date)
        if published and published > analysis_date:
            dropped.append(source.source_id)
            continue
        kept.append(source)
    kept.sort(key=lambda source: _source_sort_key(source, analysis_date), reverse=True)
    return [_renumber_source(source, index) for index, source in enumerate(kept)], dropped


def _source_sort_key(source: SourceItem, analysis_date: date) -> tuple[int, int]:
    published = parse_date_like(source.published_date)
    if not published:
        return (0, 0)
    age_days = max((analysis_date - published).days, 0)
    return (max(365 - age_days, 0), 1)


def _renumber_source(source: SourceItem, index: int) -> SourceItem:
    return replace(source, source_id=f"S{index + 1:03d}")
