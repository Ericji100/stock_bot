"""Outcome-blind symbolic reachability preflight for V4-S1.

This proves that the frozen packet/reducer composition has a legal target-route
path before spending model calls.  A symbolic witness is not a semantic answer,
course label, or expected trade; it is never included in an AI-visible packet.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PREFLIGHT_BUILDER_PATH = Path(__file__).resolve()
SOURCE_VALIDATOR_PATH = ROOT / "scripts/hybrid_v3_consistency_v3.py"

try:
    from .hybrid_v4_s1_atomic_policy_v1 import (
        POLICY_VERSION,
        reduce_atomic_v4_s1,
        stage_permission_v4_s1,
        validate_atomic,
    )
    from .hybrid_v3_consistency_v3 import (
        load_sampling_contract,
        validate_source_record,
    )
    from .hybrid_v3_sharding_v2 import _publish_immutable
except ImportError:  # pragma: no cover
    from scripts.hybrid_v4_s1_atomic_policy_v1 import (
        POLICY_VERSION,
        reduce_atomic_v4_s1,
        stage_permission_v4_s1,
        validate_atomic,
    )
    from scripts.hybrid_v3_consistency_v3 import (
        load_sampling_contract,
        validate_source_record,
    )
    from scripts.hybrid_v3_sharding_v2 import _publish_immutable


PREFLIGHT_VERSION = "hybrid-v4-s1-reachability-preflight-v1"
EXPECTED_POLICY_VERSION = "hybrid-v4-s1-atomic-policy-v1"
EXPECTED_POLICY_SHA256 = "20900a32d5a752497493b7005da1f10074ef9e6f0a653670aee9e9ecea08ba8d"
EXPECTED_SOURCE_SHA256 = "49e5125ad57c55b1536b53746705a60a7c505e49ed1b85c2a72764a61df104bb"
EXPECTED_SOURCE_MANIFEST_SHA256 = "a2c63ec49e236049313354795b1c164a1dfe28be473a1808c024168681ebc7eb"
EXPECTED_STAGE_SHA256 = "0372803b966a7dd47d44350838a1ed79c3be40a5e331f27a69db38f99bc0745e"
EXPECTED_SAMPLING_CONTRACT_SHA256 = "b22c9212a880f45896d60d80e6aaee77e5cb881e19cf9139a8075f462772bff1"
EXPECTED_STAGE_VERSION = "hybrid-v4-s1-mature-stage-v1"
EXPECTED_STOCKS = 1029
EXPECTED_REVIEW_POINTS = 7180
EXPECTED_MONITORING_START = "2023-06-01"
EXPECTED_MONITORING_END = "2024-02-02"
SOURCE_SAMPLING_CONTRACT = ROOT / "config/hybrid_multilabel_sampling_v3.json"
TARGET_SCENARIO = "MATURE_TREND_PULLBACK"
TARGET_ROUTE = "V2_CORE"
LOCAL_NEGATIVE_ATOMS = {
    "EXH_ATTACK_SHORTENING",
    "EXH_SLOPE_DECAY",
    "EXH_PRICE_VOLUME_DIVERGENCE",
    "EXH_FAILED_CONTINUATION",
    "EXH_TIME_SPACE_EXHAUSTION",
}
LOCAL_POSITIVE_ATOMS = {
    "LOCATION_REMAINING_SPACE_ADEQUATE",
    "LOCATION_NOT_EXTENDED_FROM_ORIGIN",
}
FORBIDDEN_SOURCE_KEYS = {
    "mfe",
    "mae",
    "pnl",
    "profit",
    "return_after",
    "forward_return",
    "future_return",
    "future_open",
    "future_high",
    "future_low",
    "future_close",
    "exit_date",
    "exit_price",
    "realized_return",
    "unrealized_return",
    "stock_name",
    "stock_code",
    "ticker",
    "company_name",
    "security_name",
    "isin",
    "winner",
    "outcome",
    "next_20d_return",
    "profit_factor",
}


class ReachabilityPreflightError(ValueError):
    """The frozen source or V4-S1 reachability contract is invalid."""


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReachabilityPreflightError(f"cannot read JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ReachabilityPreflightError(f"expected JSON object: {path}")
    return value


def _jsonl(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with Path(path).open(encoding="utf-8-sig") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ReachabilityPreflightError(
                        f"expected object at {path}:{line_number}"
                    )
                yield value
    except (OSError, json.JSONDecodeError) as exc:
        raise ReachabilityPreflightError(f"cannot read JSONL: {path}") from exc


def _contains_forbidden_key(value: Any) -> str | None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).lower()
            if (
                normalized in FORBIDDEN_SOURCE_KEYS
                or normalized.startswith(("future_", "forward_"))
                or "winner" in normalized
                or normalized.endswith(("_mfe", "_mae", "_pnl"))
            ):
                return str(key)
            found = _contains_forbidden_key(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _contains_forbidden_key(child)
            if found:
                return found
    return None


def _verdict(result: str, evidence_ref: str) -> dict[str, Any]:
    if result == "PASS":
        return {
            "result": "PASS",
            "supporting_evidence_refs": [evidence_ref],
            "contradicting_evidence_refs": [],
            "missing_evidence_codes": [],
            "reason_code": "VISIBLE_EVIDENCE_SATISFIES_DEFINITION",
        }
    if result == "FAIL":
        return {
            "result": "FAIL",
            "supporting_evidence_refs": [],
            "contradicting_evidence_refs": [evidence_ref],
            "missing_evidence_codes": [],
            "reason_code": "VISIBLE_EVIDENCE_CONTRADICTS_DEFINITION",
        }
    raise ReachabilityPreflightError(f"unsupported symbolic verdict: {result}")


def _answer_rows(
    manifest_rows: Iterable[Mapping[str, Any]], evidence_ref: str
) -> list[dict[str, Any]]:
    return [
        {
            "subject_ref": row["subject_ref"],
            "answers": {
                question_id: _verdict("FAIL", evidence_ref)
                for question_id in row["required_question_ids"]
            },
        }
        for row in manifest_rows
    ]


def symbolic_witness(
    packet: dict[str, Any], hypothesis: Mapping[str, Any]
) -> dict[str, Any]:
    """Construct one satisfiability witness, not a chart-truth assertion."""

    evidence = packet.get("evidence") or []
    if not evidence or not evidence[0].get("ref"):
        raise ReachabilityPreflightError("packet has no evidence ref for witness")
    evidence_ref = str(evidence[0]["ref"])
    manifest = packet.get("question_manifest") or {}
    semantic = {
        "_preflight_symbolic_only": {
            "preflight_version": PREFLIGHT_VERSION,
            "classification": "NOT_AI_OUTPUT_NOT_COURSE_GOLD",
        },
        "schema_version": "hybrid-atomic-semantics-v2",
        "protocol_version": "hybrid-monitoring-protocol-v2",
        "contract_status": "FINAL",
        "review_id": packet["review_id"],
        "anonymous_stock_id": packet["anonymous_stock_id"],
        "as_of": packet["as_of"],
        "input_packet_sha256": packet["input_packet_sha256"],
        "question_manifest_sha256": packet["question_manifest_sha256"],
        "evidence_catalog_sha256": packet["evidence_catalog_sha256"],
        "candidate_answers": {
            "anchor_candidates": _answer_rows(
                manifest.get("anchor_candidates") or [], evidence_ref
            ),
            "relation_candidates": _answer_rows(
                manifest.get("relation_candidates") or [], evidence_ref
            ),
            "stop_candidates": _answer_rows(
                manifest.get("stop_candidates") or [], evidence_ref
            ),
        },
        "global_answers": {
            question_id: _verdict("PASS", evidence_ref)
            for question_id in (manifest.get("global_question_ids") or [])
        },
        "causal_attestation": {
            "latest_visible_bar": packet["as_of"],
            "used_future_data": False,
            "identity_visible": False,
            "performance_visible": False,
            "invented_evidence_ref": False,
            "invented_candidate_ref": False,
            "answered_complete_manifest": True,
            "selected_scenario": False,
            "selected_phase": False,
            "selected_route": False,
            "decided_permission": False,
            "issued_trade_instruction": False,
        },
    }
    groups = {
        name: {
            row["subject_ref"]: row["answers"]
            for row in semantic["candidate_answers"][name]
        }
        for name in (
            "anchor_candidates",
            "relation_candidates",
            "stop_candidates",
        )
    }

    def pass_subject(group: str, subject_ref: Any) -> None:
        answers = groups[group].get(subject_ref) or {}
        for question_id in list(answers):
            answers[question_id] = _verdict("PASS", evidence_ref)

    pass_subject("anchor_candidates", hypothesis.get("anchor_ref"))
    pass_subject("relation_candidates", hypothesis.get("relation_ref"))
    pass_subject("stop_candidates", hypothesis.get("episode_stop_ref"))
    pass_subject("stop_candidates", hypothesis.get("campaign_stop_ref"))

    target_relation = groups["relation_candidates"].get(
        hypothesis.get("relation_ref")
    ) or {}
    for question_id in LOCAL_NEGATIVE_ATOMS:
        if question_id in target_relation:
            target_relation[question_id] = _verdict("FAIL", evidence_ref)
    for question_id in LOCAL_POSITIVE_ATOMS:
        if question_id in target_relation:
            target_relation[question_id] = _verdict("PASS", evidence_ref)
    return semantic


def _symbolic_validation_projection(
    symbolic: Mapping[str, Any],
) -> dict[str, Any]:
    """Remove a mandatory schema poison-pill only inside this preflight."""

    marker = symbolic.get("_preflight_symbolic_only")
    if marker != {
        "preflight_version": PREFLIGHT_VERSION,
        "classification": "NOT_AI_OUTPUT_NOT_COURSE_GOLD",
    }:
        raise ReachabilityPreflightError("symbolic witness watermark is missing")
    return {
        key: child
        for key, child in symbolic.items()
        if key != "_preflight_symbolic_only"
    }


def preflight(
    *,
    source_path: Path,
    source_manifest_path: Path,
    policy_path: Path,
    stage_protocol_path: Path,
    minimum_unique_stocks: int = 12,
    minimum_distinct_months: int = 6,
) -> dict[str, Any]:
    source_path = Path(source_path).resolve()
    manifest_path = Path(source_manifest_path).resolve()
    policy_path = Path(policy_path).resolve()
    stage_protocol_path = Path(stage_protocol_path).resolve()
    manifest = _read_json(manifest_path)
    stage = _read_json(stage_protocol_path)
    if minimum_unique_stocks <= 0 or minimum_distinct_months <= 0:
        raise ReachabilityPreflightError(
            "minimum reachability thresholds must be positive"
        )
    if file_sha256(source_path) != EXPECTED_SOURCE_SHA256:
        raise ReachabilityPreflightError(
            "source differs from the pinned full universe"
        )
    if file_sha256(manifest_path) != EXPECTED_SOURCE_MANIFEST_SHA256:
        raise ReachabilityPreflightError(
            "source manifest differs from the pinned full universe"
        )
    if file_sha256(stage_protocol_path) != EXPECTED_STAGE_SHA256:
        raise ReachabilityPreflightError("V4-S1 stage protocol hash changed")
    if (
        stage.get("stage_protocol_version") != EXPECTED_STAGE_VERSION
        or stage.get("status") != "FINAL_RESEARCH_LOCKED"
    ):
        raise ReachabilityPreflightError(
            "V4-S1 stage protocol is not locked"
        )
    stage_source = stage.get("source") or {}
    if (
        int(stage_source.get("stocks", -1)) != EXPECTED_STOCKS
        or int(stage_source.get("review_points", -1))
        != EXPECTED_REVIEW_POINTS
        or stage_source.get("monitoring_start")
        != EXPECTED_MONITORING_START
        or stage_source.get("monitoring_end") != EXPECTED_MONITORING_END
    ):
        raise ReachabilityPreflightError("V4-S1 stage source scope changed")
    for field, expected in {
        "status": "LOCKED_OUTCOME_BLIND",
        "outcome_blind": True,
        "identity_visible": False,
        "future_data_visible": False,
        "performance_visible": False,
    }.items():
        if manifest.get(field) != expected:
            raise ReachabilityPreflightError(f"source manifest changed {field}")
    if (
        manifest.get("source_order_locked") is not True
        or manifest.get("identity_isolated") is not True
        or int(manifest.get("stocks", -1)) != int(stage_source["stocks"])
        or int(manifest.get("review_points", -1))
        != int(stage_source["review_points"])
        or manifest.get("as_of_ceiling") != stage_source["monitoring_end"]
    ):
        raise ReachabilityPreflightError(
            "source manifest does not match stage scope"
        )
    if file_sha256(source_path) != manifest.get("artifact_sha256"):
        raise ReachabilityPreflightError("source artifact differs from manifest")
    if POLICY_VERSION != EXPECTED_POLICY_VERSION:
        raise ReachabilityPreflightError("unexpected V4-S1 policy version")
    if file_sha256(policy_path) != EXPECTED_POLICY_SHA256:
        raise ReachabilityPreflightError("V4-S1 policy hash changed")
    if (
        file_sha256(SOURCE_SAMPLING_CONTRACT)
        != EXPECTED_SAMPLING_CONTRACT_SHA256
    ):
        raise ReachabilityPreflightError("source sampling contract hash changed")
    source_contract = load_sampling_contract(SOURCE_SAMPLING_CONTRACT)

    counts: Counter[str] = Counter()
    blockers: Counter[str] = Counter()
    candidates: list[dict[str, Any]] = []
    seen_review: set[str] = set()
    seen_stocks: set[str] = set()
    seen_as_of: list[str] = []
    for source_ordinal, source in enumerate(_jsonl(source_path)):
        counts["source_rows"] += 1
        forbidden = _contains_forbidden_key(source)
        if forbidden:
            raise ReachabilityPreflightError(
                f"outcome/future key in source ordinal {source_ordinal}: {forbidden}"
            )
        try:
            validate_source_record(source, source_contract)
        except Exception as exc:
            raise ReachabilityPreflightError(
                f"source contract invalid at ordinal {source_ordinal}: {exc}"
            ) from exc
        if int(source.get("source_ordinal", -1)) != source_ordinal:
            raise ReachabilityPreflightError("source ordinal order changed")
        review_id = str(source.get("review_id") or "")
        if not review_id or review_id in seen_review:
            raise ReachabilityPreflightError("source review identity is missing or duplicate")
        seen_review.add(review_id)
        packet = source.get("packet") or {}
        if (
            packet.get("review_id") != review_id
            or packet.get("anonymous_stock_id")
            != source.get("anonymous_stock_id")
            or canonical_sha256(packet) != source.get("packet_sha256")
        ):
            raise ReachabilityPreflightError(
                "source row identity or packet hash changed"
            )
        seen_stocks.add(str(source["anonymous_stock_id"]))
        seen_as_of.append(str(packet.get("as_of") or ""))
        hypotheses = (
            ((packet.get("objective_facts") or {}).get("scenario_hypotheses") or {}).get(TARGET_SCENARIO)
            or []
        )
        if not hypotheses:
            continue
        counts["rows_with_mature_hypothesis"] += 1
        reachable_witness: tuple[Mapping[str, Any], dict[str, Any], dict[str, Any]] | None = None
        for hypothesis in hypotheses:
            counts["mature_hypotheses_tested"] += 1
            symbolic = symbolic_witness(packet, hypothesis)
            semantic = _symbolic_validation_projection(symbolic)
            errors = validate_atomic(packet, semantic)
            if errors:
                counts["invalid_symbolic_witnesses"] += 1
                blockers.update(["VALIDATION:" + error for error in errors])
                continue
            counts["valid_symbolic_witnesses"] += 1
            decision = reduce_atomic_v4_s1(packet, semantic)
            if stage_permission_v4_s1(decision) == "TRADE":
                reachable_witness = (hypothesis, semantic, decision)
                break
            reason_codes = decision.get("reason_codes") or ["NO_REASON_CODE"]
            blockers.update("DECISION:" + str(code) for code in reason_codes)
        if reachable_witness is None:
            continue
        hypothesis, semantic, decision = reachable_witness
        counts["reachable_rows"] += 1
        candidates.append(
            {
                "source_ordinal": source_ordinal,
                "review_id": review_id,
                "anonymous_stock_id": packet["anonymous_stock_id"],
                "as_of": packet["as_of"],
                "packet_sha256": canonical_sha256(packet),
                "hypothesis_id": hypothesis["hypothesis_id"],
                "symbolic_witness_sha256": canonical_sha256(symbolic),
                "action_signature_sha256": canonical_sha256(
                    decision["action_signature"]
                ),
                "classification": "SYMBOLIC_REACHABLE_NOT_SEMANTIC_GOLD",
            }
        )

    expected_rows = int(manifest.get("review_points", -1))
    if counts["source_rows"] != expected_rows:
        raise ReachabilityPreflightError("source row coverage changed")
    if len(seen_stocks) != int(stage_source["stocks"]):
        raise ReachabilityPreflightError(
            "source stock coverage differs from stage"
        )
    if (
        not seen_as_of
        or min(seen_as_of) != stage_source["monitoring_start"]
        or max(seen_as_of) != stage_source["monitoring_end"]
    ):
        raise ReachabilityPreflightError(
            "source date coverage differs from stage"
        )
    unique_stocks = len({row["anonymous_stock_id"] for row in candidates})
    distinct_months = len({str(row["as_of"])[:7] for row in candidates})
    passed = (
        counts["reachable_rows"] >= minimum_unique_stocks
        and unique_stocks >= minimum_unique_stocks
        and distinct_months >= minimum_distinct_months
    )
    status = (
        "PASS_SYMBOLIC_TARGET_ROUTE_REACHABLE"
        if passed
        else "FAIL_INSUFFICIENT_SYMBOLIC_TARGET_ROUTE_CAPACITY"
    )
    core = {
        "preflight_version": PREFLIGHT_VERSION,
        "status": status,
        "target": {"scenario": TARGET_SCENARIO, "route": TARGET_ROUTE},
        "classification": "SOFTWARE_REACHABILITY_ONLY_NOT_AI_LABEL_NOT_COURSE_GOLD",
        "outcome_blind": True,
        "identity_visible": False,
        "future_or_performance_visible": False,
        "ai_output_read": False,
        "symbolic_witness_inside_ai_packet": False,
        "symbolic_witness_has_schema_poison_pill": True,
        "source": {
            "path": str(source_path),
            "sha256": file_sha256(source_path),
            "manifest_path": str(manifest_path),
            "manifest_sha256": file_sha256(manifest_path),
        },
        "policy": {
            "path": str(policy_path),
            "version": POLICY_VERSION,
            "sha256": file_sha256(policy_path),
        },
        "stage_protocol": {
            "path": str(stage_protocol_path),
            "version": stage["stage_protocol_version"],
            "sha256": file_sha256(stage_protocol_path),
        },
        "producer_provenance": {
            "preflight_builder": {
                "relative_path": PREFLIGHT_BUILDER_PATH.relative_to(ROOT).as_posix(),
                "sha256": file_sha256(PREFLIGHT_BUILDER_PATH),
            },
            "source_validator": {
                "relative_path": SOURCE_VALIDATOR_PATH.relative_to(ROOT).as_posix(),
                "sha256": file_sha256(SOURCE_VALIDATOR_PATH),
            },
            "source_sampling_contract": {
                "relative_path": SOURCE_SAMPLING_CONTRACT.relative_to(ROOT).as_posix(),
                "sha256": file_sha256(SOURCE_SAMPLING_CONTRACT),
            },
        },
        "source_scope": {
            "stocks": int(stage_source["stocks"]),
            "review_points": int(stage_source["review_points"]),
            "monitoring_start": stage_source["monitoring_start"],
            "monitoring_end": stage_source["monitoring_end"],
        },
        "requirements": {
            "minimum_unique_stocks": minimum_unique_stocks,
            "minimum_distinct_months": minimum_distinct_months,
        },
        "capacity": {
            "reachable_rows": counts["reachable_rows"],
            "unique_anonymous_stocks": unique_stocks,
            "distinct_months": distinct_months,
        },
        "counts": dict(sorted(counts.items())),
        "blockers": dict(blockers.most_common()),
        "candidates": candidates,
        "candidates_sha256": canonical_sha256(candidates),
        "limitations": [
            "A symbolic witness proves only that software gates can be satisfied.",
            "It does not assert that cited market evidence semantically supports PASS or FAIL.",
            "AI must independently judge the unchanged atomic questions without seeing this witness.",
            "No course-fidelity or performance conclusion is permitted from this preflight.",
        ],
    }
    return {**core, "preflight_sha256": canonical_sha256(core)}


def markdown(report: Mapping[str, Any]) -> str:
    capacity = report["capacity"]
    counts = report["counts"]
    lines = [
        "# V4-S1 結果盲化可達性預檢",
        "",
        f"- 狀態：`{report['status']}`",
        "- 這只證明程式路徑可以成立，不是 AI 答案、課程標準答案或交易績效。",
        "- 未讀取股票身分、AI輸出、未來行情或績效。",
        "",
        "## 容量",
        "",
        f"- 完整結果盲化候選日：{counts.get('source_rows', 0):,}",
        f"- 含成熟多頭假說：{counts.get('rows_with_mature_hypothesis', 0):,}",
        f"- 規則上可達的候選日：{capacity['reachable_rows']:,}",
        f"- 不同匿名股票：{capacity['unique_anonymous_stocks']:,}",
        f"- 涵蓋月份：{capacity['distinct_months']:,}",
        "",
        "## 解讀限制",
        "",
        "可達性代表新規則不再互相封死。候選仍必須由AI在不知道此witness及未來績效的情況下，依實際證據判斷；不能直接視為進場清單。",
        "",
        f"Preflight SHA-256: `{report['preflight_sha256']}`",
        "",
    ]
    return "\n".join(lines)


def publish_report(report: Mapping[str, Any], output_dir: Path) -> dict[str, str]:
    """Atomically publish an idempotent preflight report and return file hashes."""

    output_dir = Path(output_dir).resolve()
    payloads = {
        output_dir / "preflight.json": canonical_json_bytes(dict(report)) + b"\n",
        output_dir / "preflight.md": markdown(report).encode("utf-8"),
    }
    for path, payload in payloads.items():
        try:
            _publish_immutable(path, payload)
        except Exception as exc:
            raise ReachabilityPreflightError(
                f"refusing to overwrite changed preflight: {path}"
            ) from exc
    return {path.name: file_sha256(path) for path in payloads}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--stage-protocol", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    report = preflight(
        source_path=args.source,
        source_manifest_path=args.source_manifest,
        policy_path=args.policy,
        stage_protocol_path=args.stage_protocol,
    )
    output_dir = args.output_dir.resolve()
    publish_report(report, output_dir)
    print(
        json.dumps(
            {
                "status": report["status"],
                "capacity": report["capacity"],
                "preflight_sha256": report["preflight_sha256"],
                "output_dir": str(output_dir),
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["status"].startswith("PASS") else 2


if __name__ == "__main__":
    raise SystemExit(main())
