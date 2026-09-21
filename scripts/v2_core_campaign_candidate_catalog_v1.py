"""Build outcome-blind campaign-scale candidates for M2A reference alignment.

This is an additive candidate representation.  It does not modify the frozen
R1 objective catalog and it never assigns a course role, scenario, or trade
permission.  Calibration labels and legacy AI answers are deliberately absent
from every input used by this generator.
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
CATALOG_VERSION = "v2-core-campaign-candidate-catalog-r1-candidate"
MANIFEST_VERSION = "v2-core-campaign-candidate-manifest-r1-candidate"
OUTPUT_DIRECTORY = "campaign_candidate_catalogs_candidate_r1"
SCHEMA_FILE = "campaign_candidate_catalog.schema.candidate_r1.json"
SOURCE_PACKET_DIRECTORY = "feasibility_probe_packets_r2"
SOURCE_MANIFEST_FILE = "feasibility_probe_manifest.json"


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_id(prefix: str, *parts: Any) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(f"{CATALOG_VERSION}|{payload}".encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:20]}"


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
    if not rows or len(rows) != len(date_index):
        raise ValueError("daily context is empty or contains duplicate dates")
    if str(rows[-1]["date"]) != str(packet["as_of"]):
        raise ValueError("daily context does not end at AS-OF")
    return rows, date_index


def _price(row: dict[str, Any]) -> float:
    return _number(row.get("adj_close") or row.get("close"))


def _price_ref(value: str, evidence_refs: set[str]) -> str:
    ref = f"PRICE:{value}"
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
    start_index = date_index[start_date]
    end_index = date_index[through_date]
    if start_index > end_index:
        raise ValueError(f"segment endpoints reversed: {start_date}/{through_date}")
    segment = rows[start_index : end_index + 1]
    closes = [_price(row) for row in segment]
    net_change = 100.0 * (observed_end_price / start_price - 1.0)
    direction_sign = 1.0 if direction == "UP" else -1.0
    total_path = sum(abs(right - left) for left, right in zip(closes, closes[1:]))
    efficiency = (
        1.0
        if len(closes) == 1
        else abs(closes[-1] - closes[0]) / total_path
        if total_path
        else 0.0
    )
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
        "observed_net_change_pct": _round(net_change),
        "directional_move_pct": _round(direction_sign * net_change),
        "directional_move_atr_at_start": (
            None if directional_atr is None else _round(directional_atr)
        ),
        "path_efficiency": _round(min(1.0, max(0.0, efficiency))),
        "max_countermove_pct": _round(max_countermove),
    }


def _candidate(
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
    component_refs: list[str],
) -> dict[str, Any]:
    start = _number(start_price)
    observed_end = _number(observed_end_price)
    confirmed_price = None if confirmed_end_price is None else _number(confirmed_end_price)
    refs = list(dict.fromkeys(component_refs))
    if len(refs) < 2 or any(ref not in evidence_refs for ref in refs):
        raise ValueError(f"candidate evidence refs invalid: {refs}")
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
        "candidate_id": stable_id("CAMSEG", *identity),
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
        "component_count": len(refs),
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


def _macd_candidates(
    packet: dict[str, Any],
    rows: list[dict[str, Any]],
    date_index: dict[str, int],
    evidence_refs: set[str],
) -> list[dict[str, Any]]:
    as_of = str(packet["as_of"])
    cycles = list(packet["completed_macd_21_55_55_cycles_to_as_of"])
    candidates: list[dict[str, Any]] = []
    cycle_refs: list[str] = []
    for index, cycle in enumerate(cycles):
        cycle_ref = f"MACD:CYCLE-{index + 1}"
        if cycle_ref not in evidence_refs:
            raise ValueError(f"missing MACD evidence ref: {cycle_ref}")
        cycle_refs.append(cycle_ref)
        start_date = str(cycle["start"])
        end_date = str(cycle["end"])
        if start_date not in date_index or end_date not in date_index:
            continue
        direction = "UP" if str(cycle["sign"]) == "POSITIVE" else "DOWN"
        candidates.append(
            _candidate(
                rows,
                date_index,
                evidence_refs,
                basis="MACD_COMPLETED_REGIME_SPAN",
                scale="REGIME",
                direction=direction,
                status="CONFIRMED",
                start_date=start_date,
                start_price=cycle["start_close"],
                confirmed_end_date=end_date,
                confirmed_end_price=cycle["last_close"],
                observed_through=end_date,
                observed_end_price=cycle["last_close"],
                component_refs=[
                    cycle_ref,
                    _price_ref(start_date, evidence_refs),
                    _price_ref(end_date, evidence_refs),
                ],
            )
        )

    # A bounded, label-independent family of multi-regime spans.  This exposes
    # a full campaign object without claiming that every span is a course anchor.
    for start_index in range(len(cycles)):
        for end_index in range(start_index + 1, min(len(cycles), start_index + 4)):
            left, right = cycles[start_index], cycles[end_index]
            start_date = str(left["start"])
            end_date = str(right["end"])
            if start_date not in date_index or end_date not in date_index:
                continue
            start_price = _number(left["start_close"])
            end_price = _number(right["last_close"])
            if start_price == end_price:
                continue
            direction = "UP" if end_price > start_price else "DOWN"
            candidates.append(
                _candidate(
                    rows,
                    date_index,
                    evidence_refs,
                    basis="MACD_MULTI_REGIME_SPAN",
                    scale="CAMPAIGN",
                    direction=direction,
                    status="CONFIRMED",
                    start_date=start_date,
                    start_price=start_price,
                    confirmed_end_date=end_date,
                    confirmed_end_price=end_price,
                    observed_through=end_date,
                    observed_end_price=end_price,
                    component_refs=[
                        *cycle_refs[start_index : end_index + 1],
                        _price_ref(start_date, evidence_refs),
                        _price_ref(end_date, evidence_refs),
                    ],
                )
            )

    # The packet stores only completed MACD regimes, so derive the current sign
    # run from AS-OF daily rows.  No future bar is consulted.
    last_hist = rows[-1].get("macd_hist")
    last_sign = 1 if last_hist is not None and float(last_hist) > 0 else -1 if last_hist is not None and float(last_hist) < 0 else 0
    if last_sign:
        start_row = rows[-1]
        for row in reversed(rows):
            hist = row.get("macd_hist")
            sign = 1 if hist is not None and float(hist) > 0 else -1 if hist is not None and float(hist) < 0 else 0
            if sign != last_sign:
                break
            start_row = row
        current_direction = "UP" if last_sign > 0 else "DOWN"
        current_refs = [
            _price_ref(str(start_row["date"]), evidence_refs),
            _price_ref(as_of, evidence_refs),
        ]
        if cycle_refs:
            current_refs.insert(0, cycle_refs[-1])
        candidates.append(
            _candidate(
                rows,
                date_index,
                evidence_refs,
                basis="MACD_FORMING_REGIME_SPAN",
                scale="REGIME",
                direction=current_direction,
                status="FORMING",
                start_date=str(start_row["date"]),
                start_price=_price(start_row),
                confirmed_end_date=None,
                confirmed_end_price=None,
                observed_through=as_of,
                observed_end_price=_price(rows[-1]),
                component_refs=current_refs,
            )
        )

        # Also expose longer forming campaigns beginning at recent completed
        # regime starts.  Direction is objective endpoint direction, not a role.
        for index in range(max(0, len(cycles) - 8), len(cycles)):
            cycle = cycles[index]
            start_date = str(cycle["start"])
            if start_date not in date_index:
                continue
            start_price = _number(cycle["start_close"])
            current_price = _price(rows[-1])
            if start_price == current_price:
                continue
            direction = "UP" if current_price > start_price else "DOWN"
            candidates.append(
                _candidate(
                    rows,
                    date_index,
                    evidence_refs,
                    basis="MACD_MULTI_REGIME_FORMING",
                    scale="CAMPAIGN",
                    direction=direction,
                    status="FORMING",
                    start_date=start_date,
                    start_price=start_price,
                    confirmed_end_date=None,
                    confirmed_end_price=None,
                    observed_through=as_of,
                    observed_end_price=current_price,
                    component_refs=[
                        *cycle_refs[index:],
                        _price_ref(start_date, evidence_refs),
                        _price_ref(as_of, evidence_refs),
                    ],
                )
            )
    return candidates


def _pivot_ref(packet: dict[str, Any], index: int) -> str:
    path = f"/confirmed_pivots_to_as_of/{index}"
    for item in packet["evidence_catalog"]:
        if str(item.get("path")) == path:
            return str(item["ref"])
    raise ValueError(f"missing pivot evidence ref at index {index}")


def _pivot_candidates(
    packet: dict[str, Any],
    rows: list[dict[str, Any]],
    date_index: dict[str, int],
    evidence_refs: set[str],
) -> list[dict[str, Any]]:
    as_of = str(packet["as_of"])
    as_of_price = _price(rows[-1])
    indexed = [
        (pivot, _pivot_ref(packet, index))
        for index, pivot in enumerate(packet["confirmed_pivots_to_as_of"])
        if str(pivot["confirmation_date"]) <= as_of
        and str(pivot["source_date"]) in date_index
    ]
    candidates: list[dict[str, Any]] = []
    for scale in ("LARGE", "SMALL"):
        scoped = sorted(
            [(pivot, ref) for pivot, ref in indexed if str(pivot["scale"]) == scale],
            key=lambda item: (
                str(item[0]["source_date"]),
                str(item[0]["confirmation_date"]),
                str(item[0]["side"]),
            ),
        )
        # Every start is paired only with a bounded number of subsequent
        # pivots, preventing quadratic catalog growth while preserving nearby
        # and multi-leg campaign alternatives.
        for left_index, (left, left_ref) in enumerate(scoped):
            for right_index in range(left_index + 1, min(len(scoped), left_index + 25)):
                right, right_ref = scoped[right_index]
                start_date = str(left["source_date"])
                end_date = str(right["source_date"])
                if start_date >= end_date:
                    continue
                left_side, right_side = str(left["side"]), str(right["side"])
                if (left_side, right_side) == ("LOW", "HIGH") and float(right["price"]) > float(left["price"]):
                    direction = "UP"
                elif (left_side, right_side) == ("HIGH", "LOW") and float(right["price"]) < float(left["price"]):
                    direction = "DOWN"
                else:
                    continue
                component_refs = [ref for _, ref in scoped[left_index : right_index + 1]]
                candidates.append(
                    _candidate(
                        rows,
                        date_index,
                        evidence_refs,
                        basis="PIVOT_CONFIRMED_CAMPAIGN",
                        scale=scale,
                        direction=direction,
                        status="CONFIRMED",
                        start_date=start_date,
                        start_price=left["price"],
                        confirmed_end_date=end_date,
                        confirmed_end_price=right["price"],
                        observed_through=end_date,
                        observed_end_price=right["price"],
                        component_refs=[
                            *component_refs,
                            _price_ref(start_date, evidence_refs),
                            _price_ref(end_date, evidence_refs),
                        ],
                    )
                )

        # Forming candidates retain all recent objective starting pivots; the
        # AI later decides whether any one has course-level meaning.
        for pivot, pivot_ref in scoped[-24:]:
            start_date = str(pivot["source_date"])
            if start_date >= as_of:
                continue
            start_price = _number(pivot["price"])
            side = str(pivot["side"])
            if side == "LOW" and as_of_price > start_price:
                direction = "UP"
            elif side == "HIGH" and as_of_price < start_price:
                direction = "DOWN"
            else:
                continue
            candidates.append(
                _candidate(
                    rows,
                    date_index,
                    evidence_refs,
                    basis="PIVOT_FORMING_CAMPAIGN",
                    scale=scale,
                    direction=direction,
                    status="FORMING",
                    start_date=start_date,
                    start_price=start_price,
                    confirmed_end_date=None,
                    confirmed_end_price=None,
                    observed_through=as_of,
                    observed_end_price=as_of_price,
                    component_refs=[
                        pivot_ref,
                        _price_ref(start_date, evidence_refs),
                        _price_ref(as_of, evidence_refs),
                    ],
                )
            )
    return candidates


def build_candidates(packet: dict[str, Any]) -> list[dict[str, Any]]:
    rows, date_index = _date_rows(packet)
    evidence_refs = {str(item["ref"]) for item in packet["evidence_catalog"]}
    candidates = _macd_candidates(packet, rows, date_index, evidence_refs)
    candidates.extend(_pivot_candidates(packet, rows, date_index, evidence_refs))
    unique = {item["candidate_id"]: item for item in candidates}
    return sorted(
        unique.values(),
        key=lambda item: (
            item["observed_through"],
            item["start_date"],
            item["basis"],
            item["scale"],
            item["candidate_id"],
        ),
    )


def shortlist(candidates: list[dict[str, Any]]) -> list[str]:
    selected: dict[str, dict[str, Any]] = {}

    def add(rows: list[dict[str, Any]]) -> None:
        for row in rows:
            selected[row["candidate_id"]] = row

    for basis, recent_count, magnitude_count in (
        ("MACD_COMPLETED_REGIME_SPAN", 10, 6),
        ("MACD_FORMING_REGIME_SPAN", 2, 2),
        ("MACD_MULTI_REGIME_SPAN", 12, 8),
        ("MACD_MULTI_REGIME_FORMING", 8, 6),
        ("PIVOT_CONFIRMED_CAMPAIGN", 12, 8),
        ("PIVOT_FORMING_CAMPAIGN", 12, 8),
    ):
        rows = [item for item in candidates if item["basis"] == basis]
        add(
            sorted(
                rows,
                key=lambda item: (
                    item["observed_through"],
                    item["start_date"],
                    item["candidate_id"],
                ),
                reverse=True,
            )[:recent_count]
        )
        add(
            sorted(
                rows,
                key=lambda item: (
                    abs(item["objective_metrics"]["directional_move_atr_at_start"] or 0.0),
                    item["objective_metrics"]["trading_bars"],
                    item["observed_through"],
                    item["candidate_id"],
                ),
                reverse=True,
            )[:magnitude_count]
        )
    ordered = sorted(
        selected.values(),
        key=lambda item: (
            item["observed_through"],
            item["start_date"],
            item["candidate_id"],
        ),
        reverse=True,
    )[:80]
    return [item["candidate_id"] for item in ordered]


def validate_catalog(catalog: dict[str, Any], schema: dict[str, Any]) -> None:
    errors = sorted(
        Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER).iter_errors(catalog),
        key=lambda error: list(error.path),
    )
    if errors:
        raise ValueError("campaign candidate schema invalid: " + "; ".join(error.message for error in errors[:5]))
    as_of = str(catalog["as_of"])
    ids = [item["candidate_id"] for item in catalog["campaign_candidates"]]
    if len(ids) != len(set(ids)):
        raise ValueError("campaign candidate IDs are not unique")
    if not set(catalog["prompt_shortlist_campaign_ids"]).issubset(ids):
        raise ValueError("campaign shortlist references missing candidate")
    for item in catalog["campaign_candidates"]:
        if item["start_date"] > item["observed_through"] or item["observed_through"] > as_of:
            raise ValueError("future or reversed campaign candidate")
        if item["confirmed_end_date"] is not None and item["confirmed_end_date"] != item["observed_through"]:
            raise ValueError("confirmed candidate endpoint mismatch")


def build_catalog(packet: dict[str, Any], packet_sha256: str, schema: dict[str, Any]) -> dict[str, Any]:
    candidates = build_candidates(packet)
    catalog = {
        "catalog_version": CATALOG_VERSION,
        "status": "CANDIDATE_CAMPAIGN_OBJECTS_ONLY（僅大段落候選、非課程判讀）",
        "review_id": packet["review_id"],
        "anonymous_stock_id": packet["anonymous_stock_id"],
        "as_of": packet["as_of"],
        "source_packet_sha256": packet_sha256,
        "generator_contract": {
            "candidate_only": True,
            "ai_course_judgement_included": False,
            "future_performance_used": False,
            "sealed_labels_used": False,
            "legacy_ai_answers_used": False,
            "identity_used": False,
            "source_packet_unchanged": True,
            "shortlist_uses_outcomes": False,
        },
        "campaign_candidates": candidates,
        "prompt_shortlist_campaign_ids": shortlist(candidates),
    }
    validate_catalog(catalog, schema)
    return catalog


def write_new_or_identical(path: Path, payload: bytes) -> None:
    if path.exists() and path.read_bytes() != payload:
        raise FileExistsError(f"refusing to overwrite non-identical artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def build_all(artifact_dir: Path) -> dict[str, Any]:
    manifest_path = artifact_dir / SOURCE_MANIFEST_FILE
    schema_path = artifact_dir / SCHEMA_FILE
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    schema = json.loads(schema_path.read_text(encoding="utf-8-sig"))
    output_dir = artifact_dir / OUTPUT_DIRECTORY
    rows: list[dict[str, Any]] = []
    for source in manifest["rows"]:
        packet_path = artifact_dir / SOURCE_PACKET_DIRECTORY / str(source["packet_file"])
        packet_sha = sha256_path(packet_path)
        if packet_sha != str(source["packet_sha256"]):
            raise ValueError(f"source packet hash mismatch: {source['review_id']}")
        packet = json.loads(packet_path.read_text(encoding="utf-8-sig"))
        catalog = build_catalog(packet, packet_sha, schema)
        output_path = output_dir / f"{catalog['review_id']}.json"
        write_new_or_identical(output_path, canonical_bytes(catalog))
        basis_counts: dict[str, int] = {}
        for item in catalog["campaign_candidates"]:
            basis_counts[item["basis"]] = basis_counts.get(item["basis"], 0) + 1
        rows.append(
            {
                "review_id": catalog["review_id"],
                "as_of": catalog["as_of"],
                "source_packet_file": packet_path.name,
                "source_packet_sha256": packet_sha,
                "catalog_file": output_path.name,
                "catalog_sha256": sha256_path(output_path),
                "campaign_candidate_count": len(catalog["campaign_candidates"]),
                "shortlist_count": len(catalog["prompt_shortlist_campaign_ids"]),
                "basis_counts": dict(sorted(basis_counts.items())),
            }
        )
    result = {
        "manifest_version": MANIFEST_VERSION,
        "status": "CANDIDATE_CAMPAIGN_CATALOG_COMPLETE（大段落候選目錄完成）",
        "case_count": len(rows),
        "source_manifest_file": SOURCE_MANIFEST_FILE,
        "source_manifest_sha256": sha256_path(manifest_path),
        "source_packet_directory": SOURCE_PACKET_DIRECTORY,
        "catalog_directory": OUTPUT_DIRECTORY,
        "catalog_schema_file": SCHEMA_FILE,
        "catalog_schema_sha256": sha256_path(schema_path),
        "formal_ai_calls": 0,
        "future_performance_used": False,
        "sealed_labels_used": False,
        "legacy_ai_answers_used": False,
        "identity_used": False,
        "ready_for_formal_ai": False,
        "rows": sorted(rows, key=lambda item: item["review_id"]),
    }
    write_new_or_identical(
        artifact_dir / "campaign_candidate_catalog_manifest_candidate_r1.json",
        canonical_bytes(result),
    )
    return result


def markdown(manifest: dict[str, Any]) -> str:
    basis_totals: dict[str, int] = {}
    for row in manifest["rows"]:
        for key, value in row["basis_counts"].items():
            basis_totals[key] = basis_totals.get(key, 0) + int(value)
    lines = [
        "# M2A 大段落候選目錄 Candidate R1",
        "",
        f"- 狀態：`{manifest['status']}`",
        f"- 案例：{manifest['case_count']}",
        f"- 正式 AI 呼叫：{manifest['formal_ai_calls']}",
        "- 未使用未來績效、股票身分、密封標籤或舊 AI 答案。",
        "- 本目錄只擴充可供 AI 看見的客觀結構物件，不代表課程定錨或交易許可。",
        "",
        "| 候選基礎 | 數量 |",
        "|---|---:|",
    ]
    for key, value in sorted(basis_totals.items()):
        lines.append(f"| `{key}` | {value} |")
    lines.extend(
        [
            "",
            "## 邊界",
            "",
            "程式只建立有限、可追溯、AS-OF 合法的完整 MACD 週期、跨週期段落與多樞紐段落候選。",
            "候選是否為大定錨、修正段、複製段、控制結構或可交易情境，仍必須由正式 AI 原子判讀與程式真值表共同決定。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, default=ARTIFACT_DIR)
    args = parser.parse_args()
    manifest = build_all(args.artifact_dir)
    md_path = args.artifact_dir / "campaign_candidate_catalog_manifest_candidate_r1.md"
    write_new_or_identical(md_path, markdown(manifest).encode("utf-8"))
    print(json.dumps({"status": manifest["status"], "case_count": manifest["case_count"], "manifest": str((args.artifact_dir / 'campaign_candidate_catalog_manifest_candidate_r1.json').resolve())}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
