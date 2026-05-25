from __future__ import annotations

import html
import json
import re
from typing import Any


def render_report_html(report_json: dict[str, Any], markdown: str, disclaimer: str = "") -> str:
    title = html.escape(str(report_json.get("report_title") or "AI 投研報告"))
    tabs = _build_tabs(report_json, markdown)
    disclaimer_html = f'<div class="disclaimer">{html.escape(disclaimer)}</div>' if disclaimer else ""
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    * {{ box-sizing: border-box; }}
    html, body {{ width: 100%; max-width: 100%; overflow-x: hidden; }}
    body {{ margin: 0; color: #172033; background: #eef2f7; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans TC", sans-serif; line-height: 1.72; }}
    main {{ width: min(100%, 1080px); margin: 0 auto; padding: 24px 16px 56px; background: #fff; min-height: 100vh; }}
    h1, h2, h3 {{ color: #111827; line-height: 1.35; overflow-wrap: anywhere; letter-spacing: 0; }}
    h1 {{ font-size: 28px; margin: 0; padding-bottom: 12px; border-bottom: 2px solid #e5e7eb; }}
    h2 {{ font-size: 22px; margin-top: 30px; padding-bottom: 6px; border-bottom: 1px solid #eef2f7; }}
    h3 {{ font-size: 18px; margin-top: 22px; }}
    p, li, a, td, th {{ overflow-wrap: anywhere; word-break: break-word; }}
    p {{ margin: 0 0 14px; }}
    .readable-paragraph {{ margin-bottom: 14px; }}
    .report-subsection {{ margin: 18px 0 20px; }}
    .report-subsection-title {{ margin: 0 0 8px; color: #0f172a; font-size: 16px; line-height: 1.45; }}
    .inline-section-title {{ font-weight: 800; color: #0f172a; }}
    a {{ color: #2563eb; text-decoration-thickness: 1px; text-underline-offset: 2px; }}
    ul {{ padding-left: 22px; }}
    code {{ white-space: pre-wrap; overflow-wrap: anywhere; }}
    pre {{ max-width: 100%; overflow-x: auto; white-space: pre-wrap; background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 6px; padding: 12px; }}
    .report-meta {{ margin: 10px 0 18px; color: #4b5563; font-size: 14px; }}
    .tab-input {{ position: absolute; opacity: 0; pointer-events: none; }}
    .tab-list {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 16px 0 20px; position: sticky; top: 0; z-index: 2; background: rgba(255,255,255,.96); padding: 8px 0; border-bottom: 1px solid #e5e7eb; }}
    .tab-label {{ display: inline-flex; align-items: center; min-height: 38px; padding: 8px 12px; border: 1px solid #cbd5e1; border-radius: 6px; background: #fff; color: #334155; font-size: 14px; cursor: pointer; user-select: none; }}
    .tab-label:hover {{ border-color: #2563eb; color: #1d4ed8; }}
    .tab-panel {{ display: none; width: 100%; min-width: 0; }}
    .table-wrap {{ width: 100%; max-width: 100%; overflow-x: auto; margin: 14px 0 20px; border: 1px solid #e5e7eb; border-radius: 8px; }}
    table {{ width: 100%; border-collapse: collapse; min-width: 640px; background: #fff; }}
    th, td {{ padding: 10px 12px; border-bottom: 1px solid #e5e7eb; text-align: left; vertical-align: top; white-space: normal; }}
    th {{ background: #f1f5f9; color: #0f172a; font-weight: 700; position: sticky; top: 0; z-index: 1; }}
    tr:nth-child(even) td {{ background: #fafafa; }}
    td.num, th.num {{ text-align: right; }}
    .source-card, .quality-card {{ border: 1px solid #e5e7eb; border-radius: 8px; padding: 12px; margin: 10px 0; background: #f8fafc; }}
    .source-title {{ font-weight: 700; color: #111827; }}
    .source-meta {{ color: #64748b; font-size: 13px; margin-top: 4px; }}
    .disclaimer {{ margin-top: 32px; padding: 12px; background: #fff7ed; border-left: 4px solid #f97316; overflow-wrap: anywhere; }}
    #tab-main:checked ~ .tab-list label[for="tab-main"],
    #tab-quality:checked ~ .tab-list label[for="tab-quality"],
    #tab-sources:checked ~ .tab-list label[for="tab-sources"],
    #tab-local-scoring:checked ~ .tab-list label[for="tab-local-scoring"],
    #tab-metadata:checked ~ .tab-list label[for="tab-metadata"],
    #tab-qa:checked ~ .tab-list label[for="tab-qa"] {{ background: #1d4ed8; color: #fff; border-color: #1d4ed8; }}
    #tab-main:checked ~ .panels #panel-main,
    #tab-quality:checked ~ .panels #panel-quality,
    #tab-sources:checked ~ .panels #panel-sources,
    #tab-local-scoring:checked ~ .panels #panel-local-scoring,
    #tab-metadata:checked ~ .panels #panel-metadata,
    #tab-qa:checked ~ .panels #panel-qa {{ display: block; }}
    @media (max-width: 680px) {{
      main {{ padding: 16px 12px 42px; }}
      h1 {{ font-size: 23px; }}
      h2 {{ font-size: 19px; }}
      h3 {{ font-size: 17px; }}
      .tab-list {{ position: static; gap: 6px; }}
      .tab-label {{ flex: 1 1 calc(50% - 6px); justify-content: center; padding: 8px; font-size: 13px; }}
      .table-wrap {{ overflow-x: hidden; border: 0; }}
      table.responsive-table {{ min-width: 0; border-collapse: separate; border-spacing: 0 10px; }}
      table.responsive-table thead {{ display: none; }}
      table.responsive-table, table.responsive-table tbody, table.responsive-table tr, table.responsive-table td {{ display: block; width: 100%; }}
      table.responsive-table tr {{ border: 1px solid #e5e7eb; border-radius: 8px; background: #fff; overflow: hidden; }}
      table.responsive-table td {{ display: grid; grid-template-columns: minmax(96px, 38%) 1fr; gap: 10px; border-bottom: 1px solid #f1f5f9; padding: 9px 10px; }}
      table.responsive-table td::before {{ content: attr(data-label); color: #475569; font-weight: 700; }}
      table.responsive-table td:last-child {{ border-bottom: 0; }}
    }}
  </style>
</head>
<body><main>{tabs}{disclaimer_html}</main></body>
</html>"""


def _build_tabs(report_json: dict[str, Any], markdown: str) -> str:
    title = html.escape(str(report_json.get("report_title") or "AI 投研報告"))
    report_date = html.escape(str(report_json.get("report_date") or ""))
    mode = html.escape(str(report_json.get("mode") or ""))
    model = html.escape(str((report_json.get("metadata") or {}).get("analysis_model") or ""))
    sections = _split_markdown(markdown)
    main_html = _markdown_to_html(sections.get("main") or markdown)
    quality_html = _quality_html(report_json)
    sources_html = _sources_html(report_json)
    local_scoring_html = _local_scoring_html(report_json)
    metadata_html = f"<pre>{html.escape(json.dumps(_metadata_summary(report_json), ensure_ascii=False, indent=2, default=str))}</pre>"
    qa_html = _markdown_to_html(sections.get("qa") or _qa_markdown(report_json))
    return f"""
<div class="report-shell">
  <h1>{title}</h1>
  <div class="report-meta">報告日期：{report_date}　模式：{mode}{'　模型：' + model if model else ''}</div>
  <input class="tab-input" type="radio" name="report-tabs" id="tab-main" checked>
  <input class="tab-input" type="radio" name="report-tabs" id="tab-quality">
  <input class="tab-input" type="radio" name="report-tabs" id="tab-sources">
  <input class="tab-input" type="radio" name="report-tabs" id="tab-local-scoring">
  <input class="tab-input" type="radio" name="report-tabs" id="tab-metadata">
  <input class="tab-input" type="radio" name="report-tabs" id="tab-qa">
  <nav class="tab-list" aria-label="報告分頁">
    <label class="tab-label" for="tab-main">主報告</label>
    <label class="tab-label" for="tab-quality">資料品質</label>
    <label class="tab-label" for="tab-sources">完整來源</label>
    <label class="tab-label" for="tab-local-scoring">本地底稿</label>
    <label class="tab-label" for="tab-metadata">Metadata</label>
    <label class="tab-label" for="tab-qa">QA</label>
  </nav>
  <div class="panels">
    <section class="tab-panel" id="panel-main">{main_html}</section>
    <section class="tab-panel" id="panel-quality">{quality_html}</section>
    <section class="tab-panel" id="panel-sources">{sources_html}</section>
    <section class="tab-panel" id="panel-local-scoring">{local_scoring_html}</section>
    <section class="tab-panel" id="panel-metadata">{metadata_html}</section>
    <section class="tab-panel" id="panel-qa">{qa_html}</section>
  </div>
</div>"""


def _markdown_to_html(markdown: str) -> str:
    output: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []
    code_lines: list[str] = []
    table_lines: list[str] = []
    in_code = False

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            output.extend(_paragraph_lines_to_html(paragraph))
            paragraph = []

    def flush_list() -> None:
        nonlocal list_items
        if list_items:
            output.append("<ul>" + "".join(f"<li>{_inline_markup(item)}</li>" for item in list_items) + "</ul>")
            list_items = []

    def flush_code() -> None:
        nonlocal code_lines
        if code_lines:
            output.append("<pre><code>" + html.escape("\n".join(code_lines)) + "</code></pre>")
            code_lines = []

    def flush_table() -> None:
        nonlocal table_lines
        if table_lines:
            output.append(_markdown_table_to_html(table_lines))
            table_lines = []

    for raw in markdown.splitlines():
        line = raw.rstrip()
        if line.startswith("```"):
            flush_paragraph()
            flush_list()
            flush_table()
            if in_code:
                flush_code()
            in_code = not in_code
            continue
        if in_code:
            code_lines.append(line)
            continue
        if _is_table_line(line):
            flush_paragraph()
            flush_list()
            table_lines.append(line)
            continue
        flush_table()
        if line.startswith("# "):
            flush_paragraph()
            flush_list()
            output.append(f"<h1>{html.escape(line[2:].strip())}</h1>")
        elif line.startswith("## "):
            flush_paragraph()
            flush_list()
            output.append(f"<h2>{html.escape(line[3:].strip())}</h2>")
        elif line.startswith("### "):
            flush_paragraph()
            flush_list()
            output.append(f"<h3>{html.escape(line[4:].strip())}</h3>")
        elif line.startswith("- "):
            flush_paragraph()
            list_items.append(line[2:].strip())
        elif not line.strip():
            flush_paragraph()
            flush_list()
        else:
            paragraph.append(line.strip())
    flush_table()
    flush_paragraph()
    flush_list()
    if in_code:
        flush_code()
    return "\n".join(output)


def _markdown_table_to_html(lines: list[str]) -> str:
    rows = [_split_table_row(line) for line in lines if _split_table_row(line)]
    if not rows:
        return ""
    if len(rows) >= 2 and _is_separator_row(rows[1]):
        headers = rows[0]
        body = rows[2:]
    else:
        headers = [f"欄位 {index + 1}" for index in range(max(len(row) for row in rows))]
        body = rows
    thead = "<thead><tr>" + "".join(f"<th>{html.escape(cell)}</th>" for cell in headers) + "</tr></thead>"
    body_rows = []
    for row in body:
        padded = row + [""] * max(0, len(headers) - len(row))
        cells = []
        for index, cell in enumerate(padded[: len(headers)]):
            label = html.escape(headers[index])
            cls = ' class="num"' if _looks_numeric(cell) else ""
            cells.append(f'<td data-label="{label}"{cls}>{_inline_markup(cell)}</td>')
        body_rows.append("<tr>" + "".join(cells) + "</tr>")
    return '<div class="table-wrap"><table class="responsive-table">' + thead + "<tbody>" + "".join(body_rows) + "</tbody></table></div>"


def _paragraph_lines_to_html(lines: list[str]) -> list[str]:
    blocks: list[str] = []
    for text in _split_readable_blocks(lines):
        heading, body = _extract_bold_lead_heading(text)
        if heading is not None:
            body_html = "".join(f'<p class="readable-paragraph">{_inline_markup(part)}</p>' for part in _split_long_text(body))
            blocks.append(
                '<section class="report-subsection">'
                f'<h4 class="report-subsection-title">{_inline_markup(heading)}</h4>'
                f"{body_html}"
                "</section>"
            )
            continue
        for part in _split_long_text(text):
            blocks.append(f'<p class="readable-paragraph">{_inline_markup(part)}</p>')
    return blocks


def _split_readable_blocks(lines: list[str]) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    for line in lines:
        text = line.strip()
        if not text:
            continue
        if current and _starts_bold_lead_heading(text):
            blocks.append(" ".join(current).strip())
            current = [text]
        else:
            current.append(text)
    if current:
        blocks.append(" ".join(current).strip())
    return [block for block in blocks if block]


def _starts_bold_lead_heading(text: str) -> bool:
    return bool(re.match(r"^\*\*[^*\n]{2,80}\*\*(?:\s*[:：、-]\s*|\s+|$)", text.strip()))


def _extract_bold_lead_heading(text: str) -> tuple[str | None, str]:
    match = re.match(r"^\*\*(?P<title>[^*\n]{2,80})\*\*(?P<sep>\s*[:：、-]?\s*)(?P<body>.*)$", text.strip())
    if not match:
        return None, text
    title = match.group("title").strip().rstrip(":：、- ")
    body = match.group("body").strip()
    if not title:
        return None, text
    return title, body


def _split_long_text(text: str, max_chars: int = 120) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    segments = re.split(r"(?<=[。！？!?；;])\s*", text)
    parts: list[str] = []
    current = ""
    for segment in segments:
        segment = segment.strip()
        if not segment:
            continue
        if current and len(current) + len(segment) > max_chars:
            parts.append(current)
            current = segment
        else:
            current = f"{current}{segment}" if current else segment
    if current:
        parts.append(current)
    return parts or [text]


def _quality_html(report_json: dict[str, Any]) -> str:
    metadata = report_json.get("metadata") or {}
    quality = metadata.get("report_quality") or {}
    rows = quality.get("data_completeness_matrix") or (report_json.get("structured_data") or {}).get("data_completeness_matrix") or []
    summary = quality.get("source_coverage_summary") or metadata.get("source_coverage_summary") or {}
    warnings = quality.get("qa_warnings") or []
    parts = [
        "<h2>報告資料完整度與來源品質</h2>",
        '<article class="quality-card">',
        f"<p>資料覆蓋分數：<strong>{html.escape(str(quality.get('data_coverage_score') or metadata.get('data_coverage_score') or 0))}/100</strong></p>",
        f"<p>來源總數：{html.escape(str(summary.get('total_sources') or 0))}；有日期來源：{html.escape(str(summary.get('dated_sources') or 0))}；無日期來源：{html.escape(str(summary.get('undated_sources') or 0))}</p>",
        "<p>QA 提醒：" + (html.escape("；".join(str(item) for item in warnings)) if warnings else "無") + "</p>",
        "</article>",
    ]
    if rows:
        table = ["| 欄位 | 狀態 | 數量 |", "|---|---|---|"]
        for row in rows:
            table.append(f"| {row.get('field')} | {'有資料' if row.get('available') else '缺資料'} | {row.get('count', 0)} |")
        parts.append(_markdown_table_to_html(table))
    policy = quality.get("missing_data_policy") or {}
    if policy:
        parts.append("<h3>Missing Data Policy</h3><ul>")
        parts.extend(f"<li><strong>{html.escape(str(key))}</strong>：{html.escape(str(value))}</li>" for key, value in policy.items())
        parts.append("</ul>")
    return "\n".join(parts)


def _sources_html(report_json: dict[str, Any]) -> str:
    sources = report_json.get("sources") or []
    if not sources:
        return "<h2>完整來源</h2><p>目前沒有完整資料來源清單。</p>"
    cards = ["<h2>完整來源</h2>"]
    quality_items = {
        str(item.get("source_id")): item
        for item in ((report_json.get("metadata") or {}).get("source_quality") or {}).get("items", [])
    }
    for item in sources:
        sid = html.escape(str(item.get("source_id") or ""))
        q = quality_items.get(str(item.get("source_id"))) or {}
        title = html.escape(str(item.get("title") or item.get("url") or ""))
        url = html.escape(str(item.get("url") or ""))
        level = html.escape(str(item.get("source_level") or ""))
        provider = html.escape(str(item.get("provider") or item.get("fetch_provider") or "unknown"))
        provider_detail = html.escape(str(item.get("provider_detail") or ""))
        date = html.escape(str(item.get("published_date") or ""))
        score = html.escape(str(q.get("source_quality_score") or ""))
        snippet = html.escape(str(item.get("snippet") or ""))
        link = f'<a href="{url}" target="_blank" rel="noopener noreferrer">{url}</a>' if url.startswith(("http://", "https://")) else url
        meta = " / ".join(part for part in [level, provider, provider_detail, date, f"quality={score}" if score else ""] if part)
        cards.append(f'<article class="source-card"><div class="source-title">[{sid}] {title}</div><div class="source-meta">{meta}</div><div>{link}</div>{f"<p>{snippet}</p>" if snippet else ""}</article>')
    return "\n".join(cards)


def _local_scoring_html(report_json: dict[str, Any]) -> str:
    local_scoring = ((report_json.get("metadata") or {}).get("local_scoring") or {})
    scores = local_scoring.get("scores") or []
    if not scores:
        return "<h2>本地量化底稿</h2><p>本次沒有本地量化底稿。</p>"
    rows = ["| 項目 | 分數 | 理由 | 扣分/限制 |", "|---|---|---|---|"]
    for item in scores:
        rows.append(
            "| {name} | {score}/{max_score} | {reason} | {deduction} |".format(
                name=item.get("score_name") or "",
                score=item.get("score_value") or "",
                max_score=item.get("score_max") or "",
                reason=item.get("score_reason") or "",
                deduction=item.get("deduction_reason") or "",
            )
        )
    return "<h2>本地量化底稿</h2><p>本地底稿只供 AI 參考，不是最終投研評分。</p>" + _markdown_table_to_html(rows)


def _qa_markdown(report_json: dict[str, Any]) -> str:
    qa = ((report_json.get("metadata") or {}).get("qa_validation") or {})
    warnings = qa.get("warnings") or []
    if not qa and not warnings:
        return "## QA\n\n- 無 QA 提醒。"
    lines = ["## QA", "", f"- passed: {qa.get('passed')}"]
    lines.extend(f"- {item}" for item in warnings)
    for item in qa.get("missing_sections") or []:
        lines.append(f"- missing_section: {item}")
    for item in qa.get("schema_errors") or []:
        lines.append(f"- schema_error: {item}")
    return "\n".join(lines)


def _metadata_summary(report_json: dict[str, Any]) -> dict[str, Any]:
    metadata = report_json.get("metadata") or {}
    return {
        "report_type": report_json.get("report_type"),
        "target": report_json.get("target"),
        "mode": report_json.get("mode"),
        "report_date": report_json.get("report_date"),
        "ai_used": metadata.get("ai_used"),
        "analysis_model": metadata.get("analysis_model"),
        "report_schema_version": metadata.get("report_schema_version"),
        "data_coverage_score": metadata.get("data_coverage_score"),
        "source_coverage_summary": metadata.get("source_coverage_summary"),
        "qa_validation": metadata.get("qa_validation"),
        "report_quality": metadata.get("report_quality"),
    }


def _split_markdown(markdown: str) -> dict[str, str]:
    buckets = {"main": [], "qa": [], "sources": []}
    current = "main"
    for line in markdown.splitlines():
        heading = line.lstrip("#").strip()
        if line.startswith("## "):
            if any(key in heading for key in ("完整資料來源", "資料來源清單", "完整來源")):
                current = "sources"
            elif any(key in heading for key in ("QA", "規格檢查", "檢查提醒")):
                current = "qa"
            else:
                current = "main"
        buckets.setdefault(current, []).append(line)
    return {key: "\n".join(value).strip() for key, value in buckets.items()}


def _inline_markup(text: str) -> str:
    escaped = html.escape(str(text or ""))
    escaped = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2" target="_blank" rel="noopener noreferrer">\1</a>', escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    return escaped


def _is_table_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2


def _split_table_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_separator_row(row: list[str]) -> bool:
    return all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in row if cell.strip())


def _looks_numeric(value: str) -> bool:
    return bool(re.fullmatch(r"[-+]?[\d,.]+%?", str(value or "").strip()))
