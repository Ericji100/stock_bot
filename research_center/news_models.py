"""News data models for the news knowledge base."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


NEWS_SIGNAL_TAGS = {
    "topic_clue": "題材線索",
    "catalyst": "催化事件",
    "counter_evidence": "反證",
    "heat_risk": "過熱風險",
    "official_fact": "官方事實",
    "sentiment": "情緒/小道",
}


@dataclass
class NewsItem:
    """Single news article."""

    id: str = ""
    title: str = ""
    url: str = ""
    source: str = ""
    published_at: str = ""
    category: str = ""
    related_symbols: list[str] = field(default_factory=list)
    related_topics: list[str] = field(default_factory=list)
    summary: str = ""
    full_text: str = ""
    importance_score: int = 0
    impact_direction: str = ""
    tags: list[str] = field(default_factory=list)
    news_signal_score: int = 0
    news_heat_risk_score: int = 0
    news_signal_reason: str = ""
    news_heat_risk_reason: str = ""
    news_origin: str = "refresh"
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "published_at": self.published_at,
            "category": self.category,
            "related_symbols": self.related_symbols,
            "related_topics": self.related_topics,
            "summary": self.summary,
            "full_text": self.full_text,
            "importance_score": self.importance_score,
            "impact_direction": self.impact_direction,
            "tags": self.tags,
            "news_signal_score": self.news_signal_score,
            "news_heat_risk_score": self.news_heat_risk_score,
            "news_signal_reason": self.news_signal_reason,
            "news_heat_risk_reason": self.news_heat_risk_reason,
            "news_origin": self.news_origin,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NewsItem":
        item = cls(
            id=str(data.get("id", "")),
            title=str(data.get("title", "")),
            url=str(data.get("url", "")),
            source=str(data.get("source", "")),
            published_at=str(data.get("published_at", "")),
            category=str(data.get("category", "")),
            related_symbols=list(data.get("related_symbols", []) or []),
            related_topics=list(data.get("related_topics", []) or []),
            summary=str(data.get("summary", "")),
            full_text=str(data.get("full_text", "")),
            importance_score=int(data.get("importance_score", 0) or 0),
            impact_direction=str(data.get("impact_direction", "")),
            tags=list(data.get("tags", []) or []),
            news_signal_score=int(data.get("news_signal_score", 0) or 0),
            news_heat_risk_score=int(data.get("news_heat_risk_score", 0) or 0),
            news_signal_reason=str(data.get("news_signal_reason", "")),
            news_heat_risk_reason=str(data.get("news_heat_risk_reason", "")),
            news_origin=str(data.get("news_origin", "refresh") or "refresh"),
            created_at=str(data.get("created_at", "")),
        )
        return apply_news_signal_tags(item)


def apply_news_signal_tags(item: NewsItem) -> NewsItem:
    """Attach local signal/heat tags used by reports and AI context."""
    existing_tags = list(item.tags or [])
    existing_signal_score = int(item.news_signal_score or 0)
    existing_heat_score = int(item.news_heat_risk_score or 0)
    existing_signal_reason = item.news_signal_reason
    existing_heat_reason = item.news_heat_risk_reason
    result = classify_news_signal(item)
    item.tags = list(dict.fromkeys([*existing_tags, *result["tags"]]))
    item.news_signal_score = max(existing_signal_score, result["news_signal_score"])
    item.news_heat_risk_score = max(existing_heat_score, result["news_heat_risk_score"])
    item.news_signal_reason = existing_signal_reason or result["news_signal_reason"]
    item.news_heat_risk_reason = existing_heat_reason or result["news_heat_risk_reason"]
    return item


def classify_news_signal(item: NewsItem) -> dict[str, Any]:
    text = " ".join(
        [
            item.title or "",
            item.summary or "",
            item.full_text or "",
            item.category or "",
            item.source or "",
            " ".join(item.related_topics or []),
            " ".join(item.related_symbols or []),
        ]
    ).lower()
    tags: list[str] = []
    reasons: list[str] = []
    heat_reasons: list[str] = []
    signal_score = 0
    heat_score = 0

    official_keywords = ("公告", "法說", "財報", "月營收", "營收", "mops", "twse", "tpex", "annual report", "earnings")
    catalyst_keywords = ("打入", "供應", "訂單", "客戶", "量產", "認證", "合約", "得標", "併購", "轉型", "擴產", "出貨")
    counter_keywords = ("澄清", "無關", "下修", "衰退", "虧損", "取消", "延後", "庫存", "降價", "反壟斷", "調查")
    heat_keywords = ("漲停", "連漲", "飆", "狂飆", "爆量", "熱炒", "爆紅", "追高", "大漲", "創高", "噴出")
    sentiment_keywords = ("傳聞", "爆料", "市場傳出", "法人圈", "社群", "論壇", "ptt", "dcard", "mobile01")
    topic_keywords = ("ai", "gpu", "伺服器", "半導體", "高速傳輸", "電源", "散熱", "機器人", "電動車", "低軌", "資料中心", "供應鏈")

    if any(k in text for k in official_keywords):
        tags.append("official_fact")
        signal_score += 35
        reasons.append("含公告、營收、財報或官方來源字眼")
    if any(k in text for k in catalyst_keywords):
        tags.append("catalyst")
        signal_score += 30
        reasons.append("含訂單、客戶、量產、認證或轉型等催化字眼")
    if any(k in text for k in topic_keywords) or item.related_topics:
        tags.append("topic_clue")
        signal_score += 20
        reasons.append("含產業題材或供應鏈線索")
    if any(k in text for k in counter_keywords):
        tags.append("counter_evidence")
        signal_score -= 10
        heat_score += 5
        reasons.append("含可能反證或基本面壓力")
    if any(k in text for k in sentiment_keywords):
        tags.append("sentiment")
        heat_score += 15
        heat_reasons.append("含傳聞、社群或論壇線索")
    if any(k in text for k in heat_keywords):
        tags.append("heat_risk")
        heat_score += 35
        heat_reasons.append("含漲停、爆量、創高或追價語氣")
    if int(item.importance_score or 0) >= 85 and "official_fact" not in tags:
        heat_score += 10
        heat_reasons.append("高重要度但非官方確認，需檢查是否只是熱度")

    tags = list(dict.fromkeys(tags))
    return {
        "tags": tags,
        "news_signal_score": max(0, min(100, signal_score)),
        "news_heat_risk_score": max(0, min(100, heat_score)),
        "news_signal_reason": "；".join(reasons[:3]),
        "news_heat_risk_reason": "；".join(heat_reasons[:3]),
    }


@dataclass
class NewsDigest:
    """Categorized news digest for Telegram display."""

    category: str = ""
    items: list[NewsItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "items": [item.to_dict() for item in self.items],
        }


@dataclass
class HoldingNewsGroup:
    """News group for a single holding stock."""

    code: str = ""
    name: str = ""
    items: list[NewsItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "items": [item.to_dict() for item in self.items],
        }


@dataclass
class NewsPreference:
    """A lightweight record of user-saved news preference signals."""

    id: str = ""
    url: str = ""
    title: str = ""
    category: str = ""
    normalized_category: str = ""
    source: str = ""
    news_type: str = ""
    weight: int = 1
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "title": self.title,
            "category": self.category,
            "normalized_category": self.normalized_category,
            "source": self.source,
            "news_type": self.news_type,
            "weight": self.weight,
            "created_at": self.created_at,
        }
