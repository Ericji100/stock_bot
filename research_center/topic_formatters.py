"""Topic formatters for Telegram display."""
from __future__ import annotations

from typing import Any

from .topic_models import TopicApplyResult, TopicChangePack
from .topic_repository import load_topic_profiles


def format_change_pack_list(packs: list[TopicChangePack]) -> str:
    """Format a list of change packs for Telegram display."""
    if not packs:
        return "目前沒有變更包。"

    lines = [f"📦 變更包列表（共 {len(packs)} 個）", ""]
    for pack in packs:
        status_icon = {"pending": "⏳", "confirmed": "✅", "rejected": "❌", "failed": "⚠️"}.get(pack.status.value, "❓")
        lines.append(f"{status_icon} `{pack.change_id}` | {pack.mode.value} | {pack.summary[:30]}")
    lines.append("")
    lines.append("使用 /topic_review <change_id> 查看詳情")
    return "\n".join(lines)


def format_change_pack_detail(pack: TopicChangePack) -> str:
    """Format a single change pack for Telegram display."""
    status_icon = {"pending": "⏳", "confirmed": "✅", "rejected": "❌", "failed": "⚠️"}.get(pack.status.value, "❓")
    lines = [
        f"📋 變更包詳情",
        f"ID：{pack.change_id}",
        f"模式：{pack.mode.value}",
        f"狀態：{status_icon} {pack.status.value}",
        f"模型：{pack.model}",
        f"信心度：{pack.confidence}",
        f"摘要：{pack.summary}",
        "",
        "📌 變更動作：",
    ]

    for i, action in enumerate(pack.actions, 1):
        conf_icon = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(action.confidence.value, "⚪")
        lines.append(f"{i}. 【{action.action_type.value}】{action.theme_name} ({conf_icon}{action.confidence.value})")
        lines.append(f"   ID：{action.theme_id}")
        if action.reason:
            lines.append(f"   原因：{action.reason}")
        if action.evidence:
            ev = action.evidence[0]
            lines.append(f"   證據：{ev.source} ({ev.source_level.value})")

    if pack.warnings:
        lines.append("")
        lines.append("⚠️ 警告：")
        for w in pack.warnings:
            lines.append(f"  • {w}")

    # Show adjustment_check if present
    adj_check = pack.adjustment_check or {}
    if adj_check:
        lines.append("")
        lines.append("🧭 調整意見檢查：")
        user_req = adj_check.get("user_request_summary", "")
        if user_req:
            lines.append(f"  使用者要求：{user_req}")
        changes_made = adj_check.get("changes_made", [])
        if changes_made:
            lines.append("  已完成：")
            for c in changes_made:
                lines.append(f"    • {c}")
        not_satisfied = adj_check.get("not_fully_satisfied", [])
        if not_satisfied:
            lines.append("  未完成：")
            for c in not_satisfied:
                lines.append(f"    • {c}")
        satisfaction = adj_check.get("satisfaction", "")
        satisfaction_label = {"satisfied": "✅ satisfied", "partial": "⚠️ partial", "not_satisfied": "❌ not_satisfied"}.get(satisfaction, satisfaction)
        lines.append(f"  結論：{satisfaction_label}")

    lines.append("")
    lines.append(format_next_steps(pack.change_id, pack.status.value))
    return "\n".join(lines)


def format_apply_result(result: TopicApplyResult) -> str:
    """Format an apply result for Telegram display."""
    icon = "✅" if result.success else "❌"
    lines = [
        f"{icon} 套用結果：{'成功' if result.success else '部分失敗'}",
        f"變更包：{result.change_id}",
        f"新增：{result.created} | 更新：{result.updated} | 合併：{result.merged} | 略過：{result.skipped} | 失敗：{result.failed}",
    ]
    if result.errors:
        lines.append("")
        lines.append("錯誤：")
        for e in result.errors:
            lines.append(f"  • {e}")
    if result.backup_path:
        lines.append(f"\n備份路徑：{result.backup_path}")
    lines.append("")
    lines.append("使用 /topic_profiles 查看正式題材庫")
    return "\n".join(lines)


def format_topic_profiles() -> str:
    """Format the formal topic library for Telegram display."""
    profiles = load_topic_profiles()
    if not profiles:
        return (
            "目前正式題材知識庫沒有題材。\n"
            "請執行 /topic_maintain 讓 AI 建立第一版題材知識庫。"
        )

    lines = [f"📚 正式題材知識庫（共 {len(profiles)} 個題材）", ""]
    for p in profiles:
        lines.append(f"• **{p.theme_name}** (`{p.theme_id}`)")
        if p.keywords:
            lines.append(f"  關鍵詞：{', '.join(p.keywords[:5])}")
    lines.append("")
    lines.append("使用 /topic_maintain [--model gemini|deepseek|minimax] 大範圍更新題材庫")
    return "\n".join(lines)


def format_next_steps(change_id: str, status: str = "pending") -> str:
    """Format next-step instructions for Telegram display."""
    if status == "pending":
        return (
            "📌 下一步操作：\n"
            f"• /topic_confirm {change_id} - 確認套用\n"
            f"• /topic_reject {change_id} - 拒絕\n"
            "也可直接使用下方按鈕操作。"
        )
    if status == "failed":
        return (
            "📌 下一步操作：\n"
            f"• /topic_reject {change_id} - 拒絕\n"
            "• /topic_maintain - 重新產生\n"
            "也可直接使用下方按鈕操作。"
        )
    return f"變更包已 {status}，如需重新產生請使用 /topic_maintain"
