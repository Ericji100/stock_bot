from pathlib import Path

import pytest

from scripts import hybrid_v3_research_track_v3 as v3


def test_candidate4_execution_components_exist_and_are_distinct() -> None:
    paths = [v3.LAUNCHER, v3.REVIEWER, v3.RUNNER, v3.POLICY, v3.PROMPT, v3.SCHEMA, v3.PROTOCOL]
    assert all(Path(path).is_file() for path in paths)
    assert len({Path(path).resolve() for path in paths}) == len(paths)


def test_preflight_must_be_complete_and_outcome_blind(tmp_path: Path) -> None:
    path = tmp_path / "preflight.json"
    path.write_text(
        '{"status":"PASS","invalid_records":1,"identity_visible":false,'
        '"future_performance_visible":false}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="did not pass"):
        v3._validate_preflight(path)
