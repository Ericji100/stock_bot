"""Outcome-blind research calibration snapshots for Hybrid Monitoring V2.

This module deliberately does not declare the existing eight cases to be gold.
It rebuilds production-equivalent atomic packets at each historical ``as_of``
and keeps identities and evaluator notes outside the AI-visible directory.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd

try:
    from .hybrid_v3_atomic_packets_v2 import (
        assert_anonymous_and_causal,
        build_atomic_packet,
        build_daily_objective_states,
    )
    from .hybrid_v3_atomic_policy_v2 import expected_question_manifest
    from .hybrid_v3_sharding_v2 import canonical_sha256
except ImportError:
    from hybrid_v3_atomic_packets_v2 import (
        assert_anonymous_and_causal,
        build_atomic_packet,
        build_daily_objective_states,
    )
    from hybrid_v3_atomic_policy_v2 import expected_question_manifest
    from hybrid_v3_sharding_v2 import canonical_sha256


ROOT = Path(__file__).resolve().parents[1]
SOURCE_CASES = ROOT / "config/enlightenment_ai_calibration_cases_v1.json"
CANDIDATE_CASES = ROOT / "config/hybrid_v2_research_calibration_candidates_v1.json"
SOURCE_MANIFEST = ROOT / "reports/course_backtest/2026-09-06/tg_enlightenment_ai_v2/input_manifest.json"
REVIEW_PACKET_DIR = ROOT / "reports/course_backtest/2026-09-06/tg_formal_ai_v1_v2/review_packets"
DEFAULT_RUN = ROOT / "reports/course_backtest/2026-09-08/hybrid_v2_research_calibration_v1"

CALIBRATION_VERSION = "hybrid-v2-research-calibration-v1"
# The tool is final, but the currently materialized calibration data is not.
# Never use TOOL_STATUS as evidence that the 20-case human rubric gate passed.
TOOL_STATUS = "FINAL"
STATUS = "DRAFT/INCOMPLETE_RESEARCH_CALIBRATION"
SNAPSHOT_CLASSIFICATION = "CALIBRATION_ONLY_NOT_PRODUCTION_REVIEW_POINT"
AI_INPUT_CLASSIFICATION = "ANONYMOUS_OUTCOME_BLIND_ATOMIC_PACKET"
EVALUATOR_CLASSIFICATION = "EVALUATOR_ONLY_NEVER_AI_INPUT"
IDENTITY_SEED = b"hybrid-v2-research-calibration-v1-anonymous-id"
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

FORBIDDEN_AI_EXACT_KEYS = {
    "stock", "code", "name", "symbol", "identity", "ticker",
    "old_ai", "prior_ai", "previous_ai", "mfe", "mae", "pnl", "profit", "exit",
}
FORBIDDEN_RUBRIC_FIELDS = {
    "gold", "gold_label", "gold_answer", "expected_primary_lens", "known_design_contamination",
}
REQUIRED_FROZEN_RUBRIC_FIELDS = {
    "rubric_version", "rubric_status", "snapshot_sha256", "frozen_by",
    "frozen_on", "expected_atomic_answers", "allowed_decisions", "rubric_sha256",
}


class CalibrationError(ValueError):
    pass


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise CalibrationError(f"expected JSON object: {path}")
    return value


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_immutable(path: Path, value: Any) -> None:
    payload = _json_bytes(value)
    if path.exists():
        if path.read_bytes() != payload:
            raise CalibrationError(f"immutable artifact differs: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _anon_ids(source_case_id: str, as_of: str) -> tuple[str, str]:
    stock_digest = hashlib.sha256(IDENTITY_SEED + source_case_id.encode("utf-8")).hexdigest()
    review_digest = hashlib.sha256(
        IDENTITY_SEED + b"|review|" + source_case_id.encode("utf-8") + b"|" + as_of.encode("ascii")
    ).hexdigest()
    return f"S-{stock_digest[:16]}", f"D-{review_digest[:24]}"


def causal_adjusted_price_frame(source: pd.DataFrame, as_of: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Rebase full-history adjusted OHLC to the adjustment factor visible at as_of.

    Dividing every historical full-history factor by the as-of factor cancels
    distributions and splits that occur after ``as_of`` while retaining actions
    between each historical bar and ``as_of``.
    """

    required = {"date", "raw_open", "raw_high", "raw_low", "raw_close", "adj_close", "volume"}
    missing = sorted(required - set(source.columns))
    if missing:
        raise CalibrationError(f"price source lacks causal rebase fields: {missing}")
    frame = source.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="raise")
    cutoff = pd.Timestamp(as_of)
    future_count = int((frame["date"] > cutoff).sum())
    frame = frame[frame["date"] <= cutoff].sort_values("date").drop_duplicates("date", keep="last")
    if frame.empty or frame.iloc[-1]["date"].date().isoformat() != as_of:
        raise CalibrationError(f"as_of is not the final visible trading bar: {as_of}")
    for field in ("raw_open", "raw_high", "raw_low", "raw_close", "adj_close", "volume"):
        frame[field] = pd.to_numeric(frame[field], errors="raise")
    factors = frame["adj_close"] / frame["raw_close"]
    as_of_factor = float(factors.iloc[-1])
    if not pd.notna(as_of_factor) or as_of_factor <= 0:
        raise CalibrationError("as_of adjustment factor is invalid")
    causal_factor = factors / as_of_factor
    output = pd.DataFrame({"date": frame["date"], "volume": frame["volume"]})
    for target, raw in (("open", "raw_open"), ("high", "raw_high"), ("low", "raw_low"), ("close", "raw_close")):
        output[target] = frame[raw] * causal_factor
    if output[["open", "high", "low", "close", "volume"]].isna().any().any():
        raise CalibrationError("causal adjusted price frame contains null numeric values")
    return output.reset_index(drop=True), {
        "technical_coordinate": "AS_OF_REBASED_ADJUSTED_OHLC",
        "as_of_adjustment_factor": round(as_of_factor, 12),
        "visible_bar_count": int(len(output)),
        "first_visible_bar": output.iloc[0]["date"].date().isoformat(),
        "last_visible_bar": as_of,
        "future_price_rows_removed": future_count,
    }


def _scale_prices(value: Any, divisor: float) -> Any:
    if value is None:
        return None
    return float(value) / divisor


def causal_review_packet(source: Mapping[str, Any], as_of: str, as_of_factor: float) -> dict[str, Any]:
    selection_timeline = {
        str(day): list(sources)
        for day, sources in (source.get("selection_timeline") or {}).items()
        if str(day) <= as_of
    }
    pivots = []
    for original in source.get("confirmed_pivots") or []:
        if str(original.get("source_date") or "") > as_of or str(original.get("confirmation_date") or "") > as_of:
            continue
        row = copy.deepcopy(original)
        row["price"] = _scale_prices(row.get("price"), as_of_factor)
        pivots.append(row)
    cycles = []
    for original in source.get("macd_21_55_55_cycles") or []:
        start = str(original.get("start") or "")
        if not start or start > as_of:
            continue
        if original.get("end") is None or str(original.get("end")) > as_of:
            cycles.append({"sign": original["sign"], "start": start, "end": None, "status": "FORMING"})
            continue
        row = copy.deepcopy(original)
        for field in ("low", "high", "start_close", "last_close"):
            if field in row:
                row[field] = _scale_prices(row[field], as_of_factor)
        cycles.append(row)
    return {
        "selection_timeline": selection_timeline,
        "confirmed_pivots": pivots,
        "macd_21_55_55_cycles": cycles,
    }


def cutoff_corporate_actions(source: Mapping[str, Any], as_of: str) -> tuple[dict[str, list[dict[str, Any]]], int]:
    groups: dict[str, list[dict[str, Any]]] = {}
    future_removed = 0
    raw_groups = source.get("all_events") if isinstance(source.get("all_events"), dict) else None
    if raw_groups is None:
        raw_groups = {key: value for key, value in source.items() if isinstance(value, list)}
    for event_type, rows in raw_groups.items():
        visible = []
        for original in rows:
            if not isinstance(original, dict) or not original.get("date"):
                raise CalibrationError(f"corporate action lacks date: {event_type}")
            if str(original["date"]) > as_of:
                future_removed += 1
                continue
            visible.append(copy.deepcopy(original))
        groups[str(event_type)] = sorted(visible, key=lambda row: str(row["date"]))
    return groups, future_removed


def _strip_calibration_forbidden_fields(packet: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(packet)
    for evidence in result.get("evidence") or []:
        values = evidence.get("values") or {}
        if isinstance(values, dict):
            values.pop("return_1d_pct", None)
    result["question_manifest_sha256"] = canonical_sha256(result["question_manifest"])
    result["evidence_catalog_sha256"] = canonical_sha256(result["evidence"])
    objective = result.get("objective_facts") or {}
    if "ai_visible_evidence_sha256" in objective:
        objective["ai_visible_evidence_sha256"] = canonical_sha256(result["evidence"])
    core = dict(result)
    core.pop("input_packet_sha256", None)
    result["input_packet_sha256"] = canonical_sha256(core)
    return result


def _walk(value: Any) -> Iterable[tuple[str | None, Any]]:
    if isinstance(value, dict):
        for key, nested in value.items():
            yield str(key), nested
            yield from _walk(nested)
    elif isinstance(value, list):
        for nested in value:
            yield None, nested
            yield from _walk(nested)


def assert_outcome_blind_packet(packet: dict[str, Any], *, as_of: str) -> None:
    if packet.get("as_of") != as_of:
        raise CalibrationError("packet as_of mismatch")
    for key, value in _walk(packet):
        if key is not None:
            lowered = key.lower()
            if (
                lowered in FORBIDDEN_AI_EXACT_KEYS
                or lowered.startswith("return")
                or "_return" in lowered
                or any(token in lowered for token in ("old_ai", "prior_ai", "previous_ai"))
                or lowered in {"stock_identity", "source_identity", "real_identity"}
            ):
                raise CalibrationError(f"forbidden AI-visible field: {key}")
        if isinstance(value, str) and ISO_DATE.fullmatch(value) and value > as_of:
            raise CalibrationError(f"future date leaked into AI packet: {value} > {as_of}")
    if packet.get("question_manifest") != expected_question_manifest(packet):
        raise CalibrationError("packet question manifest is not production-equivalent")
    assert_anonymous_and_causal(packet, as_of=as_of)


def _source_items() -> dict[str, dict[str, Any]]:
    source = read_json(SOURCE_MANIFEST)
    return {str(row["code"]): row for row in source.get("items") or []}


def _pending_rubric(
    anonymous_stock_id: str, snapshot_sha256: str, source_case: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "anonymous_stock_id": anonymous_stock_id,
        "classification": EVALUATOR_CLASSIFICATION,
        "rubric_version": None,
        "rubric_status": "HUMAN_RUBRIC_NOT_FROZEN",
        "snapshot_sha256": snapshot_sha256,
        "frozen_by": [],
        "frozen_on": None,
        "expected_atomic_answers": [],
        "allowed_decisions": [],
        "rubric_sha256": None,
        "source_evaluator_notes": copy.deepcopy(source_case.get("evaluator_only") or {}),
        "warning": "Source notes are contaminated calibration context, not a complete gold rubric and never AI input.",
    }


def rubric_completeness_errors(rubric: Mapping[str, Any], snapshot_sha256: str) -> list[str]:
    errors = []
    missing = REQUIRED_FROZEN_RUBRIC_FIELDS - set(rubric)
    if missing:
        errors.append("missing rubric fields: " + ",".join(sorted(missing)))
    if rubric.get("rubric_status") != "HUMAN_FROZEN":
        errors.append("rubric is not HUMAN_FROZEN")
    if rubric.get("snapshot_sha256") != snapshot_sha256:
        errors.append("rubric snapshot hash mismatch")
    if not rubric.get("frozen_by") or not rubric.get("frozen_on"):
        errors.append("rubric human freeze attestation is incomplete")
    if not rubric.get("expected_atomic_answers") or not rubric.get("allowed_decisions"):
        errors.append("rubric expectations are incomplete")
    supplied = rubric.get("rubric_sha256")
    core = dict(rubric)
    core.pop("rubric_sha256", None)
    if supplied != canonical_sha256(core):
        errors.append("rubric hash is absent or invalid")
    return sorted(set(errors))


def evaluate_incomplete_calibration(
    manifest: Mapping[str, Any], rubrics: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    rubric_by_stock = {str(row.get("anonymous_stock_id")): row for row in rubrics}
    rows = []
    for case in manifest.get("cases") or []:
        rubric = rubric_by_stock.get(str(case["anonymous_stock_id"]), {})
        errors = rubric_completeness_errors(rubric, str(case["snapshot_sha256"]))
        rows.append({
            "ordinal": case["ordinal"],
            "anonymous_stock_id": case["anonymous_stock_id"],
            "evaluation": "N/A" if errors else "NOT_RUN",
            "gate_result": "FAIL_CLOSED",
            "rubric_errors": errors,
        })
    return {
        "version": CALIBRATION_VERSION,
        "status": STATUS,
        "classification": "RESEARCH_CALIBRATION_NOT_PERFORMANCE_HOLDOUT",
        "overall_evaluation": "N/A",
        "overall_gate_result": "FAIL_CLOSED",
        "claim_gold_pass": False,
        "case_count": len(rows),
        "human_frozen_rubric_count": sum(not row["rubric_errors"] for row in rows),
        "cases": rows,
    }


def _manifest_with_hash(core: dict[str, Any]) -> dict[str, Any]:
    return {**core, "manifest_sha256": canonical_sha256(core)}


def build_calibration(run_dir: Path = DEFAULT_RUN) -> dict[str, Any]:
    source_cases = read_json(SOURCE_CASES)
    cases = source_cases.get("cases") or []
    if len(cases) != 8:
        raise CalibrationError("research calibration skeleton requires the existing eight cases exactly")
    source_items = _source_items()
    manifest_rows = []
    identities = []
    rubrics = []
    for ordinal, source_case in enumerate(cases):
        source_case_id = str(source_case["id"])
        code = str(source_case["stock"]["code"])
        as_of = str(source_case["analysis_as_of"])
        item = source_items.get(code)
        review_path = REVIEW_PACKET_DIR / f"{code}.json"
        if item is None or not review_path.exists():
            raise CalibrationError(f"missing production source for calibration case: {source_case_id}")
        price_path = Path(item["price_path"])
        event_path = Path(item["event_path"])
        price_frame, price_audit = causal_adjusted_price_frame(pd.read_csv(price_path), as_of)
        review = causal_review_packet(read_json(review_path), as_of, float(price_audit["as_of_adjustment_factor"]))
        visible, states = build_daily_objective_states(
            price_frame, review, monitor_on=as_of, as_of=as_of,
        )
        if not states:
            raise CalibrationError(f"no forced objective state at {source_case_id}")
        anonymous_stock_id, review_id = _anon_ids(source_case_id, as_of)
        packet = build_atomic_packet(
            anonymous_stock_id=anonymous_stock_id,
            review_id=review_id,
            as_of=as_of,
            visible_frame=visible,
            daily_state=states[-1],
            review_packet=review,
        )
        packet = _strip_calibration_forbidden_fields(packet)
        assert_outcome_blind_packet(packet, as_of=as_of)
        packet_relpath = f"ai_packets/{anonymous_stock_id}.json"
        packet_path = run_dir / packet_relpath
        write_immutable(packet_path, packet)
        snapshot_sha256 = file_sha256(packet_path)

        actions, future_actions_removed = cutoff_corporate_actions(read_json(event_path), as_of)
        action_audit = {
            "version": CALIBRATION_VERSION,
            "status": STATUS,
            "classification": EVALUATOR_CLASSIFICATION,
            "anonymous_stock_id": anonymous_stock_id,
            "as_of": as_of,
            **price_audit,
            "corporate_actions_to_as_of": actions,
            "future_corporate_actions_removed": future_actions_removed,
            "source_price_sha256": file_sha256(price_path),
            "source_event_sha256": file_sha256(event_path),
            "source_review_packet_sha256": file_sha256(review_path),
        }
        action_relpath = f"evaluator_only/corporate_action_audits/{anonymous_stock_id}.json"
        action_path = run_dir / action_relpath
        write_immutable(action_path, action_audit)
        manifest_rows.append({
            "ordinal": ordinal,
            "anonymous_stock_id": anonymous_stock_id,
            "review_id": review_id,
            "as_of": as_of,
            "review_point_classification": SNAPSHOT_CLASSIFICATION,
            "ai_input_classification": AI_INPUT_CLASSIFICATION,
            "packet": packet_relpath,
            "snapshot_sha256": snapshot_sha256,
            "corporate_action_audit": action_relpath,
            "corporate_action_audit_sha256": file_sha256(action_path),
        })
        identities.append({
            "ordinal": ordinal,
            "anonymous_stock_id": anonymous_stock_id,
            "source_case_id": source_case_id,
            "stock": copy.deepcopy(source_case["stock"]),
            "analysis_as_of": as_of,
            "category": source_case.get("category"),
            "is_performance_holdout": False,
        })
        rubrics.append(_pending_rubric(anonymous_stock_id, snapshot_sha256, source_case))

    identity_payload = {
        "version": CALIBRATION_VERSION,
        "status": STATUS,
        "classification": EVALUATOR_CLASSIFICATION,
        "must_never_be_passed_to_ai": True,
        "cases": identities,
    }
    identity_path = run_dir / "evaluator_only/sealed_identity_map.json"
    write_immutable(identity_path, identity_payload)
    rubric_payload = {
        "version": CALIBRATION_VERSION,
        "status": STATUS,
        "classification": EVALUATOR_CLASSIFICATION,
        "must_never_be_passed_to_ai": True,
        "rubrics": rubrics,
    }
    rubric_path = run_dir / "evaluator_only/pending_human_rubrics.json"
    write_immutable(rubric_path, rubric_payload)

    core = {
        "version": CALIBRATION_VERSION,
        "status": STATUS,
        "classification": "RESEARCH_CALIBRATION_NOT_PERFORMANCE_HOLDOUT",
        "forced_snapshot_policy": SNAPSHOT_CLASSIFICATION,
        "production_review_points_modified": False,
        "outcome_blind": True,
        "source_cases_sha256": file_sha256(SOURCE_CASES),
        "candidate_only_cases_sha256": file_sha256(CANDIDATE_CASES),
        "source_manifest_sha256": file_sha256(SOURCE_MANIFEST),
        "sealed_identity_map_sha256": file_sha256(identity_path),
        "pending_human_rubrics_sha256": file_sha256(rubric_path),
        "case_count": len(manifest_rows),
        "human_frozen_rubric_count": 0,
        "cases": manifest_rows,
    }
    evaluation = evaluate_incomplete_calibration(core, rubrics)
    evaluation_path = run_dir / "evaluation.json"
    write_immutable(evaluation_path, evaluation)
    core["evaluation_sha256"] = file_sha256(evaluation_path)
    manifest = _manifest_with_hash(core)
    manifest_path = run_dir / "immutable_manifest.json"
    write_immutable(manifest_path, manifest)
    return manifest


def verify_calibration(run_dir: Path = DEFAULT_RUN) -> list[str]:
    errors = []
    manifest_path = run_dir / "immutable_manifest.json"
    if not manifest_path.exists():
        return ["immutable manifest is missing"]
    manifest = read_json(manifest_path)
    supplied = manifest.pop("manifest_sha256", None)
    if supplied != canonical_sha256(manifest):
        errors.append("manifest hash mismatch")
    if manifest.get("status") != STATUS:
        errors.append("calibration status is not draft/incomplete")
    if manifest.get("production_review_points_modified") is not False:
        errors.append("forced calibration snapshots changed production review points")
    source_hashes = (
        (SOURCE_CASES, "source_cases_sha256"),
        (CANDIDATE_CASES, "candidate_only_cases_sha256"),
        (SOURCE_MANIFEST, "source_manifest_sha256"),
    )
    for path, field in source_hashes:
        if not path.exists() or manifest.get(field) != file_sha256(path):
            errors.append(f"source hash mismatch: {field}")
    artifact_hashes = (
        (run_dir / "evaluator_only/sealed_identity_map.json", "sealed_identity_map_sha256"),
        (run_dir / "evaluator_only/pending_human_rubrics.json", "pending_human_rubrics_sha256"),
        (run_dir / "evaluation.json", "evaluation_sha256"),
    )
    for path, field in artifact_hashes:
        if not path.exists() or manifest.get(field) != file_sha256(path):
            errors.append(f"artifact hash mismatch: {field}")
    evaluation_path = run_dir / "evaluation.json"
    if evaluation_path.exists():
        evaluation = read_json(evaluation_path)
        if (
            evaluation.get("overall_evaluation") != "N/A"
            or evaluation.get("overall_gate_result") != "FAIL_CLOSED"
            or evaluation.get("claim_gold_pass") is not False
        ):
            errors.append("incomplete research calibration evaluation is not fail-closed")
    for row in manifest.get("cases") or []:
        packet_path = run_dir / row["packet"]
        action_path = run_dir / row["corporate_action_audit"]
        if not packet_path.exists() or file_sha256(packet_path) != row["snapshot_sha256"]:
            errors.append(f"snapshot hash mismatch: {row.get('ordinal')}")
        if not action_path.exists() or file_sha256(action_path) != row["corporate_action_audit_sha256"]:
            errors.append(f"corporate action audit hash mismatch: {row.get('ordinal')}")
        if packet_path.exists():
            try:
                assert_outcome_blind_packet(read_json(packet_path), as_of=str(row["as_of"]))
            except Exception as exc:  # verification must report every artifact error
                errors.append(f"snapshot invalid {row.get('ordinal')}: {exc}")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("build", "verify"), nargs="?", default="build")
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN)
    args = parser.parse_args()
    if args.command == "build":
        manifest = build_calibration(args.run_dir)
        print(f"status={manifest['status']} cases={manifest['case_count']} manifest={args.run_dir / 'immutable_manifest.json'}")
    else:
        errors = verify_calibration(args.run_dir)
        if errors:
            raise SystemExit("\n".join(errors))
        print(f"verified={args.run_dir / 'immutable_manifest.json'}")


if __name__ == "__main__":
    main()
