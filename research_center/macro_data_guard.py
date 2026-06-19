from __future__ import annotations

from typing import Any


INDEX_ABS_DAILY_PCT_ALERT = 8.0
TW_INDEX_DAILY_POINTS_ALERT = 2000.0
TW_INDEX_FIVE_DAY_POINTS_ALERT = 5000.0
GLOBAL_INDEX_DAILY_PCT_ALERT = 6.0


def build_macro_data_guard(structured_data: dict[str, Any]) -> dict[str, Any]:
    """Build a verifiable macro numeric layer for AI prompts and reports."""
    quantitative = structured_data.get("quantitative_market") or {}
    indices = quantitative.get("indices") or {}
    global_macro = quantitative.get("global_public_macro") or {}
    volatility = quantitative.get("volatility") or {}
    cash_flow = quantitative.get("official_cash_institutional_flow") or {}
    futures = quantitative.get("official_futures_institutional") or {}

    facts: list[dict[str, Any]] = []
    alerts: list[dict[str, Any]] = []
    missing: list[str] = []

    for label, metrics in indices.items():
        _add_index_facts(facts, alerts, str(label), metrics, market="tw")
    for label, metrics in global_macro.items():
        if label == "policy" or not isinstance(metrics, dict):
            continue
        _add_index_facts(facts, alerts, str(label), metrics, market="global")

    _add_volatility_facts(facts, missing, volatility)
    _add_cash_flow_facts(facts, missing, cash_flow)
    _add_futures_facts(facts, missing, futures)

    if not indices:
        missing.append("缺少台股主要指數硬數據。")
    if not global_macro:
        missing.append("缺少全球主要股匯商品 proxy 硬數據。")

    policy = {
        "hard_number_rule": "AI 使用指數點數、漲跌幅、匯率、利率、油價、VIX、資金流、期貨籌碼等硬數字時，只能引用 macro_data_guard.facts 或可驗證來源表。",
        "abnormal_number_rule": "macro_data_guard.alerts 代表疑似異常或需複核的數字，不得直接寫成主結論，必須標示為待驗證。",
        "missing_number_rule": "macro_data_guard.missing_data 代表缺漏數字，不得自行估算或補寫。",
    }
    confidence = "high" if facts and not alerts else "medium" if facts else "low"
    return {
        "schema_version": "macro_data_guard_v1",
        "confidence": confidence,
        "facts": facts,
        "alerts": alerts,
        "missing_data": _unique(missing),
        "policy": policy,
        "prompt_rules": _prompt_rules(alerts, missing),
    }


def _add_index_facts(facts: list[dict[str, Any]], alerts: list[dict[str, Any]], label: str, metrics: Any, *, market: str) -> None:
    if not isinstance(metrics, dict):
        return
    if metrics.get("latest_close") is not None:
        facts.append(_fact(label, "最新收盤", metrics.get("latest_close"), metrics.get("latest_date"), "市場行情資料"))
    for field, name in (
        ("one_day_change_points", "單日點數變化"),
        ("one_day_return_pct", "單日漲跌幅%"),
        ("five_day_change_points", "五日點數變化"),
        ("five_day_return_pct", "五日漲跌幅%"),
        ("twenty_day_return_pct", "二十日漲跌幅%"),
        ("realized_volatility_20d_pct", "二十日實現波動率%"),
    ):
        if metrics.get(field) is not None:
            facts.append(_fact(label, name, metrics.get(field), metrics.get("latest_date"), "市場行情資料"))

    daily_pct = _to_float(metrics.get("one_day_return_pct"))
    daily_points = _to_float(metrics.get("one_day_change_points"))
    five_points = _to_float(metrics.get("five_day_change_points"))
    if daily_pct is not None:
        threshold = INDEX_ABS_DAILY_PCT_ALERT if market == "tw" else GLOBAL_INDEX_DAILY_PCT_ALERT
        if abs(daily_pct) >= threshold:
            alerts.append(_alert(label, "單日漲跌幅異常", daily_pct, f"單日漲跌幅絕對值 >= {threshold}%"))
    if market == "tw" and daily_points is not None and abs(daily_points) >= TW_INDEX_DAILY_POINTS_ALERT:
        alerts.append(_alert(label, "台股單日點數異常", daily_points, f"台股單日點數變化絕對值 >= {TW_INDEX_DAILY_POINTS_ALERT}"))
    if market == "tw" and five_points is not None and abs(five_points) >= TW_INDEX_FIVE_DAY_POINTS_ALERT:
        alerts.append(_alert(label, "台股五日點數異常", five_points, f"台股五日點數變化絕對值 >= {TW_INDEX_FIVE_DAY_POINTS_ALERT}"))


def _add_volatility_facts(facts: list[dict[str, Any]], missing: list[str], volatility: dict[str, Any]) -> None:
    taifex_latest = ((volatility.get("taifex_option_iv") or {}).get("latest") or {}).get("value")
    taifex_date = ((volatility.get("taifex_option_iv") or {}).get("latest") or {}).get("date")
    if taifex_latest is not None:
        facts.append(_fact("TAIFEX VIX", "最新波動率", taifex_latest, taifex_date, "TAIFEX 官方資料"))
    else:
        missing.append("TAIFEX VIX 最新波動率。")
    global_vix = volatility.get("global_vix") or {}
    if global_vix.get("latest") is not None:
        facts.append(_fact("Global VIX", "最新波動率", global_vix.get("latest"), global_vix.get("latest_date"), "Yahoo Finance proxy"))
    else:
        missing.append("Global VIX proxy 最新波動率。")


def _add_cash_flow_facts(facts: list[dict[str, Any]], missing: list[str], cash_flow: dict[str, Any]) -> None:
    if cash_flow.get("net_amount_total") is not None:
        facts.append(_fact("TWSE 三大法人", "合計買賣超金額", cash_flow.get("net_amount_total"), None, "TWSE BFI82U"))
    else:
        missing.append("TWSE 三大法人合計買賣超金額。")


def _add_futures_facts(facts: list[dict[str, Any]], missing: list[str], futures: dict[str, Any]) -> None:
    rows = futures.get("tx_futures_rows") or []
    found = False
    for row in rows:
        if row.get("identity") in {"外資", "Foreign Investors"} and row.get("open_interest_net_contracts") is not None:
            facts.append(_fact("TAIFEX 台指期", "外資未平倉淨口數", row.get("open_interest_net_contracts"), futures.get("report_date"), "TAIFEX 官方資料"))
            found = True
            break
    if not found:
        missing.append("TAIFEX 台指期外資未平倉淨口數。")


def _fact(label: str, metric: str, value: Any, date: Any, source: str) -> dict[str, Any]:
    return {"標的": label, "指標": metric, "數值": value, "日期": date or "日期不可驗證", "資料來源": source}


def _alert(label: str, kind: str, value: Any, rule: str) -> dict[str, Any]:
    return {"標的": label, "異常類型": kind, "數值": value, "觸發規則": rule, "使用限制": "不得直接寫成主結論，必須標示為待驗證或需複核。"}


def _prompt_rules(alerts: list[dict[str, Any]], missing: list[str]) -> list[str]:
    rules = [
        "硬數字只能引用 macro_data_guard.facts 或完整來源表中的可驗證數值。",
        "不得自行創造、推估或改寫指數點數、漲跌幅、匯率、利率、油價、VIX、資金流、期貨籌碼等硬數字。",
        "若 facts 與來源敘述互相矛盾，必須以 facts 為準，並說明矛盾來源。",
        "若 alerts 不為空，不得把該數字直接寫成主結論，必須標示為異常待驗證。",
        "若 missing_data 不為空，不得自行估算缺漏數字。",
    ]
    if alerts:
        rules.append(f"目前有 {len(alerts)} 個異常數字警示，相關數字只能作為待複核資訊。")
    if missing:
        rules.append(f"目前有 {len(missing)} 個硬數字缺口，相關段落必須寫資料不足。")
    return rules


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result
