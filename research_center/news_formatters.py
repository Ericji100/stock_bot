"""Telegram news formatters for categorized news digest messages."""
from __future__ import annotations

from html import escape

from .news_categories import news_category_label
from .news_models import NEWS_SIGNAL_TAGS, HoldingNewsGroup, NewsDigest, NewsItem


def _html_link(title: str, url: str) -> str:
    safe_title = escape(title or "未命名新聞")
    safe_url = escape(url or "", quote=True)
    if not safe_url:
        return safe_title
    return f'<a href="{safe_url}">{safe_title}</a>'


def _html_text(text: str) -> str:
    return escape(text or "")


def _news_id_label(item: NewsItem) -> str:
    if not item.id:
        return ""
    return f"<code>N{_html_text(item.id)}</code> "


def _tag_label_text(item: NewsItem) -> str:
    labels = [NEWS_SIGNAL_TAGS.get(tag, tag) for tag in (item.tags or [])[:3]]
    return " / ".join(labels)


def format_news_digest(digests: list[NewsDigest], period_label: str = "最新") -> str:
    """Format categorized news digest for Telegram.

    The digest intentionally shows titles first. Users can open the source by
    tapping the title, or request a stored summary with /news_detail N{id}.
    """
    lines = [f"📰 {period_label}新聞摘要\n"]
    total = sum(len(d.items) for d in digests)
    lines.append(f"共 {total} 則")
    lines.append("點標題可開啟原文；輸入 /news_detail N編號 可查看摘要。")

    for digest in digests:
        category_label = news_category_label(digest.category)
        if not digest.items:
            lines.append(f"\n📌 {category_label}")
            lines.append("  本期暫無符合新聞")
            continue
        lines.append(f"\n📌 {category_label}")
        for item in digest.items[:8]:
            pub = item.published_at[:10] if item.published_at else ""
            symbols = " ".join(item.related_symbols[:5])
            symbols_text = f" ({symbols})" if symbols else ""
            lines.append(f"• {_news_id_label(item)}{_html_link(item.title, item.url)}{_html_text(symbols_text)}")
            lines.append(f"  <i>{_html_text(item.source)} {_html_text(pub)}</i>")
        if len(digest.items) > 8:
            lines.append(f"  另有 {len(digest.items) - 8} 則")
    return "\n".join(lines)


def format_holding_news(groups: list[HoldingNewsGroup]) -> str:
    """Format holding-specific news for Telegram."""
    lines = ["📰 庫存持股新聞\n"]

    for group in groups:
        lines.append(f"\n📌 {group.code} {group.name}")
        if not group.items:
            lines.append("  無新聞")
            continue
        for item in group.items[:5]:
            pub = item.published_at[:10] if item.published_at else ""
            lines.append(f"• {_news_id_label(item)}{_html_link(item.title, item.url)}")
            lines.append(f"  <i>{_html_text(item.source)} {_html_text(pub)}</i>")
        if len(group.items) > 5:
            lines.append(f"  另有 {len(group.items) - 5} 則")

    return "\n".join(lines)


def format_news_refresh_result(saved: int, skipped: int, total_categories: int) -> str:
    """Format news refresh summary."""
    return (
        "📰 新聞整理完成\n"
        f"新增：{saved} 則\n"
        f"略過重複：{skipped} 則\n"
        f"分類數：{total_categories}\n"
    )


def format_news_detail(item: NewsItem | None) -> str:
    """Format a single stored news item for Telegram."""
    if item is None:
        return "找不到這則新聞，請確認 news_id，例如 /news_detail N123。"

    pub = item.published_at[:19] if item.published_at else ""
    lines = [
        "📰 新聞摘要",
        "",
        _html_link(item.title, item.url),
    ]
    if item.id:
        lines.append(f"ID：<code>N{_html_text(item.id)}</code>")
    if item.category:
        lines.append(f"分類：{_html_text(news_category_label(item.category))}")
    if item.source or pub:
        lines.append(f"來源：{_html_text(item.source)} {_html_text(pub)}")
    if item.related_symbols:
        lines.append(f"相關股票：{_html_text(' '.join(item.related_symbols[:10]))}")
    if item.related_topics:
        lines.append(f"相關題材：{_html_text('、'.join(item.related_topics[:10]))}")
    tag_text = _tag_label_text(item)
    if tag_text:
        lines.append(f"新聞標示：{_html_text(tag_text)}")
    if item.news_signal_score or item.news_heat_risk_score:
        lines.append(f"線索分：{item.news_signal_score}；過熱風險：{item.news_heat_risk_score}")
    if item.news_signal_reason:
        lines.append(f"線索原因：{_html_text(item.news_signal_reason)}")
    if item.news_heat_risk_reason:
        lines.append(f"過熱原因：{_html_text(item.news_heat_risk_reason)}")
    summary = (item.summary or item.full_text or "").strip().replace("\n", " ")
    if summary:
        lines.extend(["", _html_text(summary[:500])])
    return "\n".join(lines)
