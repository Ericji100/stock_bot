from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from html import unescape
from typing import Callable
from urllib.parse import quote_plus, urljoin

import httpx

from .config import load_research_config
from .models import SourceItem
from .source_rank import make_source_items

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
SERPER_SEARCH_URL = "https://google.serper.dev/search"

COMMON_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.7,en;q=0.6",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


@dataclass(frozen=True)
class ForumFetchResult:
    sources: list[SourceItem]
    notes: list[str]
    failure_count: int = 0


def fetch_forum_sources(query: str, report_date: date | None = None, deep: bool = False, progress: Callable[[str], None] | None = None) -> ForumFetchResult:
    if not query.strip():
        return ForumFetchResult([], ["論壇查詢略過：查詢字串為空。"], failure_count=0)

    raw_sources: list[dict[str, str]] = []
    notes: list[str] = []
    limit = 8 if deep else 4
    failure_count = 0
    collectors = (_collect_ptt_stock, _collect_dcard, _collect_mobile01, _collect_cmoney)
    with httpx.Client(timeout=10.0, follow_redirects=True, headers=COMMON_HEADERS) as client:
        for collector in collectors:
            label = _collector_label(collector.__name__)
            if progress:
                progress(f"論壇來源：查詢 {label}")
            try:
                before = len(raw_sources)
                raw_sources.extend(collector(client, query, limit))
                added = len(raw_sources) - before
                if progress:
                    progress(f"論壇來源：{label} 完成，新增 {added} 筆")
            except Exception as exc:
                note = f"{collector.__name__} 失敗：{exc}"
                notes.append(note)
                failure_count += 1
                if progress:
                    progress(f"論壇來源：{label} 失敗：{exc}")

        if progress:
            progress(f"論壇來源搜尋完成：成功 {len(raw_sources)} 筆，失敗 {failure_count} 筆")

    if failure_count > 0 and raw_sources:
        notes.append(
            "論壇搜尋備援：部分論壇直連失敗；Serper/Jina 已停用，後續由 Tavily Search 與 Gemini Search fallback 補足外部來源。"
        )
    elif failure_count > 0:
        notes.append(
            "論壇搜尋備援：Serper/Jina 已停用；後續由 Tavily Search 與 Gemini Search fallback 補足外部來源。"
        )

    sources = make_source_items(raw_sources)
    if report_date is not None:
        sources = [source for source in sources if _source_date_allowed(source, report_date)]
        notes.append("--date 模式：論壇來源若無法確認發布日期，會被保守排除。")
    if not sources and not notes:
        notes.append("未找到可用論壇來源。")
    return ForumFetchResult(sources[: limit * 5], notes, failure_count=failure_count)


def _collector_label(name: str) -> str:
    return {
        "_collect_ptt_stock": "PTT Stock",
        "_collect_dcard": "Dcard",
        "_collect_mobile01": "Mobile01",
        "_collect_cmoney": "理財寶股市爆料同學會",
    }.get(name, name)


def _collect_ptt_stock(client: httpx.Client, query: str, limit: int) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for term in _query_terms(query):
        url = f"https://www.ptt.cc/bbs/Stock/search?q={quote_plus(term)}"
        response = client.get(url, cookies={"over18": "1"})
        response.raise_for_status()
        for match in re.finditer(r'<div class="title">\s*<a href="([^"]+)">(.+?)</a>', response.text, re.S):
            href, title = match.groups()
            items.append({"title": _clean_html(title), "url": urljoin("https://www.ptt.cc", href), "snippet": f"PTT Stock search result: {term}"})
            if len(items) >= limit:
                return _dedupe_raw(items)
    return _dedupe_raw(items)


def _collect_dcard(client: httpx.Client, query: str, limit: int) -> list[dict[str, str]]:
    items = _collect_dcard_api(client, query, limit)
    if items:
        return items
    url = f"https://www.dcard.tw/search?query={quote_plus(query)}"
    response = client.get(url, headers={**COMMON_HEADERS, "Referer": "https://www.dcard.tw/"})
    response.raise_for_status()
    html = response.text
    for match in re.finditer(r'href="(/f/[^"?]+/p/\d+[^\"]*)"[^>]*>(.*?)</a>', html, re.S):
        href, title = match.groups()
        title = _clean_html(title)
        if title:
            items.append({"title": title, "url": urljoin("https://www.dcard.tw", href), "snippet": "Dcard web search result"})
        if len(items) >= limit:
            break
    return _dedupe_raw(items[:limit])


def _collect_dcard_api(client: httpx.Client, query: str, limit: int) -> list[dict[str, str]]:
    url = "https://www.dcard.tw/service/api/v2/search/posts"
    response = client.get(url, params={"query": query, "limit": limit}, headers={**COMMON_HEADERS, "Referer": "https://www.dcard.tw/"})
    response.raise_for_status()
    payload = response.json()
    items: list[dict[str, str]] = []
    for row in payload if isinstance(payload, list) else []:
        title = str(row.get("title") or "Dcard 討論")
        post_id = row.get("id")
        forum_alias = str(row.get("forumAlias") or row.get("forumName") or "stock")
        created = str(row.get("createdAt") or "")[:10] or None
        if post_id:
            items.append({"title": title, "url": f"https://www.dcard.tw/f/{forum_alias}/p/{post_id}", "published_date": created or "", "snippet": "Dcard API search result"})
    return items[:limit]


def _collect_mobile01(client: httpx.Client, query: str, limit: int) -> list[dict[str, str]]:
    url = f"https://www.mobile01.com/search.php?q={quote_plus(query)}"
    response = client.get(url, headers={**COMMON_HEADERS, "Referer": "https://www.mobile01.com/"})
    response.raise_for_status()
    html = response.text
    items: list[dict[str, str]] = []
    patterns = [
        r'href="(/topicdetail\.php\?f=\d+&t=\d+[^"]*)"[^>]*>(.*?)</a>',
        r'href="(https://www\.mobile01\.com/topicdetail\.php\?f=\d+&t=\d+[^"]*)"[^>]*>(.*?)</a>',
        r'href="(/topic/\d+[^"]*)"[^>]*>(.*?)</a>',
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, html, re.S):
            href, title = match.groups()
            title = _clean_html(title)
            if title:
                items.append({"title": title, "url": urljoin("https://www.mobile01.com", href), "snippet": "Mobile01 search result"})
            if len(items) >= limit:
                return _dedupe_raw(items)
    return _dedupe_raw(items)


def _collect_cmoney(client: httpx.Client, query: str, limit: int) -> list[dict[str, str]]:
    code = _extract_stock_code(query)
    items: list[dict[str, str]] = []
    if code:
        url = f"https://social.cmoney.tw/forum/stock/{code}?tab=discuss"
        response = client.get(url, headers={**COMMON_HEADERS, "Referer": "https://social.cmoney.tw/"})
        response.raise_for_status()
        title = _clean_html(_first_match(response.text, r"<title>(.*?)</title>") or f"{code} 理財寶個股討論")
        snippet = _clean_html(_first_match(response.text, r'<meta[^>]+name="description"[^>]+content="([^"]+)"') or "理財寶股市爆料同學會個股討論區")
        items.append({"title": title or f"{code} 理財寶個股討論", "url": url, "snippet": snippet or "CMoney stock discussion page"})
    if len(items) < limit:
        url = f"https://www.cmoney.tw/forum/search?keyword={quote_plus(query)}"
        try:
            response = client.get(url, headers={**COMMON_HEADERS, "Referer": "https://www.cmoney.tw/forum/"})
            response.raise_for_status()
            for match in re.finditer(r'href="([^"]*(?:cmoney\.tw|/forum/)[^"]*)"[^>]*>(.*?)</a>', response.text, re.S):
                href, title = match.groups()
                title = _clean_html(title)
                full_url = urljoin("https://www.cmoney.tw", href)
                if title and "cmoney.tw" in full_url:
                    items.append({"title": title, "url": full_url, "snippet": "CMoney forum search result"})
                if len(items) >= limit:
                    break
        except Exception:
            pass
    return _dedupe_raw(items[:limit])


def _collect_forum_search_fallback(client: httpx.Client, query: str, label: str, limit: int, serper_api_key: str | None) -> list[dict[str, str]]:
    if not serper_api_key:
        return []
    site_query = _site_query(label, query)
    if not site_query:
        return []
    headers = {"X-API-KEY": serper_api_key, "Content-Type": "application/json"}
    payload = {"q": site_query, "num": limit, "gl": "tw", "hl": "zh-tw"}
    response = client.post(SERPER_SEARCH_URL, headers=headers, json=payload)
    response.raise_for_status()
    data = response.json()
    items: list[dict[str, str]] = []
    for raw in (data.get("organic") or [])[:limit]:
        url = str(raw.get("link") or "").strip()
        if not url:
            continue
        items.append(
            {
                "title": str(raw.get("title") or url),
                "url": url,
                "published_date": str(raw.get("date") or "") or None,
                "snippet": f"{label} Serper site: fallback: {raw.get('snippet') or ''}"[:500],
            }
        )
    return _dedupe_raw(items)


def _site_query(label: str, query: str) -> str:
    mapping = {
        "PTT Stock": "site:ptt.cc/bbs/Stock",
        "Dcard": "site:dcard.tw/f 股票 OR 投資 OR 理財",
        "Mobile01": "site:mobile01.com 股票 投資",
        "理財寶股市爆料同學會": "site:social.cmoney.tw/forum/stock OR site:cmoney.tw/forum",
    }
    prefix = mapping.get(label)
    return f"{prefix} {query}" if prefix else ""


def _query_terms(query: str) -> list[str]:
    code = _extract_stock_code(query)
    parts = [query]
    if code:
        parts.append(code)
    for token in re.split(r"\s+", query):
        token = token.strip()
        if token and token not in parts:
            parts.append(token)
    return parts[:4]


def _extract_stock_code(query: str) -> str | None:
    match = re.search(r"(?<!\d)(\d{4})(?!\d)", query)
    return match.group(1) if match else None


def _serper_api_key() -> str | None:
    try:
        config = load_research_config()
    except Exception:
        return None
    if not getattr(config, "enable_serper_search", False):
        return None
    return config.serper_api_key


def _source_date_allowed(source: SourceItem, report_date: date) -> bool:
    if not source.published_date:
        return False
    try:
        published = datetime.strptime(source.published_date[:10], "%Y-%m-%d").date()
    except ValueError:
        return False
    return published <= report_date


def _clean_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", "", value)
    return unescape(re.sub(r"\s+", " ", text)).strip()


def _first_match(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text, re.S | re.I)
    return match.group(1) if match else None


def _dedupe_raw(items: list[dict[str, str]]) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        url = str(item.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        output.append(item)
    return output
