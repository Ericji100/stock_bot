from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from .models import AcceptedCoreState, Decision, MarketDataSnapshot, MonitorPhase


def _number(value: Decimal | None, places: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:.{places}f}"


def _decision_text(decision: Decision) -> str:
    return {
        Decision.BUY_NEXT_OPEN: "BUY_NEXT_OPEN｜下一官方交易日開盤觀察買進",
        Decision.SELL_NEXT_OPEN: "SELL_NEXT_OPEN｜下一官方交易日開盤觀察賣出",
        Decision.HOLD: "HOLD｜維持目前策略狀態",
        Decision.DATA_UNAVAILABLE: "DATA_UNAVAILABLE｜資料不足，停止產生交易建議",
    }[decision]


def render_core_message(
    *,
    phase: MonitorPhase,
    core: AcceptedCoreState,
    snapshot: MarketDataSnapshot,
    correction_of: str | None = None,
    pending_transition: Mapping[str, Any] | None = None,
) -> str:
    phase_text = {
        MonitorPhase.PREVIEW: "12:45 盤中預覽（不寫入正式策略狀態）",
        MonitorPhase.DECISION: "13:55 收盤決策",
        MonitorPhase.RECONCILIATION: "22:00 完整資料核對",
        MonitorPhase.TEST: "離線測試",
    }[phase]
    pending = ""
    if core.decision in {Decision.BUY_NEXT_OPEN, Decision.SELL_NEXT_OPEN}:
        pending = "\n成交價：尚未成交，待下一官方交易日開盤；不預填未知價格。"
    correction = ""
    if correction_of is not None:
        correction = (
            f"\n更正：追加更正事件，取代事件 {correction_of}；原紀錄保留。"
        )
    transition_text = ""
    transition = pending_transition or {}
    if transition and transition.get("outcome") != "UNCHANGED":
        transition_text = (
            f"\n待執行指令：{transition.get('old_action', 'UNKNOWN')} → "
            f"{transition.get('new_action', core.decision.value)}；"
            f"處理={transition.get('outcome', 'UNKNOWN')}；"
            f"舊狀態={transition.get('old_status') or '無'}；"
            f"舊 action_id={transition.get('old_action_id') or '無'}；"
            f"leg={transition.get('old_leg_id') or '無'} → "
            f"{transition.get('new_leg_id') or '無'}；"
            f"參考價={transition.get('old_signal_reference_price') or '無'} → "
            f"{transition.get('new_signal_reference_price') or '無'}。"
        )
    provisional = "是" if snapshot.is_provisional else "否"
    return (
        "0050 final-v1.7｜影子監控｜僅供策略觀察，非下單指示\n"
        f"階段：{phase_text}\n"
        f"交易日：{core.as_of.isoformat()}\n"
        f"決策：{_decision_text(core.decision)}\n"
        f"Regime：{core.regime}\n"
        f"收盤/現價：{_number(core.raw_close)}；MA21：{_number(core.sma21)}；"
        f"MA144：{_number(core.sma144)}\n"
        f"MACD histogram：{_number(core.histogram, 6)}；armed："
        f"{'是' if core.macd_armed else '否'}\n"
        f"策略持倉：{'LONG' if core.active_position else 'FLAT'}；固定初始防守："
        f"{_number(core.fixed_initial_stop)}\n"
        f"資料：{snapshot.source}｜{snapshot.price_basis}｜as_of={snapshot.as_of.isoformat()}｜"
        f"snapshot={snapshot.snapshot_id}｜provisional={provisional}"
        f"{pending}{correction}{transition_text}"
    )

def render_data_unavailable(*, phase: MonitorPhase, trading_date: str, reason_code: str) -> str:
    return (
        "0050 final-v1.7｜影子監控｜資料健康警示\n"
        f"階段：{phase.value}；交易日：{trading_date}\n"
        "決策：DATA_UNAVAILABLE｜已 fail-closed，不產生 BUY/SELL/HOLD 推論。\n"
        f"原因代碼：{reason_code}\n"
        "未改用 Yahoo、未回退舊資料源、未送往其他群組。"
    )


def render_offline_test(decision: Decision) -> str:
    return (
        "測試訊息｜非即時交易建議\n"
        "0050 final-v1.7｜離線驗證\n"
        f"案例：{decision.value}\n"
        "此訊息僅供 mock/outbox 測試，不代表市場狀態。"
    )
