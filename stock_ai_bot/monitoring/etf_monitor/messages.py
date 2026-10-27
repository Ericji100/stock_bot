from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .models import AcceptedCoreState, Decision, MarketDataSnapshot, MonitorPhase


def _number(value: Decimal | None, places: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:.{places}f}"


def _phase_text(phase: MonitorPhase) -> str:
    return {
        MonitorPhase.PREVIEW: "盤中預覽（非正式訊號）",
        MonitorPhase.DECISION: "收盤後正式決策",
        MonitorPhase.RECONCILIATION: "晚間完整資料確認",
        MonitorPhase.TEST: "模擬測試",
    }[phase]


def _phase_time(phase: MonitorPhase) -> str:
    return {
        MonitorPhase.PREVIEW: "12:45",
        MonitorPhase.DECISION: "13:55",
        MonitorPhase.RECONCILIATION: "22:00",
        MonitorPhase.TEST: "模擬時點",
    }[phase]


def _action_text(value: Any) -> str:
    return {
        Decision.BUY_NEXT_OPEN.value: "進場",
        Decision.SELL_NEXT_OPEN.value: "出場",
        Decision.HOLD.value: "維持原狀",
    }.get(str(value or ""), "既有策略安排")


def _exit_reason_text(reason: str | None) -> str:
    translations = {
        "INITIAL_PROTECTIVE_STOP": "跌破固定初始防守價",
        "STRUCTURAL_STOP": "跌破策略防守價",
        "MACD_RED_TO_GREEN": "動能轉弱條件成立",
        "REGIME_EXIT": "均線多頭區間結束",
        "PROFIT_EXIT": "獲利保護條件成立",
        "KD_PROFIT_EXIT": "震盪指標死亡交叉停利",
    }
    if not reason:
        return "策略出場條件成立"
    if reason not in translations:
        raise ValueError("public ETF message has an unmapped exit reason")
    return translations[reason]


def _hold_text(core: AcceptedCoreState) -> str:
    if core.active_position:
        return "目前持有，持續觀察"
    return "目前空手，等待下一次正式訊號"


def _data_time(value: datetime | None) -> str | None:
    if value is None or value.tzinfo is None:
        return None
    return value.astimezone(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M")


def _market_data_text(
    *, phase: MonitorPhase, core: AcceptedCoreState, snapshot: MarketDataSnapshot
) -> str:
    quote = snapshot.quote
    latest = snapshot.raw_row_for(snapshot.as_of) or {}
    if ((quote is not None and quote.source == "TWSE_MIS_OFFICIAL")
            or snapshot.source == "HASH_VERIFIED_REGISTRY_PLUS_TWSE_MIS"):
        source = "臺灣證券交易所（官方即時行情）"
    elif latest.get("source") == "TWSE_OFFICIAL_DAILY_DELTA":
        source = "臺灣證券交易所（官方日行情）"
    elif snapshot.source == "HASH_VERIFIED_ETF_SNAPSHOT_REGISTRY":
        source = "已驗證歷史日行情"
    else:
        source = "已驗證策略行情"
    label = "盤中現價" if phase is MonitorPhase.PREVIEW else "收盤價"
    lines = [f"{label}：{_number(core.raw_close)} 元"]
    trade_time = _data_time(None if quote is None else quote.last_trade_at)
    if trade_time is not None:
        kind = "盤中" if phase is MonitorPhase.PREVIEW else "收盤"
        lines.append(f"價格時間：{trade_time}（{kind}）")
    else:
        lines.append(f"價格日期：{snapshot.as_of.isoformat()}")
    lines.append(f"資料來源：{source}")
    return "\n".join(lines)


def _transition_text(
    *, core: AcceptedCoreState, transition: Mapping[str, Any] | None
) -> str:
    if not transition or transition.get("outcome") == "UNCHANGED":
        return ""
    old_action = str(transition.get("old_action") or "")
    new_action = str(transition.get("new_action") or core.decision.value)
    outcome = str(transition.get("outcome") or "")
    if old_action == Decision.BUY_NEXT_OPEN.value and new_action == Decision.HOLD.value:
        state = "維持持有觀察" if core.active_position else "維持空手"
        detail = f"收盤資料確認後，取消先前進場訊號，{state}。"
    elif old_action == Decision.SELL_NEXT_OPEN.value and new_action == Decision.HOLD.value:
        state = "維持持有觀察" if core.active_position else "維持空手等待"
        detail = f"收盤資料確認後，取消先前出場訊號，{state}。"
    elif old_action and new_action and old_action != new_action:
        detail = (
            "收盤資料確認後，先前"
            f"{_action_text(old_action)}訊號改為{_action_text(new_action)}訊號。"
        )
    elif outcome == "CREATED_BY_RECONCILIATION":
        detail = f"收盤資料確認後，新增{_action_text(new_action)}訊號。"
    elif outcome == "RESTORED_AFTER_RECONCILIATION":
        detail = f"資料恢復確認後，恢復先前{_action_text(new_action)}訊號。"
    elif outcome == "SUPERSEDED_BY_RECONCILIATION":
        detail = (
            f"收盤資料確認後，更新先前{_action_text(new_action)}訊號的參考資訊，"
            "策略安排不變。"
        )
    elif outcome == "CANCELLED_BY_RECONCILIATION":
        detail = f"收盤資料確認後，取消先前{_action_text(old_action)}訊號。"
    else:
        detail = "收盤資料確認後，正式策略判讀已更新。"
    return f"\n更正內容：{detail}原紀錄保留於內部稽核。"


def render_core_message(
    *,
    phase: MonitorPhase,
    core: AcceptedCoreState,
    snapshot: MarketDataSnapshot,
    correction_of: str | None = None,
    pending_transition: Mapping[str, Any] | None = None,
) -> str:
    if core.decision is Decision.DATA_UNAVAILABLE:
        return render_data_unavailable(
            phase=phase, trading_date=core.as_of.isoformat(), reason_code=""
        )
    market_data = _market_data_text(phase=phase, core=core, snapshot=snapshot)
    when = f"{core.as_of.isoformat()} {_phase_time(phase)}"
    if phase is MonitorPhase.PREVIEW:
        preview = {
            Decision.BUY_NEXT_OPEN: "進場條件暫時出現",
            Decision.SELL_NEXT_OPEN: "出場條件暫時出現",
            Decision.HOLD: "目前沒有新的進出場條件",
            Decision.DATA_UNAVAILABLE: "資料不足，無法形成預覽判讀",
        }[core.decision]
        return (
            "0050 趨勢策略｜盤中預覽\n"
            f"訊息性質：{_phase_text(phase)}\n"
            f"觀察時間：{when}\n"
            f"{market_data}\n"
            f"預覽判讀：{preview}\n"
            "策略安排：僅供盤中觀察，須待收盤後正式決策確認"
        )

    transition_text = _transition_text(core=core, transition=pending_transition)
    correction_text = ""
    if correction_of is not None and not transition_text:
        correction_text = (
            "\n更正內容：收盤資料確認後，正式策略判讀已更新；"
            "原紀錄保留於內部稽核。"
        )
    common = (
        f"訊息性質：{_phase_text(phase)}\n"
        f"訊號時間：{when}\n"
        f"{market_data}"
    )
    if core.decision is Decision.BUY_NEXT_OPEN:
        stop = (
            ""
            if core.fixed_initial_stop is None
            else f"初始防守價：{_number(core.fixed_initial_stop)} 元\n"
        )
        return (
            "0050 趨勢策略｜觸發進場\n"
            f"{common}\n"
            f"進場參考價：{_number(core.signal_reference_price)} 元"
            f"\n{stop}"
            "策略安排：下一交易日開盤進場"
            f"{correction_text}{transition_text}"
        )
    if core.decision is Decision.SELL_NEXT_OPEN:
        return (
            "0050 趨勢策略｜觸發出場\n"
            f"{common}\n"
            f"出場參考價：{_number(core.signal_reference_price)} 元\n"
            f"出場原因：{_exit_reason_text(core.exit_reason)}\n"
            "策略安排：下一交易日開盤出場"
            f"{correction_text}{transition_text}"
        )
    if core.decision is Decision.HOLD:
        title = (
            "訊號更正"
            if correction_of is not None
            else "持有觀察" if core.active_position else "空手等待"
        )
        stop = (
            ""
            if core.fixed_initial_stop is None
            else f"\n初始防守價：{_number(core.fixed_initial_stop)} 元"
        )
        return (
            f"0050 趨勢策略｜{title}\n"
            f"{common}\n"
            f"策略狀態：{_hold_text(core)}"
            f"{stop}\n"
            "策略安排：本次沒有新增進出場安排"
            f"{correction_text}{transition_text}"
        )
    return (
        "0050 趨勢策略｜資料異常\n"
        f"{common}\n"
        "狀態：資料不足，暫停產生正式策略訊號\n"
        "處理：不產生進場、出場或持有判讀；內部原因已寫入稽核紀錄。"
    )

def render_data_unavailable(*, phase: MonitorPhase, trading_date: str, reason_code: str) -> str:
    del reason_code  # Detailed diagnostics remain in the durable audit payload.
    status = (
        "資料不足，本次無法形成盤中預覽"
        if phase is MonitorPhase.PREVIEW
        else "資料不足，暫停產生正式策略訊號"
    )
    return (
        "0050 趨勢策略｜資料異常\n"
        f"訊息性質：{_phase_text(phase)}\n"
        f"檢查日期：{trading_date}\n"
        "現價：無可用報價\n"
        "價格時間：未能驗證\n"
        "預定資料來源：臺灣證券交易所（官方資料）\n"
        f"狀態：{status}\n"
        "處理：不產生進場、出場或持有判讀，不改用其他資料來源；"
        "內部原因已寫入稽核紀錄。"
    )


def render_market_closed(*, trading_date: str) -> str:
    return (
        "0050 趨勢策略｜今日休市\n"
        f"日期：{trading_date}\n"
        "狀態：證交所公告休市，今天不進行策略判讀\n"
        "策略安排：不新增進出場訊號，既有策略紀錄維持不變"
    )


def render_offline_test(decision: Decision) -> str:
    case = {
        Decision.BUY_NEXT_OPEN: "進場情境",
        Decision.SELL_NEXT_OPEN: "出場情境",
        Decision.HOLD: "持有或空手情境",
        Decision.DATA_UNAVAILABLE: "資料異常情境",
    }[decision]
    return (
        "模擬測試｜非真實交易訊號\n"
        "0050 趨勢策略｜離線驗證\n"
        f"案例：{case}\n"
        "此訊息只用於訊息格式與待發佇列測試，不代表市場狀態。"
    )
