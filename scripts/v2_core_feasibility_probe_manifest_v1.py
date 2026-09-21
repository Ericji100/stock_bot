"""Build the 48-case, outcome-blind M2A feasibility-probe manifest.

Selection labels remain in ``sealed/`` and are never copied into review
packets.  The builder uses only calibration-era legacy judgements and AS-OF
market data; it never reads trade outcomes, MFE, MAE, PnL, or exits.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.formal_ai_full_review_packets import confirmed_pivot_timeline
from scripts.formal_ai_historical_2023_packets import _causal_frame, _daily_fact_with_raw
from scripts.v2_core_source_audit_v1 import BATCHES, read_jsonl, sha256


VERSION = "v2-core-feasibility-probe-r2-candidate"
CONTEXT_BARS = 750
POSITIVE = "POSITIVE_REFERENCE（舊V2核准正例）"
V1_ONLY = "HARD_NEGATIVE_V1_ONLY（V1核准但V2未核准）"
SCENARIOS = (
    "MATURE_TREND_PULLBACK",
    "MACRO_COPY_RESONANCE",
    "BEAR_REVERSAL_LEFT_RIGHT",
    "FRESH_Q1_EXPANSION",
)
BOUNDARY_TERMS = (
    "空間",
    "壓力",
    "防線",
    "距離",
    "緊貼",
    "位階",
    "追價",
    "Q3",
    "末段",
    "不完整",
    "不足",
    "尚未",
)
FORBIDDEN_KEYS = {
    "code",
    "name",
    "symbol",
    "case_role",
    "intended_scenario",
    "expected_permission",
    "legacy_v2_trigger",
    "legacy_v2_no_trade_reason",
    "future_outcome",
    "mfe",
    "mae",
    "pnl",
    "profit",
    "return_pct",
    "exit_date",
    "exit_price",
}


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def stable_score(*parts: str) -> int:
    return int.from_bytes(
        hashlib.sha256("|".join(parts).encode("utf-8")).digest()[:8], "big"
    )


def review_id(code: str, as_of: str) -> str:
    return "FP-" + hashlib.sha256(
        f"{VERSION}|review|{code}|{as_of}".encode("utf-8")
    ).hexdigest()[:24]


def anonymous_stock_id(code: str) -> str:
    return "FS-" + hashlib.sha256(
        f"{VERSION}|stock|{code}".encode("utf-8")
    ).hexdigest()[:20]


def contains_forbidden_key(value: Any) -> set[str]:
    hits: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                hits.add(str(key))
            hits.update(contains_forbidden_key(child))
    elif isinstance(value, list):
        for child in value:
            hits.update(contains_forbidden_key(child))
    return hits


def load_ledgers(repo_root: Path) -> dict[str, list[dict[str, Any]]]:
    return {
        batch_id: read_jsonl((repo_root / BATCHES[batch_id] / "ai_decisions_merged.jsonl").resolve())
        for batch_id in ("889", "1029")
    }


def v1_scenario_for_case(
    case: dict[str, Any], ledgers: dict[str, list[dict[str, Any]]]
) -> str | None:
    batch = str(case["batch_id"])
    index = int(case["source_line_index"]) - 1
    if batch not in ledgers or not 0 <= index < len(ledgers[batch]):
        return None
    decision = ledgers[batch][index]
    matches = {
        str(trigger.get("scenario"))
        for trigger in (decision.get("v1") or {}).get("triggers") or []
        if str(trigger.get("signal_date")) == str(case["as_of"])
        and str(trigger.get("scenario")) in SCENARIOS
    }
    return next(iter(matches)) if len(matches) == 1 else None


def retrospective_bear_positives(repo_root: Path) -> list[dict[str, Any]]:
    run = (repo_root / BATCHES["747"]).resolve()
    rows = read_jsonl(run / "ai_decisions_merged.jsonl")
    result: list[dict[str, Any]] = []
    for row_index, row in enumerate(rows, 1):
        code = str(row["code"])
        packet_path = run / "review_packets" / f"{code}.json"
        if not packet_path.is_file():
            continue
        for trigger in (row.get("v2") or {}).get("triggers") or []:
            if trigger.get("scenario") != "BEAR_REVERSAL_LEFT_RIGHT":
                continue
            result.append(
                {
                    "batch_id": "747",
                    "source_line_index": row_index,
                    "code": code,
                    "name": row.get("name"),
                    "as_of": str(trigger["signal_date"]),
                    "packet_path": str(packet_path.resolve()),
                    "packet_sha256": sha256(packet_path),
                    "legacy_v2_trigger": trigger,
                    "source_classification": "PARTIAL_REFERENCE_ONLY（部分有效、僅供參考）",
                }
            )
    return result


def select_cases(repo_root: Path, cases_path: Path) -> list[dict[str, Any]]:
    cases = read_jsonl(cases_path)
    ledgers = load_ledgers(repo_root)
    positives: dict[str, list[dict[str, Any]]] = defaultdict(list)
    v1_only: dict[str, list[dict[str, Any]]] = defaultdict(list)

    seen_positive_key: set[tuple[str, str]] = set()
    for case in cases:
        if case.get("case_kind") == POSITIVE:
            trigger = case.get("legacy_v2_trigger") or {}
            scenario = str(trigger.get("scenario"))
            key = (str(case["code"]), str(case["as_of"]))
            if scenario in SCENARIOS and key not in seen_positive_key:
                item = dict(case)
                item["source_classification"] = (
                    "VALID_CALIBRATION_SUBSET（有效校準子集）"
                )
                positives[scenario].append(item)
                seen_positive_key.add(key)
        elif case.get("case_kind") == V1_ONLY:
            scenario = v1_scenario_for_case(case, ledgers)
            if scenario:
                item = dict(case)
                item["intended_scenario"] = scenario
                item["source_classification"] = (
                    "VALID_CALIBRATION_SUBSET（有效校準子集）"
                )
                v1_only[scenario].append(item)

    selected: list[dict[str, Any]] = []
    used_codes: set[str] = set()

    def take(
        pool: list[dict[str, Any]],
        *,
        scenario: str,
        role: str,
        count: int,
        boundary_first: bool = False,
    ) -> None:
        def key(item: dict[str, Any]) -> tuple[int, int]:
            reason = str(item.get("legacy_v2_no_trade_reason") or "")
            boundary_score = sum(term in reason for term in BOUNDARY_TERMS)
            order = -boundary_score if boundary_first else boundary_score
            return order, stable_score(VERSION, scenario, role, str(item["code"]), str(item["as_of"]))

        added = 0
        for item in sorted(pool, key=key):
            code = str(item["code"])
            if code in used_codes:
                continue
            enriched = dict(item)
            enriched["case_role"] = role
            enriched["intended_scenario"] = scenario
            selected.append(enriched)
            used_codes.add(code)
            added += 1
            if added == count:
                break
        if added != count:
            raise RuntimeError(f"insufficient unique cases for {scenario}/{role}: {added}/{count}")

    for scenario in SCENARIOS:
        available = positives[scenario]
        need_valid = 2 if scenario == "BEAR_REVERSAL_LEFT_RIGHT" else 4
        take(
            available,
            scenario=scenario,
            role="POSITIVE_REFERENCE",
            count=need_valid,
        )
        if scenario == "BEAR_REVERSAL_LEFT_RIGHT":
            take(
                retrospective_bear_positives(repo_root),
                scenario=scenario,
                role="POSITIVE_REFERENCE",
                count=2,
            )

    for scenario in SCENARIOS:
        take(
            v1_only[scenario],
            scenario=scenario,
            role="BOUNDARY_REFERENCE",
            count=4,
            boundary_first=True,
        )
        take(
            v1_only[scenario],
            scenario=scenario,
            role="NEGATIVE_REFERENCE",
            count=4,
            boundary_first=False,
        )
    return selected


def input_manifest_item(run_root: Path, code: str) -> dict[str, Any]:
    manifest = json.loads((run_root / "input_manifest.json").read_text(encoding="utf-8-sig"))
    matches = [item for item in manifest.get("items") or [] if str(item.get("code")) == code]
    if len(matches) != 1:
        raise RuntimeError(f"expected one input manifest item for {code}: {run_root}")
    item = matches[0]
    if not item.get("price_path"):
        # The 747 formal-AI manifest points only to its immutable review packet.
        # Its price/event paths are preserved in the sibling source-preparation
        # run and carry the same hashes recorded by that review packet.
        companion = run_root.parent / "tg_enlightenment_ai_v2" / "input_manifest.json"
        if not companion.is_file():
            raise RuntimeError(f"price provenance missing for {code}: {run_root}")
        companion_manifest = json.loads(companion.read_text(encoding="utf-8-sig"))
        companion_matches = [
            candidate
            for candidate in companion_manifest.get("items") or []
            if str(candidate.get("code")) == code
        ]
        if len(companion_matches) != 1:
            raise RuntimeError(f"expected one companion price item for {code}: {companion}")
        item = companion_matches[0]
    price_path = Path(str(item["price_path"]))
    if not price_path.is_file() or sha256(price_path) != str(item.get("price_sha256")):
        raise RuntimeError(f"immutable price source mismatch: {price_path}")
    return item


def visible_actions(item: dict[str, Any], as_of: str) -> list[dict[str, Any]]:
    path_text = item.get("event_path")
    if not path_text:
        return []
    path = Path(str(path_text))
    if not path.is_file() or sha256(path) != str(item.get("event_sha256")):
        raise RuntimeError(f"immutable corporate-action source mismatch: {path}")
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    actions: list[dict[str, Any]] = []
    for action_type, rows in payload.items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or str(row.get("date") or "9999-12-31") > as_of:
                continue
            actions.append({"type": str(action_type), **row})
    return sorted(actions, key=lambda row: (str(row.get("date")), str(row.get("type"))))


def safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 6)


def count_reclaims(rows: list[dict[str, Any]], ma_key: str) -> int:
    count = 0
    previous: dict[str, Any] | None = None
    for row in rows:
        close = safe_float(row.get("adj_close") or row.get("close"))
        ma = safe_float(row.get(ma_key))
        if previous is not None:
            prior_close = safe_float(previous.get("adj_close") or previous.get("close"))
            prior_ma = safe_float(previous.get(ma_key))
            if None not in (close, ma, prior_close, prior_ma) and prior_close <= prior_ma and close > ma:
                count += 1
        previous = row
    return count


def slope_atr(rows: list[dict[str, Any]], ma_key: str, lookback: int) -> float | None:
    if len(rows) <= lookback:
        return None
    current = safe_float(rows[-1].get(ma_key))
    prior = safe_float(rows[-1 - lookback].get(ma_key))
    atr = safe_float(rows[-1].get("atr14"))
    if None in (current, prior, atr) or not atr:
        return None
    return round((current - prior) / atr, 6)


def build_proxies(daily: list[dict[str, Any]], pivots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    last = daily[-1]
    atr = safe_float(last.get("atr14"))
    open_ = safe_float(last.get("adj_open") or last.get("open"))
    close = safe_float(last.get("adj_close") or last.get("close"))
    high = safe_float(last.get("adj_high") or last.get("high"))
    low = safe_float(last.get("adj_low") or last.get("low"))
    body_atr = None if None in (open_, close, atr) or not atr else round(abs(close - open_) / atr, 6)
    range_atr = None if None in (high, low, atr) or not atr else round((high - low) / atr, 6)
    recent_252 = daily[-252:]
    atr_values_20 = [safe_float(row.get("atr14")) for row in daily[-20:]]
    atr_values_60 = [safe_float(row.get("atr14")) for row in daily[-60:]]
    atr_values_20 = [value for value in atr_values_20 if value is not None]
    atr_values_60 = [value for value in atr_values_60 if value is not None]
    confirmed_recent = [
        pivot for pivot in pivots if str(pivot.get("confirmation_date") or "") <= str(last["date"])
    ]
    return [
        {
            "ref": "PROXY:ASOF_SIGNAL",
            "values": {
                "body_atr": body_atr,
                "range_atr": range_atr,
                "return_1d_pct": safe_float(last.get("return_1d_pct")),
                "volume_ratio_20": safe_float(last.get("volume_ratio_20")),
            },
            "supports_only": ["STOP_NEARBY", "RISK_REWARD_DAMAGED"],
        },
        {
            "ref": "PROXY:LONG_MA_CONTEXT_252",
            "values": {
                "ma105_slope_21_atr": slope_atr(daily, "ma105", 21),
                "ma144_slope_21_atr": slope_atr(daily, "ma144", 21),
                "ma105_reclaim_count_252": count_reclaims(recent_252, "ma105"),
                "ma144_reclaim_count_252": count_reclaims(recent_252, "ma144"),
            },
            "supports_only": ["TIME_SUFFICIENT", "EARLY_STAGE", "ANCHOR_CLEAN"],
        },
        {
            "ref": "PROXY:VOLATILITY_CONTEXT",
            "values": {
                "atr14": atr,
                "atr_vs_median_20": None if not atr_values_20 or not atr else round(atr / statistics.median(atr_values_20), 6),
                "atr_vs_median_60": None if not atr_values_60 or not atr else round(atr / statistics.median(atr_values_60), 6),
            },
            "supports_only": ["ANCHOR_MEATY", "TREND_WEAKENED", "RISK_REWARD_DAMAGED"],
        },
        {
            "ref": "PROXY:CONFIRMED_STRUCTURE_CONTEXT",
            "values": {
                "confirmed_pivot_count_120": sum(
                    str(pivot.get("confirmation_date") or "") >= str(daily[max(0, len(daily) - 120)]["date"])
                    for pivot in confirmed_recent
                ),
                "confirmed_pivot_count_total": len(confirmed_recent),
            },
            "supports_only": ["ANCHOR_DESTRUCTIVE", "INTERNAL_TOO_MESSY", "TIME_SUFFICIENT"],
        },
    ]


def build_evidence_catalog(packet: dict[str, Any]) -> list[dict[str, str]]:
    catalog: list[dict[str, str]] = [
        {"ref": "QUALITY:SUMMARY", "path": "/data_quality", "kind": "QUALITY"}
    ]
    for index, row in enumerate(packet["daily_structure_context_to_as_of"]):
        catalog.append(
            {"ref": f"PRICE:{row['date']}", "path": f"/daily_structure_context_to_as_of/{index}", "kind": "PRICE"}
        )
    for index, pivot in enumerate(packet["confirmed_pivots_to_as_of"]):
        source = str(pivot.get("date") or pivot.get("source_date") or index)
        confirmed = str(pivot.get("confirmation_date") or pivot.get("confirmed_on") or index)
        catalog.append(
            {"ref": f"PIVOT:{source}:{confirmed}:{index}", "path": f"/confirmed_pivots_to_as_of/{index}", "kind": "PIVOT"}
        )
    for index, _cycle in enumerate(packet["completed_macd_21_55_55_cycles_to_as_of"]):
        catalog.append(
            {"ref": f"MACD:CYCLE-{index + 1}", "path": f"/completed_macd_21_55_55_cycles_to_as_of/{index}", "kind": "MACD"}
        )
    for index, proxy in enumerate(packet["proxy_evidence"]):
        catalog.append(
            {"ref": str(proxy["ref"]), "path": f"/proxy_evidence/{index}", "kind": "PROXY"}
        )
    for index, action in enumerate(packet["data_quality"]["corporate_actions_visible_to_as_of"]):
        date = str(action.get("date") or index)
        catalog.append(
            {"ref": f"ACTION:{date}:{index}", "path": f"/data_quality/corporate_actions_visible_to_as_of/{index}", "kind": "ACTION"}
        )
    refs = [item["ref"] for item in catalog]
    if len(refs) != len(set(refs)):
        raise RuntimeError("evidence refs are not unique")
    return catalog


def build_packet(case: dict[str, Any]) -> dict[str, Any]:
    code = str(case["code"])
    as_of = str(case["as_of"])
    source_path = Path(str(case["packet_path"]))
    if not source_path.is_file() or sha256(source_path) != str(case["packet_sha256"]):
        raise RuntimeError(f"source packet mismatch: {code}:{as_of}")
    source = json.loads(source_path.read_text(encoding="utf-8-sig"))
    run_root = source_path.parent.parent
    item = input_manifest_item(run_root, code)
    frame = _causal_frame(item, as_of)
    context = frame.tail(CONTEXT_BARS)
    daily = [_daily_fact_with_raw(frame, int(index), {}) for index in context.index]
    cycles = [
        cycle
        for cycle in (source.get("macd_21_55_55_cycles") or [])
        if str(cycle.get("end") or "9999-12-31") <= as_of
    ]
    pivots = [
        pivot
        for pivot in confirmed_pivot_timeline(frame, start="2018-01-01")
        if str(pivot.get("confirmation_date") or "9999-12-31") <= as_of
    ]
    actions = [
        action
        for action in ((source.get("corporate_action_audit") or {}).get("events_in_window") or [])
        if str(action.get("date") or "9999-12-31") <= as_of
    ]
    if not actions:
        actions = visible_actions(item, as_of)
    packet: dict[str, Any] = {
        "packet_version": VERSION,
        "review_id": review_id(code, as_of),
        "anonymous_stock_id": anonymous_stock_id(code),
        "as_of": as_of,
        "task": "V2_CORE_M2A_COMMON_STRUCTURE_AND_SINGLE_SCENARIO_PROBE",
        "source_packet_sha256": sha256(source_path),
        "source_price_sha256": str(item["price_sha256"]),
        "data_quality": {
            "preferred_750_met": len(frame) >= CONTEXT_BARS,
            "available_bars_to_as_of": int(len(frame)),
            "context_bars": int(len(context)),
            "context_start": daily[0]["date"],
            "context_end": daily[-1]["date"],
            "raw_and_adjusted_ohlc_present": bool((source.get("data_quality") or {}).get("raw_and_adjusted_ohlc_present")),
            "corporate_action_unresolved_as_of": bool((source.get("corporate_action_audit") or {}).get("corporate_action_unresolved")),
            "corporate_actions_visible_to_as_of": actions,
        },
        "completed_macd_21_55_55_cycles_to_as_of": cycles,
        "confirmed_pivots_to_as_of": pivots,
        "daily_structure_context_to_as_of": daily,
        "proxy_evidence": build_proxies(daily, pivots),
        "review_constraints": {
            "identity_blind": True,
            "legacy_answer_blind": True,
            "future_performance_blind": True,
            "monitor_start_and_upstream_selection_hidden": True,
            "stage_b_before_program_route": True,
            "stage_d_only_after_program_route": True,
            "program_truth_table_is_final": True,
            "formal_model": "gpt-5.6-sol",
            "reasoning_effort": "xhigh",
        },
    }
    packet["evidence_catalog"] = build_evidence_catalog(packet)
    forbidden = contains_forbidden_key(packet)
    if forbidden:
        raise RuntimeError(f"forbidden keys in probe packet: {sorted(forbidden)}")
    if not daily or str(daily[-1]["date"]) != as_of:
        raise RuntimeError(f"packet does not end on AS-OF: {code}:{as_of}")
    return packet


def write_new_or_identical(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() != payload:
        raise RuntimeError(f"refusing to overwrite changed artifact: {path}")
    path.write_bytes(payload)


def build(repo_root: Path, artifact_dir: Path, cases_path: Path) -> dict[str, Any]:
    selected = select_cases(repo_root, cases_path)
    if len(selected) != 48:
        raise RuntimeError(f"expected 48 cases, found {len(selected)}")
    packet_dir = artifact_dir / "feasibility_probe_packets_r2"
    public_rows: list[dict[str, Any]] = []
    sealed_rows: list[dict[str, Any]] = []
    for case in sorted(selected, key=lambda item: review_id(str(item["code"]), str(item["as_of"]))):
        packet = build_packet(case)
        packet_path = packet_dir / f"{packet['review_id']}.json"
        write_new_or_identical(packet_path, canonical_bytes(packet))
        public_rows.append(
            {
                "review_id": packet["review_id"],
                "anonymous_stock_id": packet["anonymous_stock_id"],
                "as_of": packet["as_of"],
                "packet_file": packet_path.name,
                "packet_sha256": sha256(packet_path),
                "context_bars": packet["data_quality"]["context_bars"],
                "preferred_750_met": packet["data_quality"]["preferred_750_met"],
                "future_performance_included": False,
                "identity_included": False,
                "legacy_answer_included": False,
            }
        )
        sealed_rows.append(
            {
                "review_id": packet["review_id"],
                "anonymous_stock_id": packet["anonymous_stock_id"],
                "code": str(case["code"]),
                "name": case.get("name"),
                "as_of": str(case["as_of"]),
                "case_role": str(case["case_role"]),
                "intended_scenario": str(case["intended_scenario"]),
                "source_batch": str(case["batch_id"]),
                "source_classification": str(case["source_classification"]),
                "source_packet_sha256": str(case["packet_sha256"]),
                "future_performance_included": False,
            }
        )

    public = {
        "manifest_version": VERSION,
        "status": "READY_FOR_CONTRACT_VALIDATION（等待契約驗證）",
        "packet_directory": packet_dir.name,
        "case_count": len(public_rows),
        "selection_labels_sealed": True,
        "future_performance_used_for_selection": False,
        "formal_model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "required_rounds": 3,
        "rows": public_rows,
    }
    sealed = {
        "manifest_version": VERSION,
        "classification": "CALIBRATION_LABELS_NOT_FOR_AI（校準標籤、不得提供AI）",
        "summary": {
            "roles": dict(sorted(Counter(row["case_role"] for row in sealed_rows).items())),
            "scenarios": dict(sorted(Counter(row["intended_scenario"] for row in sealed_rows).items())),
            "scenario_roles": {
                scenario: dict(
                    sorted(
                        Counter(
                            row["case_role"]
                            for row in sealed_rows
                            if row["intended_scenario"] == scenario
                        ).items()
                    )
                )
                for scenario in SCENARIOS
            },
            "source_classifications": dict(
                sorted(Counter(row["source_classification"] for row in sealed_rows).items())
            ),
        },
        "rows": sealed_rows,
    }
    write_new_or_identical(artifact_dir / "feasibility_probe_manifest.json", canonical_bytes(public))
    write_new_or_identical(
        artifact_dir / "sealed" / "feasibility_probe_calibration_labels.json",
        canonical_bytes(sealed),
    )
    return {"public": public, "sealed": sealed}


def markdown(payload: dict[str, Any]) -> str:
    public = payload["public"]
    sealed = payload["sealed"]
    summary = sealed["summary"]
    lines = [
        "# V2核心M2A四情境可行性探針清單",
        "",
        f"- 版本：`{VERSION}`",
        "- 狀態：`READY_FOR_CONTRACT_VALIDATION（等待契約驗證）`",
        f"- 匿名AS-OF案例：{public['case_count']}",
        "- 正式設定：`gpt-5.6-sol／xhigh`，同封包獨立三輪",
        "- 個別案例的股票身分、舊答案、角色與預期情境存於sealed校準標籤，不得提供AI。",
        "- 選樣及封包均未使用MFE、MAE、損益、出場或其他未來績效。",
        "",
        "## 平衡設計",
        "",
        "| 情境 | 舊V2正例 | 相近負例 | 邊界例 | 合計 |",
        "|---|---:|---:|---:|---:|",
    ]
    for scenario in SCENARIOS:
        counts = summary["scenario_roles"][scenario]
        lines.append(
            f"| `{scenario}` | {counts.get('POSITIVE_REFERENCE', 0)} | {counts.get('NEGATIVE_REFERENCE', 0)} | {counts.get('BOUNDARY_REFERENCE', 0)} | {sum(counts.values())} |"
        )
    lines.extend(
        [
            "",
            "## BEAR樣本揭露",
            "",
            "正式校準分割只有2個 `BEAR_REVERSAL_LEFT_RIGHT` 舊V2正例。為滿足小型可行性探針的4個正例，另加入2個747批舊V2案例，並明確標記 `PARTIAL_REFERENCE_ONLY（部分有效、僅供參考）`。它們只能測試語意可判讀性，不能作正式重現率或績效標準答案。BEAR的數字門檻仍不得由這4例推導。",
            "",
            "## 執行關卡",
            "",
            "本清單建立不等於可以立即跑AI。必須先通過：packet schema、evidence ref存在性、AS-OF截止、禁止欄位、三輪輸入雜湊及STAGE_B／STAGE_D輸出契約驗證。通過後才把完全相同的48包交給三輪正式模型。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1",
    )
    parser.add_argument("--cases", type=Path, default=None)
    args = parser.parse_args()
    cases = args.cases or args.artifact_dir / "calibration_daily_cases_v2.jsonl"
    payload = build(args.repo_root.resolve(), args.artifact_dir.resolve(), cases.resolve())
    write_new_or_identical(
        args.artifact_dir.resolve() / "feasibility_probe_manifest.md",
        markdown(payload).encode("utf-8"),
    )
    print(
        json.dumps(
            {
                "status": "CREATED_OR_IDENTICAL（已建立或內容相同）",
                "case_count": payload["public"]["case_count"],
                "summary": payload["sealed"]["summary"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
