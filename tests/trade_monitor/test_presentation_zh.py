from __future__ import annotations

from trade_monitor.analysis_contract import render_analysis_markdown
from trade_monitor.presentation_zh import (
    annotate_reversal_types,
    localize_user_text,
    normalize_user_visible_times,
    operational_error_label,
    reversal_type_definitions,
)


def test_named_setup_stages_are_chinese_in_user_text() -> None:
    text = localize_user_text(
        "FORMING -> ARMED -> AGGRESSIVE_CONFIRMED -> CONSERVATIVE_CONFIRMED -> INVALIDATED / NO_CHASE"
    )
    assert text == "形成中 -> 待觸發 -> 積極確認 -> 保守確認 -> 已失效 / 不宜追價"


def test_reversal_types_have_clear_chinese_names_and_definitions() -> None:
    text = localize_user_text("Type1, TYPE_2, Type 3")
    assert text == "Type1, Type2, Type3"
    annotated = annotate_reversal_types(f"{text}; Type2 再次出現")
    assert annotated == (
        "Type1（小級先反向，大級防線尚未失守）, "
        "Type2（原方向防線失守，反向結構完成確認）, "
        "Type3（強勢跨級急轉，連續突破大小級防線）; Type2 再次出現"
    )
    assert annotate_reversal_types("Type2成立；Type2仍有效") == (
        "Type2（原方向防線失守，反向結構完成確認）成立；Type2仍有效"
    )
    definitions = reversal_type_definitions()
    assert "Type1（小級先反向，大級防線尚未失守）" in definitions[0]
    assert "Type2（原方向防線失守，反向結構完成確認）" in definitions[1]
    assert "Type3（強勢跨級急轉，連續突破大小級防線）" in definitions[2]


def test_renderer_localizes_internal_tokens_for_both_delivery_channels() -> None:
    payload = {
        "original_decision": "NOTIFY",
        "notification_reason": "setup 從 FORMING 進入 AGGRESSIVE_CONFIRMED。",
        "latest_closed_k_price_estimate": "點位無法可靠估計。",
        "latest_closed_k_details": ["DETAIL 圖有效，OVERVIEW 不可用。"],
        "large_trend": {"classification": "資料不足", "details": ["QUADRANT_PRIMARY；Q4。"]},
        "current_trend": {"classification": "轉換中", "details": ["Type2 尚待確認。"]},
        "market_state": ["ATR14 只作交叉驗證。"],
        "pattern_observation": {
            "status": "條件式偏空",
            "pattern": "TREND_PULLBACK_CONTINUATION",
            "details": ["NO_CHASE。"],
        },
        "missing_conditions_or_trigger": ["等待 CONSERVATIVE_CONFIRMED。"],
        "entry_and_structural_stop": ["FORMING。"],
        "risk_and_nearest_obstacle": ["1R 尚無法計算。"],
        "single_contract_management_or_prohibition": ["INVALIDATED 後退出。"],
    }
    context = {
        "expected_latest_closed_k_hhmm": "10:39",
        "current_unclosed_k_hhmm": "10:40",
        "new_closed_bar_count": 1,
    }
    message = render_analysis_markdown(payload, context, resumed=False)
    for token in (
        "FORMING",
        "AGGRESSIVE_CONFIRMED",
        "CONSERVATIVE_CONFIRMED",
        "QUADRANT_PRIMARY",
        "Q4",
        "TREND_PULLBACK_CONTINUATION",
        "NO_CHASE",
        "INVALIDATED",
        "1R",
    ):
        assert token not in message
    assert "四象限主判讀" in message
    assert "第四象限" in message
    assert "Type2（原方向防線失守，反向結構完成確認）" in message
    assert "ATR14 只作交叉驗證" in message
    assert "一倍初始風險與最近障礙" in message


def test_operational_error_codes_are_not_exposed_to_users() -> None:
    assert operational_error_label("codex_timeout") == "分析程序逾時"
    assert operational_error_label("unmapped_internal_code") == "本機監控發生未分類錯誤"


def test_course_and_data_jargon_is_localized_in_user_text() -> None:
    text = localize_user_text("X 流程、家族 DNA、S／S／T／V、OHLC、session setup、canonical message")
    assert text == "整合決策流程、家族結構特徵、箱型四面向品質、開高低收、交易時段 進場型態、標準訊息"


def test_setup_evaluation_uses_continuous_scan_wording() -> None:
    assert localize_user_text("SETUP_EVALUATION") == "盤中交易機會持續掃描"


def test_course_original_terms_atr14_and_abc_grade_points_are_preserved() -> None:
    text = localize_user_text("ATR14、A_CANDIDATE、B_CANDIDATE、C_CANDIDATE")
    assert text == "ATR14、A級點、B級點、C級點"
    assert "十四期平均真實波幅" not in text
    assert all(term not in text for term in ("甲級", "乙級", "丙級"))


def test_moving_average_names_keep_numeric_periods() -> None:
    text = localize_user_text(
        "二十一期均價、二十一期與一百零五期均價、MA21、MA 105"
    )

    assert text == "均價21、均價21與均價105、均價21、均價105"
    assert "二十一期" not in text
    assert "一百零五期" not in text


def test_user_visible_clock_times_use_compact_24_hour_format() -> None:
    assert normalize_user_visible_times("十一時二十一分") == "11:21"
    assert normalize_user_visible_times("上午九點五分") == "09:05"
    assert normalize_user_visible_times("下午一時二十一分") == "13:21"
    assert localize_user_text("作用中大錨由十一時二十一分起算。") == "作用中大錨由11:21起算。"
    assert localize_user_text("既有時間 11:21 不變。") == "既有時間 11:21 不變。"


def test_strategy_machine_codes_never_reach_user_visible_text() -> None:
    text = localize_user_text(
        "YIZHI_CENTRIFUGAL、TAIJI_REANCHOR_AFTER_FAILURE、DOW_TYPE2_REVERSAL"
    )

    assert "YIZHI_CENTRIFUGAL" not in text
    assert "TAIJI_REANCHOR_AFTER_FAILURE" not in text
    assert "DOW_TYPE2_REVERSAL" not in text
    assert "一之－離心力" in text
    assert "太極－失敗後重新定錨" in text
    assert "道氏－Type2 防線反轉" in text


def test_dow_swing_descriptions_use_secondary_high_and_low_terms() -> None:
    text = localize_user_text("較低高點、較低的高點、較高低點、較高的低點、次高／次低、次高低")
    assert text == "次高點、次高點、次低點、次低點、次高點／次低點、次高點／次低點"
