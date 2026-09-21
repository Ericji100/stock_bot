"""Run a fresh outcome-blind MiniMax-M3 review over every V3 queue batch.

The model receives only as-of facts from ``formal_ai_v3_review_digest.py``.
Raw provider responses and parsed decisions are persisted before any outcome
or backtest file is opened.  This module does not infer routes itself.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v3_full_review"
BATCH_MANIFEST = RUN / "ai_review_batch_manifest.json"
OUTPUT = RUN / "minimax_m3_reviews_v2"
MODEL = "MiniMax-M3"
BASE_URL = "https://api.minimax.io/v1"
ALLOWED = {"NEAR_PASS_MACRO_COPY", "NEAR_PASS_FRESH_Q1", "BEAR_REVERSAL_PROBE", "NO_TRADE"}


SYSTEM_PROMPT = """你是臺股日K結構的審慎審核AI。你正在執行已凍結的啟蒙層V3歷史回放。
只能使用輸入JSON中的判讀日收盤以前資料；沒有V1訊號、期後價格、MFE、損益或股票排名。
輸出只能是合法JSON物件，不得有Markdown或思考過程。證據不足就是NO_TRADE，不得猜測。
不要展開長篇推理；逐筆直接依固定gate輸出短結論，把有限token優先用於完整涵蓋所有review_id。
"""


RULE_PROMPT = """固定V3決策規則：
1. 本批只含V2正式帳本未核准的股票，因此你不得輸出V2_CORE。若你認為V2所有條件全PASS，仍輸出NO_TRADE並把blocking_gates設為[\"V2_REVIEW_DISAGREEMENT\"]。
2. 所有試單共同硬閘必須全PASS：監控有效、資料足夠、收盤已完成獨立小級控制權轉換、因果episode防線、非Q3、非末代耗竭、大小級無方向衝突、非單一指標、風險可執行、V2必要條件零FAIL。
3. NEAR_PASS_MACRO_COPY：已完成向上父代、父代後修正仍有效、修正空頭線因果成立、小級向上重定錨突破、非Q3/耗竭、防線成立。COMPLETED_PARENT_ANCHOR、TAIJI_GENERATION_MAPPED、DUAL_SCALE_LONG_ALIGNMENT三項必須恰一項UNKNOWN、另兩項PASS、零FAIL。
4. NEAR_PASS_FRESH_Q1：已有可追溯早期向上新錨、不是成熟長多或已完成父代複製；MACD只能輔助、位階早且有空間、防線成立。CLEAN_MEATY_DESTRUCTIVE_TRACEABLE、DYNAMIC_Q1_EXPANSION、EARLY_TAIJI_GENERATION三項必須恰一項UNKNOWN、另兩項PASS、零FAIL。若第一項UNKNOWN，只能是更高級破壞尚未確認；乾淨、有肉、可追溯與小級破壞仍需PASS。
5. BEAR_REVERSAL_PROBE：大級下降定錨/空頭朝代仍有效且大空防線可指出；小級完成LR/RL/RR/DIRECT_TO_RIGHT並有自己的防線；大小級分開。只允許BEAR_LATE_STAGE_EVIDENCE一項UNKNOWN，且至少一項可量化部分線索（攻擊縮短、斜率衰退、量價背離、末跌後不續低、時間/空間耗損）。LL禁止交易。
6. 同一候選只能單一路由。單根紅K、均線、MACD、量能或創高都不能單獨核准；但輸入每一筆都已客觀確認「收盤突破判讀日前已確認的小樞紐高」，因此不得把它誤寫成只有均線、MACD或上游選股來源。突破太晚、明顯第五段/第三次衰退、停損不在收盤下方、風險與結構不相稱、主要解讀衝突，均NO_TRADE。
7. 風險不另套固定百分比上限；但stop必須低於close、距離與結構相稱。次日跳空追價限制由成交引擎處理，不在此偷看。
8. selected_today只表示上游為何加入監控，永遠不是啟蒙層觸發，也不能因來源少就拒絕。MACD為負、30日區間位置偏高、量比高低或均線位置都只能作輔助，不能單獨列為blocking gate。你必須以大小級樞紐的時間順序、高低點關係、父段/修正/再發動與因果防線判斷。
9. NO_TRADE的blocking_gates只能使用下列正式語意之一：ACTIVE_WATCHLIST、DATA_SUFFICIENT_FOR_ROUTE、TRIGGER_COMPLETED、CAUSAL_EPISODE_STOP、NOT_Q3、NOT_LATE_OR_EXHAUSTED、NO_SCALE_DIRECTION_CONFLICT、NOT_SINGLE_INDICATOR_SIGNAL、RISK_EXECUTABLE、NO_V2_FAIL、COMPLETED_PARENT_ANCHOR、CORRECTION_INTACT、CORRECTION_BEAR_DOW_LINE_CAUSAL、SMALL_UP_REANCHOR_BREAK、TAIJI_GENERATION_MAPPED、DUAL_SCALE_LONG_ALIGNMENT、FRESH_UP_ANCHOR、CLEAN_MEATY_DESTRUCTIVE_TRACEABLE、DYNAMIC_Q1_EXPANSION、EARLY_TAIJI_GENERATION、ACTIVE_LARGE_BEAR_ANCHOR、LARGE_BEAR_DOW_DEFENSE_CAUSAL、LEFT_RIGHT_PHASE_MAPPED、DUAL_SCALE_SEPARATED、PHASE_STOP_CAUSAL、BEAR_LATE_STAGE_EVIDENCE、PRIMARY_SCENARIO_UNRESOLVED、V2_REVIEW_DISAGREEMENT。不得自創RANGE_TOO_HIGH、MACD_MISALIGNED或NO_NEW_SELECTION等閘門。

每筆輸出：
- review_id（必須原樣）
- decision：上述三個試單路由之一或NO_TRADE
- unknown_gate：核准時為唯一允許UNKNOWN；NO_TRADE為null
- phase：LR/RL/RR/DIRECT_TO_RIGHT或NONE
- blocking_gates：NO_TRADE至少一項；核准必須空陣列
- reason：繁體中文、40至140字，引用日期/價位關係，說明結構而非指標投票
- evidence：恰好兩條短證據，核准與拒絕都要有

輸出格式：{\"batch_id\":字串,\"decisions\":[...]}。輸出筆數與review_id集合必須和輸入完全相同。
"""


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_text(value: str) -> str:
    return _sha_bytes(value.encode("utf-8"))


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _load_key() -> str:
    secret_path = ROOT / "config/secrets.json"
    data = _read(secret_path) if secret_path.exists() else {}
    key = str(data.get("minimax_api_key") or "").strip()
    if not key:
        raise RuntimeError("minimax_api_key is not configured")
    return key


def _candidate(row: dict[str, Any]) -> dict[str, Any]:
    # The digest was already restricted to as-of facts.  Remove provider-irrelevant
    # paths/hashes and retain enough chronology for anchor/generation judgement.
    return {
        "review_id": row["review_id"],
        "code": row["code"],
        "name": row["name"],
        "first_selected_on": row["first_selected_on"],
        "review_as_of": row["review_as_of"],
        "market": {key: value for key, value in row["market"].items() if key not in {"range30_position_pct", "facts"}},
        "structure": row["structure"],
        "completed_macd_cycles_asof": row["cycles"],
        "risk": row["risk"],
    }


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I).strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.S | re.I).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        return json.loads(cleaned[start:end + 1])


def _validate(batch_id: str, source: list[dict[str, Any]], result: dict[str, Any]) -> list[dict[str, Any]]:
    if result.get("batch_id") != batch_id:
        raise ValueError(f"batch_id mismatch: {result.get('batch_id')!r} != {batch_id!r}")
    decisions = result.get("decisions")
    if not isinstance(decisions, list):
        raise ValueError("decisions must be an array")
    expected = [str(row["review_id"]) for row in source]
    actual = [str(row.get("review_id")) for row in decisions if isinstance(row, dict)]
    if len(actual) != len(set(actual)) or set(actual) != set(expected):
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        raise ValueError(f"review_id coverage mismatch missing={missing[:5]} extra={extra[:5]}")
    by_id = {str(row["review_id"]): row for row in decisions}
    ordered = []
    for review_id in expected:
        row = by_id[review_id]
        decision = str(row.get("decision") or "")
        if decision not in ALLOWED:
            raise ValueError(f"{review_id}: invalid decision {decision!r}")
        blockers = row.get("blocking_gates")
        evidence = row.get("evidence")
        if not isinstance(evidence, list) or len(evidence) != 2 or not all(str(value).strip() for value in evidence):
            raise ValueError(f"{review_id}: evidence must contain two items")
        if decision == "NO_TRADE":
            if row.get("unknown_gate") is not None or not isinstance(blockers, list) or not blockers:
                raise ValueError(f"{review_id}: invalid NO_TRADE fields")
        else:
            allowed_unknowns = {
                "NEAR_PASS_MACRO_COPY": {"COMPLETED_PARENT_ANCHOR", "TAIJI_GENERATION_MAPPED", "DUAL_SCALE_LONG_ALIGNMENT"},
                "NEAR_PASS_FRESH_Q1": {"CLEAN_MEATY_DESTRUCTIVE_TRACEABLE", "DYNAMIC_Q1_EXPANSION", "EARLY_TAIJI_GENERATION"},
                "BEAR_REVERSAL_PROBE": {"BEAR_LATE_STAGE_EVIDENCE"},
            }[decision]
            if row.get("unknown_gate") not in allowed_unknowns or blockers != []:
                raise ValueError(f"{review_id}: invalid approval fields")
            allowed_phases = {"LR", "RL", "RR", "DIRECT_TO_RIGHT"}
            if str(row.get("phase")) not in allowed_phases:
                raise ValueError(f"{review_id}: invalid approval phase")
        if not str(row.get("reason") or "").strip():
            raise ValueError(f"{review_id}: missing reason")
        ordered.append(row)
    return ordered


def _call(api_key: str, batch_id: str, candidates: list[dict[str, Any]], timeout: float) -> tuple[dict[str, Any], dict[str, Any]]:
    user_prompt = RULE_PROMPT + "\n批次與輸入：\n" + json.dumps(
        {"batch_id": batch_id, "candidates": candidates}, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    payload = {
        "model": MODEL,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_prompt}],
        "temperature": 0,
        "max_completion_tokens": 16384,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.post(f"{BASE_URL}/chat/completions", headers=headers, json=payload)
        response.raise_for_status()
        raw = response.json()
    choice = (raw.get("choices") or [{}])[0]
    content = str((choice.get("message") or {}).get("content") or "")
    parsed = _extract_json(content)
    diagnostics = {
        "requested_model": MODEL,
        "returned_model": raw.get("model"),
        "finish_reason": choice.get("finish_reason"),
        "usage": raw.get("usage") or {},
        "system_prompt_sha256": _sha_text(SYSTEM_PROMPT),
        "rule_prompt_sha256": _sha_text(RULE_PROMPT),
        "temperature": 0,
        "candidate_count": len(candidates),
    }
    return {"provider_response": raw, "parsed": parsed}, diagnostics


def run(*, start: int, limit: int | None, call_size: int, timeout: float) -> dict[str, Any]:
    manifest = _read(BATCH_MANIFEST)
    source_files = manifest["files"]
    source_rows: list[dict[str, Any]] = []
    for file_info in source_files:
        path = Path(file_info["jsonl"])
        actual = _sha_bytes(path.read_bytes())
        if actual != file_info["jsonl_sha256"]:
            raise ValueError(f"input hash changed: {path}")
        source_rows.extend(json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip())
    # Digest files are round-major and contain no same-stock duplicates within
    # each source batch.  Split only inside those boundaries.
    calls: list[dict[str, Any]] = []
    offset = 0
    for file_info in source_files:
        path = Path(file_info["jsonl"])
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
        for local in range(0, len(rows), call_size):
            chunk = rows[local:local + call_size]
            calls.append({
                "call_index": len(calls),
                "batch_id": f"r{int(file_info['round']):02d}b{int(file_info['batch']):02d}c{local // call_size:02d}",
                "source_jsonl": str(path.resolve()),
                "source_jsonl_sha256": file_info["jsonl_sha256"],
                "rows": chunk,
                "global_offset": offset + local,
            })
        offset += len(rows)
    selected = calls[start:] if limit is None else calls[start:start + limit]
    OUTPUT.mkdir(parents=True, exist_ok=True)
    api_key = _load_key()
    completed = []
    for call in selected:
        batch_id = call["batch_id"]
        result_path = OUTPUT / f"{call['call_index']:04d}_{batch_id}.json"
        if result_path.exists():
            saved = _read(result_path)
            _validate(batch_id, call["rows"], saved["result"]["parsed"])
            completed.append({"call_index": call["call_index"], "batch_id": batch_id, "status": "SKIPPED_VALID_EXISTING", "path": str(result_path.resolve()), "sha256": _sha_bytes(result_path.read_bytes())})
            continue
        candidates = [_candidate(row) for row in call["rows"]]
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                result, diagnostics = _call(api_key, batch_id, candidates, timeout)
                decisions = _validate(batch_id, call["rows"], result["parsed"])
                result["parsed"]["decisions"] = decisions
                envelope = {
                    "review_protocol": "FRESH_OUTCOME_BLIND_MINIMAX_M3_V3_REVIEW_V2",
                    "call_index": call["call_index"],
                    "batch_id": batch_id,
                    "source_jsonl": call["source_jsonl"],
                    "source_jsonl_sha256": call["source_jsonl_sha256"],
                    "review_ids": [row["review_id"] for row in call["rows"]],
                    "asof_only": True,
                    "future_performance_in_prompt": False,
                    "v1_signal_in_prompt": False,
                    "result": result,
                    "diagnostics": diagnostics,
                }
                result_path.write_text(json.dumps(envelope, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
                completed.append({"call_index": call["call_index"], "batch_id": batch_id, "status": "COMPLETED", "path": str(result_path.resolve()), "sha256": _sha_bytes(result_path.read_bytes()), "usage": diagnostics["usage"]})
                print(json.dumps({"call": call["call_index"], "batch": batch_id, "status": "COMPLETED", "decisions": len(decisions), "usage": diagnostics["usage"]}, ensure_ascii=False), flush=True)
                break
            except Exception as exc:  # preserve progress and retry malformed provider output
                last_error = exc
                print(json.dumps({"call": call["call_index"], "batch": batch_id, "attempt": attempt, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), flush=True)
                time.sleep(2 * attempt)
        else:
            raise RuntimeError(f"review failed for {batch_id}: {last_error}") from last_error
    run_manifest = {
        "protocol": "FRESH_OUTCOME_BLIND_MINIMAX_M3_V3_REVIEW_V2",
        "model": MODEL,
        "temperature": 0,
        "system_prompt_sha256": _sha_text(SYSTEM_PROMPT),
        "rule_prompt_sha256": _sha_text(RULE_PROMPT),
        "batch_manifest": str(BATCH_MANIFEST.resolve()),
        "batch_manifest_sha256": _sha_bytes(BATCH_MANIFEST.read_bytes()),
        "total_queue_rows": len(source_rows),
        "total_calls": len(calls),
        "call_size": call_size,
        "this_invocation": {"start": start, "limit": limit, "completed": completed},
    }
    invocation = OUTPUT / f"invocation_{start:04d}_{'all' if limit is None else start + len(selected) - 1:>04}.json"
    invocation.write_text(json.dumps(run_manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return run_manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--call-size", type=int, default=40)
    parser.add_argument("--timeout", type=float, default=600.0)
    args = parser.parse_args()
    print(json.dumps(run(start=args.start, limit=args.limit, call_size=args.call_size, timeout=args.timeout), ensure_ascii=True, indent=2))
