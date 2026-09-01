from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from experiments.trade_monitor_clock_probe.clock_probe import (
    build_isolated_analysis_prompt,
    build_structured_analysis_prompt,
    commit_completed_capture,
    decide_capture,
    expected_latest_closed_k,
    next_minute_slot,
    parse_capture_time,
    read_automation_prompt,
    sha256_bytes,
    snapshot_prompt,
    verify_manifest,
)
from experiments.trade_monitor_clock_probe.message_renderer import (
    AnalysisValidationError,
    render_analysis_markdown,
    render_unavailable_markdown,
    validate_analysis_payload,
)


def sample_analysis_payload() -> dict[str, object]:
    return {
        "latest_closed_k_price_estimate": "收盤約 46,820～46,825（圖面估計）。",
        "latest_closed_k_details": ["屬夜盤、美股開盤後時段。"],
        "large_trend": {
            "classification": "偏多但回檔",
            "details": ["價格仍在緩升的均價105上方。"],
        },
        "current_trend": {
            "classification": "轉換中",
            "details": ["價格位於均價21附近。"],
        },
        "market_state": ["多頭結構中的高檔回檔。"],
        "pattern_observation": {
            "status": "條件式偏多",
            "pattern": "趨勢拉回延續",
            "details": ["尚未完成重新轉多觸發。"],
        },
        "missing_conditions_or_trigger": ["等待已收盤K突破拉回小區間。"],
        "entry_and_structural_stop": ["觸發完成後下一根第一個可成交價格評估。"],
        "risk_and_nearest_obstacle": ["尚無進場價，無法可靠計算1R。"],
        "single_contract_management_or_prohibition": ["目前觀望，不建立持倉。"],
    }


def test_read_automation_prompt_preserves_text(tmp_path: Path) -> None:
    automation = tmp_path / "automation.toml"
    automation.write_text('prompt = "第一行\\n第二行"\n', encoding="utf-8")

    assert read_automation_prompt(automation) == "第一行\n第二行"


def test_snapshot_and_verify_do_not_modify_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    experiment_dir = tmp_path / "experiment"
    experiment_dir.mkdir()
    automation = tmp_path / "automation.toml"
    protected = tmp_path / "protected.py"
    automation.write_text('prompt = "固定規則"\n', encoding="utf-8")
    protected.write_text("unchanged\n", encoding="utf-8")
    source_before = protected.read_bytes()

    import experiments.trade_monitor_clock_probe.clock_probe as module

    monkeypatch.setattr(module, "HERE", experiment_dir)
    runtime = experiment_dir / ".runtime"
    manifest = snapshot_prompt(automation, runtime, (automation, protected))

    assert protected.read_bytes() == source_before
    assert (runtime / "prompt_snapshot.txt").read_text(encoding="utf-8") == "固定規則"
    assert manifest["prompt_sha256"] == sha256_bytes("固定規則".encode("utf-8"))
    ok, changes = verify_manifest(runtime / "protected_manifest.json")
    assert ok is True
    assert changes == []


def test_verify_detects_protected_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    experiment_dir = tmp_path / "experiment"
    experiment_dir.mkdir()
    automation = tmp_path / "automation.toml"
    protected = tmp_path / "protected.py"
    automation.write_text('prompt = "固定規則"\n', encoding="utf-8")
    protected.write_text("before\n", encoding="utf-8")

    import experiments.trade_monitor_clock_probe.clock_probe as module

    monkeypatch.setattr(module, "HERE", experiment_dir)
    runtime = experiment_dir / ".runtime"
    snapshot_prompt(automation, runtime, (automation, protected))
    protected.write_text("after\n", encoding="utf-8")

    ok, changes = verify_manifest(runtime / "protected_manifest.json")
    assert ok is False
    assert any(change["path"] == str(protected) for change in changes)


def test_next_minute_slot_uses_requested_second() -> None:
    now = datetime.fromisoformat("2026-09-01T19:12:07+08:00")
    assert next_minute_slot(now, 5) == datetime.fromisoformat("2026-09-01T19:13:05+08:00")


def test_analysis_adapter_keeps_original_prompt_exact_and_disables_side_effects() -> None:
    original = "原始規則\n第二行"
    digest = sha256_bytes(original.encode("utf-8"))
    effective = build_isolated_analysis_prompt(original, digest)

    assert f"<ORIGINAL_PROMPT>\n{original}\n</ORIGINAL_PROMPT>" in effective
    assert digest in effective
    assert "不得呼叫任何工具" in effective
    assert "不得傳送任何訊息" in effective


def test_capture_time_anchor_uses_previous_full_minute() -> None:
    captured = parse_capture_time("2026-09-01T23:11:26+08:00")
    assert expected_latest_closed_k(captured).isoformat() == "2026-09-01T23:10:00+08:00"


def test_parse_capture_time_requires_timezone() -> None:
    with pytest.raises(ValueError, match="UTC offset"):
        parse_capture_time("2026-09-01T23:11:26")


def test_capture_decision_prevents_duplicate_bar(tmp_path: Path) -> None:
    image = tmp_path / "chart.png"
    image.write_bytes(b"chart-1")
    first_time = parse_capture_time("2026-09-01T23:11:05+08:00")
    first = decide_capture(
        image_path=image,
        captured_at=first_time,
        previous_state={},
        stale_after_seconds=90,
    )
    completed = commit_completed_capture(first.next_state, first.context)
    duplicate = decide_capture(
        image_path=image,
        captured_at=first_time + timedelta(seconds=30),
        previous_state=completed,
        stale_after_seconds=90,
    )
    assert first.status == "FRESH"
    assert duplicate.status == "SAME_BAR"


def test_capture_decision_detects_stale_identical_chart(tmp_path: Path) -> None:
    image = tmp_path / "chart.png"
    image.write_bytes(b"frozen")
    first_time = parse_capture_time("2026-09-01T23:11:05+08:00")
    first = decide_capture(
        image_path=image,
        captured_at=first_time,
        previous_state={},
        stale_after_seconds=90,
    )
    completed = commit_completed_capture(first.next_state, first.context)
    stale = decide_capture(
        image_path=image,
        captured_at=first_time + timedelta(minutes=2),
        previous_state=completed,
        stale_after_seconds=90,
    )
    assert stale.status == "STALE"
    assert stale.context["image_unchanged_seconds"] == 120.0


def test_capture_decision_detects_time_regression(tmp_path: Path) -> None:
    image = tmp_path / "chart.png"
    image.write_bytes(b"new")
    state = {"last_completed_closed_k": "2026-09-01T23:10:00+08:00"}
    decision = decide_capture(
        image_path=image,
        captured_at=parse_capture_time("2026-09-01T23:10:05+08:00"),
        previous_state=state,
        stale_after_seconds=90,
    )
    assert decision.status == "REGRESSION"


def test_structured_prompt_keeps_original_exact_and_anchors_time() -> None:
    original = "原始規則\n第二行"
    digest = sha256_bytes(original.encode("utf-8"))
    context = {
        "expected_latest_closed_k_hhmm": "23:10",
        "current_unclosed_k_hhmm": "23:11",
    }
    effective = build_structured_analysis_prompt(original, digest, context)
    assert f"<ORIGINAL_PROMPT>\n{original}\n</ORIGINAL_PROMPT>" in effective
    assert '"expected_latest_closed_k_hhmm": "23:10"' in effective
    assert "不得從圖面猜測或改寫" in effective
    assert "最終只回傳符合 output schema 的 JSON" in effective


def test_renderer_has_exact_nine_headings_and_fixed_order() -> None:
    context = {
        "expected_latest_closed_k_hhmm": "23:10",
        "current_unclosed_k_hhmm": "23:11",
    }
    message = render_analysis_markdown(sample_analysis_payload(), context)
    headings = [
        "**時間／最新已收盤 K**",
        "**大趨勢**",
        "**當前趨勢**",
        "**市場狀態**",
        "**觀察型態與狀態**",
        "**尚缺條件／觸發**",
        "**進場與結構停損**",
        "**1R與最近障礙**",
        "**單口管理／禁止原因**",
    ]
    assert [line for line in message.splitlines() if line in headings] == headings
    assert message.count("\n**") == 8
    assert "- 23:10；收盤約 46,820～46,825（圖面估計）。" in message
    assert "- 23:11 為未收盤即時 K，只供觀察。" in message


def test_renderer_removes_model_markdown_and_newlines() -> None:
    payload = sample_analysis_payload()
    payload["market_state"] = ["**高檔回檔**\n- 不追價"]
    message = render_analysis_markdown(payload, {
        "expected_latest_closed_k_hhmm": "23:10",
        "current_unclosed_k_hhmm": "23:11",
    })
    assert "- 高檔回檔 - 不追價" in message
    assert "****" not in message


def test_renderer_removes_repeated_timestamp_and_disclaimer() -> None:
    payload = sample_analysis_payload()
    payload["latest_closed_k_price_estimate"] = (
        "2026-09-01 23:10（台北時間）；收盤約 46,820～46,825（圖面估計）。"
    )
    payload["risk_and_nearest_obstacle"] = [
        "最近障礙約46,850（圖面估計）。",
        "一般技術分析，非個人化投資建議；畫面可能延遲。",
    ]
    message = render_analysis_markdown(payload, {
        "expected_latest_closed_k_hhmm": "23:10",
        "current_unclosed_k_hhmm": "23:11",
    })
    assert message.count("23:10") == 1
    assert message.count("一般技術分析") == 1


def test_validation_rejects_extra_or_missing_fields() -> None:
    payload = sample_analysis_payload()
    payload["unexpected"] = "x"
    with pytest.raises(AnalysisValidationError, match="invalid top-level fields"):
        validate_analysis_payload(payload)


def test_unavailable_message_keeps_same_nine_section_layout() -> None:
    context = {
        "expected_latest_closed_k_hhmm": "23:10",
        "current_unclosed_k_hhmm": "23:11",
    }
    message = render_unavailable_markdown(context, "圖表疑似停滯。")
    assert message.count("\n**") == 8
    assert "**禁止依本輪資料建立真實或模擬持倉。**" in message
