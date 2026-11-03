from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any
from datetime import date


def point_in_time_data(data: dict[str, Any], report_date: date | None) -> dict[str, Any]:
    """Quarantine explicitly ineligible dated records outside prompts, retaining originals."""
    if report_date is None:
        return data
    import hashlib
    import json
    import os
    from pathlib import Path
    from zoneinfo import ZoneInfo
    from .config import ROOT_DIR
    from .source_date_normalizer import normalize_published_date, source_publication_status, source_temporal_record
    excluded = []
    target = report_date.isoformat()
    historical = report_date < datetime.now(ZoneInfo('Asia/Taipei')).date()
    def visit(value: Any, path: str, *, root: bool = False, forecast_scope: bool = False) -> Any:
        if isinstance(value, dict):
            forecast = forecast_scope or value.get('kind') in {'forecast', 'prediction', 'scenario'} or value.get('record_type') in {'forecast', 'prediction', 'scenario'}
            reason = None
            if not root and not forecast:
                for key in ('data_date', 'price_date', 'market_data_date', 'as_of_date', 'period_end', 'announcement_date', 'Date'):
                    day = normalize_published_date(value.get(key))
                    if day and day > target:
                        reason = f'{key} 晚於分析日'
                        break
                if value.get('url') or value.get('source_url'):
                    updated = normalize_published_date(value.get('updated_date'))
                    publication = normalize_published_date(value.get('published_date') or value.get('published_at'))
                    if publication and publication > target:
                        reason = '來源發布日晚於分析日'
                    elif historical and updated and updated > target:
                        reason = '目前正文在分析日後更新，不能當成當時快照'
                    elif historical and source_publication_status(value) != 'explicit' and any(value.get(key) for key in ('content', 'snippet', 'summary')):
                        reason = '歷史來源發布時間無法核對'
                    elif historical and source_temporal_record(value, report_date)['embedded_future_actuals']:
                        reason = '動態正文含分析日之後的實績，無可驗證歷史版本'
            if reason:
                excluded.append({'path': path, 'reason': reason, 'record': value})
                return {'point_in_time_status': 'excluded', 'reason': reason, 'source_id': value.get('source_id')}
            return {key: visit(item, f'{path}.{key}', forecast_scope=forecast) for key, item in value.items()}
        if isinstance(value, list):
            return [visit(item, f'{path}[{index}]', forecast_scope=forecast_scope) for index, item in enumerate(value)]
        return value
    result = visit(data, 'data', root=True)
    if excluded:
        serialized = json.dumps(excluded, ensure_ascii=False, sort_keys=True, default=str)
        digest = hashlib.sha256(serialized.encode()).hexdigest()
        test_root = os.environ.get('CODEX_TASK_TEST_ROOT')
        folder = (Path(test_root) if test_root else ROOT_DIR / '.cache') / 'point_in_time_archives'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f'{digest}.json'
        if not path.exists():
            path.write_text(serialized, encoding='utf-8')
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('歷史資料保存檔雜湊不符，停止建立可能失真的入模包。')
        result['point_in_time_audit'] = {'report_date': target, 'excluded_record_count': len(excluded),
            'archive_path': str(path), 'archive_sha256': digest,
            'scope': '檢查明確日期欄位及可識別的正文實績期間；無日期的語意與未來預測仍須另行核對。'}
    return result

from .models import CommandRequest, SourceItem


def target_for_snapshots(request: CommandRequest, structured_data: dict[str, Any]) -> str:
    if request.command == "research":
        stock = structured_data.get("stock") or {}
        return str(stock.get("code") or request.target or "unknown")
    return str(request.target or request.theme_scope or request.market_scope or request.candidate_pool or request.command)


def build_source_snapshots(request: CommandRequest, sources: list[SourceItem], structured_data: dict[str, Any], gemini_raw: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    target = target_for_snapshots(request, structured_data)
    report_date = request.report_date.isoformat() if request.report_date else None
    snapshots: list[dict[str, Any]] = []
    for source in sources:
        snapshots.append(
            {
                "target": target,
                "command": request.command,
                "source_url": source.url,
                "title": source.title,
                "source_level": source.source_level,
                "published_date": source.published_date,
                "fetched_at": now,
                "report_date": report_date,
                "content_type": "source_item",
                "content": {"source": asdict(source), "request": request.raw_text},
            }
        )
    if gemini_raw:
        snapshots.append(
            {
                "target": target,
                "command": request.command,
                "source_url": f"gemini://{request.command}/{now}",
                "title": "Gemini raw grounding metadata",
                "source_level": "Level 3",
                "published_date": None,
                "fetched_at": now,
                "report_date": report_date,
                "content_type": "gemini_raw",
                "content": _compact_gemini_raw(gemini_raw),
            }
        )
    return snapshots


def snapshots_to_structured_context(snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "status": "historical_snapshots_loaded" if snapshots else "no_historical_snapshots",
        "snapshot_count": len(snapshots),
        "policy": "歷史日期模式只允許 Gemini 整理已保存 snapshot，不直接使用現在網路搜尋。",
        "items": [
            {
                "title": item.get("title"),
                "source_url": item.get("source_url"),
                "source_level": item.get("source_level"),
                "published_date": item.get("published_date"),
                "fetched_at": item.get("fetched_at"),
                "content_type": item.get("content_type"),
                "content": item.get("content"),
            }
            for item in snapshots[:80]
        ],
    }


def _compact_gemini_raw(raw: dict[str, Any]) -> dict[str, Any]:
    candidates = raw.get("candidates") or []
    if not candidates:
        return {"status": "empty"}
    metadata = candidates[0].get("groundingMetadata") or candidates[0].get("grounding_metadata") or {}
    return {
        "groundingMetadata": metadata,
        "usageMetadata": raw.get("usageMetadata") or raw.get("usage_metadata"),
    }
