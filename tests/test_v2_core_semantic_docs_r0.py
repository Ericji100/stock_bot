from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "course_backtest" / "2026-09-10" / "v2_core_reproducible_goal_v1"

SCENARIO_GATES = {
    "MATURE_TREND_PULLBACK": {
        "ACTIVE_LARGE_UPTREND",
        "LONG_TREND_PERSISTENCE",
        "LONG_MA_HABIT",
        "CORRECTION_WITHIN_CAMPAIGN",
        "TAIJI_GENERATION_MAPPED",
        "DYNAMIC_QUADRANTS_SUPPORT",
        "SMALL_UP_CONTROL_CAUSAL",
        "EPISODE_STOP_CAUSAL",
    },
    "MACRO_COPY_RESONANCE": {
        "COMPLETED_PARENT_ANCHOR",
        "CORRECTION_INTACT",
        "TAIJI_GENERATION_MAPPED",
        "CORRECTION_BEAR_DOW_LINE_CAUSAL",
        "SMALL_UP_REANCHOR_BREAK",
        "DUAL_SCALE_LONG_ALIGNMENT",
        "NOT_Q3_OR_EXHAUSTED",
        "EPISODE_STOP_CAUSAL",
    },
    "BEAR_REVERSAL_LEFT_RIGHT": {
        "ACTIVE_LARGE_BEAR_ANCHOR",
        "LARGE_BEAR_DOW_DEFENSE_CAUSAL",
        "BEAR_LATE_STAGE_EVIDENCE",
        "LEFT_RIGHT_PHASE_MAPPED",
        "DUAL_SCALE_SEPARATED",
        "PHASE_STOP_CAUSAL",
    },
    "FRESH_Q1_EXPANSION": {
        "FRESH_UP_ANCHOR",
        "CLEAN_MEATY_DESTRUCTIVE_TRACEABLE",
        "DYNAMIC_Q1_EXPANSION",
        "EARLY_TAIJI_GENERATION",
        "MACD_SUPPORT_ONLY",
        "EARLY_LOCATION_WITH_SPACE",
        "FRESH_ANCHOR_STOP_CAUSAL",
    },
}


def read(name: str) -> str:
    return (OUT / name).read_text(encoding="utf-8")


def test_common_semantics_contains_all_shared_layers() -> None:
    text = read("v2_core_semantics.md")
    for term in (
        "資料充分性",
        "定錨原子語意",
        "父代、修正、複製與長多慣性",
        "太極與世代",
        "大小級與道氏",
        "動態四象限",
        "左右階段",
        "位階、空間與耗竭",
        "防線與交易生命週期",
        "觸發語意",
        "AI交易權限真值條件",
    ):
        assert term in text
    assert "狀態：`DRAFT_FOR_CALIBRATION（校準草案）`" in text
    assert "狀態：`FROZEN（已凍結）`" not in text


def test_boundaries_include_every_scenario_and_gate() -> None:
    text = read("v2_scenario_boundaries.md")
    for scenario, gates in SCENARIO_GATES.items():
        assert scenario in text
        for gate in gates:
            assert gate in text


def test_comparison_discloses_duplicate_and_sample_limits() -> None:
    text = read("legacy_ai_positive_negative_comparison.md")
    assert "127列正例只有121個不同股票日" in text
    assert "3個主要情境衝突" in text
    assert "BEAR_REVERSAL_LEFT_RIGHT（空頭末段左右反轉）`只有2列正例" in text
    assert "未來MFE、MAE、損益、出場與大贏家標籤" in text


def test_semantics_separates_structure_and_position_lifecycle() -> None:
    text = read("v2_core_semantics.md")
    for field in ("taiji_phase", "taiji_leg_index", "campaign_generation", "position_role", "episode_number"):
        assert field in text
    assert "位置角色與監控起始不得決定結構情境" in text
