from pathlib import Path

from scripts import hybrid_v4_s1_v7_consistency_v1 as consistency
from scripts import hybrid_v4_s1_v7_execution_orchestrator_v1 as orchestrator
from scripts import hybrid_v4_s1_v7_track_v1 as track


def test_v7_track_keeps_ai_and_rule_contract_fixed():
    assert track.MODEL == "gpt-5.6-sol"
    assert track.REASONING == "xhigh"
    assert track.V4_S1_POLICY == track.base.V4_S1_POLICY
    assert track.PROMPT == track.base.PROMPT
    assert track.SCHEMA == track.base.SCHEMA
    assert track.STOP_REMAINING_PERCENT == 50


def test_v7_adapters_bind_exact_versioned_paths():
    assert consistency.TRACK_VERSION == track.TRACK_VERSION
    assert consistency.FREEZE_VERSION == track.FREEZE_VERSION
    assert consistency.TRACK_STATUS == track.TRACK_STATUS
    assert orchestrator.EXPECTED_THRESHOLD == 50.0
    for relative in consistency.V4_COMPONENT_PATHS.values():
        assert (track.ROOT / Path(relative)).is_file()
