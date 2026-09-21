from scripts.course_tg_selection_catalog import (
    parse_chip,
    parse_curated,
    parse_fundamental,
    parse_radar,
    parse_technical,
)


STOCKS = {"2108": "南帝", "3709": "鑫聯大投控", "2027": "大成鋼"}


def message(text: str):
    return {"id": 1, "date": "2026-07-02T14:52:27", "text": text}


def test_technical_full_report_keeps_each_strategy_membership():
    text = """🔍 今日技術面選股掃描報告
📅 日期：2026-07-02
📂 突破 21MA
2108 南帝 (30)
📂 跌破後收復 105MA
3709 鑫聯大投控 (74.5)
策略 A：多頭延續回檔突破
2108 南帝｜前波漲幅 30%
"""
    rows, unknown = parse_technical(message(text), text, STOCKS)
    assert not unknown
    assert {(row["strategy"], row["code"]) for row in rows} == {
        ("TECH_MA21_BREAKOUT", "2108"),
        ("TECH_MA105_RECLAIM", "3709"),
        ("TECH_A_PULLBACK_BREAKOUT", "2108"),
    }


def test_technical_strategy_d_new_and_legacy_titles_map_to_same_catalog():
    for title in ("策略 D：動能背景短線轉強", "策略 D：中長均線背景短線轉強", "策略 D：強勢股急跌收復"):
        text = f"""🔍 今日技術面選股掃描報告
📅 日期：2026-07-02
{title}
D1｜短均線首次收復
[橡膠業]
2108 南帝 (30.0)｜首次收復 MA5
"""
        rows, unknown = parse_technical(message(text), text, STOCKS)
        assert not unknown
        assert [(row["strategy"], row["code"]) for row in rows] == [("TECH_D_DROP_RECLAIM", "2108")]


def test_fundamental_keeps_growth_group_and_grade():
    text = """🔍 今日財報營收選股掃描報告
📅 日期：2026-07-02
📂 【營收第一組：連 4 月穩健成長】
🥇 A級：毛利連三季遞增
2027 大成鋼 (40)
📂 【營收第二組：動能轉強】
🥈 B級：毛利穩定波動
2108 南帝 (30)
"""
    rows = parse_fundamental(message(text), text, STOCKS)
    assert [(row["strategy"], row["grade"]) for row in rows] == [
        ("FUND_REVENUE_G1", "A"), ("FUND_REVENUE_G2", "B")
    ]


def test_chip_grade_is_preserved():
    text = """🔍 今日投信認養股掃描報告
📅 日期：2026-07-02
🥇 S級
2108 南帝 (30)
🥈 A級
2027 大成鋼 (40)
"""
    rows = parse_chip(message(text), text, STOCKS, "CHIP_TRUST_ADOPTION")
    assert [row["grade"] for row in rows] == ["S", "A"]


def test_curated_and_radar_metadata_are_preserved():
    curated = """⭐ 精選選股交叉命中報告
📅 日期：2026-07-02
【命中 3 個策略】
2108 南帝 | 產業：橡膠
"""
    rows = parse_curated(message(curated), curated, STOCKS)
    assert rows[0]["grade"] == "3"
    radar = """📡 每日選股雷達 2026-07-02
1. 2027 大成鋼｜45分｜策略 A
"""
    rows = parse_radar(message(radar), radar, STOCKS)
    assert rows[0]["grade"] == "45"
    assert "排名1" in rows[0]["detail"]
