from pathlib import Path

from scripts.v2_core_stage_b1a1_smoke_compare_v3 import find_invalid_artifacts


def test_find_invalid_artifacts_reports_only_preserved_invalid_raw(tmp_path: Path) -> None:
    review_id = "FP-test"
    manifest = {
        "required_rounds": 3,
        "rows": [{"review_id": review_id}],
    }
    invalid = (
        tmp_path
        / "round_2"
        / "stage_b1a1"
        / f"{review_id}.invalid.raw"
    )
    invalid.parent.mkdir(parents=True)
    invalid.write_bytes(b'{"invalid":true}\n')
    ordinary = (
        tmp_path
        / "round_1"
        / "stage_b1a1"
        / f"{review_id}.raw.json"
    )
    ordinary.parent.mkdir(parents=True)
    ordinary.write_bytes(b'{"valid":true}\n')

    found = find_invalid_artifacts(manifest=manifest, run_directory=tmp_path)

    assert len(found) == 1
    assert found[0]["case_round"] == "round_2:FP-test"
    assert found[0]["size_bytes"] == len(b'{"invalid":true}\n')
    assert len(found[0]["sha256"]) == 64
