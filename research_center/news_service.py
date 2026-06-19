"""News service — search, fetch, classify, summarize, and store news."""
from __future__ import annotations

import json
import os
import re
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .config import ROOT_DIR
from .ai_workflow_service import build_ai_workflow_coverage
from .models import CommandRequest, SourceItem
from .news_categories import (
    normalize_news_category,
    ordered_news_category_keys,
)
from .news_models import NewsDigest, NewsItem, HoldingNewsGroup, NewsPreference, apply_news_signal_tags
from .news_repository import NewsRepository
from .preferred_sources import build_site_queries, match_preferred_source
from .source_date_normalizer import normalize_published_date
from .source_rank import sort_sources_by_preferred_weight
from .web_fetch_enrichment import _enrich_sources_with_web_fetch
from .web_fetch_service import WebFetchService

ProgressCallback = Callable[[str], None]

NEWS_CATEGORIES = ordered_news_category_keys()

_NEWS_CATEGORY_TASK_LABELS = {
    "market_focus": "台股與大盤",
    "sector_rotation": "題材與族群輪動",
    "ai_semiconductor": "AI / 半導體",
    "supply_chain": "供應鏈與產業",
    "macro_policy": "政策 / 匯率 / 總經",
    "company_news": "個股利多利空",
}
_SCHEDULED_LIGHTWEIGHT_CORE_CATEGORIES = tuple(_NEWS_CATEGORY_TASK_LABELS)
_SCHEDULED_LIGHTWEIGHT_PRIMARY_MIN = 8


def build_news_discovery_queries(period: str = "latest") -> list[dict[str, Any]]:
    """Build category-balanced Taiwan finance discovery search queries."""
    today = datetime.now()
    today_dash = today.strftime("%Y-%m-%d")
    today_slash = today.strftime("%Y/%m/%d")
    date_hint = f"{today_dash} {today_slash}" if period != "7d" else "最近7天"
    recency_terms = "今日 最新 近24小時" if period != "7d" else "最近7天"
    base_queries = [
        {"title": "台股與大盤", "items": [
            f"台股 今日 大盤 盤勢 法人 買賣超 成交量 加權 櫃買 {date_hint}".strip(),
            f"台股 加權指數 櫃買指數 上市 上櫃 漲跌家數 盤中 盤後 焦點 {today_slash}".strip(),
            f"台股 {recency_terms} 大盤 族群 資金 流向".strip(),
            f"Taiwan stock market TWSE TPEX fund flow {today_dash}".strip(),
        ]},
        {"title": "題材與族群輪動", "items": [
            f"台股 題材 族群 輪動 強勢股 概念股 {date_hint}".strip(),
            f"台股 資金輪動 熱門族群 電子 傳產 金融 {today_slash}".strip(),
            f"台股 {recency_terms} 題材股 概念股 法人 點名".strip(),
            f"Taiwan stocks sector rotation theme stocks concept stocks {today_dash}".strip(),
        ]},
        {"title": "AI / 半導體", "items": [
            f"台股 AI 半導體 先進封裝 ASIC CoWoS HBM {date_hint}".strip(),
            f"AI伺服器 半導體 台積電 供應鏈 台股 新聞 {today_slash}".strip(),
            f"台股 {recency_terms} AI晶片 GPU ASIC CPO 半導體設備".strip(),
            f"Taiwan AI semiconductor stock news {today_dash}".strip(),
        ]},
        {"title": "供應鏈與產業", "items": [
            f"台股 供應鏈 產業新聞 PCB CCL 散熱 電源 {date_hint}".strip(),
            f"台灣 電子零組件 供應鏈 訂單 漲價 缺貨 新聞 {today_slash}".strip(),
            f"台股 {recency_terms} 產業鏈 受惠股 供應商 出貨".strip(),
            f"Taiwan electronics supply chain stock news {today_dash}".strip(),
        ]},
        {"title": "金融與高股息", "items": [
            f"台股 金融股 高股息 ETF 配息 殖利率 {date_hint}".strip(),
            f"金控 銀行 保險 證券 台股 新聞 {today_slash}".strip(),
            f"台股 {recency_terms} 金融 0056 00878 高股息 ETF".strip(),
            f"Taiwan financial high dividend stock news {today_dash}".strip(),
        ]},
        {"title": "政策 / 匯率 / 總經", "items": [
            f"台灣 財經 政策 匯率 利率 央行 台股 影響 VIX 美債 美元 {date_hint}".strip(),
            f"台幣 匯率 出口 景氣 通膨 台股 新聞 Fed 油價 關稅 {today_slash}".strip(),
            f"台股 {recency_terms} 政策 法規 總經 產業影響 VIX 美債 美元 戰爭".strip(),
            f"Taiwan macro policy interest rate exchange rate VIX DXY oil tariff stock {today_dash}".strip(),
        ]},
        {"title": "台指期與盤前風險", "items": [
            f"台指期 夜盤 跌停 急跌 盤前風險 VIX {date_hint}".strip(),
            f"台灣期貨 台指期 夜盤 重挫 跌停 台股 影響 {today_slash}".strip(),
            f"台股 {recency_terms} 台指期 台指選擇權 Put Call 未平倉 盤前 重挫".strip(),
            f"Taiwan futures TAIEX options put call open interest VIX night session {today_dash}".strip(),
        ]},
        {"title": "個股利多利空", "items": [
            f"台股 個股 利多 利空 營收 法說會 股價 目標價 {date_hint}".strip(),
            f"台股 公司 營收 EPS 外資 投信 調升 調降 {today_slash}".strip(),
            f"台股 {recency_terms} 公司訂單 展望 財報 漲停 跌停".strip(),
            f"Taiwan stock company earnings revenue target price upgrade downgrade {today_dash}".strip(),
        ]},
        {"title": "營收與法人", "items": [
            f"台股 月營收 法人 買超 賣超 投信 外資 {date_hint}".strip(),
            f"台股 營收成長 法人同步加碼 個股 新聞 {today_slash}".strip(),
            f"台股 {recency_terms} 營收創高 法人報告 財測 展望".strip(),
            f"Taiwan stock revenue foreign investor institutional {today_dash}".strip(),
        ]},
        {"title": "個股公告 / MOPS", "items": [
            f"公開資訊觀測站 重大訊息 月營收 法說會 台股 {date_hint}".strip(),
            f"MOPS 重大訊息 公司公告 月營收 法人說明會 {today_slash}".strip(),
            f"台股 {recency_terms} 公司公告 法說會 簡報 月營收".strip(),
            f"Taiwan MOPS material information monthly revenue investor conference {today_dash}".strip(),
        ]},
        {"title": "資金與籌碼", "items": [
            f"台股 三大法人 投信 融資融券 大戶持股 買賣超 {date_hint}".strip(),
            f"台股 外資 投信 自營商 融資 餘額 大戶 TDCC {today_slash}".strip(),
            f"台股 {recency_terms} 籌碼 集保 大戶 外資期貨".strip(),
            f"Taiwan stocks institutional flow margin financing TDCC futures {today_dash}".strip(),
        ]},
        {"title": "反證與利空", "items": [
            f"台股 題材退燒 利空 毛利下滑 庫存 需求轉弱 {date_hint}".strip(),
            f"台股 個股 營收衰退 砍單 庫存調整 風險 新聞 {today_slash}".strip(),
            f"台股 {recency_terms} 利空 退燒 轉弱 毛利率 下滑".strip(),
            f"Taiwan stocks negative news inventory demand slowdown margin decline {today_dash}".strip(),
        ]},
        {"title": "科技與AI供應鏈", "items": [
            f"台股 科技 AI 供應鏈 訂單 伺服器 網通 {date_hint}".strip(),
            f"台股 科技股 AI供應鏈 新產品 新客戶 新訂單 {today_slash}".strip(),
            f"台股 {recency_terms} 科技 產業鏈 半導體 電子零組件".strip(),
            f"Taiwan tech AI supply chain order news {today_dash}".strip(),
        ]},
    ]

    tasks = []
    for group in base_queries:
        tasks.append({
            "label": group.get("title", ""),
            "objective": f"搜尋台股財經新聞：{group.get('title', '新聞')}",
            "exclude": ["行情頁", "報價頁", "個股列表頁", "首頁", "泛國際新聞"],
            "queries": [group],
        })

    # Add limited site: queries per task.
    MAX_SITE_PER_TASK = 4
    for task in tasks:
        existing_items: list[str] = []
        queries = task.get("queries") or []
        for q in queries:
            if isinstance(q, dict):
                existing_items.extend(q.get("items", []))
            elif str(q).strip():
                existing_items.append(q)
        added = 0
        for base_query in existing_items[:2]:
            if added >= MAX_SITE_PER_TASK:
                break
            site_qs = build_site_queries(base_query, max_domains=MAX_SITE_PER_TASK)
            for sq in site_qs:
                if added >= MAX_SITE_PER_TASK:
                    break
                queries.append(sq)
                added += 1
    if os.environ.get("NEWS_SMOKE_TEST") == "1":
        try:
            task_limit = int(os.environ.get("NEWS_SMOKE_TASK_LIMIT", "2"))
        except ValueError:
            task_limit = 2
        if task_limit > 0:
            tasks = tasks[:task_limit]
    tasks = _filter_news_tasks_by_env(tasks)
    return tasks


def _filter_news_tasks_by_env(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    raw = os.environ.get("NEWS_REFRESH_TASK_CATEGORIES", "")
    requested = {part.strip() for part in raw.split(",") if part.strip()}
    if not requested:
        return tasks
    allowed_labels = set(requested)
    for key in requested:
        label = _NEWS_CATEGORY_TASK_LABELS.get(key)
        if label:
            allowed_labels.add(label)
    filtered = [task for task in tasks if str(task.get("label") or "") in allowed_labels]
    return filtered or tasks

def build_holding_news_discovery_queries(portfolio: dict[str, str]) -> list[dict[str, Any]]:
    """Build focused news queries for portfolio holdings."""
    tasks: list[dict[str, Any]] = []
    today = datetime.now()
    today_dash = today.strftime("%Y-%m-%d")
    today_slash = today.strftime("%Y/%m/%d")
    for code, name in list(portfolio.items())[:30]:
        code = str(code).strip()
        name = str(name).strip()
        if not code and not name:
            continue
        label = f"holding_{code or name}"
        query_name = name or code
        tasks.append({
            "label": label,
            "objective": f"Find Taiwan finance news related to holding {code} {name}".strip(),
            "queries": [
                f"{code} {query_name} 今日新聞 台股 {today_dash} {today_slash}",
                f"{query_name} 法說 營收 外資 產業 新聞 {today_dash}",
                f"{query_name} 台股 財經 股票 今日 {today_slash}",
                f"{code} {query_name} 利多 利空 毛利率 庫存 客戶 訂單 {today_dash}",
                f"{query_name} 目標價 下修 評等 法人報告 新聞 {today_slash}",
            ],
            "holding_code": code,
            "holding_name": name,
        })
    return tasks


def _flatten_news_task_queries(task: dict[str, Any], limit: int | None = None) -> list[str]:
    queries: list[str] = []
    for q in task.get("queries", []) or []:
        if isinstance(q, dict):
            queries.extend(str(item).strip() for item in q.get("items", []) if str(item).strip())
        elif str(q).strip():
            queries.append(str(q).strip())
    if limit is not None and limit > 0:
        return queries[:limit]
    return queries


_GENERIC_NEWS_TITLES = {
    "",
    "readmo.ai - 投資網誌",
    "readmo.ai",
    "投資網誌",
    "cmoney",
}


def _clean_markdown_heading(text: str) -> str:
    cleaned = str(text or "").strip()
    cleaned = cleaned.strip("#").strip()
    for token in ("**", "__", "`"):
        cleaned = cleaned.replace(token, "")
    return " ".join(cleaned.split())


def _extract_first_markdown_h1(text: str) -> str:
    """Return the first useful Markdown H1 from fetched article text."""
    import re

    for line in str(text or "").splitlines():
        candidate = line.strip()
        if not candidate.startswith("# ") or candidate.startswith("## "):
            continue
        candidate = _clean_markdown_heading(candidate)
        if not candidate:
            continue
        lower = candidate.lower()
        if lower in _GENERIC_NEWS_TITLES:
            continue
        if candidate.startswith("!") or "logo" in lower or "powered by" in lower:
            continue
        if len(candidate) < 6 or len(candidate) > 120:
            continue
        if re.fullmatch(r"[\W_]+", candidate):
            continue
        return candidate
    return ""


def _is_generic_news_title(title: str) -> bool:
    raw = " ".join(str(title or "").split()).strip()
    lower = raw.lower()
    if lower in _GENERIC_NEWS_TITLES:
        return True
    if lower.endswith(" - 投資網誌"):
        return True
    if raw in {"新聞", "投資", "文章", "市場焦點"}:
        return True
    return False


def _normalize_news_title(title: str, article_text: str = "") -> str:
    """Replace generic site titles with the first article H1 when available."""
    raw = " ".join(str(title or "").split()).strip()
    if not _is_generic_news_title(raw):
        return raw
    h1 = _extract_first_markdown_h1(article_text)
    return h1 or raw


def _apply_news_title_cleanup(items: list[NewsItem]) -> list[NewsItem]:
    """Normalize titles in-place for newly fetched and legacy stored news."""
    for item in items:
        item.title = _normalize_news_title(item.title, item.full_text or item.summary or "")
    return items


def _sources_to_news_items(sources: list[SourceItem]) -> list[NewsItem]:
    """Convert SourceItem list to NewsItem list."""
    items: list[NewsItem] = []
    seen: set[str] = set()
    for src in sources:
        url = (src.url or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        matched = match_preferred_source(url)
        source_name = matched.get("name") if matched else (src.provider or "")
        summary = src.snippet or ""
        title = _normalize_news_title(src.title or "", summary)
        items.append(apply_news_signal_tags(NewsItem(
            id=str(uuid.uuid4())[:8],
            title=title,
            url=url,
            source=source_name,
            published_at=src.published_date or "",
            summary=summary,
            full_text=summary,
            news_origin="refresh",
        )))
    return items


def _deduplicate_items(items: list[NewsItem]) -> list[NewsItem]:
    """Deduplicate by URL and title+source."""
    seen_urls: set[str] = set()
    seen_keys: set[str] = set()
    result: list[NewsItem] = []
    for item in items:
        key = f"{item.title.strip()}|{item.source.strip()}|{item.published_at[:10] if item.published_at else ''}"
        if item.url in seen_urls or key in seen_keys:
            continue
        seen_urls.add(item.url)
        seen_keys.add(key)
        result.append(item)
    return result


def _backfill_refresh_item_dates(items: list[NewsItem]) -> list[NewsItem]:
    now_text = datetime.now().isoformat(timespec="seconds")
    for item in items:
        if getattr(item, "news_origin", "refresh") != "refresh":
            continue
        if not item.created_at:
            item.created_at = now_text
        if item.published_at:
            normalized = normalize_published_date(item.published_at)
            if normalized:
                item.published_at = normalized
            continue
        inferred = normalize_published_date(
            item.title,
            item.summary,
            item.full_text,
            item.url,
            {"created_at": item.created_at, "news_origin": item.news_origin},
        )
        if inferred:
            item.published_at = inferred
    return items


def _collect_holding_news_sources(
    center: Any,
    portfolio: dict[str, str],
    request: CommandRequest,
    progress: ProgressCallback | None = None,
) -> list[SourceItem]:
    """Search focused portfolio-holding news through configured search providers."""
    if not portfolio:
        return []
    tasks = build_holding_news_discovery_queries(portfolio)
    sources: list[SourceItem] = []

    def emit(message: str) -> None:
        if progress:
            progress(message)

    if hasattr(center, "minimax_search") and center.minimax_search is not None:
        try:
            if center.minimax_search.is_configured():
                for task in tasks:
                    for query in _flatten_news_task_queries(task, limit=2):
                        try:
                            result = center.minimax_search.search(query)
                            for row in (result.get("results") or [])[:5]:
                                sources.append(SourceItem(
                                    source_id=f"H{len(sources)+1:03d}",
                                    title=str(row.get("title") or ""),
                                    url=str(row.get("url") or ""),
                                    source_level="Level 3",
                                    published_date=row.get("published_date"),
                                    snippet=str(row.get("snippet") or ""),
                                    provider="minimax_mcp_search",
                                    provider_detail=str(task.get("label") or ""),
                                    found_by=[str(task.get("holding_code") or ""), str(task.get("holding_name") or "")],
                                ))
                        except Exception:
                            continue
        except Exception as exc:
            emit(f"Holding MiniMax search failed: {exc}")

    if not sources and hasattr(center, "tavily_search") and center.tavily_search is not None:
        try:
            if center.tavily_search.is_configured():
                tavily_tasks = [
                    {"label": task.get("label"), "queries": _flatten_news_task_queries(task, limit=2)}
                    for task in tasks
                ]
                result = center.tavily_search.discover(request, tavily_tasks, progress=progress)
                sources.extend(result.sources)
        except Exception as exc:
            emit(f"Holding Tavily search failed: {exc}")

    return sources


def _news_quality_score(item: NewsItem, portfolio: dict[str, str] | None = None) -> int:
    text = f"{item.title} {item.summary} {item.full_text}".lower()
    score = int(item.importance_score or 0)
    preferred = match_preferred_source(item.url)
    if preferred:
        score += int(preferred.get("weight", 0) or 0)
    if item.full_text and len(item.full_text) >= 300:
        score += 25
    if item.summary and len(item.summary) >= 80:
        score += 10
    for keyword in ("taiwan", "台股", "股票", "半導體", "ai", "營收", "法說", "外資", "產業", "金融", "匯率", "政策"):
        if keyword.lower() in text:
            score += 8
    if portfolio:
        for code, name in portfolio.items():
            if str(code).lower() in text or (name and str(name).lower() in text):
                score += 35
                break
    if len(item.summary or "") < 30:
        score -= 10
    return score


def _rank_news_for_ai(items: list[NewsItem], portfolio: dict[str, str] | None = None) -> list[NewsItem]:
    return sorted(items, key=lambda item: _news_quality_score(item, portfolio), reverse=True)


def _classify_limit() -> int:
    try:
        return int(os.environ.get("NEWS_AI_CLASSIFY_LIMIT", "18"))
    except ValueError:
        return 18


def _env_int(name: str, default: int = 0) -> int:
    try:
        return int(os.environ.get(name, str(default)) or default)
    except ValueError:
        return default


def _classify_batch_size() -> int:
    try:
        return max(1, int(os.environ.get("NEWS_AI_CLASSIFY_BATCH_SIZE", "3")))
    except ValueError:
        return 3


def _classify_timeout_seconds() -> float:
    try:
        return max(1.0, float(os.environ.get("NEWS_AI_CLASSIFY_TIMEOUT_SECONDS", "45")))
    except ValueError:
        return 45.0


def _classify_text_limit() -> int:
    try:
        return max(0, int(os.environ.get("NEWS_AI_CLASSIFY_TEXT_LIMIT", "500")))
    except ValueError:
        return 500


def _classify_retry_text_limit() -> int:
    try:
        return max(0, int(os.environ.get("NEWS_AI_CLASSIFY_RETRY_TEXT_LIMIT", "120")))
    except ValueError:
        return 120


def _news_high_tier_classify_limit() -> int:
    try:
        return max(0, int(os.environ.get("NEWS_HIGH_TIER_CLASSIFY_LIMIT", "12")))
    except ValueError:
        return 12


def _select_high_tier_news_items(items: list[NewsItem], limit: int) -> list[NewsItem]:
    if limit <= 0:
        return []
    ranked = sorted(items, key=_high_tier_news_score, reverse=True)
    return ranked[:limit]


def _high_tier_news_score(item: NewsItem) -> int:
    text = f"{item.title} {item.summary} {' '.join(item.related_topics or [])}".lower()
    score = int(item.importance_score or 0)
    preferred = match_preferred_source(item.url)
    if preferred:
        score += int(preferred.get("weight", 0) or 0)
    if item.source and any(key in item.source.lower() for key in ("cna", "中央社", "工商", "經濟日報", "鉅亨", "moneydj")):
        score += 30
    if item.news_signal_score:
        score += int(item.news_signal_score)
    for keyword in (
        "重大訊息", "法說", "財報", "月營收", "ai", "半導體", "伺服器", "關稅",
        "fed", "匯率", "油價", "戰爭", "政策", "降息", "升息", "供應鏈",
    ):
        if keyword in text:
            score += 12
    if item.impact_direction in {"positive", "negative", "mixed"}:
        score += 15
    return score


def _tag_portfolio_news_items(items: list[NewsItem], portfolio: dict[str, str]) -> list[NewsItem]:
    if not portfolio:
        return items
    for item in items:
        text = f"{item.title} {item.summary} {item.full_text}".lower()
        symbols = list(item.related_symbols or [])
        topics = list(item.related_topics or [])
        for code, name in portfolio.items():
            code_s = str(code).strip()
            name_s = str(name).strip()
            if (code_s and code_s.lower() in text) or (name_s and name_s.lower() in text):
                if code_s and code_s not in symbols:
                    symbols.append(code_s)
                if name_s and name_s not in topics:
                    topics.append(name_s)
        item.related_symbols = symbols
        item.related_topics = topics
    return items


def _parse_news_datetime(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    lower = text.lower()
    now = datetime.now()
    if "hour" in lower or "小時" in lower:
        digits = "".join(ch for ch in lower if ch.isdigit())
        if digits:
            return now - timedelta(hours=int(digits))
    if "minute" in lower or "分鐘" in lower or "min" in lower:
        digits = "".join(ch for ch in lower if ch.isdigit())
        if digits:
            return now - timedelta(minutes=int(digits))
    if "day" in lower or "天前" in lower:
        digits = "".join(ch for ch in lower if ch.isdigit())
        if digits:
            return now - timedelta(days=int(digits))
    normalized = text.replace("Z", "+00:00")
    for fmt in (
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y.%m.%d",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%b %d, %Y",
        "%B %d, %Y",
    ):
        try:
            return datetime.strptime(normalized[:len(fmt)], fmt)
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(normalized)
        return parsed.replace(tzinfo=None)
    except ValueError:
        return None


_EMBEDDED_DATE_PATTERNS = (
    re.compile(r"(?P<year>20\d{2})[./-](?P<month>\d{1,2})[./-](?P<day>\d{1,2})"),
    re.compile(r"(?P<year>20\d{2})\s*年\s*(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*日?"),
)


def _extract_embedded_news_datetime(item: NewsItem) -> datetime | None:
    """Extract explicit publish-like dates from fetched article text."""
    text = " ".join(
        part
        for part in (item.title, item.summary, item.full_text)
        if part
    )
    for pattern in _EMBEDDED_DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        try:
            return datetime(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
            )
        except ValueError:
            continue
    return None


def _is_trusted_refresh_news(item: NewsItem) -> bool:
    if getattr(item, "news_origin", "refresh") != "refresh":
        return False
    if _is_non_article_page(item):
        return False
    matched = match_preferred_source(item.url or "")
    if not matched:
        return False
    level = str(matched.get("level") or "").lower()
    return level.startswith("l1_") or level.startswith("l2_")


def _effective_news_datetime(item: NewsItem) -> datetime | None:
    """Return the display recency timestamp for a news item.

    Prefer the source-provided published_at. Only /news refresh items may
    fall back to created_at when the source omitted publish time and no
    explicit date can be extracted from the fetched content.
    """
    published_text = (item.published_at or "").strip()
    published = _parse_news_datetime(published_text)
    if published is not None:
        return published
    if published_text:
        return None
    if getattr(item, "news_origin", "refresh") == "refresh":
        embedded = _extract_embedded_news_datetime(item)
        if embedded is not None:
            return embedded
        if _is_trusted_refresh_news(item):
            return _parse_news_datetime(item.created_at)
    return None


def _query_refresh_news(repository: NewsRepository, hours: int) -> list[NewsItem]:
    """Query news generated by /news refresh only, with fake-repository fallback."""
    try:
        return repository.query_recent(hours=hours, news_origin="refresh")
    except TypeError:
        items = repository.query_recent(hours=hours)
        return [item for item in items if getattr(item, "news_origin", "refresh") == "refresh"]


def _filter_by_published_window(items: list[NewsItem], hours: int, keep_unknown_date: bool = True) -> list[NewsItem]:
    cutoff = datetime.now() - timedelta(hours=hours)
    result: list[NewsItem] = []
    for item in items:
        published_text = (item.published_at or "").strip().lower()
        if hours <= 48 and re.search(r"\b(?:2|[3-9]|\d{2,})\s*days?\b", published_text):
            continue
        if hours <= 48 and re.search(r"(?:2|[3-9]|\d{2,})\s*天前", published_text):
            continue
        published = _effective_news_datetime(item)
        if published is None:
            if keep_unknown_date:
                result.append(item)
            continue
        if published >= cutoff:
            result.append(item)
    return result


def _boost_latest_recency(items: list[NewsItem], primary_hours: int = 24) -> list[NewsItem]:
    """Prefer primary-window news while still allowing recent fallback items."""
    cutoff = datetime.now() - timedelta(hours=primary_hours)
    for item in items:
        published = _effective_news_datetime(item)
        if published is not None and published >= cutoff:
            item.importance_score = int(item.importance_score or 0) + 1000
    return items


def _has_explicit_news_datetime(item: NewsItem) -> bool:
    """Return True when source content provides a publish-like timestamp."""
    published_text = (item.published_at or "").strip()
    if published_text and _parse_news_datetime(published_text) is not None:
        return True
    return _extract_embedded_news_datetime(item) is not None


def _penalize_implicit_news_dates(items: list[NewsItem], penalty: int = 250) -> list[NewsItem]:
    """Keep no-published-date articles visible but rank them below explicit-date news."""
    for item in items:
        if not _has_explicit_news_datetime(item):
            item.importance_score = int(item.importance_score or 0) - penalty
    return items


def run_news_refresh(
    center: Any,
    repository: NewsRepository,
    progress: ProgressCallback | None = None,
    ai_model: str = "gemini",
) -> tuple[list[NewsItem], dict[str, Any]]:
    """Refresh news: search, fetch, classify, save.

    Args:
        center: ResearchCenter instance with AI model clients
        repository: NewsRepository for persistence
        progress: Optional progress callback
        ai_model: AI model to use for classification ("gemini", "deepseek", "minimax")
    """
    def emit(msg: str) -> None:
        if progress:
            progress(msg)

    # Smoke test mode: limit MiniMax MCP Search queries per task to 2 to avoid long runs
    smoke_test = os.environ.get("NEWS_SMOKE_TEST") == "1"
    if smoke_test and hasattr(center, "minimax_search") and center.minimax_search is not None:
        original_limit = getattr(center.minimax_search, "max_queries_per_task", 0)
        if original_limit <= 0:
            center.minimax_search.max_queries_per_task = 2
    else:
        original_limit = None

    # Smoke test mode: skip MiniMax MCP Search entirely (use Tavily only)
    skip_minimax = os.environ.get("NEWS_SKIP_MINIMAX_SEARCH") == "1"
    saved_minimax_enabled = None
    if skip_minimax and hasattr(center, "minimax_search") and center.minimax_search is not None:
        # _GeminiDiscoveryRunner checks center.config.enable_minimax_search
        if hasattr(center, "config") and hasattr(center.config, "enable_minimax_search"):
            saved_minimax_enabled = center.config.enable_minimax_search
            # ResearchCenterConfig is frozen, use object.__setattr__ to bypass
            object.__setattr__(center.config, "enable_minimax_search", False)
        elif hasattr(center.minimax_search, "_config") and hasattr(center.minimax_search._config, "enable_minimax_search"):
            saved_minimax_enabled = center.minimax_search._config.enable_minimax_search
            object.__setattr__(center.minimax_search._config, "enable_minimax_search", False)

    try:
        emit("新聞搜尋開始")
        tasks = build_news_discovery_queries("latest")
        try:
            from portfolio_manager import load_portfolio
            portfolio = load_portfolio()
        except Exception:
            portfolio = {}

        # Collect sources via discovery
        all_sources: list[SourceItem] = []
        minimax_diag: dict[str, Any] = {}
        try:
            from .orchestrator import _GeminiDiscoveryRunner
            request = CommandRequest(
                command="news", raw_text="/news refresh", target="台股財經新聞",
                target_type="news",
                mode="normal", source_only=False, score=False, brief=False,
                top=None, ai_model="gemini", report_date=None,
                output_formats=("md",), user_id="", created_at=None,
            )
            structured_data_discovery: dict[str, Any] = {}
            runner = _GeminiDiscoveryRunner(center)
            sources_out, _ = runner.run_discovery_flow(
                request, sources=list(all_sources), structured_data=structured_data_discovery,
                use_grounding=True, progress=progress,
            )
            all_sources.extend(sources_out)
            minimax_diag = structured_data_discovery.get("minimax_search_discovery", {})
        except Exception as exc:
            emit(f"Discovery 搜尋略過：{exc}")

        holding_sources = _collect_holding_news_sources(
            center,
            portfolio,
            CommandRequest(
                command="news", raw_text="/news refresh holdings", target="portfolio holdings",
                target_type="news", mode="normal", source_only=False, score=False, brief=False,
                top=None, ai_model=ai_model, report_date=None, output_formats=("md",), user_id="", created_at=None,
            ),
            progress=progress,
        )
        if holding_sources:
            all_sources.extend(holding_sources)
            emit(f"Holding news sources: {len(holding_sources)}")

        # Fallback: try direct search via center's search services
        if not all_sources:
            try:
                if hasattr(center, "minimax_search") and center.minimax_search.is_configured():
                    for task in tasks:
                        flat_queries = _flatten_news_task_queries(task)
                        for q in flat_queries[:3]:
                            try:
                                result = center.minimax_search.search(q)
                                for r in result.get("results", []):
                                    all_sources.append(SourceItem(
                                        source_id=f"N{len(all_sources)+1:03d}",
                                        title=r.get("title", ""),
                                        url=r.get("url", ""),
                                        source_level="Level 3",
                                        published_date=r.get("published_date"),
                                        snippet=r.get("snippet", ""),
                                    ))
                            except Exception:
                                pass
            except Exception:
                pass

        emit(f"搜尋完成，共 {len(all_sources)} 筆來源")

        # Sort by preferred weight and fetch content
        sorted_sources = sort_sources_by_preferred_weight(all_sources)
        total_source_count = len(sorted_sources)
        refresh_max_sources = _env_int("NEWS_REFRESH_MAX_SOURCES", 0)
        if refresh_max_sources > 0 and len(sorted_sources) > refresh_max_sources:
            emit(f"News refresh source limit: {len(sorted_sources)} -> {refresh_max_sources}")
            sorted_sources = sorted_sources[:refresh_max_sources]
        if smoke_test:
            try:
                max_sources = int(os.environ.get("NEWS_SMOKE_MAX_SOURCES", "5"))
            except ValueError:
                max_sources = 5
            if max_sources > 0 and len(sorted_sources) > max_sources:
                emit(f"Smoke mode: limit news sources {len(sorted_sources)} -> {max_sources}")
                sorted_sources = sorted_sources[:max_sources]

        # WebFetch enrichment
        structured_data: dict[str, Any] = {}
        fetch_request = CommandRequest(
            command="news", raw_text="/news refresh", target="", target_type="news",
            mode="normal", source_only=False, score=False, brief=False,
            top=None, ai_model="gemini", report_date=None,
            output_formats=("md",), user_id="", created_at=None,
        )
        try:
            _enrich_sources_with_web_fetch(fetch_request, sorted_sources, structured_data, progress=progress)
        except Exception as exc:
            emit(f"WebFetch 略過：{exc}")

        web_fetch_diag = structured_data.get("web_fetch_diagnostics") or {}
        webfetch_success = int(web_fetch_diag.get("enriched_source_count") or web_fetch_diag.get("successful") or 0)

        # Convert to NewsItem
        items = _sources_to_news_items(sorted_sources)
        items = _apply_news_title_cleanup(items)
        items = _deduplicate_items(items)
        emit(f"去重後 {len(items)} 筆")

        # Filter to Taiwan-finance relevant only
        items = _filter_taiwan_finance_news(items)
        items = _filter_by_published_window(items, hours=168)
        emit(f"台灣過濾後 {len(items)} 筆")

        items = _rank_news_for_ai(items, portfolio)
        filtered_count = len(items)
        classify_limit = _classify_limit()
        refresh_classify_limit = _env_int("NEWS_REFRESH_CLASSIFY_LIMIT", 0)
        if refresh_classify_limit > 0:
            classify_limit = min(classify_limit, refresh_classify_limit) if classify_limit > 0 else refresh_classify_limit
        if classify_limit > 0 and len(items) > classify_limit:
            emit(f"AI classification limit: {len(items)} -> {classify_limit}")
            items = items[:classify_limit]
        if smoke_test:
            try:
                classify_limit = int(os.environ.get("NEWS_SMOKE_CLASSIFY_LIMIT", "5"))
            except ValueError:
                classify_limit = 5
            if classify_limit > 0 and len(items) > classify_limit:
                emit(f"Smoke mode: limit AI classification {len(items)} -> {classify_limit}")
                items = items[:classify_limit]

        # Batch classify with AI
        primary_classifier = _primary_news_classifier_model(center, ai_model)
        classified = _batch_classify_news(items, center, emit, ai_model=ai_model)
        classified = _tag_portfolio_news_items(classified, portfolio)

        for item in classified:
            item.news_origin = "refresh"
            item.category = _correct_news_category(item)
        classified = _backfill_refresh_item_dates(classified)

        category_counts = Counter(normalize_news_category(item.category) for item in classified)
        holding_count = sum(1 for item in classified if _matches_portfolio(item, portfolio))
        if category_counts:
            ordered = [
                f"{cat}:{category_counts[cat]}"
                for cat in ordered_news_category_keys()
                if category_counts.get(cat)
            ]
            extras = [
                f"{cat}:{count}"
                for cat, count in category_counts.items()
                if cat not in set(ordered_news_category_keys())
            ]
            emit(f"分類統計：{', '.join(ordered + extras)}")
        emit(f"持股新聞候選：{holding_count}")

        # Save to repository
        saved, skipped = repository.save_many(classified)
        emit(f"儲存完成：新增 {saved}，略過 {skipped}")

        low_digest_meta = {
            "schema_version": "low_model_digest_v1",
            "status": "success" if primary_classifier == "minimax_low" else "skipped",
            "model": "MiniMax-M3",
            "reason": "news_general_classification" if primary_classifier == "minimax_low" else "low_model_news_classifier_not_used",
        }
        ai_workflow_coverage = build_ai_workflow_coverage(
            "news",
            local_data_package=True,
            low_model_digest=low_digest_meta,
            high_model_input_package=True,
            dedupe_strategy="news_batch_deduped_classification",
            source_index=True,
            input_audit=True,
            html_sections=False,
            diagnostics={
                "classified_count": len(classified),
                "filtered_count": filtered_count,
                "search_sources": total_source_count,
                "primary_classifier": primary_classifier,
                "high_tier_model": ai_model,
                "high_tier_limit": _news_high_tier_classify_limit(),
            },
            notes=["News refresh 是資料維護型 AI 流程，不產出投研 HTML 報告。"],
            not_applicable=["html_sections"],
        )

        return classified, {
            "saved": saved,
            "skipped": skipped,
            "total": len(classified),
            "search_sources": total_source_count,
            "smoke_sources_used": len(sorted_sources),
            "webfetch_success": webfetch_success,
            "filtered_count": filtered_count,
            "category_counts": dict(category_counts),
            "holding_count": holding_count,
            "minimax_diagnostics": minimax_diag,
            "web_fetch_diagnostics": web_fetch_diag,
            "ai_workflow_coverage": ai_workflow_coverage,
        }
    finally:
        # Restore original MiniMax search limit after smoke test
        if original_limit is not None and hasattr(center, "minimax_search") and center.minimax_search is not None:
            center.minimax_search.max_queries_per_task = original_limit
        # Restore MiniMax enabled flag if it was temporarily disabled
        if saved_minimax_enabled is not None:
            if hasattr(center, "config") and hasattr(center.config, "enable_minimax_search"):
                object.__setattr__(center.config, "enable_minimax_search", saved_minimax_enabled)
            elif hasattr(center, "minimax_search") and hasattr(center.minimax_search, "_config"):
                object.__setattr__(center.minimax_search._config, "enable_minimax_search", saved_minimax_enabled)


def _batch_classify_news(
    items: list[NewsItem],
    center: Any,
    emit: Callable[[str], None],
    ai_model: str = "gemini",
) -> list[NewsItem]:
    """Batch classify and summarize news via AI.
    
    Args:
        items: List of NewsItem to classify
        center: ResearchCenter with AI model clients
        emit: Progress callback
        ai_model: Model to use ("gemini", "deepseek", "minimax")
    """
    if not items:
        return items

    # Load prompt
    prompt_path = ROOT_DIR / "prompt" / "news" / "news_summary.md"
    prompt_text = ""
    if prompt_path and prompt_path.exists():
        prompt_text = prompt_path.read_text(encoding="utf-8")

    if not prompt_text:
        # Fallback: categorize by simple keyword matching
        return _simple_categorize(items)

    primary_model = _primary_news_classifier_model(center, ai_model)
    if primary_model != ai_model:
        emit(f"新聞分類分流：一般分類使用 MiniMax M3，高階模型 {ai_model} 僅複核重要新聞")
    result_items = _classify_news_batches(
        items,
        center,
        emit,
        ai_model=primary_model,
        prompt_text=prompt_text,
        label="MiniMax M3 一般新聞分類" if primary_model != ai_model else "AI 分類",
    )
    if primary_model == ai_model:
        return result_items

    high_limit = _news_high_tier_classify_limit()
    selected = _select_high_tier_news_items(result_items, high_limit)
    if not selected:
        return result_items
    emit(f"高階新聞複核開始：{len(selected)} 則，model={ai_model}")
    reviewed = _classify_news_batches(
        selected,
        center,
        emit,
        ai_model=ai_model,
        prompt_text=prompt_text,
        label="高階新聞複核",
    )
    return _merge_reviewed_news_items(result_items, reviewed)


def _primary_news_classifier_model(center: Any, ai_model: str) -> str:
    if ai_model == "minimax":
        return ai_model
    low_model = getattr(center, "low_model_minimax", None)
    is_configured = getattr(low_model, "is_configured", None) if low_model is not None else None
    if callable(is_configured):
        try:
            if is_configured() is True:
                return "minimax_low"
        except Exception:
            return ai_model
    return ai_model


def _merge_reviewed_news_items(base_items: list[NewsItem], reviewed_items: list[NewsItem]) -> list[NewsItem]:
    by_url = {item.url: item for item in reviewed_items if item.url}
    by_id = {item.id: item for item in reviewed_items if item.id}
    merged: list[NewsItem] = []
    for item in base_items:
        reviewed = by_url.get(item.url) or by_id.get(item.id)
        merged.append(reviewed or item)
    return merged


def _classify_news_batches(
    items: list[NewsItem],
    center: Any,
    emit: Callable[[str], None],
    *,
    ai_model: str,
    prompt_text: str,
    label: str,
) -> list[NewsItem]:
    batch_size = _classify_batch_size()
    timeout_seconds = _classify_timeout_seconds()
    text_limit = _classify_text_limit()
    retry_text_limit = _classify_retry_text_limit()
    result_items: list[NewsItem] = []
    total_batches = (len(items) + batch_size - 1) // batch_size
    emit(f"{label}開始：{len(items)} 則，共 {total_batches} 批，每批最多 {batch_size} 則")
    for i in range(0, len(items), batch_size):
        batch = items[i : i + batch_size]
        batch_no = (i // batch_size) + 1
        emit(f"{label} {batch_no}/{total_batches} 開始：{len(batch)} 則")
        batch_json = json.dumps(_classification_payload(batch, text_limit), ensure_ascii=False)
        full_prompt = prompt_text.replace("{news_batch_json}", batch_json)
        emit(f"{label} {batch_no}/{total_batches} prompt={len(full_prompt)} chars est_tokens={max(1, len(full_prompt) // 4)} items={len(batch)}")

        try:
            ai_result = _call_news_classifier(center, ai_model, full_prompt, timeout_seconds)
            raw = getattr(ai_result, "raw", str(ai_result))
            parsed = _parse_ai_json_batch(raw)
            result_items.extend(_apply_classification_meta(batch, parsed))
            emit(f"{label} {batch_no}/{total_batches} 完成")
        except Exception as exc:
            emit(f"{label} {batch_no}/{total_batches} failed: {exc}; retrying with lightweight payload")
            retry_json = json.dumps(_classification_payload(batch, retry_text_limit, lightweight=True), ensure_ascii=False)
            retry_prompt = prompt_text.replace("{news_batch_json}", retry_json)
            emit(f"{label} {batch_no}/{total_batches} retry_prompt={len(retry_prompt)} chars est_tokens={max(1, len(retry_prompt) // 4)} items={len(batch)}")
            try:
                ai_result = _call_news_classifier(center, ai_model, retry_prompt, timeout_seconds)
                raw = getattr(ai_result, "raw", str(ai_result))
                parsed = _parse_ai_json_batch(raw)
                result_items.extend(_apply_classification_meta(batch, parsed))
                emit(f"{label} {batch_no}/{total_batches} lightweight retry completed")
            except Exception as retry_exc:
                emit(f"{label} {batch_no}/{total_batches} fallback to local rules: {retry_exc}")
                result_items.extend(_fallback_classification(batch))
                if _is_timeout_exception(exc) or _is_timeout_exception(retry_exc):
                    remaining = items[i + batch_size :]
                    if remaining:
                        emit(f"{label} timeout; fallback remaining {len(remaining)} items to local rules")
                        result_items.extend(_fallback_classification(remaining))
                    break

    return result_items


def _is_timeout_exception(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return "timeout" in text or "timed out" in text or "read operation timed out" in text


def _truncate_for_ai(text: str, limit: int) -> str:
    value = " ".join(str(text or "").split())
    if limit <= 0 or len(value) <= limit:
        return value
    return value[:limit].rstrip() + "..."


def _classification_payload(items: list[NewsItem], text_limit: int, lightweight: bool = False) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for item in items:
        snippet_limit = min(text_limit, 240) if text_limit else 200
        row = {
            "title": item.title,
            "url": item.url,
            "source": item.source,
            "published_at": item.published_at,
            "snippet": _truncate_for_ai(item.summary, snippet_limit),
        }
        if not lightweight:
            body = item.full_text or item.summary
            row["text"] = _truncate_for_ai(body, text_limit)
        payload.append(row)
    return payload


def _apply_classification_meta(batch: list[NewsItem], parsed: dict[str, Any]) -> list[NewsItem]:
    result_items: list[NewsItem] = []
    for idx, it in enumerate(batch):
        meta = parsed.get(str(idx)) or parsed.get(it.url, {})
        if meta.get("category") == "exclude":
            continue
        if meta:
            it.category = normalize_news_category(meta.get("category", it.category) or _guess_category(it.title))
            it.summary = meta.get("summary", it.summary) or it.summary
            it.related_symbols = meta.get("related_symbols", []) or []
            it.related_topics = meta.get("related_topics", []) or []
            it.importance_score = meta.get("importance_score", 0) or 0
            it.impact_direction = meta.get("impact_direction", "") or ""
            if isinstance(meta.get("tags"), list):
                it.tags = [str(tag) for tag in meta.get("tags") or [] if tag]
            it.news_signal_score = int(meta.get("news_signal_score", it.news_signal_score) or 0)
            it.news_heat_risk_score = int(meta.get("news_heat_risk_score", it.news_heat_risk_score) or 0)
            it.news_signal_reason = str(meta.get("news_signal_reason", it.news_signal_reason) or "")
            it.news_heat_risk_reason = str(meta.get("news_heat_risk_reason", it.news_heat_risk_reason) or "")
        else:
            it.category = _guess_category(it.title)
        it.category = _correct_news_category(it)
        result_items.append(apply_news_signal_tags(it))
    return result_items


def _fallback_classification(batch: list[NewsItem]) -> list[NewsItem]:
    result_items: list[NewsItem] = []
    for it in batch:
        it.category = _guess_category(it.title)
        it.category = _correct_news_category(it)
        result_items.append(apply_news_signal_tags(it))
    return result_items


def _call_news_classifier(center: Any, ai_model: str, prompt: str, timeout_seconds: float) -> Any:
    """Call the selected model with a temporary timeout for news classification."""

    if ai_model == "deepseek":
        client = center.opencode
        return _call_with_temporary_timeout(client, timeout_seconds, lambda: client.generate_report(prompt))
    if ai_model == "minimax":
        client = center.minimax
        if not hasattr(client, "generate_json"):
            raise RuntimeError("MiniMax JSON-only API (generate_json) is not available. Cannot classify news.")
        return _call_with_temporary_timeout(client, timeout_seconds, lambda: client.generate_json(prompt))
    if ai_model == "minimax_low":
        client = getattr(center, "low_model_minimax", None)
        if client is None or not hasattr(client, "generate_json"):
            raise RuntimeError("MiniMax M3 low-model classifier is not available.")
        return _call_with_temporary_timeout(client, timeout_seconds, lambda: client.generate_json(prompt))

    client = center.gemini
    return _call_with_temporary_timeout(
        client,
        timeout_seconds,
        lambda: client.generate_report(prompt, enable_grounding=False),
    )


def _call_with_temporary_timeout(client: Any, timeout_seconds: float, call: Callable[[], Any]) -> Any:
    if not hasattr(client, "timeout_seconds"):
        return call()
    old_timeout = getattr(client, "timeout_seconds")
    try:
        setattr(client, "timeout_seconds", timeout_seconds)
        return call()
    finally:
        setattr(client, "timeout_seconds", old_timeout)


def _parse_ai_json_batch(raw: str | dict | Any) -> dict[str, Any]:
    """Parse AI JSON batch response from any model wrapper.
    
    Handles:
    - Direct dict: {"0": {...}}  (e.g. from generate_json)
    - Direct JSON string: {"0": {...}}
    - OpenAI/DeepSeek: {"choices": [{"message": {"content": "..."}}]}
    - Gemini: {"candidates": [{"content": {"parts": [{"text": "..."}]}}]}
    - MiniMax: <thinking>...</thinking> blocks wrapping JSON
    - Markdown fence: ```json ... ```
    """
    import re

    if isinstance(raw, dict):
        if "choices" not in raw and "candidates" not in raw:
            return raw
        raw = json.dumps(raw, ensure_ascii=False)

    text = raw.strip() if raw else ""
    
    # Try markdown fence strip first
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()
    
    # Gemini format: {"candidates": [{"content": {"parts": [{"text": "..."}]}}]}
    try:
        parsed = json.loads(text)
        if "candidates" in parsed:
            parts = parsed.get("candidates", [{}])[0]
            content = parts.get("content", {}) if isinstance(parts, dict) else {}
            parts_list = content.get("parts", []) if isinstance(content, dict) else []
            if parts_list:
                inner_text = parts_list[0].get("text", "")
                if inner_text:
                    text = inner_text.strip()
    except Exception:
        pass

    # OpenAI/DeepSeek format: {"choices": [{"message": {"content": "..."}}]}
    try:
        parsed = json.loads(text)
        if "choices" in parsed:
            choices = parsed.get("choices", [])
            for choice in choices:
                msg = choice.get("message", {}) if isinstance(choice, dict) else {}
                content = msg.get("content", "")
                if content:
                    text = content.strip()
                    break
    except Exception:
        pass

    # MiniMax may prepend reasoning; remove it and keep the JSON payload.
    text = re.sub(r"<thinking>[\s\S]*?</thinking>", "", text).strip()
    text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE).strip()

    # Now parse the cleaned text as JSON
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to extract top-level JSON object
    try:
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            return json.loads(match.group(0))
    except json.JSONDecodeError:
        pass
    
    return {}


def _simple_categorize(items: list[NewsItem]) -> list[NewsItem]:
    """Simple keyword-based categorization fallback."""
    for it in items:
        it.category = _guess_category(it.title)
        it.category = _correct_news_category(it)
        apply_news_signal_tags(it)
    return items


def _guess_category(title: str) -> str:
    """Guess category from title keywords."""
    t = title.lower()
    if any(k in t for k in ["ai", "gpu", "nvidia", "半導體", "台積電", "晶片", "ic", "伺服器"]):
        return "ai_semiconductor"
    if any(k in t for k in ["pcb", "被動元件", "電子", "面板", "手機", "零組件"]):
        return "supply_chain"
    if any(k in t for k in ["金融", "銀行", "保險", "股息", "殖利率", "高股息"]):
        return "market_focus"
    if any(k in t for k in ["政策", "央行", "升息", "降息", "匯率", "通膨", "gdp"]):
        return "macro_policy"
    if any(k in t for k in ["風險", "利空", "暴跌", "裁員", "虧損", "違約"]):
        return "company_news"
    if any(k in t for k in ["資金", "外資", "投信", "大盤", "指數", "成交量"]):
        return "market_focus"
    if any(k in t for k in ["傳產", "鋼鐵", "水泥", "塑化", "航運", "食品"]):
        return "sector_rotation"
    return "sector_rotation"


_MARKET_FOCUS_TERMS = [
    "台股",
    "加權指數",
    "櫃買",
    "台指期",
    "盤中",
    "盤前",
    "盤後",
    "開盤",
    "收盤",
    "大盤",
    "指數",
    "萬點",
    "創高",
    "三大法人",
    "外資",
    "投信",
    "自營商",
    "買超",
    "賣超",
    "成交量",
    "成交值",
    "資金行情",
    "資金轉向",
    "資金輪動",
    "台股盤前",
    "台股盤中",
    "台股盤後",
    "台股風向球",
    "台股操盤",
    "台股早盤",
    "台股收盤",
    "台股站上",
    "台股重返",
    "台股狂飆",
]

_MACRO_POLICY_STRONG_TERMS = [
    "央行",
    "利率",
    "升息",
    "降息",
    "匯率",
    "新台幣",
    "台幣",
    "cpi",
    "gdp",
    "pmi",
    "通膨",
    "關稅",
    "政策",
    "fed",
    "fomc",
    "美債",
    "殖利率",
    "美元指數",
]


_COMPANY_EVENT_TERMS = [
    "漲停",
    "跌停",
    "亮燈",
    "攻漲停",
    "處置股",
    "注意股",
    "法說",
    "財報",
    "營收",
    "月增",
    "年增",
    "eps",
    "獲利",
    "虧損",
    "接單",
    "出貨",
    "訂單",
    "客戶",
    "目標價",
    "升評",
    "降評",
    "調升",
    "調降",
    "外資看好",
    "limit up",
    "target price",
    "upgrade",
    "downgrade",
    "revenue",
    "earnings",
    "shipment",
    "order",
]

_SECTOR_ROTATION_TERMS = [
    "族群",
    "題材",
    "概念股",
    "供應鏈",
    "多檔",
    "齊漲",
    "齊揚",
    "受惠股",
    "資金轉進",
    "資金輪動",
    "被動元件",
    "pcb",
    "ccl",
    "散熱",
    "重電",
    "機器人",
    "矽光子",
    "cpo",
    "低軌衛星",
    "記憶體",
    "asic",
    "ai伺服器",
    "ai 伺服器",
    "server supply chain",
    "sector rotation",
    "theme stocks",
    "concept stocks",
]

_SECTOR_WITH_COMPANY_TERMS = ["多檔", "族群", "概念股", "齊漲", "齊揚", "受惠股"]


def _news_text(item: NewsItem, *, include_related: bool = False) -> str:
    parts = [item.title or "", item.summary or "", item.full_text or ""]
    if include_related:
        parts.extend(item.related_symbols or [])
        parts.extend(item.related_topics or [])
    return " ".join(parts).lower()


def _looks_like_company_event_news(item: NewsItem) -> bool:
    text = _news_text(item)
    if not any(term.lower() in text for term in _COMPANY_EVENT_TERMS):
        return False
    return not any(term.lower() in text for term in _SECTOR_WITH_COMPANY_TERMS)


def _looks_like_sector_rotation_news(item: NewsItem) -> bool:
    text = _news_text(item, include_related=True)
    return any(term.lower() in text for term in _SECTOR_ROTATION_TERMS)


_HEADLINE_COMPANY_EVENT_TERMS = [
    "漲停",
    "跌停",
    "亮燈",
    "法說",
    "財報",
    "營收",
    "獲利",
    "目標價",
    "升評",
    "降評",
    "調升",
    "調降",
    "接單",
    "出貨",
    "股東會",
    "合約價",
    "再創佳績",
    "創佳績",
    "看好",
    "target price",
    "upgrade",
    "downgrade",
    "revenue",
    "earnings",
    "shipment",
]

_HEADLINE_SECTOR_ROTATION_TERMS = [
    "族群",
    "概念股",
    "受惠股",
    "多檔",
    "齊漲",
    "齊揚",
    "輪動",
    "補漲",
    "集體",
    "爆發",
    "噴發",
    "資金轉進",
    "concept stock",
    "concept stocks",
    "passive components",
    "stocks rally",
    "stock rally",
    "beneficiary stocks",
]

_HEADLINE_SUPPLY_CHAIN_TERMS = [
    "pcb",
    "ccl",
    "載板",
    "散熱",
    "電源",
    "電源管理",
    "mlcc",
    "被動元件",
    "記憶體",
    "dram",
    "供應鏈",
    "產業鏈",
    "光通訊",
    "cpo",
    "伺服器",
    "材料",
    "報價",
    "產能",
]

_HEADLINE_AI_SEMICONDUCTOR_TERMS = [
    "ai",
    "半導體",
    "台積電",
    "聯發科",
    "輝達",
    "gpu",
    "asic",
    "hbm",
    "cowos",
    "先進封裝",
    "晶片",
]

_HEADLINE_MARKET_FOCUS_TERMS = [
    "台股再創高",
    "台股創高",
    "台股站上",
    "台股攻上",
    "加權指數",
    "三大法人",
    "法人買超",
    "法人賣超",
    "盤中速報",
    "盤後分析",
    "盤前要聞",
    "五件財經大事",
]

_HEADLINE_MACRO_POLICY_TERMS = [
    "美股",
    "fed",
    "fomc",
    "中東",
    "匯率",
    "利率",
    "央行",
    "通膨",
    "政策",
    "美元",
    "新台幣",
    "關稅",
]


def _title_has_any(item: NewsItem, terms: list[str]) -> bool:
    title = (item.title or "").lower()
    for term in terms:
        lowered = term.lower()
        if lowered == "ai":
            if re.search(r"(?<![a-z0-9])ai(?![a-z0-9])", title):
                return True
            continue
        if lowered in title:
            return True
    return False


def _has_broad_market_headline(item: NewsItem) -> bool:
    return _title_has_any(item, _HEADLINE_MARKET_FOCUS_TERMS)


def _has_macro_policy_headline(item: NewsItem) -> bool:
    return _title_has_any(item, _HEADLINE_MACRO_POLICY_TERMS)


def _has_company_event_headline(item: NewsItem) -> bool:
    return _title_has_any(item, _HEADLINE_COMPANY_EVENT_TERMS)


def _has_sector_rotation_headline(item: NewsItem) -> bool:
    return _title_has_any(item, _HEADLINE_SECTOR_ROTATION_TERMS)


def _has_supply_chain_headline(item: NewsItem) -> bool:
    return _title_has_any(item, _HEADLINE_SUPPLY_CHAIN_TERMS)


def _has_ai_semiconductor_headline(item: NewsItem) -> bool:
    return _title_has_any(item, _HEADLINE_AI_SEMICONDUCTOR_TERMS)


def _has_explicit_sector_basket_headline(item: NewsItem) -> bool:
    return _title_has_any(
        item,
        [
            "概念股",
            "多檔",
            "齊漲",
            "齊揚",
            "集體",
            "爆發",
            "噴發",
            "concept stock",
            "concept stocks",
            "stocks rally",
            "stock rally",
            "beneficiary stocks",
        ],
    )


_CLEAN_SUPPLY_CHAIN_TERMS = [
    "供應鏈", "產業鏈", "pcb", "ccl", "散熱", "水冷", "電源", "電源管理",
    "伺服器", "組裝", "代工", "封測", "載板", "零組件", "被動元件",
    "mlcc", "電容", "電感", "光通訊", "cpo", "材料", "出貨", "訂單",
    "缺貨", "漲價", "報價", "產能", "供應商",
]

_CLEAN_AI_SEMICONDUCTOR_TERMS = [
    "ai", "半導體", "台積電", "聯發科", "聯電", "ic", "晶片", "gpu",
    "asic", "hbm", "cowos", "先進封裝", "封裝", "nvidia", "輝達",
]

_CLEAN_MACRO_POLICY_TERMS = [
    "央行", "利率", "升息", "降息", "匯率", "新台幣", "台幣",
    "關稅", "政策", "金管會", "財政部", "通膨", "cpi", "gdp", "pmi",
    "fed", "fomc", "出口", "景氣", "貨幣政策", "總經",
]

_CLEAN_MARKET_FOCUS_TERMS = [
    "台股", "大盤", "加權指數", "櫃買", "盤中", "盤後", "三大法人",
    "外資", "投信", "自營商", "買超", "賣超", "成交量", "爆量",
    "指數", "開高", "收漲", "收黑",
]

_CLEAN_STRONG_MARKET_FOCUS_TERMS = [
    "大盤", "加權指數", "櫃買指數", "櫃買", "三大法人", "外資",
    "投信", "自營商", "買超", "賣超", "成交量", "指數創高",
    "台股再創高", "台股創高", "短線過熱", "盤勢", "收盤",
    "盤中", "盤後",
]

_CLEAN_COMPANY_EVENT_TERMS = [
    "營收", "獲利", "eps", "法說", "訂單", "目標價", "升評", "降評",
    "漲停", "跌停", "利多", "利空", "財報", "配息", "除息", "公告",
    "併購", "增資", "減資", "出貨",
]

_CLEAN_SECTOR_ROTATION_TERMS = [
    "族群", "題材", "概念股", "輪動", "補漲", "強勢股", "資金流向",
    "熱門", "受惠股", "類股", "點名", "集體", "噴出", "爆發",
    "sector rotation", "concept stock", "concept stocks", "theme stock",
    "theme stocks", "stock rally", "stocks rally", "beneficiary stocks",
]

_CLEAN_STRONG_SECTOR_ROTATION_TERMS = [
    "題材", "概念股", "輪動", "補漲", "強勢股", "資金流向", "類股輪動",
    "sector rotation", "concept stock", "concept stocks", "theme stock",
    "theme stocks", "stock rally", "stocks rally", "beneficiary stocks",
]


def _contains_any_clean_term(item: NewsItem, terms: list[str]) -> bool:
    text = _news_text(item, include_related=True)
    return any(term.lower() in text for term in terms)


def _correct_news_category(item: NewsItem) -> str:
    """Apply category corrections shared by AI classification, fallback, and display."""
    category = normalize_news_category(item.category)
    text = _news_text(item, include_related=True)
    title_text = (item.title or "").lower()
    if category == "holdings":
        return category

    if _has_broad_market_headline(item) and not (
        _has_company_event_headline(item) or _has_sector_rotation_headline(item) or _has_supply_chain_headline(item)
    ):
        return "market_focus"
    if _has_macro_policy_headline(item) and not _has_broad_market_headline(item):
        return "macro_policy"
    if _has_supply_chain_headline(item) and not _has_explicit_sector_basket_headline(item):
        return "supply_chain"
    if _has_company_event_headline(item) and not _has_sector_rotation_headline(item):
        return "company_news"
    if category == "ai_semiconductor" and _has_ai_semiconductor_headline(item):
        return "ai_semiconductor"
    if _has_sector_rotation_headline(item):
        return "sector_rotation"
    if _has_supply_chain_headline(item):
        if _has_sector_rotation_headline(item):
            return "sector_rotation"
        return "supply_chain"
    if _has_ai_semiconductor_headline(item) and not _has_broad_market_headline(item):
        return "ai_semiconductor"

    if category == "ai_semiconductor":
        return category
    if any(term in text for term in [
        "題材", "概念股", "受惠股", "多檔", "輪動", "漲價題材",
        "sector rotation", "concept stock", "concept stocks", "stocks rally",
        "stock rally", "beneficiary stocks",
    ]):
        return "sector_rotation"
    if any(term in text for term in [
        "pcb", "ccl", "載板", "mlcc", "被動元件", "記憶體", "dram", "封裝",
        "供應鏈", "材料", "伺服器", "光通訊",
    ]):
        return "supply_chain"
    if _contains_any_clean_term(item, _CLEAN_SUPPLY_CHAIN_TERMS):
        return "supply_chain"
    if re.search(r"(?<![a-z0-9])ai(?![a-z0-9])", text) or any(term in text for term in [
        "gpu", "asic", "hbm", "cowos", "nvidia",
    ]):
        return "ai_semiconductor"
    if any(term in text for term in [
        "財經大事", "五件財經大事", "台股再創高", "台股站上", "台股攻", "大盤", "加權指數",
        "三大法人", "法人買超", "法人賣超", "盤中速報", "盤後分析", "市場焦點",
    ]):
        return "market_focus"
    if any(term in title_text for term in [
        "美股", "fed", "fomc", "中東", "匯率", "利率", "央行", "通膨", "政策",
        "美元", "新台幣", "關稅",
    ]) and not any(term in title_text for term in ["台股", "大盤", "加權指數", "三大法人"]):
        return "macro_policy"
    if any(term in text for term in [
        "美股", "fed", "fomc", "中東", "匯率", "利率", "央行", "通膨", "政策",
        "美元", "新台幣", "關稅",
    ]) and not any(term in text for term in ["台股", "大盤", "加權指數", "三大法人"]):
        return "macro_policy"
    if any(term in text for term in [
        "pcb", "ccl", "載板", "散熱", "電源", "mlcc", "被動元件", "記憶體",
        "dram", "封裝", "供應鏈", "材料", "伺服器", "光通訊",
    ]):
        return "supply_chain"
    if any(term in text for term in [
        "營收", "漲停", "亮燈", "法說", "目標價", "財報", "接單", "訂單",
        "獲利", "eps", "股東會", "除息", "合約價",
    ]):
        return "company_news"
    if category == "ai_semiconductor" and _contains_any_clean_term(item, _CLEAN_AI_SEMICONDUCTOR_TERMS):
        return "ai_semiconductor"
    if _contains_any_clean_term(item, _CLEAN_STRONG_MARKET_FOCUS_TERMS):
        return "market_focus"
    if _contains_any_clean_term(item, _CLEAN_MACRO_POLICY_TERMS):
        if not _contains_any_clean_term(item, _CLEAN_MARKET_FOCUS_TERMS):
            return "macro_policy"
    if _contains_any_clean_term(item, _CLEAN_STRONG_SECTOR_ROTATION_TERMS):
        return "sector_rotation"
    if _contains_any_clean_term(item, _CLEAN_SUPPLY_CHAIN_TERMS):
        return "supply_chain"
    if _contains_any_clean_term(item, _CLEAN_COMPANY_EVENT_TERMS):
        return "company_news"
    if _contains_any_clean_term(item, _CLEAN_SECTOR_ROTATION_TERMS):
        return "sector_rotation"
    if _looks_like_company_event_news(item):
        return "company_news"
    if category in {"market_focus", "macro_policy", "other", "sector_rotation"} and _looks_like_sector_rotation_news(item):
        return "sector_rotation"
    return _correct_market_news_category(item)


def _correct_market_news_category(item: NewsItem) -> str:
    """Prefer market_focus for Taiwan market tape/fund-flow news."""
    category = normalize_news_category(item.category)
    if category not in {"macro_policy", "other", "sector_rotation"}:
        return category

    text = f"{item.title} {item.summary} {item.full_text or ''}".lower()
    has_market_focus = any(term.lower() in text for term in _MARKET_FOCUS_TERMS)
    if not has_market_focus:
        return category

    strong_macro_hits = sum(1 for term in _MACRO_POLICY_STRONG_TERMS if term.lower() in text)
    if strong_macro_hits >= 2 and not any(term in text for term in ["台股", "台指期", "三大法人", "外資", "投信", "大盤"]):
        return "macro_policy"
    return "market_focus"


def build_news_digests(items: list[NewsItem], *, include_empty_categories: bool = False) -> list[NewsDigest]:
    """Group news items into category digests."""
    by_category: dict[str, list[NewsItem]] = {}
    for it in items:
        cat = normalize_news_category(it.category)
        it.category = cat
        by_category.setdefault(cat, []).append(it)

    digests: list[NewsDigest] = []
    for cat in ordered_news_category_keys():
        if cat in by_category or include_empty_categories:
            # Sort by importance score descending
            sorted_items = sorted(by_category.get(cat, []), key=_news_display_sort_key, reverse=True)
            digests.append(NewsDigest(category=cat, items=sorted_items))
    # Append any uncategorized
    for cat, cat_items in by_category.items():
        if cat not in NEWS_CATEGORIES:
            sorted_items = sorted(cat_items, key=_news_display_sort_key, reverse=True)
            digests.append(NewsDigest(category=cat, items=sorted_items))
    return digests


def _news_display_sort_key(item: NewsItem) -> tuple[int, int, str]:
    published = _effective_news_datetime(item)
    published_ts = int(published.timestamp()) if published else 0
    return (int(item.importance_score or 0), published_ts, item.title or "")


def _matches_portfolio_in_title(item: NewsItem, code_s: str, name_s: str) -> bool:
    title = (item.title or "").lower()
    if code_s and re.search(rf"(?<!\d){re.escape(code_s)}(?!\d)", title):
        return True
    return bool(name_s and name_s in title)


def _matches_portfolio(item: NewsItem, portfolio: dict[str, str]) -> bool:
    if not portfolio:
        return False
    category = normalize_news_category(_correct_news_category(item))
    symbols = {str(value).strip().lower() for value in (item.related_symbols or []) if str(value).strip()}
    topics = {str(value).strip().lower() for value in (item.related_topics or []) if str(value).strip()}
    use_symbol_metadata = 0 < len(symbols) <= 5
    use_topic_metadata = 0 < len(topics) <= 8
    lead_text = f"{item.summary} {item.full_text or ''}"[:500].lower()
    for code, name in portfolio.items():
        code_s = str(code).strip().lower()
        name_s = str(name).strip().lower()
        if _matches_portfolio_in_title(item, code_s, name_s):
            return True
        if category == "company_news":
            if use_symbol_metadata and code_s and code_s in symbols:
                return True
            if use_topic_metadata and name_s and name_s in topics:
                return True
            if (code_s and re.search(rf"(?<!\d){re.escape(code_s)}(?!\d)", lead_text)) or (name_s and name_s in lead_text):
                return True
    return False


def _split_holding_digest(items: list[NewsItem], portfolio: dict[str, str]) -> tuple[list[NewsItem], NewsDigest]:
    market_items: list[NewsItem] = []
    holding_items: list[NewsItem] = []
    seen_holding_urls: set[str] = set()
    for item in items:
        if _matches_portfolio(item, portfolio):
            if item.url not in seen_holding_urls:
                holding_items.append(item)
                seen_holding_urls.add(item.url)
        else:
            market_items.append(item)
    holding_items = sorted(holding_items, key=lambda x: x.importance_score, reverse=True)[:8]
    return market_items, NewsDigest(category="holdings", items=holding_items)


def _preference_stats_from_repository(repository: NewsRepository) -> dict[str, dict[str, int]]:
    if hasattr(repository, "get_preference_stats"):
        try:
            return repository.get_preference_stats()
        except Exception:
            return {}
    if not hasattr(repository, "list_preferences"):
        return {}
    try:
        prefs = repository.list_preferences(limit=300)
    except Exception:
        return {}
    stats: dict[str, dict[str, int]] = {"news_type": {}, "category": {}, "source": {}}
    for pref in prefs:
        weight = int(getattr(pref, "weight", 1) or 1)
        news_type = str(getattr(pref, "news_type", "") or "")
        category = str(getattr(pref, "normalized_category", "") or "")
        source = str(getattr(pref, "source", "") or "").strip().lower()
        if news_type:
            stats["news_type"][news_type] = stats["news_type"].get(news_type, 0) + weight
        if category:
            stats["category"][category] = stats["category"].get(category, 0) + weight
        if source:
            stats["source"][source] = stats["source"].get(source, 0) + weight
    return stats


def _preference_boost(item: NewsItem, stats: dict[str, dict[str, int]]) -> int:
    if not stats:
        return 0
    news_type = _guess_news_type(item)
    category = normalize_news_category(item.category)
    source = (item.source or "").strip().lower()
    type_hits = stats.get("news_type", {}).get(news_type, 0)
    category_hits = stats.get("category", {}).get(category, 0)
    source_hits = stats.get("source", {}).get(source, 0)
    return min(type_hits * 6, 30) + min(category_hits, 5) + min(source_hits, 3)


def _apply_user_news_preferences(items: list[NewsItem], repository: NewsRepository) -> list[NewsItem]:
    """Apply lightweight preference boosts after display filtering."""
    stats = _preference_stats_from_repository(repository)
    if not stats:
        return items
    boosted: list[NewsItem] = []
    for item in items:
        boost = _preference_boost(item, stats)
        if boost:
            item.importance_score = int(item.importance_score or 0) + boost
        boosted.append(item)
    return boosted


_LATEST_PRIMARY_MIN_ITEMS = 20

_LOW_PRIORITY_DISPLAY_SOURCE_MARKERS = (
    "cmoney",
    "cmoney.tw/notes",
    "cmnews.com.tw",
    "readmo.cmoney.tw",
)
_VERY_LOW_PRIORITY_DISPLAY_TEXT_MARKERS = (
    "readmo.ai",
    "投資網誌",
    "延伸閱讀",
    "cmoney研究員",
)


def _is_low_priority_display_source(item: NewsItem) -> bool:
    combined = f"{item.url or ''} {item.source or ''}".lower()
    return any(marker in combined for marker in _LOW_PRIORITY_DISPLAY_SOURCE_MARKERS)


def _apply_display_source_penalties(items: list[NewsItem]) -> list[NewsItem]:
    """Demote lightweight investment-blog sources without removing them."""
    for item in items:
        if _is_low_priority_display_source(item):
            item.importance_score = int(item.importance_score or 0) - 260
        combined = f"{item.title or ''} {item.summary or ''} {item.source or ''} {item.url or ''}".lower()
        if any(marker.lower() in combined for marker in _VERY_LOW_PRIORITY_DISPLAY_TEXT_MARKERS):
            item.importance_score = int(item.importance_score or 0) - 180
    return items


def _merge_unique_news_items(primary: list[NewsItem], fallback: list[NewsItem]) -> list[NewsItem]:
    seen: set[str] = set()
    merged: list[NewsItem] = []
    for item in [*primary, *fallback]:
        key = item.url or item.id or item.title
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def run_news_latest(repository: NewsRepository, portfolio: dict[str, str] | None = None) -> list[NewsDigest]:
    """Return latest news, preferring 24h items and falling back to 48h."""
    items = _query_refresh_news(repository, hours=48)
    items = _filter_and_prune_news_items(items, repository)
    primary_items = _filter_by_published_window(items, hours=24, keep_unknown_date=False)
    primary_explicit = [item for item in primary_items if _has_explicit_news_datetime(item)]
    if len(primary_explicit) >= _LATEST_PRIMARY_MIN_ITEMS:
        items = primary_explicit
    else:
        fallback_items = _filter_by_published_window(items, hours=48, keep_unknown_date=False)
        items = _merge_unique_news_items(primary_explicit, fallback_items)
    items = _penalize_implicit_news_dates(items)
    items = _boost_latest_recency(items, primary_hours=24)
    items = _apply_user_news_preferences(items, repository)
    items = _apply_display_source_penalties(items)
    market_items, holding_digest = _split_holding_digest(items, portfolio or {})
    return build_news_digests(market_items, include_empty_categories=True) + [holding_digest]


def run_news_scheduled_latest(repository: NewsRepository, portfolio: dict[str, str] | None = None) -> list[NewsDigest]:
    """Return news for scheduled Telegram pushes with stricter freshness rules.

    Scheduled pushes should not treat a newly saved row as fresh unless the
    article itself has an explicit publish timestamp or an embedded article
    date. This prevents old undated articles from reappearing in daily pushes
    just because /news refresh saved them recently.
    """
    items = _query_refresh_news(repository, hours=48)
    items = _filter_and_prune_news_items(items, repository)
    primary_items = _filter_by_published_window(items, hours=24, keep_unknown_date=False)
    if len(primary_items) >= _LATEST_PRIMARY_MIN_ITEMS:
        items = primary_items
    else:
        fallback_items = _filter_by_published_window(items, hours=48, keep_unknown_date=False)
        items = _merge_unique_news_items(primary_items, fallback_items)
    items = _penalize_implicit_news_dates(items)
    items = _boost_latest_recency(items, primary_hours=24)
    items = _apply_user_news_preferences(items, repository)
    items = _apply_display_source_penalties(items)
    market_items, holding_digest = _split_holding_digest(items, portfolio or {})
    return build_news_digests(market_items, include_empty_categories=True) + [holding_digest]


def build_scheduled_news_diagnostics(
    repository: NewsRepository,
    digests: list[NewsDigest] | None = None,
) -> dict[str, Any]:
    raw = _query_refresh_news(repository, hours=48)
    pruned = _filter_and_prune_news_items(raw, repository)
    explicit = [item for item in pruned if _has_explicit_news_datetime(item)]
    primary = _filter_by_published_window(pruned, hours=24, keep_unknown_date=False)
    fallback = _filter_by_published_window(pruned, hours=48, keep_unknown_date=False)
    display_items = [item for digest in (digests or run_news_scheduled_latest(repository)) for item in digest.items]
    missing_dates = [item for item in pruned if not _has_explicit_news_datetime(item)]
    return {
        "raw48": len(raw),
        "pruned48": len(pruned),
        "explicit_dates": len(explicit),
        "missing_dates": len(missing_dates),
        "primary24": len(primary),
        "fallback48": len(fallback),
        "display_total": len(display_items),
        "display_categories": dict(Counter(item.category for item in display_items)),
    }


def scheduled_news_lightweight_refresh_categories(diagnostics: dict[str, Any]) -> list[str]:
    """Return category keys that should receive one lightweight scheduled-news refill."""
    display_categories = diagnostics.get("display_categories") or {}
    missing = [
        category
        for category in _SCHEDULED_LIGHTWEIGHT_CORE_CATEGORIES
        if int(display_categories.get(category, 0) or 0) <= 0
    ]
    primary24 = int(diagnostics.get("primary24", 0) or 0)
    if primary24 < _SCHEDULED_LIGHTWEIGHT_PRIMARY_MIN:
        low_count = [
            category
            for category in _SCHEDULED_LIGHTWEIGHT_CORE_CATEGORIES
            if int(display_categories.get(category, 0) or 0) < 2
        ]
        categories = missing or low_count
    else:
        categories = missing
    return categories[:4]


@contextmanager
def _temporary_env(updates: dict[str, str]):
    old_values = {key: os.environ.get(key) for key in updates}
    try:
        for key, value in updates.items():
            os.environ[key] = value
        yield
    finally:
        for key, old_value in old_values.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


def run_scheduled_news_lightweight_refresh(
    center: Any,
    repository: NewsRepository,
    progress: ProgressCallback | None = None,
    ai_model: str = "minimax",
    diagnostics: dict[str, Any] | None = None,
) -> tuple[list[NewsItem], dict[str, Any]]:
    """Run at most one lightweight refill for scheduled pushes when 24h news is thin."""
    diagnostics = diagnostics or build_scheduled_news_diagnostics(repository)
    categories = scheduled_news_lightweight_refresh_categories(diagnostics)
    if not categories:
        return [], {
            "ran": False,
            "reason": "scheduled_news_sufficient",
            "primary24": int(diagnostics.get("primary24", 0) or 0),
            "categories": [],
        }

    def emit(message: str) -> None:
        if progress:
            progress(message)

    emit(
        "定時新聞輕量補強："
        f"primary24={int(diagnostics.get('primary24', 0) or 0)} "
        f"categories={','.join(categories)}"
    )
    env_updates = {
        "NEWS_REFRESH_TASK_CATEGORIES": ",".join(categories),
        "NEWS_REFRESH_MAX_SOURCES": "24",
        "NEWS_REFRESH_WEBFETCH_MAX_URLS": "6",
        "NEWS_REFRESH_CLASSIFY_LIMIT": "8",
        "NEWS_AI_CLASSIFY_BATCH_SIZE": "2",
        "NEWS_AI_CLASSIFY_TEXT_LIMIT": "350",
    }
    with _temporary_env(env_updates):
        items, meta = run_news_refresh(center, repository, progress=progress, ai_model=ai_model)
    meta = dict(meta)
    meta.update({
        "ran": True,
        "reason": "scheduled_news_lightweight_refill",
        "categories": categories,
        "primary24_before": int(diagnostics.get("primary24", 0) or 0),
    })
    return items, meta


def run_news_7d(repository: NewsRepository, portfolio: dict[str, str] | None = None) -> list[NewsDigest]:
    """Return news from last 7 days."""
    items = _query_refresh_news(repository, hours=168)
    items = _filter_and_prune_news_items(items, repository)
    items = _filter_by_published_window(items, hours=168, keep_unknown_date=False)
    items = _penalize_implicit_news_dates(items, penalty=100)
    items = _apply_user_news_preferences(items, repository)
    market_items, holding_digest = _split_holding_digest(items, portfolio or {})
    return build_news_digests(market_items, include_empty_categories=True) + [holding_digest]


def _filter_and_prune_news_items(items: list[NewsItem], repository: NewsRepository | None = None) -> list[NewsItem]:
    """Filter displayed news and normalize categories without deleting stored rows."""
    items = _apply_news_title_cleanup(items)
    filtered = _filter_taiwan_finance_news(items)
    filtered = [item for item in filtered if _is_display_language_allowed(item)]
    for item in filtered:
        item.category = normalize_news_category(item.category)
        item.category = _correct_news_category(item)
    return _dedupe_display_news_items(filtered)


_DISPLAY_EXCLUDED_ENGLISH_DOMAINS = {
    "digitimes.com",
    "www.digitimes.com",
    "trendforce.com",
    "www.trendforce.com",
}


def _has_cjk_text(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in str(text or ""))


def _is_display_language_allowed(item: NewsItem) -> bool:
    """Hide English-only sources from Telegram display while keeping DB rows."""
    if _has_cjk_text(item.title or ""):
        return True
    parsed = urlparse(item.url or "")
    netloc = parsed.netloc.lower()
    path = parsed.path.lower()
    if netloc in _DISPLAY_EXCLUDED_ENGLISH_DOMAINS:
        return False
    if netloc.endswith(".digitimes.com"):
        return False
    if netloc.endswith("twse.com.tw") and ("/en/" in path or path.startswith("/en")):
        return False
    if netloc.endswith("tpex.org.tw") and ("/en/" in path or path.startswith("/en")):
        return False
    return True


def _display_dedupe_key(item: NewsItem) -> str:
    """Return a stable display-only key for near-duplicate news."""
    title = (item.title or "").strip().lower()
    title = title.split(" | ")[0].split("｜")[0].split("|")[0]
    title = re.sub(r"\s+", "", title)
    title = re.sub(r"[^\w\u4e00-\u9fff]+", "", title)
    if len(title) >= 8:
        return f"title:{title}"
    parsed = urlparse(item.url or "")
    return f"url:{parsed.netloc.lower()}{parsed.path.rstrip('/')}"


def _dedupe_display_news_items(items: list[NewsItem]) -> list[NewsItem]:
    """Hide near-duplicates in Telegram output without deleting stored rows."""
    best_by_key: dict[str, NewsItem] = {}
    order: list[str] = []
    for item in items:
        key = _display_dedupe_key(item)
        current = best_by_key.get(key)
        if current is None:
            best_by_key[key] = item
            order.append(key)
            continue
        if int(item.importance_score or 0) > int(current.importance_score or 0):
            best_by_key[key] = item
    return [best_by_key[key] for key in order if key in best_by_key]


def _has_taiwan_market_link(text: str, url: str = "") -> bool:
    combined = f"{text} {url}".lower()
    return any(term.lower() in combined for term in _TAIWAN_MARKET_LINK_TERMS)


def _is_global_market_only_news(item: NewsItem) -> bool:
    combined = f"{item.title} {item.summary} {item.full_text or ''}".lower()
    if not any(term.lower() in combined for term in _GLOBAL_MARKET_ONLY_TERMS):
        return False
    return not _has_taiwan_market_link(combined, item.url or "")


# Patterns that indicate non-Taiwan or generic news to exclude
_TAIWAN_EXCLUDE_PATTERNS = [
    "wikipedia.org",
    "dictionary",
    "general news",
    "world news",
    "breaking news",
    "latest news",
    "news update",
    "bbc.com",
    "cnn.com",
    "cbsnews.com",
    "apnews.com",
    "news.google.com",
    "youtube.com",
    "washingtonpost.com",
    "instagram.com",
    "bebee.com",
    "agilebrandguide.com",
    "blog.stephenturner.us",
    "associated press",
    "reuters.com/world",
    "bbc.co.uk",
    "theguardian",
    "nytimes.com",
    " washington post",
    "bloomberg.com/global",
    "financial times",
    "the economist",
    "forbes.com/global",
    "businesstoday.com.tw" "/international",
    "techcrunch.com",
    "theverge.com",
    "wired.com",
    "arstechnica.com",
]

_GENERIC_NON_TAIWAN_DOMAINS = [
    "choosechicago.com",
    "downtownwestchester.com",
    "aimsfx.com",
    "cnn.com",
    "cbsnews.com",
    "apnews.com",
    "bbc.com",
    "news.google.com",
    "youtube.com",
    "washingtonpost.com",
]

_NON_FINANCE_EVENT_PATTERNS = [
    "open-air market",
    "street market",
    "farmers market",
    "flea market",
    "event/",
    "/events/",
    "choose chicago",
    "downtown west chester",
]

_PURE_CRYPTO_PATTERNS = [
    "crypto market",
    "tokens rising",
    "top 10 gainers",
    "usdt",
    "bitcoin",
    "ethereum",
]

_TAIWAN_FINANCE_ASCII_HINTS = [
    "taiwan",
    "taipei",
    "twse",
    "tpex",
    "mops",
    "semiconductor",
    "tsmc",
    "mediatek",
    "foxconn",
    "hon hai",
    "quanta",
    "wistron",
    "compal",
    "inventec",
    "novatek",
    "realtek",
    "umc",
    "ase",
    "cathay financial",
    "fubon",
    "taiwan stock",
]

_TAIWAN_STRICT_ASCII_HINTS = [
    "taiwan",
    "taipei",
    "twse",
    "tpex",
    "mops",
    "taiwan stock",
    "tsmc",
    "mediatek",
    "foxconn",
    "hon hai",
    "quanta",
    "wistron",
    "compal",
    "inventec",
    "novatek",
    "realtek",
    "umc",
    "ase technology",
    "cathay financial",
    "fubon",
]

_TRUSTED_TAIWAN_FINANCE_DOMAINS = [
    "cna.com.tw",
    "ctee.com.tw",
    "money.udn.com",
    "cnyes.com",
    "moneydj.com",
    "tw.stock.yahoo.com",
    "uanalyze.com.tw",
    "moneyweekly.com.tw",
    "readmo.cmoney.tw",
    "cmoney.tw",
    "businesstoday.com.tw",
    "technews.tw",
    "digitimes.com",
]

_REDIRECT_ONLY_DOMAINS = {
    "share.google",
}

_MONEYDJ_INTERNAL_PAGE_MARKERS = [
    "djhtm",
    "type=list",
    "svc=nv",
    "nvkmdj",
    "新聞內文-{",
]

_GLOBAL_MARKET_ONLY_TERMS = [
    "美股",
    "歐股",
    "日股",
    "韓股",
    "陸股",
    "港股",
    "a股",
    "s&p",
    "sp500",
    "nasdaq",
    "dow jones",
    "russell",
    "euro stoxx",
    "nikkei",
    "kospi",
    "hang seng",
    "crypto market",
    "bitcoin",
    "ethereum",
]

_TAIWAN_MARKET_LINK_TERMS = [
    "台股",
    "台灣",
    "臺灣",
    "台積電",
    "聯發科",
    "鴻海",
    "廣達",
    "緯創",
    "仁寶",
    "英業達",
    "台達電",
    "日月光",
    "聯電",
    "台指期",
    "外資買超",
    "外資賣超",
    "投信買超",
    "三大法人",
    "twse",
    "tpex",
    "taiwan stock",
    "taiwan semiconductor",
    "tsmc",
    "mediatek",
    "foxconn",
    "hon hai",
    "quanta",
    "wistron",
    "compal",
    "inventec",
    "delta electronics",
    "ase technology",
]

# Keywords that indicate Taiwan finance relevance
_TAIWAN_RELEVANT_KEYWORDS = [
    "台股", "台灣", "加權", "大盤", "上市", "上櫃",
    "半導體", "台積電", "聯發科", "聯電", "鴻海", "廣達",
    "緯創", "仁寶", "英業達", "友達", "群創", "京元電子",
    "聯詠", "瑞昱", "日月光", "矽力", "信驊", "奇鋐",
    "三大法人", "外資", "投信", "大盤", "指數", "成交量",
    "股息", "殖利率", "除權", "除息", "填息",
    "AI", "伺服器", "GPU", "HBM", "CoWoS",
    "HPC", "Edge AI", "NPU",
    "PCB", "CCL", "被動元件", "鋁箔", "電容", "電感",
    "面板", "中小尺寸", "車用", "工控",
    "記憶體", "DRAM", "NAND", "NOR",
    "成熟製程", "先進製程", "7奈米", "5奈米", "3奈米",
    "nvidia", "amd", "intel", "高通", "聯發科",
    "黃金", "石油", "原物料", "鋼鐵", "塑化", "航運",
    "金融", "銀行", "壽險", "證券", "投顧",
    "央行", "升息", "降息", "匯率", "台幣", "新台幣",
    "通膨", "CPI", "GDP", "景氣", "PMI",
    "政策", "金管會", "證交所", "櫃買中心",
    "陸股", "A股", "港股", "美股", "日股", "韓股",
    "中美貿易", "關稅", "制裁", "華為",
]

# Chinese dictionary / encyclopedia patterns
_DICT_EXCLUDE_PATTERNS = [
    "wiki", "dictionary", "辭典", "字典", "百科",
    "教育部", "教育部字典", "教育部國語",
]


def _is_taiwan_finance_news(item: NewsItem) -> bool:
    """Check if a news item is relevant to Taiwan finance/stock market.

    Returns True if the item is Taiwan-relevant and not a dictionary/generic page,
    not a non-article page (homepage, search, list, opendata, etc.).
    """
    # Reject non-article pages first
    if _is_non_article_page(item):
        return False

    title_lower = item.title.lower()
    url_lower = item.url.lower()
    snippet_lower = item.summary.lower()
    full_text_lower = (item.full_text or "").lower()
    combined = f"{title_lower} {snippet_lower} {full_text_lower}"
    host = urlparse(item.url or "").netloc.lower()
    trusted_taiwan_domain = any(host == d or host.endswith("." + d) for d in _TRUSTED_TAIWAN_FINANCE_DOMAINS)

    if host in _REDIRECT_ONLY_DOMAINS:
        return False

    if any(marker in combined or marker in url_lower for marker in _MONEYDJ_INTERNAL_PAGE_MARKERS):
        return False

    if _is_global_market_only_news(item):
        return False

    if any(domain in url_lower for domain in _GENERIC_NON_TAIWAN_DOMAINS):
        return False

    if any(pattern in combined or pattern in url_lower for pattern in _NON_FINANCE_EVENT_PATTERNS):
        return False

    has_taiwan_ascii_hint = any(hint in combined or hint in url_lower for hint in _TAIWAN_FINANCE_ASCII_HINTS)
    has_strict_taiwan_ascii_hint = any(hint in combined or hint in url_lower for hint in _TAIWAN_STRICT_ASCII_HINTS)
    if any(pattern in combined for pattern in _PURE_CRYPTO_PATTERNS) and not has_strict_taiwan_ascii_hint:
        return False

    # Exclude dictionary / encyclopedia pages
    for pat in _DICT_EXCLUDE_PATTERNS:
        if pat in url_lower:
            return False

    # Exclude patterns in title
    for pat in _TAIWAN_EXCLUDE_PATTERNS:
        if pat in title_lower or pat in url_lower:
            return False

    # Must have at least one Taiwan-relevant keyword
    has_keyword = any(str(kw).lower() in combined for kw in _TAIWAN_RELEVANT_KEYWORDS)

    # Additionally require: no English generic news + has Taiwan indicator
    english_generic = any(
        phrase in combined for phrase in [
            "breaking news", "latest news", "world news",
            "international news", "global news",
        ]
    )
    has_taiwan_indicator = trusted_taiwan_domain or has_taiwan_ascii_hint or any(
        str(kw).lower() in combined for kw in [
            "台", "Taiwan", "台灣", "台股", "Taipei",
            "加權指數", "櫃買", "上市櫃", "IPO", "增資", "減資",
            "股息", "殖利率", "除權", "除息", "填息",
            "AI", "伺服器", "GPU", "HBM", "CoWoS",
            "HPC", "Edge AI", "NPU",
            "PCB", "CCL", "被動元件", "鋁箔", "電容", "電感",
            "面板", "中小尺寸", "車用", "工控",
            "記憶體", "DRAM", "NAND", "NOR",
            "成熟製程", "先進製程", "7奈米", "5奈米", "3奈米",
            "nvidia", "amd", "intel", "高通", "聯發科",
            "黃金", "石油", "原物料", "鋼鐵", "塑化", "航運",
            "金融", "銀行", "壽險", "證券", "投顧",
            "央行", "升息", "降息", "匯率", "台幣", "新台幣",
            "通膨", "CPI", "GDP", "景氣", "PMI",
            "政策", "金管會", "證交所", "櫃買中心",
            "陸股", "A股", "港股", "美股", "日股", "韓股",
            "中美貿易", "關稅", "制裁", "華為",
        ]
    )

    ascii_letters = sum(1 for ch in combined if ("a" <= ch <= "z"))
    cjk_chars = sum(1 for ch in combined if "\u4e00" <= ch <= "\u9fff")
    mostly_english = ascii_letters > max(80, cjk_chars * 3)

    if english_generic and not has_taiwan_indicator:
        return False

    if mostly_english and not (trusted_taiwan_domain or has_strict_taiwan_ascii_hint):
        return False

    return has_keyword or trusted_taiwan_domain


def _filter_taiwan_finance_news(items: list[NewsItem]) -> list[NewsItem]:
    """Filter list to keep only Taiwan finance relevant news items."""
    return [it for it in items if _is_taiwan_finance_news(it)]


def _guess_news_type(item: NewsItem) -> str:
    """Infer a lightweight user preference type from article content."""
    text = f"{item.title} {item.summary} {item.full_text}".lower()
    if any(k in text for k in ["漲價", "報價", "價格", "缺貨", "供不應求", "產能", "庫存"]):
        return "price_or_quote"
    if any(k in text for k in ["供應鏈", "受惠", "零組件", "封裝", "pcb", "ccl", "電源", "功率", "被動元件", "mlcc"]):
        return "supply_chain_benefit"
    if any(k in text for k in ["集體爆發", "族群", "概念股", "熱門", "題材", "飆", "強攻", "漲停"]):
        return "market_hype"
    if any(k in text for k in ["法說", "營收", "訂單", "併購", "公告", "財報", "目標價", "升評", "降評", "利多", "利空"]):
        return "company_catalyst"
    if any(k in text for k in ["法人", "專家", "投信", "外資", "券商", "分析師", "看好"]):
        return "institution_view"
    if any(k in text for k in ["台股", "大盤", "匯率", "新台幣", "政策", "央行", "利率", "資金", "買超", "賣超"]):
        return "macro_market"
    return "other"


def _build_news_preference(item: NewsItem) -> NewsPreference:
    normalized = normalize_news_category(item.category)
    return NewsPreference(
        url=item.url,
        title=item.title,
        category=item.category,
        normalized_category=normalized,
        source=item.source,
        news_type=_guess_news_type(item),
        weight=1,
        created_at=datetime.now().isoformat(timespec="seconds"),
    )


def _record_news_preference(repository: NewsRepository, item: NewsItem | None) -> None:
    if item is None or not hasattr(repository, "save_preference"):
        return
    try:
        repository.save_preference(_build_news_preference(item))
    except Exception:
        pass


def save_user_submitted_news_url(
    url: str,
    center: Any,
    repository: NewsRepository,
    progress: ProgressCallback | None = None,
    ai_model: str = "gemini",
) -> tuple[NewsItem | None, str]:
    """Fetch, classify, and save a user-pasted news URL.

    Returns (item, status), where status is one of:
    saved, duplicate, invalid_url, non_article_page, fetch_failed,
    not_taiwan_finance_news.
    """
    clean_url = str(url or "").strip()
    if not clean_url.startswith(("http://", "https://")):
        return None, "invalid_url"

    shell_item = NewsItem(title="", url=clean_url)
    if _is_non_article_page(shell_item):
        return None, "non_article_page"

    existing = repository.get_by_url(clean_url) if hasattr(repository, "get_by_url") else None
    if existing is not None:
        _record_news_preference(repository, existing)
        return existing, "duplicate"

    def emit(message: str) -> None:
        if progress:
            progress(message)

    fetched = WebFetchService(timeout=20.0).fetch(clean_url, progress=progress)
    if fetched.content_status == "failed" and not fetched.content:
        return None, "fetch_failed"

    matched = match_preferred_source(clean_url)
    source_name = matched.get("name") if matched else (fetched.fetch_provider or "user_submitted")
    full_text = (fetched.content or "").strip()
    title = _normalize_news_title(fetched.title or clean_url, full_text)
    summary = full_text[:500] if full_text else title
    item = NewsItem(
        title=title,
        url=clean_url,
        source=source_name,
        published_at=datetime.now().isoformat(timespec="seconds"),
        summary=summary,
        full_text=full_text,
        news_origin="manual",
        created_at=datetime.now().isoformat(timespec="seconds"),
    )

    if not _is_taiwan_finance_news(item):
        return None, "not_taiwan_finance_news"

    try:
        classified = _batch_classify_news([item], center, emit, ai_model=ai_model)
        if classified:
            item = classified[0]
    except Exception as exc:
        emit(f"User submitted news classification failed: {exc}; fallback to local category")
        item.category = _guess_category(item.title)

    if not item.category:
        item.category = _guess_category(item.title)
    item.category = normalize_news_category(item.category)

    if repository.save(item):
        saved_item = repository.get_by_url(clean_url)
        _record_news_preference(repository, saved_item or item)
        return saved_item or item, "saved"

    existing = repository.get_by_url(clean_url) if hasattr(repository, "get_by_url") else None
    _record_news_preference(repository, existing or item)
    return existing or item, "duplicate"


# URL/path patterns that indicate a non-article, non-news page
_NON_ARTICLE_URL_PATTERNS = [
    "/",  # root home page
    # Query and search pages
    "/opendata", "/opendata/",
    "/query", "/query/",
    "/search", "/search/",
    "/list", "/list/",
    # Trading and market data pages
    "/holiday", "/holiday/",
    "/warrant", "/warrant/",
    "/cbond", "/cbond/",
    "/attention", "/attention/",
    "/product", "/product/",
    "/closing", "/closing/",  # daily closing prices
    # Broker trading pages
    "/broker_trading", "/broker_trading/brokerBS",
    # Report and industry pages
    "/industry", "/industry.html",
    "/report-detail", "/report-detail.html",
    # ETF and company list pages
    "/etf", "/etf/",
    "/company", "/company/",
    # Institutional news landing pages (not article pages)
    "/news/",        # /news/ alone = news listing, not article
    "/news/index",   # news index page
    "/latestNews",   # latest news landing
    "/promoteNewsArticle", "/promoteNewsArticleCh",  # promotion landing
    # Static file paths that are not articles
    "/staticFiles/news",
    "/TIB/zh",
]

_NON_ARTICLE_URL_SUBSTRINGS = [
    "ctee.com.tw/market-stock/",
    "tw.stock.yahoo.com/quote/",
    "tw.stock.yahoo.com/rank/",
    "tw.stock.yahoo.com/tw-market",
    "tw.stock.yahoo.com/s/otc.php",
    "tw.stock.yahoo.com/class-quote",
    "hk.finance.yahoo.com/quote/",
    "msn.com/zh-tw/money/markets",
    "histock.tw/stock/",
    "goodinfo.tw/tw/stock",
    "nstock.tw/",
    "nstock.tw/stock_info",
    "finance.biggo.com.tw/quote/",
    "pchome.megatime.com.tw/m/stockinfo/",
    "statementdog.com/analysis/",
    "treelazy.com/stock/",
    "wantgoo.com/stock/calendar/",
    "fugle.tw/ai/",
    "ytdf.yuanta.com.tw/prod/yesidmz/stockpreview/",
    "www.cnyes.com/twstock/",
    "cnyes.com/twstock/idx_cashflow.aspx",
    "money-link.com.tw/twstock/stocknews.aspx",
    "moneydj.com/kmdj/news/newsreallist.aspx",
    "ctee.com.tw/stock/matchplay",
    "sinotrade.com.tw/richclub/daily_livestream/",
    "sinotrade.com.tw/richclub/hotopic/",
    "statementdog.com/tags/",
    "wantgoo.com/stock/dividend-yield",
    "rate.bot.com.tw/xrt",
    "yuanta.com.tw/eyuanta/securities/news/getapilist",
    "tw.stock.yahoo.com/institutional-trading",
    "cmoney.tw/forum/stock/",
    "ptt.cc/bbs/stock/",
    "my-finance.com.tw/tw/news_detail/",
    "vocus.cc/salon/",
    "youtube.com/watch",
]

# URL query param keys that indicate a query/list page
_NON_ARTICLE_QUERY_KEYS = {
    "query", "search", "broker_trading", "warrant", "opendata",
    "holiday", "attention", "cbond", "marketinfo", "keyword",
    "stock_code", "stk_code", "fund_id",
}

# Title patterns that indicate a non-article, non-news page
_NON_ARTICLE_TITLE_PATTERNS = [
    "首頁",
    "開休市",
    "每日收盤行情",
    "上市權證",
    "上櫃權證",
    "上櫃公布注意",
    "公司債",
    "產品介紹",
    "公開資訊觀測站",
    "臺灣證券交易所",
    "證券櫃檯買賣中心",
    # Institutional / reference pages
    "查詢系統",
    "中心",
    "專區",
    "下載",
    "說明",
    "指南",
    "評分",
    "介紹",
    "參考範例",
    "公司治理中心",
    "產業新聞報導",
    "前瞻產業研究報告",
    "最新行情",
    "歷史行情",
    "盤後交易",
    "當日行情",
    # PDF / report patterns in title
    "PDF",
    "報告",
    "手冊",
    "簡報",
    "年報",
    "財務報告",
    "即時新聞 -",
    "最新股價",
    "即時股價",
    "走勢圖",
    "K線走勢",
    "MSN 財經",
    "逐洞賽",
    "股價與走勢",
    "股價- 技術分析",
    "市場加權指數上漲",
    "漲幅排行",
    "台股盤勢",
    "資金流向",
    "新聞日誌",
    "概念股有哪些",
    "現金殖利率排行",
    "Foreign Exchange Rate",
    "熱門新聞 - 元大證券",
    "法人進出 - Yahoo股市",
    "走勢與討論",
    "股市爆料同學會",
    # Institutional names that are landing pages
    "壯大臺灣資本市場高峰會",
    "公司治理評鑑",
    "產業價值鏈資訊平台",
    "打造亞洲那斯達克",
]

_NON_ARTICLE_TITLE_PATTERNS.extend([
    "股價、新聞、報價",
    "最新股票消息和頭條新聞",
    "股價行情概況",
    "股價走勢",
    "K線走勢",
    "技術分析",
    "基本資料",
    "財報分析",
    "每股盈餘",
    "EPS 即時股價",
    "行事曆",
    "市場公告",
    "年報電子書",
    "估值方法和財務統計數據",
])

# Official source domains whose root pages should be excluded
# Include both bare domain and www-prefixed variants
_OFFICIAL_ROOT_DOMAINS = {
    "twse.com.tw",
    "www.twse.com.tw",
    "mops.twse.com.tw",
    "tpex.org.tw",
    "www.tpex.org.tw",
    "tdcc.com.tw",
    "www.tdcc.com.tw",
}

# TWSE/TPEX subdomains that have news/article content but also landing pages
# Need special path-based filtering to distinguish articles from landing pages
_OFFICIAL_NEWS_DOMAINS = {
    "twse.com.tw",
    "www.twse.com.tw",
    "tpex.org.tw",
    "www.tpex.org.tw",
    "cgc.twse.com.tw",  # corporate governance center
    "accessibility.twse.com.tw",  # TWSE accessibility site
}

# URL patterns for known financial media that are real news articles
# These override generic exclusion rules
_KEEP_MEDIA_URL_PATTERNS = [
    "ctee.com.tw/news/",
    "money.udn.com/money/story/",
    "cna.com.tw/news/",
    "cnyes.com/news/id/",
    "moneydj.com/kmdj/news/newsviewer",
    "chinatimes.com/article/",
    "ltn.com.tw/news/",
    "pts.org.tw/",
    "storm.mg/article/",
]


def _is_non_article_page(item: NewsItem) -> bool:
    """Check if URL or title indicates this is not a news article page.

    Returns True for: homepage, search page, list page, query page,
    institutional landing pages, opendata pages, PDF pages, etc.
    Returns False (article page) if URL is a known media article or
    passes official-domain-specific article path checks.
    """
    url = item.url.lower()
    title = item.title
    parsed = urlparse(url)
    path = parsed.path.lower()
    netloc = parsed.netloc.lower()
    query = parsed.query.lower()
    path_stripped = parsed.path.rstrip("/")

    if netloc in _REDIRECT_ONLY_DOMAINS:
        return True

    if netloc == "vertexaisearch.cloud.google.com":
        clean_title = (item.title or "").strip().lower()
        if re.fullmatch(r"(?:[a-z0-9-]+\.)+[a-z]{2,}", clean_title):
            return True
        if clean_title.startswith("home - ") or clean_title in {"home", "homepage"}:
            return True

    text_for_page_check = f"{item.title} {item.summary} {item.full_text or ''}".lower()
    if any(marker in url or marker in text_for_page_check for marker in _MONEYDJ_INTERNAL_PAGE_MARKERS):
        return True

    if netloc == "tw.stock.yahoo.com" and path_stripped.lower() in ("/news", "/news/"):
        return True

    if any(pattern in url for pattern in _NON_ARTICLE_URL_SUBSTRINGS):
        return True

    # First: check known financial media article patterns — KEEP these
    for pattern in _KEEP_MEDIA_URL_PATTERNS:
        if pattern in url:
            return False

    # Second: reject PDF pages by URL extension
    if url.endswith(".pdf") or path.endswith(".pdf") or "/pdf" in url:
        return True

    # Third: reject pages with query params that indicate query/list pages
    query_keys = set(k.split("=")[0] for k in query.split("&") if k)
    if query_keys & _NON_ARTICLE_QUERY_KEYS:
        return True

    # Fourth: reject URL path patterns that are non-article pages
    # For official news domains, compound patterns like /company or /industry
    # should NOT be rejected here if they have an article-like subpath.
    # Only reject simple (single-component) path patterns here.
    path_parts = path.strip("/").split("/") if path.strip("/") else []
    for pattern in _NON_ARTICLE_URL_PATTERNS:
        if pattern == "/":
            continue
        pattern_stripped = pattern.lstrip("/")
        # Only reject single-component patterns at this stage
        if pattern.count("/") < 2:
            for part in path_parts:
                if part == pattern_stripped or part.startswith(pattern_stripped):
                    # For official news domains, allow if path has article-like subpath
                    if netloc in _OFFICIAL_NEWS_DOMAINS:
                        has_article = False
                        for p2 in path_parts:
                            if (p2.startswith("news") and len(p2) > 4 and not p2.startswith("latestnews")) or \
                               (p2.startswith("article") and len(p2) > 8) or \
                               ("content" in p2 and len(p2) > 7) or \
                               (p2 == "press" and len(p2) == 5):
                                has_article = True
                        if has_article:
                            continue  # skip this pattern, go to next
                    return True

    # Handle "/" pattern: only reject if path is exactly "/" (root)
    # BUT allow trusted media domains whose root IS a news portal
    if path_stripped == "" or path_stripped == "/":
        # Trusted media sites: root URL is a news portal/article page
        trusted_media_patterns = {
            "chinatimes.com",    # includes tech.chinatimes.com, www.chinatimes.com
            "moneydj.com",
            "cna.com.tw",        # includes www.cna.com.tw
            "udn.com",          # includes money.udn.com, www.udn.com
            "cnyes.com",
            "ltn.com.tw",
            "pts.org.tw",
            "storm.mg",
        }
        # Check if netloc matches any trusted media pattern (with or without www)
        is_trusted_media = False
        for pattern in trusted_media_patterns:
            if netloc == pattern or netloc == "www." + pattern or netloc.endswith("." + pattern):
                is_trusted_media = True
                break
        if is_trusted_media:
            return True  # Media root is a portal/list page, not a single article
        if netloc in _OFFICIAL_ROOT_DOMAINS:
            return True  # Official domains root should be rejected
        return True

    # Fifth: reject title patterns that indicate non-article pages
    for pattern in _NON_ARTICLE_TITLE_PATTERNS:
        if pattern in title:
            return True

    # Sixth: for official news domains, require path to contain news/article path
    # components to distinguish real articles from landing pages.
    # For these domains, if path doesn't contain article-like segments, reject.
    if netloc in _OFFICIAL_NEWS_DOMAINS:
        # Check if path has article-like components
        news_count = sum(1 for p in path_parts if p == "news")
        has_article_path = False
        for part in path_parts:
            part_lower = part.lower()
            # Real article paths: news123, article-detail, content-page
            # Multiple "news" components indicate a news archive path (TWSE staticFiles)
            if part.startswith("news") and len(part) > 4:
                has_article_path = True
            if part.startswith("latestnews") or part.startswith("latestNews"):
                pass  # landing page indicator, not article
            if part.startswith("article") and len(part) > 8:
                has_article_path = True
            if "content" in part and len(part) > 7:
                has_article_path = True
            # /press/ subdirectory is a press release article indicator
            if part == "press" and len(part) == 5:
                has_article_path = True
        # Multiple news components (e.g., staticFiles/news/news/...) indicate news archive
        if news_count >= 2:
            has_article_path = True
        if not has_article_path:
            return True  # No article-like path found, reject

    # Seventh: check if it's the root domain of known financial sites
    # Only reject if it's an official domain with root-like path
    if netloc in _OFFICIAL_ROOT_DOMAINS and path_stripped in ("", "/", "/index", "/index.html"):
        return True

    return False
