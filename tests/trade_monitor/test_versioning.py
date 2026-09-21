from __future__ import annotations

import io
import hashlib
import json
from pathlib import Path

from trade_monitor import versioning


ACTIVE_VERSION_ID = "enlightenment-integrated-v2.1.8-anchor-origin"
ACTIVE_PROMPT_VERSION = "enlightenment-integrated-v2.1.8-anchor-origin-analysis"
ACTIVE_RESTORE_POINT = "enlightenment-integrated-v2.1.7-context-consistency"


def test_all_manifest_versions_verify() -> None:
    manifest = versioning.load_manifest()

    results = [versioning.verify_version(item["version_id"]) for item in manifest["versions"]]

    assert results
    assert all(result["ok"] for result in results)


def test_v19_xprocess_rc_is_integrated_isolated_and_preserved_after_formal_promotion() -> None:
    root = Path(versioning.RULES_ROOT)
    rc_dir = root / "versions/enlightenment-integrated-v1.9-xprocess-rc-shadow"
    execution_prompt = (rc_dir / "prompt.md").read_text(encoding="utf-8")
    analysis_prompt = (rc_dir / "analysis-prompt.md").read_text(encoding="utf-8")
    schema = json.loads((rc_dir / "analysis-schema.json").read_text(encoding="utf-8"))
    config = json.loads((rc_dir / "local_scheduler_config.json").read_text(encoding="utf-8"))
    marker = "# 台指期 1 分 K 趨勢與交易機會監控｜完整課程 X 流程整合 RC Shadow 分析規則"

    assert execution_prompt[execution_prompt.index(marker) :] == analysis_prompt
    required = (
        "前一現貨收盤 → 當日現貨開盤跳空",
        "cash_gap_source",
        "VERIFIED_CASH",
        "FUTURES_PROXY",
        "第一次 DH／DL",
        "TAIJI_PRIMARY",
        "QUADRANT_PRIMARY",
        "COMBINED_CONFIRMATION",
        "YIZHI_OVERRIDE",
        "家族 DNA",
        "SAME_QUADRANT",
        "SMALL_Q1",
        "COPY_CORRECTION",
        "A_CANDIDATE",
        "expected_behavior",
        "max_wait_bars",
        "behavior_invalidation",
        "不增加第五種型態",
        "不新增第十欄",
    )
    assert all(item in analysis_prompt for item in required)
    assert analysis_prompt.count("### 7.") == 4
    assert schema["$id"] == "trade-monitor-analysis-v7"
    assert schema["properties"]["market_structure_state"]["properties"]["version"]["const"] == 5
    assert "decision_chain_context" in schema["properties"]["market_structure_state"]["required"]
    assert config["market_structure_state_version"] == 5
    assert "xprocess_v19_shadow" in config["dual_scale"]["state_path"]
    assert config["prompt_sha256"] == hashlib.sha256(analysis_prompt.encode("utf-8")).hexdigest()

    active = json.loads((root / "active-version.json").read_text(encoding="utf-8"))
    formal_config = json.loads((root.parent / "local_scheduler_config.json").read_text(encoding="utf-8"))
    assert active["version_id"] == ACTIVE_VERSION_ID
    assert active["automation_status"] in {"PAUSED", "ACTIVE"}
    assert formal_config["prompt_version"] == ACTIVE_PROMPT_VERSION
    assert formal_config["market_structure_state_version"] == 6


def test_v19_xprocess_production_artifacts_are_self_contained_and_version_locked() -> None:
    root = Path(versioning.RULES_ROOT)
    release_dir = root / "versions/enlightenment-integrated-v1.9-xprocess"
    execution_prompt = (release_dir / "prompt.md").read_text(encoding="utf-8")
    analysis_prompt = (release_dir / "analysis-prompt.md").read_text(encoding="utf-8")
    schema = json.loads((release_dir / "analysis-schema.json").read_text(encoding="utf-8"))
    config = json.loads((release_dir / "local_scheduler_config.json").read_text(encoding="utf-8"))
    marker = "# 台指期 1 分 K 趨勢與交易機會監控｜完整課程 X 流程整合正式分析規則"

    assert execution_prompt[execution_prompt.index(marker) :] == analysis_prompt
    assert "enlightenment-integrated-v1.9-xprocess-analysis" in execution_prompt
    assert "X 流程整合正式執行層" in execution_prompt
    assert "RC Shadow" not in execution_prompt
    assert "decision_chain_context" in analysis_prompt
    assert analysis_prompt.count("### 7.") == 4
    assert schema["$id"] == "trade-monitor-analysis-v7"
    assert schema["properties"]["market_structure_state"]["properties"]["version"]["const"] == 5
    assert config["market_structure_state_version"] == 5
    assert config["prompt_version"] == "enlightenment-integrated-v1.9-xprocess-analysis"
    assert config["prompt_sha256"] == hashlib.sha256(analysis_prompt.encode("utf-8")).hexdigest()
    assert config["dual_scale"]["state_path"] == ".runtime/trade_monitor/dual_scale_state.json"


def test_v20_unified_production_artifacts_are_self_contained_and_preserved() -> None:
    root = Path(versioning.RULES_ROOT)
    release_dir = root / "versions/enlightenment-integrated-v2.0-unified"
    execution_prompt = (release_dir / "prompt.md").read_text(encoding="utf-8")
    analysis_prompt = (release_dir / "analysis-prompt.md").read_text(encoding="utf-8")
    schema = json.loads((release_dir / "analysis-schema.json").read_text(encoding="utf-8"))
    config = json.loads((release_dir / "local_scheduler_config.json").read_text(encoding="utf-8"))
    marker = "# 台指期 1 分 K 趨勢與交易機會監控｜完整課程統一交易系統正式分析規則"

    assert execution_prompt[execution_prompt.index(marker) :].rstrip("\n") == analysis_prompt.rstrip("\n")
    assert "完整課程統一交易系統正式執行層" in execution_prompt
    assert "enlightenment-integrated-v2.0-unified-analysis" in execution_prompt
    assert "本版只供隔離候選與影子驗證使用" not in execution_prompt
    assert "兩腳以上依結構清晰度選定單一主判讀工具" in analysis_prompt
    assert "第一類反轉" in analysis_prompt
    assert "第二類反轉" in analysis_prompt
    assert "第三類反轉／跨級急轉" in analysis_prompt
    assert "不得把任何內部代碼、英文縮寫或蛇形命名直接顯示給使用者" in analysis_prompt
    assert schema["$id"] == "trade-monitor-analysis-v7"
    assert schema["properties"]["market_structure_state"]["properties"]["version"]["const"] == 5
    assert config["prompt_version"] == "enlightenment-integrated-v2.0-unified-analysis"
    assert config["prompt_sha256"] == hashlib.sha256(analysis_prompt.encode("utf-8")).hexdigest()
    assert config["unified_lens_selection_enabled"] is True
    assert config["structured_market_data"]["enabled"] is False


def test_pre_xprocess_v18_restore_point_is_byte_identical_to_formal_v18() -> None:
    root = Path(versioning.RULES_ROOT)
    backup = root / "versions/pre-xprocess-v1.8-20260902"
    formal = root / "versions/enlightenment-integrated-v1.8-cclass"

    for name in ("prompt.md", "analysis-prompt.md", "analysis-schema.json", "local_scheduler_config.json"):
        assert (backup / name).read_bytes() == (formal / name).read_bytes()
    metadata = json.loads((backup / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["immutable"] is True
    assert metadata["automation_status"] == "PAUSED"


def test_compare_is_read_only_and_reports_different_hashes() -> None:
    result = versioning.compare_versions(
        "current-pre-enlightenment-20260902",
        "enlightenment-integrated-v1",
    )

    assert result["different"] is True
    assert result["versions"]["current-pre-enlightenment-20260902"]["verified"] is True
    assert result["versions"]["enlightenment-integrated-v1"]["verified"] is True


def test_cli_lists_versions_as_json() -> None:
    stdout = io.StringIO()

    code = versioning.main(["list"], stdout=stdout)
    payload = json.loads(stdout.getvalue())

    assert code == 0
    assert payload["ok"] is True
    assert "enlightenment-integrated-v1" in payload["versions"]


def test_v15_prompt_contains_symmetric_scenario_and_notification_contract() -> None:
    root = Path(versioning.RULES_ROOT)
    prompt = (root / "versions/enlightenment-integrated-v1.5-scenario-analysis/prompt.md").read_text(encoding="utf-8")
    required = [
        "多空完全鏡像",
        "小級多頭、小級空頭、大級多頭、大級空頭",
        "波段階段、位置、兩條路徑與級數一致性",
        "AGGRESSIVE_CONFIRMED",
        "CONSERVATIVE_CONFIRMED",
        "四個象限都必須監控並通知",
        "左右戰法必須加入通知情境",
        "trade_monitor/schemas/analysis-v4.json",
        "market_structure_state.version=2",
    ]
    assert all(item in prompt for item in required)
    assert "不增加第五種型態" in prompt


def test_v16_dual_scale_rc_is_chrome_only_and_current_release_is_active() -> None:
    root = Path(versioning.RULES_ROOT)
    version_dir = root / "versions/enlightenment-integrated-v1.6-dual-scale-rc"
    execution_prompt = (version_dir / "prompt.md").read_text(encoding="utf-8")
    analysis_prompt = (version_dir / "analysis-prompt.md").read_text(encoding="utf-8")
    marker = "# 台指期 1 分 K 趨勢與交易機會監控｜啟蒙交易＋道氏三兄弟正式分析規則"

    assert execution_prompt[execution_prompt.index(marker) :] == analysis_prompt
    assert "CHROME_CONTROL_OVERVIEW" in execution_prompt
    assert "不得使用 Computer Use" in execution_prompt
    assert "DETAIL" in analysis_prompt and "OVERVIEW" in analysis_prompt
    assert "不改四型態、停損、交易憲法、九欄或 TG 雙模式" in analysis_prompt
    assert "不增加第五種型態" in analysis_prompt

    active = json.loads((root / "active-version.json").read_text(encoding="utf-8"))
    formal_config = json.loads((root.parent / "local_scheduler_config.json").read_text(encoding="utf-8"))
    assert active["version_id"] == ACTIVE_VERSION_ID
    assert active["automation_status"] in {"PAUSED", "ACTIVE"}
    assert formal_config["prompt_version"] == ACTIVE_PROMPT_VERSION
    assert formal_config["dual_scale"]["enabled"] is True
    assert formal_config["dual_scale"]["controller"] == "CHROME_CONTROL_ONLY"


def test_v16_rc_does_not_modify_v15_trading_sections_or_schema() -> None:
    root = Path(versioning.RULES_ROOT)
    parent = (root / "versions/enlightenment-integrated-v1.5-scenario-analysis/prompt.md").read_text(
        encoding="utf-8"
    )
    rc_dir = root / "versions/enlightenment-integrated-v1.6-dual-scale-rc"
    rc = (rc_dir / "analysis-prompt.md").read_text(encoding="utf-8")

    trading_start = "## 2. 資料紀律與時間錨定"
    acquisition_start = "## 15. 快速監控與每輪追最新"
    assert parent[parent.index(trading_start) : parent.index(acquisition_start)] == rc[
        rc.index(trading_start) : rc.index(acquisition_start)
    ]
    assert (rc_dir / "analysis-schema.json").read_bytes() == (
        root.parent / "schemas/analysis-v4.json"
    ).read_bytes()


def test_v16_production_release_is_self_contained_and_preserves_rc_logic() -> None:
    root = Path(versioning.RULES_ROOT)
    rc_dir = root / "versions/enlightenment-integrated-v1.6-dual-scale-rc"
    release_dir = root / "versions/enlightenment-integrated-v1.6-dual-scale"
    execution_prompt = (release_dir / "prompt.md").read_text(encoding="utf-8")
    analysis_prompt = (release_dir / "analysis-prompt.md").read_text(encoding="utf-8")
    rc_analysis = (rc_dir / "analysis-prompt.md").read_text(encoding="utf-8")
    marker = "# 台指期 1 分 K 趨勢與交易機會監控｜啟蒙交易＋道氏三兄弟正式分析規則"
    trading_start = "## 2. 資料紀律與時間錨定"

    assert execution_prompt[execution_prompt.index(marker) :] == analysis_prompt
    assert "enlightenment-integrated-v1.6-dual-scale-analysis" in execution_prompt
    assert "Chrome 雙視角正式執行層" in execution_prompt
    assert "不得使用 Computer Use" in execution_prompt
    assert analysis_prompt[analysis_prompt.index(trading_start) :].rstrip("\n") == rc_analysis[
        rc_analysis.index(trading_start) :
    ].rstrip("\n")
    assert (release_dir / "analysis-schema.json").read_bytes() == (
        root.parent / "schemas/analysis-v4.json"
    ).read_bytes()


def test_v17_anchor_rc_is_self_contained_and_preserved_after_formal_promotion() -> None:
    root = Path(versioning.RULES_ROOT)
    rc_dir = root / "versions/enlightenment-integrated-v1.7-anchor-rc-shadow"
    execution_prompt = (rc_dir / "prompt.md").read_text(encoding="utf-8")
    analysis_prompt = (rc_dir / "analysis-prompt.md").read_text(encoding="utf-8")
    marker = "# 台指期 1 分 K 趨勢與交易機會監控｜啟蒙交易＋道氏三兄弟正式分析規則"

    assert execution_prompt[execution_prompt.index(marker) :] == analysis_prompt
    assert "定錨、反向定錨與動態象限" in analysis_prompt
    assert "anchor_context" in analysis_prompt
    assert "trade_monitor/schemas/analysis-v5.json" in analysis_prompt
    assert "market_structure_state.version=3" in analysis_prompt
    assert "不增加第五種型態" in analysis_prompt
    assert analysis_prompt.count("### 7.") == 4
    assert (rc_dir / "analysis-schema.json").read_bytes() == (
        root.parent / "schemas/analysis-v5.json"
    ).read_bytes()

    rc_config = json.loads((rc_dir / "local_scheduler_config.json").read_text(encoding="utf-8"))
    formal_config = json.loads((root.parent / "local_scheduler_config.json").read_text(encoding="utf-8"))
    active = json.loads((root / "active-version.json").read_text(encoding="utf-8"))
    assert rc_config["market_structure_state_version"] == 3
    assert rc_config["prompt_version"] == "enlightenment-integrated-v1.7-anchor-rc-shadow-analysis"
    assert formal_config["prompt_version"] == ACTIVE_PROMPT_VERSION
    assert formal_config["market_structure_state_version"] == 6
    assert active["version_id"] == ACTIVE_VERSION_ID
    assert active["automation_status"] in {"PAUSED", "ACTIVE"}


def test_v17_anchor_production_artifacts_are_self_contained_and_version_locked() -> None:
    root = Path(versioning.RULES_ROOT)
    release_dir = root / "versions/enlightenment-integrated-v1.7-anchor"
    execution_prompt = (release_dir / "prompt.md").read_text(encoding="utf-8")
    analysis_prompt = (release_dir / "analysis-prompt.md").read_text(encoding="utf-8")
    marker = "# 台指期 1 分 K 趨勢與交易機會監控｜啟蒙交易＋道氏三兄弟正式分析規則"

    assert execution_prompt[execution_prompt.index(marker) :] == analysis_prompt
    assert "定錨整合正式執行層" in execution_prompt
    assert "enlightenment-integrated-v1.7-anchor-analysis" in execution_prompt
    assert "RC 狀態" not in execution_prompt
    assert "定錨、反向定錨與動態象限" in analysis_prompt
    assert "anchor_context" in analysis_prompt
    assert "market_structure_state.version=3" in analysis_prompt
    assert "不增加第五種型態" in analysis_prompt
    assert analysis_prompt.count("### 7.") == 4
    assert (release_dir / "analysis-schema.json").read_bytes() == (
        root.parent / "schemas/analysis-v5.json"
    ).read_bytes()

    config = json.loads((release_dir / "local_scheduler_config.json").read_text(encoding="utf-8"))
    assert config["market_structure_state_version"] == 3
    assert config["prompt_version"] == "enlightenment-integrated-v1.7-anchor-analysis"
    assert config["prompt_sha256"] == hashlib.sha256(analysis_prompt.encode("utf-8")).hexdigest()


def test_pre_cclass_restore_point_matches_actual_v17_and_is_immutable() -> None:
    root = Path(versioning.RULES_ROOT)
    backup = root / "versions/pre-cclass-v1.7-20260902"
    formal = root / "versions/enlightenment-integrated-v1.7-anchor"
    metadata = json.loads((backup / "metadata.json").read_text(encoding="utf-8"))

    for name in ("prompt.md", "analysis-prompt.md", "analysis-schema.json", "local_scheduler_config.json"):
        assert (backup / name).read_bytes() == (formal / name).read_bytes()
    assert metadata["immutable"] is True
    assert metadata["automation_status"] == "PAUSED"
    assert metadata["prompt_sha256"] == hashlib.sha256((backup / "prompt.md").read_bytes()).hexdigest()
    assert metadata["automation_toml_sha256"] == hashlib.sha256(
        (backup / "automation.toml").read_bytes()
    ).hexdigest()


def test_v18_cclass_rc_is_complete_isolated_and_preserved_after_promotion() -> None:
    root = Path(versioning.RULES_ROOT)
    rc_dir = root / "versions/enlightenment-integrated-v1.8-cclass-rc-shadow"
    execution_prompt = (rc_dir / "prompt.md").read_text(encoding="utf-8")
    analysis_prompt = (rc_dir / "analysis-prompt.md").read_text(encoding="utf-8")
    marker = "# 台指期 1 分 K 趨勢與交易機會監控｜啟蒙交易＋道氏三兄弟＋戰法 C 班 RC 分析規則"
    required_complete_concepts = (
        "ANCHOR_1",
        "CORRECTION_2",
        "COPY_3",
        "CORRECTION_4",
        "COPY_5",
        "POST_5",
        "幅度關係",
        "所用時間關係",
        "斜率關係",
        "乾淨度關係",
        "結構破壞性關係",
        "CENTRIFUGAL_CONFIRMED",
        "DRAGON_EARLY",
        "LIFE_DEATH_GATE_ARMED",
        "thesis_context",
    )

    assert execution_prompt[execution_prompt.index(marker) :] == analysis_prompt
    assert all(item in analysis_prompt for item in required_complete_concepts)
    assert analysis_prompt.count("### 7.") == 4
    assert "不增加第五種型態" in analysis_prompt
    assert "不得顯示成第十個章節" in analysis_prompt
    assert (rc_dir / "analysis-schema.json").read_bytes() == (
        root.parent / "schemas/analysis-v6.json"
    ).read_bytes()

    rc_config = json.loads((rc_dir / "local_scheduler_config.json").read_text(encoding="utf-8"))
    formal_config = json.loads((root.parent / "local_scheduler_config.json").read_text(encoding="utf-8"))
    active = json.loads((root / "active-version.json").read_text(encoding="utf-8"))
    assert rc_config["market_structure_state_version"] == 4
    assert rc_config["prompt_version"] == "enlightenment-integrated-v1.8-cclass-rc-shadow-analysis"
    assert rc_config["prompt_sha256"] == hashlib.sha256(analysis_prompt.encode("utf-8")).hexdigest()
    assert "cclass_v18_shadow" in rc_config["dual_scale"]["state_path"]
    assert formal_config["prompt_version"] == ACTIVE_PROMPT_VERSION
    assert formal_config["market_structure_state_version"] == 6
    assert active["version_id"] == ACTIVE_VERSION_ID
    assert active["automation_status"] in {"PAUSED", "ACTIVE"}


def test_v18_cclass_production_artifacts_are_self_contained_and_version_locked() -> None:
    root = Path(versioning.RULES_ROOT)
    release_dir = root / "versions/enlightenment-integrated-v1.8-cclass"
    execution_prompt = (release_dir / "prompt.md").read_text(encoding="utf-8")
    analysis_prompt = (release_dir / "analysis-prompt.md").read_text(encoding="utf-8")
    marker = "# 台指期 1 分 K 趨勢與交易機會監控｜啟蒙交易＋道氏三兄弟＋戰法 C 班正式分析規則"

    assert execution_prompt[execution_prompt.index(marker) :].rstrip("\n") == analysis_prompt.rstrip("\n")
    assert "戰法 C 班整合正式執行層" in execution_prompt
    assert "enlightenment-integrated-v1.8-cclass-analysis" in execution_prompt
    assert "RC 隔離狀態" not in execution_prompt
    assert "trade-monitor-analysis-v6" in analysis_prompt
    assert "market_structure_state.version=4" in analysis_prompt
    assert analysis_prompt.count("### 7.") == 4
    assert (release_dir / "analysis-schema.json").read_bytes() == (
        root.parent / "schemas/analysis-v6.json"
    ).read_bytes()

    config = json.loads((release_dir / "local_scheduler_config.json").read_text(encoding="utf-8"))
    assert config["market_structure_state_version"] == 4
    assert config["prompt_version"] == "enlightenment-integrated-v1.8-cclass-analysis"
    assert config["prompt_sha256"] == hashlib.sha256(analysis_prompt.encode("utf-8")).hexdigest()
    assert config["dual_scale"]["state_path"] == ".runtime/trade_monitor/dual_scale_state.json"


def test_v21_prospective_all_course_candidate_is_self_contained_and_version_locked() -> None:
    root = Path(versioning.RULES_ROOT)
    release_dir = root / "versions/enlightenment-integrated-v2.1-prospective"
    execution_prompt = (release_dir / "prompt.md").read_text(encoding="utf-8")
    analysis_prompt = (release_dir / "analysis-prompt.md").read_text(encoding="utf-8")
    marker = "# 台指期 1 分 K 趨勢與交易機會監控｜完整課程統一交易系統正式分析規則"
    config = json.loads((release_dir / "local_scheduler_config.json").read_text(encoding="utf-8"))
    metadata = json.loads((release_dir / "metadata.json").read_text(encoding="utf-8"))

    assert execution_prompt[execution_prompt.index(marker) :].rstrip("\n") == analysis_prompt.rstrip("\n")
    assert "全戰法判讀矩陣、前瞻情境與主控進出場" in analysis_prompt
    assert "原四型態**：保留為四型態戰法家族" in analysis_prompt
    assert "太極演化必須前瞻化" in analysis_prompt
    assert "主控戰法決定出場，持倉中不得換理由" in analysis_prompt
    assert "再只從原四種型態選出唯一可執行候選" not in analysis_prompt
    assert (release_dir / "analysis-schema.json").read_bytes() == (
        root.parent / "schemas/analysis-v8.json"
    ).read_bytes()
    assert config["prompt_version"] == "enlightenment-integrated-v2.1-prospective-analysis"
    assert config["market_structure_state_version"] == 6
    assert config["prompt_sha256"] == hashlib.sha256(analysis_prompt.encode("utf-8")).hexdigest()
    assert metadata["contract_version"] == 8
    assert metadata["prospective_context_version"] == 1
    assert metadata["status"] in {"validated_not_deployed", "production_release_readback_verified_paused"}

    formal_config = json.loads((root.parent / "local_scheduler_config.json").read_text(encoding="utf-8"))
    active = json.loads((root / "active-version.json").read_text(encoding="utf-8"))
    assert formal_config["prompt_version"] == ACTIVE_PROMPT_VERSION
    assert active["version_id"] == ACTIVE_VERSION_ID
    assert active["restore_point"] == ACTIVE_RESTORE_POINT
    assert active["automation_status"] == "ACTIVE"


def test_v211_course_terms_release_preserves_original_course_names_without_rule_changes() -> None:
    root = Path(versioning.RULES_ROOT)
    release_dir = root / "versions/enlightenment-integrated-v2.1.1-course-terms"
    parent_dir = root / "versions/enlightenment-integrated-v2.1-prospective"
    execution_prompt = (release_dir / "prompt.md").read_text(encoding="utf-8")
    analysis_prompt = (release_dir / "analysis-prompt.md").read_text(encoding="utf-8")
    config = json.loads((release_dir / "local_scheduler_config.json").read_text(encoding="utf-8"))
    metadata = json.loads((release_dir / "metadata.json").read_text(encoding="utf-8"))
    marker = "# 台指期 1 分 K 趨勢與交易機會監控｜完整課程統一交易系統正式分析規則"

    assert execution_prompt[execution_prompt.index(marker) :].rstrip("\n") == analysis_prompt.rstrip("\n")
    assert "`ATR14` 保留原名" in analysis_prompt
    assert "A級點／B級點／C級點" in analysis_prompt
    assert "十四期平均真實波幅" not in analysis_prompt
    assert all(term not in analysis_prompt for term in ("甲級", "乙級", "丙級", "甲／乙／丙"))
    assert config["prompt_version"] == "enlightenment-integrated-v2.1.1-course-terms-analysis"
    assert config["prompt_sha256"] == hashlib.sha256(analysis_prompt.encode("utf-8")).hexdigest()
    assert metadata["user_visible_chinese_presentation_version"] == 2
    assert metadata["restore_point"] == "enlightenment-integrated-v2.1-prospective"
    assert (release_dir / "analysis-schema.json").read_bytes() == (parent_dir / "analysis-schema.json").read_bytes()

def test_v212_pivot_terms_release_uses_secondary_high_and_low_without_rule_changes() -> None:
    root = Path(versioning.RULES_ROOT)
    release_dir = root / "versions/enlightenment-integrated-v2.1.2-pivot-terms"
    parent_dir = root / "versions/enlightenment-integrated-v2.1.1-course-terms"
    execution_prompt = (release_dir / "prompt.md").read_text(encoding="utf-8")
    analysis_prompt = (release_dir / "analysis-prompt.md").read_text(encoding="utf-8")
    config = json.loads((release_dir / "local_scheduler_config.json").read_text(encoding="utf-8"))
    metadata = json.loads((release_dir / "metadata.json").read_text(encoding="utf-8"))
    marker = "# 台指期 1 分 K 趨勢與交易機會監控｜完整課程統一交易系統正式分析規則"

    assert execution_prompt[execution_prompt.index(marker) :].rstrip("\n") == analysis_prompt.rstrip("\n")
    assert "次高點" in analysis_prompt
    assert "次低點" in analysis_prompt
    assert "只出現次低點或次高點" in analysis_prompt
    assert config["prompt_version"] == "enlightenment-integrated-v2.1.2-pivot-terms-analysis"
    assert config["prompt_sha256"] == hashlib.sha256(analysis_prompt.encode("utf-8")).hexdigest()
    assert metadata["user_visible_chinese_presentation_version"] == 3
    assert metadata["restore_point"] == "enlightenment-integrated-v2.1.1-course-terms"
    assert (release_dir / "analysis-schema.json").read_bytes() == (parent_dir / "analysis-schema.json").read_bytes()

def test_v213_type_terms_release_preserves_type_names_with_first_use_explanations() -> None:
    root = Path(versioning.RULES_ROOT)
    release_dir = root / "versions/enlightenment-integrated-v2.1.3-type-terms"
    parent_dir = root / "versions/enlightenment-integrated-v2.1.2-pivot-terms"
    execution_prompt = (release_dir / "prompt.md").read_text(encoding="utf-8")
    analysis_prompt = (release_dir / "analysis-prompt.md").read_text(encoding="utf-8")
    config = json.loads((release_dir / "local_scheduler_config.json").read_text(encoding="utf-8"))
    metadata = json.loads((release_dir / "metadata.json").read_text(encoding="utf-8"))
    marker = "# 台指期 1 分 K 趨勢與交易機會監控｜完整課程統一交易系統正式分析規則"

    assert execution_prompt[execution_prompt.index(marker) :].rstrip("\n") == analysis_prompt.rstrip("\n")
    assert "Type1（小級先反向，大級防線尚未失守）" in analysis_prompt
    assert "Type2（原方向防線失守，反向結構完成確認）" in analysis_prompt
    assert "Type3（強勢跨級急轉，連續突破大小級防線）" in analysis_prompt
    assert "同一 Type 後續再次出現時只寫代號" in analysis_prompt
    assert all(term not in analysis_prompt for term in ("第一類反轉", "第二類反轉", "第三類反轉"))
    assert config["prompt_version"] == "enlightenment-integrated-v2.1.3-type-terms-analysis"
    assert config["prompt_sha256"] == hashlib.sha256(analysis_prompt.encode("utf-8")).hexdigest()
    assert metadata["user_visible_chinese_presentation_version"] == 4
    assert metadata["restore_point"] == "enlightenment-integrated-v2.1.2-pivot-terms"
    assert (release_dir / "analysis-schema.json").read_bytes() == (parent_dir / "analysis-schema.json").read_bytes()

    assert (release_dir / "analysis-schema.json").read_bytes() == (parent_dir / "analysis-schema.json").read_bytes()


def test_v213_schema_hotfix_preserves_trading_prompts() -> None:
    root = Path(versioning.RULES_ROOT)
    release_dir = root / "versions/enlightenment-integrated-v2.1.3-schema-hotfix"
    parent_dir = root / "versions/enlightenment-integrated-v2.1.3-type-terms"
    config = json.loads((release_dir / "local_scheduler_config.json").read_text(encoding="utf-8"))
    schema = json.loads((release_dir / "analysis-schema.json").read_text(encoding="utf-8"))

    assert (release_dir / "prompt.md").read_bytes() == (parent_dir / "prompt.md").read_bytes()
    assert (release_dir / "analysis-prompt.md").read_bytes() == (parent_dir / "analysis-prompt.md").read_bytes()
    assert config["prompt_version"] == "enlightenment-integrated-v2.1.3-type-terms-analysis"

    def walk(value: object) -> None:
        if isinstance(value, dict):
            assert "uniqueItems" not in value
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(schema)


def test_v214_same_grade_quadrant_release_is_self_contained_and_axis_consistent() -> None:
    root = Path(versioning.RULES_ROOT)
    release_dir = root / "versions/enlightenment-integrated-v2.1.4-same-grade-quadrant"
    parent_dir = root / "versions/enlightenment-integrated-v2.1.3-schema-hotfix"
    execution_prompt = (release_dir / "prompt.md").read_text(encoding="utf-8")
    analysis_prompt = (release_dir / "analysis-prompt.md").read_text(encoding="utf-8")
    config = json.loads((release_dir / "local_scheduler_config.json").read_text(encoding="utf-8"))
    metadata = json.loads((release_dir / "metadata.json").read_text(encoding="utf-8"))
    marker = "# 台指期 1 分 K 趨勢與交易機會監控｜完整課程統一交易系統正式分析規則"

    assert execution_prompt[execution_prompt.index(marker) :].rstrip("\n") == analysis_prompt.rstrip("\n")
    assert "工作象限的同級證據約束" in analysis_prompt
    assert "主要候選只能在第一與第四象限間排序" in analysis_prompt
    assert "不能單獨讓當前明確趨勢維持第二象限" in analysis_prompt
    assert config["prompt_version"] == "enlightenment-integrated-v2.1.4-same-grade-quadrant-analysis"
    assert config["prompt_sha256"] == hashlib.sha256(analysis_prompt.encode("utf-8")).hexdigest()
    assert metadata["restore_point"] == "enlightenment-integrated-v2.1.3-schema-hotfix"
    assert (release_dir / "analysis-schema.json").read_bytes() == (parent_dir / "analysis-schema.json").read_bytes()


def test_v215_defense_consistency_release_is_self_contained() -> None:
    root = Path(versioning.RULES_ROOT)
    release_dir = root / "versions/enlightenment-integrated-v2.1.5-defense-consistency"
    parent_dir = root / "versions/enlightenment-integrated-v2.1.4-same-grade-quadrant"
    execution_prompt = (release_dir / "prompt.md").read_text(encoding="utf-8")
    analysis_prompt = (release_dir / "analysis-prompt.md").read_text(encoding="utf-8")
    config = json.loads((release_dir / "local_scheduler_config.json").read_text(encoding="utf-8"))
    metadata = json.loads((release_dir / "metadata.json").read_text(encoding="utf-8"))
    marker = "# 台指期 1 分 K 趨勢與交易機會監控｜完整課程統一交易系統正式分析規則"

    assert execution_prompt[execution_prompt.index(marker) :].rstrip("\n") == analysis_prompt.rstrip("\n")
    assert "可見歷史結構復原與防線一致性" in analysis_prompt
    assert "確認的小級道氏方向" in analysis_prompt
    assert "不得把已成立的小級防線一併否定" in analysis_prompt
    assert config["prompt_version"] == "enlightenment-integrated-v2.1.5-defense-consistency-analysis"
    assert config["prompt_sha256"] == hashlib.sha256(analysis_prompt.encode("utf-8")).hexdigest()
    assert metadata["restore_point"] == "enlightenment-integrated-v2.1.4-same-grade-quadrant"
    assert (release_dir / "analysis-schema.json").read_bytes() == (parent_dir / "analysis-schema.json").read_bytes()


def test_v216_anchor_hierarchy_release_is_self_contained_and_preserved() -> None:
    root = Path(versioning.RULES_ROOT)
    release_dir = root / "versions/enlightenment-integrated-v2.1.6-anchor-hierarchy"
    parent_dir = root / "versions/enlightenment-integrated-v2.1.5-defense-consistency"
    execution_prompt = (release_dir / "prompt.md").read_text(encoding="utf-8")
    analysis_prompt = (release_dir / "analysis-prompt.md").read_text(encoding="utf-8")
    config = json.loads((release_dir / "local_scheduler_config.json").read_text(encoding="utf-8"))
    metadata = json.loads((release_dir / "metadata.json").read_text(encoding="utf-8"))
    formal_config = json.loads((root.parent / "local_scheduler_config.json").read_text(encoding="utf-8"))
    active = json.loads((root / "active-version.json").read_text(encoding="utf-8"))
    marker = "# 台指期 1 分 K 趨勢與交易機會監控｜完整課程統一交易系統正式分析規則"

    assert execution_prompt[execution_prompt.index(marker) :].rstrip("\n") == analysis_prompt.rstrip("\n")
    assert "大小錨父子結構、跨級升級與時間價格一致性" in analysis_prompt
    assert "不得只保留 `ONLY_SMALL`" in analysis_prompt
    assert "start_bar_time" in analysis_prompt and "start_price_estimate" in analysis_prompt
    assert config["prompt_version"] == "enlightenment-integrated-v2.1.6-anchor-hierarchy-analysis"
    assert config["prompt_sha256"] == hashlib.sha256(analysis_prompt.encode("utf-8")).hexdigest()
    assert metadata["restore_point"] == "enlightenment-integrated-v2.1.5-defense-consistency"
    assert metadata["status"] == "production_release_readback_verified_active"
    assert (release_dir / "analysis-schema.json").read_bytes() == (parent_dir / "analysis-schema.json").read_bytes()
    assert formal_config["prompt_version"] == ACTIVE_PROMPT_VERSION
    assert active["version_id"] == ACTIVE_VERSION_ID
    assert active["automation_status"] == "ACTIVE"


def test_v218_anchor_origin_release_is_self_contained_and_active() -> None:
    root = Path(versioning.RULES_ROOT)
    release_dir = root / f"versions/{ACTIVE_VERSION_ID}"
    parent_dir = root / "versions/enlightenment-integrated-v2.1.7-context-consistency"
    execution_prompt = (release_dir / "prompt.md").read_text(encoding="utf-8")
    analysis_prompt = (release_dir / "analysis-prompt.md").read_text(encoding="utf-8")
    config = json.loads((release_dir / "local_scheduler_config.json").read_text(encoding="utf-8"))
    metadata = json.loads((release_dir / "metadata.json").read_text(encoding="utf-8"))
    formal_config = json.loads((root.parent / "local_scheduler_config.json").read_text(encoding="utf-8"))
    active = json.loads((root / "active-version.json").read_text(encoding="utf-8"))
    marker = "# 台指期 1 分 K 趨勢與交易機會監控｜完整課程統一交易系統正式分析規則"

    assert execution_prompt[execution_prompt.index(marker) :].rstrip("\n") == analysis_prompt.rstrip("\n")
    assert "不得回報「尚無完整複製／修正」" in analysis_prompt
    assert "向下第四象限，而不是把「大多方趨勢變弱」誤寫成第三象限" in analysis_prompt
    assert "主要與次要象限候選必須只差一個軸" in analysis_prompt
    assert "盤中交易機會持續掃描" in analysis_prompt
    assert "新大錨必須承接該前一反向錨的最後極值" in analysis_prompt
    assert "`ANCHOR_1.start_bar_time` 必須與該大錨 `start_bar_time` 相同" in analysis_prompt
    assert "跨分鐘單一槽位採兩階段生命週期" in analysis_prompt
    assert config["prompt_version"] == ACTIVE_PROMPT_VERSION
    assert config["prompt_sha256"] == hashlib.sha256(analysis_prompt.encode("utf-8")).hexdigest()
    assert metadata["restore_point"] == ACTIVE_RESTORE_POINT
    assert metadata["status"] == "production_release_readback_verified_active"
    assert (release_dir / "analysis-schema.json").read_bytes() == (parent_dir / "analysis-schema.json").read_bytes()
    assert formal_config == config
    assert active["version_id"] == ACTIVE_VERSION_ID
    assert active["prompt_sha256"] == hashlib.sha256((release_dir / "prompt.md").read_bytes()).hexdigest()
    assert active["schema_sha256"] == hashlib.sha256((release_dir / "analysis-schema.json").read_bytes()).hexdigest()
    assert active["automation_status"] == "ACTIVE"
