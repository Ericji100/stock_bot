from __future__ import annotations

from dataclasses import dataclass

from telegram import MessageEntity


STOCK_MARK_START = "[[STOCK]]"
STOCK_MARK_END = "[[/STOCK]]"


@dataclass(frozen=True)
class TelegramTextChunk:
    text: str
    entities: list[MessageEntity]


@dataclass(frozen=True)
class _StockSpan:
    start: int
    end: int


@dataclass(frozen=True)
class _FormatSpan:
    start: int
    end: int
    entity_types: tuple[str, ...]


def mark_stock_text(text: object) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    if STOCK_MARK_START in value or STOCK_MARK_END in value:
        return value
    return f"{STOCK_MARK_START}{value}{STOCK_MARK_END}"


def strip_stock_markers(text: str) -> str:
    clean, _ = _strip_stock_markers_with_spans(str(text or ""))
    return clean


def prepare_telegram_text(text: str) -> TelegramTextChunk:
    clean, spans = _strip_formatting_with_spans(str(text or ""))
    return TelegramTextChunk(clean, _entities_for_range(clean, spans, 0, len(clean)))


def prepare_telegram_chunks(text: str, limit: int = 4000) -> list[TelegramTextChunk]:
    if limit <= 0:
        raise ValueError("Telegram chunk limit must be positive")

    clean, spans = _strip_formatting_with_spans(str(text or ""))
    if len(clean) <= limit:
        return [TelegramTextChunk(clean, _entities_for_range(clean, spans, 0, len(clean)))]

    chunks: list[TelegramTextChunk] = []
    start = 0
    total_length = len(clean)
    while start < total_length:
        if total_length - start <= limit:
            end = total_length
        else:
            split_at = clean.rfind("\n", start, start + limit + 1)
            if split_at >= start:
                split_at += 1
            if split_at <= start:
                split_at = start + limit
            end = _adjust_split_for_format_span(start, split_at, spans, limit)

        if end <= start:
            end = min(start + limit, total_length)
        chunk_text = clean[start:end]
        chunks.append(
            TelegramTextChunk(
                chunk_text,
                _entities_for_range(clean, spans, start, end),
            )
        )
        start = end

    return chunks or [TelegramTextChunk(clean, _entities_for_range(clean, spans, 0, len(clean)))]


def _strip_stock_markers_with_spans(text: str) -> tuple[str, list[_StockSpan]]:
    clean_parts: list[str] = []
    spans: list[_StockSpan] = []
    index = 0
    clean_length = 0

    while index < len(text):
        start = text.find(STOCK_MARK_START, index)
        if start == -1:
            tail = text[index:]
            clean_parts.append(tail)
            clean_length += len(tail)
            break

        before = text[index:start]
        clean_parts.append(before)
        clean_length += len(before)

        marked_start = start + len(STOCK_MARK_START)
        end = text.find(STOCK_MARK_END, marked_start)
        if end == -1:
            remainder = text[start:]
            clean_parts.append(remainder)
            clean_length += len(remainder)
            break

        marked_text = text[marked_start:end]
        span_start = clean_length
        clean_parts.append(marked_text)
        clean_length += len(marked_text)
        if marked_text:
            spans.append(_StockSpan(span_start, clean_length))
        index = end + len(STOCK_MARK_END)

    return "".join(clean_parts), spans


def _strip_formatting_with_spans(text: str) -> tuple[str, list[_FormatSpan]]:
    stock_clean, stock_spans = _strip_stock_markers_with_spans(text)
    clean_parts: list[str] = []
    bold_spans: list[_FormatSpan] = []
    removed_ranges: list[tuple[int, int]] = []
    index = 0
    clean_length = 0

    while index < len(stock_clean):
        start = stock_clean.find("**", index)
        if start == -1:
            clean_parts.append(stock_clean[index:])
            break

        end = stock_clean.find("**", start + 2)
        if end == -1:
            clean_parts.append(stock_clean[index:])
            break

        content = stock_clean[start + 2:end]
        if not content:
            clean_parts.append(stock_clean[index:end + 2])
            clean_length += end + 2 - index
            index = end + 2
            continue

        before = stock_clean[index:start]
        clean_parts.append(before)
        clean_length += len(before)
        span_start = clean_length
        clean_parts.append(content)
        clean_length += len(content)
        bold_spans.append(_FormatSpan(span_start, clean_length, (MessageEntity.BOLD,)))
        removed_ranges.extend(((start, start + 2), (end, end + 2)))
        index = end + 2

    clean = "".join(clean_parts)
    formatted_stock_spans = [
        _FormatSpan(
            _position_after_removals(span.start, removed_ranges),
            _position_after_removals(span.end, removed_ranges),
            (MessageEntity.BOLD, MessageEntity.UNDERLINE),
        )
        for span in stock_spans
    ]
    return clean, sorted(formatted_stock_spans + bold_spans, key=lambda span: (span.start, span.end))


def _position_after_removals(position: int, removed_ranges: list[tuple[int, int]]) -> int:
    removed_before = sum(max(0, min(position, end) - start) for start, end in removed_ranges if start < position)
    return position - removed_before


def _entities_for_range(clean: str, spans: list[_FormatSpan], start: int, end: int) -> list[MessageEntity]:
    entities: list[MessageEntity] = []
    seen: set[tuple[str, int, int]] = set()
    chunk_text = clean[start:end]
    for span in spans:
        entity_start = max(span.start, start)
        entity_end = min(span.end, end)
        if entity_start >= entity_end:
            continue
        local_start = entity_start - start
        local_end = entity_end - start
        offset = _telegram_utf16_length(chunk_text[:local_start])
        length = _telegram_utf16_length(chunk_text[local_start:local_end])
        for entity_type in span.entity_types:
            key = (entity_type, offset, length)
            if key in seen:
                continue
            seen.add(key)
            entities.append(MessageEntity(type=entity_type, offset=offset, length=length))
    return entities


def _adjust_split_for_format_span(start: int, split_at: int, spans: list[_FormatSpan], limit: int) -> int:
    for span in spans:
        if span.start < split_at < span.end:
            if span.start > start:
                return span.start
            if span.end - start <= limit:
                return span.end
            return split_at
    return split_at


def _telegram_utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2
