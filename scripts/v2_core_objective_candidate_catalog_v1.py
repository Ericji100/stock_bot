"""Build deterministic, outcome-blind segment/trigger/stop candidates for M2A.

The generated objects are finite *objective candidates*.  They are not AI
judgements and must never be treated as course anchors, triggers, or causal
stops until the formal AI stages and program truth table approve them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


ARTIFACT_DIR = (
    ROOT
    / "reports"
    / "course_backtest"
    / "2026-09-10"
    / "v2_core_reproducible_goal_v1"
)
CATALOG_VERSION = "v2-core-objective-candidate-catalog-r1-draft"
MANIFEST_VERSION = "v2-core-objective-candidate-manifest-r1-draft"
OUTPUT_DIRECTORY = "objective_candidate_catalogs_r1"
SCHEMA_FILE = "objective_candidate_catalog.schema.candidate_r1.json"


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stable_id(prefix: str, *parts: Any) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}-" + hashlib.sha256(
        f"{CATALOG_VERSION}|{payload}".encode("utf-8")
    ).hexdigest()[:20]


def _number(value: Any) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"expected positive finite number, found {value!r}")
    return round(number, 6)


def _round(value: float) -> float:
    return round(float(value), 6)


def _date_rows(packet: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rows = list(packet["daily_structure_context_to_as_of"])
    date_index = {str(row["date"]): index for index, row in enumerate(rows)}
    if len(date_index) != len(rows):
        raise ValueError("daily context contains duplicate dates")
    return rows, date_index


def _price_ref(date: str, evidence_refs: set[str]) -> str:
    ref = f"PRICE:{date}"
    if ref not in evidence_refs:
        raise ValueError(f"missing price evidence ref: {ref}")
    return ref


def _metrics(
    rows: list[dict[str, Any]],
    date_index: dict[str, int],
    *,
    start_date: str,
    through_date: str,
    start_price: float,
    observed_end_price: float,
    direction: str,
) -> dict[str, Any]:
    if start_date not in date_index or through_date not in date_index:
        raise ValueError(f"segment endpoint missing from daily context: {start_date}/{through_date}")
    start_index = date_index[start_date]
    end_index = date_index[through_date]
    if start_index > end_index:
        raise ValueError(f"segment endpoints reversed: {start_date}/{through_date}")
    segment = rows[start_index : end_index + 1]
    closes = [_number(row.get("adj_close") or row.get("close")) for row in segment]
    observed_net_change_pct = 100.0 * (observed_end_price / start_price - 1.0)
    direction_sign = 1.0 if direction == "UP" else -1.0
    total_path = sum(abs(right - left) for left, right in zip(closes, closes[1:]))
    path_efficiency = 1.0 if len(closes) == 1 else abs(closes[-1] - closes[0]) / total_path if total_path else 0.0
    max_countermove = 0.0
    if direction == "UP":
        running = closes[0]
        for close in closes[1:]:
            running = max(running, close)
            max_countermove = max(max_countermove, 100.0 * (running - close) / running)
    else:
        running = closes[0]
        for close in closes[1:]:
            running = min(running, close)
            max_countermove = max(max_countermove, 100.0 * (close - running) / running)
    atr = rows[start_index].get("atr14")
    atr_number = float(atr) if atr not in (None, "") else None
    directional_atr = (
        None
        if not atr_number or not math.isfinite(atr_number) or atr_number <= 0
        else direction_sign * (observed_end_price - start_price) / atr_number
    )
    return {
        "trading_bars": len(segment),
        "observed_net_change_pct": _round(observed_net_change_pct),
        "directional_move_pct": _round(direction_sign * observed_net_change_pct),
        "directional_move_atr_at_start": None if directional_atr is None else _round(directional_atr),
        "path_efficiency": _round(min(1.0, max(0.0, path_efficiency))),
        "max_countermove_pct": _round(max_countermove),
    }


def _segment_candidate(
    rows: list[dict[str, Any]],
    date_index: dict[str, int],
    evidence_refs: set[str],
    *,
    basis: str,
    scale: str,
    direction: str,
    status: str,
    start_date: str,
    start_price: Any,
    confirmed_end_date: str | None,
    confirmed_end_price: Any | None,
    observed_through: str,
    observed_end_price: Any,
    source_refs: list[str],
) -> dict[str, Any]:
    start = _number(start_price)
    observed_end = _number(observed_end_price)
    confirmed_price = None if confirmed_end_price is None else _number(confirmed_end_price)
    refs = list(dict.fromkeys(source_refs))
    if len(refs) < 2 or any(ref not in evidence_refs for ref in refs):
        raise ValueError(f"segment evidence refs invalid: {refs}")
    identity = (
        basis,
        scale,
        direction,
        status,
        start_date,
        start,
        confirmed_end_date,
        confirmed_price,
        observed_through,
        observed_end,
        refs,
    )
    return {
        "candidate_id": stable_id("SEG", *identity),
        "basis": basis,
        "scale": scale,
        "direction": direction,
        "status": status,
        "start_date": start_date,
        "start_price": start,
        "confirmed_end_date": confirmed_end_date,
        "confirmed_end_price": confirmed_price,
        "observed_through": observed_through,
        "observed_end_price": observed_end,
        "source_evidence_refs": refs,
        "objective_metrics": _metrics(
            rows,
            date_index,
            start_date=start_date,
            through_date=observed_through,
            start_price=start,
            observed_end_price=observed_end,
            direction=direction,
        ),
        "not_course_anchor_judgement": True,
    }


def build_segment_candidates(packet: dict[str, Any]) -> list[dict[str, Any]]:
    rows, date_index = _date_rows(packet)
    evidence_refs = {str(item["ref"]) for item in packet["evidence_catalog"]}
    as_of = str(packet["as_of"])
    last_close = _number(rows[-1].get("adj_close") or rows[-1].get("close"))
    candidates: list[dict[str, Any]] = []

    cycles = list(packet["completed_macd_21_55_55_cycles_to_as_of"])
    for index, (left, right) in enumerate(zip(cycles, cycles[1:])):
        left_ref = f"MACD:CYCLE-{index + 1}"
        right_ref = f"MACD:CYCLE-{index + 2}"
        if left_ref not in evidence_refs or right_ref not in evidence_refs:
            raise ValueError("MACD evidence catalog is incomplete")
        signs = (str(left.get("sign")), str(right.get("sign")))
        if signs == ("NEGATIVE", "POSITIVE"):
            basis = "MACD_NEGATIVE_TO_POSITIVE"
            direction = "UP"
            start_date, start_price = str(left["low_date"]), left["low"]
            end_date, end_price = str(right["high_date"]), right["high"]
        elif signs == ("POSITIVE", "NEGATIVE"):
            basis = "MACD_POSITIVE_TO_NEGATIVE"
            direction = "DOWN"
            start_date, start_price = str(left["high_date"]), left["high"]
            end_date, end_price = str(right["low_date"]), right["low"]
        else:
            continue
        if (
            start_date > end_date
            or end_date > as_of
            or start_date not in date_index
            or end_date not in date_index
        ):
            continue
        candidates.append(
            _segment_candidate(
                rows,
                date_index,
                evidence_refs,
                basis=basis,
                scale="AUXILIARY",
                direction=direction,
                status="CONFIRMED",
                start_date=start_date,
                start_price=start_price,
                confirmed_end_date=end_date,
                confirmed_end_price=end_price,
                observed_through=end_date,
                observed_end_price=end_price,
                source_refs=[
                    left_ref,
                    right_ref,
                    _price_ref(start_date, evidence_refs),
                    _price_ref(end_date, evidence_refs),
                ],
            )
        )

    pivots = list(packet["confirmed_pivots_to_as_of"])
    indexed_pivots: list[tuple[dict[str, Any], str]] = []
    for index, pivot in enumerate(pivots):
        ref = next(
            (
                str(item["ref"])
                for item in packet["evidence_catalog"]
                if str(item.get("path")) == f"/confirmed_pivots_to_as_of/{index}"
            ),
            "",
        )
        if not ref:
            raise ValueError(f"pivot evidence ref missing at index {index}")
        indexed_pivots.append((pivot, ref))

    for scale in ("LARGE", "SMALL"):
        scoped = sorted(
            (
                (pivot, ref)
                for pivot, ref in indexed_pivots
                if str(pivot.get("scale")) == scale
            ),
            key=lambda item: (
                str(item[0].get("source_date")),
                str(item[0].get("confirmation_date")),
                str(item[0].get("side")),
            ),
        )
        for (left, left_ref), (right, right_ref) in zip(scoped, scoped[1:]):
            left_date = str(left["source_date"])
            right_date = str(right["source_date"])
            sides = (str(left.get("side")), str(right.get("side")))
            if (
                left_date >= right_date
                or left_date not in date_index
                or right_date not in date_index
                or sides not in {("LOW", "HIGH"), ("HIGH", "LOW")}
            ):
                continue
            direction = "UP" if sides == ("LOW", "HIGH") else "DOWN"
            candidates.append(
                _segment_candidate(
                    rows,
                    date_index,
                    evidence_refs,
                    basis="CONFIRMED_PIVOT_LEG",
                    scale=scale,
                    direction=direction,
                    status="CONFIRMED",
                    start_date=left_date,
                    start_price=left["price"],
                    confirmed_end_date=right_date,
                    confirmed_end_price=right["price"],
                    observed_through=right_date,
                    observed_end_price=right["price"],
                    source_refs=[
                        left_ref,
                        right_ref,
                        _price_ref(left_date, evidence_refs),
                        _price_ref(right_date, evidence_refs),
                    ],
                )
            )
        for pivot, pivot_ref in scoped[-3:]:
            start_date = str(pivot["source_date"])
            if start_date >= as_of or start_date not in date_index:
                continue
            side = str(pivot["side"])
            direction = "UP" if side == "LOW" else "DOWN"
            candidates.append(
                _segment_candidate(
                    rows,
                    date_index,
                    evidence_refs,
                    basis="FORMING_FROM_CONFIRMED_PIVOT",
                    scale=scale,
                    direction=direction,
                    status="FORMING",
                    start_date=start_date,
                    start_price=pivot["price"],
                    confirmed_end_date=None,
                    confirmed_end_price=None,
                    observed_through=as_of,
                    observed_end_price=last_close,
                    source_refs=[
                        pivot_ref,
                        _price_ref(start_date, evidence_refs),
                        _price_ref(as_of, evidence_refs),
                    ],
                )
            )

    unique = {row["candidate_id"]: row for row in candidates}
    return sorted(
        unique.values(),
        key=lambda row: (
            row["observed_through"],
            row["start_date"],
            row["scale"],
            row["basis"],
            row["candidate_id"],
        ),
    )


def segment_shortlist(candidates: list[dict[str, Any]]) -> list[str]:
    selected: dict[str, dict[str, Any]] = {}

    def add(rows: list[dict[str, Any]]) -> None:
        for row in rows:
            selected[row["candidate_id"]] = row

    forming = [row for row in candidates if row["status"] == "FORMING"]
    add(sorted(forming, key=lambda row: (row["start_date"], row["candidate_id"]), reverse=True)[:4])
    groups = (
        ([row for row in candidates if row["basis"].startswith("MACD_")], 8, 6),
        ([row for row in candidates if row["basis"] == "CONFIRMED_PIVOT_LEG" and row["scale"] == "LARGE"], 8, 4),
        ([row for row in candidates if row["basis"] == "CONFIRMED_PIVOT_LEG" and row["scale"] == "SMALL"], 6, 2),
    )
    for rows, recent_count, magnitude_count in groups:
        add(sorted(rows, key=lambda row: (row["observed_through"], row["start_date"], row["candidate_id"]), reverse=True)[:recent_count])
        add(
            sorted(
                rows,
                key=lambda row: (
                    abs(row["objective_metrics"]["directional_move_atr_at_start"] or 0.0),
                    abs(row["objective_metrics"]["directional_move_pct"]),
                    row["observed_through"],
                    row["candidate_id"],
                ),
                reverse=True,
            )[:magnitude_count]
        )
    ordered = sorted(
        selected.values(),
        key=lambda row: (row["observed_through"], row["start_date"], row["candidate_id"]),
        reverse=True,
    )
    if len(ordered) > 40:
        raise ValueError(f"segment shortlist exceeds contract: {len(ordered)}")
    return [row["candidate_id"] for row in ordered]


def build_stop_candidates(packet: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    as_of = str(packet["as_of"])
    for index, pivot in enumerate(packet["confirmed_pivots_to_as_of"]):
        source_date = str(pivot["source_date"])
        confirmed_on = str(pivot["confirmation_date"])
        if max(source_date, confirmed_on) > as_of:
            raise ValueError("future pivot reached objective stop catalog")
        ref = next(
            (
                str(item["ref"])
                for item in packet["evidence_catalog"]
                if str(item.get("path")) == f"/confirmed_pivots_to_as_of/{index}"
            ),
            "",
        )
        if not ref:
            raise ValueError(f"pivot evidence ref missing at index {index}")
        scale = str(pivot["scale"])
        side = str(pivot["side"])
        if scale not in {"LARGE", "SMALL"} or side not in {"LOW", "HIGH"}:
            continue
        identity = (ref, source_date, confirmed_on, _number(pivot["price"]), scale, side)
        candidates.append(
            {
                "candidate_id": stable_id("STOPC", *identity),
                "source_pivot_ref": ref,
                "source_date": source_date,
                "confirmed_on": confirmed_on,
                "price": _number(pivot["price"]),
                "scale": scale,
                "side": side,
                "objective_defense_direction": "BULL_DEFENSE" if side == "LOW" else "BEAR_DEFENSE",
                "not_course_stop_judgement": True,
            }
        )
    return sorted(candidates, key=lambda row: (row["confirmed_on"], row["source_date"], row["candidate_id"]))


def stop_shortlist(candidates: list[dict[str, Any]]) -> list[str]:
    selected: dict[str, dict[str, Any]] = {}
    for scale in ("LARGE", "SMALL"):
        for side in ("LOW", "HIGH"):
            rows = [row for row in candidates if row["scale"] == scale and row["side"] == side]
            for row in sorted(rows, key=lambda item: (item["confirmed_on"], item["source_date"], item["candidate_id"]), reverse=True)[:5]:
                selected[row["candidate_id"]] = row
    ordered = sorted(
        selected.values(),
        key=lambda row: (row["confirmed_on"], row["source_date"], row["candidate_id"]),
        reverse=True,
    )
    if len(ordered) > 20:
        raise ValueError(f"stop shortlist exceeds contract: {len(ordered)}")
    return [row["candidate_id"] for row in ordered]


def build_trigger_candidates(packet: dict[str, Any]) -> list[dict[str, Any]]:
    as_of = str(packet["as_of"])
    last = packet["daily_structure_context_to_as_of"][-1]
    if str(last["date"]) != as_of:
        raise ValueError("last daily row does not equal AS-OF")
    price_ref = f"PRICE:{as_of}"
    evidence_refs = {str(item["ref"]) for item in packet["evidence_catalog"]}
    if price_ref not in evidence_refs:
        raise ValueError("AS-OF price ref missing")
    candidates = []
    for event_code in sorted(set(str(value) for value in (last.get("facts") or []))):
        candidates.append(
            {
                "candidate_id": stable_id("TRIGC", event_code, as_of, price_ref),
                "event_code": event_code,
                "event_date": as_of,
                "source_evidence_refs": [price_ref],
                "not_course_trigger_judgement": True,
            }
        )
    return candidates


def validate_catalog(catalog: dict[str, Any], schema: dict[str, Any]) -> None:
    errors = sorted(
        Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER).iter_errors(catalog),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        detail = "; ".join(f"{'/'.join(map(str, error.absolute_path))}: {error.message}" for error in errors[:10])
        raise ValueError(f"candidate catalog schema invalid: {detail}")
    as_of = str(catalog["as_of"])
    segment_ids = [row["candidate_id"] for row in catalog["objective_segment_candidates"]]
    stop_ids = [row["candidate_id"] for row in catalog["objective_stop_candidates"]]
    trigger_ids = [row["candidate_id"] for row in catalog["objective_trigger_candidates"]]
    if len(segment_ids) != len(set(segment_ids)) or len(stop_ids) != len(set(stop_ids)) or len(trigger_ids) != len(set(trigger_ids)):
        raise ValueError("candidate IDs are not unique")
    if not set(catalog["prompt_shortlist_segment_ids"]).issubset(segment_ids):
        raise ValueError("segment shortlist references missing candidate")
    if not set(catalog["prompt_shortlist_stop_ids"]).issubset(stop_ids):
        raise ValueError("stop shortlist references missing candidate")
    for row in catalog["objective_segment_candidates"]:
        dates = [row["start_date"], row["observed_through"]]
        if row["confirmed_end_date"] is not None:
            dates.append(row["confirmed_end_date"])
        if max(dates) > as_of:
            raise ValueError("future date in segment candidate")
        if row["status"] == "FORMING" and (row["confirmed_end_date"] is not None or row["confirmed_end_price"] is not None):
            raise ValueError("forming segment has a confirmed endpoint")
        if row["status"] == "CONFIRMED" and (row["confirmed_end_date"] is None or row["confirmed_end_price"] is None):
            raise ValueError("confirmed segment lacks endpoint")
    for row in catalog["objective_stop_candidates"]:
        if max(row["source_date"], row["confirmed_on"]) > as_of:
            raise ValueError("future date in stop candidate")
    for row in catalog["objective_trigger_candidates"]:
        if row["event_date"] != as_of:
            raise ValueError("trigger candidate is not an AS-OF event")


def build_catalog(packet: dict[str, Any], packet_sha256: str, schema: dict[str, Any]) -> dict[str, Any]:
    segments = build_segment_candidates(packet)
    stops = build_stop_candidates(packet)
    catalog = {
        "catalog_version": CATALOG_VERSION,
        "status": "OBJECTIVE_CANDIDATES_ONLY（僅客觀候選、非課程判讀）",
        "review_id": str(packet["review_id"]),
        "anonymous_stock_id": str(packet["anonymous_stock_id"]),
        "as_of": str(packet["as_of"]),
        "source_packet_sha256": packet_sha256,
        "generator_contract": {
            "candidate_only": True,
            "ai_course_judgement_included": False,
            "future_performance_used": False,
            "sealed_labels_used": False,
            "identity_used": False,
            "source_packet_unchanged": True,
            "shortlist_uses_outcomes": False,
        },
        "objective_segment_candidates": segments,
        "prompt_shortlist_segment_ids": segment_shortlist(segments),
        "objective_trigger_candidates": build_trigger_candidates(packet),
        "objective_stop_candidates": stops,
        "prompt_shortlist_stop_ids": stop_shortlist(stops),
    }
    validate_catalog(catalog, schema)
    return catalog


def write_new_or_identical(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() != payload:
        raise RuntimeError(f"refusing to overwrite changed versioned artifact: {path}")
    path.write_bytes(payload)


def build_all(artifact_dir: Path) -> dict[str, Any]:
    source_manifest_path = artifact_dir / "feasibility_probe_manifest.json"
    source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    schema_path = artifact_dir / SCHEMA_FILE
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    output_dir = artifact_dir / OUTPUT_DIRECTORY
    rows = []
    for item in source_manifest["rows"]:
        packet_path = artifact_dir / source_manifest["packet_directory"] / str(item["packet_file"])
        packet_hash_before = sha256_path(packet_path)
        if packet_hash_before != str(item["packet_sha256"]):
            raise RuntimeError(f"source packet hash mismatch: {packet_path}")
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        catalog = build_catalog(packet, packet_hash_before, schema)
        output_path = output_dir / f"{catalog['review_id']}.json"
        write_new_or_identical(output_path, canonical_bytes(catalog))
        if sha256_path(packet_path) != packet_hash_before:
            raise RuntimeError(f"source packet changed during build: {packet_path}")
        rows.append(
            {
                "review_id": catalog["review_id"],
                "as_of": catalog["as_of"],
                "source_packet_file": str(item["packet_file"]),
                "source_packet_sha256": packet_hash_before,
                "catalog_file": output_path.name,
                "catalog_sha256": sha256_path(output_path),
                "segment_candidate_count": len(catalog["objective_segment_candidates"]),
                "segment_shortlist_count": len(catalog["prompt_shortlist_segment_ids"]),
                "trigger_candidate_count": len(catalog["objective_trigger_candidates"]),
                "stop_candidate_count": len(catalog["objective_stop_candidates"]),
                "stop_shortlist_count": len(catalog["prompt_shortlist_stop_ids"]),
            }
        )
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "status": "DRAFT_OBJECTIVE_CATALOG_COMPLETE（客觀候選目錄草案完成）",
        "source_manifest_file": source_manifest_path.name,
        "source_manifest_sha256": sha256_path(source_manifest_path),
        "source_packet_directory": str(source_manifest["packet_directory"]),
        "catalog_directory": OUTPUT_DIRECTORY,
        "catalog_schema_file": SCHEMA_FILE,
        "catalog_schema_sha256": sha256_path(schema_path),
        "case_count": len(rows),
        "formal_ai_calls": 0,
        "future_performance_used": False,
        "sealed_labels_used": False,
        "identity_used": False,
        "course_judgement_generated_by_program": False,
        "relation_candidates_complete": False,
        "ready_for_formal_ai": False,
        "rows": rows,
    }
    write_new_or_identical(artifact_dir / "objective_candidate_catalog_manifest_r1.json", canonical_bytes(manifest))
    return manifest


def markdown(manifest: dict[str, Any]) -> str:
    rows = manifest["rows"]
    total = lambda key: sum(int(row[key]) for row in rows)
    return "\n".join(
        [
            "# V2核心客觀候選目錄草案 R1",
            "",
            f"- 狀態：`{manifest['status']}`",
            f"- 匿名AS-OF案例：{manifest['case_count']}",
            "- 正式AI呼叫：0",
            "- 未讀取股票身分、sealed標籤或未來績效。",
            "- 程式只列出有限客觀候選，沒有產生定錨、太極、象限、道氏、位階、課程觸發或因果防線判讀。",
            "- 原48包未修改；每個sidecar都記錄來源封包與自身SHA-256。",
            "",
            "## 數量",
            "",
            "| 項目 | 數量 |",
            "|---|---:|",
            f"| 客觀片段候選 | {total('segment_candidate_count')} |",
            f"| 片段prompt短名單 | {total('segment_shortlist_count')} |",
            f"| AS-OF客觀事件候選 | {total('trigger_candidate_count')} |",
            f"| 已確認樞紐防線候選 | {total('stop_candidate_count')} |",
            f"| 防線prompt短名單 | {total('stop_shortlist_count')} |",
            "",
            "## 尚未完成",
            "",
            "- 父代—修正—複製與左右關係的有限候選尚未建立。",
            "- AI只能引用候選ID的Stage B1／B2 schema與prompt尚未建立。",
            "- 控制定錨、情境、觸發及防線的tie-break尚未校準與凍結。",
            "- 本草案不得送入正式AI、不得宣稱M2A改善，也不得用於交易或績效回測。",
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    args = parser.parse_args()
    artifact_dir = args.artifact_dir.resolve()
    manifest = build_all(artifact_dir)
    write_new_or_identical(
        artifact_dir / "objective_candidate_catalog_manifest_r1.md",
        markdown(manifest).encode("utf-8"),
    )
    print(
        json.dumps(
            {
                "status": manifest["status"],
                "case_count": manifest["case_count"],
                "formal_ai_calls": 0,
                "manifest": str((artifact_dir / "objective_candidate_catalog_manifest_r1.json").resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
