"""Outcome-blind reachability audit for V3 scenario 1.

The audit never reads AI decisions, identities, future bars, or performance.  It
checks whether the frozen packet builder and frozen reducer can produce a
MATURE_TREND_PULLBACK/V2_CORE trade candidate in the complete anonymous review
point source universe.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping


AUDIT_VERSION = "hybrid-v3-mature-s1-reachability-audit-v1"
EXPECTED_FROZEN_COMPONENT_SHA256 = {
    "builder": "aee29455083defe3b2846f43c8836d3a5952127ef94a5e9af55067e293eef72e",
    "policy": "b7504b8a1414dc3baab17e047f1827e96c3fa2fdc62b3ce2e6845f18a5cb40b9",
    "policy_adapter": "78219086e8f1d5e3f3fc1be977dbb49e3ba3b232ee9c96bc88097106699d8196",
}
LATE_GENERATIONS = {"COPY_LEG_5", "LATER_GENERATION"}
EARLY_COMPATIBLE_GENERATIONS = {"ANCHOR_LEG_1", "COPY_LEG_3"}
FORBIDDEN_KEYS = {
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
}


class ReachabilityAuditError(ValueError):
    """The frozen outcome-blind source cannot support a valid audit."""


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
        raise ReachabilityAuditError(f"cannot read JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ReachabilityAuditError(f"expected JSON object: {path}")
    return value


def _jsonl(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with Path(path).open(encoding="utf-8-sig") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ReachabilityAuditError(
                        f"expected JSON object at {path}:{line_number}"
                    )
                yield value
    except (OSError, json.JSONDecodeError) as exc:
        raise ReachabilityAuditError(f"cannot read JSONL: {path}") from exc


def _contains_forbidden_key(value: Any) -> str | None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower() in FORBIDDEN_KEYS:
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


def audit(
    *,
    source_path: Path,
    source_manifest_path: Path,
    builder_path: Path,
    policy_path: Path,
    policy_adapter_path: Path,
    enforce_frozen_hashes: bool = True,
) -> dict[str, Any]:
    source_path = Path(source_path).resolve()
    manifest_path = Path(source_manifest_path).resolve()
    manifest = _read_json(manifest_path)
    for field, expected in {
        "status": "LOCKED_OUTCOME_BLIND",
        "outcome_blind": True,
        "identity_visible": False,
        "future_data_visible": False,
        "performance_visible": False,
    }.items():
        if manifest.get(field) != expected:
            raise ReachabilityAuditError(f"source manifest changed {field}")
    if file_sha256(source_path) != manifest.get("artifact_sha256"):
        raise ReachabilityAuditError("source artifact hash differs from manifest")
    components = {
        "builder": Path(builder_path).resolve(),
        "policy": Path(policy_path).resolve(),
        "policy_adapter": Path(policy_adapter_path).resolve(),
    }
    component_hashes = {name: file_sha256(path) for name, path in components.items()}
    if enforce_frozen_hashes:
        for name, expected in EXPECTED_FROZEN_COMPONENT_SHA256.items():
            if component_hashes[name] != expected:
                raise ReachabilityAuditError(f"frozen {name} hash changed")
        if component_hashes["builder"] != manifest.get("builder_code_sha256"):
            raise ReachabilityAuditError("builder hash differs from source manifest")

    row_count = 0
    eligible_rows = 0
    mature_rows = 0
    mature_hypotheses = 0
    late_generation_hypotheses = 0
    early_compatible_hypotheses = 0
    early_compatible_rows = 0
    prior_ge_one_late = 0
    prior_ge_one_early = 0
    generation_counts: Counter[str] = Counter()
    attack_counts: Counter[str] = Counter()

    for row in _jsonl(source_path):
        row_count += 1
        forbidden = _contains_forbidden_key(row)
        if forbidden:
            raise ReachabilityAuditError(
                f"outcome/future field entered source row {row_count - 1}: {forbidden}"
            )
        if "V2_CORE_OBJECTIVE_PROXY" in (row.get("eligible_sampling_strata") or []):
            eligible_rows += 1
        packet = row.get("packet") or {}
        objective = packet.get("objective_facts") or {}
        hypotheses = (
            (objective.get("scenario_hypotheses") or {}).get(
                "MATURE_TREND_PULLBACK"
            )
            or []
        )
        if not hypotheses:
            continue
        mature_rows += 1
        has_early = False
        for hypothesis in hypotheses:
            if not isinstance(hypothesis, dict):
                raise ReachabilityAuditError("mature hypothesis is not an object")
            mature_hypotheses += 1
            generation = str(hypothesis.get("taiji_generation"))
            attack_number = hypothesis.get("same_direction_attack_number")
            prior = hypothesis.get("completed_prior_copy_count")
            generation_counts[generation] += 1
            attack_counts[str(attack_number)] += 1
            if generation in LATE_GENERATIONS:
                late_generation_hypotheses += 1
            if generation in EARLY_COMPATIBLE_GENERATIONS:
                early_compatible_hypotheses += 1
                has_early = True
            if isinstance(prior, int) and prior >= 1:
                if generation in LATE_GENERATIONS:
                    prior_ge_one_late += 1
                if generation in EARLY_COMPATIBLE_GENERATIONS:
                    prior_ge_one_early += 1
        if has_early:
            early_compatible_rows += 1

    expected_rows = int(manifest.get("review_points", -1))
    if row_count != expected_rows:
        raise ReachabilityAuditError(
            f"source row coverage differs: {row_count} != {expected_rows}"
        )

    # Frozen reducer semantics:
    # - MATURE scenario requires completed_prior_copy_count >= 1.
    # - COPY_LEG_5/LATER_GENERATION always derives LATE or EXHAUSTED.
    # - common hard guard NOT_LATE_OR_EXHAUSTED therefore returns FAIL.
    reachable_hypotheses = prior_ge_one_early
    status = (
        "FULL_SOURCE_UNIVERSE_UNREACHABLE_BY_FROZEN_BUILDER_POLICY_COMPOSITION"
        if reachable_hypotheses == 0
        else "OBJECTIVELY_REACHABLE_CONTROLS_EXIST"
    )
    core = {
        "audit_version": AUDIT_VERSION,
        "status": status,
        "scope": "MATURE_TREND_PULLBACK/V2_CORE_ONLY",
        "outcome_blind": True,
        "identity_visible": False,
        "future_or_performance_visible": False,
        "ai_output_read": False,
        "rules_modified": False,
        "source": {
            "path": str(source_path),
            "sha256": file_sha256(source_path),
            "manifest_path": str(manifest_path),
            "manifest_sha256": file_sha256(manifest_path),
            "rows": row_count,
        },
        "frozen_components": {
            name: {"path": str(path), "sha256": component_hashes[name]}
            for name, path in components.items()
        },
        "audit_code_sha256": file_sha256(Path(__file__).resolve()),
        "counts": {
            "v2_core_objective_proxy_rows": eligible_rows,
            "rows_with_mature_hypothesis": mature_rows,
            "mature_hypotheses": mature_hypotheses,
            "late_generation_hypotheses": late_generation_hypotheses,
            "early_compatible_generation_hypotheses": early_compatible_hypotheses,
            "rows_with_early_compatible_generation": early_compatible_rows,
            "prior_ge_one_and_late_generation": prior_ge_one_late,
            "prior_ge_one_and_early_compatible_generation": prior_ge_one_early,
            "reachable_positive_control_hypotheses": reachable_hypotheses,
            "generation_counts": dict(sorted(generation_counts.items())),
            "attack_number_counts": dict(
                sorted(attack_counts.items(), key=lambda item: (int(item[0]) if item[0].isdigit() else 10**9, item[0]))
            ),
        },
        "proof": [
            "The frozen packet builder assigns attack 1=ANCHOR_LEG_1, 2=COPY_LEG_3, 3=COPY_LEG_5, and >=4=LATER_GENERATION.",
            "The frozen builder enumerates MATURE_TREND_PULLBACK only when completed_prior_copy_count >= 1; in the frozen builder this begins at attack 3.",
            "The frozen reducer derives COPY_LEG_5 and LATER_GENERATION as LATE or EXHAUSTED for every semantic answer combination.",
            "The frozen V2_CORE common hard guard requires NOT_LATE_OR_EXHAUSTED=PASS.",
            "Therefore every mature hypothesis produced by this builder is rejected before V2_CORE can trade.",
        ],
        "separate_technical_defect": {
            "present": True,
            "description": (
                "hybrid_v3_atomic_policy_v3.validate_atomic permits the frozen "
                "REVERSAL_PROBE adapter exception, but reduce_atomic_v3 is aliased "
                "to the V2 reducer, which re-validates with the V2 validator and can "
                "return INVALID_PACKET. This is independent of the mature-route "
                "unreachability proof and requires a new versioned technical fix."
            ),
        },
        "conclusion": (
            "A different sample cannot make scenario 1 trade while the frozen builder "
            "and frozen V3 policy remain composed this way. Course audit and performance "
            "backtest must remain sealed."
        ),
    }
    return {**core, "audit_sha256": canonical_sha256(core)}


def markdown(report: Mapping[str, Any]) -> str:
    counts = report["counts"]
    lines = [
        "# V3 情境1結果盲化可達性稽核",
        "",
        f"- 狀態：`{report['status']}`",
        "- 範圍：`MATURE_TREND_PULLBACK / V2_CORE`",
        "- 未讀取 AI 輸出、股票身分、未來行情或績效。",
        "- 未修改 V1、V2、V3 規則與既有凍結產物。",
        "",
        "## 全母體結果",
        "",
        f"- 結果盲化候選日：{report['source']['rows']:,}",
        f"- `V2_CORE_OBJECTIVE_PROXY` 候選日：{counts['v2_core_objective_proxy_rows']:,}",
        f"- 含成熟多頭假說的候選日：{counts['rows_with_mature_hypothesis']:,}",
        f"- 成熟多頭假說總數：{counts['mature_hypotheses']:,}",
        f"- `COPY_LEG_5`／`LATER_GENERATION`：{counts['late_generation_hypotheses']:,}",
        f"- `ANCHOR_LEG_1`／`COPY_LEG_3`：{counts['early_compatible_generation_hypotheses']:,}",
        f"- 在規則上可能通過 `NOT_LATE_OR_EXHAUSTED` 的成熟假說：{counts['reachable_positive_control_hypotheses']:,}",
        "",
        "## 矛盾鏈",
        "",
        "1. 封包建立器只把已有至少一段完成複製的關係列為成熟多頭；這從第3次同向攻擊開始。",
        "2. 第3次同向攻擊固定標為 `COPY_LEG_5`，之後固定標為 `LATER_GENERATION`。",
        "3. 凍結 reducer 對這兩種世代必定推導為 `LATE` 或 `EXHAUSTED`。",
        "4. `V2_CORE` 又強制要求 `NOT_LATE_OR_EXHAUSTED=PASS`。",
        "5. 因此情境1在目前建立器＋規則組合中，無論 AI 如何回答都無法進場。",
        "",
        "## 結論",
        "",
        str(report["conclusion"]),
        "",
        "另外發現一個獨立技術問題：V3 validator 接受 `REVERSAL_PROBE` 例外，但 V3 reducer 仍呼叫 V2 validator，部分合法封包會被錯標為 `INVALID_PACKET`。此問題不改變上述情境1不可達的結論。",
        "",
        f"Audit SHA-256: `{report['audit_sha256']}`",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--builder", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--policy-adapter", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    report = audit(
        source_path=args.source,
        source_manifest_path=args.source_manifest,
        builder_path=args.builder,
        policy_path=args.policy,
        policy_adapter_path=args.policy_adapter,
    )
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "reachability_audit.json"
    markdown_path = output_dir / "reachability_audit.md"
    json_payload = canonical_json_bytes(report) + b"\n"
    markdown_payload = markdown(report).encode("utf-8")
    for path, payload in ((json_path, json_payload), (markdown_path, markdown_payload)):
        if path.exists() and path.read_bytes() != payload:
            raise ReachabilityAuditError(f"refusing to overwrite changed audit: {path}")
        if not path.exists():
            path.write_bytes(payload)
    print(json.dumps({"status": report["status"], "audit_sha256": report["audit_sha256"], "output_dir": str(output_dir)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
