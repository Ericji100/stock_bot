from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from trade_monitor.analysis_contract import DISCLAIMER


SNAPSHOT = "SNAPSHOT"
OBSERVATION = "OBSERVATION"
ARMED = "ARMED"
ENTRY = "ENTRY"
MANAGEMENT = "MANAGEMENT"
STOP = "STOP"
EXIT = "EXIT"
UNCHANGED = "UNCHANGED"


@dataclass(frozen=True)
class RenderedReplayMessage:
    kind: str
    title: str
    body: str


_STRATEGY_ZH = {
    "NONE": "尚無主控戰法",
    "OPENING_RANGE_BREAKOUT_RETEST": "開盤區間突破回踩",
    "TREND_PULLBACK_CONTINUATION": "趨勢拉回延續",
    "INTRADAY_COMPRESSION_BREAKOUT": "盤中壓縮突破",
    "FALSE_BREAK_REVERSAL": "假突破／假跌破反轉",
    "QUADRANT_Q1_MOMENTUM_BREAKOUT": "第一象限動能突破",
    "QUADRANT_Q2_COUNTER_PULLBACK": "第二象限逆勢拉回",
    "QUADRANT_Q3_QUALIFIED_BOX": "第三象限合格箱型",
    "QUADRANT_Q4_TREND_ZONE": "第四象限順勢區域",
    "BOX_QUALIFIED_BOUNDARY_BREAK": "合格箱型邊界突破",
    "BOX_FALSE_BREAK_REVERSAL": "箱型假突破反轉",
    "DOW_SECONDARY_RETEST": "道氏次高／次低回測",
    "DOW_TYPE2_REVERSAL": "道氏第二類反轉",
    "DOW_TYPE3_REVERSAL": "道氏第三類跨級急轉",
    "LEFT_LEFT_REVERSAL": "左左反轉觀察",
    "LEFT_RIGHT_REVERSAL": "左右反轉觀察",
    "RIGHT_LEFT_RETEST": "右左回測",
    "RIGHT_RIGHT_CONTINUATION": "右右延續",
    "TAIJI_COPY_AFTER_CORRECTION": "太極修正後複製",
    "TAIJI_REANCHOR_AFTER_FAILURE": "太極失敗後重新定錨",
    "YIZHI_CENTRIFUGAL": "一之戰法離心力",
    "YIZHI_DRAGON_EARLY": "一之戰法一條龍初段",
    "YIZHI_LIFE_DEATH_GATE": "一之戰法生死門",
}

_SETUP_STAGE_ZH = {
    "NONE": "觀望",
    "FORMING": "形成中",
    "ARMED": "待觸發",
    "ENTRY_ELIGIBLE": "可進場",
    "AGGRESSIVE_CONFIRMED": "積極確認",
    "CONSERVATIVE_CONFIRMED": "保守確認",
    "INVALIDATED": "已失效",
    "NO_CHASE": "不宜追價",
}

_CCLASS_MODE_ZH = {
    "TAIJI_ORDERED": "太極有序",
    "YIZHI_MOMENTUM": "一之動能",
    "UNORDERED": "無序",
    "RESETTING": "重置中",
    "UNDEFINED": "未定",
}

_ENTRY_REJECTION_ZH = {
    "STOP_TOO_WIDE": "結構停損過寬",
    "HARD_OBSTACLE_TOO_CLOSE": "硬障礙太近",
    "GRADE_CONFLICT": "大小級數衝突",
    "CONSTITUTION_BLOCKED": "交易憲法禁止",
}

_X_STAGE_ZH = {
    "PREOPEN_CONTEXT": "盤前脈絡",
    "OPENING_EVIDENCE": "開盤證據",
    "FIRST_ENDPOINT": "第一次端點",
    "ANCHOR_LENS_SELECTION": "定錨／主鏡頭",
    "OPPORTUNITY_GRADING": "機會分級",
    "ENTRY_EXECUTION": "進場執行",
    "POSITION_MANAGEMENT": "持倉管理",
    "RESET": "重置",
}

_EXECUTION_STYLE_ZH = {
    "NOT_APPLICABLE": "尚未選定",
    "STANDARD_STRUCTURAL": "標準結構式",
    "MOMENTUM_STRIKE": "動能快速失效式",
    "WOODPECKER": "啄木鳥式",
    "ONE_ROUND": "受限一回合式",
}

_DIRECTION_ZH = {
    "BULL": "多方",
    "BEAR": "空方",
    "LONG": "多方",
    "SHORT": "空方",
    "RANGE": "盤整",
    "UNDEFINED": "未定",
    None: "未定",
}

_QUALITY_ZH = {
    "CLEAN": "乾淨",
    "CAUTION": "有疑慮",
    "UNQUALIFIED": "不合格",
    "UNKNOWN": "品質未定",
    "STRONG": "強",
    "ACCEPTABLE": "合格",
    "WEAK": "偏弱",
    "FAILED": "失敗",
}

_ANCHOR_STATUS_ZH = {
    "FORMING": "形成中",
    "CONFIRMED": "已確認",
    "INVALIDATED": "已失效",
    "REPLACED": "已取代",
}

_PIVOT_STATUS_ZH = {
    "CANDIDATE": "候選",
    "LOCAL_CONFIRMED": "局部成立",
    "PAIRED_CONFIRMED": "配對確認",
    "INVALIDATED": "已失效",
    "REPLACED": "已取代",
}

_QUADRANT_ZH = {
    "Q1": "第一象限",
    "Q2": "第二象限",
    "Q3": "第三象限",
    "Q4": "第四象限",
    "TRANSITION": "象限轉換中",
    "UNDEFINED": "象限未定",
}

_TREND_DYNAMICS_ZH = {
    "INCREASING": "趨勢性增加",
    "DECREASING": "趨勢性減少",
    "UNCLEAR": "趨勢性未定",
}

_VOLATILITY_DYNAMICS_ZH = {
    "EXPANDING": "波動擴張",
    "CONTRACTING": "波動收縮",
    "UNSTABLE": "波動不穩",
    "UNCLEAR": "波動未定",
}

_GRADE_ZH = {"LARGE": "大級", "SMALL": "小級", "UNDEFINED": "控制級數未定"}
_GRADE_RELATION_ZH = {
    "ALIGNED": "大小級同向",
    "CONFLICT": "大小級衝突",
    "ONLY_LARGE": "僅大級有效",
    "ONLY_SMALL": "僅小級有效",
    "UNDEFINED": "大小級關係未定",
}

_REVERSAL_ZH = {
    "NONE": "無正式反轉",
    "TYPE_1": "第一類反轉警告",
    "TYPE_2": "第二類反轉確認",
    "TYPE_3": "第三類跨級急轉",
}
_MATURITY_ZH = {
    "NONE": "尚未進入左右反轉流程",
    "LEFT_LEFT": "左左",
    "LEFT_RIGHT": "左右",
    "RIGHT_LEFT": "右左",
    "RIGHT_RIGHT": "右右",
}

_PROCESS_STAGE_ZH = {
    "UNDEFINED": "流程未定",
    "SESSION_OPEN_PENDING": "等待開盤證據",
    "OPENING_EVIDENCE": "開盤證據建立中",
    "FIRST_ENDPOINT_SAMPLE": "第一次日內端點樣本",
    "STRUCTURE_BUILDING": "結構建立中",
    "SETUP_EVALUATION": "盤中交易機會持續掃描",
    "LATE_OR_RESETTING": "末段或重新定錨",
}

_LENS_ZH = {
    "UNDEFINED": "主鏡頭未定",
    "OPENING_EVIDENCE_ONLY": "開盤證據",
    "TAIJI_PRIMARY": "太極",
    "QUADRANT_PRIMARY": "四象限",
    "COMBINED_CONFIRMATION": "太極＋四象限共同確認",
    "YIZHI_OVERRIDE": "一之戰法動能優先",
}

_FAMILY_DNA_ZH = {
    "CONSISTENT": "家族結構一致",
    "CHANGING": "家族結構改變中",
    "BROKEN": "家族結構已破壞",
    "UNAVAILABLE": "家族結構未定",
}

_CONFLUENCE_ZH = {
    "SAME_QUADRANT": "同象限共振",
    "SMALL_Q1": "小級第一象限共振",
    "COPY_CORRECTION": "複製／修正共振",
}

_COURSE_GRADE_ZH = {
    "A_CANDIDATE": "A級點候選",
    "B_CANDIDATE": "B級點候選",
    "C_CANDIDATE": "C級點候選",
    "OBSERVE": "僅觀察",
    "UNDEFINED": "機會等級未定",
}
_EVIDENCE_ZH = {"HIGH": "高", "MEDIUM": "中", "LOW": "低", "UNAVAILABLE": "未定"}

_ENGINE_ZH = {
    "TAIJI_ORDERED": "太極有序模式",
    "YIZHI_MOMENTUM": "一之戰法動能模式",
    "UNORDERED": "市場無序",
    "RESETTING": "重新定錨中",
    "UNDEFINED": "模式未定",
}

_ORDER_ZH = {
    "ORDERED": "有序",
    "UNORDERED": "無序",
    "TRANSITION": "秩序轉換中",
    "UNDEFINED": "秩序未定",
}

_TAIJI_SEQUENCE_ZH = {
    "NONE": "尚未建立段序",
    "ANCHOR_1": "第一段定錨",
    "CORRECTION_2": "第二段修正",
    "COPY_3": "第三段複製",
    "CORRECTION_4": "第四段修正",
    "COPY_5": "第五段複製",
    "POST_5": "五段後延伸／重置觀察",
}

_ANCHOR_TIME_ZH = {
    "FRESH": "定錨新鮮",
    "DECAYING": "定錨效力衰退",
    "STALE": "定錨過期",
    "UNDEFINED": "定錨時間效力未定",
}

_PREVIOUS_CONTEXT_ZH = {
    "ALIGNED": "前世今生同向",
    "OPPOSED": "前世今生反向",
    "NEUTRAL": "前世背景中性",
    "UNDEFINED": "前世背景未定",
}

_MOMENTUM_STAGE_ZH = {
    "NONE": "尚無一之動能事件",
    "CENTRIFUGAL_FORMING": "離心力形成中",
    "CENTRIFUGAL_CONFIRMED": "離心力已確認",
    "DRAGON_EARLY": "一條龍初段",
    "DRAGON_MIDDLE": "一條龍中段",
    "DRAGON_LATE": "一條龍末段",
    "LIFE_DEATH_GATE_FORMING": "生死門形成中",
    "LIFE_DEATH_GATE_ARMED": "生死門待觸發",
    "EXHAUSTION_WARNING": "動能耗竭警告",
    "FAILED": "一之動能失效",
}

_LOCATION_ZH = {
    "SESSION_EXTERNAL": "日外",
    "SESSION_INTERNAL": "日內",
    "BOUNDARY": "日內端點邊界",
    "UNDEFINED": "位置未定",
}

_GAP_DIRECTION_ZH = {"BULL": "向上", "BEAR": "向下", "FLAT": "近乎平開", "UNDEFINED": "方向未定"}
_GAP_SIZE_ZH = {"SMALL": "偏小", "MODERATE": "適中", "LARGE": "偏大", "EXHAUSTED": "可能耗盡", "UNDEFINED": "幅度未定"}
_OPENING_RELATION_ZH = {"SAME": "與跳空同向", "OPPOSITE": "反向推翻跳空", "MIXED": "同反向混合", "UNDEFINED": "關係未定"}
_OPENING_QUALITY_ZH = {"CLEAN": "乾淨", "MIXED": "混合", "NOISY": "雜訊偏多", "OVERHEATED": "可能過熱", "UNAVAILABLE": "不可得"}
_ENDPOINT_STYLE_ZH = {"PENDING": "等待確認", "TRUE_LIKE": "偏真突破", "FALSE_LIKE": "偏假突破", "MIXED": "真假混合", "UNAVAILABLE": "不可判定"}

_HYPOTHESIS_STATUS_ZH = {
    "POTENTIAL": "潛在",
    "STRENGTHENING": "增強中",
    "NEAR_CONFIRMATION": "接近確認",
    "CONFIRMED": "已確認",
    "DEGRADED": "已降級",
    "CANCELLED": "已取消",
    "UNDEFINED": "未定",
}
_CONFIDENCE_ZH = {"HIGH": "高", "MEDIUM": "中", "LOW": "低", "UNAVAILABLE": "未定"}


def render_replay_event_card(
    payload: Mapping[str, Any],
    context: Mapping[str, Any],
    *,
    stage: str,
    constitution_snapshot: Mapping[str, Any] | None = None,
) -> RenderedReplayMessage:
    """Select material facts from one validated analysis; never re-analyze it."""

    kind = classify_replay_message_kind(payload, stage=stage, constitution_snapshot=constitution_snapshot)
    title = _event_title(payload, kind)
    latest = _clean_text(context.get("expected_latest_closed_k_hhmm") or _as_hhmm(context.get("expected_latest_closed_k_iso")))
    price = _clean_text(payload.get("latest_closed_k_price_estimate") or "點位未定")

    if kind == UNCHANGED:
        lines = [f"**{latest}｜{title}**", f"最新已收盤K：{latest}；{price}"]
        lines.extend(_quiet_summary(payload))
        lines.append(DISCLAIMER)
        return RenderedReplayMessage(kind=kind, title=title, body="\n".join(lines).rstrip() + "\n")

    output: list[str] = [f"**{latest}｜{title}**", f"最新已收盤K：{latest}；{price}", ""]
    output.extend(_section("盤勢摘要", _trend_lines(payload)))
    state = _state(payload)
    structure_lines = _structure_lines(state, compact=kind in {MANAGEMENT, STOP, EXIT})
    if structure_lines:
        output.extend(_section("定錨／樞紐／道氏防線", structure_lines))
    if kind not in {STOP, EXIT}:
        reading_lines = _reading_lines(payload, state, snapshot=kind == SNAPSHOT)
        if reading_lines:
            output.extend(_section("主判讀與戰法", reading_lines))
    if kind in {ENTRY, MANAGEMENT, STOP, EXIT}:
        position_lines = _position_lines(payload, kind=kind, constitution_snapshot=constitution_snapshot)
        if position_lines:
            heading = "進場與風險" if kind == ENTRY else "持倉管理" if kind == MANAGEMENT else "出場"
            output.extend(_section(heading, position_lines))
    scenario_lines = _scenario_action_lines(payload, state, kind=kind)
    if scenario_lines:
        output.extend(_section("情境與操作", scenario_lines))
    output.append(DISCLAIMER)
    return RenderedReplayMessage(kind=kind, title=title, body="\n".join(output).rstrip() + "\n")


def render_semantic_replay_event_card(
    payload: Mapping[str, Any],
    context: Mapping[str, Any],
    *,
    stage: str,
    ledger: Mapping[str, Any],
    position_before: Mapping[str, Any],
    position_after: Mapping[str, Any],
    previous_memory: Mapping[str, Any] | None = None,
) -> RenderedReplayMessage:
    """Render the replay-only semantic contract from program-owned evidence."""

    if payload.get("message_type"):
        return _render_semantic_v3_event_card(
            payload,
            context,
            stage=stage,
            ledger=ledger,
            position_before=position_before,
            position_after=position_after,
            previous_memory=previous_memory,
        )

    action = payload.get("action") if isinstance(payload.get("action"), Mapping) else {}
    reading = payload.get("course_reading") if isinstance(payload.get("course_reading"), Mapping) else {}
    event = str(action.get("position_action") or "NONE")
    if stage == "preopen":
        kind, title = SNAPSHOT, "盤前完整快照"
    elif event == "ENTER":
        kind, title = ENTRY, "模擬進場"
    elif event == "STOP":
        kind, title = STOP, "停損出場"
    elif event == "EXIT":
        kind, title = EXIT, "條件失效出場"
    elif position_before.get("status") in {"LONG", "SHORT"}:
        kind, title = MANAGEMENT, "持倉管理"
    elif payload.get("original_decision") == "DONT_NOTIFY":
        kind, title = UNCHANGED, "條件不變，繼續觀望"
    elif reading.get("setup_stage") in {"ARMED", "AGGRESSIVE_CONFIRMED", "CONSERVATIVE_CONFIRMED"}:
        kind, title = ARMED, "交易條件已準備"
    elif reading.get("setup_stage") == "INVALIDATED":
        kind, title = OBSERVATION, "候選已失效"
    elif reading.get("setup_stage") == "NO_CHASE":
        kind, title = OBSERVATION, "方向仍在，但不追價"
    else:
        kind, title = OBSERVATION, "盤勢結構更新"

    latest = _clean_text(context.get("expected_latest_closed_k_hhmm") or _as_hhmm(context.get("expected_latest_closed_k_iso")))
    price = _clean_text(payload.get("latest_closed_k_price_estimate") or "點位未定")
    if kind == UNCHANGED:
        scenario = payload.get("scenario") if isinstance(payload.get("scenario"), Mapping) else {}
        lines = [
            f"**{latest}｜{title}**",
            f"最新已收盤K：{latest}；{price}",
            f"大結構：{_semantic_trend(payload.get('large_trend'))}｜小結構：{_semantic_trend(payload.get('current_trend'))}｜{_QUADRANT_ZH.get(str(reading.get('quadrant')), '象限未定')}",
            f"主控：{_clean_text(reading.get('main_strategy') or '尚無主控戰法')}；{_public_reason(payload.get('notification_reason'))}",
            f"情境權重：多方{scenario.get('bull_probability', '?')}%｜盤整{scenario.get('range_probability', '?')}%｜空方{scenario.get('bear_probability', '?')}%",
            f"下一步：{_clean_text(action.get('trigger') or '等待新結構。')}",
            DISCLAIMER,
        ]
        return RenderedReplayMessage(kind=kind, title=title, body="\n".join(lines).rstrip() + "\n")

    output = [f"**{latest}｜{title}**", f"最新已收盤K：{latest}；{price}", ""]
    output.extend(
        _section(
            "盤勢摘要",
            [
                f"大結構：{_semantic_trend(payload.get('large_trend'), detail=True)}",
                f"小結構：{_semantic_trend(payload.get('current_trend'), detail=True)}",
                *[_clean_text(item) for item in payload.get("market_summary", []) if _clean_text(item)][:2],
            ],
        )
    )
    structure = _semantic_structure_lines(reading, ledger)
    if structure:
        output.extend(_section("定錨／樞紐／道氏防線", structure))
    output.extend(
        _section(
            "主判讀與戰法",
            _nonempty(
                [
                    f"主鏡頭：{_clean_text(reading.get('primary_lens'))}；{_QUADRANT_ZH.get(str(reading.get('quadrant')), '象限未定')}",
                    f"太極：{_clean_text(reading.get('taiji'))}",
                    f"一之：{_clean_text(reading.get('yizhi'))}",
                    f"左右：{_clean_text(reading.get('left_right'))}",
                    f"道氏：{_clean_text(reading.get('dow'))}",
                    f"主控戰法：{_clean_text(reading.get('main_strategy'))}｜{_SETUP_STAGE_ZH.get(str(reading.get('setup_stage')), '狀態未定')}",
                    *[_clean_text(item) for item in reading.get("strategy_reason", []) if _clean_text(item)][:3],
                ]
            ),
        )
    )
    scenario = payload.get("scenario") if isinstance(payload.get("scenario"), Mapping) else {}
    output.extend(
        _section(
            "情境與操作",
            _nonempty(
                [
                    f"情境權重：多方{scenario.get('bull_probability', '?')}%｜盤整{scenario.get('range_probability', '?')}%｜空方{scenario.get('bear_probability', '?')}%",
                    f"主要情境：{_clean_text(scenario.get('primary'))}",
                    f"備用情境：{_clean_text(scenario.get('alternative'))}",
                    f"觀察區：{_clean_text(action.get('observation_area'))}",
                    f"觸發：{_clean_text(action.get('trigger'))}",
                    f"進場：{_clean_text(action.get('entry'))}",
                    f"結構停損：{_clean_text(action.get('structural_stop'))}",
                    f"最近障礙：{_clean_text(action.get('nearest_obstacle'))}",
                    f"應有行為／等待：{_clean_text(action.get('expected_behavior'))}；{_clean_text(action.get('max_wait'))}",
                    f"不追價：{_clean_text(action.get('no_chase'))}",
                    f"管理：{_clean_text(action.get('management'))}",
                    f"改變看法：{_clean_text(scenario.get('view_change'))}",
                ]
            ),
        )
    )
    if kind in {ENTRY, MANAGEMENT, STOP, EXIT}:
        output.extend(_section("模擬持倉", _semantic_position_lines(position_before, position_after, action)))
    output.append(DISCLAIMER)
    return RenderedReplayMessage(kind=kind, title=title, body="\n".join(output).rstrip() + "\n")


def _render_semantic_v3_event_card(
    payload: Mapping[str, Any],
    context: Mapping[str, Any],
    *,
    stage: str,
    ledger: Mapping[str, Any],
    position_before: Mapping[str, Any],
    position_after: Mapping[str, Any],
    previous_memory: Mapping[str, Any] | None = None,
) -> RenderedReplayMessage:
    """Render one short, event-specific card; detailed state stays in JSON artifacts."""

    reading = payload.get("course_reading") if isinstance(payload.get("course_reading"), Mapping) else {}
    scenario = payload.get("scenario") if isinstance(payload.get("scenario"), Mapping) else {}
    action = payload.get("action") if isinstance(payload.get("action"), Mapping) else {}
    ledger = _v3_presentation_ledger(
        ledger,
        action=action,
        position_before=position_before,
        position_after=position_after,
    )
    message_type = "SNAPSHOT" if stage == "preopen" else str(payload.get("message_type") or "OBSERVATION")
    kind = {
        "SNAPSHOT": SNAPSHOT,
        "OBSERVATION": OBSERVATION,
        "PREPARATION": ARMED,
        "ENTRY": ENTRY,
        "REENTRY": "REENTRY",
        "MANAGEMENT": MANAGEMENT,
        "STOP": STOP,
        "EXIT": EXIT,
        "STRUCTURE_UPGRADE": "STRUCTURE_UPGRADE",
        "STRUCTURE_DOWNGRADE": "STRUCTURE_DOWNGRADE",
        "INVALIDATION": "INVALIDATION",
        "UNCHANGED": UNCHANGED,
    }.get(message_type, OBSERVATION)
    title = _v3_event_title(
        payload,
        message_type,
        ledger=ledger,
        previous_memory=previous_memory,
        position_before=position_before,
    )
    latest = _clean_text(context.get("expected_latest_closed_k_hhmm") or _as_hhmm(context.get("expected_latest_closed_k_iso")))
    lines = [f"**{latest}｜{title}**"]

    if message_type == "UNCHANGED":
        lines.extend(
            [
                _v3_public_text(payload.get("notification_reason"), ledger, 100),
                _v3_quadrant_line(reading, ledger),
                _v3_weight_line(scenario),
                f"下一步：{_v3_public_text(action.get('trigger'), ledger, 120)}",
                DISCLAIMER,
            ]
        )
        return RenderedReplayMessage(kind=kind, title=title, body="\n".join(_nonempty(lines)).rstrip() + "\n")

    event_line = _v3_structure_event_line(reading, ledger)
    if event_line:
        lines.append(event_line)
    if message_type != "STRUCTURE_UPGRADE":
        lines.append(_v3_structure_summary(payload, reading, ledger=ledger))

    structure_lines = _v3_structure_lines(reading, ledger, message_type=message_type)
    lines.extend(structure_lines)
    lines.append(_v3_quadrant_line(reading, ledger))

    method_lines = _v3_course_method_lines(reading, ledger)
    if message_type == "STRUCTURE_UPGRADE" and "x_process" not in reading:
        method_lines = []
    if message_type == "PREPARATION" and "x_process" not in reading:
        method_lines = method_lines[:1]
    lines.extend(method_lines)
    main_strategy = _v3_public_text(reading.get("main_strategy"), ledger, 110)
    if main_strategy:
        stage_text = _SETUP_STAGE_ZH.get(str(reading.get("setup_stage")), "狀態未定")
        if (
            message_type == "PREPARATION"
            and reading.get("setup_stage") == "ENTRY_ELIGIBLE"
            and action.get("position_action") == "NONE"
            and action.get("entry_rejection_reason") not in {None, "", "NONE"}
        ):
            stage_text = "本次已否決"
        lines.append(f"主控：{main_strategy}｜{stage_text}")

    if message_type in {"ENTRY", "REENTRY", "MANAGEMENT", "STOP", "EXIT"}:
        lines.extend(
            _v3_position_lines(
                message_type,
                position_before,
                position_after,
                action,
                ledger=ledger,
            )
        )

    if message_type == "PREPARATION":
        entry_decision = ""
        if reading.get("setup_stage") == "ENTRY_ELIGIBLE":
            if action.get("position_action") == "ENTER":
                entry_decision = "決策：接受訊號；下一根1分K第一個可成交價建立模擬倉。"
            else:
                reason = _ENTRY_REJECTION_ZH.get(str(action.get("entry_rejection_reason")), "否決原因未定")
                entry_decision = f"決策：不進場｜{reason}。"
        lines.extend(
            _nonempty(
                [
                    (
                        f"準備：{_v3_public_clause(action.get('observation_area'), ledger, 65)}；"
                        f"{_v3_public_clause(action.get('trigger'), ledger, 85)}"
                    ),
                    f"停損：{_v3_public_clause(action.get('structural_stop'), ledger, 90)}",
                    _v3_obstacle_line(action, ledger),
                    _v3_expected_line(action, ledger),
                    entry_decision,
                ]
            )
        )
    elif message_type in {"ENTRY", "REENTRY"}:
        lines.extend(
            _nonempty(
                [
                    f"操作：{_v3_public_text(action.get('entry'), ledger, 100)}",
                    f"觀察／觸發：{_v3_public_text(action.get('observation_area'), ledger, 90)}；{_v3_public_text(action.get('trigger'), ledger, 110)}",
                    f"停損：{_v3_public_text(action.get('structural_stop'), ledger, 120)}",
                    _v3_obstacle_line(action, ledger),
                    _v3_expected_line(action, ledger),
                ]
            )
        )
    elif message_type in {"STOP", "EXIT", "INVALIDATION"}:
        lines.append(f"後續：{_v3_public_text(action.get('management'), ledger, 130)}")
        reentry_line = _v3_reentry_line(action, ledger)
        if reentry_line:
            lines.append(reentry_line)
    else:
        compact_structure_event = message_type in {"STRUCTURE_UPGRADE", "STRUCTURE_DOWNGRADE"}
        plan_limit = 80 if compact_structure_event else 120
        operation_source = action.get("entry") if compact_structure_event else action.get("management") or action.get("trigger")
        lines.extend(
            _nonempty(
                [
                    _v3_weight_line(scenario),
                    f"多方：{_v3_public_clause(scenario.get('bull_plan'), ledger, plan_limit)}",
                    f"空方：{_v3_public_clause(scenario.get('bear_plan'), ledger, plan_limit)}",
                    f"操作：{_v3_public_clause(operation_source, ledger, 90 if compact_structure_event else 130)}",
                    (
                        f"改變看法：{_v3_public_clause(scenario.get('view_change'), ledger, 80)}"
                        if compact_structure_event
                        else ""
                    ),
                ]
            )
        )
    if message_type in {"PREPARATION", "ENTRY", "REENTRY", "MANAGEMENT", "STOP", "EXIT"}:
        lines.append(_v3_weight_line(scenario))
    lines.append(DISCLAIMER)
    return RenderedReplayMessage(kind=kind, title=title, body="\n".join(_nonempty(lines)).rstrip() + "\n")


def _v3_event_title(
    payload: Mapping[str, Any],
    message_type: str,
    *,
    ledger: Mapping[str, Any] | None = None,
    previous_memory: Mapping[str, Any] | None = None,
    position_before: Mapping[str, Any] | None = None,
) -> str:
    direction = str(payload.get("message_direction") or "NEUTRAL")
    reading = payload.get("course_reading")
    reading = reading if isinstance(reading, Mapping) else {}
    action = payload.get("action")
    action = action if isinstance(action, Mapping) else {}
    event = None
    if isinstance(ledger, Mapping):
        event = _by_id(ledger.get("structure_events")).get(reading.get("structure_event_ref"))
    event_direction = str(event.get("direction") or "") if isinstance(event, Mapping) else ""
    position_direction = (
        {"LONG": "BULL", "SHORT": "BEAR"}.get(str(position_before.get("status")))
        if isinstance(position_before, Mapping)
        else None
    )
    if message_type in {"MANAGEMENT", "STOP", "EXIT"} and position_direction:
        direction = position_direction
    elif event_direction in {"BULL", "BEAR"}:
        direction = event_direction
    elif message_type == "INVALIDATION" and isinstance(previous_memory, Mapping):
        setup_key = action.get("setup_key")
        prior_setups = previous_memory.get("active_setups")
        setup_items = prior_setups if isinstance(prior_setups, list) else []
        invalidated = next(
            (
                item
                for item in setup_items
                if isinstance(item, Mapping) and item.get("setup_key") == setup_key
            ),
            None,
        )
        if not isinstance(invalidated, Mapping) and setup_key is None:
            actionable = [
                item
                for item in setup_items
                if isinstance(item, Mapping)
                and item.get("stage") in {
                    "ARMED", "ENTRY_ELIGIBLE", "AGGRESSIVE_CONFIRMED", "CONSERVATIVE_CONFIRMED",
                }
            ]
            if len(actionable) == 1:
                # A disappearing sole actionable setup is the invalidated
                # subject even when the analyzer omitted action.setup_key.
                # The current market direction may already be opposite and
                # must not relabel which candidate actually failed.
                invalidated = actionable[0]
        prior_direction = invalidated.get("direction") if isinstance(invalidated, Mapping) else None
        direction = {"LONG": "BULL", "SHORT": "BEAR"}.get(str(prior_direction), direction)
    side = {"BULL": "多方", "BEAR": "空方", "NEUTRAL": ""}.get(direction, "")
    entry_denied = (
        message_type == "PREPARATION"
        and reading.get("setup_stage") == "ENTRY_ELIGIBLE"
        and action.get("position_action") == "NONE"
        and action.get("entry_rejection_reason") not in {None, "", "NONE"}
    )
    label = {
        "SNAPSHOT": "盤前快照",
        "OBSERVATION": "觀察",
        "PREPARATION": (
            "進場取消"
            if entry_denied
            else "進場資格"
            if reading.get("setup_stage") == "ENTRY_ELIGIBLE"
            else "準備"
        ),
        "ENTRY": "模擬進場",
        "REENTRY": "模擬再進場",
        "MANAGEMENT": "持倉管理",
        "STOP": "停損出場",
        "EXIT": "條件出場",
        "STRUCTURE_UPGRADE": "結構升級",
        "STRUCTURE_DOWNGRADE": "結構降級",
        "INVALIDATION": "候選失效",
        "UNCHANGED": "條件不變",
    }.get(message_type, "盤勢更新")
    if message_type == "STRUCTURE_DOWNGRADE" and event_direction in {"BULL", "BEAR"}:
        label = "大級結構降級"
    elif message_type == "INVALIDATION" and isinstance(event, Mapping) and event.get("event_type") == "STRUCTURE_INVALIDATED":
        label = "升級結構失效"
    icon = {
        "SNAPSHOT": "📋",
        "OBSERVATION": "🟦",
        "PREPARATION": "🟡",
        "ENTRY": "🟢",
        "REENTRY": "🟢",
        "MANAGEMENT": "🟠",
        "STOP": "🔴",
        "EXIT": "🔴",
        "STRUCTURE_UPGRADE": "🔄",
        "STRUCTURE_DOWNGRADE": "🔄",
        "INVALIDATION": "⚠️",
        "UNCHANGED": "⚪",
    }.get(message_type, "🟦")
    titled = f"{side}{label}" if side and not label.startswith(side) else label
    return f"{icon} {titled}"


def _v3_structure_lines(
    reading: Mapping[str, Any],
    ledger: Mapping[str, Any],
    *,
    message_type: str,
) -> list[str]:
    if _v3_is_ai_hybrid(ledger):
        return _v3_ai_hybrid_structure_lines(reading, ledger)
    lifecycle = ledger.get("anchor_lifecycle")
    if isinstance(lifecycle, Mapping):
        return _anchor_lifecycle_lines(lifecycle)
    legs = _by_id(ledger.get("legs"))
    defenses = _by_id(ledger.get("defenses"))
    result: list[str] = []
    requested = [
        ("large_anchor_ref", "大錨"),
        ("working_anchor_ref", "工作錨"),
    ]
    seen: set[str] = set()
    for key, label in requested:
        reference = reading.get(key)
        leg = legs.get(reference)
        if not isinstance(leg, Mapping) or str(reference) in seen:
            continue
        seen.add(str(reference))
        result.append(_v3_leg_line(label, leg))
    defense_parts: list[str] = []
    for key, label in (("large_defense_ref", "大級"), ("small_defense_ref", "小級")):
        defense = defenses.get(reading.get(key))
        if isinstance(defense, Mapping):
            defense_parts.append(f"{label}{_v3_defense_ref(defense)}")
    if defense_parts:
        result.append("道氏防線：" + "｜".join(defense_parts))
    return result[:3]


def _v3_is_ai_hybrid(ledger: Mapping[str, Any] | None) -> bool:
    policy = ledger.get("program_trade_policy") if isinstance(ledger, Mapping) else None
    return (
        isinstance(policy, Mapping)
        and policy.get("actionable_setups") == "PROGRAM_CANDIDATES_AI_DECISION"
        and policy.get("decision_authority") == "AI_HYBRID"
    )


def _v3_ai_hybrid_structure_lines(
    reading: Mapping[str, Any], ledger: Mapping[str, Any]
) -> list[str]:
    """Render only the role-specific evidence that the AI actually selected.

    The program owns the record's time and price, but in AI_HYBRID the model
    owns whether a candidate has enough course meaning to appear as the active
    large/small anchor or defense.  Falling back to the whole lifecycle here
    would silently turn the public card back into a program-only judgment.
    """

    records = _by_id(ledger.get("anchor_records"))
    result: list[str] = []
    seen: set[str] = set()
    for key, label in (
        ("large_anchor_ref", "大錨"),
        ("small_anchor_ref", "小錨"),
        ("working_anchor_ref", "目前工作段"),
    ):
        reference = reading.get(key)
        record = records.get(reference)
        if not isinstance(record, Mapping) or str(reference) in seen:
            continue
        seen.add(str(reference))
        result.append(_anchor_role_line(label, record))

    reverse_ref = reading.get("reverse_anchor_candidate_ref")
    reverse = records.get(reverse_ref)
    if isinstance(reverse, Mapping) and str(reverse_ref) not in seen:
        direction = str(reverse.get("direction") or "")
        signed_amplitude = float(reverse.get("amplitude_points") or 0)
        signed_amplitude = -abs(signed_amplitude) if direction == "BEAR" else abs(signed_amplitude)
        status = {
            "QUALIFIED": "候選成立、尚未接管",
            "DEFENSE_BREAK": "已破防線、等待確認接管",
            "AWAITING_CONTINUATION": "防線已收復、等待反向再延續",
        }.get(str(reverse.get("status")), "形成中")
        result.append(
            "反向候選："
            f"{_v3_bar_level_ref(reverse.get('start_time'), 'HIGH' if direction == 'BEAR' else 'LOW', reverse.get('start_price'))}→"
            f"{_v3_bar_level_ref(reverse.get('current_extreme_time'), 'LOW' if direction == 'BEAR' else 'HIGH', reverse.get('current_extreme_price'))}"
            f"（{signed_amplitude:+,.0f}點／{int(reverse.get('duration_minutes') or 0)}分，{status}）"
        )

    defense_parts: list[str] = []
    for key, grade in (("large_defense_ref", "大級"), ("small_defense_ref", "小級")):
        defense = _v3_defense_record(ledger, reading.get(key))
        if not isinstance(defense, Mapping):
            continue
        side = "多防" if defense.get("direction") == "BULL" else "空防"
        state = str(defense.get("state") or "ACTIVE")
        if state == "ACTIVE":
            status = "作用中"
        elif defense.get("broken_at"):
            status = f"{_as_hhmm(defense.get('broken_at'))}失守"
        else:
            status = "已失效"
        defense_parts.append(f"{grade}{side}{_v3_defense_ref(defense)}（{status}）")
    if defense_parts:
        result.append("道氏防線：" + "｜".join(defense_parts))
    return result


def _v3_defense_record(
    ledger: Mapping[str, Any], reference: Any
) -> Mapping[str, Any] | None:
    if not reference:
        return None
    direct = _by_id(ledger.get("defenses")).get(reference)
    if isinstance(direct, Mapping):
        return direct
    for record in ledger.get("anchor_records") or []:
        if not isinstance(record, Mapping):
            continue
        defense = record.get("defense")
        if isinstance(defense, Mapping) and defense.get("id") == reference:
            return defense
    lifecycle = ledger.get("anchor_lifecycle")
    dow = lifecycle.get("dow_context") if isinstance(lifecycle, Mapping) else None
    if isinstance(dow, Mapping):
        for key in (
            "large_bull_defense",
            "large_bear_defense",
            "small_bull_defense",
            "small_bear_defense",
        ):
            defense = dow.get(key)
            if isinstance(defense, Mapping) and defense.get("id") == reference:
                return defense
    return None


def _anchor_lifecycle_lines(lifecycle: Mapping[str, Any]) -> list[str]:
    result: list[str] = []
    background = lifecycle.get("background_anchor")
    child = lifecycle.get("child_anchor")
    working = lifecycle.get("working_leg")
    candidate = lifecycle.get("reverse_candidate")
    if isinstance(background, Mapping):
        direction = str(background.get("direction") or "")
        start_kind = (
            "OPEN"
            if background.get("anchor_origin_kind") == "SESSION_OPEN"
            else "LOW" if direction == "BULL" else "HIGH"
        )
        end_kind = "HIGH" if direction == "BULL" else "LOW"
        status = {
            "ACTIVE": "作用中",
            "DEGRADED": "防線失守、背景降級",
            "DEGRADED_RECLAIMED": "防線收復、背景仍降級",
        }.get(str(background.get("status")), "狀態未定")
        result.append(
            "大錨："
            f"{_v3_bar_level_ref(background.get('origin_time'), start_kind, background.get('origin_price'))}→"
            f"{_v3_bar_level_ref(background.get('latest_extreme_time'), end_kind, background.get('latest_extreme_price'))}"
            f"（{float(background.get('amplitude_points') or 0):+,.0f}／{int(background.get('duration_minutes') or 0)}分，{status}）"
        )
        defense = background.get("defense")
        if isinstance(defense, Mapping) and not isinstance(lifecycle.get("dow_context"), Mapping):
            defense_kind = "LOW" if direction == "BULL" else "HIGH"
            state = {
                "ACTIVE": "作用中",
                "BROKEN": f"{_as_hhmm(defense.get('broken_at'))}失守",
                "BROKEN_AND_RECLAIMED": (
                    f"{_as_hhmm(defense.get('broken_at'))}失守、"
                    f"{_as_hhmm(defense.get('reclaimed_at'))}收復"
                ),
            }.get(str(defense.get("state")), "狀態未定")
            result.append(
                "大級道氏防線："
                f"{_v3_bar_level_ref(defense.get('time'), defense_kind, defense.get('price'))}（{state}）"
            )
    child_is_candidate = (
        isinstance(child, Mapping)
        and isinstance(candidate, Mapping)
        and child.get("direction") == candidate.get("direction")
        and child.get("origin_time") == candidate.get("start_time")
        and child.get("latest_extreme_time") == candidate.get("current_extreme_time")
    )
    child_is_background = (
        isinstance(child, Mapping)
        and isinstance(background, Mapping)
        and child.get("direction") == background.get("direction")
        and child.get("origin_time") == background.get("origin_time")
        and child.get("latest_extreme_time") == background.get("latest_extreme_time")
        and child.get("latest_extreme_price") == background.get("latest_extreme_price")
    )
    if isinstance(child, Mapping) and not child_is_candidate and not child_is_background:
        result.append(_anchor_role_line("小錨", child))
        defense_candidate = child.get("defense_candidate")
        formal_defense = child.get("defense")
        if (
            isinstance(defense_candidate, Mapping)
            and defense_candidate.get("state") == "CANDIDATE"
            and not isinstance(formal_defense, Mapping)
        ):
            direction = str(child.get("direction") or "")
            side = "空方" if direction == "BEAR" else "多方"
            kind = "HIGH" if direction == "BEAR" else "LOW"
            pending = "尚未創新低" if direction == "BEAR" else "尚未創新高"
            result.append(
                f"{side}防線候選："
                f"{_v3_bar_level_ref(defense_candidate.get('time'), kind, defense_candidate.get('price'))}"
                f"（{pending}）"
            )
    if isinstance(candidate, Mapping):
        status = {
            "QUALIFIED": "候選成立、尚未接管",
            "DEFENSE_BREAK": "已破防線、等待確認接管",
            "AWAITING_CONTINUATION": "防線已收復、等待反向再延續",
        }.get(str(candidate.get("status")), "形成中")
        signed_amplitude = float(candidate.get("amplitude_points") or 0)
        if candidate.get("direction") == "BEAR":
            signed_amplitude = -abs(signed_amplitude)
        else:
            signed_amplitude = abs(signed_amplitude)
        result.append(
            ("小錨／反向候選：" if child_is_candidate else "反向候選：") +
            f"{_v3_bar_level_ref(candidate.get('start_time'), 'HIGH' if candidate.get('direction') == 'BEAR' else 'LOW', candidate.get('start_price'))}→"
            f"{_v3_bar_level_ref(candidate.get('current_extreme_time'), 'LOW' if candidate.get('direction') == 'BEAR' else 'HIGH', candidate.get('current_extreme_price'))}"
            f"（{signed_amplitude:+,.0f}點／{int(candidate.get('duration_minutes') or 0)}分，{status}）"
        )
    dow_context = lifecycle.get("dow_context")
    if isinstance(working, Mapping):
        small_state = str(dow_context.get("small_state") or "") if isinstance(dow_context, Mapping) else None
        reference_direction = small_state if small_state in {"BULL", "BEAR"} else None
        result.append(_anchor_role_line("目前工作段", working, reference_direction=reference_direction))
    if isinstance(dow_context, Mapping):
        result.extend(_course_dow_lines(dow_context))
    return result


def _course_dow_lines(context: Mapping[str, Any]) -> list[str]:
    result: list[str] = []
    for level, label in (("large", "大級道氏"), ("small", "小級道氏")):
        state = {
            "BULL": "偏多",
            "BEAR": "偏空",
            "BEAR_WITH_BULL_REVERSAL": "空方背景／多方反彈",
            "BULL_WITH_BEAR_REVERSAL": "多方背景／空方修正",
            "CONFLICT": "雙向發展中",
            "UNDEFINED": "未成立",
        }.get(str(context.get(f"{level}_state")), "未成立")
        parts = [state]
        for direction, side in (("bull", "多防"), ("bear", "空防")):
            defense = context.get(f"{level}_{direction}_defense")
            if not isinstance(defense, Mapping):
                continue
            kind = "LOW" if direction == "bull" else "HIGH"
            defense_state = str(defense.get("state") or "ACTIVE")
            if defense_state == "ACTIVE":
                status = "作用中"
            elif defense.get("broken_at"):
                status = f"{_as_hhmm(defense.get('broken_at'))}失守"
            else:
                status = "已失效"
            parts.append(
                f"{side}{_v3_bar_level_ref(defense.get('time'), kind, defense.get('price'))}（{status}）"
            )
        result.append(f"{label}：" + "｜".join(parts))
    return result


def _anchor_role_line(
    label: str,
    record: Mapping[str, Any],
    *,
    reference_direction: str | None = None,
) -> str:
    direction = str(record.get("direction") or "")
    start_kind = (
        "OPEN"
        if record.get("anchor_origin_kind") == "SESSION_OPEN"
        else "LOW" if direction == "BULL" else "HIGH"
    )
    end_kind = "HIGH" if direction == "BULL" else "LOW"
    start_time = record.get("origin_time", record.get("start_time"))
    start_price = record.get("origin_price", record.get("start_price"))
    end_time = record.get("latest_extreme_time", record.get("current_extreme_time"))
    end_price = record.get("latest_extreme_price", record.get("current_extreme_price"))
    lifecycle_status = {
        "ACTIVE": "作用中",
        "DEGRADED": "防線失守、背景降級",
        "DEGRADED_RECLAIMED": "防線收復、背景仍降級",
    }.get(str(record.get("status"))) if record.get("record_type") == "ANCHOR" else None
    role = lifecycle_status or {
        "BACKGROUND_CORRECTION": "背景修正",
        "BACKGROUND_RETEST": "同向再測",
        "REVERSE_CANDIDATE_IMPULSE": "反向候選推進",
        "REVERSE_CANDIDATE_PULLBACK": "反向候選修正",
        "UNCLASSIFIED": "尚未定級",
    }.get(str(record.get("role")), "作用中" if record.get("status") != "FORMING" else "形成中")
    if (
        label == "目前工作段"
        and reference_direction in {"BULL", "BEAR"}
        and record.get("direction") in {"BULL", "BEAR"}
    ):
        role = "同向再測" if record.get("direction") == reference_direction else "反向修正"
    duration = (
        f"{_as_hhmm(record.get('qualification_time'))} OR5確認"
        if record.get("qualification") == "OR5_CLOSED"
        else f"{int(record.get('duration_minutes') or 0)}分"
    )
    return (
        f"{label}：{_v3_bar_level_ref(start_time, start_kind, start_price)}→"
        f"{_v3_bar_level_ref(end_time, end_kind, end_price)}"
        f"（{float(record.get('amplitude_points') or 0):+,.0f}／{duration}，{role}）"
    )


def _v3_structure_summary(
    payload: Mapping[str, Any],
    reading: Mapping[str, Any],
    *,
    ledger: Mapping[str, Any] | None = None,
) -> str:
    large = _semantic_trend(payload.get("large_trend"))
    current = _semantic_trend(payload.get("current_trend"))
    return f"結構：大級{large}｜小級{current}"


def _v3_leg_line(label: str, leg: Mapping[str, Any]) -> str:
    duration = int(leg.get("duration_minutes") or 0)
    duration_text = "首根" if duration == 0 else f"{duration}分"
    status = "形成中" if leg.get("status") == "FORMING" else "已確認"
    direction = str(leg.get("direction") or "")
    start_kind = "LOW" if direction == "BULL" else "HIGH" if direction == "BEAR" else None
    end_kind = "HIGH" if direction == "BULL" else "LOW" if direction == "BEAR" else None
    return (
        f"{label}：{_v3_bar_level_ref(leg.get('start_time'), start_kind, leg.get('start_price'))}→"
        f"{_v3_bar_level_ref(leg.get('end_time'), end_kind, leg.get('end_price'))}"
        f"（{float(leg.get('amplitude_points') or 0):+,.0f}／{duration_text}，{status}）"
    )


def _v3_upgrade_line(reading: Mapping[str, Any], ledger: Mapping[str, Any]) -> str:
    return _v3_structure_event_line(reading, ledger)


def _v3_structure_event_line(reading: Mapping[str, Any], ledger: Mapping[str, Any]) -> str:
    events = _by_id(ledger.get("structure_events"))
    pivots = _by_id(ledger.get("pivots"))
    defenses = _by_id(ledger.get("defenses"))
    event = events.get(reading.get("structure_event_ref"))
    if not isinstance(event, Mapping):
        return ""
    event_type = str(event.get("event_type") or "")
    if event_type == "GRADE_DOWNGRADE":
        upgrade = events.get(event.get("source_upgrade_event_id"))
        broken_ref = _v3_event_pivot_ref(
            upgrade,
            "replacement_defense_pivot_id",
            pivots,
            event.get("broken_defense_price"),
            "LOW" if event.get("direction") == "BULL" else "HIGH",
        )
        parent_ref = _v3_event_pivot_ref(
            upgrade,
            "parent_origin_pivot_id",
            pivots,
            event.get("parent_origin_price"),
            "LOW" if event.get("direction") == "BULL" else "HIGH",
        )
        return (
            f"降級：升級防線{broken_ref}失守；"
            f"父級{parent_ref}尚未等同翻向。"
        )
    if event_type == "STRUCTURE_INVALIDATED":
        upgrade = events.get(event.get("source_upgrade_event_id"))
        direction = {"BULL": "多方", "BEAR": "空方"}.get(
            str(event.get("direction") or ""),
            "原方向",
        )
        parent_ref = _v3_event_pivot_ref(
            upgrade,
            "parent_origin_pivot_id",
            pivots,
            event.get("broken_parent_price"),
            "LOW" if event.get("direction") == "BULL" else "HIGH",
        )
        return f"失效：原{direction}升級結構的父級起點{parent_ref}已被收盤破壞。"
    if event_type == "FALSE_BREAK_RECLAIM":
        bullish = event.get("direction") == "BULL"
        label = "假跌破收復" if bullish else "假突破收回"
        extreme_label = "掃低" if bullish else "掃高"
        source_ref = _v3_false_break_source_ref(event, ledger, pivots, defenses)
        breach_action = "跌破" if bullish else "向上突破"
        reclaim_action = "收復" if bullish else "收回其下"
        return (
            f"{label}：{source_ref}於{_as_hhmm(event.get('breach_time'))}{breach_action}，"
            f"{_as_hhmm(event.get('first_seen_at'))}{reclaim_action}；{extreme_label}{_number_text(event.get('breach_extreme'))}"
            f"（{event.get('bars_to_reclaim')}根）。"
        )
    if event_type != "GRADE_UPGRADE":
        return ""
    direction = str(event.get("direction") or "")
    origin_kind = "LOW" if direction == "BULL" else "HIGH"
    boundary_kind = "HIGH" if direction == "BULL" else "LOW"
    policy = ledger.get("program_trade_policy")
    long_only = bool(
        isinstance(policy, Mapping)
        and policy.get("trade_direction_policy") == "LONG_ONLY"
    )
    event_pivots = pivots
    if long_only:
        # A grade upgrade may become causal while the replacement endpoint is
        # still the current working pivot.  Its time and price are known and
        # should remain visible instead of falling back to a bare number.
        event_pivots = {**pivots, **_by_id(ledger.get("working_pivots"))}
    absorbed_ref = _v3_event_pivot_ref(
        event,
        "absorbed_defense_pivot_id",
        event_pivots,
        event.get("absorbed_defense_price"),
        origin_kind,
    )
    parent_ref = _v3_event_pivot_ref(
        event,
        "parent_origin_pivot_id",
        event_pivots,
        event.get("parent_origin_price"),
        origin_kind,
    )
    boundary_ref = _v3_event_pivot_ref(
        event,
        "reclaimed_boundary_pivot_id",
        event_pivots,
        event.get("reclaimed_boundary_price"),
        boundary_kind,
    )
    replacement_ref = _v3_event_pivot_ref(
        event,
        "replacement_defense_pivot_id",
        event_pivots,
        event.get("replacement_defense_price"),
        origin_kind,
    )
    if long_only:
        side = "多方" if direction == "BULL" else "空方"
        boundary_action = "突破" if direction == "BULL" else "跌破"
        return (
            f"升級：原小級{side}結構{absorbed_ref}雖被{replacement_ref}穿越，但"
            f"父級{parent_ref}守住；"
            f"本輪收盤{boundary_action}{boundary_ref}，{side}級數升級，"
            f"{replacement_ref}成大級防線候選。"
        )
    return (
        f"升級：小級防線{absorbed_ref}失守，但"
        f"父級{parent_ref}守住；"
        f"再破{boundary_ref}，"
        f"{replacement_ref}成大級防線候選。"
    )


def _v3_quadrant_line(reading: Mapping[str, Any], ledger: Mapping[str, Any] | None = None) -> str:
    lifecycle = ledger.get("anchor_lifecycle") if isinstance(ledger, Mapping) else None
    context = lifecycle.get("quadrant_context") if isinstance(lifecycle, Mapping) else None
    if (
        not _v3_is_ai_hybrid(ledger)
        and isinstance(context, Mapping)
        and context.get("authority") != "EVIDENCE_ONLY"
    ):
        side = {"BULL": "多方", "BEAR": "空方"}.get(str(context.get("anchor_direction")), "方向未定")
        phase = {"PUSH": "推進", "CORRECTION": "修正"}.get(str(context.get("phase")), "發展中")
        background = str(context.get("background_primary") or "UNDEFINED")
        background_suffix = "候選" if context.get("background_confidence") == "CANDIDATE" else ""
        background_candidates = [
            str(item) for item in context.get("background_candidates") or []
            if str(item) not in {background, "UNDEFINED", "TRANSITION"}
        ]
        working = str(context.get("working_primary") or "UNDEFINED")
        working_side = {"BULL": "多方", "BEAR": "空方"}.get(
            str(context.get("working_structure_direction")), ""
        )
        working_phase = {"PUSH": "推進", "CORRECTION": "修正"}.get(
            str(context.get("working_phase")), "發展中"
        )
        working_candidates = [
            str(item) for item in context.get("working_candidates") or []
            if str(item) not in {working, "UNDEFINED", "TRANSITION"}
        ]
        background_next = f"；次看{'／'.join(background_candidates)}" if background_candidates else ""
        working_text = (
            f"{working_side}{working_phase}中，主看{working}"
            if working not in {"UNDEFINED", "TRANSITION"}
            else f"{working_side}形成中"
        )
        working_next = f"；次看{'／'.join(working_candidates)}" if working_candidates else ""
        working_suffix = "候選" if context.get("working_confidence") == "CANDIDATE" and working not in {"UNDEFINED", "TRANSITION"} else ""
        # During a newly opened monitoring segment the only active anchor may
        # still be SMALL.  Its anchor-relative quadrant is useful, but must not
        # be presented as an already established large-grade background.
        if not isinstance(lifecycle.get("background_anchor"), Mapping):
            if background not in {"UNDEFINED", "TRANSITION"}:
                small_text = f"{side}{phase}中，主看{background}{background_suffix}{background_next}"
            elif background == "TRANSITION" and background_candidates:
                small_text = (
                    f"{side}{phase}中，主看{background_candidates[0]}候選"
                    + (
                        f"；次看{'／'.join(background_candidates[1:])}"
                        if len(background_candidates) > 1
                        else ""
                    )
                )
            else:
                small_text = "尚未形成"
            return f"象限：大級尚未形成｜小級{small_text}"
        return (
            f"象限：大級{side}{phase}中，主看{background}{background_suffix}{background_next}｜"
            f"小級{working_text}{working_suffix}{working_next}"
        )
    background = str(reading.get("background_quadrant") or "UNDEFINED")
    working = str(reading.get("working_quadrant") or "UNDEFINED")
    primary = str(reading.get("primary_quadrant_candidate") or "UNDEFINED")
    # Quadrant trend dynamics describe strengthening/weakening, not bullish or
    # bearish price direction.  Keep the label explicit so Q1 in a bear leg is
    # not misread as "price is rising".
    trend_labels = {"INCREASING": "趨勢性↑", "DECREASING": "趨勢性↓", "UNCLEAR": "趨勢性未定"}
    volatility_labels = {"EXPANDING": "波動擴張", "CONTRACTING": "波動收縮", "UNSTABLE": "波動不穩", "UNCLEAR": "波動未定"}
    background_axes = (
        f"{trend_labels.get(str(reading.get('background_trend_dynamics')), '趨勢未定')}／"
        f"{volatility_labels.get(str(reading.get('background_volatility_dynamics')), '波動未定')}"
    )
    working_axes = (
        f"{trend_labels.get(str(reading.get('working_trend_dynamics')), '趨勢未定')}／"
        f"{volatility_labels.get(str(reading.get('working_volatility_dynamics')), '波動未定')}"
    )
    work_text = primary if working in {"TRANSITION", "UNDEFINED"} and primary not in {"TRANSITION", "UNDEFINED"} else working
    suffix = "候選" if work_text != working else ""
    background_text = {"TRANSITION": "象限轉換中", "UNDEFINED": "象限未定"}.get(background, background)
    working_text = {"TRANSITION": "象限轉換中", "UNDEFINED": "象限未定"}.get(work_text, f"{work_text}{suffix}")
    if _v3_is_ai_hybrid(ledger):
        records = _by_id(ledger.get("anchor_records") if isinstance(ledger, Mapping) else None)
        large = records.get(reading.get("large_anchor_ref"))
        small = records.get(reading.get("small_anchor_ref"))
        working_record = records.get(reading.get("working_anchor_ref"))
        background_side = {
            "BULL": "多方",
            "BEAR": "空方",
        }.get(str(large.get("direction") if isinstance(large, Mapping) else ""), "")
        working_controller = small if isinstance(small, Mapping) else working_record
        working_side = {
            "BULL": "多方",
            "BEAR": "空方",
        }.get(
            str(
                working_controller.get("direction")
                if isinstance(working_controller, Mapping)
                else ""
            ),
            "",
        )
        if background_text not in {"象限轉換中", "象限未定"}:
            background_text = background_side + background_text
        if working_text not in {"象限轉換中", "象限未定"}:
            working_text = working_side + working_text
    return f"象限：大級{background_text}（{background_axes}）｜小級{working_text}（{working_axes}）"


def _v3_focus_method_lines(reading: Mapping[str, Any]) -> list[str]:
    source = {
        "TAIJI": ("太極", reading.get("taiji")),
        "YIZHI": ("一之", reading.get("yizhi")),
        "LEFT_RIGHT": ("左右", reading.get("left_right")),
        "DOW": ("道氏", reading.get("dow")),
        "X_PROCESS": ("X流程", reading.get("x_process")),
        "QUADRANT": ("四象限", reading.get("primary_lens")),
    }
    result: list[str] = []
    for method in reading.get("focus_methods", []):
        label, value = source.get(str(method), ("", ""))
        text = _compact_public_text(value, 115)
        if label and text:
            result.append(f"{label}：{text}")
    return result


def _v3_course_method_lines(
    reading: Mapping[str, Any], ledger: Mapping[str, Any] | None = None
) -> list[str]:
    if "x_process" not in reading:
        return _v3_focus_method_lines(reading)
    lifecycle = ledger.get("anchor_lifecycle") if isinstance(ledger, Mapping) else None
    taiji_context = lifecycle.get("taiji_context") if isinstance(lifecycle, Mapping) else None
    if _v3_is_ai_hybrid(ledger) or not isinstance(taiji_context, Mapping):
        taiji_text = _compact_public_text(reading.get("taiji"), 115)
    else:
        taiji_text = _v3_taiji_public_text(reading.get("taiji"), ledger, 150)
    taiji_text = re.sub(r"^太極[：:]", "", taiji_text)
    taiji_line = f"太極：{taiji_text}"
    lines = [taiji_line]
    if _v3_is_ai_hybrid(ledger):
        lines.append(f"道氏：{_v3_public_text(reading.get('dow'), ledger, 125)}")
        if "LEFT_RIGHT" in (reading.get("focus_methods") or []):
            lines.append(f"左右：{_v3_public_text(reading.get('left_right'), ledger, 115)}")
    lines.extend(
        [
            f"一之：{_v3_public_text(reading.get('yizhi'), ledger, 115)}",
            f"X流程：{_X_STAGE_ZH.get(str(reading.get('x_stage')), '階段未定')}｜{_v3_public_text(reading.get('x_process'), ledger, 130)}",
        ]
    )
    return _nonempty(lines)


def render_programmatic_entry_fill(event: Mapping[str, Any]) -> RenderedReplayMessage:
    direction = _DIRECTION_ZH.get(event.get("direction"), "未定")
    fill_time = _as_hhmm(event.get("fill_time"))
    signal_time = _as_hhmm(event.get("signal_time"))
    role = "模擬再進場" if event.get("entry_role") == "REENTRY" else "模擬進場"
    lines = [
        f"**{fill_time}｜🟢 {direction}{role}**",
        f"訊號：{signal_time}已收盤K完成觸發；本根使用第一個可成交價。",
        f"進場：{_number_text(event.get('fill_price'))}",
        f"結構停損：{_number_text(event.get('stop_price'))}",
        f"主控：{_compact_public_text(event.get('setup_name'), 100)}",
        f"應有行為：{_compact_public_text(event.get('expected_behavior'), 130)}",
        DISCLAIMER,
    ]
    title = f"🟢 {direction}{role}"
    return RenderedReplayMessage(kind="REENTRY" if event.get("entry_role") == "REENTRY" else ENTRY, title=title, body="\n".join(_nonempty(lines)).rstrip() + "\n")


def render_programmatic_exit_signal(event: Mapping[str, Any]) -> RenderedReplayMessage:
    direction = _DIRECTION_ZH.get(event.get("direction"), "未定")
    signal_time = _as_hhmm(event.get("signal_time") or event.get("as_of"))
    entry_time = _as_hhmm(event.get("entry_time"))
    reason = {
        "EXPECTED_BEHAVIOR_EXPIRED_NO_PROGRESS": "應有行為期限內未產生足夠推進",
        "EXPECTED_BEHAVIOR_FAILED_HOLD": "應守位置失守，原進場動機失效",
    }.get(str(event.get("reason_code") or ""), "原交易計畫的時間／動機失效")
    behavior = event.get("behavior_audit") if isinstance(event.get("behavior_audit"), Mapping) else {}
    max_wait = behavior.get("max_wait_bars")
    checkpoint = behavior.get("checkpoint_price")
    behavior_line = ""
    if isinstance(max_wait, int) and isinstance(checkpoint, (int, float)) and not isinstance(checkpoint, bool):
        behavior_line = f"應有行為：{max_wait}根內至少推進至{_number_text(checkpoint)}；期限已到，未完成。"
    lines = [
        f"**{signal_time}｜🔴 {direction}出場訊號**",
        f"持倉：{entry_time}進場{_number_text(event.get('entry_price'))}",
        f"原因：{reason}",
        behavior_line,
        "處理：本根收盤鎖定出場，下一根1分K第一個可成交價執行。",
        "後續：原交易條件退役；等待新的獨立結構，不沿用舊觸發。",
        DISCLAIMER,
    ]
    title = f"🔴 {direction}出場訊號"
    return RenderedReplayMessage(
        kind=EXIT,
        title=title,
        body="\n".join(_nonempty(lines)).rstrip() + "\n",
    )


def render_programmatic_stop_fill(event: Mapping[str, Any]) -> RenderedReplayMessage:
    direction = _DIRECTION_ZH.get(event.get("direction"), "未定")
    fill_time = _as_hhmm(event.get("fill_time") or event.get("trigger_time"))
    entry_time = _as_hhmm(event.get("entry_time"))
    entry_price = event.get("entry_price")
    fill_price = event.get("fill_price")
    gross_points = None
    if (
        isinstance(entry_price, (int, float))
        and not isinstance(entry_price, bool)
        and isinstance(fill_price, (int, float))
        and not isinstance(fill_price, bool)
    ):
        gross_points = (
            float(fill_price) - float(entry_price)
            if event.get("direction") == "LONG"
            else float(entry_price) - float(fill_price)
        )
    execution_note = (
        "本根跳空越過停損，依第一個可成交價計算。"
        if event.get("gap_through_stop")
        else "本根高低價觸及原先鎖定的結構停損。"
    )
    lines = [
        f"**{fill_time}｜🔴 {direction}模擬停損**",
        f"持倉：{entry_time}進場{_number_text(entry_price)}",
        f"停損：{fill_time} {_number_text(fill_price)}",
        f"原因：{execution_note}",
        f"本筆毛損益：{gross_points:+,.0f}點" if gross_points is not None else "",
        "再進場：至少等待1根完整1分K，且必須由新的獨立結構重新觸發。",
        DISCLAIMER,
    ]
    title = f"🔴 {direction}模擬停損"
    return RenderedReplayMessage(
        kind=STOP,
        title=title,
        body="\n".join(_nonempty(lines)).rstrip() + "\n",
    )


def render_programmatic_exit_fill(event: Mapping[str, Any]) -> RenderedReplayMessage:
    direction = _DIRECTION_ZH.get(event.get("direction"), "未定")
    fill_time = _as_hhmm(event.get("fill_time"))
    signal_time = _as_hhmm(event.get("signal_time"))
    reason = {
        "EXPECTED_BEHAVIOR_EXPIRED_NO_PROGRESS": "應有行為期限內未產生有利推進",
        "EXPECTED_BEHAVIOR_FAILED_HOLD": "應守位置失守，原進場動機失效",
    }.get(str(event.get("reason_code") or ""), "原交易計畫的時間／動機失效")
    entry_price = event.get("entry_price")
    fill_price = event.get("fill_price")
    gross_points = None
    if (
        isinstance(entry_price, (int, float))
        and not isinstance(entry_price, bool)
        and isinstance(fill_price, (int, float))
        and not isinstance(fill_price, bool)
    ):
        gross_points = (
            float(fill_price) - float(entry_price)
            if event.get("direction") == "LONG"
            else float(entry_price) - float(fill_price)
        )
    lines = [
        f"**{fill_time}｜🔴 {direction}模擬出場**",
        f"訊號：{signal_time}已收盤K確認失效；本根使用第一個可成交價。",
        f"出場：{_number_text(fill_price)}",
        f"原因：{reason}",
        f"本筆毛損益：{gross_points:+,.0f}點" if gross_points is not None else "",
        "後續：原setup退役；等待新的獨立結構，不沿用舊觸發追單。",
        DISCLAIMER,
    ]
    title = f"🔴 {direction}模擬出場"
    return RenderedReplayMessage(
        kind=EXIT,
        title=title,
        body="\n".join(_nonempty(lines)).rstrip() + "\n",
    )


def _v3_weight_line(scenario: Mapping[str, Any]) -> str:
    return (
        f"情境權重：多{scenario.get('bull_probability', '?')}%｜"
        f"盤{scenario.get('range_probability', '?')}%｜空{scenario.get('bear_probability', '?')}%"
    )


def _v3_obstacle_line(action: Mapping[str, Any], ledger: Mapping[str, Any]) -> str:
    obstacles = action.get("obstacles") if isinstance(action.get("obstacles"), list) else []
    parts: list[str] = []
    role = {"TRIGGER": "觸發", "CHECKPOINT": "檢查點", "HARD_TARGET": "硬目標"}
    for item in obstacles[:3]:
        if not isinstance(item, Mapping):
            continue
        parts.append(
            f"{role.get(str(item.get('role')), '價位')}"
            f"{_v3_obstacle_ref(item, ledger)}"
        )
    return "障礙：" + "｜".join(parts) if parts else ""


def _v3_bar_level_ref(time: Any, kind: Any, price: Any) -> str:
    kind_text = {"OPEN": "開", "HIGH": "高", "LOW": "低", "CLOSE": "收"}.get(
        str(kind or "").upper(), ""
    )
    time_text = _as_hhmm(time)
    if time_text == "時間未定":
        return _number_text(price)
    return f"{time_text}{kind_text}{_number_text(price)}"


def _v3_pivot_ref(pivot: Any, price: Any = None, kind: Any = None) -> str:
    if isinstance(pivot, Mapping):
        return _v3_bar_level_ref(
            pivot.get("bar_time"),
            pivot.get("kind") or kind,
            pivot.get("price") if pivot.get("price") is not None else price,
        )
    return _number_text(price)


def _v3_defense_ref(defense: Mapping[str, Any]) -> str:
    direction = str(defense.get("direction") or "")
    kind = "LOW" if direction == "BULL" else "HIGH" if direction == "BEAR" else None
    return _v3_bar_level_ref(defense.get("bar_time"), kind, defense.get("price"))


def _v3_event_pivot_ref(
    event: Any,
    key: str,
    pivots: Mapping[Any, Mapping[str, Any]],
    price: Any,
    kind: str,
) -> str:
    pivot = pivots.get(event.get(key)) if isinstance(event, Mapping) else None
    return _v3_pivot_ref(pivot, price, kind)


def _v3_false_break_source_ref(
    event: Mapping[str, Any],
    ledger: Mapping[str, Any],
    pivots: Mapping[Any, Mapping[str, Any]],
    defenses: Mapping[Any, Mapping[str, Any]],
) -> str:
    source_type = str(event.get("source_type") or "")
    source_id = event.get("source_id")
    if source_type == "DOW_DEFENSE":
        defense = defenses.get(source_id)
        if isinstance(defense, Mapping):
            return _v3_defense_ref(defense)
    if source_type in {"PIVOT_LOW", "PIVOT_HIGH"}:
        pivot = pivots.get(source_id)
        if isinstance(pivot, Mapping):
            return _v3_pivot_ref(pivot)
    opening_match = re.fullmatch(r"(OR(?:5|15))_(LOW|HIGH)", source_type)
    if opening_match:
        side = "低" if opening_match.group(2) == "LOW" else "高"
        return f"{opening_match.group(1)}{side}{_number_text(event.get('level_price'))}"
    return _v3_resolve_level_ref(event.get("level_price"), ledger)


def _v3_obstacle_ref(item: Mapping[str, Any], ledger: Mapping[str, Any]) -> str:
    label = _clean_text(item.get("label"), max_chars=40)
    price = item.get("price")
    if re.search(r"(?:^|\b)(?:21|105)MA|ATR\d*|均價", label, re.IGNORECASE):
        return f"{label}{_number_text(price)}"
    if re.search(r"夜盤|OR(?:5|15)|開盤區間", label, re.IGNORECASE):
        resolved = _v3_resolve_level_ref(price, ledger)
        return f"{label}{resolved}"
    return _v3_resolve_level_ref(
        price,
        ledger,
        prefer_defense="防線" in label,
        prefer_trigger=str(item.get("role") or "") == "TRIGGER" or "觸發" in label,
    )


def _v3_resolve_level_ref(
    price: Any,
    ledger: Mapping[str, Any],
    *,
    prefer_defense: bool = False,
    prefer_working: bool = False,
    prefer_trigger: bool = False,
) -> str:
    numeric = _number(price)
    if numeric is None:
        return _number_text(price)

    if prefer_trigger:
        trade_levels = ledger.get("trade_levels")
        candidate = (
            trade_levels.get("continuation_arm_candidate")
            if isinstance(trade_levels, Mapping)
            else None
        )
        if (
            isinstance(candidate, Mapping)
            and _v3_same_level(candidate.get("trigger_level"), numeric)
            and candidate.get("trigger_time")
        ):
            direction = str(candidate.get("direction") or "")
            kind = "LOW" if direction == "SHORT" else "HIGH" if direction == "LONG" else None
            return _v3_bar_level_ref(candidate.get("trigger_time"), kind, numeric)

    # A price can occur on several bars.  When it is also the origin of the
    # active session anchor, that structural role is more meaningful than a
    # later incidental high/low at the same number.  Resolve it before generic
    # pivots and recent-bar matches so public text keeps the anchor's true time.
    anchor_points: list[tuple[int, str, str, Any, str]] = []
    for record in ledger.get("anchor_records") or []:
        if not isinstance(record, Mapping):
            continue
        record_type = str(record.get("record_type") or "")
        status = str(record.get("status") or "")
        direction = str(record.get("direction") or "")
        start_kind = "LOW" if direction == "BULL" else "HIGH" if direction == "BEAR" else ""
        end_kind = "HIGH" if direction == "BULL" else "LOW" if direction == "BEAR" else ""
        base_priority = 100 if record_type == "ANCHOR" and status == "ACTIVE" else 60
        for time_key, price_key, kind, point_priority in (
            ("origin_time", "origin_price", "OPEN" if record.get("anchor_origin_kind") == "SESSION_OPEN" else start_kind, 30),
            ("start_time", "start_price", start_kind, 20),
            ("first_extreme_time", "first_extreme_price", end_kind, 10),
            ("latest_extreme_time", "latest_extreme_price", end_kind, 5),
            ("current_extreme_time", "current_extreme_price", end_kind, 0),
        ):
            if _v3_same_level(record.get(price_key), numeric) and record.get(time_key):
                anchor_points.append(
                    (
                        base_priority + point_priority,
                        str(record.get(time_key)),
                        kind,
                        record.get(time_key),
                        record_type,
                    )
                )
    if prefer_working:
        working_points = [item for item in anchor_points if item[4] == "WORKING_LEG"]
        if working_points:
            _, _, kind, at, _ = max(working_points, key=lambda item: (item[0], item[1]))
            return _v3_bar_level_ref(at, kind, numeric)
    if anchor_points and not prefer_defense:
        _, _, kind, at, _ = max(anchor_points, key=lambda item: (item[0], item[1]))
        return _v3_bar_level_ref(at, kind, numeric)

    defenses = [
        item
        for item in ledger.get("defenses", [])
        if isinstance(item, Mapping) and _v3_same_level(item.get("price"), numeric)
    ]
    pivots = [
        item
        for item in [*(ledger.get("pivots") or []), *(ledger.get("working_pivots") or [])]
        if isinstance(item, Mapping) and _v3_same_level(item.get("price"), numeric)
    ]
    if prefer_defense and defenses:
        return _v3_defense_ref(_v3_latest_point(defenses))
    if pivots:
        return _v3_pivot_ref(_v3_latest_point(pivots))
    if defenses:
        return _v3_defense_ref(_v3_latest_point(defenses))

    opening = ledger.get("opening_ranges")
    if isinstance(opening, Mapping):
        matches: list[str] = []
        for name in ("or5", "or15"):
            value = opening.get(name)
            if not isinstance(value, Mapping):
                continue
            for key, side in (("low", "低"), ("high", "高")):
                if _v3_same_level(value.get(key), numeric):
                    level_time = value.get(f"{key}_time")
                    if level_time:
                        matches.append(_v3_bar_level_ref(level_time, key.upper(), numeric))
                    else:
                        matches.append(f"{name.upper()}{side}")
        if matches:
            if all(match.endswith(_number_text(numeric)) for match in matches):
                return "/".join(matches)
            return f"{'/'.join(matches)}{_number_text(numeric)}"

    if anchor_points:
        _, _, kind, at, _ = max(anchor_points, key=lambda item: (item[0], item[1]))
        return _v3_bar_level_ref(at, kind, numeric)

    latest = ledger.get("latest_closed_k")
    if isinstance(latest, Mapping):
        for key in ("low", "high", "open", "close"):
            if _v3_same_level(latest.get(key), numeric):
                return _v3_bar_level_ref(latest.get("time"), key.upper(), numeric)

    recent_matches: list[tuple[int, str, str, Any]] = []
    for bar in ledger.get("recent_bar_levels") or []:
        if not isinstance(bar, Mapping):
            continue
        for key in ("low", "high", "open", "close"):
            if _v3_same_level(bar.get(key), numeric) and bar.get("time"):
                priority = 2 if key in {"low", "high"} else 1
                recent_matches.append((priority, str(bar.get("time")), key.upper(), bar.get("time")))
    if recent_matches:
        _, _, kind, at = max(recent_matches, key=lambda item: (item[0], item[1]))
        return _v3_bar_level_ref(at, kind, numeric)

    for leg in reversed(ledger.get("legs") or []):
        if not isinstance(leg, Mapping):
            continue
        direction = str(leg.get("direction") or "")
        if _v3_same_level(leg.get("end_price"), numeric):
            kind = "HIGH" if direction == "BULL" else "LOW" if direction == "BEAR" else None
            return _v3_bar_level_ref(leg.get("end_time"), kind, numeric)
        if _v3_same_level(leg.get("start_price"), numeric):
            kind = "LOW" if direction == "BULL" else "HIGH" if direction == "BEAR" else None
            return _v3_bar_level_ref(leg.get("start_time"), kind, numeric)
    return _number_text(numeric)


def _v3_level_source_times(price: Any, ledger: Mapping[str, Any]) -> set[str]:
    """Enumerate plausible structural sources; equal prices need not be the same bar."""
    times: set[str] = set()
    def add(value: Any, at: Any) -> None:
        if at and _v3_same_level(value, price):
            times.add(str(at))
    for item in ledger.get("anchor_records") or []:
        if isinstance(item, Mapping):
            for key in ("origin", "start", "first_extreme", "latest_extreme", "current_extreme"):
                add(item.get(f"{key}_price"), item.get(f"{key}_time"))
    for name in ("pivots", "working_pivots", "defenses"):
        for item in ledger.get(name) or []:
            if isinstance(item, Mapping):
                add(item.get("price"), item.get("bar_time") or item.get("time"))
    for item in ledger.get("legs") or []:
        if isinstance(item, Mapping):
            for key in ("start", "end"):
                add(item.get(f"{key}_price"), item.get(f"{key}_time"))
    for item in ledger.get("recent_bar_levels") or []:
        if isinstance(item, Mapping):
            for key in ("open", "high", "low", "close"):
                add(item.get(key), item.get("time"))
    return times


def _v3_recent_close_ref(price: Any, ledger: Mapping[str, Any]) -> str | None:
    """Resolve an explicitly described close without guessing an equal-price extreme."""

    matches: list[Mapping[str, Any]] = []
    latest = ledger.get("latest_closed_k")
    if isinstance(latest, Mapping) and _v3_same_level(latest.get("close"), price):
        matches.append(latest)
    for item in ledger.get("recent_bar_levels") or []:
        if (
            isinstance(item, Mapping)
            and item.get("time")
            and _v3_same_level(item.get("close"), price)
        ):
            matches.append(item)
    if not matches:
        return None
    selected = max(matches, key=lambda item: str(item.get("time") or ""))
    return _v3_bar_level_ref(selected.get("time"), "CLOSE", price)


def _v3_annotate_known_levels(value: Any, ledger: Mapping[str, Any]) -> str:
    """Add a causal bar reference to public five-digit price levels when known."""

    text = _compact_public_text(value, 300)
    pattern = re.compile(r"(?<![\d,])(?:\d{2},\d{3}|\d{5})(?![\d,])")

    def replace(match: re.Match[str]) -> str:
        numeric_text = match.group(0).replace(",", "")
        prefix = text[max(0, match.start() - 18) : match.start()]
        if re.search(
            r"\d{2}:\d{2}(?:的)?(?:開盤價|開盤|收盤價|收盤|高點|低點|[開高低收](?:點)?)?$",
            prefix,
        ):
            formatted = _number_text(match.group(0).replace(",", ""))
            return formatted.removesuffix("點") if text[match.end() : match.end() + 1] == "點" else formatted
        if re.search(r"(?:該|同|本)根(?:的)?(?:開盤價|開盤|收盤價|收盤|高點|低點|[開高低收])$", prefix):
            formatted = _number_text(match.group(0).replace(",", ""))
            return formatted.removesuffix("點") if text[match.end() : match.end() + 1] == "點" else formatted
        if re.search(r"本根(?:即|反向)?(?:收|收回至|收復至)$", prefix):
            # The card timestamp already identifies this bar.  Adding another
            # `10:28收` here would produce misleading prose such as
            # `本根收10:28收...`.
            formatted = _number_text(match.group(0).replace(",", ""))
            return formatted.removesuffix("點") if text[match.end() : match.end() + 1] == "點" else formatted
        if re.search(r"(?:21|105)MA(?:的)?$|ATR\d*(?:的)?$|均價(?:的)?$", prefix, re.IGNORECASE):
            return match.group(0)
        derived_protection_levels = ledger.get("_derived_protection_levels")
        if isinstance(derived_protection_levels, list) and any(
            _v3_same_level(numeric_text, item)
            for item in derived_protection_levels
            if isinstance(item, (int, float)) and not isinstance(item, bool)
        ):
            # The operational stop is a calculated execution level.  Even if
            # it equals a historical OHLC value by chance, all occurrences in
            # this card must remain a protection price rather than acquire an
            # unrelated candle timestamp.
            formatted = _number_text(numeric_text)
            return formatted.removesuffix("點") if text[match.end() : match.end() + 1] == "點" else formatted
        # A calculated stop/buffer can coincidentally round to a historical
        # bar price.  Its source is the stated structural endpoint plus the
        # buffer, not that unrelated bar; never invent a timestamp for it.
        derived_prefix = text[max(0, match.start() - 40) : match.start()]
        if re.search(
            r"(?:(?:緩衝|計算(?:值|價)?|推估(?:值|價)?|參考(?:停損|價))"
            r"(?:後)?(?:的)?(?:參考停損)?(?:為|約為)?|"
            r"(?:實際)?保護(?:停損|價)(?:調整|上移|收緊)?(?:為|至)?)"
            r"[^。；;\n]{0,12}$",
            derived_prefix,
        ):
            formatted = _number_text(match.group(0).replace(",", ""))
            return formatted.removesuffix("點") if text[match.end() : match.end() + 1] == "點" else formatted
        # Scope hints to this clause. A previous sentence mentioning the
        # current leg must not relabel an older equal-price Taiji endpoint.
        clause = re.split(r"[。；;\n]", text[:match.start()])[-1]
        suffix = text[match.end() : match.end() + 4]
        latest = ledger.get("latest_closed_k")
        explicit_close = bool(re.match(r"(?:點)?收盤", suffix))
        current_close = bool(
            isinstance(latest, Mapping)
            and _v3_same_level(latest.get("close"), numeric_text)
            and re.search(r"(?:本根|最新(?:價|收盤)?|現價)[^。；;\n]{0,18}(?:收|收回|收復|站上|跌破|突破|至)$", clause)
        )
        if explicit_close or current_close:
            close_ref = _v3_recent_close_ref(numeric_text, ledger)
            if close_ref is not None:
                if explicit_close:
                    at = close_ref[:5]
                    formatted = _number_text(numeric_text)
                    return f"{at}以{formatted}"
                already_has_unit = text[match.end() : match.end() + 1] == "點"
                return close_ref.removesuffix("點") if already_has_unit else close_ref
        prefer_working = bool(re.search(r"工作段|延伸至", clause))
        prefer_defense = "防線" in clause
        prefer_trigger = bool(
            "觸發" in clause or re.match(r"(?:點)?觸發", suffix)
        )
        stop_context = text[max(0, match.start() - 24) : min(len(text), match.end() + 24)]
        if (
            "停損" in stop_context
            or "保護價" in stop_context
            or re.match(r"(?:點)?(?:保護)?停損", suffix)
        ):
            # A stop is often an endpoint plus volatility buffer.  Even when
            # its rounded value equals an unrelated historical OHLC level,
            # that coincidence must not fabricate a source timestamp.
            formatted = _number_text(numeric_text)
            return formatted.removesuffix("點") if text[match.end() : match.end() + 1] == "點" else formatted
        explicit_anchor = bool(re.search(r"(?:大|小|定|方)錨|父代", clause))
        if not (prefer_working or prefer_defense or explicit_anchor) and len(
            _v3_level_source_times(numeric_text, ledger)
        ) > 1:
            return match.group(0)
        resolved = _v3_resolve_level_ref(
            numeric_text,
            ledger,
            prefer_working=prefer_working,
            prefer_defense=prefer_defense,
            prefer_trigger=prefer_trigger,
        )
        if resolved == _number_text(numeric_text):
            return match.group(0)
        already_has_unit = text[match.end() : match.end() + 1] == "點"
        return resolved.removesuffix("點") if already_has_unit else resolved

    return pattern.sub(replace, text)


def _v3_presentation_ledger(
    ledger: Mapping[str, Any],
    *,
    action: Mapping[str, Any],
    position_before: Mapping[str, Any],
    position_after: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Attach card-local calculated stop levels that must never be bar-labelled."""

    levels: list[float] = []
    for source, key in (
        (action, "stop_price"),
        (position_before, "stop_price"),
        (position_after, "stop_price"),
    ):
        value = source.get(key) if isinstance(source, Mapping) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            numeric = float(value)
            if not any(_v3_same_level(numeric, existing) for existing in levels):
                levels.append(numeric)
    if not levels:
        return ledger
    return {**dict(ledger), "_derived_protection_levels": levels}


def _v3_public_text(value: Any, ledger: Mapping[str, Any] | None, maximum: int) -> str:
    text = (
        _v3_annotate_known_levels(value, ledger)
        if isinstance(ledger, Mapping)
        else _compact_public_text(value, 300)
    )
    # Level annotation can turn raw ``45835低點`` into
    # ``13:25低45,835點低點``.  The bar-role prefix already says this is the
    # low/high, so remove only the duplicated suffix after annotation.
    text = re.sub(r"((?:高|低)[\d,]+點)\s*(?:高點|低點)", r"\1", text)
    if len(text) <= maximum:
        return text
    head = text[:maximum]
    sentence_breaks = [match.end() for match in re.finditer(r"[。；]", head)]
    minimum_sentence = max(20, int(maximum * 0.55))
    usable_sentences = [index for index in sentence_breaks if index >= minimum_sentence]
    if usable_sentences:
        return head[: usable_sentences[-1]].rstrip()
    clause_breaks = [match.end() for match in re.finditer(r"[，、]", head)]
    minimum_clause = max(20, int(maximum * 0.7))
    usable_clauses = [index for index in clause_breaks if index >= minimum_clause]
    if usable_clauses:
        return head[: usable_clauses[-1]].rstrip("，、") + "…"
    return head[: maximum - 1].rstrip("，；。") + "…"


def _v3_taiji_public_text(value: Any, ledger: Mapping[str, Any] | None, maximum: int) -> str:
    """Disambiguate the active Taiji generation from its durable dynasty anchor."""

    text = _clean_text(value)
    lifecycle = ledger.get("anchor_lifecycle") if isinstance(ledger, Mapping) else None
    background = lifecycle.get("background_anchor") if isinstance(lifecycle, Mapping) else None
    taiji = lifecycle.get("taiji_context") if isinstance(lifecycle, Mapping) else None
    if isinstance(background, Mapping) and isinstance(taiji, Mapping):
        parent_start = _number(taiji.get("parent_start_price"))
        parent_end = _number(taiji.get("parent_end_price"))
        anchor_start = _number(background.get("origin_price"))
        anchor_end = _number(background.get("first_extreme_price"))
        is_current_generation = (
            parent_start is not None
            and parent_end is not None
            and anchor_start is not None
            and anchor_end is not None
            and (
                abs(parent_start - anchor_start) > 0.5
                or abs(parent_end - anchor_end) > 0.5
            )
        )
        if is_current_generation:
            text = re.sub(r"大級(多方|空方)父代", r"本代\1父代", text)
    return _v3_public_text(text, ledger, maximum)


def _v3_public_clause(value: Any, ledger: Mapping[str, Any] | None, maximum: int) -> str:
    text = _v3_public_text(value, ledger, 300)
    clauses = [item for item in re.split(r"[。；]", text) if item]
    selected = clauses[0] if clauses else text
    return selected if len(selected) <= maximum else selected[: maximum - 1].rstrip("，；。") + "…"


def _v3_latest_point(items: list[Mapping[str, Any]]) -> Mapping[str, Any]:
    return max(items, key=lambda item: str(item.get("bar_time") or item.get("first_seen_at") or ""))


def _v3_same_level(left: Any, right: Any) -> bool:
    left_number = _number(left)
    right_number = _number(right)
    return left_number is not None and right_number is not None and abs(left_number - right_number) <= 0.5


def _v3_expected_line(action: Mapping[str, Any], ledger: Mapping[str, Any] | None = None) -> str:
    wait = action.get("max_wait_bars")
    prefix = f"預期{wait}根內：" if isinstance(wait, int) else "預期："
    return prefix + _v3_public_text(action.get("expected_behavior"), ledger, 120)


def _v3_position_lines(
    message_type: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    action: Mapping[str, Any],
    *,
    ledger: Mapping[str, Any] | None = None,
) -> list[str]:
    if message_type in {"ENTRY", "REENTRY"}:
        label = "再進場" if message_type == "REENTRY" else "進場"
        return [
            f"{label}：{_DIRECTION_ZH.get(after.get('status'), '未定')} {_number_text(after.get('entry_price'))}",
            f"結構停損：{_number_text(after.get('stop_price'))}",
        ]
    if message_type in {"STOP", "EXIT"}:
        lines = [
            f"原持倉：{_DIRECTION_ZH.get(before.get('status'), '未定')}，進場{_number_text(before.get('entry_price'))}",
        ]
        latest = ledger.get("latest_closed_k") if isinstance(ledger, Mapping) else None
        stop_fill = _number(action.get("stop_price")) if message_type == "STOP" else None
        exit_price = (
            stop_fill
            if stop_fill is not None
            else _number(latest.get("close")) if isinstance(latest, Mapping) else None
        )
        entry_price = _number(before.get("entry_price"))
        if exit_price is not None:
            stop_audit = ledger.get("protective_stop_audit") if isinstance(ledger, Mapping) else None
            stop_trigger_time = (
                stop_audit.get("trigger_time")
                if message_type == "STOP"
                and isinstance(stop_audit, Mapping)
                and stop_audit.get("status") == "TRIGGERED"
                else None
            )
            at = (
                _as_hhmm(stop_trigger_time)
                if stop_trigger_time
                else _as_hhmm(latest.get("time"))
                if isinstance(latest, Mapping)
                else ""
            )
            if message_type == "STOP":
                pnl = None
                if entry_price is not None:
                    if before.get("status") == "LONG":
                        pnl = exit_price - entry_price
                    elif before.get("status") == "SHORT":
                        pnl = entry_price - exit_price
                pnl_text = f"｜{pnl:+,.0f}點" if pnl is not None else ""
                lines.append(f"模擬停損：{at} {_number_text(exit_price)}{pnl_text}")
            else:
                # A discretionary EXIT is decided only after this candle has
                # closed.  Its close is therefore the signal reference, not an
                # executable fill.  Performance accounting fills the order at
                # the next observed open; do not present hindsight P&L here.
                lines.append(
                    f"出場訊號：{at}收{_number_text(exit_price)}"
                    "｜按下一根1分K第一個可成交價執行"
                )
        reason = action.get("trigger") if message_type == "STOP" else action.get("management")
        lines.append(f"原因：{_compact_public_text(reason, 120)}")
        return lines
    old_stop = before.get("stop_price")
    new_stop = after.get("stop_price")
    stop_text = f"停損{_number_text(new_stop)}"
    if old_stop is not None and new_stop is not None and _number(old_stop) != _number(new_stop):
        stop_text = f"停損{_number_text(old_stop)}→{_number_text(new_stop)}"
    return [
        f"持倉：{_DIRECTION_ZH.get(before.get('status'), '未定')}，進場{_number_text(before.get('entry_price'))}，{stop_text}",
        f"管理：{_compact_public_text(action.get('management'), 130)}",
    ]


def _compact_public_text(value: Any, maximum: int = 140) -> str:
    text = _clean_text(value)
    replacements = {
        "一分鐘K棒": "1分K",
        "一分鐘K": "1分K",
        "一分K棒": "1分K",
        "十五分鐘": "15分",
        "二十一期均價": "21MA",
        "一百零五期均價": "105MA",
        "十四期平均真實波幅": "ATR14",
        "三至五根": "3～5根",
        "三到五根": "3～5根",
        "二至三根": "2～3根",
        "兩至三根": "2～3根",
        "兩到三根": "2～3根",
        "空手不新增追價": "不新增追價",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = re.sub(r"OR(5|15)(?:高點)?OR\1高", r"OR\1高", text)
    text = re.sub(r"OR(5|15)(?:低點)?OR\1低", r"OR\1低", text)
    text = re.sub(r"((?:高|低)[\d,]+點)\s*(?:高點|低點)", r"\1", text)
    for boilerplate in (
        "，不攤平、不放寬未來結構停損",
        "，不攤平、不放寬結構停損",
        "，不攤平、不放寬停損",
        "；不攤平、不放寬未來結構停損",
        "；不攤平、不放寬結構停損",
        "；不攤平、不放寬停損",
        "，禁止攤平或放寬停損",
        "；禁止攤平或放寬停損",
    ):
        text = text.replace(boilerplate, "")
    text = re.sub(r"\s+", "", text)
    text = re.sub(
        r"(?<![\d.])(?:\d{2},?\d{3})\.\d+(?!\d)",
        lambda match: f"{float(match.group(0).replace(',', '')):,.0f}",
        text,
    )
    text = re.sub(
        r"(?<![\d.])\d{1,4}\.\d{3,}(?!\d)",
        lambda match: f"{float(match.group(0)):.1f}".rstrip("0").rstrip("."),
        text,
    )
    text = re.sub(
        r"(?<![\d,])(\d{5})(?![\d,])",
        lambda match: f"{int(match.group(1)):,}",
        text,
    )
    return text if len(text) <= maximum else text[: maximum - 1].rstrip("，；。") + "…"


def _compact_public_clause(value: Any, maximum: int = 100) -> str:
    text = _compact_public_text(value, 300)
    clauses = [item for item in re.split(r"[。；]", text) if item]
    selected = clauses[0] if clauses else text
    return selected if len(selected) <= maximum else selected[: maximum - 1].rstrip("，；。") + "…"


def _semantic_structure_lines(reading: Mapping[str, Any], ledger: Mapping[str, Any]) -> list[str]:
    legs = _by_id(ledger.get("legs"))
    defenses = _by_id(ledger.get("defenses"))
    pivots = ledger.get("pivots") if isinstance(ledger.get("pivots"), list) else []
    lines: list[str] = []
    for key, label in (("large_anchor_ref", "大錨"), ("small_anchor_ref", "小錨")):
        leg = legs.get(reading.get(key))
        if isinstance(leg, Mapping):
            duration = int(leg.get("duration_minutes") or 0)
            duration_text = "首根形成中" if leg.get("status") == "FORMING" and duration == 0 else f"{duration}分鐘"
            status = "形成中" if leg.get("status") == "FORMING" else "已確認"
            lines.append(
                f"{label}：{_DIRECTION_ZH.get(leg.get('direction'), '方向未定')} "
                f"{_as_hhmm(leg.get('start_time'))} {_number_text(leg.get('start_price'))} → "
                f"{_as_hhmm(leg.get('end_time'))} {_number_text(leg.get('end_price'))}｜"
                f"{float(leg.get('amplitude_points') or 0):+,.0f}點／{duration_text}｜{status}"
            )
    recent = [item for item in pivots if isinstance(item, Mapping)]
    recent.sort(key=lambda item: str(item.get("bar_time") or ""), reverse=True)
    if recent:
        parts = [
            f"{_as_hhmm(item.get('bar_time'))} {'高點' if item.get('kind') == 'HIGH' else '低點'}{_number_text(item.get('price'))}（{item.get('level') == 'LARGE' and '二級' or '一級'}已確認）"
            for item in recent[:2]
        ]
        lines.append("樞紐：" + "；".join(parts))
    for key, label in (("large_defense_ref", "大級道氏防線"), ("small_defense_ref", "小級道氏防線")):
        defense = defenses.get(reading.get(key))
        if isinstance(defense, Mapping):
            lines.append(
                f"{label}：{_DIRECTION_ZH.get(defense.get('direction'), '方向未定')} "
                f"{_as_hhmm(defense.get('bar_time'))} {_number_text(defense.get('price'))}"
            )
    broken = [
        item
        for item in defenses.values()
        if isinstance(item, Mapping) and item.get("state") == "BROKEN"
    ]
    broken.sort(key=lambda item: str(item.get("broken_at") or ""), reverse=True)
    for item in broken[:1]:
        grade = "大級" if item.get("level") == "LARGE" else "小級"
        lines.append(
            f"{grade}前道氏防線：{_as_hhmm(item.get('bar_time'))} {_number_text(item.get('price'))}；"
            f"{_as_hhmm(item.get('broken_at'))}收盤跌破，已失效"
        )
    return lines


def _semantic_position_lines(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    action: Mapping[str, Any],
) -> list[str]:
    event = action.get("position_action")
    if event == "ENTER":
        return [
            f"方向：{_DIRECTION_ZH.get(after.get('status'), '未定')}；收盤確認模擬進場價{_number_text(after.get('entry_price'))}",
            f"結構停損：{_number_text(after.get('stop_price'))}；{_clean_text(action.get('structural_stop'))}",
        ]
    if event in {"STOP", "EXIT"}:
        return [
            f"原持倉：{_DIRECTION_ZH.get(before.get('status'), '未定')}；本根收盤退出模擬持倉。",
            _clean_text(action.get("management")),
        ]
    old_stop = before.get("stop_price")
    new_stop = after.get("stop_price")
    stop_text = f"停損{_number_text(new_stop)}"
    if old_stop is not None and new_stop is not None and _number(old_stop) != _number(new_stop):
        stop_text = f"停損{_number_text(old_stop)}→{_number_text(new_stop)}"
    return [
        f"持倉：{_DIRECTION_ZH.get(before.get('status'), '未定')}；進場價{_number_text(before.get('entry_price'))}；{stop_text}",
        _clean_text(action.get("management")),
    ]


def _semantic_trend(value: Any, *, detail: bool = False) -> str:
    if not isinstance(value, Mapping):
        return "未定"
    classification = _clean_text(value.get("classification") or "未定")
    first = _first_text(value.get("details"))
    return classification + (f"；{first}" if detail and first else "")


def _by_id(value: Any) -> dict[Any, Mapping[str, Any]]:
    if not isinstance(value, list):
        return {}
    return {item.get("id"): item for item in value if isinstance(item, Mapping) and item.get("id")}


def _public_reason(value: Any) -> str:
    text = _clean_text(value or "條件不變。")
    text = re.sub(r"系統強制通知[：:].*", "", text).strip(" ；。")
    return text + "。" if text else "條件不變。"


def classify_replay_message_kind(
    payload: Mapping[str, Any],
    *,
    stage: str,
    constitution_snapshot: Mapping[str, Any] | None = None,
) -> str:
    if stage == "preopen":
        return SNAPSHOT
    event = payload.get("constitution_event")
    event_type = event.get("event_type") if isinstance(event, Mapping) else "NONE"
    if event_type == "SIM_ENTER":
        return ENTRY
    if event_type == "SIM_STOP":
        return STOP
    if event_type == "SIM_EXIT":
        return EXIT
    position = constitution_snapshot.get("simulated_position") if isinstance(constitution_snapshot, Mapping) else None
    if isinstance(position, Mapping):
        return MANAGEMENT
    if payload.get("original_decision") == "DONT_NOTIFY":
        return UNCHANGED
    setup = _setup(_state(payload))
    setup_stage = setup.get("stage") if isinstance(setup, Mapping) else None
    pattern_status = _pattern(payload).get("status")
    if setup_stage in {"ARMED", "AGGRESSIVE_CONFIRMED", "CONSERVATIVE_CONFIRMED"} or pattern_status == "可評估進場":
        return ARMED
    return OBSERVATION


def _event_title(payload: Mapping[str, Any], kind: str) -> str:
    titles = {
        SNAPSHOT: "盤前完整快照",
        ENTRY: "模擬進場",
        MANAGEMENT: "持倉管理",
        STOP: "停損出場",
        EXIT: "條件失效出場",
        UNCHANGED: "條件不變，繼續觀望",
    }
    if kind in titles:
        return titles[kind]
    setup = _setup(_state(payload))
    stage = setup.get("stage") if isinstance(setup, Mapping) else None
    if stage == "INVALIDATED":
        return "候選已失效"
    if stage == "NO_CHASE":
        return "方向仍在，但不追價"
    if kind == ARMED:
        return "交易條件已準備"
    event = payload.get("constitution_event")
    if isinstance(event, Mapping) and event.get("event_type") == "COOLDOWN_REQUALIFIED":
        return "冷卻完成，重新觀察"
    return "盤勢結構更新"


def _quiet_summary(payload: Mapping[str, Any]) -> list[str]:
    state = _state(payload)
    anchor = state.get("anchor_context") if isinstance(state, Mapping) else None
    quadrant = "象限未定"
    if isinstance(anchor, Mapping):
        quadrant = _QUADRANT_ZH.get(str(anchor.get("working_quadrant")), "象限未定")
    pattern = _pattern(payload)
    result = [
        f"大結構：{_trend_class(payload, 'large_trend')}｜小結構：{_trend_class(payload, 'current_trend')}｜{quadrant}",
        f"主控：{_clean_text(pattern.get('pattern') or '尚無主控戰法')}；{_clean_text(payload.get('notification_reason') or '條件不變。')}",
    ]
    missing = _first_text(payload.get("missing_conditions_or_trigger"))
    if missing:
        result.append(f"下一步：{missing}")
    return result


def _trend_lines(payload: Mapping[str, Any]) -> list[str]:
    lines = [_trend_line("大結構", payload.get("large_trend")), _trend_line("小結構", payload.get("current_trend"))]
    state = _state(payload)
    anchor = state.get("anchor_context") if isinstance(state, Mapping) else None
    if isinstance(anchor, Mapping):
        grade = _GRADE_ZH.get(str(anchor.get("controlling_grade")), "控制級數未定")
        relation = _GRADE_RELATION_ZH.get(str(anchor.get("grade_relation")), "大小級關係未定")
        lines.append(f"目前控制：{grade}；{relation}。{_clean_text(anchor.get('control_reason'))}")
    return _nonempty(lines)


def _trend_line(label: str, value: Any) -> str:
    if not isinstance(value, Mapping):
        return f"{label}：未定"
    classification = _clean_text(value.get("classification") or "未定")
    detail = _first_text(value.get("details"))
    return f"{label}：{classification}" + (f"；{detail}" if detail else "")


def _structure_lines(state: Mapping[str, Any], *, compact: bool) -> list[str]:
    if not state:
        return []
    lines: list[str] = []
    anchor_context = state.get("anchor_context")
    if isinstance(anchor_context, Mapping):
        anchors = anchor_context.get("anchors")
        anchors = anchors if isinstance(anchors, list) else []
        by_id = {item.get("anchor_id"): item for item in anchors if isinstance(item, Mapping)}
        for field, label in (("active_large_anchor_id", "大錨"), ("active_small_anchor_id", "小錨")):
            active = by_id.get(anchor_context.get(field))
            if isinstance(active, Mapping):
                lines.append(_anchor_line(label, active, by_id))
    if compact and lines:
        lines = lines[:2]
    if not compact:
        lines.extend(_pivot_lines(state))
    lines.extend(_defense_lines(state, compact=compact))
    return _nonempty(lines)


def _anchor_line(label: str, anchor: Mapping[str, Any], by_id: Mapping[Any, Mapping[str, Any]]) -> str:
    start_time = _as_hhmm(anchor.get("start_bar_time"))
    extreme_time = _as_hhmm(anchor.get("extreme_bar_time"))
    start = _number_text(anchor.get("start_price_estimate"))
    extreme = _number_text(anchor.get("extreme_price_estimate"))
    direction = _DIRECTION_ZH.get(anchor.get("direction"), "方向未定")
    status = _ANCHOR_STATUS_ZH.get(str(anchor.get("status")), "狀態未定")
    quality = _QUALITY_ZH.get(str(anchor.get("quality")), "品質未定")
    metrics: list[str] = []
    start_value = _number(anchor.get("start_price_estimate"))
    extreme_value = _number(anchor.get("extreme_price_estimate"))
    if start_value is not None and extreme_value is not None:
        metrics.append(f"{extreme_value - start_value:+,.0f}點")
    duration = _minutes_between(anchor.get("start_bar_time"), anchor.get("extreme_bar_time"))
    if duration is not None:
        metrics.append(f"{duration}分鐘")
    metric_text = "／".join(metrics) if metrics else "幅度／時間未定"
    parent_text = ""
    parent = by_id.get(anchor.get("parent_anchor_id"))
    if isinstance(parent, Mapping):
        parent_text = f"；父級大錨始於{_as_hhmm(parent.get('start_bar_time'))}"
    return f"{label}：{direction} {start_time} {start} → {extreme_time} {extreme}｜{metric_text}｜{quality}、{status}{parent_text}"


def _pivot_lines(state: Mapping[str, Any]) -> list[str]:
    primary = state.get("primary_pivots")
    primary = primary if isinstance(primary, list) else []
    candidates = [item for item in primary if isinstance(item, Mapping) and item.get("state") not in {"INVALIDATED", "REPLACED"}]
    candidates.sort(key=lambda item: str(item.get("bar_time") or ""), reverse=True)
    if not candidates:
        return ["樞紐：目前尚無有效的一級樞紐候選或確認點。"]
    descriptions = []
    for pivot in candidates[:2]:
        kind = "高點" if pivot.get("kind") == "HIGH" else "低點"
        descriptions.append(
            f"{_as_hhmm(pivot.get('bar_time'))} {kind}{_number_text(pivot.get('price_estimate'))}"
            f"（{_PIVOT_STATUS_ZH.get(str(pivot.get('state')), '狀態未定')}）"
        )
    return ["樞紐：" + "；".join(descriptions)]


def _defense_lines(state: Mapping[str, Any], *, compact: bool) -> list[str]:
    defenses = state.get("defense_lines")
    if not isinstance(defenses, Mapping):
        return []
    pivots = []
    for key in ("primary_pivots", "secondary_pivots"):
        value = state.get(key)
        if isinstance(value, list):
            pivots.extend(item for item in value if isinstance(item, Mapping))
    by_id = {item.get("pivot_id"): item for item in pivots}
    labels = {
        "large_bull": "大級多頭防線",
        "large_bear": "大級空頭防線",
        "small_bull": "小級多頭防線",
        "small_bear": "小級空頭防線",
    }
    result = []
    for key in ("large_bull", "large_bear", "small_bull", "small_bear"):
        defense = defenses.get(key)
        if not isinstance(defense, Mapping):
            continue
        status = {"ACTIVE": "有效", "BROKEN": "已突破", "REPLACED": "已取代"}.get(str(defense.get("status")), "狀態未定")
        pivot = by_id.get(defense.get("pivot_id"))
        source = "來源樞紐未定"
        if isinstance(pivot, Mapping):
            kind = "高點" if pivot.get("kind") == "HIGH" else "低點"
            source = f"源自{_as_hhmm(pivot.get('bar_time'))}{kind}"
        result.append(f"{labels[key]}：{_number_text(defense.get('price_estimate'))}（{source}；{status}）")
    if not result:
        return ["道氏防線：大、小級均尚未形成可確認防線。"]
    if compact:
        active = [line for line in result if "；有效）" in line]
        return (active or result)[:2]
    return result[:4]


def _reading_lines(payload: Mapping[str, Any], state: Mapping[str, Any], *, snapshot: bool) -> list[str]:
    lines: list[str] = []
    anchor = state.get("anchor_context") if isinstance(state, Mapping) else None
    if isinstance(anchor, Mapping):
        quadrant = _QUADRANT_ZH.get(str(anchor.get("working_quadrant")), "象限未定")
        trend = _TREND_DYNAMICS_ZH.get(str(anchor.get("trend_dynamics")), "趨勢性未定")
        volatility = _VOLATILITY_DYNAMICS_ZH.get(str(anchor.get("volatility_dynamics")), "波動未定")
        lines.append(f"四象限：{quadrant}；{trend}、{volatility}。{_clean_text(anchor.get('quadrant_reason'))}")

    cclass = state.get("cclass_context") if isinstance(state, Mapping) else None
    if isinstance(cclass, Mapping):
        engine = _ENGINE_ZH.get(str(cclass.get("engine_mode")), "模式未定")
        order = _ORDER_ZH.get(str(cclass.get("order_state")), "秩序未定")
        engine_line = f"市場節奏：{engine}（{order}）。"
        if cclass.get("engine_mode") == "TAIJI_ORDERED":
            taiji = cclass.get("taiji_context")
            if isinstance(taiji, Mapping):
                active = _active_taiji_leg(taiji)
                sequence = _TAIJI_SEQUENCE_ZH.get(str(active.get("sequence")) if active else "NONE", "段序未定")
                quality = ""
                if active:
                    raw_quality = active.get("copy_quality") if active.get("role") == "COPY" else active.get("correction_quality")
                    if raw_quality not in {None, "NOT_APPLICABLE", "UNKNOWN"}:
                        quality = f"，品質{_QUALITY_ZH.get(str(raw_quality), '未定')}"
                engine_line += (
                    f"目前{sequence}{quality}；"
                    f"{_ANCHOR_TIME_ZH.get(str(taiji.get('anchor_time_status')), '定錨時間效力未定')}；"
                    f"{_PREVIOUS_CONTEXT_ZH.get(str(taiji.get('previous_context_alignment')), '前世背景未定')}。"
                )
        elif cclass.get("engine_mode") == "YIZHI_MOMENTUM":
            momentum = cclass.get("momentum_context")
            if isinstance(momentum, Mapping):
                engine_line += (
                    f"目前{_MOMENTUM_STAGE_ZH.get(str(momentum.get('stage')), '動能階段未定')}，"
                    f"{_LOCATION_ZH.get(str(momentum.get('location')), '位置未定')}，"
                    f"品質{_QUALITY_ZH.get(str(momentum.get('quality')), '未定')}。"
                )
        else:
            engine_line += _clean_text(cclass.get("order_reason"))
        lines.append(engine_line)

    decision = state.get("decision_chain_context") if isinstance(state, Mapping) else None
    if isinstance(decision, Mapping):
        structure = decision.get("structure_context")
        opportunity = decision.get("opportunity_context")
        process = _PROCESS_STAGE_ZH.get(str(decision.get("process_stage")), "流程未定")
        if isinstance(structure, Mapping):
            lens = _LENS_ZH.get(str(structure.get("analysis_lens")), "主鏡頭未定")
            dna = _FAMILY_DNA_ZH.get(str(structure.get("family_dna")), "家族結構未定")
            confluences = "、".join(_CONFLUENCE_ZH.get(str(item), str(item)) for item in structure.get("confluences") or []) or "尚無共振"
            lines.append(f"整合流程：{process}；主鏡頭為{lens}；已確認{int(structure.get('confirmed_leg_count') or 0)}腳；{dna}；{confluences}。")
        if isinstance(opportunity, Mapping):
            grade = _COURSE_GRADE_ZH.get(str(opportunity.get("course_grade")), "機會等級未定")
            probability = _EVIDENCE_ZH.get(str(opportunity.get("probability_evidence")), "未定")
            payoff = _EVIDENCE_ZH.get(str(opportunity.get("payoff_evidence")), "未定")
            lines.append(f"機會品質：{grade}；成立證據{probability}、賠率證據{payoff}（質性條件，不是統計勝率）。")

    setup = _setup(state)
    pattern = _pattern(payload)
    setup_pattern = _STRATEGY_ZH.get(str(setup.get("pattern"))) if isinstance(setup, Mapping) else None
    strategy = setup_pattern if setup_pattern and setup_pattern != _STRATEGY_ZH["NONE"] else _clean_text(pattern.get("pattern") or "尚無主控戰法")
    setup_stage = _SETUP_STAGE_ZH.get(str(setup.get("stage")), _clean_text(pattern.get("status") or "觀望")) if isinstance(setup, Mapping) else _clean_text(pattern.get("status") or "觀望")
    playbook = _select_playbook(payload, state)
    style = ""
    if isinstance(playbook, Mapping):
        style = f"；執行採{_EXECUTION_STYLE_ZH.get(str(playbook.get('execution_style')), '尚未選定')}"
    lines.append(f"主控戰法：{strategy}（{setup_stage}）{style}。")
    lines.extend(_visible_course_lines(payload, snapshot=snapshot))
    if snapshot:
        opening = _opening_line(decision)
        if opening:
            lines.insert(0, opening)
    reversal = state.get("reversal_type") if isinstance(state, Mapping) else "NONE"
    maturity = state.get("maturity") if isinstance(state, Mapping) else "NONE"
    if reversal != "NONE" or maturity != "NONE":
        lines.append(f"反轉流程：{_REVERSAL_ZH.get(str(reversal), '反轉狀態未定')}；左右成熟度為{_MATURITY_ZH.get(str(maturity), '未定')}。")
    return _nonempty(lines[:7])


def _opening_line(decision: Any) -> str:
    if not isinstance(decision, Mapping):
        return ""
    opening = decision.get("opening_context")
    if not isinstance(opening, Mapping):
        return ""
    source = opening.get("cash_gap_source")
    if source == "UNAVAILABLE":
        return "開盤證據：現貨跳空資料不可得；不得以期貨價格冒充現貨開盤結論。"
    source_text = "現貨驗證" if source == "VERIFIED_CASH" else "期貨代理"
    prices = ""
    if opening.get("previous_cash_close") is not None and opening.get("cash_open") is not None:
        prices = f"（昨收{_number_text(opening.get('previous_cash_close'))}→開盤{_number_text(opening.get('cash_open'))}）"
    endpoint = ""
    side = opening.get("first_endpoint_side")
    if side in {"DH", "DL"}:
        endpoint = f"；第一次{'當日高點' if side == 'DH' else '當日低點'}突破{_ENDPOINT_STYLE_ZH.get(str(opening.get('first_endpoint_style')), '尚未確認')}"
    return (
        f"開盤證據：{source_text}{prices}，{_GAP_DIRECTION_ZH.get(str(opening.get('gap_direction')), '方向未定')}跳空、"
        f"幅度{_GAP_SIZE_ZH.get(str(opening.get('gap_size')), '未定')}；"
        f"{_OPENING_RELATION_ZH.get(str(opening.get('opening_direction_relation')), '關係未定')}；"
        f"開盤前K棒{_OPENING_QUALITY_ZH.get(str(opening.get('pre_cash_open_quality')), '未定')}{endpoint}。"
    )


def _visible_course_lines(payload: Mapping[str, Any], *, snapshot: bool) -> list[str]:
    values: list[str] = []
    sources: list[Any] = [payload.get("market_state"), _pattern(payload).get("details")]
    keywords = ("號盤", "箱型", "S／S／T／V", "開盤", "跳空", "端點")
    for source in sources:
        if not isinstance(source, Sequence) or isinstance(source, (str, bytes)):
            continue
        for item in source:
            text = _clean_text(item)
            if text and any(keyword in text for keyword in keywords) and text not in values:
                values.append(text)
    return [f"課程條件：{text}" for text in values[: 2 if snapshot else 1]]


def _scenario_action_lines(payload: Mapping[str, Any], state: Mapping[str, Any], *, kind: str) -> list[str]:
    lines: list[str] = []
    prospective = state.get("prospective_context") if isinstance(state, Mapping) else None
    primary: Mapping[str, Any] | None = None
    alternative: Mapping[str, Any] | None = None
    if isinstance(prospective, Mapping):
        primary_value = prospective.get("primary_hypothesis")
        alternative_value = prospective.get("alternative_hypothesis")
        primary = primary_value if isinstance(primary_value, Mapping) else None
        alternative = alternative_value if isinstance(alternative_value, Mapping) else None
    if primary:
        lines.append(_hypothesis_line("主要情境", primary))
    if alternative and kind in {SNAPSHOT, OBSERVATION, ARMED}:
        lines.append(_hypothesis_line("備用情境", alternative))
    if primary and alternative and kind in {SNAPSHOT, OBSERVATION}:
        lines.append(
            f"情境排序：{_DIRECTION_ZH.get(primary.get('direction'), '未定')}優先、"
            f"{_DIRECTION_ZH.get(alternative.get('direction'), '未定')}備用；這是條件排序，不是統計勝率。"
        )
    playbook = _select_playbook(payload, state)
    if kind == ARMED and isinstance(playbook, Mapping):
        lines.extend(_armed_lines(playbook))
    elif kind in {OBSERVATION, SNAPSHOT}:
        two_sided = _two_sided_trigger_line(prospective)
        if two_sided:
            lines.append(two_sided)
        missing = _first_text(payload.get("missing_conditions_or_trigger"))
        if missing:
            lines.append(f"下一步：{missing}")
    elif kind in {STOP, EXIT} and primary:
        next_scenario = _clean_text(primary.get("next_scenario"))
        if next_scenario:
            lines.append(f"退出後：{next_scenario}；不因出場自動反手。")
    reason = _clean_text(payload.get("notification_reason"))
    if reason:
        lines.append(f"本輪結論：{reason}")
    return _nonempty(lines[:7])


def _hypothesis_line(label: str, hypothesis: Mapping[str, Any]) -> str:
    direction = _DIRECTION_ZH.get(hypothesis.get("direction"), "未定")
    status = _HYPOTHESIS_STATUS_ZH.get(str(hypothesis.get("status")), "未定")
    confidence = _CONFIDENCE_ZH.get(str(hypothesis.get("confidence")), "未定")
    title = _clean_text(hypothesis.get("title"))
    thesis = _clean_text(hypothesis.get("thesis"))
    return f"{label}：{direction}／{status}／質性信心{confidence}；{title}。{thesis}"


def _armed_lines(playbook: Mapping[str, Any]) -> list[str]:
    wait = playbook.get("max_wait_bars")
    wait_text = "等待窗尚未啟動" if wait is None else f"最多等待{int(wait)}根已收盤一分K"
    return [
        f"觀察區：{_zone_text(playbook.get('observation_zone'))}；需看到{_clean_text(playbook.get('required_k_behavior'))}",
        f"已收盤觸發：{_clean_text(playbook.get('close_trigger'))}；{_clean_text(playbook.get('next_bar_entry'))}",
        f"結構停損：{_clean_text(playbook.get('structural_stop'))}；第一障礙{_zone_text(playbook.get('first_obstacle_zone'))}",
        f"應有行為：{_clean_text(playbook.get('expected_behavior'))}；{wait_text}；動機失效為{_clean_text(playbook.get('behavior_invalidation'))}",
        f"不追價：{_clean_text(playbook.get('no_chase'))}",
    ]


def _two_sided_trigger_line(prospective: Any) -> str:
    if not isinstance(prospective, Mapping):
        return ""
    parts = []
    long_playbook = prospective.get("long_playbook")
    short_playbook = prospective.get("short_playbook")
    if isinstance(long_playbook, Mapping) and long_playbook.get("status") != "UNAVAILABLE":
        parts.append(f"多方要看到：{_clean_text(long_playbook.get('close_trigger'), max_chars=120)}")
    if isinstance(short_playbook, Mapping) and short_playbook.get("status") != "UNAVAILABLE":
        parts.append(f"空方要看到：{_clean_text(short_playbook.get('close_trigger'), max_chars=120)}")
    return "；".join(parts)


def _position_lines(
    payload: Mapping[str, Any],
    *,
    kind: str,
    constitution_snapshot: Mapping[str, Any] | None,
) -> list[str]:
    event = payload.get("constitution_event")
    event = event if isinstance(event, Mapping) else {}
    existing = constitution_snapshot.get("simulated_position") if isinstance(constitution_snapshot, Mapping) else None
    position = existing if isinstance(existing, Mapping) else event
    direction = position.get("direction")
    entry = _number(position.get("entry_price_estimate"))
    stop = _number(position.get("stop_price_estimate"))
    risk = _number(position.get("risk_points"))
    lines: list[str] = []
    if direction in {"LONG", "SHORT"} and entry is not None:
        position_text = f"模擬{_DIRECTION_ZH[direction]}：進場約{entry:,.0f}點"
        if stop is not None:
            position_text += f"；原結構停損約{stop:,.0f}點"
        if risk is not None:
            position_text += f"；初始風險{risk:,.0f}點"
        lines.append(position_text)
        if kind == ENTRY and risk is not None:
            one_r = entry + risk if direction == "LONG" else entry - risk
            lines.append(f"一倍初始風險位置：約{one_r:,.0f}點。")
    reason = _clean_text(event.get("reason"))
    if kind in {STOP, EXIT} and reason:
        lines.append(f"出場原因：{reason}")
    risk_lines = payload.get("risk_and_nearest_obstacle")
    if isinstance(risk_lines, list):
        lines.extend(_clean_text(item) for item in risk_lines[:2])
    management = payload.get("single_contract_management_or_prohibition")
    if isinstance(management, list):
        lines.extend(_clean_text(item) for item in management[:2])
    state = _state(payload)
    playbook = _select_playbook(payload, state, preferred_direction=direction)
    if kind in {ENTRY, MANAGEMENT} and isinstance(playbook, Mapping):
        wait = playbook.get("max_wait_bars")
        wait_text = "等待窗尚未啟動" if wait is None else f"最多等待{int(wait)}根已收盤一分K"
        lines.append(f"應有行為：{_clean_text(playbook.get('expected_behavior'))}；{wait_text}。")
        lines.append(f"失效／退出：{_clean_text(playbook.get('behavior_invalidation'))}；{_clean_text(playbook.get('exit_plan'))}")
    return _nonempty(lines[:7])


def _select_playbook(
    payload: Mapping[str, Any],
    state: Mapping[str, Any],
    *,
    preferred_direction: Any = None,
) -> Mapping[str, Any] | None:
    prospective = state.get("prospective_context") if isinstance(state, Mapping) else None
    if not isinstance(prospective, Mapping):
        return None
    direction = preferred_direction
    setup = _setup(state)
    if direction not in {"LONG", "SHORT"} and isinstance(setup, Mapping):
        direction = setup.get("direction")
    primary = prospective.get("primary_hypothesis")
    if direction not in {"LONG", "SHORT"} and isinstance(primary, Mapping):
        direction = {"BULL": "LONG", "BEAR": "SHORT"}.get(primary.get("direction"))
    if direction == "LONG":
        value = prospective.get("long_playbook")
        return value if isinstance(value, Mapping) else None
    if direction == "SHORT":
        value = prospective.get("short_playbook")
        return value if isinstance(value, Mapping) else None
    # 中立／未定情境沒有主要執行路徑，不能靜默回退使用多方預案。
    return None


def _active_taiji_leg(taiji: Mapping[str, Any]) -> Mapping[str, Any] | None:
    active_id = taiji.get("active_leg_id")
    legs = taiji.get("legs")
    if not isinstance(legs, list):
        return None
    for leg in legs:
        if isinstance(leg, Mapping) and leg.get("leg_id") == active_id:
            return leg
    return None


def _setup(state: Mapping[str, Any]) -> Mapping[str, Any]:
    scenario = state.get("scenario_context") if isinstance(state, Mapping) else None
    setup = scenario.get("setup") if isinstance(scenario, Mapping) else None
    return setup if isinstance(setup, Mapping) else {}


def _pattern(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    value = payload.get("pattern_observation")
    return value if isinstance(value, Mapping) else {}


def _state(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    value = payload.get("market_structure_state")
    return value if isinstance(value, Mapping) and value.get("version") == 6 else {}


def _trend_class(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    return _clean_text(value.get("classification") if isinstance(value, Mapping) else "未定")


def _zone_text(value: Any) -> str:
    if not isinstance(value, Mapping):
        return "區域未定"
    low = _number(value.get("low"))
    high = _number(value.get("high"))
    if low is None or high is None:
        return _clean_text(value.get("reason") or "區域未定")
    if abs(low - high) < 1e-9:
        return f"{low:,.0f}點"
    return f"{min(low, high):,.0f}～{max(low, high):,.0f}點"


def _section(title: str, lines: Sequence[str]) -> list[str]:
    cleaned = _nonempty(lines)
    if not cleaned:
        return []
    output = [f"**{title}**"]
    output.extend(f"- {line}" for line in cleaned)
    output.append("")
    return output


def _first_text(value: Any) -> str:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            text = _clean_text(item)
            if text:
                return text
        return ""
    return _clean_text(value)


def _nonempty(lines: Sequence[Any]) -> list[str]:
    result: list[str] = []
    for line in lines:
        text = _clean_text(line, max_chars=420)
        if text and text not in result:
            result.append(text)
    return result


def _clean_text(value: Any, *, max_chars: int = 300) -> str:
    if value is None:
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    text = text.lstrip("- ").replace("**", "")
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip("，；。,. ") + "…"
    return text


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _number_text(value: Any) -> str:
    number = _number(value)
    return "點位未定" if number is None else f"{number:,.0f}點"


def _as_hhmm(value: Any) -> str:
    if not value:
        return "時間未定"
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime("%H:%M")
    except ValueError:
        match = re.search(r"\b(\d{2}:\d{2})\b", str(value))
        return match.group(1) if match else "時間未定"


def _v3_reentry_line(
    action: Mapping[str, Any],
    ledger: Mapping[str, Any],
) -> str | None:
    """Render program-owned re-entry availability ahead of AI prose."""

    constitution = ledger.get("program_constitution")
    if (
        isinstance(constitution, Mapping)
        and constitution.get("trading_locked") is True
    ):
        cooldown_until = constitution.get("cooldown_until")
        cooldown_text = _as_hhmm(cooldown_until) if cooldown_until else None
        return (
            "再進場：交易憲法已鎖定"
            + (f"至{cooldown_text}" if cooldown_text else "")
            + "；冷卻後仍須新的完整結構重新取得資格。"
        )
    if action.get("reentry_status") == "WAIT_ONE_BAR":
        return "再進場：等待1根完整1分K。"
    if action.get("reentry_status") == "AVAILABLE":
        return "再進場：資格可用；仍須重新形成合格觸發，不能直接重進。"
    return None


def _minutes_between(start: Any, end: Any) -> int | None:
    try:
        start_time = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        end_time = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except ValueError:
        return None
    minutes = int((end_time - start_time).total_seconds() // 60)
    return minutes if minutes >= 0 else None
