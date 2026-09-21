import json
from collections import Counter
from pathlib import Path

import pytest

from scripts import hybrid_v4_s1_track_v1 as track


def _packet(index: int, *, mature: bool = True, leaked_key: str | None = None) -> dict:
    facts = {
        "scenario_hypotheses": {
            track.TARGET_SCENARIO: [{"hypothesis_id": f"H-{index}"}] if mature else [],
            "MACRO_COPY_RESONANCE": [],
            "FRESH_Q1_EXPANSION": [],
            "BEAR_REVERSAL_LEFT_RIGHT": [],
        },
        "wait_boundary_reasons": [],
    }
    packet = {
        "review_id": f"R-{index}",
        "anonymous_stock_id": f"S-{index}",
        "as_of": f"2023-{6 + index % 6:02d}-01",
        "objective_facts": facts,
    }
    if leaked_key is not None:
        packet[leaked_key] = "SHOULD_NOT_BE_VISIBLE"
    return packet


def _record(index: int, focus: str) -> dict:
    packet = _packet(index, mature=focus.startswith("MATURE_"))
    return {
        "source_ordinal": index,
        "review_id": packet["review_id"],
        "anonymous_stock_id": packet["anonymous_stock_id"],
        "packet_sha256": track.canonical_sha256(packet),
        "as_of": packet["as_of"],
        "eligible_sampling_strata": [],
        "eligible_stage_focuses": [focus],
    }


def _small_protocol(quotas: dict[str, int], *, minimum_months: int = 1) -> dict:
    return {
        "classification": "OUTCOME_BLIND_SINGLE_ROUTE_RESEARCH_NOT_COURSE_VALIDATED",
        "consistency_sample": {
            "seed": "TEST-V4-S1",
            "runs": 3,
            "cases": sum(quotas.values()),
            "focus_order": list(quotas),
            "quotas": quotas,
            "minimum_distinct_months": minimum_months,
            "maximum_cases_per_month": 9,
        },
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_locked_protocol_has_exact_v4_s1_quota_and_lineage() -> None:
    value = track._stage_protocol()
    sample = value["consistency_sample"]
    assert value["status"] == "FINAL_RESEARCH_LOCKED"
    assert value["target"] == {
        "primary_scenario": "MATURE_TREND_PULLBACK",
        "trade_route": "V2_CORE",
        "label_zh": "長多慣性拉回再發動完整合格",
        "other_scenarios_are_negative_controls": True,
        "other_v3_routes_suspended": True,
    }
    assert value["rule_lineage"]["v4_s1_changes_v3"] is True
    assert value["rule_lineage"]["v1_v2_v3_are_read_only"] is True
    assert sample["quotas"]["MATURE_SYMBOLIC_REACHABLE"] == 18
    assert sample["quotas"]["MATURE_SYMBOLIC_UNREACHABLE"] == 6
    assert list(sample["quotas"]) == sample["focus_order"]
    assert sum(sample["quotas"].values()) == sample["cases"] == 36
    assert sample["minimum_distinct_months"] >= 6
    assert sample["maximum_cases_per_month"] <= 9


def test_stage_focuses_separate_symbolic_mature_and_objective_controls() -> None:
    mature = {
        "review_id": "R-1",
        "eligible_sampling_strata": [
            track.SOURCE_STRATA["MACRO"],
            track.SOURCE_STRATA["REMOVE"],
            track.SOURCE_STRATA["WAIT"],
        ],
        "packet": _packet(1, mature=True),
    }
    mature["packet"]["objective_facts"]["wait_boundary_reasons"] = [
        "UP_ATTACK_WITHOUT_CAUSAL_STOP",
        "MATERIAL_OBJECTIVE_HYPOTHESIS_CONFLICT",
    ]
    assert track.stage_focuses(mature, {"R-1"}) == [
        "MATURE_SYMBOLIC_REACHABLE",
        "MACRO_DEFENSE_REMOVE",
        "WAIT_NO_CAUSAL_STOP",
        "WAIT_MATERIAL_CONFLICT",
    ]
    assert track.stage_focuses(mature, set())[0] == "MATURE_SYMBOLIC_UNREACHABLE"

    macro_only = {
        "review_id": "R-2",
        "eligible_sampling_strata": [track.SOURCE_STRATA["MACRO"]],
        "packet": _packet(2, mature=False),
    }
    assert track.stage_focuses(macro_only, set()) == ["COMPETING_MACRO_ONLY"]


@pytest.mark.parametrize(
    ("source_key", "focus"),
    [
        ("MACRO", "COMPETING_MACRO_ONLY"),
        ("FRESH", "COMPETING_FRESH_ONLY"),
        ("BEAR", "COMPETING_BEAR_ONLY"),
    ],
)
def test_competing_focuses_require_absent_mature_hypothesis(
    source_key: str, focus: str
) -> None:
    row = {
        "review_id": "R-9",
        "eligible_sampling_strata": [track.SOURCE_STRATA[source_key]],
        "packet": _packet(9, mature=False),
    }
    assert track.stage_focuses(row, set()) == [focus]
    row["packet"] = _packet(9, mature=True)
    assert focus not in track.stage_focuses(row, set())


def test_allocator_fills_all_36_quotas_with_stock_and_month_constraints() -> None:
    protocol = track._stage_protocol()
    rows: list[dict] = []
    index = 0
    for focus, quota in protocol["consistency_sample"]["quotas"].items():
        for _ in range(quota):
            rows.append(_record(index, focus))
            index += 1

    selected = track.allocate(rows, protocol)
    assert len(selected) == 36
    assert len({row["anonymous_stock_id"] for row in selected}) == 36
    assert Counter(row["sampling_focus"] for row in selected) == Counter(
        protocol["consistency_sample"]["quotas"]
    )
    months = Counter(row["as_of"][:7] for row in selected)
    assert len(months) >= 6
    assert max(months.values()) <= 9


def test_allocator_fails_closed_when_month_coverage_is_impossible() -> None:
    quotas = {"MATURE_SYMBOLIC_REACHABLE": 2}
    protocol = _small_protocol(quotas, minimum_months=2)
    rows = [_record(0, next(iter(quotas))), _record(6, next(iter(quotas)))]
    assert {row["as_of"][:7] for row in rows} == {"2023-06"}
    with pytest.raises(track.V4S1TrackError, match="cannot fill|month"):
        track.allocate(rows, protocol)


def test_allocator_reserves_multi_eligible_case_for_scarce_focus() -> None:
    quotas = {
        "MATURE_SYMBOLIC_REACHABLE": 1,
        "WAIT_MATERIAL_CONFLICT": 1,
    }
    protocol = _small_protocol(quotas, minimum_months=2)
    flexible = _record(0, "MATURE_SYMBOLIC_REACHABLE")
    flexible["eligible_stage_focuses"].append("WAIT_MATERIAL_CONFLICT")
    mature_only = _record(1, "MATURE_SYMBOLIC_REACHABLE")
    selected = track.allocate([flexible, mature_only], protocol)
    by_focus = {row["sampling_focus"]: row for row in selected}
    assert by_focus["WAIT_MATERIAL_CONFLICT"]["review_id"] == flexible["review_id"]
    assert len({row["anonymous_stock_id"] for row in selected}) == 2
    assert len({row["as_of"][:7] for row in selected}) == 2


def test_preflight_loader_validates_hash_chain_and_discards_witness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "review_points.jsonl"
    source.write_bytes(b"locked-source\n")
    source_manifest = tmp_path / "review_point_manifest.json"
    source_manifest.write_bytes(b"{}\n")
    candidates = [
        {
            "source_ordinal": index,
            "review_id": f"R-{index}",
            "anonymous_stock_id": f"S-{index}",
            "as_of": f"2023-{6 + index % 6:02d}-01",
            "packet_sha256": f"{index + 1:064x}",
            "hypothesis_id": f"H-{index}",
            "symbolic_witness_sha256": "b" * 64,
            "action_signature_sha256": "c" * 64,
            "classification": "SYMBOLIC_REACHABLE_NOT_SEMANTIC_GOLD",
        }
        for index in range(18)
    ]
    core = {
        "preflight_version": track.PREFLIGHT_VERSION,
        "status": track.EXPECTED_PREFLIGHT_STATUS,
        "target": {"scenario": track.TARGET_SCENARIO, "route": track.TARGET_ROUTE},
        "classification": track.EXPECTED_PREFLIGHT_CLASSIFICATION,
        "outcome_blind": True,
        "identity_visible": False,
        "future_or_performance_visible": False,
        "ai_output_read": False,
        "symbolic_witness_inside_ai_packet": False,
        "symbolic_witness_has_schema_poison_pill": True,
        "source": {
            "sha256": track.file_sha256(source),
            "manifest_sha256": track.file_sha256(source_manifest),
        },
        "policy": {
            "version": "hybrid-v4-s1-atomic-policy-v1",
            "sha256": track.file_sha256(track.V4_S1_POLICY),
        },
        "stage_protocol": {
            "version": "hybrid-v4-s1-mature-stage-v1",
            "sha256": track.file_sha256(track.STAGE_PROTOCOL),
        },
        "producer_provenance": {
            "preflight_builder": {
                "relative_path": "scripts/hybrid_v4_s1_reachability_preflight_v1.py",
                "sha256": track.EXPECTED_PREFLIGHT_BUILDER_SHA256,
            },
            "source_validator": {
                "relative_path": "scripts/hybrid_v3_consistency_v3.py",
                "sha256": track.EXPECTED_SOURCE_VALIDATOR_SHA256,
            },
            "source_sampling_contract": {
                "relative_path": "config/hybrid_multilabel_sampling_v3.json",
                "sha256": track.EXPECTED_SOURCE_SAMPLING_CONTRACT_SHA256,
            },
        },
        "source_scope": {
            "stocks": 1029,
            "review_points": 7180,
            "monitoring_start": "2023-06-01",
            "monitoring_end": "2024-02-02",
        },
        "requirements": {
            "minimum_unique_stocks": 12,
            "minimum_distinct_months": 6,
        },
        "capacity": {
            "reachable_rows": 18,
            "unique_anonymous_stocks": 18,
            "distinct_months": 6,
        },
        "candidates": candidates,
        "candidates_sha256": track.canonical_sha256(candidates),
    }
    preflight = {**core, "preflight_sha256": track.canonical_sha256(core)}
    preflight_path = tmp_path / "preflight.json"
    _write_json(preflight_path, preflight)

    with pytest.raises(track.V4S1TrackError, match="authoritative source hash"):
        track.load_reachability_preflight(
            preflight_path,
            source_path=source,
            source_manifest_path=source_manifest,
        )
    monkeypatch.setattr(track, "EXPECTED_SOURCE_SHA256", track.file_sha256(source))
    monkeypatch.setattr(
        track,
        "EXPECTED_SOURCE_MANIFEST_SHA256",
        track.file_sha256(source_manifest),
    )
    monkeypatch.setattr(
        track, "EXPECTED_PREFLIGHT_FILE_SHA256", track.file_sha256(preflight_path)
    )
    minimal = track.load_reachability_preflight(
        preflight_path,
        source_path=source,
        source_manifest_path=source_manifest,
    )
    assert len(minimal) == 18
    assert minimal["R-3"] == {
        "source_ordinal": 3,
        "anonymous_stock_id": "S-3",
        "as_of": "2023-09-01",
        "packet_sha256": f"{4:064x}",
    }
    assert "symbolic" not in json.dumps(minimal)

    preflight["capacity"]["reachable_rows"] = 2
    _write_json(preflight_path, preflight)
    monkeypatch.setattr(
        track, "EXPECTED_PREFLIGHT_FILE_SHA256", track.file_sha256(preflight_path)
    )
    with pytest.raises(track.V4S1TrackError, match="self-hash"):
        track.load_reachability_preflight(
            preflight_path,
            source_path=source,
            source_manifest_path=source_manifest,
        )


def test_scan_source_cross_checks_reachable_identity_and_never_reads_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packets = [_packet(0, mature=True), _packet(1, mature=True)]
    rows = [
        {
            "source_ordinal": index,
            "review_id": packet["review_id"],
            "anonymous_stock_id": packet["anonymous_stock_id"],
            "eligible_sampling_strata": [track.SOURCE_STRATA["MATURE"]],
            "packet_sha256": track.canonical_sha256(packet),
            "packet": packet,
        }
        for index, packet in enumerate(packets)
    ]
    source = tmp_path / "source.jsonl"
    source.write_bytes(track._canonical_jsonl(rows))
    manifest = {
        "status": "LOCKED_OUTCOME_BLIND",
        "outcome_blind": True,
        "identity_visible": False,
        "identity_isolated": True,
        "performance_visible": False,
        "future_data_visible": False,
        "source_order_locked": True,
        "artifact_sha256": track.file_sha256(source),
        "sampling_contract_sha256": track.file_sha256(track.SOURCE_SAMPLING_CONTRACT),
        "review_points": 2,
    }
    manifest_path = tmp_path / "manifest.json"
    _write_json(manifest_path, manifest)
    reachable = {
        "R-0": {
            "source_ordinal": 0,
            "anonymous_stock_id": "S-0",
            "as_of": packets[0]["as_of"],
            "packet_sha256": track.canonical_sha256(packets[0]),
        }
    }
    monkeypatch.setattr(track, "load_reachability_preflight", lambda *args, **kwargs: reachable)
    monkeypatch.setattr(track, "load_sampling_contract", lambda *args, **kwargs: {})
    monkeypatch.setattr(track, "validate_source_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        track, "_validate_authoritative_source_scope", lambda *args, **kwargs: None
    )

    records, _, returned = track.scan_source(source, manifest_path, tmp_path / "unused")
    assert returned == reachable
    assert records[0]["eligible_stage_focuses"] == ["MATURE_SYMBOLIC_REACHABLE"]
    assert records[1]["eligible_stage_focuses"] == ["MATURE_SYMBOLIC_UNREACHABLE"]

    rows[1]["packet"]["pnl"] = 123
    source.write_bytes(track._canonical_jsonl(rows))
    manifest["artifact_sha256"] = track.file_sha256(source)
    _write_json(manifest_path, manifest)
    with pytest.raises(track.V4S1TrackError, match="outcome/future key"):
        track.scan_source(source, manifest_path, tmp_path / "unused")


@pytest.mark.parametrize(
    "leaked_key",
    ["sampling_focus", "_preflight_symbolic_only", "action_signature_sha256"],
)
def test_extract_packets_preserves_packet_but_rejects_sampling_or_witness_leak(
    tmp_path: Path, leaked_key: str,
) -> None:
    packet = _packet(1)
    source_row = {
        "review_id": packet["review_id"],
        "packet_sha256": track.canonical_sha256(packet),
        "packet": packet,
    }
    source = tmp_path / "source.jsonl"
    source.write_bytes(track._canonical_jsonl([source_row]))
    selected = [
        {
            "review_id": packet["review_id"],
            "packet_sha256": track.canonical_sha256(packet),
        }
    ]
    assert track.extract_packets(source, selected) == [packet]

    leaked = _packet(2, leaked_key=leaked_key)
    source.write_bytes(
        track._canonical_jsonl(
            [
                {
                    "review_id": leaked["review_id"],
                    "packet_sha256": track.canonical_sha256(leaked),
                    "packet": leaked,
                }
            ]
        )
    )
    selected[0] = {
        "review_id": leaked["review_id"],
        "packet_sha256": track.canonical_sha256(leaked),
    }
    with pytest.raises(track.V4S1TrackError, match="leaked into AI packet"):
        track.extract_packets(source, selected)


def test_v3_transport_chain_and_v4_decision_chain_are_separate() -> None:
    v2 = track._v2_components()
    assert [row["name"] for row in v2] == [
        "launcher",
        "reviewer",
        "runner",
        "policy",
        "prompt",
        "schema",
        "protocol",
    ]
    assert next(row for row in v2 if row["name"] == "policy")["relative_path"] == (
        "scripts/hybrid_v3_atomic_policy_v3.py"
    )
    assert track.V4_S1_POLICY.name == "hybrid_v4_s1_atomic_policy_v1.py"
    assert track.REACHABILITY_PREFLIGHT_BUILDER.name == (
        "hybrid_v4_s1_reachability_preflight_v1.py"
    )
    assert track.CONSISTENCY_EVALUATOR.name == "hybrid_v4_s1_consistency_v1.py"
    assert track.EXECUTION_ORCHESTRATOR.name == (
        "hybrid_v4_s1_execution_orchestrator_v1.py"
    )


def test_v4_component_chain_requires_real_evaluator_and_producers() -> None:
    preflight = (
        track.V4_ROOT / "reachability_preflight_v2_integrity/preflight.json"
    )
    assert preflight.is_file()
    components = track._v4_s1_components(preflight)
    assert [row["name"] for row in components] == [
        "stage_protocol",
        "reachability_preflight",
        "reachability_preflight_builder",
        "policy",
        "track_builder",
        "evaluator",
        "execution_orchestrator",
    ]
    for row in components:
        path = track.ROOT / row["relative_path"]
        assert path.is_file()
        assert row["sha256"] == track.file_sha256(path)


def test_prepare_writes_only_immutable_outcome_blind_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protocol = track._stage_protocol()
    rows: list[dict] = []
    packets_by_review: dict[str, dict] = {}
    index = 0
    for focus, quota in protocol["consistency_sample"]["quotas"].items():
        for _ in range(quota):
            row = _record(index, focus)
            rows.append(row)
            packets_by_review[row["review_id"]] = _packet(
                index, mature=focus.startswith("MATURE_")
            )
            index += 1

    source = tmp_path / "source.jsonl"
    source.write_bytes(b"synthetic outcome-blind source\n")
    source_manifest = tmp_path / "source.manifest.json"
    source_manifest.write_bytes(b"{}\n")
    preflight = tmp_path / "preflight.json"
    preflight.write_bytes(b"{}\n")
    output = tmp_path / "track"

    monkeypatch.setattr(
        track,
        "scan_source",
        lambda *args, **kwargs: (rows, {"review_points": 36}, {f"R-{i}": {} for i in range(18)}),
    )
    monkeypatch.setattr(
        track,
        "extract_packets",
        lambda _source, selected: [
            packets_by_review[str(row["review_id"])] for row in selected
        ],
    )
    monkeypatch.setattr(
        track,
        "_v4_s1_components",
        lambda _path: [
            {
                "name": name,
                "relative_path": f"placeholder/{name}",
                "status": "PINNED_FOR_TEST",
                "sha256": "d" * 64,
            }
            for name in (
                "stage_protocol",
                "reachability_preflight",
                "reachability_preflight_builder",
                "policy",
                "track_builder",
                "evaluator",
                "execution_orchestrator",
            )
        ],
    )

    result = track.prepare(
        source_path=source,
        source_manifest_path=source_manifest,
        preflight_path=preflight,
        output_dir=output,
        shard_count=4,
    )
    assert result["cases"] == 36
    assert result["runs"] == 3
    assert result["policy_decision_changed_relative_to_v3"] is True
    for relative in (
        "selection_plan.json",
        "primary_packets.jsonl",
        "primary_packets.manifest.json",
        "research_execution_freeze.json",
        "primary_cases/assignment.manifest.json",
        "research_track_manifest.json",
    ):
        assert (output / relative).is_file()

    freeze = json.loads((output / "research_execution_freeze.json").read_text())
    freeze_core = {key: value for key, value in freeze.items() if key != "freeze_sha256"}
    assert freeze["freeze_sha256"] == track.canonical_sha256(freeze_core)
    assert freeze["expected_cases"] == 36
    assert freeze["expected_runs"] == 3
    assert freeze["policy_decision_changed_relative_to_v3"] is True
    assert freeze["strategy_or_gate_change"] is True
    assert freeze["v1_v2_v3_read_only"] is True
    assert freeze["v1_v2_v3_artifacts_modified"] is False
    assert freeze["symbolic_witness_inside_ai_packet"] is False
    assert freeze["budget_guard"]["stop_when_remaining_percent_at_or_below"] == 20
    assert freeze["budget_guard"]["preserve_resume_checkpoint"] is True
    assert freeze["budget_guard"]["enforcement_boundary"] == (
        "EXTERNAL_EXECUTION_ORCHESTRATOR"
    )
    assert freeze["budget_guard"]["enforcement_component"] == (
        "execution_orchestrator"
    )
    assert [row["name"] for row in freeze["v4_s1_components"]] == [
        "stage_protocol",
        "reachability_preflight",
        "reachability_preflight_builder",
        "policy",
        "track_builder",
        "evaluator",
        "execution_orchestrator",
    ]

    packet_rows = [
        json.loads(line)
        for line in (output / "primary_packets.jsonl").read_text().splitlines()
    ]
    assert len(packet_rows) == 36
    assert all("sampling_focus" not in json.dumps(packet) for packet in packet_rows)
    assert all("symbolic_witness" not in json.dumps(packet) for packet in packet_rows)
    case_rows = []
    for shard in sorted((output / "primary_cases").glob("shard_*.jsonl")):
        case_rows.extend(json.loads(line) for line in shard.read_text().splitlines())
    assert len(case_rows) == 36
    assert all(row["execution_contract_sha256"] == track.file_sha256(output / "research_execution_freeze.json") for row in case_rows)
    assert all("sampling_focus" not in row for row in case_rows)

    # Publishing the exact same bytes is idempotent.
    assert track.prepare(
        source_path=source,
        source_manifest_path=source_manifest,
        preflight_path=preflight,
        output_dir=output,
        shard_count=4,
    ) == result

    # A changed pre-existing output is never overwritten.
    selection = output / "selection_plan.json"
    selection.write_bytes(b"conflicting artifact\n")
    with pytest.raises(Exception, match="refusing to overwrite immutable artifact"):
        track.prepare(
            source_path=source,
            source_manifest_path=source_manifest,
            preflight_path=preflight,
            output_dir=output,
            shard_count=4,
        )
