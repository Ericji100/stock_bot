"""User-facing source text cleanup helpers."""
from __future__ import annotations

import re
from html import unescape
from typing import Any
from urllib.parse import urlparse


def article_text_sections(text: str) -> tuple[str, str]:
    """Separate explicitly labelled related articles; retain both, never infer relevance."""
    marker = re.search(r'(?m)^\s*(?:#{1,6}\s*)?(?:延伸閱讀|相關新聞|更多相關新聞|推薦閱讀|Related articles|Read next)\s*[:：]?\s*$', text, re.I)
    return (text[:marker.start()].rstrip(), text[marker.start():]) if marker else (text, '')


_MOJIBAKE_MARKERS = (
    "é",
    "è",
    "ç",
    "å",
    "æ",
    "ä",
    "¤",
    "¦",
    "‡",
    "‰",
    "€",
    "“",
    "”",
    "–",
    "�",
)


def clean_source_text(value: Any) -> str:
    """Repair common UTF-8 mojibake in source titles/snippets."""
    text = unescape(str(value or "")).strip()
    if not text:
        return ""
    repaired = _repair_utf8_mojibake(text)
    return _normalize_spaces(repaired)


def clean_source_title(value: Any, url: str = "") -> str:
    """Isolate script/style dumps misidentified as titles, not financial prose."""
    title = str(value or "")
    code_markers = re.findall(r"window\[|function\s*\(|font-family\s*:|box-sizing\s*:|@media\b|\.cm-", title)
    if len(title) > 2000 and len(code_markers) >= 3:
        host = urlparse(url).netloc or "來源網站"
        return f"來源網頁（{host}）"
    return title


def _repair_utf8_mojibake(text: str) -> str:
    if not any(marker in text for marker in _MOJIBAKE_MARKERS):
        return text
    candidates: list[str] = []
    for encoding in ("latin1", "cp1252"):
        try:
            candidates.append(text.encode(encoding).decode("utf-8"))
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        try:
            candidates.append(text.encode(encoding, errors="ignore").decode("utf-8", errors="ignore"))
        except UnicodeEncodeError:
            pass
    try:
        raw = _mixed_single_byte_bytes(text)
        candidates.append(raw.decode("utf-8"))
    except UnicodeDecodeError:
        pass
    try:
        candidates.append(_mixed_single_byte_bytes(text).decode("utf-8", errors="ignore"))
    except UnicodeDecodeError:
        pass
    candidates = [candidate for candidate in candidates if candidate and candidate != text]
    if not candidates:
        return text
    return max(candidates, key=lambda candidate: _text_quality_score(candidate, text))


def _mixed_single_byte_bytes(text: str) -> bytes:
    output = bytearray()
    for char in text:
        codepoint = ord(char)
        if codepoint <= 0xFF:
            output.append(codepoint)
            continue
        try:
            output.extend(char.encode("cp1252"))
        except UnicodeEncodeError:
            output.extend(char.encode("utf-8"))
    return bytes(output)


def _text_quality_score(candidate: str, original: str) -> int:
    candidate_bad = sum(candidate.count(marker) for marker in _MOJIBAKE_MARKERS)
    original_bad = sum(original.count(marker) for marker in _MOJIBAKE_MARKERS)
    cjk_count = sum(1 for char in candidate if "\u4e00" <= char <= "\u9fff")
    replacement_penalty = candidate.count("�") * 3
    return (cjk_count * 5) + ((original_bad - candidate_bad) * 3) - replacement_penalty


def _normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
