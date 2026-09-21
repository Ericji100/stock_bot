import json
from pathlib import Path

import pytest

from scripts import hybrid_v3_mature_s1_reachability_audit_v1 as audit


def _sha(path: Path) -> str:
    return audit.file_sha256(path)


def _fixture(tmp_path: Path, generations: list[tuple[str, int, int]]) -> tuple[Path, Path, Path, Path, Path]:
    builder = tmp_path / "builder.py"
    policy = tmp_path / "policy.py"
    adapter = tmp_path / "adapter.py"
    for path in (builder, policy, adapter):
        path.write_text("# frozen\n", encoding="utf-8")
    rows = []
    for index, (generation, attack, prior) in enumerate(generations):
        rows.append(
            {
                "eligible_sampling_strata": ["V2_CORE_OBJECTIVE_PROXY"],
                "packet": {
                    "objective_facts": {
                        "scenario_hypotheses": {
                            "MATURE_TREND_PULLBACK": [
                                {
                                    "hypothesis_id": f"H-{index}",
                                    "taiji_generation": generation,
                                    "same_direction_attack_number": attack,
                                    "completed_prior_copy_count": prior,
                                }
                            ]
                        }
                    }
                },
            }
        )
    source = tmp_path / "source.jsonl"
    source.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "status": "LOCKED_OUTCOME_BLIND",
                "outcome_blind": True,
                "identity_visible": False,
                "future_data_visible": False,
                "performance_visible": False,
                "review_points": len(rows),
                "artifact_sha256": _sha(source),
            }
        ),
        encoding="utf-8",
    )
    return source, manifest, builder, policy, adapter


def test_all_mature_builder_generations_are_unreachable(tmp_path: Path) -> None:
    paths = _fixture(tmp_path, [("COPY_LEG_5", 3, 1), ("LATER_GENERATION", 4, 2)])
    result = audit.audit(
        source_path=paths[0], source_manifest_path=paths[1], builder_path=paths[2],
        policy_path=paths[3], policy_adapter_path=paths[4], enforce_frozen_hashes=False
    )
    assert result["status"].startswith("FULL_SOURCE_UNIVERSE_UNREACHABLE")
    assert result["counts"]["reachable_positive_control_hypotheses"] == 0
    assert result["counts"]["late_generation_hypotheses"] == 2


def test_counterexample_is_reported_reachable(tmp_path: Path) -> None:
    paths = _fixture(tmp_path, [("COPY_LEG_3", 2, 1)])
    result = audit.audit(
        source_path=paths[0], source_manifest_path=paths[1], builder_path=paths[2],
        policy_path=paths[3], policy_adapter_path=paths[4], enforce_frozen_hashes=False
    )
    assert result["status"] == "OBJECTIVELY_REACHABLE_CONTROLS_EXIST"
    assert result["counts"]["reachable_positive_control_hypotheses"] == 1


def test_future_or_performance_key_is_rejected(tmp_path: Path) -> None:
    paths = _fixture(tmp_path, [("COPY_LEG_5", 3, 1)])
    row = json.loads(paths[0].read_text(encoding="utf-8").splitlines()[0])
    row["packet"]["future_return"] = 12.3
    paths[0].write_text(json.dumps(row) + "\n", encoding="utf-8")
    manifest = json.loads(paths[1].read_text(encoding="utf-8"))
    manifest["artifact_sha256"] = _sha(paths[0])
    paths[1].write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(audit.ReachabilityAuditError, match="outcome/future"):
        audit.audit(
            source_path=paths[0], source_manifest_path=paths[1], builder_path=paths[2],
            policy_path=paths[3], policy_adapter_path=paths[4], enforce_frozen_hashes=False
        )


def test_source_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    paths = _fixture(tmp_path, [("COPY_LEG_5", 3, 1)])
    paths[0].write_text(paths[0].read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(audit.ReachabilityAuditError, match="hash differs"):
        audit.audit(
            source_path=paths[0], source_manifest_path=paths[1], builder_path=paths[2],
            policy_path=paths[3], policy_adapter_path=paths[4], enforce_frozen_hashes=False
        )


def test_frozen_component_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    paths = _fixture(tmp_path, [("COPY_LEG_5", 3, 1)])
    with pytest.raises(audit.ReachabilityAuditError, match="frozen builder hash changed"):
        audit.audit(
            source_path=paths[0], source_manifest_path=paths[1], builder_path=paths[2],
            policy_path=paths[3], policy_adapter_path=paths[4]
        )
