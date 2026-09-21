from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import hybrid_v3_atomic_policy_v2 as v2
from scripts import hybrid_v4_s1_reachability_preflight_v1 as preflight
from tests.test_hybrid_v3_atomic_policy_v2 import _hypothesis, _packet


def _source(tmp_path: Path, packet: dict) -> tuple[Path, Path]:
    row = {
        "source_ordinal": 0,
        "review_id": packet["review_id"],
        "anonymous_stock_id": packet["anonymous_stock_id"],
        "packet_sha256": preflight.canonical_sha256(packet),
        "eligible_sampling_strata": ["V2_CORE_OBJECTIVE_PROXY"],
        "packet": packet,
    }
    source = tmp_path / "review_points.jsonl"
    source.write_text(json.dumps(row) + "\n", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "status": "LOCKED_OUTCOME_BLIND",
                "outcome_blind": True,
                "identity_visible": False,
                "future_data_visible": False,
                "performance_visible": False,
                "source_order_locked": True,
                "identity_isolated": True,
                "stocks": 1,
                "as_of_ceiling": packet["as_of"],
                "review_points": 1,
                "artifact_sha256": preflight.file_sha256(source),
            }
        ),
        encoding="utf-8",
    )
    return source, manifest


def _stage(tmp_path: Path, packet: dict) -> Path:
    path = tmp_path / "stage.json"
    path.write_text(
        json.dumps(
            {
                "stage_protocol_version": preflight.EXPECTED_STAGE_VERSION,
                "status": "FINAL_RESEARCH_LOCKED",
                "source": {
                    "stocks": 1,
                    "review_points": 1,
                    "monitoring_start": packet["as_of"],
                    "monitoring_end": packet["as_of"],
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _patch_fixture_scope(
    monkeypatch: pytest.MonkeyPatch,
    *,
    source: Path,
    manifest: Path,
    stage: Path,
) -> None:
    monkeypatch.setattr(preflight, "EXPECTED_SOURCE_SHA256", preflight.file_sha256(source))
    monkeypatch.setattr(
        preflight,
        "EXPECTED_SOURCE_MANIFEST_SHA256",
        preflight.file_sha256(manifest),
    )
    monkeypatch.setattr(preflight, "EXPECTED_STAGE_SHA256", preflight.file_sha256(stage))
    monkeypatch.setattr(preflight, "EXPECTED_STOCKS", 1)
    monkeypatch.setattr(preflight, "EXPECTED_REVIEW_POINTS", 1)
    stage_value = json.loads(stage.read_text(encoding="utf-8"))
    monkeypatch.setattr(
        preflight,
        "EXPECTED_MONITORING_START",
        stage_value["source"]["monitoring_start"],
    )
    monkeypatch.setattr(
        preflight,
        "EXPECTED_MONITORING_END",
        stage_value["source"]["monitoring_end"],
    )
    monkeypatch.setattr(preflight, "validate_source_record", lambda *_args: None)


def _mature_packet() -> dict:
    packet = _packet()
    mature = _hypothesis(packet, "MATURE_TREND_PULLBACK")
    mature.update(
        {
            "taiji_generation": "COPY_LEG_5",
            "same_direction_attack_number": 3,
            "completed_prior_copy_count": 1,
        }
    )
    packet["objective_facts"]["scenario_hypotheses"] = {
        "MATURE_TREND_PULLBACK": [mature],
        "MACRO_COPY_RESONANCE": [],
        "BEAR_REVERSAL_LEFT_RIGHT": [],
        "FRESH_Q1_EXPANSION": [],
    }
    for scenario in (
        "MACRO_COPY_RESONANCE",
        "BEAR_REVERSAL_LEFT_RIGHT",
        "FRESH_Q1_EXPANSION",
    ):
        packet["objective_facts"]["data_sufficiency_by_route"][scenario].update(
            {
                "status": False,
                "reason_code": "INSUFFICIENT_ROUTE_OBJECTIVE_EVIDENCE",
            }
        )
    packet["question_manifest"] = v2.expected_question_manifest(packet)
    return packet


def test_symbolic_witness_reaches_v4_s1_trade() -> None:
    packet = _mature_packet()
    semantic = preflight.symbolic_witness(
        packet,
        packet["objective_facts"]["scenario_hypotheses"]["MATURE_TREND_PULLBACK"][0],
    )
    assert semantic["_preflight_symbolic_only"] == {
        "preflight_version": preflight.PREFLIGHT_VERSION,
        "classification": "NOT_AI_OUTPUT_NOT_COURSE_GOLD",
    }
    assert preflight.validate_atomic(packet, semantic)
    projected = preflight._symbolic_validation_projection(semantic)
    assert preflight.validate_atomic(packet, projected) == []
    decision = preflight.reduce_atomic_v4_s1(packet, projected)
    assert preflight.stage_permission_v4_s1(decision) == "TRADE"


def test_preflight_reports_capacity_without_claiming_semantic_gold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packet = _mature_packet()
    source, manifest = _source(tmp_path, packet)
    stage = _stage(tmp_path, packet)
    policy = Path(preflight.__file__).with_name("hybrid_v4_s1_atomic_policy_v1.py")
    monkeypatch.setattr(
        preflight, "EXPECTED_POLICY_SHA256", preflight.file_sha256(policy)
    )
    _patch_fixture_scope(
        monkeypatch, source=source, manifest=manifest, stage=stage
    )
    result = preflight.preflight(
        source_path=source,
        source_manifest_path=manifest,
        policy_path=policy,
        stage_protocol_path=stage,
        minimum_unique_stocks=1,
        minimum_distinct_months=1,
    )
    assert result["status"] == "PASS_SYMBOLIC_TARGET_ROUTE_REACHABLE"
    assert result["capacity"]["reachable_rows"] == 1
    assert result["candidates"][0]["classification"] == "SYMBOLIC_REACHABLE_NOT_SEMANTIC_GOLD"
    assert result["future_or_performance_visible"] is False
    assert result["ai_output_read"] is False
    assert result["symbolic_witness_has_schema_poison_pill"] is True
    assert result["source_scope"] == {
        "stocks": 1,
        "review_points": 1,
        "monitoring_start": packet["as_of"],
        "monitoring_end": packet["as_of"],
    }
    assert result["stage_protocol"]["version"] == preflight.EXPECTED_STAGE_VERSION
    assert result["producer_provenance"] == {
        "preflight_builder": {
            "relative_path": "scripts/hybrid_v4_s1_reachability_preflight_v1.py",
            "sha256": preflight.file_sha256(preflight.PREFLIGHT_BUILDER_PATH),
        },
        "source_validator": {
            "relative_path": "scripts/hybrid_v3_consistency_v3.py",
            "sha256": preflight.file_sha256(preflight.SOURCE_VALIDATOR_PATH),
        },
        "source_sampling_contract": {
            "relative_path": "config/hybrid_multilabel_sampling_v3.json",
            "sha256": preflight.file_sha256(preflight.SOURCE_SAMPLING_CONTRACT),
        },
    }


def test_outcome_field_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packet = _mature_packet()
    packet["future_return"] = 10.0
    source, manifest = _source(tmp_path, packet)
    stage = _stage(tmp_path, packet)
    policy = Path(preflight.__file__).with_name("hybrid_v4_s1_atomic_policy_v1.py")
    monkeypatch.setattr(
        preflight, "EXPECTED_POLICY_SHA256", preflight.file_sha256(policy)
    )
    _patch_fixture_scope(
        monkeypatch, source=source, manifest=manifest, stage=stage
    )
    with pytest.raises(preflight.ReachabilityPreflightError, match="outcome/future"):
        preflight.preflight(
            source_path=source,
            source_manifest_path=manifest,
            policy_path=policy,
            stage_protocol_path=stage,
            minimum_unique_stocks=1,
            minimum_distinct_months=1,
        )


def test_policy_hash_mismatch_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packet = _mature_packet()
    source, manifest = _source(tmp_path, packet)
    stage = _stage(tmp_path, packet)
    _patch_fixture_scope(
        monkeypatch, source=source, manifest=manifest, stage=stage
    )
    policy = tmp_path / "policy.py"
    policy.write_text("# not frozen\n", encoding="utf-8")
    with pytest.raises(preflight.ReachabilityPreflightError, match="policy hash changed"):
        preflight.preflight(
            source_path=source,
            source_manifest_path=manifest,
            policy_path=policy,
            stage_protocol_path=stage,
            minimum_unique_stocks=1,
            minimum_distinct_months=1,
        )


@pytest.mark.parametrize(
    "forbidden_key",
    ["stock_name", "ticker", "winner", "outcome", "next_20d_return"],
)
def test_identity_and_outcome_synonyms_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forbidden_key: str,
) -> None:
    packet = _mature_packet()
    packet[forbidden_key] = "leaked"
    source, manifest = _source(tmp_path, packet)
    stage = _stage(tmp_path, packet)
    policy = Path(preflight.__file__).with_name("hybrid_v4_s1_atomic_policy_v1.py")
    monkeypatch.setattr(
        preflight, "EXPECTED_POLICY_SHA256", preflight.file_sha256(policy)
    )
    _patch_fixture_scope(
        monkeypatch, source=source, manifest=manifest, stage=stage
    )
    with pytest.raises(preflight.ReachabilityPreflightError, match="outcome/future"):
        preflight.preflight(
            source_path=source,
            source_manifest_path=manifest,
            policy_path=policy,
            stage_protocol_path=stage,
            minimum_unique_stocks=1,
            minimum_distinct_months=1,
        )


@pytest.mark.parametrize(
    ("minimum_unique_stocks", "minimum_distinct_months"),
    [(0, 1), (-1, 1), (1, 0), (1, -1)],
)
def test_nonpositive_capacity_thresholds_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    minimum_unique_stocks: int,
    minimum_distinct_months: int,
) -> None:
    packet = _mature_packet()
    source, manifest = _source(tmp_path, packet)
    stage = _stage(tmp_path, packet)
    policy = Path(preflight.__file__).with_name("hybrid_v4_s1_atomic_policy_v1.py")
    monkeypatch.setattr(
        preflight, "EXPECTED_POLICY_SHA256", preflight.file_sha256(policy)
    )
    _patch_fixture_scope(
        monkeypatch, source=source, manifest=manifest, stage=stage
    )
    with pytest.raises(
        preflight.ReachabilityPreflightError,
        match="minimum reachability thresholds must be positive",
    ):
        preflight.preflight(
            source_path=source,
            source_manifest_path=manifest,
            policy_path=policy,
            stage_protocol_path=stage,
            minimum_unique_stocks=minimum_unique_stocks,
            minimum_distinct_months=minimum_distinct_months,
        )


def test_stage_scope_mismatch_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packet = _mature_packet()
    source, manifest = _source(tmp_path, packet)
    stage = _stage(tmp_path, packet)
    stage_value = json.loads(stage.read_text(encoding="utf-8"))
    stage_value["source"]["stocks"] = 2
    stage.write_text(json.dumps(stage_value), encoding="utf-8")
    policy = Path(preflight.__file__).with_name("hybrid_v4_s1_atomic_policy_v1.py")
    monkeypatch.setattr(
        preflight, "EXPECTED_POLICY_SHA256", preflight.file_sha256(policy)
    )
    _patch_fixture_scope(
        monkeypatch, source=source, manifest=manifest, stage=stage
    )
    with pytest.raises(
        preflight.ReachabilityPreflightError, match="stage source scope changed"
    ):
        preflight.preflight(
            source_path=source,
            source_manifest_path=manifest,
            policy_path=policy,
            stage_protocol_path=stage,
            minimum_unique_stocks=1,
            minimum_distinct_months=1,
        )


def test_preflight_publish_is_idempotent_and_rejects_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packet = _mature_packet()
    source, manifest = _source(tmp_path, packet)
    stage = _stage(tmp_path, packet)
    policy = Path(preflight.__file__).with_name("hybrid_v4_s1_atomic_policy_v1.py")
    monkeypatch.setattr(
        preflight, "EXPECTED_POLICY_SHA256", preflight.file_sha256(policy)
    )
    _patch_fixture_scope(
        monkeypatch, source=source, manifest=manifest, stage=stage
    )
    result = preflight.preflight(
        source_path=source,
        source_manifest_path=manifest,
        policy_path=policy,
        stage_protocol_path=stage,
        minimum_unique_stocks=1,
        minimum_distinct_months=1,
    )
    output = tmp_path / "published"
    first = preflight.publish_report(result, output)
    second = preflight.publish_report(result, output)
    assert first == second
    assert set(first) == {"preflight.json", "preflight.md"}

    changed = dict(result)
    changed["status"] = "FAIL_CHANGED_FOR_CONFLICT_TEST"
    with pytest.raises(
        preflight.ReachabilityPreflightError,
        match="refusing to overwrite changed preflight",
    ):
        preflight.publish_report(changed, output)
