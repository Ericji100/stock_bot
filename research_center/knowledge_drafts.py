from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import CommandRequest, SourceItem

ROOT_DIR = Path(__file__).resolve().parents[1]
DRAFT_DIR = ROOT_DIR / "logs" / "knowledge_drafts"
KEYWORDS = ("產品", "客戶", "供應鏈", "營收占比", "CAGR", "護城河", "轉型", "題材", "法說", "年報", "公告", "反證")


def write_knowledge_draft(request: CommandRequest, markdown: str, sources: list[SourceItem], structured_data: dict[str, Any]) -> Path | None:
    if request.command not in {"research", "theme", "value_scan"}:
        return None
    snippets = _extract_relevant_lines(markdown)
    if not snippets:
        return None
    DRAFT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = _safe(request.target or request.theme_scope or request.candidate_pool or request.command)
    path = DRAFT_DIR / f"{stamp}_{request.command}_{target}_{uuid4().hex[:6]}.json"
    payload = {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "command": request.command,
        "target": request.target or request.theme_scope or request.candidate_pool,
        "report_date": request.report_date.isoformat() if request.report_date else None,
        "status": "draft_requires_review",
        "policy": "Gemini/報告抽取草稿，不自動覆寫 company_knowledge.json；需人工或規則審核後才能成為正式知識庫。",
        "snippets": snippets[:80],
        "source_urls": [source.url for source in sources],
        "candidate_source_policy": structured_data.get("candidate_source_policy"),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def _extract_relevant_lines(markdown: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    current = "摘要"
    for line in markdown.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            continue
        clean = line.strip(" -\t")
        if not clean:
            continue
        hit = [keyword for keyword in KEYWORDS if keyword.lower() in clean.lower()]
        if hit:
            rows.append({"section": current, "keywords": hit, "text": clean[:500], "source_refs": sorted(set(re.findall(r"S\d{3}", clean)))})
    return rows


def _safe(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_.-]+", "_", str(value)).strip("_")[:60] or "target"
