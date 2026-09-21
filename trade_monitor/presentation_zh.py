from __future__ import annotations

import re
from collections.abc import Mapping

from .strategy_catalog import STRATEGY_METHOD_LABELS_ZH


# These are stable machine identifiers used by the persisted schema and state
# machines.  They intentionally remain unchanged internally.  Every string
# shown in the Codex task or Telegram passes through this presentation layer.
USER_VISIBLE_TRANSLATIONS: Mapping[str, str] = {
    # Strategy names are stable machine identifiers in JSON, but they must never
    # leak into the canonical message shown in Codex or Telegram.  NONE keeps the
    # shorter generic translation defined below.
    **{key: value for key, value in STRATEGY_METHOD_LABELS_ZH.items() if key != "NONE"},
    "AGGRESSIVE_CONFIRMED": "積極確認",
    "CONSERVATIVE_CONFIRMED": "保守確認",
    "CENTRIFUGAL_CONFIRMED": "離心力已確認",
    "CENTRIFUGAL_FORMING": "離心力形成中",
    "LIFE_DEATH_GATE_FORMING": "生死門形成中",
    "LIFE_DEATH_GATE_ARMED": "生死門待觸發",
    "TREND_PULLBACK_CONTINUATION": "趨勢拉回延續",
    "OPENING_RANGE_BREAKOUT_RETEST": "開盤區間突破回踩",
    "INTRADAY_COMPRESSION_BREAKOUT": "盤中壓縮突破",
    "FALSE_BREAK_REVERSAL": "假突破／假跌破反轉",
    "OPENING_EVIDENCE_ONLY": "僅採開盤證據",
    "COMBINED_CONFIRMATION": "太極與四象限共同確認",
    "QUADRANT_PRIMARY": "四象限主判讀",
    "TAIJI_PRIMARY": "太極主判讀",
    "YIZHI_OVERRIDE": "一之動能優先判讀",
    "TAIJI_ORDERED": "太極有序結構",
    "YIZHI_MOMENTUM": "一之動能結構",
    "SESSION_OPEN_PENDING": "等待時段開盤",
    "FIRST_ENDPOINT_SAMPLE": "第一個端點取樣",
    "OPENING_EVIDENCE": "開盤證據判讀",
    "STRUCTURE_BUILDING": "結構建立中",
    "SETUP_EVALUATION": "盤中交易機會持續掃描",
    "LATE_OR_RESETTING": "末段或重置中",
    "VERIFIED_CASH": "現貨資料已驗證",
    "FUTURES_PROXY": "期貨代理資料",
    "TYPE_3_DOUBLE_DEFENSE": "Type3 雙防線",
    "TYPE_1": "Type1",
    "TYPE_2": "Type2",
    "TYPE_3": "Type3",
    "HEAD_BOTTOM_SEQUENCE": "頭底序列",
    "OPENING_FIRST_VALID": "開盤第一個有效方向",
    "OPENING_DIRECTION_HOLD": "開盤方向守住",
    "OR_BREAK": "開盤區間突破",
    "OR_OPPOSITE": "開盤區間對側",
    "DEFENSE_BREAK": "防線突破",
    "STRUCTURE_BREAK": "結構突破",
    "SESSION_EXTREME_BREAK": "時段極值突破",
    "SMALL_DEFENSE_BREAK": "小級防線突破",
    "LARGE_DEFENSE_BREAK": "大級防線突破",
    "STRUCTURE_BOUNDARY": "結構邊界",
    "BOUNDARY": "邊界",
    "SESSION_EXTREME": "時段極值",
    "BREAKOUT_RETEST": "突破回測位置",
    "DEFENSE_RETEST": "防線回測位置",
    "NEAR_RESISTANCE": "接近壓力",
    "NEAR_SUPPORT": "接近支撐",
    "SESSION_EXTERNAL": "時段外圍",
    "SESSION_INTERNAL": "時段內部",
    "SESSION_BOUNDARY": "時段邊界",
    "CONTINUATION_STRONGER": "延續方較強",
    "REVERSAL_STRONGER": "反轉方較強",
    "LIFE_DEATH_GATE": "生死門",
    "DRAGON": "一條龍",
    "EXHAUSTION_WARNING": "動能耗竭警告",
    "DRAGON_EARLY": "一條龍初段",
    "DRAGON_MIDDLE": "一條龍中段",
    "DRAGON_LATE": "一條龍末段",
    "A_CANDIDATE": "A級點",
    "B_CANDIDATE": "B級點",
    "C_CANDIDATE": "C級點",
    "NOT_APPLICABLE": "不適用",
    "OBSERVE": "觀察",
    "EXIT_ON_FIRST_VALID_RECOVERY": "首次有效恢復即退出",
    "NOTIFY": "詳細通知",
    "DONT_NOTIFY": "靜默不通知",
    "NO_CHASE": "不宜追價",
    "INVALIDATED": "已失效",
    "REPLACED": "已替換",
    "COMPLETED": "已完成",
    "CONFIRMED": "已確認",
    "FORMING": "形成中",
    "CANDIDATE": "候選",
    "ARMED": "待觸發",
    "ACTIVE": "有效",
    "BROKEN": "已突破／跌破",
    "COOLDOWN_REQUALIFIED": "冷卻後重新合格",
    "SIM_ENTER": "模擬進場",
    "SIM_STOP": "模擬停損",
    "SIM_EXIT": "模擬出場",
    "LOCAL_CONFIRMED": "小級已確認",
    "PAIRED_CONFIRMED": "配對已確認",
    "TRANSITION": "轉換中",
    "UNDEFINED": "尚未定義",
    "UNAVAILABLE": "資料不可用",
    "UNKNOWN": "未知",
    "CAUTION": "警戒",
    "UNQUALIFIED": "不合格",
    "ACCEPTABLE": "可接受",
    "STRONG": "強",
    "WEAK": "弱",
    "FAILED": "失敗",
    "ALIGNED": "一致",
    "SAME_QUADRANT": "同一象限",
    "SMALL_Q1": "小級第一象限",
    "COPY_CORRECTION": "複製與修正",
    "PARTIAL": "部分一致",
    "MISALIGNED": "不一致",
    "CONFLICT": "互相衝突",
    "CONSISTENT": "一致",
    "CHANGING": "正在改變",
    "MIXED": "混合",
    "CLEAN": "乾淨",
    "NOISY": "雜訊偏多",
    "OVERHEATED": "過熱",
    "EXHAUSTED": "耗竭",
    "NORMAL": "正常",
    "SUSPECT": "可疑",
    "MODERATE": "中等",
    "SMALL": "小",
    "LARGE": "大",
    "HIGH": "高",
    "MEDIUM": "中",
    "LOW": "低",
    "BULLISH": "偏多",
    "BEARISH": "偏空",
    "CONDITIONAL": "條件式",
    "NEUTRAL": "中性",
    "BULL": "多方",
    "BEAR": "空方",
    "LONG": "做多",
    "SHORT": "做空",
    "FLAT": "無方向",
    "NONE": "無",
    "PENDING": "等待確認",
    "TRUE_LIKE": "偏真突破",
    "FALSE_LIKE": "偏假突破",
    "SAME": "同向",
    "OPPOSITE": "反向",
    "OPPOSED": "相反",
    "ORDERED": "有序",
    "UNORDERED": "無序",
    "RESETTING": "重置中",
    "INITIAL": "初段",
    "MIDDLE": "中段",
    "LATE": "末段",
    "EARLY": "初段",
    "PULLBACK": "回檔",
    "REBOUND": "反彈",
    "RANGE": "盤整區間",
    "RANGE_EDGE": "區間邊緣",
    "RANGE_MIDDLE": "區間中央",
    "PRIOR_HIGH": "前高",
    "PRIOR_LOW": "前低",
    "DIRECT": "直接讀取",
    "ESTIMATED": "圖面估計",
    "INCREASING": "擴大",
    "DECREASING": "縮小",
    "EXPANDING": "擴張",
    "CONTRACTING": "收斂",
    "UNSTABLE": "不穩定",
    "UNCLEAR": "不清楚",
    "FRESH": "資料新鮮",
    "DECAYING": "有效性衰退",
    "STALE": "資料過期",
    "ONLY_LARGE": "僅大級有效",
    "ONLY_SMALL": "僅小級有效",
    "LEFT_LEFT": "左左成熟",
    "LEFT_RIGHT": "左右成熟",
    "RIGHT_LEFT": "右左成熟",
    "RIGHT_RIGHT": "右右成熟",
    "ANCHOR_1": "第一段定錨",
    "CORRECTION_2": "第二段修正",
    "COPY_3": "第三段複製",
    "CORRECTION_4": "第四段修正",
    "COPY_5": "第五段複製",
    "POST_5": "第五段之後",
    "ANCHOR": "定錨段",
    "CORRECTION": "修正段",
    "COPY": "複製段",
    "POST_TREND": "趨勢後段",
    "STRONGER": "較強",
    "SIMILAR": "相近",
    "WEAKER": "較弱",
    "FRESH": "新鮮",
    "GOLD": "黃金龍",
    "K_GOLD": "Ｋ黃金龍",
    "EARTH": "地龍",
    "CROOKED": "歪龍",
    "STABLE": "穩定",
    "MICRO": "微小級",
    "STRUCTURAL": "結構級",
    "BALANCED": "勢均力敵",
    "Q1": "第一象限",
    "Q2": "第二象限",
    "Q3": "第三象限",
    "Q4": "第四象限",
    "DETAIL": "細節圖",
    "OVERVIEW": "全局圖",
    "CHROME_CONTROL_OVERVIEW": "瀏覽器控制全局圖",
}

_PHRASE_TRANSLATIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"較低(?:的)?高點"), "次高點"),
    (re.compile(r"較高(?:的)?低點"), "次低點"),
    (re.compile(r"次高低"), "次高點／次低點"),
    (re.compile(r"次高(?!點)"), "次高點"),
    (re.compile(r"次低(?!點)"), "次低點"),
    (re.compile(r"(?i)X\s*流程"), "整合決策流程"),
    (re.compile(r"(?i)家族\s*DNA"), "家族結構特徵"),
    (re.compile(r"(?i)\bDNA\b"), "結構特徵"),
    (re.compile(r"(?i)S\s*[／/]\s*S\s*[／/]\s*T\s*[／/]\s*V"), "箱型四面向品質"),
    (re.compile(r"(?i)\bsetup\b"), "進場型態"),
    (re.compile(r"(?i)\bshadow\b"), "影子驗證"),
    (re.compile(r"(?i)\bcanonical\s+message\b"), "標準訊息"),
    (re.compile(r"(?i)\bengine\s+mode\b"), "判讀模式"),
    (re.compile(r"(?i)\bsession\b"), "交易時段"),
    (re.compile(r"(?i)\bproxy\b"), "替代判斷"),
    (re.compile(r"(?i)\bOHLC\b"), "開高低收"),
    (re.compile(r"(?i)\bDay\s+High\b"), "當日高點"),
    (re.compile(r"(?i)\bDay\s+Low\b"), "當日低點"),
    (re.compile(r"(?i)\bType\s*[_-]?\s*1\b"), "Type1"),
    (re.compile(r"(?i)\bType\s*[_-]?\s*2\b"), "Type2"),
    (re.compile(r"(?i)\bType\s*[_-]?\s*3\b"), "Type3"),
    (re.compile(r"(?i)\bOR\s*5\b"), "開盤五分鐘區間"),
    (re.compile(r"(?i)\bOR\s*15\b"), "開盤十五分鐘區間"),
    (re.compile(r"二十一期(?:\s*均價)?"), "均價21"),
    (re.compile(r"一百零五期(?:\s*均價)?"), "均價105"),
    (re.compile(r"(?i)\bMA\s*21\b"), "均價21"),
    (re.compile(r"(?i)\bMA\s*105\b"), "均價105"),
    (re.compile(r"(?i)\bMACD\b"), "指數平滑異同移動平均線"),
    (re.compile(r"(?i)\bKD\b"), "隨機指標"),
    (re.compile(r"(?i)\bDH\b"), "當日高點"),
    (re.compile(r"(?i)\bDL\b"), "當日低點"),
    (re.compile(r"(?i)(?<![A-Za-z0-9])1R(?![A-Za-z0-9])"), "一倍初始風險"),
    (re.compile(r"(?i)(?<![A-Za-z0-9])1\.5R(?![A-Za-z0-9])"), "一點五倍初始風險"),
)

_TOKEN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(" + "|".join(
        re.escape(token) for token in sorted(USER_VISIBLE_TRANSLATIONS, key=len, reverse=True)
    ) + r")(?![A-Za-z0-9_])"
)

_CHINESE_NUMBER_DIGITS: Mapping[str, int] = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "兩": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CLOCK_EXPRESSION_PATTERN = re.compile(
    r"(?P<period>凌晨|早上|上午|中午|下午|傍晚|晚上)?\s*"
    r"(?P<hour>\d{1,2}|[零〇一二兩三四五六七八九十]{1,3})\s*[時點]\s*"
    r"(?P<minute>\d{1,2}|[零〇一二兩三四五六七八九十]{1,3})\s*分"
)


def _parse_clock_number(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    if "十" in value:
        tens_text, ones_text = value.split("十", 1)
        tens = 1 if not tens_text else _CHINESE_NUMBER_DIGITS.get(tens_text)
        ones = 0 if not ones_text else _CHINESE_NUMBER_DIGITS.get(ones_text)
        if tens is None or ones is None:
            return None
        return tens * 10 + ones
    digits = [_CHINESE_NUMBER_DIGITS.get(char) for char in value]
    if not digits or any(digit is None for digit in digits):
        return None
    return int("".join(str(digit) for digit in digits))


def normalize_user_visible_times(text: str) -> str:
    """Render prose clock expressions as compact 24-hour HH:MM times."""

    def replace(match: re.Match[str]) -> str:
        hour = _parse_clock_number(match.group("hour"))
        minute = _parse_clock_number(match.group("minute"))
        if hour is None or minute is None or not (0 <= hour <= 23 and 0 <= minute <= 59):
            return match.group(0)

        period = match.group("period")
        if period in {"下午", "傍晚", "晚上", "中午"} and hour < 12:
            hour += 12
        elif period in {"凌晨", "早上", "上午"} and hour == 12:
            hour = 0
        return f"{hour:02d}:{minute:02d}"

    return _CLOCK_EXPRESSION_PATTERN.sub(replace, text)


def localize_user_text(text: str) -> str:
    """Translate stable machine identifiers before text reaches either channel."""
    localized = normalize_user_visible_times(text)
    for pattern, replacement in _PHRASE_TRANSLATIONS:
        localized = pattern.sub(replacement, localized)
    return _TOKEN_PATTERN.sub(lambda match: USER_VISIBLE_TRANSLATIONS[match.group(1)], localized)


REVERSAL_TYPE_EXPLANATIONS: Mapping[str, str] = {
    "1": "小級先反向，大級防線尚未失守",
    "2": "原方向防線失守，反向結構完成確認",
    "3": "強勢跨級急轉，連續突破大小級防線",
}
_REVERSAL_TYPE_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9_])Type\s*[_-]?\s*([123])(?![A-Za-z0-9_])(?:（[^）]*）)?"
)


def annotate_reversal_types(text: str) -> str:
    """Explain the first occurrence of each Type label in one canonical message."""
    seen: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        number = match.group(1)
        label = f"Type{number}"
        if number in seen:
            return label
        seen.add(number)
        return f"{label}（{REVERSAL_TYPE_EXPLANATIONS[number]}）"

    return _REVERSAL_TYPE_PATTERN.sub(replace, text)


def reversal_type_definitions() -> tuple[str, str, str]:
    return (
        "Type1（小級先反向，大級防線尚未失守）：屬提早警告，不自動確認大級翻向。",
        "Type2（原方向防線失守，反向結構完成確認）：是一般有效方向翻轉的主要定義。",
        "Type3（強勢跨級急轉，連續突破大小級防線）：屬高風險急變，不追第一段。",
    )


_OPERATIONAL_ERROR_TRANSLATIONS: Mapping[str, str] = {
    "align_second_invalid": "每分鐘啟動秒數設定無效",
    "automation_invalid": "排程設定無效",
    "automation_mismatch": "排程識別不一致",
    "automation_missing": "找不到排程設定",
    "automation_status_invalid": "排程狀態無效",
    "capture_cleanup_unsafe": "圖表暫存清理範圍不安全",
    "capture_output_invalid": "圖表擷取輸出無效",
    "capture_path_invalid": "圖表擷取路徑無效",
    "capture_time_invalid": "圖表擷取時間無效",
    "clock_timezone_missing": "系統時間缺少時區",
    "codex_executable_missing": "找不到分析程式",
    "codex_failed": "分析程序執行失敗",
    "codex_output_invalid": "分析輸出格式無效",
    "codex_output_inconsistent": "分析結構前後不一致",
    "codex_start_failed": "分析程序無法啟動",
    "codex_timeout": "分析程序逾時",
    "config_invalid": "本機監控設定無效",
    "config_missing": "找不到本機監控設定",
    "cycles_invalid": "監控輪數設定無效",
    "dual_scale_config_invalid": "雙尺度圖表設定無效",
    "dual_scale_detail_not_ready": "細節圖尚未恢復",
    "dual_scale_wait_invalid": "雙尺度圖表等待狀態無效",
    "prompt_hash_mismatch": "分析規則雜湊不一致",
    "scheduler_already_running": "本機監控已有執行個體",
    "structured_market_data_config_invalid": "結構化行情設定無效",
    "local_scheduler_failed": "本機監控發生未分類錯誤",
}


def operational_error_label(error_code: str) -> str:
    return _OPERATIONAL_ERROR_TRANSLATIONS.get(error_code, "本機監控發生未分類錯誤")
