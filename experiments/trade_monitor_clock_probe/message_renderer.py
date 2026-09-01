from __future__ import annotations

import re
from typing import Any, Iterable, Mapping


DISCLAIMER = "一般技術分析，非個人化投資建議；遠端畫面可能延遲，非交易所等級即時訊號。"

LARGE_TRENDS = {
    "強勢偏多",
    "偏多但回檔",
    "盤整",
    "偏空但反彈",
    "強勢偏空",
    "資料不足",
}
CURRENT_TRENDS = {"偏多", "偏空", "盤整", "轉換中"}
PATTERN_STATUSES = {
    "觀望",
    "條件式偏多",
    "條件式偏空",
    "可評估進場",
    "不宜追價／禁止",
}

REQUIRED_KEYS = (
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


class AnalysisValidationError(ValueError):
    pass


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        raise AnalysisValidationError("analysis text values must be strings")
    text = value.replace("**", "").replace("__", "")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^(?:[-*#>]\s*)+", "", text)
    if not text:
        raise AnalysisValidationError("analysis text values must not be empty")
    return text


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
    if actual != required:
        missing = sorted(required - actual)
        extra = sorted(actual - required)
        raise AnalysisValidationError(f"invalid top-level fields; missing={missing}, extra={extra}")

    large = payload["large_trend"]
    current = payload["current_trend"]
    pattern = payload["pattern_observation"]
    if not isinstance(large, Mapping) or set(large) != {"classification", "details"}:
        raise AnalysisValidationError("large_trend has invalid fields")
    if not isinstance(current, Mapping) or set(current) != {"classification", "details"}:
        raise AnalysisValidationError("current_trend has invalid fields")
    if not isinstance(pattern, Mapping) or set(pattern) != {"status", "pattern", "details"}:
        raise AnalysisValidationError("pattern_observation has invalid fields")

    large_classification = _clean_text(large["classification"])
    current_classification = _clean_text(current["classification"])
    pattern_status = _clean_text(pattern["status"])
    if large_classification not in LARGE_TRENDS:
        raise AnalysisValidationError("invalid large trend classification")
    if current_classification not in CURRENT_TRENDS:
        raise AnalysisValidationError("invalid current trend classification")
    if pattern_status not in PATTERN_STATUSES:
        raise AnalysisValidationError("invalid pattern status")

    return {
        "latest_closed_k_price_estimate": _clean_price_estimate(
            payload["latest_closed_k_price_estimate"]
        ),
        "latest_closed_k_details": _clean_list(
            payload["latest_closed_k_details"], "latest_closed_k_details"
        ),
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
        "missing_conditions_or_trigger": _clean_list(
            payload["missing_conditions_or_trigger"], "missing_conditions_or_trigger"
        ),
        "entry_and_structural_stop": _clean_list(
            payload["entry_and_structural_stop"], "entry_and_structural_stop"
        ),
        "risk_and_nearest_obstacle": _clean_list(
            payload["risk_and_nearest_obstacle"], "risk_and_nearest_obstacle"
        ),
        "single_contract_management_or_prohibition": _clean_list(
            payload["single_contract_management_or_prohibition"],
            "single_contract_management_or_prohibition",
        ),
    }


def _section(title: str, lines: Iterable[str]) -> list[str]:
    output = [f"**{title}**", ""]
    output.extend(f"- {line}" for line in lines)
    output.append("")
    return output


def render_analysis_markdown(payload: Mapping[str, Any], context: Mapping[str, Any]) -> str:
    data = validate_analysis_payload(payload)
    latest = _clean_text(context["expected_latest_closed_k_hhmm"])
    current = _clean_text(context["current_unclosed_k_hhmm"])
    price = data["latest_closed_k_price_estimate"]

    output: list[str] = []
    output += _section(
        "時間／最新已收盤 K",
        [
            f"{latest}；{price}",
            f"{current} 為未收盤即時 K，只供觀察。",
            *data["latest_closed_k_details"],
        ],
    )
    output += _section(
        "大趨勢",
        [
            f"**{data['large_trend']['classification']}**",
            *data["large_trend"]["details"],
        ],
    )
    output += _section(
        "當前趨勢",
        [
            f"**{data['current_trend']['classification']}**",
            *data["current_trend"]["details"],
        ],
    )
    output += _section("市場狀態", data["market_state"])
    pattern = data["pattern_observation"]
    output += _section(
        "觀察型態與狀態",
        [f"**{pattern['status']}：{pattern['pattern']}**", *pattern["details"]],
    )
    output += _section("尚缺條件／觸發", data["missing_conditions_or_trigger"])
    output += _section("進場與結構停損", data["entry_and_structural_stop"])
    output += _section("1R與最近障礙", data["risk_and_nearest_obstacle"])
    output += _section(
        "單口管理／禁止原因",
        data["single_contract_management_or_prohibition"],
    )
    output.append(DISCLAIMER)
    return "\n".join(output).rstrip() + "\n"


def render_unavailable_markdown(context: Mapping[str, Any], reason: str) -> str:
    latest = _clean_text(context["expected_latest_closed_k_hhmm"])
    current = _clean_text(context["current_unclosed_k_hhmm"])
    reason = _clean_text(reason)
    sections = (
        ("時間／最新已收盤 K", [f"排程預期最新已收盤 K 為 {latest}。", f"{current} 為未收盤即時 K。"]),
        ("大趨勢", ["**資料不足**", reason]),
        ("當前趨勢", ["**轉換中**", "本輪不進行盤勢方向確認。"]),
        ("市場狀態", ["圖表時效或事件狀態未通過驗證。"]),
        ("觀察型態與狀態", ["**觀望：資料驗證未通過**"]),
        ("尚缺條件／觸發", ["等待下一張通過時間與新鮮度驗證的純圖表。"]),
        ("進場與結構停損", ["本輪不提供進場與停損價位。"]),
        ("1R與最近障礙", ["資料不足，無法計算。"]),
        ("單口管理／禁止原因", ["**禁止依本輪資料建立真實或模擬持倉。**"]),
    )
    output: list[str] = []
    for title, lines in sections:
        output += _section(title, lines)
    output.append(DISCLAIMER)
    return "\n".join(output).rstrip() + "\n"
