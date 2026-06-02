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
    normalized = _replace_terms(normalized, terms)
    normalized = _replace_unknown_snake_case(normalized, terms)
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
    parts = []
    for chunk in re.split(r"\s*;\s*", text):
        if not chunk:
            continue
        if "=" in chunk:
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
