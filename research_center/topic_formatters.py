"""Topic formatters for Telegram display."""
from __future__ import annotations

from typing import Any

from .topic_models import TopicApplyResult, TopicChangePack, TopicChangeStatus
from .topic_repository import load_topic_profiles


def _mode_label(value: Any) -> str:
    raw = getattr(value, "value", value)
    return {
        "initial": "初始化",
        "update": "更新",
        "adjust": "調整",
    }.get(str(raw), str(raw))


def _status_label(value: Any) -> str:
    raw = getattr(value, "value", value)
    return {
        "pending": "待審核",
        "confirmed": "已套用",
        "rejected": "已拒絕",
        "failed": "未通過檢查",
    }.get(str(raw), str(raw))


def _confidence_label(value: Any) -> str:
    raw = getattr(value, "value", value)
    return {
        "high": "高",
        "medium": "中",
        "low": "低",
    }.get(str(raw), str(raw))


def _action_label(value: Any) -> str:
    raw = getattr(value, "value", value)
    return {
        "create_theme": "新增題材",
        "update_theme": "更新題材",
        "merge_theme": "合併題材",
        "deprecate_theme": "退場題材",
        "add_company": "新增公司關聯",
        "update_company": "更新公司關聯",
        "add_supply_chain_node": "新增供應鏈節點",
        "update_supply_chain_node": "更新供應鏈節點",
    }.get(str(raw), str(raw).replace("_", " "))


def _source_level_label(value: Any) -> str:
    raw = getattr(value, "value", value)
    return {
        "L1_official": "官方來源",
        "L2_media": "媒體來源",
        "L3_community": "社群來源",
    }.get(str(raw), str(raw).replace("_", " "))


def _satisfaction_label(value: Any) -> str:
    raw = str(value or "")
    return {
        "satisfied": "已滿足",
        "partial": "部分滿足",
        "not_satisfied": "未滿足",
    }.get(raw, raw.replace("_", " "))


def _pack_data_date(pack: TopicChangePack) -> str:
    extra = pack.extra if isinstance(pack.extra, dict) else {}
    command_result = extra.get("command_result") if isinstance(extra.get("command_result"), dict) else {}
    report_metadata = extra.get("report_metadata") if isinstance(extra.get("report_metadata"), dict) else {}
    for key in ("data_date", "report_date", "analysis_date", "created_at"):
        value = command_result.get(key) or report_metadata.get(key)
        if value:
            return str(value)[:10]
    return str(pack.created_at or "")[:10]


def format_change_pack_created_summary(pack: TopicChangePack) -> str:
    """Format a newly generated topic change pack for Telegram display."""
    title = "✅ 變更包已產生" if pack.status == TopicChangeStatus.PENDING else "⚠️ 變更包產生但未通過檢查"
    return "\n".join([
        title,
        f"變更包代號：{pack.change_id}",
        f"資料日期：{_pack_data_date(pack)}",
        f"維護模式：{_mode_label(pack.mode)}",
        f"審核狀態：{_status_label(pack.status)}",
        f"信心度：{_confidence_label(pack.confidence)}",
        f"摘要：{pack.summary}",
        f"變更動作數：{len(pack.actions)}",
        "",
        f"下一步：/topic_review {pack.change_id} 查看詳情",
    ])


def format_change_pack_list(packs: list[TopicChangePack]) -> str:
    """Format a list of change packs for Telegram display."""
    if not packs:
        return "目前沒有變更包。"

    lines = [f"📦 變更包列表（共 {len(packs)} 個）", ""]
    for pack in packs:
        status_icon = {"pending": "⏳", "confirmed": "✅", "rejected": "❌", "failed": "⚠️"}.get(pack.status.value, "❓")
        lines.append(f"{status_icon} `{pack.change_id}` | {_mode_label(pack.mode)} | {pack.summary[:30]}")
    lines.append("")
    lines.append("使用 /topic_review <change_id> 查看詳情")
    return "\n".join(lines)


def format_change_pack_detail(pack: TopicChangePack) -> str:
    """Format a single change pack for Telegram display."""
    status_icon = {"pending": "⏳", "confirmed": "✅", "rejected": "❌", "failed": "⚠️"}.get(pack.status.value, "❓")
    lines = [
        f"📋 變更包詳情",
        f"變更包代號：{pack.change_id}",
        f"資料日期：{_pack_data_date(pack)}",
        f"維護模式：{_mode_label(pack.mode)}",
        f"審核狀態：{status_icon} {_status_label(pack.status)}",
        f"模型：{pack.model}",
        f"信心度：{_confidence_label(pack.confidence)}",
        f"摘要：{pack.summary}",
        "",
        "📌 變更動作：",
    ]

    for i, action in enumerate(pack.actions, 1):
        conf_icon = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(action.confidence.value, "⚪")
        lines.append(f"{i}. 【{_action_label(action.action_type)}】{action.theme_name} ({conf_icon}{_confidence_label(action.confidence)})")
        lines.append(f"   題材代號：{action.theme_id}")
        if action.reason:
            lines.append(f"   原因：{action.reason}")
        if action.evidence:
            ev = action.evidence[0]
            lines.append(f"   證據：{ev.source} ({_source_level_label(ev.source_level)})")

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
        satisfaction_icon = {"satisfied": "✅", "partial": "⚠️", "not_satisfied": "❌"}.get(str(satisfaction), "")
        satisfaction_label = f"{satisfaction_icon} {_satisfaction_label(satisfaction)}".strip()
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
    return f"變更包狀態：{_status_label(status)}，如需重新產生請使用 /topic_maintain"
