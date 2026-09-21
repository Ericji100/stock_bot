from __future__ import annotations


# The original four setups remain valid methods, but they are no longer the only
# executable output of the course-integrated decision chain.
ORIGINAL_FOUR_METHODS = {
    "OPENING_RANGE_BREAKOUT_RETEST",
    "TREND_PULLBACK_CONTINUATION",
    "INTRADAY_COMPRESSION_BREAKOUT",
    "FALSE_BREAK_REVERSAL",
}

QUADRANT_METHODS = {
    "QUADRANT_Q1_MOMENTUM_BREAKOUT",
    "QUADRANT_Q2_COUNTER_PULLBACK",
    "QUADRANT_Q3_QUALIFIED_BOX",
    "QUADRANT_Q4_TREND_ZONE",
}

BOX_METHODS = {
    "BOX_QUALIFIED_BOUNDARY_BREAK",
    "BOX_FALSE_BREAK_REVERSAL",
}

DOW_METHODS = {
    "DOW_SECONDARY_RETEST",
    "DOW_TYPE2_REVERSAL",
    "DOW_TYPE3_REVERSAL",
}

LEFT_RIGHT_METHODS = {
    "LEFT_LEFT_REVERSAL",
    "LEFT_RIGHT_REVERSAL",
    "RIGHT_LEFT_RETEST",
    "RIGHT_RIGHT_CONTINUATION",
}

TAIJI_METHODS = {
    "TAIJI_COPY_AFTER_CORRECTION",
    "TAIJI_REANCHOR_AFTER_FAILURE",
}

YIZHI_METHODS = {
    "YIZHI_CENTRIFUGAL",
    "YIZHI_DRAGON_EARLY",
    "YIZHI_LIFE_DEATH_GATE",
}

STRATEGY_METHODS = {
    "NONE",
    *ORIGINAL_FOUR_METHODS,
    *QUADRANT_METHODS,
    *BOX_METHODS,
    *DOW_METHODS,
    *LEFT_RIGHT_METHODS,
    *TAIJI_METHODS,
    *YIZHI_METHODS,
}

EXECUTION_STYLES = {
    "NOT_APPLICABLE",
    "STANDARD_STRUCTURAL",
    "MOMENTUM_STRIKE",
    "WOODPECKER",
    "ONE_ROUND",
}

STRATEGY_METHOD_LABELS_ZH = {
    "NONE": "尚未選定主控戰法",
    "OPENING_RANGE_BREAKOUT_RETEST": "四型態－開盤區間突破回踩",
    "TREND_PULLBACK_CONTINUATION": "四型態－趨勢拉回延續",
    "INTRADAY_COMPRESSION_BREAKOUT": "四型態－盤中壓縮突破",
    "FALSE_BREAK_REVERSAL": "四型態－假突破／假跌破反轉",
    "QUADRANT_Q1_MOMENTUM_BREAKOUT": "四象限－第一象限順勢動能突破",
    "QUADRANT_Q2_COUNTER_PULLBACK": "四象限－第二象限反向拉回",
    "QUADRANT_Q3_QUALIFIED_BOX": "四象限－第三象限合格箱型",
    "QUADRANT_Q4_TREND_ZONE": "四象限－第四象限順勢區域低接／高空",
    "BOX_QUALIFIED_BOUNDARY_BREAK": "箱型－合格邊界突破",
    "BOX_FALSE_BREAK_REVERSAL": "箱型－邊界假突破反轉",
    "DOW_SECONDARY_RETEST": "道氏－次低點／次高點拉回延續",
    "DOW_TYPE2_REVERSAL": "道氏－Type2 防線反轉",
    "DOW_TYPE3_REVERSAL": "道氏－Type3 跨級防線反轉",
    "LEFT_LEFT_REVERSAL": "左右－左左反轉試單",
    "LEFT_RIGHT_REVERSAL": "左右－左右小級確認",
    "RIGHT_LEFT_RETEST": "左右－右左防線突破後回測",
    "RIGHT_RIGHT_CONTINUATION": "左右－右右延續確認",
    "TAIJI_COPY_AFTER_CORRECTION": "太極－良性修正後複製",
    "TAIJI_REANCHOR_AFTER_FAILURE": "太極－失敗後重新定錨",
    "YIZHI_CENTRIFUGAL": "一之－離心力",
    "YIZHI_DRAGON_EARLY": "一之－一條龍初段",
    "YIZHI_LIFE_DEATH_GATE": "一之－生死門",
}

EXECUTION_STYLE_LABELS_ZH = {
    "NOT_APPLICABLE": "尚未選定執行方式",
    "STANDARD_STRUCTURAL": "標準結構式",
    "MOMENTUM_STRIKE": "動能快速失效式",
    "WOODPECKER": "啄木鳥式快速失效",
    "ONE_ROUND": "一回合式（高風險，須有明確風險預算）",
}


def strategy_family(method: str) -> str:
    if method in ORIGINAL_FOUR_METHODS:
        return "ORIGINAL_FOUR"
    if method in QUADRANT_METHODS:
        return "QUADRANT"
    if method in BOX_METHODS:
        return "BOX"
    if method in DOW_METHODS:
        return "DOW"
    if method in LEFT_RIGHT_METHODS:
        return "LEFT_RIGHT"
    if method in TAIJI_METHODS:
        return "TAIJI"
    if method in YIZHI_METHODS:
        return "YIZHI"
    return "NONE"
