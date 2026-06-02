"""Small normalization helpers for formal topic data files."""
from __future__ import annotations

from typing import Any


_SIMPLIFIED_TO_TRADITIONAL = {
    "网络": "網路",
    "资料": "資料",
    "题材": "題材",
    "风险": "風險",
    "营收": "營收",
    "占比": "佔比",
    "客户": "客戶",
    "供应链": "供應鏈",
    "边缘": "邊緣",
    "解决方案": "解決方案",
    "车联网": "車聯網",
    "机关": "機關",
    "交换器": "交換器",
    "电子零组件业": "電子零組件業",
    "电子": "電子",
    "零组件": "零組件",
    "位于": "位於",
    "基础": "基礎",
    "建设": "建設",
    "设备": "設備",
    "候选": "候選",
    "证据": "證據",
    "支撑": "支撐",
    "须": "須",
    "对待": "對待",
    "规格": "規格",
    "竞争": "競爭",
    "名单": "名單",
    "认证": "認證",
    "出货": "出貨",
    "产业": "產業",
    "逻辑": "邏輯",
    "推断": "推斷",
    "驱动": "驅動",
    "厂商": "廠商",
    "厂": "廠",
    "高速传输": "高速傳輸",
    "云": "雲",
    "资料中心": "資料中心",
    "无线": "無線",
    "通讯": "通訊",
    "模组": "模組",
    "网通": "網通",
    "广达": "廣達",
    "纬创": "緯創",
    "启碁": "啟碁",
    "华电联网": "華電聯網",
    "华": "華",
    "数位": "數位",
    "识别": "識別",
    "预算": "預算",
    "项目": "專案",
    "专案": "專案",
    "标准": "標準",
    "标准化": "標準化",
    "获利": "獲利",
    "能力": "能力",
    "务": "務",
    "应用": "應用",
    "营运商": "營運商",
    "资讯": "資訊",
    "发包": "發包",
    "进度": "進度",
    "处": "處",
    "验证": "驗證",
    "维持": "維持",
    "持续": "持續",
    "连接器": "連接器",
    "电源": "電源",
    "电力": "電力",
    "电": "電",
    "线": "線",
    "个股": "個股",
    "龙头": "龍頭",
    "显": "顯",
    "专": "專",
    "与": "與",
    "为": "為",
    "这": "這",
    "将": "將",
    "项": "項",
    "发": "發",
    "现": "現",
    "时": "時",
    "后": "後",
    "应": "應",
    "从": "從",
    "过": "過",
    "当": "當",
    "会": "會",
    "间": "間",
    "实": "實",
    "则": "則",
}


def to_traditional_text(value: Any) -> Any:
    """Normalize common simplified Chinese fragments in strings."""
    if not isinstance(value, str):
        return value
    text = value
    for simplified, traditional in sorted(_SIMPLIFIED_TO_TRADITIONAL.items(), key=lambda item: len(item[0]), reverse=True):
        text = text.replace(simplified, traditional)
    return text


def normalize_text_tree(value: Any) -> Any:
    """Recursively normalize strings in dictionaries and lists."""
    if isinstance(value, str):
        return to_traditional_text(value)
    if isinstance(value, list):
        return [normalize_text_tree(item) for item in value]
    if isinstance(value, dict):
        return {key: normalize_text_tree(item) for key, item in value.items()}
    return value


def normalize_string_list(value: Any) -> list[str]:
    """Return a clean string list, flattening AI field-value wrappers."""
    items: list[Any]
    if value is None:
        items = []
    elif isinstance(value, list):
        items = value
    elif isinstance(value, tuple):
        items = list(value)
    else:
        items = [value]

    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        raw_values: list[Any]
        if isinstance(item, dict):
            raw = item.get("value")
            if isinstance(raw, (list, tuple)):
                raw_values = list(raw)
            elif raw not in (None, "", []):
                raw_values = [raw]
            else:
                raw_values = []
        else:
            raw_values = [item]
        for raw_value in raw_values:
            text = to_traditional_text(str(raw_value or "").strip())
            if text and text not in seen:
                result.append(text)
                seen.add(text)
    return result
