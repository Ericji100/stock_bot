from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

THEME_MARKET_COMMANDS = {"theme_radar", "theme_flow", "sector_strength"}

IRRELEVANT_MARKET_PHRASES = (
    "farmers market",
    "farmer's market",
    "food market",
    "green market",
    "open-air market",
    "open air market",
    "night market",
    "flea market",
    "marketplace event",
    "event calendar",
    "events calendar",
    "festival",
    "tour texas",
    "visit el paso",
    "marriott.com/events",
    "centralmarket.com/events",
    "coralgables.com/events",
    "sanpedrosquaremarket.com/calendar",
    "holiday calendar",
    "stock market holiday calendar",
    "crypto market cap",
    "cryptocurrency market",
    "binance market update",
    "weekly market structure",
    "oil and corn",
)

IRRELEVANT_THEME_PHRASES = (
    "大陸a股",
    "a股市場",
    "科創50",
    "寒武纪",
    "海光信息",
    "寧德時代",
    "宁德时代",
    "商業航天賽道",
)

CLICKBAIT_SOURCE_PHRASES = (
    "必漲股",
    "必漲",
    "穩賺",
    "保證獲利",
    "賺多少",
    "熱門股票懶人包",
)

IRRELEVANT_MARKET_DOMAINS = (
    "tourtexas.com",
    "visitelpaso.com",
    "centralmarket.com",
    "coralgables.com",
    "sanpedrosquaremarket.com",
    "event.marriott.com",
    "hotelnikkosf.com",
    "foodielandnm.com",
    "downtownwestchester.com",
    "choosechicago.com",
    "cjvillage.com",
    "gogastonnc.org",
    "visitpagosasprings.com",
)

TAIWAN_EQUITY_TERMS = (
    "台股",
    "上市",
    "上櫃",
    "櫃買",
    "證交所",
    "twse",
    "tpex",
    "taiwan stock",
    "taiwan stocks",
    "taiwan index",
    "台灣證券",
    "類股",
    "族群",
)


def is_irrelevant_market_source(source: Any, command: str | None = None) -> bool:
    """Return True for generic "market" hits unrelated to Taiwan equities."""
    if command and command not in THEME_MARKET_COMMANDS:
        return False
    title = str(getattr(source, "title", "") or "").lower()
    url = str(getattr(source, "url", "") or "").lower()
    summary = str(getattr(source, "summary", "") or getattr(source, "snippet", "") or "").lower()
    full_text = str(getattr(source, "full_text", "") or "").lower()
    provider_detail = str(getattr(source, "provider_detail", "") or "").lower()
    visible_text = " ".join([title, url, summary, full_text])
    text = " ".join([visible_text, provider_detail])
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if any(domain in host for domain in IRRELEVANT_MARKET_DOMAINS):
        return True
    if _is_generic_market_date_query(provider_detail) and not any(term in visible_text for term in TAIWAN_EQUITY_TERMS):
        return True
    if _is_non_taiwan_equity_source(visible_text):
        return True
    if _is_clickbait_social_or_video(host, visible_text):
        return True
    return any(phrase in text for phrase in IRRELEVANT_MARKET_PHRASES)


def _is_generic_market_date_query(provider_detail: str) -> bool:
    if "query=market " not in provider_detail:
        return False
    return any(token in provider_detail for token in ("202", "20", "/"))


def _is_non_taiwan_equity_source(visible_text: str) -> bool:
    if not any(phrase in visible_text for phrase in IRRELEVANT_THEME_PHRASES):
        return False
    return not any(term in visible_text for term in TAIWAN_EQUITY_TERMS)


def _is_clickbait_social_or_video(host: str, visible_text: str) -> bool:
    social_or_video = any(domain in host for domain in ("youtube.com", "youtu.be", "threads.com", "threads.net"))
    if not social_or_video:
        return False
    return any(phrase in visible_text for phrase in CLICKBAIT_SOURCE_PHRASES)
