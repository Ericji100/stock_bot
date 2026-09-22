from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import scripts.dual_ma_historical_selection_batch as batch


def _frozen_manifest_payload(target: date, source_id: str, fingerprints: dict[str, str]) -> dict:
    payload = {
        "schema_version": 2,
        "collector_version": "dual_ma_daily_selection_v2",
        "report_date": target.isoformat(),
        "historical_replay": True,
        "scan_settings": {"min_price": 10},
        "provenance": {
            "universe": {
                "status": "VERIFIED_AS_OF_MEMBERSHIP",
                "source_id": source_id,
            },
            "selector_source_sha256": fingerprints,
        },
        "overall_status": "DEGRADED",
        "program_order": list(batch.collection_service.PROGRAM_ORDER),
        "programs": {
            key: {"key": key, "status": "SUCCESS"}
            for key in batch.collection_service.PROGRAM_ORDER
        },
        "union_count": 0,
        "union_codes": [],
        "candidates": [],
        "universe_notice": "historical membership supplied",
    }
    digest = batch._sha256(payload)
    payload.update(
        {
            "manifest_id": f"dual-ma-selection@{target.isoformat()}#{digest[:16]}",
            "content_sha256": digest,
            "generated_at": "2026-09-21T00:00:00+08:00",
        }
    )
    return payload


def test_find_completed_manifest_validates_content_and_inputs(tmp_path):
    target = date(2022, 1, 3)
    fingerprints = {"financial": "abc"}
    payload = _frozen_manifest_payload(target, "universe-1", fingerprints)
    directory = tmp_path / "2022" / target.isoformat()
    directory.mkdir(parents=True)
    path = directory / f"{payload['manifest_id']}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert batch.find_completed_manifest(
        target,
        tmp_path,
        universe_source_id="universe-1",
        scan_settings={"min_price": 10},
        selector_source_sha256=fingerprints,
    ) == path
    assert batch.find_completed_manifest(
        target,
        tmp_path,
        universe_source_id="different",
        scan_settings={"min_price": 10},
        selector_source_sha256=fingerprints,
    ) is None


def test_run_batch_checkpoints_each_day_and_resumes(monkeypatch, tmp_path):
    dates = [date(2022, 1, 3), date(2022, 1, 4)]
    fingerprints = {"financial": "abc"}
    monkeypatch.setattr(batch, "selector_fingerprints", lambda: fingerprints)
    monkeypatch.setattr(batch, "find_completed_manifest", lambda *_args, **_kwargs: None)

    calls = []

    class FakeManifest:
        overall_status = "DEGRADED"
        union_codes = ["2330"]
        provenance = {"selector_source_sha256": fingerprints}
        programs = {
            key: SimpleNamespace(status="SUCCESS")
            for key in batch.collection_service.PROGRAM_ORDER
        }
        content_sha256 = "f" * 64

    def collect(target, *_args, **_kwargs):
        calls.append(target)
        snapshot = tmp_path / f"snapshot-{target}.json"
        snapshot.write_text("{}", encoding="utf-8")
        return FakeManifest(), snapshot

    def freeze(_manifest, output_root):
        path = Path(output_root) / "manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        return path

    monkeypatch.setattr(batch.collection_service, "collect_historical_daily_selection", collect)
    monkeypatch.setattr(batch.collection_service, "freeze_daily_selection_manifest", freeze)
    universe = SimpleNamespace(source_id="universe-1")

    result = batch.run_batch(
        start_date=dates[0],
        end_date=dates[-1],
        universe=universe,
        scan_settings={"min_price": 10},
        output_root=tmp_path / "manifests",
        snapshot_root=tmp_path / "snapshots",
        run_root=tmp_path / "runs",
        dates=dates,
        progress=None,
    )

    assert calls == dates
    assert result.completed == 2
    assert result.status == "COMPLETE"
    journal = json.loads(result.run_path.read_text(encoding="utf-8"))
    assert journal["status"] == "COMPLETE"
    assert [item["status"] for item in journal["records"]] == ["COMPLETED", "COMPLETED"]


def test_run_batch_honours_stop_file_before_next_date(monkeypatch, tmp_path):
    target = date(2022, 1, 3)
    monkeypatch.setattr(batch, "selector_fingerprints", lambda: {"financial": "abc"})
    stop_file = tmp_path / "STOP_REQUESTED"
    stop_file.write_text("pause", encoding="utf-8")

    result = batch.run_batch(
        start_date=target,
        end_date=target,
        universe=SimpleNamespace(source_id="universe-1"),
        scan_settings={},
        output_root=tmp_path / "manifests",
        snapshot_root=tmp_path / "snapshots",
        run_root=tmp_path / "runs",
        dates=[target],
        stop_file=stop_file,
        progress=None,
    )

    assert result.status == "PAUSED_STOP_REQUESTED"
    assert result.completed == 0
    journal = json.loads(result.run_path.read_text(encoding="utf-8"))
    assert journal["status"] == "PAUSED_STOP_REQUESTED"
    assert journal["records"] == []
