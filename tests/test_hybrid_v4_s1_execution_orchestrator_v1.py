import copy
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import hybrid_v4_s1_execution_orchestrator_v1 as orchestrator


NOW = datetime(2026, 9, 9, 4, 0, tzinfo=timezone.utc)


def test_direct_cli_entrypoint_can_load_project_modules() -> None:
    completed = subprocess.run(
        [sys.executable, str(Path(orchestrator.__file__).resolve()), "--help"],
        cwd=Path(orchestrator.__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--usage-attestation" in completed.stdout


def _json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _attestation(
    remaining: int | float = 21,
    *,
    check_id: str = "usage-check-0001",
    checked_at: str | None = None,
) -> dict:
    return {
        "attestation_version": orchestrator.USAGE_ATTESTATION_VERSION,
        "usage_source": orchestrator.USAGE_SOURCE,
        "remaining_percent": remaining,
        "checked_at": checked_at or NOW.isoformat().replace("+00:00", "Z"),
        "check_id": check_id,
    }


def _has_english_and_chinese(value: str) -> bool:
    return bool(re.search(r"[A-Za-z]", value)) and bool(
        re.search(r"[\u4e00-\u9fff]", value)
    )


@pytest.fixture
def frozen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    paths = {
        name: tmp_path / filename
        for name, filename in {
            "source": "primary_packets.jsonl",
            "source_manifest": "primary_packets.manifest.json",
            "track_manifest": "research_track_manifest.json",
            "assignment_manifest": "primary_cases/assignment.manifest.json",
            "execution_freeze": "research_execution_freeze.json",
            "stage_protocol": "stage.json",
            "research_protocol": "protocol.json",
            "prompt": "prompt.md",
            "schema": "schema.json",
            "policy": "policy.py",
            "shard": "primary_cases/shard_00.jsonl",
            "run_root": "runs/r1",
            "control_dir": "control",
        }.items()
    }
    paths["source"].write_text("{}\n", encoding="utf-8")
    _json(paths["source_manifest"], {"kind": "source-manifest"})
    _json(paths["track_manifest"], {"kind": "track"})
    _json(
        paths["execution_freeze"],
        {
            "expected_cases": 36,
            "expected_runs": 3,
            "budget_guard": {
                "stop_when_remaining_percent_at_or_below": 20,
                "preserve_resume_checkpoint": True,
                "enforcement_boundary": "EXTERNAL_EXECUTION_ORCHESTRATOR",
            },
            "execution_contract": {
                "model": "gpt-5.6-sol",
                "reasoning_effort": "xhigh",
            },
        },
    )
    _json(paths["stage_protocol"], {"kind": "stage"})
    _json(paths["research_protocol"], {"kind": "protocol"})
    paths["prompt"].write_text("prompt", encoding="utf-8")
    _json(paths["schema"], {"type": "object"})
    paths["policy"].write_text("# frozen policy\n", encoding="utf-8")
    paths["shard"].parent.mkdir(parents=True, exist_ok=True)
    paths["shard"].write_text('{"case_key":"K-1"}\n', encoding="utf-8")
    _json(
        paths["assignment_manifest"],
        {
            "expected_rows": 36,
            "shard_count": 1,
            "execution_contract_sha256": orchestrator.file_sha256(
                paths["execution_freeze"]
            ),
            "shards": [
                {
                    "shard_id": 0,
                    "path": str(paths["shard"].resolve()),
                    "rows": 1,
                    "sha256": orchestrator.file_sha256(paths["shard"]),
                }
            ],
        },
    )

    verify_calls: list[dict] = []

    def verify_frozen_track(**kwargs):
        verify_calls.append(kwargs)
        return [], {}, {}, {"status": "PASS / 驗證通過"}

    monkeypatch.setattr(
        orchestrator.consistency_v1, "verify_frozen_track", verify_frozen_track
    )
    expected = {
        "expected_track_sha256": orchestrator.file_sha256(paths["track_manifest"]),
        "expected_assignment_sha256": orchestrator.file_sha256(
            paths["assignment_manifest"]
        ),
        "expected_freeze_sha256": orchestrator.file_sha256(
            paths["execution_freeze"]
        ),
        "expected_shard_sha256": orchestrator.file_sha256(paths["shard"]),
    }
    return {"paths": paths, "expected": expected, "verify_calls": verify_calls}


def _execute(frozen: dict, **overrides):
    paths = frozen["paths"]
    values = {
        "source": paths["source"],
        "source_manifest": paths["source_manifest"],
        "track_manifest": paths["track_manifest"],
        "assignment_manifest": paths["assignment_manifest"],
        "execution_freeze": paths["execution_freeze"],
        "stage_protocol": paths["stage_protocol"],
        "research_protocol": paths["research_protocol"],
        "prompt": paths["prompt"],
        "schema": paths["schema"],
        "policy": paths["policy"],
        "run_root": paths["run_root"],
        "control_dir": paths["control_dir"],
        "run_number": 1,
        "shard_id": 0,
        "usage_attestation": _attestation(),
        "now": NOW,
        **frozen["expected"],
    }
    values.update(overrides)
    return orchestrator.execute_shard(**values)


@pytest.mark.parametrize("remaining", [0, 20])
def test_remaining_at_or_below_20_never_invokes_launcher_and_checkpoints(
    frozen: dict, remaining: int
) -> None:
    calls: list[object] = []

    result = _execute(
        frozen,
        usage_attestation=_attestation(
            remaining, check_id=f"usage-stop-{remaining:04d}"
        ),
        launcher=lambda args: calls.append(args),
    )
    assert calls == []
    assert result["launcher_called"] is False
    assert result["resume_required"] is True
    assert result["status"] == orchestrator.STATUS_BUDGET_STOP
    assert _has_english_and_chinese(result["status"])
    checkpoint_path = Path(result["resume_checkpoint_path"])
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    core = {key: value for key, value in checkpoint.items() if key != "receipt_sha256"}
    assert checkpoint["receipt_sha256"] == orchestrator.canonical_sha256(core)
    assert checkpoint["launcher_called"] is False
    assert checkpoint["usage_attestation"]["remaining_percent"] == remaining
    assert _has_english_and_chinese(checkpoint["status"])

    # The same external reading and target is an idempotent checkpoint replay.
    repeated = _execute(
        frozen,
        usage_attestation=_attestation(
            remaining, check_id=f"usage-stop-{remaining:04d}"
        ),
        launcher=lambda args: calls.append(args),
    )
    assert repeated == result
    assert calls == []


def test_remaining_above_20_persists_pass_then_invokes_launcher_exactly_once(
    frozen: dict,
) -> None:
    calls: list[object] = []

    def fake_launcher(args):
        receipt_paths = list(
            frozen["paths"]["control_dir"].glob(
                "budget_pass_receipts/run_01/shard_00/*.json"
            )
        )
        assert len(receipt_paths) == 1, "receipt must exist before launcher call"
        calls.append(args)
        return {"runner_result": "RESUMED_OR_COMPLETED"}

    result = _execute(frozen, launcher=fake_launcher)
    assert len(calls) == 1
    args = calls[0]
    assert args.case_records == frozen["paths"]["shard"].resolve()
    assert args.output_dir == frozen["paths"]["run_root"].resolve() / "s00"
    assert args.run_number == 1
    assert args.model == "gpt-5.6-sol"
    assert args.reasoning_effort == "xhigh"
    assert args.limit_cases is None
    assert result["launcher_called"] is True
    assert result["resume_delegated_to_existing_runner"] is True
    assert result["status"] == orchestrator.STATUS_DISPATCHED
    assert result["budget_status"] == orchestrator.STATUS_BUDGET_PASS
    assert _has_english_and_chinese(result["status"])
    assert _has_english_and_chinese(result["budget_status"])
    receipt = json.loads(Path(result["budget_receipt_path"]).read_text(encoding="utf-8"))
    assert receipt["status"] == orchestrator.STATUS_BUDGET_PASS
    assert receipt["launcher_call_authorized"] is True
    assert receipt["launcher_called_at_receipt_time"] is False


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: value.pop("remaining_percent"),
        lambda value: value.update(usage_source="SHELL_GUESS"),
        lambda value: value.update(remaining_percent=-1),
        lambda value: value.update(remaining_percent=101),
        lambda value: value.update(remaining_percent=True),
        lambda value: value.update(checked_at="2026-09-09T04:00:00"),
        lambda value: value.update(check_id="short"),
    ],
)
def test_invalid_or_missing_usage_attestation_fails_closed(mutator) -> None:
    value = _attestation()
    mutator(value)
    with pytest.raises(orchestrator.V4S1ExecutionOrchestratorError):
        orchestrator.validate_usage_attestation(value, now=NOW)


def test_stale_usage_attestation_is_rejected() -> None:
    stale = (NOW - timedelta(seconds=orchestrator.MAX_ATTESTATION_AGE_SECONDS + 1))
    with pytest.raises(orchestrator.V4S1ExecutionOrchestratorError, match="stale"):
        orchestrator.validate_usage_attestation(
            _attestation(checked_at=stale.isoformat()), now=NOW
        )


def test_missing_attestation_never_invokes_launcher(frozen: dict) -> None:
    calls: list[object] = []
    with pytest.raises(
        orchestrator.V4S1ExecutionOrchestratorError, match="attestation fields"
    ):
        _execute(
            frozen,
            usage_attestation={},
            launcher=lambda args: calls.append(args),
        )
    assert calls == []
    assert not frozen["paths"]["control_dir"].exists()


def test_same_check_id_with_conflicting_reading_is_rejected_without_launcher(
    frozen: dict,
) -> None:
    calls: list[object] = []
    _execute(
        frozen,
        usage_attestation=_attestation(20, check_id="usage-conflict-0001"),
        launcher=lambda args: calls.append(args),
    )
    with pytest.raises(
        orchestrator.V4S1ExecutionOrchestratorError,
        match="conflicting immutable receipt",
    ):
        _execute(
            frozen,
            usage_attestation=_attestation(21, check_id="usage-conflict-0001"),
            launcher=lambda args: calls.append(args),
        )
    assert calls == []


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("track_manifest", "track manifest"),
        ("assignment_manifest", "assignment manifest"),
        ("execution_freeze", "execution freeze"),
        ("shard", "shard"),
    ],
)
def test_changed_external_frozen_hashes_are_rejected_before_verification_or_launch(
    frozen: dict, target: str, message: str
) -> None:
    path = frozen["paths"][target]
    path.write_bytes(path.read_bytes() + b"changed")
    calls: list[object] = []
    with pytest.raises(orchestrator.V4S1ExecutionOrchestratorError, match=message):
        _execute(frozen, launcher=lambda args: calls.append(args))
    assert calls == []
    assert frozen["verify_calls"] == []


def test_native_frozen_verifier_receives_every_exact_input(frozen: dict) -> None:
    _execute(frozen, launcher=lambda args: {"ok": True})
    assert len(frozen["verify_calls"]) == 1
    call = frozen["verify_calls"][0]
    for name in (
        "source",
        "source_manifest",
        "track_manifest",
        "assignment_manifest",
        "execution_freeze",
        "stage_protocol",
        "research_protocol",
        "prompt",
        "schema",
        "policy",
    ):
        assert call[name] == frozen["paths"][name].resolve()


def test_unsupported_execution_override_is_rejected_without_launcher(frozen: dict) -> None:
    calls: list[object] = []
    with pytest.raises(orchestrator.V4S1ExecutionOrchestratorError, match="overrides"):
        _execute(
            frozen,
            unsupported_overrides={"model": "another-model"},
            launcher=lambda args: calls.append(args),
        )
    assert calls == []
    assert frozen["verify_calls"] == []


def test_native_v4_builder_and_evaluator_contract_dispatches_one_real_shard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import hybrid_v4_s1_track_v1 as track

    focuses = [
        focus
        for focus, count in track.EXPECTED_QUOTAS.items()
        for _ in range(count)
    ]
    selected: list[dict] = []
    packets: list[dict] = []
    for ordinal, focus in enumerate(focuses):
        packet = {
            "review_id": f"R-{ordinal:02d}",
            "anonymous_stock_id": f"S-{ordinal:02d}",
            "as_of": f"2023-{6 + ordinal % 6:02d}-01",
        }
        packets.append(packet)
        selected.append(
            {
                "source_ordinal": ordinal,
                "review_id": packet["review_id"],
                "anonymous_stock_id": packet["anonymous_stock_id"],
                "as_of": packet["as_of"],
                "packet_sha256": orchestrator.canonical_sha256(packet),
                "sampling_focus": focus,
                "eligible_stage_focuses": [focus],
            }
        )
    monkeypatch.setattr(
        track,
        "scan_source",
        lambda *_args: (copy.deepcopy(selected), {"review_points": 36}, []),
    )
    monkeypatch.setattr(track, "allocate", lambda *_args: copy.deepcopy(selected))
    monkeypatch.setattr(
        track, "extract_packets", lambda *_args: copy.deepcopy(packets)
    )
    source_input = tmp_path / "synthetic_source.jsonl"
    source_input.write_text("frozen synthetic source\n", encoding="utf-8")
    source_input_manifest = tmp_path / "synthetic_source.manifest.json"
    source_input_manifest.write_text("{}\n", encoding="utf-8")
    output = tmp_path / "native_track"
    track.prepare(
        source_path=source_input,
        source_manifest_path=source_input_manifest,
        preflight_path=track.STAGE_PROTOCOL,
        output_dir=output,
        shard_count=4,
    )
    assignment_path = output / "primary_cases/assignment.manifest.json"
    assignment = json.loads(assignment_path.read_text(encoding="utf-8"))
    shard = next(row for row in assignment["shards"] if row["shard_id"] == 0)
    calls: list[object] = []
    result = orchestrator.execute_shard(
        source=output / "primary_packets.jsonl",
        source_manifest=output / "primary_packets.manifest.json",
        track_manifest=output / "research_track_manifest.json",
        assignment_manifest=assignment_path,
        execution_freeze=output / "research_execution_freeze.json",
        stage_protocol=track.STAGE_PROTOCOL,
        research_protocol=track.RESEARCH_PROTOCOL,
        prompt=track.PROMPT,
        schema=track.SCHEMA,
        policy=track.V4_S1_POLICY,
        run_root=tmp_path / "runs/r1",
        control_dir=tmp_path / "control",
        run_number=1,
        shard_id=0,
        usage_attestation=_attestation(50, check_id="usage-native-0001"),
        expected_track_sha256=orchestrator.file_sha256(
            output / "research_track_manifest.json"
        ),
        expected_assignment_sha256=orchestrator.file_sha256(assignment_path),
        expected_freeze_sha256=orchestrator.file_sha256(
            output / "research_execution_freeze.json"
        ),
        expected_shard_sha256=shard["sha256"],
        launcher=lambda args: calls.append(args) or {"ok": True},
        now=NOW,
    )
    assert result["status"] == orchestrator.STATUS_DISPATCHED
    assert len(calls) == 1
    assert calls[0].case_records == Path(shard["path"]).resolve()
