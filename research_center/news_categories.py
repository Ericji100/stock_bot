"""Canonical news categories for storage and Telegram display."""
from __future__ import annotations

NEWS_CATEGORY_LABELS: dict[str, str] = {
    "market_focus": "台股與大盤",
    "sector_rotation": "題材與族群輪動",
    "ai_semiconductor": "AI / 半導體",
    "company_news": "個股利多利空",
    "supply_chain": "供應鏈與產業",
    "macro_policy": "政策 / 匯率 / 總經",
    "holdings": "庫存持股新聞",
    "other": "其他台股財經新聞",
}

_ALIASES: dict[str, str] = {
    "market_focus": "market_focus",
    "market": "market_focus",
    "大盤與資金動向": "market_focus",
    "台股大盤": "market_focus",
    "台股盤勢": "market_focus",
    "大盤法人": "market_focus",
    "資金動向": "market_focus",
    "法人籌碼": "market_focus",
    "盤中盤後": "market_focus",
    "台股與大盤": "market_focus",
    "sector_rotation": "sector_rotation",
    "sector_strength": "sector_rotation",
    "theme": "sector_rotation",
    "theme_radar": "sector_rotation",
    "題材與族群輪動": "sector_rotation",
    "ai_semiconductor": "ai_semiconductor",
    "ai": "ai_semiconductor",
    "semiconductor": "ai_semiconductor",
    "AI / 半導體": "ai_semiconductor",
    "company_news": "company_news",
    "stock_news": "company_news",
    "個股利多利空": "company_news",
    "supply_chain": "supply_chain",
    "電子供應鏈": "supply_chain",
    "供應鏈與產業": "supply_chain",
    "macro_policy": "macro_policy",
    "macro": "macro_policy",
    "總經與政策": "macro_policy",
    "政策 / 匯率 / 總經": "macro_policy",
    "holdings": "holdings",
    "庫存持股新聞": "holdings",
    "other": "other",
    "其他台股財經新聞": "other",
    "傳產與原物料": "sector_rotation",
    "金融與高股息": "market_focus",
    "風險事件": "company_news",
}


def normalize_news_category(category: str | None) -> str:
    """Return the canonical category key used internally."""
    raw = str(category or "").strip()
    if not raw:
        return "other"
    if raw in _ALIASES:
        return _ALIASES[raw]
    lowered = raw.lower().strip()
    if lowered in _ALIASES:
        return _ALIASES[lowered]
    if lowered in NEWS_CATEGORY_LABELS:
        return lowered
    return "other"


def news_category_label(category: str | None) -> str:
    """Return the Traditional Chinese display label for a category."""
    key = normalize_news_category(category)
    return NEWS_CATEGORY_LABELS.get(key, NEWS_CATEGORY_LABELS["other"])


def ordered_news_category_keys(include_holdings: bool = False) -> list[str]:
    """Return stable display order for news digest grouping."""
    keys = [
        "market_focus",
        "sector_rotation",
        "ai_semiconductor",
        "company_news",
        "supply_chain",
        "macro_policy",
        "other",
    ]
    if include_holdings:
        keys.append("holdings")
    return keys
