from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
TERMS_PATH = ROOT_DIR / "config" / "report_display_terms.json"


@lru_cache(maxsize=1)
def load_report_display_terms() -> dict[str, Any]:
    try:
        data = json.loads(TERMS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"terms": {}, "value_phrases": {}}
    if not isinstance(data, dict):
        return {"terms": {}, "value_phrases": {}}
    return data


def normalize_report_text(text: str) -> str:
    """Normalize report-facing text without changing structured JSON artifacts."""
    value = str(text or "")
    if not value:
        return value
    terms_data = load_report_display_terms()
    terms = _string_map(terms_data.get("terms"))
    value_phrases = _string_map(terms_data.get("value_phrases"))
    protected, placeholders = _protect_urls(value)
    normalized = _replace_value_phrases(protected, value_phrases)
    normalized = _replace_theme_radar_relation_codes(normalized)
    normalized = _replace_truncation_placeholders(normalized)
    normalized = _replace_terms(normalized, terms)
    normalized = _replace_coverage_pct_labels(normalized)
    normalized = _replace_internal_reference_paths(normalized)
    normalized = _replace_unknown_snake_case(normalized, terms)
    normalized = _cleanup_display_artifacts(normalized)
    normalized = _restore_placeholders(normalized, placeholders)
    return normalized


def display_term(raw: str) -> str:
    key = str(raw or "").strip()
    terms = _string_map(load_report_display_terms().get("terms"))
    return terms.get(key, _humanize_snake_case(key))


def display_field_label(raw: str) -> str:
    """Return a user-facing label for an internal field name."""
    key = str(raw or "").strip()
    if not key:
        return ""
    return display_term(key)


def display_value(raw: Any) -> str:
    """Return a user-facing value without leaking low-level status tokens."""
    if raw is True:
        return "是"
    if raw is False:
        return "否"
    if raw is None:
        return "無"
    text = str(raw).strip()
    if not text:
        return ""
    return normalize_report_text(text)


def display_source_level(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return "來源層級未標示"
    normalized = text.replace("Level 1", "L1_official").replace("Level 2", "L2_media").replace("Level 3", "L3").replace("Level 4", "L4_social")
    return display_term(normalized)


def display_provider(raw: str) -> str:
    text = str(raw or "").strip()
    if not text or text == "unknown":
        return "來源工具未標示"
    return display_term(text)


def display_provider_detail(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    text = re.sub(r"\[L1[_ ]official\s+([^\]]+)\]", r"[官方來源 \1]", text, flags=re.I)
    text = re.sub(r"\[L2[_ ]media\s+([^\]]+)\]", r"[媒體來源 \1]", text, flags=re.I)
    parts = []
    for chunk in re.split(r"\s*;\s*", text):
        if not chunk:
            continue
        chunk = re.sub(r"\bquery\s*[:=]\s*", "搜尋詞：", chunk, flags=re.I)
        chunk = re.sub(r"\btask\s*[:=]\s*", "搜尋任務：", chunk, flags=re.I)
        chunk = re.sub(r"\bsearch\s+depth\s*[:=]\s*", "搜尋深度：", chunk, flags=re.I)
        if "=" in chunk and re.fullmatch(r"[A-Za-z0-9_. -]+", chunk.split("=", 1)[0].strip()):
            key, value = chunk.split("=", 1)
            parts.append(f"{display_field_label(key)}：{display_value(value)}")
        else:
            parts.append(display_value(chunk))
    return "；".join(part for part in parts if part)


def _string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items() if str(k)}


def _replace_value_phrases(text: str, phrases: dict[str, str]) -> str:
    result = text
    for raw, label in sorted(phrases.items(), key=lambda item: len(item[0]), reverse=True):
        if "=" not in raw:
            continue
        key, expected = raw.split("=", 1)
        key_pattern = re.escape(key).replace(r"\.", r"\s*\.\s*")
        expected_pattern = re.escape(expected)
        pattern = re.compile(rf"`?\b{key_pattern}\b`?\s*=\s*`?\b{expected_pattern}\b`?", re.I)
        result = pattern.sub(label, result)
    return result


def _replace_terms(text: str, terms: dict[str, str]) -> str:
    result = text
    for raw, label in sorted(terms.items(), key=lambda item: len(item[0]), reverse=True):
        pattern = _term_pattern(raw)
        result = pattern.sub(label, result)
    return result


def _replace_internal_reference_paths(text: str) -> str:
    result = text

    def evidence_repl(match: re.Match[str]) -> str:
        index = int(match.group(1)) + 1
        return f"共用證據包第 {index} 筆"

    result = re.sub(
        r"共用證據包\.(?:news|items|sources|events|evidence)\[(\d+)\]",
        evidence_repl,
        result,
        flags=re.I,
    )
    result = re.sub(r"\bsource[_ ]ids?\b\s*[:=]\s*", "來源編號：", result, flags=re.I)
    return result


def _replace_coverage_pct_labels(text: str) -> str:
    """Rewrite mixed English coverage labels that models often echo in reports."""
    result = text
    replacements = [
        (r"\bfinancial\s+validation\s+coverage\s+pct\b", "財務驗證覆蓋率"),
        (r"\brevenue\s+exposure\s+coverage\s+pct\b", "營收曝險覆蓋率"),
        (r"\bchip\s+validation\s+coverage\s+pct\b", "籌碼驗證覆蓋率"),
        (r"\btheme\s+mapping\s+coverage\s+pct\b", "題材映射覆蓋率"),
        (r"(?:\btheme\s+|題材\s*)?mapping\s+coverage\s+pct\b", "題材映射覆蓋率"),
        (r"\bcompany\s+relation\b[^\n|]{0,30}\bcoverage\s+pct\b", "公司關聯證據覆蓋率"),
        (r"\bcustomer\s+coverage\s+pct\b", "客戶資料覆蓋率"),
        (r"\bsupply\s+chain\b[^\n|]{0,40}\bcoverage\s+pct\b", "供應鏈層級覆蓋率"),
        (r"(?<=覆蓋率)\s+pct\b", ""),
    ]
    for pattern, replacement in replacements:
        result = re.sub(pattern, replacement, result, flags=re.I)
    return result


def _replace_theme_radar_relation_codes(text: str) -> str:
    """Translate compact theme-radar relation codes in user-facing report text."""
    result = text
    replacements = [
        (r"\bL1\s*official\b", "官方來源"),
        (r"\bL2\s*media\b", "媒體來源"),
        (r"\bL1\s*級", "官方一級來源"),
        (r"\bL2\s*級", "媒體或市場二級來源"),
        (r"\bL1\s*（", "官方一級來源（"),
        (r"\bL2\s*（", "媒體或市場二級來源（"),
        (r"待補\s*L1\s*證據", "待補官方一級來源證據"),
        (r"進行\s*L1\s*驗證", "進行官方一級來源驗證"),
        (r"\bL1\s*驗證", "官方一級來源驗證"),
        (r"\bC\s*/\s*\(\s*V\s*\+\s*C\s*\)", "候選占比"),
        (r"\bV\s*\+\s*C\b", "已驗證加候選"),
        (r"B\s*級中\s*V\s*數最高", "B級中已驗證數量最高"),
        (r"\bV\s*數\b", "已驗證數量"),
        (r"無高\s*V\s*主題對應", "沒有高已驗證度題材對應"),
        (r"高\s*V", "高已驗證度"),
        (r"\bV\s*仍須以\s*L1\s*補強", "已驗證關聯仍須以官方或一級來源補強"),
        (r"候選股（C）", "候選股"),
        (r"已驗證股（V）", "已驗證股"),
        (r"\bV\s*/\s*C\s*/\s*I\b", "已驗證／候選／推論"),
        (r"\bV\s*/\s*C\b", "已驗證／候選"),
        (r"\bV\s+占比", "已驗證占比"),
        (r"\bC\s+占比", "候選占比"),
        (r"\bI\s+占比", "推論占比"),
        (r"\bV\s*=\s*([0-9]+)\b", r"已驗證數量為 \1"),
        (r"\bC\s*=\s*([0-9]+)\b", r"候選數量為 \1"),
        (r"\bI\s*=\s*([0-9]+)\b", r"推論數量為 \1"),
        (r"\bC\s*≥\s*V\s*×\s*2\b", "候選數量至少是已驗證的兩倍"),
        (r"\bC\s*>=\s*V\s*\*\s*2\b", "候選數量至少是已驗證的兩倍"),
        (r"\bC\s*≥\s*V\b", "候選數量高於已驗證數量"),
        (r"\bV\s+級\b", "已驗證級"),
        (r"\bC\s+級\b", "候選級"),
        (r"\bI\s+級\b", "推論級"),
        (r"\bTier\s+A\b", "A級"),
        (r"\bTier\s+B\b", "B級"),
        (r"\bTier\s+C\b", "C級"),
    ]
    for pattern, replacement in replacements:
        result = re.sub(pattern, replacement, result, flags=re.I)
    result = re.sub(r"\bV\s+(\d+)", r"已驗證 \1", result)
    result = re.sub(r"\bC\s+(\d+)", r"候選 \1", result)
    result = re.sub(r"\bI\s+(\d+)", r"推論 \1", result)
    return result


def _replace_truncation_placeholders(text: str) -> str:
    result = text
    placeholders = {
        "<list truncated>": "清單已精簡，完整明細保存在 JSON 附錄",
        "<dict truncated>": "欄位已精簡，完整明細保存在 JSON 附錄",
        "<truncated>": "內容已精簡，完整資料保存在 JSON 附錄",
        "&lt;list truncated&gt;": "清單已精簡，完整明細保存在 JSON 附錄",
        "&lt;dict truncated&gt;": "欄位已精簡，完整明細保存在 JSON 附錄",
        "&lt;truncated&gt;": "內容已精簡，完整資料保存在 JSON 附錄",
    }
    for raw, label in placeholders.items():
        result = result.replace(raw, label)
    return result


def _cleanup_display_artifacts(text: str) -> str:
    result = text
    result = result.replace("全市場 市場題材雷達報告", "全市場題材雷達報告")
    result = result.replace("`清單已精簡，完整明細保存在 JSON 附錄`", "清單已精簡，完整明細保存在 JSON 附錄")
    result = result.replace("`欄位已精簡，完整明細保存在 JSON 附錄`", "欄位已精簡，完整明細保存在 JSON 附錄")
    result = result.replace("`內容已精簡，完整資料保存在 JSON 附錄`", "內容已精簡，完整資料保存在 JSON 附錄")
    result = re.sub(r"\bA/B/C\b", "A級／B級／C級", result)
    result = re.sub(r"\bA/B\b", "A級／B級", result)
    result = re.sub(r"\b24h\b", "近24小時", result, flags=re.IGNORECASE)
    result = re.sub(r"\b7d\b", "近7日", result, flags=re.IGNORECASE)
    result = result.replace(" vs ", " 相對於 ")
    result = re.sub(r"(覆蓋率)\s*=\s*([0-9.]+%?)", r"\1為 \2", result)
    result = result.replace("`、", "、").replace("`，", "，").replace("`。", "。")
    result = re.sub(r"([0-9%％])`", r"\1", result)
    result = re.sub(r"`([，。、；：）])", r"\1", result)
    result = re.sub(r"([（(])`", r"\1", result)
    result = re.sub(r"\b來源編號\s*=\s*", "來源編號：", result)
    return result


def _term_pattern(raw: str) -> re.Pattern[str]:
    escaped = re.escape(raw).replace(r"\.", r"\s*\.\s*")
    if re.fullmatch(r"[A-Za-z0-9_.-]+", raw):
        return re.compile(rf"`?\b{escaped}\b`?")
    return re.compile(re.escape(raw))


def _replace_unknown_snake_case(text: str, terms: dict[str, str]) -> str:
    pattern = re.compile(r"`?\b([a-z][a-z0-9]+(?:_[a-z0-9]+){1,})\b`?")

    def repl(match: re.Match[str]) -> str:
        token = match.group(1)
        if token in terms:
            return terms[token]
        if _looks_like_path_or_id(token):
            return match.group(0)
        return _humanize_snake_case(token)

    return pattern.sub(repl, text)


def _humanize_snake_case(value: str) -> str:
    token = str(value or "").strip("` ")
    if not token:
        return token
    known_acronyms = {
        "ai": "AI",
        "mosfet": "MOSFET",
        "mlcc": "MLCC",
        "cpo": "CPO",
        "hbm": "HBM",
        "pcb": "PCB",
        "abf": "ABF",
        "bbu": "BBU",
        "gpu": "GPU",
        "asic": "ASIC",
    }
    words = []
    for part in token.split("_"):
        lower = part.lower()
        words.append(known_acronyms.get(lower, part))
    return " ".join(words)


def _looks_like_path_or_id(token: str) -> bool:
    if token.startswith(("http_", "https_", "file_")):
        return True
    if token.endswith(("_id", "_path", "_url")):
        return True
    if len(token) > 80:
        return True
    return False


def _protect_urls(text: str) -> tuple[str, dict[str, str]]:
    placeholders: dict[str, str] = {}

    def repl(match: re.Match[str]) -> str:
        key = f"__REPORT_DISPLAY_URL_{len(placeholders)}__"
        placeholders[key] = match.group(0)
        return key

    protected = re.sub(r"https?://[^\s)>\]]+", repl, text)
    return protected, placeholders


def _restore_placeholders(text: str, placeholders: dict[str, str]) -> str:
    result = text
    for key, value in placeholders.items():
        result = result.replace(key, value)
    return result
