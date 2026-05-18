from __future__ import annotations

from urllib.parse import urlparse

from .models import SourceItem

LEVEL_1_DOMAINS = ("twse.com.tw", "tpex.org.tw", "mops.twse.com.tw", "mopsov.twse.com.tw")
LEVEL_2_DOMAINS = ("moneydj.com", "cnyes.com", "ctee.com.tw", "udn.com", "digitimes.com", "trendforce.com", "cna.com.tw")
LEVEL_4_DOMAINS = ("ptt.cc", "dcard.tw", "mobile01.com", "threads.net", "x.com", "twitter.com", "cmoney.tw", "social.cmoney.tw")


def rank_source(url: str, title: str = "") -> str:
    host = urlparse(url).netloc.lower()
    if any(domain in host for domain in LEVEL_1_DOMAINS):
        return "Level 1"
    if any(domain in host for domain in LEVEL_2_DOMAINS):
        return "Level 2"
    if any(domain in host for domain in LEVEL_4_DOMAINS):
        return "Level 4"
    if not url:
        return "Level 5"
    return "Level 3"


def make_source_items(raw_sources: list[dict[str, str]]) -> list[SourceItem]:
    items: list[SourceItem] = []
    seen: set[str] = set()
    for raw in raw_sources:
        url = str(raw.get("url") or "").strip()
        title = str(raw.get("title") or url or "未命名來源").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        items.append(
            SourceItem(
                source_id=f"S{len(items) + 1:03d}",
                title=title,
                url=url,
                source_level=rank_source(url, title),
                published_date=raw.get("published_date"),
                snippet=raw.get("snippet"),
                provider=raw.get("provider"),
                provider_detail=raw.get("provider_detail"),
                fetch_provider=raw.get("fetch_provider"),
                fetch_status=raw.get("fetch_status"),
                failure_reason=raw.get("failure_reason"),
                found_by=raw.get("found_by", []),
            )
        )
    return items


