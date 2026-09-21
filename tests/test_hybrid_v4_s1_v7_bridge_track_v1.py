from collections import Counter
import json
from pathlib import Path
import tempfile

from scripts import hybrid_v4_s1_v7_bridge_track_v1 as bridge
from scripts import hybrid_v4_s1_v7_bridge_consistency_v1 as consistency


def test_bridge_protocol_is_exact_and_does_not_change_decision_contract():
    protocol = bridge.bridge_protocol()
    sample = protocol["consistency_sample"]
    assert sample["focus_order"] == bridge.FOCUS_ORDER
    assert sample["quotas"] == bridge.QUOTAS
    assert sum(sample["quotas"].values()) == 36
    assert protocol["repeatability_acceptance"]
    assert protocol["disagreement_policy"] == "UNANIMOUS_ELSE_UNKNOWN_AND_NO_TRADE"


def test_bridge_full_prepare_has_exact_strata_and_clean_ai_packets():
    scratch_root = bridge.ROOT / ".codex_tmp"
    scratch_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch_root) as temporary:
        output = Path(temporary) / "bridge"
        result = bridge.prepare(output_dir=output)
        selection = json.loads(
            (output / "selection_plan.json").read_text(encoding="utf-8")
        )
        assert Counter(row["sampling_focus"] for row in selection["rows"]) == Counter(bridge.QUOTAS)
        assert len({row["anonymous_stock_id"] for row in selection["rows"]}) == 36
        months = Counter(row["as_of"][:7] for row in selection["rows"])
        assert len(months) >= 6
        assert max(months.values()) <= 9
        packets = [
            json.loads(line)
            for line in (output / "primary_packets.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(packets) == 36
        serialized = json.dumps(packets, ensure_ascii=False).lower()
        for forbidden in (
            "sampling_focus",
            "legacy_s1_v2_core_positive",
            "legacy_s1_boundary_no_trade",
            "competing_or_invalid_no_trade",
            '"code"',
            '"name"',
            "future_",
            "mfe",
            "mae",
            "pnl",
        ):
            assert forbidden not in serialized
        assert result["track_version"] == bridge.TRACK_VERSION
        cases, _, _, integrity = consistency.verify_frozen_track(
            source=output / "primary_packets.jsonl",
            source_manifest=output / "primary_packets.manifest.json",
            track_manifest=output / "research_track_manifest.json",
            assignment_manifest=output / "primary_cases/assignment.manifest.json",
            execution_freeze=output / "research_execution_freeze.json",
            stage_protocol=bridge.v7.STAGE_PROTOCOL,
            research_protocol=bridge.v7.RESEARCH_PROTOCOL,
            prompt=bridge.v7.PROMPT,
            schema=bridge.v7.SCHEMA,
            policy=bridge.v7.V4_S1_POLICY,
        )
        assert len(cases) == 36
        assert integrity["status"] == "PASS"
