from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable, Mapping

from .constitution_state import ConstitutionStateError, validate_constitution_event
from .market_structure_state import (
    MarketStructureStateError,
    validate_market_structure_semantics,
    validate_market_structure_state,
)
from .presentation_zh import annotate_reversal_types, localize_user_text
from .strategy_catalog import EXECUTION_STYLE_LABELS_ZH, STRATEGY_METHOD_LABELS_ZH


DISCLAIMER = "一般技術分析，非個人化投資建議；遠端畫面可能延遲，非交易所等級即時訊號。"
QUIET_REASON_MAX_CHARS = 120
CONTRACT_VERSION = 8
LARGE_TRENDS = {"強勢偏多", "偏多但回檔", "盤整", "偏空但反彈", "強勢偏空", "資料不足"}
CURRENT_TRENDS = {"偏多", "偏空", "盤整", "轉換中"}
PATTERN_STATUSES = {"觀望", "條件式偏多", "條件式偏空", "可評估進場", "不宜追價／禁止"}
DECISIONS = {"NOTIFY", "DONT_NOTIFY"}
REQUIRED_KEYS = (
    "original_decision",
    "notification_reason",
    "latest_closed_k_price_estimate",
    "latest_closed_k_details",
    "large_trend",
    "current_trend",
    "market_state",
    "pattern_observation",
    "missing_conditions_or_trigger",
    "entry_and_structural_stop",
    "risk_and_nearest_obstacle",
    "single_contract_management_or_prohibition",
)
INTERNAL_KEY = "constitution_event"
MARKET_STRUCTURE_KEY = "market_structure_state"


class AnalysisValidationError(ValueError):
    pass


def _clean_text(value: Any, *, localize: bool = True) -> str:
    if not isinstance(value, str):
        raise AnalysisValidationError("analysis text values must be strings")
    text = value.replace("**", "").replace("__", "")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^(?:[-*#>]\s*)+", "", text)
    if not text:
        raise AnalysisValidationError("analysis text values must not be empty")
    return localize_user_text(text) if localize else text


def _is_disclaimer(text: str) -> bool:
    compact = text.replace(" ", "")
    return "一般技術分析" in compact and "投資建議" in compact


def _clean_price_estimate(value: Any) -> str:
    text = _clean_text(value)
    text = re.sub(
        r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?:\s*（[^）]*）)?\s*[；;,:，]?\s*",
        "",
        text,
    )
    text = re.sub(r"^\d{2}:\d{2}\s*[；;,:，]?\s*", "", text)
    if re.search(r"\b\d{1,2}:\d{2}\b", text):
        raise AnalysisValidationError("price estimate must not contain a K-bar time")
    return text


def _clean_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise AnalysisValidationError(f"{field} must be a non-empty list")
    cleaned = [_clean_text(item) for item in value]
    cleaned = [item for item in cleaned if not _is_disclaimer(item)]
    if not cleaned:
        raise AnalysisValidationError(f"{field} must contain analysis, not only a disclaimer")
    return cleaned


def validate_analysis_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise AnalysisValidationError("analysis payload must be an object")
    actual = set(payload)
    required = set(REQUIRED_KEYS)
    optional_internal = {INTERNAL_KEY, MARKET_STRUCTURE_KEY}
    if not required.issubset(actual) or not actual.issubset(required | optional_internal):
        raise AnalysisValidationError(
            f"invalid top-level fields; missing={sorted(required - actual)}, extra={sorted(actual - required - optional_internal)}"
        )

    large = payload["large_trend"]
    current = payload["current_trend"]
    pattern = payload["pattern_observation"]
    if not isinstance(large, Mapping) or set(large) != {"classification", "details"}:
        raise AnalysisValidationError("large_trend has invalid fields")
    if not isinstance(current, Mapping) or set(current) != {"classification", "details"}:
        raise AnalysisValidationError("current_trend has invalid fields")
    if not isinstance(pattern, Mapping) or set(pattern) != {"status", "pattern", "details"}:
        raise AnalysisValidationError("pattern_observation has invalid fields")

    decision = _clean_text(payload["original_decision"], localize=False)
    large_classification = _clean_text(large["classification"])
    current_classification = _clean_text(current["classification"])
    pattern_status = _clean_text(pattern["status"])
    if decision not in DECISIONS:
        raise AnalysisValidationError("invalid original decision")
    if large_classification not in LARGE_TRENDS:
        raise AnalysisValidationError("invalid large trend classification")
    if current_classification not in CURRENT_TRENDS:
        raise AnalysisValidationError("invalid current trend classification")
    if pattern_status not in PATTERN_STATUSES:
        raise AnalysisValidationError("invalid pattern status")

    constitution_event = None
    if INTERNAL_KEY in payload and payload[INTERNAL_KEY] is not None:
        try:
            constitution_event = validate_constitution_event(payload[INTERNAL_KEY])
        except ConstitutionStateError as exc:
            raise AnalysisValidationError(str(exc)) from exc
        if constitution_event["event_type"] != "NONE" and decision != "NOTIFY":
            raise AnalysisValidationError("constitution state changes require original_decision=NOTIFY")

    market_structure_state = None
    if MARKET_STRUCTURE_KEY in payload and payload[MARKET_STRUCTURE_KEY] is not None:
        try:
            market_structure_state = validate_market_structure_semantics(payload[MARKET_STRUCTURE_KEY])
        except MarketStructureStateError as exc:
            raise AnalysisValidationError(str(exc)) from exc

    result = {
        "original_decision": decision,
        "notification_reason": _clean_text(payload["notification_reason"]),
        "latest_closed_k_price_estimate": _clean_price_estimate(payload["latest_closed_k_price_estimate"]),
        "latest_closed_k_details": _clean_list(payload["latest_closed_k_details"], "latest_closed_k_details"),
        "large_trend": {
            "classification": large_classification,
            "details": _clean_list(large["details"], "large_trend.details"),
        },
        "current_trend": {
            "classification": current_classification,
            "details": _clean_list(current["details"], "current_trend.details"),
        },
        "market_state": _clean_list(payload["market_state"], "market_state"),
        "pattern_observation": {
            "status": pattern_status,
            "pattern": _clean_text(pattern["pattern"]),
            "details": _clean_list(pattern["details"], "pattern_observation.details"),
        },
        "missing_conditions_or_trigger": _clean_list(payload["missing_conditions_or_trigger"], "missing_conditions_or_trigger"),
        "entry_and_structural_stop": _clean_list(payload["entry_and_structural_stop"], "entry_and_structural_stop"),
        "risk_and_nearest_obstacle": _clean_list(payload["risk_and_nearest_obstacle"], "risk_and_nearest_obstacle"),
        "single_contract_management_or_prohibition": _clean_list(
            payload["single_contract_management_or_prohibition"],
            "single_contract_management_or_prohibition",
        ),
        "constitution_event": constitution_event,
        "market_structure_state": market_structure_state,
    }
    _validate_directional_structure_alignment(result)
    return result


def _validate_directional_structure_alignment(data: Mapping[str, Any]) -> None:
    state = data.get("market_structure_state")
    if not isinstance(state, Mapping):
        return
    large_direction = {
        "強勢偏多": "BULL",
        "偏多但回檔": "BULL",
        "偏空但反彈": "BEAR",
        "強勢偏空": "BEAR",
    }.get(data["large_trend"]["classification"])
    current_direction = {
        "偏多": "BULL",
        "偏空": "BEAR",
    }.get(data["current_trend"]["classification"])
    if large_direction is not None:
        direction_key = large_direction.lower()
        defenses = state["defense_lines"]
        same_direction_defenses = (
            defenses[f"small_{direction_key}"],
            defenses[f"large_{direction_key}"],
        )
        if not any(item is not None and item["status"] == "ACTIVE" for item in same_direction_defenses):
            raise AnalysisValidationError(
                "directional large trend requires at least one active same-direction Dow defense"
            )
    if current_direction is not None and state["dow_state_small"] != current_direction:
        raise AnalysisValidationError(
            "directional current trend requires the same confirmed small-grade Dow direction"
        )

    anchor_context = state.get("anchor_context")
    if not isinstance(anchor_context, Mapping):
        return
    anchors = {
        item["anchor_id"]: item
        for item in anchor_context.get("anchors", [])
        if isinstance(item, Mapping) and isinstance(item.get("anchor_id"), str)
    }
    active_large_id = anchor_context.get("active_large_anchor_id")
    active_small_id = anchor_context.get("active_small_anchor_id")
    active_large = anchors.get(active_large_id)
    active_small = anchors.get(active_small_id)

    if active_large is not None and active_large.get("status") == "CONFIRMED":
        expected_large_trends = {
            "BULL": {"強勢偏多", "偏多但回檔"},
            "BEAR": {"偏空但反彈", "強勢偏空"},
        }[active_large["direction"]]
        if data["large_trend"]["classification"] not in expected_large_trends:
            raise AnalysisValidationError(
                "an active confirmed large anchor must remain the directional large-trend background"
            )

        cclass = state.get("cclass_context")
        if isinstance(cclass, Mapping):
            taiji = cclass.get("taiji_context")
            legs = taiji.get("legs", []) if isinstance(taiji, Mapping) else []
            if cclass.get("order_state") == "UNDEFINED" or not legs:
                raise AnalysisValidationError(
                    "an active confirmed large anchor requires a retained causal Taiji leg assessment"
                )
            if taiji.get("anchor_id") == active_large_id:
                anchor_legs = [item for item in legs if item.get("sequence") == "ANCHOR_1"]
                if (
                    not anchor_legs
                    or anchor_legs[0].get("start_bar_time") != active_large.get("start_bar_time")
                ):
                    raise AnalysisValidationError(
                        "the Taiji first anchor leg must start with its active large anchor"
                    )

    if (
        active_large is not None
        and active_small is not None
        and anchor_context.get("grade_relation") == "CONFLICT"
    ):
        prospective = state.get("prospective_context")
        if isinstance(prospective, Mapping):
            scenario_directions = {
                prospective.get("primary_hypothesis", {}).get("direction"),
                prospective.get("alternative_hypothesis", {}).get("direction"),
            }
            if scenario_directions != {active_large["direction"], active_small["direction"]}:
                raise AnalysisValidationError(
                    "conflicting large/small anchors require both directional hypotheses"
                )


def _section(title: str, lines: Iterable[str]) -> list[str]:
    output = [f"**{title}**", ""]
    output.extend(f"- {line}" for line in lines)
    output.append("")
    return output


_HYPOTHESIS_STATUS_ZH = {
    "POTENTIAL": "潛在演化",
    "STRENGTHENING": "情境增強",
    "NEAR_CONFIRMATION": "接近確認",
    "CONFIRMED": "正式確認",
    "DEGRADED": "情境降級",
    "CANCELLED": "情境取消",
    "UNDEFINED": "尚未建立",
}
_CONFIDENCE_ZH = {"HIGH": "高", "MEDIUM": "中", "LOW": "低", "UNAVAILABLE": "未評定"}
_METHOD_ZH = {
    "OPENING_RANGE": "開盤區間",
    "NUMBER_BOARD": "號盤",
    "BOX": "箱型",
    "PIVOT": "樞紐",
    "DOW_STRUCTURE": "道氏結構",
    "LEFT_RIGHT": "左右戰法",
    "ANCHOR": "定錨",
    "TAIJI": "太極",
    "YIZHI": "一之戰法",
    "QUADRANT": "四象限",
    "CCLASS": "戰法C",
    "XPROCESS": "整合決策流程",
    "ORIGINAL_PATTERN": "原四型態",
}
_PLAYBOOK_STATUS_ZH = {
    "WAITING_STRUCTURE": "等待結構",
    "WATCHING": "觀察中",
    "ARMED": "待觸發",
    "PROHIBITED": "禁止執行",
    "UNAVAILABLE": "資料不足",
}
_PATTERN_ZH = STRATEGY_METHOD_LABELS_ZH

_TAIJI_SEQUENCE_ZH = {
    "NONE": "尚無作用中段落",
    "ANCHOR_1": "第一段定錨",
    "CORRECTION_2": "第二段修正",
    "COPY_3": "第三段複製",
    "CORRECTION_4": "第四段修正",
    "COPY_5": "第五段複製",
    "POST_5": "第五段後續演化",
}
_TAIJI_ASSESSMENT_ZH = {
    "STRENGTHENING": "原方向增強",
    "HEALTHY": "原方向健康",
    "WEAKENING": "原方向弱化",
    "REVERSAL_RISK": "變盤風險升高",
    "UNDEFINED": "尚未評定",
}
_QUALITY_ZH = {
    "STRONG": "強",
    "ACCEPTABLE": "合格",
    "WEAK": "弱",
    "FAILED": "失敗",
    "UNKNOWN": "未定",
}
_AMPLITUDE_TREND_ZH = {
    "EXPANDING": "幅度擴大",
    "STABLE": "幅度穩定",
    "CONTRACTING": "幅度縮短",
    "UNDEFINED": "幅度未定",
}
_DURATION_TREND_ZH = {
    "LONGER": "時間拉長",
    "STABLE": "時間穩定",
    "SHORTER": "時間縮短",
    "UNDEFINED": "時間未定",
}


def _join_conditions(items: list[str]) -> str:
    return "；".join(items)


def _zone_text(zone: Mapping[str, Any]) -> str:
    if zone["reliability"] == "UNAVAILABLE":
        return f"點位未定（{zone['reason']}）"
    low = zone["low"]
    high = zone["high"]
    reliability = "直接資料" if zone["reliability"] == "DIRECT" else "圖面估計"
    if low == high:
        return f"約{low:g}點（{reliability}）"
    return f"約{low:g}～{high:g}點（{reliability}）"


def _defense_additions(state: Mapping[str, Any] | None) -> dict[str, list[str]]:
    additions = {"large": [], "current": []}
    if not isinstance(state, Mapping):
        return additions
    normalized = validate_market_structure_state(state)
    levels = {
        "small": ("current", normalized["primary_pivots"]),
        "large": ("large", normalized["secondary_pivots"]),
    }
    for level, (section, pivots) in levels.items():
        by_id = {item["pivot_id"]: item for item in pivots}
        for direction, label, pivot_label in (
            ("bull", "多頭", "樞紐低點"),
            ("bear", "空頭", "樞紐高點"),
        ):
            defense = normalized["defense_lines"][f"{level}_{direction}"]
            if defense is None or defense["status"] != "ACTIVE":
                continue
            pivot = by_id.get(defense["pivot_id"])
            if pivot is None:
                continue
            pivot_time = datetime.fromisoformat(pivot["bar_time"]).strftime("%H:%M")
            price = defense["price_estimate"]
            price_text = "價位暫無可靠估計" if price is None else f"約{price:g}點（圖面估計）"
            grade_label = "小級" if level == "small" else "大級"
            additions[section].append(
                f"{grade_label}{label}防線已成立：來源為 {pivot_time} 的{pivot_label}，防線{price_text}。"
            )
    return additions


def _hypothesis_line(prefix: str, hypothesis: Mapping[str, Any]) -> str:
    status = _HYPOTHESIS_STATUS_ZH[hypothesis["status"]]
    confidence = _CONFIDENCE_ZH[hypothesis["confidence"]]
    return f"{prefix}：{hypothesis['title']}；階段為{status}，質性信心{confidence}。{hypothesis['thesis']}"


def _playbook_lines(label: str, playbook: Mapping[str, Any]) -> list[str]:
    status = _PLAYBOOK_STATUS_ZH[playbook["status"]]
    pattern = _PATTERN_ZH[playbook["mapped_pattern"]]
    style = EXECUTION_STYLE_LABELS_ZH[playbook["execution_style"]]
    first = (
        f"{label}預案（{status}）：主控戰法為{pattern}，執行採{style}；"
        f"選用理由：{playbook['method_selection_reason']}；觀察{_zone_text(playbook['observation_zone'])}；"
        f"需看到{playbook['required_k_behavior']}；已收盤觸發為{playbook['close_trigger']}；"
        f"{playbook['next_bar_entry']}"
    )
    second = (
        f"{label}風控：{playbook['structural_stop']}；第一障礙{_zone_text(playbook['first_obstacle_zone'])}；"
        f"{playbook['no_chase']}"
    )
    return [first, second]


def _taiji_lines(evolution: Mapping[str, Any]) -> tuple[list[str], list[str], list[str]]:
    sequence = _TAIJI_SEQUENCE_ZH[evolution["active_sequence"]]
    assessment = _TAIJI_ASSESSMENT_ZH[evolution["structural_assessment"]]
    direction = {"BULL": "多方", "BEAR": "空方", None: "方向未定"}[evolution["active_direction"]]
    copies = "、".join(_QUALITY_ZH[item] for item in evolution["copy_outcomes"]) or "尚無完整複製"
    corrections = "、".join(_QUALITY_ZH[item] for item in evolution["correction_outcomes"]) or "尚無完整修正"
    market = [
        f"太極演化：{direction}{sequence}；結構評估為{assessment}。{evolution['interpretation']}"
    ]
    pattern = [
        "太極相對變化："
        f"複製結果依序為{copies}，{_AMPLITUDE_TREND_ZH[evolution['copy_amplitude_trend']]}、"
        f"{_DURATION_TREND_ZH[evolution['copy_duration_trend']]}；"
        f"修正結果依序為{corrections}，{_AMPLITUDE_TREND_ZH[evolution['correction_amplitude_trend']]}、"
        f"{_DURATION_TREND_ZH[evolution['correction_duration_trend']]}。"
    ]
    missing = [
        f"太極延續條件：{evolution['continuation_condition']}。",
        f"太極變盤條件：{evolution['regime_change_condition']}。",
    ]
    return market, pattern, missing


def _prospective_additions(state: Mapping[str, Any] | None) -> dict[str, list[str]]:
    empty = {key: [] for key in ("large", "current", "market", "pattern", "missing", "entry", "risk", "management")}
    if not isinstance(state, Mapping) or state.get("version") != 6:
        return empty
    prospective = state["prospective_context"]
    primary = prospective["primary_hypothesis"]
    alternative = prospective["alternative_hypothesis"]
    long_playbook = prospective["long_playbook"]
    short_playbook = prospective["short_playbook"]
    taiji_evolution = prospective["taiji_evolution"]

    anchor = state["anchor_context"]
    by_id = {item["anchor_id"]: item for item in anchor["anchors"]}
    for field, label, target in (
        ("active_large_anchor_id", "作用中大錨", "large"),
        ("active_small_anchor_id", "作用中小錨", "current"),
    ):
        anchor_id = anchor[field]
        if anchor_id is None:
            empty[target].append(f"{label}尚未確認；目前不得以未確認轉折冒充工作錨。")
            continue
        item = by_id[anchor_id]
        direction = "多方" if item["direction"] == "BULL" else "空方"
        status = {"FORMING": "形成中", "CONFIRMED": "已確認"}.get(item["status"], "已終止")
        start_price = item["start_price_estimate"]
        extreme_price = item["extreme_price_estimate"]
        start_price_text = "點位未定" if start_price is None else f"約{start_price:g}點"
        extreme_price_text = "點位未定" if extreme_price is None else f"約{extreme_price:g}點"
        start_hhmm = item["start_bar_time"][11:16]
        extreme_hhmm = item["extreme_bar_time"][11:16]
        parent_text = ""
        if item["parent_anchor_id"] is not None:
            parent = by_id.get(item["parent_anchor_id"])
            if parent is not None:
                parent_text = f"；父級為{parent['start_bar_time'][11:16]}起始的大錨"
        empty[target].append(
            f"{label}：{start_hhmm} {start_price_text}起始的{direction}錨，{status}，"
            f"延伸至{extreme_hhmm} {extreme_price_text}（圖面估計）{parent_text}；{item['notes'][0]}"
        )

    empty["market"].append(_hypothesis_line("主要盤勢推演", primary))
    empty["market"].append(_hypothesis_line("備用盤勢推演", alternative))
    taiji_market, taiji_pattern, taiji_missing = _taiji_lines(taiji_evolution)
    empty["market"].extend(taiji_market)
    empty["pattern"].extend(taiji_pattern)
    methods = primary["supporting_methods"]
    if methods:
        empty["pattern"].append("主情境整合證據：" + "、".join(_METHOD_ZH[item] for item in methods) + "。")
    if primary["conflicting_evidence"]:
        empty["pattern"].append("主情境限制：" + _join_conditions(primary["conflicting_evidence"]) + "。")
    empty["missing"].extend(
        [
            "主要情境確認：" + _join_conditions(primary["confirmation_conditions"]) + "。",
            "主要情境降級／取消："
            + _join_conditions(primary["downgrade_conditions"] + primary["cancellation_conditions"])
            + f"；取消後改查：{primary['next_scenario']}。",
            "備用情境確認：" + _join_conditions(alternative["confirmation_conditions"]) + "。",
        ]
    )
    empty["missing"].extend(taiji_missing)
    empty["entry"].extend(_playbook_lines("偏多", long_playbook))
    empty["entry"].extend(_playbook_lines("偏空", short_playbook))

    primary_playbook = long_playbook if primary["direction"] == "BULL" else short_playbook
    if primary["direction"] not in {"BULL", "BEAR"}:
        primary_playbook = long_playbook
    empty["risk"].append(
        "主要執行路徑第一障礙：" + _zone_text(primary_playbook["first_obstacle_zone"]) + "。"
    )
    wait = primary_playbook["max_wait_bars"]
    wait_text = "尚未啟動等待窗" if wait is None else f"最多等待{wait}根已收盤一分K"
    empty["management"].append(
        f"進場後應有行為：{primary_playbook['expected_behavior']}；{wait_text}；"
        f"動機失效為{primary_playbook['behavior_invalidation']}；退出計畫為{primary_playbook['exit_plan']}"
    )
    empty["management"].append("看法切換：" + primary_playbook["switch_condition"])
    return empty


def render_analysis_markdown(
    payload: Mapping[str, Any],
    context: Mapping[str, Any],
    *,
    resumed: bool,
) -> str:
    data = validate_analysis_payload(payload)
    latest = _clean_text(context["expected_latest_closed_k_hhmm"])
    current = _clean_text(context["current_unclosed_k_hhmm"])
    fixed_times = {latest, current}
    if any(any(fixed in detail for fixed in fixed_times) for detail in data["latest_closed_k_details"]):
        raise AnalysisValidationError("latest_closed_k_details must not duplicate adapter-owned K-bar times")
    time_lines: list[str] = []
    if resumed:
        time_lines.append("**監控已恢復**。")
    time_lines.extend(
        [
            f"{latest}；{data['latest_closed_k_price_estimate']}",
            f"{current} 為未收盤即時 K，只供觀察。",
        ]
    )
    new_bar_count = int(context.get("new_closed_bar_count") or 1)
    if new_bar_count > 1:
        time_lines.append(
            f"距上次成功處理新增約 {new_bar_count} 根已收盤 K；"
            "已使用畫面中可見的歷史 K 完整重建大趨勢、當前趨勢、定錨、防線與目前情境；"
            "僅不把中斷期間已走完的歷史訊號追認為模擬成交。"
        )
    time_lines.extend(data["latest_closed_k_details"])
    additions = _prospective_additions(data.get("market_structure_state"))
    defense_additions = _defense_additions(data.get("market_structure_state"))

    output: list[str] = []
    output += _section("時間／最新已收盤 K", time_lines)
    output += _section(
        "大趨勢",
        [
            f"**{data['large_trend']['classification']}**",
            *data["large_trend"]["details"],
            *defense_additions["large"],
            *additions["large"],
        ],
    )
    output += _section(
        "當前趨勢",
        [
            f"**{data['current_trend']['classification']}**",
            *data["current_trend"]["details"],
            *defense_additions["current"],
            *additions["current"],
        ],
    )
    output += _section("市場狀態", [*data["market_state"], *additions["market"]])
    pattern = data["pattern_observation"]
    output += _section(
        "觀察型態與狀態",
        [f"**{pattern['status']}：{pattern['pattern']}**", *pattern["details"], *additions["pattern"]],
    )
    output += _section(
        "尚缺條件／觸發", [*data["missing_conditions_or_trigger"], *additions["missing"]]
    )
    output += _section(
        "進場與結構停損", [*data["entry_and_structural_stop"], *additions["entry"]]
    )
    output += _section(
        "一倍初始風險與最近障礙", [*data["risk_and_nearest_obstacle"], *additions["risk"]]
    )
    output += _section(
        "單口管理／禁止原因",
        [*data["single_contract_management_or_prohibition"], *additions["management"]],
    )
    output.append(DISCLAIMER)
    return annotate_reversal_types("\n".join(output).rstrip() + "\n")


def render_quiet_status_markdown(
    payload: Mapping[str, Any],
    context: Mapping[str, Any],
) -> str:
    """Render the one canonical quiet message used by every delivery channel."""
    data = validate_analysis_payload(payload)
    latest = _clean_text(context["expected_latest_closed_k_hhmm"])
    pattern = data["pattern_observation"]
    reason = data["notification_reason"]
    if len(reason) > QUIET_REASON_MAX_CHARS:
        reason = reason[: QUIET_REASON_MAX_CHARS - 1].rstrip("，；。,. ") + "…"
    status = (
        f"{latest}｜大趨勢：{data['large_trend']['classification']}｜"
        f"當前趨勢：{data['current_trend']['classification']}｜"
        f"{pattern['status']}：{pattern['pattern']}。{reason}"
    )
    return annotate_reversal_types(f"{status}\n{DISCLAIMER}\n")


def render_quiet_unavailable_markdown(
    context: Mapping[str, Any],
    reason: str,
) -> str:
    """Render a short fail-safe status when a non-notifying round has no analysis."""
    latest = _clean_text(context["expected_latest_closed_k_hhmm"])
    cleaned_reason = _clean_text(reason)
    if len(cleaned_reason) > QUIET_REASON_MAX_CHARS:
        cleaned_reason = cleaned_reason[: QUIET_REASON_MAX_CHARS - 1].rstrip("，；。,. ") + "…"
    return annotate_reversal_types(f"{latest}｜觀望：{cleaned_reason}\n{DISCLAIMER}\n")


def render_unavailable_markdown(
    context: Mapping[str, Any],
    reason: str,
    *,
    resumed: bool,
) -> str:
    latest = _clean_text(context["expected_latest_closed_k_hhmm"])
    current = _clean_text(context["current_unclosed_k_hhmm"])
    reason = _clean_text(reason)
    time_lines = []
    if resumed:
        time_lines.append("**監控已恢復**。")
    time_lines.extend([f"排程預期最新已收盤 K 為 {latest}。", f"{current} 為未收盤即時 K。"])
    sections = (
        ("時間／最新已收盤 K", time_lines),
        ("大趨勢", ["**資料不足**", reason]),
        ("當前趨勢", ["**轉換中**", "本輪不進行盤勢方向確認。"]),
        ("市場狀態", ["圖表時效或結構化分析未通過驗證。"]),
        ("觀察型態與狀態", ["**觀望：資料驗證未通過**"]),
        ("尚缺條件／觸發", ["等待下一張通過時間與新鮮度驗證的純圖表。"]),
        ("進場與結構停損", ["本輪不提供進場與停損價位。"]),
        ("一倍初始風險與最近障礙", ["資料不足，無法計算。"]),
        ("單口管理／禁止原因", ["**禁止依本輪資料建立真實或模擬持倉。**"]),
    )
    output: list[str] = []
    for title, lines in sections:
        output += _section(title, lines)
    output.append(DISCLAIMER)
    return annotate_reversal_types("\n".join(output).rstrip() + "\n")


def analysis_state_summary(payload: Mapping[str, Any]) -> dict[str, str]:
    data = validate_analysis_payload(payload)
    return {
        "large_trend": data["large_trend"]["classification"],
        "current_trend": data["current_trend"]["classification"],
        "pattern_status": data["pattern_observation"]["status"],
        "pattern": data["pattern_observation"]["pattern"],
        "notification_reason": data["notification_reason"],
    }
