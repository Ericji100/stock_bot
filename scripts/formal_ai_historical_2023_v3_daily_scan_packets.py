"""Build identity-blind, outcome-free daily packets for the 2023-H2 V3 review.

Every stock-day from the stock's first upstream selection through 2024-02-02
is represented.  The builder extracts dated facts only; it never classifies a
scenario, approves a trade, carries a position state, or reads performance.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v1_v2"
RUN = ROOT / "reports/course_backtest/2024-02-02/historical_scan_2023h2_formal_ai_v3_daily_scan"
PARENT_LEDGER = PARENT / "ai_decisions_merged.jsonl"
PARENT_MANIFEST = PARENT / "input_manifest.json"
PARENT_VALIDATION = PARENT / "decision_validation.json"
PACKET_ROOT = PARENT / "review_packets"
IDENTITY_MAP = RUN / "sealed_identity_map.json"
DAILY_INDEX = RUN / "daily_scan_index.jsonl"
ROUND_DIR = RUN / "daily_rounds"
MANIFEST = RUN / "packet_manifest.json"
AS_OF = "2024-02-02"
EXPECTED_STOCKS = 1_029


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _identity_state(rows: list[dict[str, Any]]) -> tuple[bytes, dict[str, str]]:
    if IDENTITY_MAP.exists():
        stored = _read(IDENTITY_MAP)
        salt = bytes.fromhex(str(stored["salt_hex"]))
        mapping = {str(item["code"]): str(item["anonymous_id"]) for item in stored["mapping"]}
        return salt, mapping
    salt = secrets.token_bytes(32)
    mapping = {
        str(row["code"]): "S-" + hmac.new(salt, str(row["code"]).encode("utf-8"), hashlib.sha256).hexdigest()[:16]
        for row in rows
    }
    _write_json(
        IDENTITY_MAP,
        {
            "status": "SEALED_UNTIL_ALL_AI_DAILY_DECISIONS_LOCKED",
            "salt_hex": salt.hex(),
            "mapping": [
                {"index": row["index"], "code": str(row["code"]), "name": row.get("name"), "anonymous_id": mapping[str(row["code"])]}
                for row in rows
            ],
        },
    )
    return salt, mapping


def _selection_asof(packet: dict[str, Any], day: str) -> dict[str, Any]:
    timeline = {str(key): value for key, value in packet.get("selection_timeline", {}).items() if str(key) <= day}
    return {
        "selected_today": timeline.get(day, []),
        "accumulated_sources": sorted({source for values in timeline.values() for source in values}),
        "selected_before_or_on_day": bool(timeline),
    }


def _pivots_asof(packet: dict[str, Any], day: str) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for pivot in packet.get("confirmed_pivots", []):
        if str(pivot["confirmation_date"]) <= day:
            grouped[(str(pivot["scale"]), str(pivot["side"]))].append(pivot)
    output: list[dict[str, Any]] = []
    for key in (("LARGE", "LOW"), ("LARGE", "HIGH"), ("SMALL", "LOW"), ("SMALL", "HIGH")):
        output.extend(grouped[key][-6:])
    return sorted(output, key=lambda row: (str(row["confirmation_date"]), str(row["scale"]), str(row["side"])))


def _cycles_asof(packet: dict[str, Any], day: str) -> list[dict[str, Any]]:
    return [row for row in packet.get("macd_21_55_55_cycles", []) if row.get("end") and str(row["end"]) <= day][-6:]


def _recent_asof(all_days: list[dict[str, Any]], offset: int) -> list[dict[str, Any]]:
    keep = (
        "date", "open", "high", "low", "close", "volume_ratio_20", "atr14",
        "ma5", "ma13", "ma21", "ma55", "ma105", "ma144", "macd_hist", "macd_hist_delta", "facts",
    )
    return [{key: row.get(key) for key in keep} for row in all_days[max(0, offset - 29): offset + 1]]


def build() -> dict[str, Any]:
    parent_validation = _read(PARENT_VALIDATION)
    if _sha(PARENT_LEDGER) != "8b239b5f2d4ec8f973df49bf7360013f21ad44bad7fd0d80ab359ff00ac387b4":
        raise ValueError("frozen parent ledger changed")
    rows = _jsonl(PARENT_LEDGER)
    if len(rows) != EXPECTED_STOCKS:
        raise ValueError(f"expected {EXPECTED_STOCKS} stocks, got {len(rows)}")
    identity_salt, identities = _identity_state(rows)

    ROUND_DIR.mkdir(parents=True, exist_ok=True)
    DAILY_INDEX.parent.mkdir(parents=True, exist_ok=True)
    round_handles: dict[str, Any] = {}
    round_counts: defaultdict[str, int] = defaultdict(int)
    stock_days = 0
    index_handle = DAILY_INDEX.open("w", encoding="utf-8")
    try:
        for row in rows:
            code = str(row["code"])
            anonymous_id = identities[code]
            source_path = PACKET_ROOT / f"{code}.json"
            source_sha = _sha(source_path)
            packet = _read(source_path)
            first = str(row["first_selected_on"])
            days = [value for value in packet["daily_visible_facts_from_monitoring"] if first <= str(value["date"]) <= AS_OF]
            for offset, current in enumerate(days):
                day = str(current["date"])
                opaque_review_id = hmac.new(
                    identity_salt,
                    f"{code}|{day}".encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest()[:24]
                compact = {
                "review_id": f"D-{opaque_review_id}",
                "anonymous_stock_id": anonymous_id,
                "review_as_of": day,
                "stock_day_ordinal": offset,
                "identity_visible": False,
                "outcome_exclusion_allowed": False,
                "selection_asof": _selection_asof(packet, day),
                "data_quality": packet["data_quality"],
                "candidate_day": current,
                "recent_daily_visible_facts_asof": _recent_asof(days, offset),
                "completed_macd_cycles_asof": _cycles_asof(packet, day),
                "confirmed_pivots_asof": _pivots_asof(packet, day),
                "review_contract": {
                    "rule": "V3_ONLY",
                    "required_daily_state": True,
                    "future_bars_present": False,
                    "future_performance_present": False,
                    "same_stock_future_day_visible": False,
                    "allowed_states": [
                        "NO_SETUP", "WATCHING", "ARMED", "TRIGGERED", "HOLDING",
                        "ADD_ARMED", "EXIT_WARNING", "EXIT_TRIGGERED", "REMOVED",
                    ],
                    "allowed_routes": [
                        "V2_CORE", "NEAR_PASS_MACRO_COPY", "NEAR_PASS_FRESH_Q1",
                        "BEAR_REVERSAL_PROBE", "NO_TRADE",
                    ],
                },
                }
                if day not in round_handles:
                    round_handles[day] = (ROUND_DIR / f"round_{day}.jsonl").open("w", encoding="utf-8")
                round_handles[day].write(json.dumps(compact, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n")
                index_row = {
                    "review_id": compact["review_id"],
                    "anonymous_stock_id": anonymous_id,
                    "review_as_of": day,
                    "stock_day_ordinal": offset,
                    "source_packet_sha256": source_sha,
                }
                index_handle.write(json.dumps(index_row, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n")
                round_counts[day] += 1
                stock_days += 1
    finally:
        index_handle.close()
        for handle in round_handles.values():
            handle.close()

    round_files = []
    for round_no, day in enumerate(sorted(round_counts)):
        path = ROUND_DIR / f"round_{day}.jsonl"
        round_files.append({"round": round_no, "date": day, "rows": round_counts[day], "path": str(path.resolve()), "sha256": _sha(path)})
    manifest = {
        "method_version": "formal-codex-ai-v3-every-monitored-day-packets-v1",
        "boundary": "FACTS_ONLY_NO_AI_DECISION_NO_PERFORMANCE",
        "as_of": AS_OF,
        "stocks": len(rows),
        "stock_days": stock_days,
        "trading_day_rounds": len(round_files),
        "outcome_excluded_codes": [],
        "identity_map": {"path": str(IDENTITY_MAP.resolve()), "sha256": _sha(IDENTITY_MAP), "sealed": True},
        "daily_index": {"path": str(DAILY_INDEX.resolve()), "sha256": _sha(DAILY_INDEX)},
        "parent_ledger": {"path": str(PARENT_LEDGER.resolve()), "sha256": _sha(PARENT_LEDGER)},
        "parent_manifest": {"path": str(PARENT_MANIFEST.resolve()), "sha256": _sha(PARENT_MANIFEST)},
        "parent_validation": {"path": str(PARENT_VALIDATION.resolve()), "sha256": _sha(PARENT_VALIDATION)},
        "v3_rule": {"path": str((ROOT / 'docs/enlightenment-ai-judgement-v3.md').resolve()), "sha256": _sha(ROOT / "docs/enlightenment-ai-judgement-v3.md")},
        "v3_machine_rules": {"path": str((ROOT / 'config/enlightenment_ai_rules_v3.json').resolve()), "sha256": _sha(ROOT / "config/enlightenment_ai_rules_v3.json")},
        "rounds": round_files,
    }
    _write_json(MANIFEST, manifest)
    return manifest


if __name__ == "__main__":
    result = build()
    print(json.dumps({key: result[key] for key in ("method_version", "stocks", "stock_days", "trading_day_rounds", "outcome_excluded_codes")}, ensure_ascii=False, indent=2))
