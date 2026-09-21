from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import trade_monitor.local_scheduler as scheduler
from trade_monitor import dual_scale


TAIPEI = ZoneInfo("Asia/Taipei")


def _make_config(
    tmp_path: Path,
    *,
    prompt: str = "monitor rules",
    market_structure_state_enabled: bool = False,
) -> scheduler.SchedulerConfig:
    prompt_path = tmp_path / "prompt.md"
    schema_path = tmp_path / "schema.json"
    capture_script = tmp_path / "capture.ps1"
    prompt_path.write_text(prompt, encoding="utf-8")
    schema_path.write_text("{}", encoding="utf-8")
    capture_script.write_text("# capture", encoding="utf-8")
    return scheduler.SchedulerConfig(
        project_root=tmp_path,
        automation_id="1-k",
        align_second=5,
        analysis_timeout_seconds=50,
        keep_capture_count=2,
        prompt_version="test",
        prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        prompt_path=prompt_path,
        schema_path=schema_path,
        capture_script_path=capture_script,
        capture_options={
            "crop_x": 8,
            "crop_y": 92,
            "crop_width": 1390,
            "crop_height": 940,
            "minimum_window_width": 1500,
            "minimum_window_height": 1000,
            "minimum_dark_ratio": 0.55,
            "minimum_signal_samples": 12,
        },
        market_structure_state_enabled=market_structure_state_enabled,
    )


def test_next_minute_slot_aligns_to_fifth_second() -> None:
    now = datetime(2026, 9, 2, 10, 3, 4, 900000, tzinfo=TAIPEI)
    assert scheduler.next_minute_slot(now, 5) == datetime(2026, 9, 2, 10, 3, 5, tzinfo=TAIPEI)

    after_slot = datetime(2026, 9, 2, 10, 3, 5, tzinfo=TAIPEI)
    assert scheduler.next_minute_slot(after_slot, 5) == datetime(2026, 9, 2, 10, 4, 5, tzinfo=TAIPEI)


def test_operational_failure_message_uses_chinese_label_not_internal_code() -> None:
    message = scheduler._render_operational_failure(
        datetime(2026, 9, 2, 10, 3, tzinfo=TAIPEI),
        "codex_timeout",
    )
    assert "分析程序逾時" in message
    assert "codex_timeout" not in message
    assert "一倍初始風險與最近障礙" in message


def test_capture_chart_uses_only_returned_cropped_file(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    runtime = tmp_path / "runtime"

    def fake_runner(command: list[str] | tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
        output_path = Path(command[command.index("-OutputPath") + 1])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"png")
        payload = {
            "ok": True,
            "status": "captured",
            "path": str(output_path.resolve()),
            "captured_at": "2026-09-02T10:03:05+08:00",
            "width": 1390,
            "height": 940,
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    result = scheduler.capture_chart(
        config,
        runtime,
        now=datetime(2026, 9, 2, 10, 3, 5, tzinfo=TAIPEI),
        command_runner=fake_runner,
    )

    assert Path(result["path"]).read_bytes() == b"png"
    assert Path(result["path"]).parent == (runtime / "captures").resolve()


def test_build_analysis_prompt_keeps_rules_and_runtime_context() -> None:
    prepare = {
        "context": {"expected_latest_closed_k_iso": "2026-09-02T10:02:00+08:00"},
        "previous_analysis_summary": {"large_trend": "強勢偏空"},
        "constitution_state": {"trading_locked": False},
    }

    prompt = scheduler.build_analysis_prompt(
        monitor_rules="ORIGINAL RULES",
        prompt_sha256="abc",
        prepare_payload=prepare,
    )

    assert "ORIGINAL RULES" in prompt
    assert "2026-09-02T10:02:00+08:00" in prompt
    assert "前次摘要與交易憲法狀態的唯一權威來源" in prompt
    assert "最終只回傳符合 output schema" in prompt
    assert "latest_closed_k_details 不得包含任何日期或 HH:MM 時間" in prompt
    assert "資料框時間與 context.expected_latest_closed_k_hhmm 完全一致" in prompt
    assert "必須把整個資料框視為無效舊資訊" in prompt
    assert "至少兩個清楚可見、分屬不同垂直位置的價位標記" in prompt
    assert "latest_closed_k_price_estimate 必須寫「點位無法可靠估計」" in prompt
    assert "不通過就撤回數值而不是沿用前一輪或舊資料框" in prompt


def test_build_analysis_prompt_includes_market_structure_only_when_enabled() -> None:
    prepare = {
        "context": {"expected_latest_closed_k_iso": "2026-09-02T10:02:00+08:00"},
        "market_structure_state": {
            "state_version": 1,
            "as_of": "2026-09-02T10:01:00+08:00",
            "session_key": "2026-09-02-DAY",
        },
    }

    disabled = scheduler.build_analysis_prompt(
        monitor_rules="ORIGINAL RULES",
        prompt_sha256="abc",
        prepare_payload=prepare,
    )
    enabled = scheduler.build_analysis_prompt(
        monitor_rules="ORIGINAL RULES",
        prompt_sha256="abc",
        prepare_payload=prepare,
        market_structure_state_enabled=True,
    )

    assert '"market_structure_state"' not in disabled
    assert '"market_structure_state"' in enabled
    assert "保留 pivot_id" in enabled
    assert "大小級多空四向防線" in enabled
    assert "scenario_context" in enabled
    assert "scenario_context.locations 若包含 UNDEFINED" in enabled
    assert "first_seen_at 必須是本輪" in enabled
    assert "點位區使用 null、UNAVAILABLE" in enabled
    assert "現在首次建檔可見歷史結構" in enabled
    assert "已確認三腳／多波創低" in enabled
    assert "不得因大級尚未確認就把小級一併清空" in enabled


def test_v6_runtime_prompt_states_cross_field_output_constraints() -> None:
    prepare = {
        "context": {"expected_latest_closed_k_iso": "2026-09-03T09:11:00+08:00"},
        "market_structure_state": {"version": 6},
    }

    prompt = scheduler.build_analysis_prompt(
        monitor_rules="ORIGINAL RULES",
        prompt_sha256="abc",
        prepare_payload=prepare,
        market_structure_state_enabled=True,
        market_structure_state_version=6,
        unified_lens_selection_enabled=True,
    )

    assert "其他 stage 必須填 NOT_APPLICABLE" in prompt
    assert "OPENING_EVIDENCE_ONLY 只可用於" in prompt
    assert "opportunity_context.mapped_pattern 必須填 NONE" in prompt
    assert "pattern_observation.pattern 也不得輸出 YIZHI_CENTRIFUGAL" in prompt
    assert "active_sequence=NONE 時 structural_assessment 必須為 UNDEFINED" in prompt
    assert "只有 REPLACED 才可填 replaced_by" in prompt
    assert "若建立新的 hypothesis_id" in prompt


def test_build_analysis_prompt_separates_fresh_overview_from_detail_trigger() -> None:
    prepare = {
        "context": {"expected_latest_closed_k_iso": "2026-09-02T10:16:00+08:00"},
    }
    overview = {
        "ok": True,
        "status": "FRESH",
        "context": {
            "source": "CHROME_CONTROL_OVERVIEW",
            "large_trend": "強勢偏空",
        },
    }

    prompt = scheduler.build_analysis_prompt(
        monitor_rules="ORIGINAL RULES",
        prompt_sha256="abc",
        prepare_payload=prepare,
        dual_scale_context=overview,
    )

    assert '"dual_scale_context"' in prompt
    assert "約 180 根 K 的 DETAIL 細節圖" in prompt
    assert "不得觸發進場" in prompt
    assert "STALE、UNAVAILABLE、INVALID" in prompt


def test_build_analysis_prompt_uses_fresh_structured_data_as_numeric_authority() -> None:
    prepare = {"context": {"expected_latest_closed_k_iso": "2026-09-02T10:16:00+08:00"}}
    data = {
        "ok": True,
        "status": "FRESH",
        "provenance": {"source_latest_closed_k_iso": "2026-09-02T10:16:00+08:00"},
        "latest_closed_k": {"close": 46250},
    }
    prompt = scheduler.build_analysis_prompt(
        monitor_rules="ORIGINAL RULES",
        prompt_sha256="abc",
        prepare_payload=prepare,
        structured_market_data_context=data,
    )
    assert '"structured_market_data_context"' in prompt
    assert "才是價格、OHLC" in prompt
    assert "兩者衝突時撤回進場點位" in prompt
    assert "不得假裝已有即時行情介接" in prompt


def test_load_scheduler_config_supports_explicit_project_root_and_state_flag(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    version_dir = project_root / "trade_monitor" / "rules" / "versions" / "rc"
    version_dir.mkdir(parents=True)
    (project_root / "trade_monitor" / "schemas").mkdir(parents=True)
    (project_root / "trade_monitor" / "prompt.md").write_text("rules", encoding="utf-8")
    (project_root / "trade_monitor" / "schemas" / "analysis-v3.json").write_text("{}", encoding="utf-8")
    (project_root / "trade_monitor" / "chart_capture.ps1").write_text("# capture", encoding="utf-8")
    config_path = version_dir / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "project_root": "../../../..",
                "automation_id": "1-k",
                "align_second": 5,
                "analysis_timeout_seconds": 50,
                "keep_capture_count": 5,
                "prompt_version": "rc",
                "prompt_sha256": hashlib.sha256(b"rules").hexdigest(),
                "prompt_path": "trade_monitor/prompt.md",
                "schema_path": "trade_monitor/schemas/analysis-v3.json",
                "capture_script_path": "trade_monitor/chart_capture.ps1",
                "market_structure_state_enabled": True,
                "market_structure_state_version": 3,
                "dual_scale": {
                    "enabled": True,
                    "state_path": ".runtime/trade_monitor/dual_scale_state.json",
                    "overview_max_age_minutes": 20,
                },
                "capture": {
                    "crop_x": 8,
                    "crop_y": 92,
                    "crop_width": 1390,
                    "crop_height": 940,
                    "minimum_window_width": 1500,
                    "minimum_window_height": 1000,
                    "minimum_dark_ratio": 0.55,
                    "minimum_signal_samples": 12,
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = scheduler.load_scheduler_config(config_path)

    assert loaded.project_root == project_root.resolve()
    assert loaded.market_structure_state_enabled is True
    assert loaded.market_structure_state_version == 3
    assert loaded.dual_scale_enabled is True
    assert loaded.dual_scale_state_path == (project_root / ".runtime/trade_monitor/dual_scale_state.json").resolve()
    assert loaded.dual_scale_overview_max_age_minutes == 20
    assert loaded.structured_market_data_enabled is False


def test_load_scheduler_config_defaults_state_flag_off(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    config_dir = project_root / "trade_monitor"
    config_dir.mkdir(parents=True)
    (config_dir / "prompt.md").write_text("rules", encoding="utf-8")
    (config_dir / "schema.json").write_text("{}", encoding="utf-8")
    (config_dir / "capture.ps1").write_text("# capture", encoding="utf-8")
    config_path = config_dir / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "automation_id": "1-k",
                "align_second": 5,
                "analysis_timeout_seconds": 50,
                "keep_capture_count": 5,
                "prompt_version": "production-compatible",
                "prompt_sha256": hashlib.sha256(b"rules").hexdigest(),
                "prompt_path": "trade_monitor/prompt.md",
                "schema_path": "trade_monitor/schema.json",
                "capture_script_path": "trade_monitor/capture.ps1",
                "capture": {
                    "crop_x": 8,
                    "crop_y": 92,
                    "crop_width": 1390,
                    "crop_height": 940,
                    "minimum_window_width": 1500,
                    "minimum_window_height": 1000,
                    "minimum_dark_ratio": 0.55,
                    "minimum_signal_samples": 12,
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = scheduler.load_scheduler_config(config_path)

    assert loaded.project_root == project_root.resolve()
    assert loaded.market_structure_state_enabled is False
    assert loaded.market_structure_state_version == 2
    assert loaded.dual_scale_enabled is False
    assert loaded.dual_scale_state_path is None
    assert loaded.structured_market_data_enabled is False
    assert loaded.structured_market_data_source_path is None


def test_load_scheduler_config_discovers_project_root_from_version_directory(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    version_dir = project_root / "trade_monitor" / "rules" / "versions" / "candidate"
    version_dir.mkdir(parents=True)
    (project_root / "trade_monitor" / "schemas").mkdir(parents=True)
    (project_root / "trade_monitor" / "prompt.md").write_text("rules", encoding="utf-8")
    (project_root / "trade_monitor" / "schemas" / "analysis-v3.json").write_text("{}", encoding="utf-8")
    (project_root / "trade_monitor" / "chart_capture.ps1").write_text("# capture", encoding="utf-8")
    config_path = version_dir / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "automation_id": "1-k",
                "align_second": 5,
                "analysis_timeout_seconds": 50,
                "keep_capture_count": 5,
                "prompt_version": "candidate",
                "prompt_sha256": hashlib.sha256(b"rules").hexdigest(),
                "prompt_path": "trade_monitor/prompt.md",
                "schema_path": "trade_monitor/schemas/analysis-v3.json",
                "capture_script_path": "trade_monitor/chart_capture.ps1",
                "market_structure_state_enabled": True,
                "capture": {
                    "crop_x": 8,
                    "crop_y": 92,
                    "crop_width": 1390,
                    "crop_height": 940,
                    "minimum_window_width": 1500,
                    "minimum_window_height": 1000,
                    "minimum_dark_ratio": 0.55,
                    "minimum_signal_samples": 12,
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = scheduler.load_scheduler_config(config_path)

    assert loaded.project_root == project_root.resolve()
    assert loaded.market_structure_state_enabled is True
    assert loaded.market_structure_state_version == 2


def test_v3_analysis_prompt_requires_causal_anchor_context() -> None:
    prepare_payload = {
        "context": {"expected_latest_closed_k_iso": "2026-09-02T10:00:00+08:00"},
        "previous_analysis_summary": None,
        "constitution_state": {"ok": True},
        "market_structure_state": {"version": 3, "anchor_context": {}},
    }

    v2 = scheduler.build_analysis_prompt(
        monitor_rules="rules",
        prompt_sha256="abc",
        prepare_payload=prepare_payload,
        market_structure_state_enabled=True,
        market_structure_state_version=2,
    )
    v3 = scheduler.build_analysis_prompt(
        monitor_rules="rules",
        prompt_sha256="abc",
        prepare_payload=prepare_payload,
        market_structure_state_enabled=True,
        market_structure_state_version=3,
    )

    assert "anchor_id、起點與首次看見時間不得重寫" not in v2
    assert "anchor_id、起點與首次看見時間不得重寫" in v3
    assert "scenario_context.setup.stage 必須為 NONE" not in v2
    assert "scenario_context.setup.stage 必須為 NONE" in v3
    assert "NO_CHASE 只可用於已辨識且仍在追蹤的既有型態" in v3


def test_v4_analysis_prompt_requires_complete_cclass_context_without_parallel_setup() -> None:
    prepare_payload = {
        "context": {"expected_latest_closed_k_iso": "2026-09-02T10:00:00+08:00"},
        "previous_analysis_summary": None,
        "constitution_state": {"ok": True},
        "market_structure_state": {
            "version": 4,
            "anchor_context": {},
            "cclass_context": {},
        },
    }

    v4 = scheduler.build_analysis_prompt(
        monitor_rules="rules",
        prompt_sha256="abc",
        prepare_payload=prepare_payload,
        market_structure_state_enabled=True,
        market_structure_state_version=4,
    )

    assert '"cclass_context"' in v4
    assert "太極保存朝代、定錨、1～5 段與父子關係" in v4
    assert "幅度、時間、斜率、乾淨度及破壞性" in v4
    assert "離心力、一條龍、生死門" in v4
    assert "不得另產生平行交易答案" in v4
    assert "不得把三個動能階段新增為第五種型態" in v4
    assert "複製失敗或動能失效只能先降級／中立" in v4


def test_v5_formal_analysis_prompt_keeps_existing_four_leg_gate() -> None:
    prepare_payload = {
        "context": {"expected_latest_closed_k_iso": "2026-09-02T10:00:00+08:00"},
        "previous_analysis_summary": None,
        "constitution_state": {"ok": True},
        "market_structure_state": {
            "version": 5,
            "anchor_context": {},
            "cclass_context": {},
            "decision_chain_context": {},
        },
    }

    v5 = scheduler.build_analysis_prompt(
        monitor_rules="rules",
        prompt_sha256="abc",
        prepare_payload=prepare_payload,
        market_structure_state_enabled=True,
        market_structure_state_version=5,
    )

    assert '"decision_chain_context"' in v5
    assert "X 只是一條決策鏈，不是第五種型態" in v5
    assert "第一次 DH／DL 樣本" in v5
    assert "少於四腳不得使用 QUADRANT_PRIMARY" in v5
    assert "mapped_pattern 必須等於既有 scenario setup" in v5
    assert "expected_behavior" in v5
    assert "1～10 根 max_wait_bars" in v5


def test_v5_unified_candidate_uses_clarity_after_two_legs() -> None:
    prepare_payload = {
        "context": {"expected_latest_closed_k_iso": "2026-09-02T10:00:00+08:00"},
        "market_structure_state": {
            "version": 5,
            "anchor_context": {},
            "cclass_context": {},
            "decision_chain_context": {},
        },
    }
    prompt = scheduler.build_analysis_prompt(
        monitor_rules="rules",
        prompt_sha256="abc",
        prepare_payload=prepare_payload,
        market_structure_state_enabled=True,
        market_structure_state_version=5,
        unified_lens_selection_enabled=True,
    )
    assert "整合決策流程只是一條決策鏈，不是第五種型態" in prompt
    assert "第一次當日高點／當日低點樣本" in prompt
    assert "不得再以「四腳」作為四象限主判讀的硬門檻" in prompt
    assert "兩腳以上必須依結構清晰度選擇唯一主判讀工具" in prompt
    assert "少於四腳不得使用 QUADRANT_PRIMARY" not in prompt


def test_v6_uses_all_course_methods_and_strategy_native_execution() -> None:
    prepare_payload = {
        "context": {"expected_latest_closed_k_iso": "2026-09-03T10:00:00+08:00"},
        "market_structure_state": {
            "version": 6,
            "anchor_context": {},
            "cclass_context": {},
            "decision_chain_context": {},
            "prospective_context": {},
        },
    }
    prompt = scheduler.build_analysis_prompt(
        monitor_rules="rules",
        prompt_sha256="abc",
        prepare_payload=prepare_payload,
        market_structure_state_enabled=True,
        market_structure_state_version=6,
        unified_lens_selection_enabled=True,
    )

    assert "不限四型態" in prompt
    assert "唯一選定的主控戰法" in prompt
    assert "不得強制映射原四型態" in prompt
    assert "taiji_evolution" in prompt
    assert "複製／修正成功失敗序列" in prompt


def test_run_codex_analysis_verifies_hash_and_reads_json(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    image = tmp_path / "chart.png"
    image.write_bytes(b"png")

    def fake_runner(command: list[str] | tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text('{"original_decision":"DONT_NOTIFY"}', encoding="utf-8")
        assert "monitor rules" in str(kwargs["input"])
        return subprocess.CompletedProcess(command, 0, "", "")

    original_resolver = scheduler.resolve_codex_executable
    scheduler.resolve_codex_executable = lambda: Path("codex")
    try:
        payload, metrics = scheduler.run_codex_analysis(
            config,
            image_path=image,
            prepare_payload={"context_id": "context-1", "context": {}},
            runtime_root=tmp_path / "runtime",
            command_runner=fake_runner,
        )
    finally:
        scheduler.resolve_codex_executable = original_resolver

    assert payload["original_decision"] == "DONT_NOTIFY"
    assert metrics["prompt_sha256"] == config.prompt_sha256


def test_terminal_anchor_history_is_retained_without_masking_a_missing_active_anchor() -> None:
    previous = {
        "session_key": "2026-09-03-DAY",
        "anchor_context": {
            "anchors": [
                {"anchor_id": "A-OLD", "status": "INVALIDATED", "notes": ["history"]},
                {"anchor_id": "A-ACTIVE", "status": "CONFIRMED", "notes": ["active"]},
            ]
        },
    }
    current = {
        "session_key": "2026-09-03-DAY",
        "anchor_context": {
            "anchors": [
                {"anchor_id": "A-NEW", "status": "FORMING", "notes": ["new"]},
            ]
        },
    }

    retained = scheduler._retain_terminal_anchor_history(previous, current)

    assert [item["anchor_id"] for item in retained["anchor_context"]["anchors"]] == ["A-OLD", "A-NEW"]
    assert all(item["anchor_id"] != "A-ACTIVE" for item in retained["anchor_context"]["anchors"])
    assert current["anchor_context"]["anchors"] == [
        {"anchor_id": "A-NEW", "status": "FORMING", "notes": ["new"]}
    ]


def test_run_codex_analysis_retries_once_for_defense_consistency(tmp_path: Path) -> None:
    config = _make_config(tmp_path, market_structure_state_enabled=True)
    image = tmp_path / "chart.png"
    image.write_bytes(b"png")
    example_path = Path(__file__).parents[2] / "trade_monitor/examples/valid-analysis-v8.json"
    valid = json.loads(example_path.read_text(encoding="utf-8"))
    invalid = json.loads(example_path.read_text(encoding="utf-8"))
    invalid["large_trend"] = {"classification": "強勢偏空", "details": ["大級持續向下。"]}
    invalid["current_trend"] = {"classification": "偏空", "details": ["小級持續向下。"]}
    invalid["market_structure_state"]["dow_state_small"] = "UNDEFINED"
    invalid["market_structure_state"]["defense_lines"]["small_bear"] = None
    attempts: list[str] = []

    def fake_runner(command: list[str] | tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        attempts.append(str(kwargs["input"]))
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(invalid if len(attempts) == 1 else valid, ensure_ascii=False),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    original_resolver = scheduler.resolve_codex_executable
    scheduler.resolve_codex_executable = lambda: Path("codex")
    try:
        payload, metrics = scheduler.run_codex_analysis(
            config,
            image_path=image,
            prepare_payload={"context_id": "context-1", "context": {}},
            runtime_root=tmp_path / "runtime",
            command_runner=fake_runner,
        )
    finally:
        scheduler.resolve_codex_executable = original_resolver

    assert payload == valid
    assert metrics["attempts"] == 2
    assert "VALIDATION_CORRECTION" not in attempts[0]
    assert "VALIDATION_CORRECTION" in attempts[1]


def test_run_codex_analysis_retries_transition_lifecycle_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _make_config(tmp_path, market_structure_state_enabled=True)
    image = tmp_path / "chart.png"
    image.write_bytes(b"png")
    example_path = Path(__file__).parents[2] / "trade_monitor/examples/valid-analysis-v8.json"
    valid = json.loads(example_path.read_text(encoding="utf-8"))
    candidate_state = valid["market_structure_state"]
    attempts: list[str] = []
    transition_calls = 0

    def fake_runner(command: list[str] | tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        attempts.append(str(kwargs["input"]))
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(json.dumps(valid, ensure_ascii=False), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    def fake_transition(previous: object, current: object, *, expected_as_of: str) -> dict:
        nonlocal transition_calls
        transition_calls += 1
        assert previous == candidate_state
        assert expected_as_of == candidate_state["as_of"]
        if transition_calls == 1:
            raise scheduler.MarketStructureStateError(
                "active setup must terminate before a new setup replaces it"
            )
        return dict(candidate_state)

    monkeypatch.setattr(scheduler, "resolve_codex_executable", lambda: Path("codex"))
    monkeypatch.setattr(scheduler, "validate_market_structure_transition", fake_transition)

    payload, metrics = scheduler.run_codex_analysis(
        config,
        image_path=image,
        prepare_payload={
            "context_id": "context-transition",
            "context": {"expected_latest_closed_k_iso": candidate_state["as_of"]},
            "market_structure_state": candidate_state,
        },
        runtime_root=tmp_path / "runtime",
        command_runner=fake_runner,
    )

    assert payload["market_structure_state"] == candidate_state
    assert metrics["attempts"] == 2
    assert transition_calls == 2
    assert "active setup must terminate" in attempts[1]


def test_run_codex_analysis_fails_closed_on_prompt_change(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config.prompt_path.write_text("changed", encoding="utf-8")
    image = tmp_path / "chart.png"
    image.write_bytes(b"png")

    try:
        scheduler.run_codex_analysis(
            config,
            image_path=image,
            prepare_payload={"context_id": "context-1", "context": {}},
            runtime_root=tmp_path / "runtime",
        )
    except scheduler.LocalSchedulerError as exc:
        assert exc.code == "prompt_hash_mismatch"
    else:
        raise AssertionError("Expected prompt hash mismatch")


def test_resolve_codex_executable_uses_path(monkeypatch, tmp_path: Path) -> None:
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"exe")
    monkeypatch.setattr(scheduler.shutil, "which", lambda _name: str(executable))

    assert scheduler.resolve_codex_executable() == executable.resolve()


def test_run_monitor_once_publishes_finalized_canonical_message(tmp_path: Path, monkeypatch) -> None:
    config = _make_config(tmp_path)
    runtime = tmp_path / "runtime"
    automation_file = tmp_path / "automation.toml"
    automation_file.write_text('status = "ACTIVE"\n', encoding="utf-8")
    image = tmp_path / "chart.png"
    image.write_bytes(b"png")

    monkeypatch.setattr(
        scheduler,
        "capture_chart",
        lambda *_args, **_kwargs: {
            "path": str(image),
            "captured_at": "2026-09-02T10:03:05+08:00",
        },
    )
    monkeypatch.setattr(
        scheduler,
        "prepare_capture",
        lambda *_args, **_kwargs: {
            "context_id": "context-1",
            "requires_analysis": True,
            "context": {"expected_latest_closed_k_iso": "2026-09-02T10:02:00+08:00"},
        },
    )
    monkeypatch.setattr(
        scheduler,
        "run_codex_analysis",
        lambda *_args, **_kwargs: ({"payload": True}, {"duration_seconds": 1.25}),
    )

    async def fake_finalize(**_kwargs):
        return {
            "event_id": "event-1",
            "decision": "NOTIFY",
            "message": "same canonical message",
            "latest_closed_bar_time": "2026-09-02T10:02:00+08:00",
            "capture_status": "FRESH",
            "telegram_status": "dry_run",
        }

    monkeypatch.setattr(scheduler, "finalize_analysis", fake_finalize)

    result = asyncio.run(
        scheduler.run_monitor_once(
            config,
            runtime_root=runtime,
            automation_file=automation_file,
            telegram_config=tmp_path / "config.json",
            dry_run=True,
        )
    )

    assert result["status"] == "completed"
    latest = json.loads((runtime / "outbox" / "latest.json").read_text(encoding="utf-8"))
    assert latest["message"] == "same canonical message"
    assert latest["event_id"] == "event-1"


def test_run_monitor_once_supplies_only_fresh_chrome_overview_context(tmp_path: Path, monkeypatch) -> None:
    runtime = tmp_path / "runtime"
    state_path = runtime / "dual-scale.json"
    config = replace(
        _make_config(tmp_path),
        dual_scale_enabled=True,
        dual_scale_state_path=state_path,
        dual_scale_overview_max_age_minutes=20,
    )
    automation_file = tmp_path / "automation.toml"
    automation_file.write_text('status = "ACTIVE"\n', encoding="utf-8")
    image = tmp_path / "chart.png"
    image.write_bytes(b"png")

    dual_scale.restore_detail(
        state_path,
        owner="chrome-test",
        at=datetime(2026, 9, 2, 9, 59, 50, tzinfo=TAIPEI),
    )
    dual_scale.plan_overview(state_path, at=datetime(2026, 9, 2, 10, 0, tzinfo=TAIPEI))
    dual_scale.acquire_overview(
        state_path,
        owner="overview-1000",
        at=datetime(2026, 9, 2, 10, 0, 1, tzinfo=TAIPEI),
    )
    dual_scale.complete_overview(
        state_path,
        owner="overview-1000",
        at=datetime(2026, 9, 2, 10, 0, 4, tzinfo=TAIPEI),
        restored_detail=True,
        summary={
            "latest_closed_bar_time": "2026-09-02T09:59:00+08:00",
            "captured_at": "2026-09-02T10:00:03+08:00",
            "session_key": "2026-09-02-DAY",
            "large_trend": "強勢偏空",
            "wave_stage": "推進中段",
            "quadrant": "第四象限",
            "position": "位於大級空方趨勢的反彈壓力區",
            "large_defense_context": ["大級空方防線有效"],
            "key_zones": ["上方壓力區圖面估計"],
            "source": "CHROME_CONTROL_OVERVIEW",
        },
    )

    monkeypatch.setattr(
        scheduler,
        "capture_chart",
        lambda *_args, **_kwargs: {
            "path": str(image),
            "captured_at": "2026-09-02T10:01:05+08:00",
        },
    )
    monkeypatch.setattr(
        scheduler,
        "prepare_capture",
        lambda *_args, **_kwargs: {
            "context_id": "context-dual",
            "requires_analysis": True,
            "context": {"expected_latest_closed_k_iso": "2026-09-02T10:00:00+08:00"},
        },
    )

    def fake_analysis(*_args, **kwargs):
        overview = kwargs["dual_scale_context"]
        assert overview["status"] == "FRESH"
        assert overview["context"]["source"] == "CHROME_CONTROL_OVERVIEW"
        return {"payload": True}, {"duration_seconds": 0.5}

    monkeypatch.setattr(scheduler, "run_codex_analysis", fake_analysis)

    async def fake_finalize(**_kwargs):
        return {
            "event_id": "event-dual",
            "decision": "DONT_NOTIFY",
            "message": "same short canonical message",
            "latest_closed_bar_time": "2026-09-02T10:00:00+08:00",
            "capture_status": "FRESH",
            "telegram_status": "dry_run",
        }

    monkeypatch.setattr(scheduler, "finalize_analysis", fake_finalize)

    result = asyncio.run(
        scheduler.run_monitor_once(
            config,
            runtime_root=runtime,
            automation_file=automation_file,
            telegram_config=tmp_path / "config.json",
            dry_run=True,
        )
    )

    assert result["status"] == "completed"
    assert result["dual_scale_overview_status"] == "FRESH"


def test_paused_automation_does_not_capture(tmp_path: Path, monkeypatch) -> None:
    config = _make_config(tmp_path)
    automation_file = tmp_path / "automation.toml"
    automation_file.write_text('status = "PAUSED"\n', encoding="utf-8")
    monkeypatch.setattr(
        scheduler,
        "capture_chart",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("capture must not run")),
    )

    result = asyncio.run(
        scheduler.run_monitor_once(
            config,
            runtime_root=tmp_path / "runtime",
            automation_file=automation_file,
            dry_run=True,
        )
    )

    assert result["status"] == "paused"
