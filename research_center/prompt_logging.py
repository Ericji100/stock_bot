from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import CommandRequest, SourceItem
from .prompt_manifest_service import prompt_bundle_for_request

ROOT_DIR = Path(__file__).resolve().parents[1]
PROMPT_LOG_DIR = ROOT_DIR / "logs" / "ai_prompts"


def write_prompt_log(request: CommandRequest, prompt: str, model: str, grounding_enabled: bool, sources: list[SourceItem], metadata: dict[str, Any] | None = None) -> Path:
    PROMPT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = _safe(request.target or request.market_scope or request.candidate_pool or request.command)
    path = PROMPT_LOG_DIR / f"{stamp}_{request.command}_{target}_{uuid4().hex[:6]}.json"
    metadata_payload = dict(metadata or {})
    metadata_payload.setdefault("prompt_bundle", prompt_bundle_for_request(request))
    payload = {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "command": request.command,
        "raw_text": request.raw_text,
        "mode": request.mode,
        "report_date": request.report_date.isoformat() if request.report_date else None,
        "model": model,
        "grounding_enabled": grounding_enabled,
        "prompt_length": len(prompt),
        "source_count": len(sources),
        "metadata": metadata_payload,
        "prompt": prompt,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def _safe(value: str) -> str:
    import re

    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_.-]+", "_", str(value)).strip("_")[:60] or "target"
