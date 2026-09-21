"""Compare B1a2 rounds with a manifest-frozen path-length escalation policy."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v2_core_stage_b1a2_active_link_compare_v1 import compare as raw_compare
from scripts.v2_core_stage_b1a2_active_link_runner_v1 import validate_existing_artifacts
from scripts.v2_core_stage_b1a2_active_link_validator_v1 import load_json


def _pct(numerator: int, denominator: int) -> float:
    return round(100.0 * numerator / denominator, 2) if denominator else 100.0


def compare(*, artifact_dir: Path, manifest_path: Path) -> dict[str, Any]:
    raw = raw_compare(artifact_dir=artifact_dir, manifest_path=manifest_path)
    if not raw.get("metrics_published"):
        return {**raw, "escalation_metrics": None}
    manifest = load_json(manifest_path)
    threshold = int(manifest["escalation_min_fixed_path_edges"])
    input_manifest = artifact_dir / manifest["input_manifest_file"]
    schema = artifact_dir / manifest["schema_file"]
    prompt = artifact_dir / manifest["prompt_file"]
    run_dir = artifact_dir / manifest["run_directory"]
    total = stable = unflagged = unflagged_stable = drifts = captured = 0
    cases = []
    for row in manifest["rows"]:
        review_id = str(row["review_id"])
        input_path = artifact_dir / manifest["input_packet_directory"] / row["input_packet_file"]
        packet = load_json(input_path)
        options = {str(item["candidate_id"]): item for item in packet["role_link_evidence_options"]}
        validations = []
        for round_number in range(1, int(manifest["required_rounds"]) + 1):
            validations.append(
                validate_existing_artifacts(
                    input_packet_path=input_path,
                    input_manifest_path=input_manifest,
                    schema_path=schema,
                    prompt_path=prompt,
                    output_path=run_dir / f"round_{round_number}" / "stage_b1a2_active_link" / f"{review_id}.json",
                    receipt_path=run_dir / f"round_{round_number}" / "receipts" / f"{review_id}.stage_b1a2_active_link.json",
                )
            )
        indexes = [
            {str(item["candidate_id"]): item["active_campaign_link"]["result"] for item in result["derived_candidate_results"]}
            for result in validations
        ]
        final_ids = []
        unknown_ids = []
        escalation_ids = []
        local_unflagged = local_stable = 0
        for candidate_id, option in options.items():
            results = [index[candidate_id] for index in indexes]
            is_stable = len(set(results)) == 1
            lengths = [len(path["relation_ids"]) for path in option["fixed_relation_paths"]]
            escalate = bool(lengths) and min(lengths) >= threshold
            total += 1
            stable += is_stable
            if not escalate:
                unflagged += 1
                unflagged_stable += int(is_stable)
                local_unflagged += 1
                local_stable += int(is_stable)
            if not is_stable:
                drifts += 1
                captured += int(escalate)
            if escalate:
                escalation_ids.append(candidate_id)
                if all(value == "PASS" for value in results):
                    final_ids.append(candidate_id)
                elif not all(value == "FAIL" for value in results):
                    unknown_ids.append(candidate_id)
            elif results[0] == "PASS" and is_stable:
                final_ids.append(candidate_id)
            elif not is_stable:
                unknown_ids.append(candidate_id)
        cases.append(
            {
                "review_id": review_id,
                "candidate_count": len(options),
                "escalation_candidate_ids": sorted(escalation_ids),
                "final_shortlist_ids": sorted(final_ids),
                "final_unknown_ids": sorted(unknown_ids),
                "unflagged_consistency_percent": _pct(local_stable, local_unflagged),
            }
        )
    metrics = {
        "escalation_min_fixed_path_edges": threshold,
        "raw_active_link_consistency_percent": _pct(stable, total),
        "unflagged_active_link_consistency_percent": _pct(unflagged_stable, unflagged),
        "raw_drift_candidate_count": drifts,
        "drift_capture_percent": _pct(captured, drifts),
        "escalation_candidate_count": sum(len(case["escalation_candidate_ids"]) for case in cases),
        "final_shortlist_reproducibility_percent": (
            100.0 if unflagged_stable == unflagged and captured == drifts
            else _pct(unflagged_stable, unflagged)
        ),
        "cases": cases,
    }
    passed = (
        raw["metrics"]["schema_and_semantic_valid_percent"] == 100
        and metrics["raw_active_link_consistency_percent"] >= 90
        and metrics["unflagged_active_link_consistency_percent"] >= 95
        and metrics["drift_capture_percent"] == 100
        and metrics["final_shortlist_reproducibility_percent"] == 100
    )
    return {
        **raw,
        "status": "SMOKE_PASSED_WITH_R4_ESCALATION（R4升級政策通過）" if passed else "SMOKE_REVISION_REQUIRED（Smoke需要修訂）",
        "escalation_metrics": metrics,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = compare(artifact_dir=args.artifact_dir, manifest_path=args.manifest)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
