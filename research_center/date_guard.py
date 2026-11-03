from __future__ import annotations

from datetime import date, datetime
from typing import Iterable

from .models import SourceItem
from .source_date_normalizer import source_publication_status


def filter_sources_for_report_date(sources: Iterable[SourceItem], report_date: date | None) -> tuple[list[SourceItem], list[str]]:
    if report_date is None:
        return list(sources), []

    kept: list[SourceItem] = []
    dropped: list[str] = []
    for source in sources:
        if not source.published_date or source_publication_status(source) != 'explicit':
            dropped.append(f"{source.source_id} 無發布日期，已依 --date 保守排除。")
            continue
        from .source_date_normalizer import source_temporal_record
        temporal = source_temporal_record(source, report_date)
        if temporal['content_updated_after_report_date'] or temporal['data_after_report_date']:
            dropped.append(f"{source.source_id} 正文更新或實績日期晚於報告日期，已排除。")
            continue
        try:
            published = datetime.strptime(source.published_date[:10], "%Y-%m-%d").date()
        except ValueError:
            dropped.append(f"{source.source_id} 發布日期格式無法辨識，已依 --date 保守排除。")
            continue
        if published <= report_date:
            kept.append(source)
        else:
            dropped.append(f"{source.source_id} 發布日期 {published.isoformat()} 晚於報告日期，已排除。")

    return _renumber_sources(kept), dropped


def _renumber_sources(sources: list[SourceItem]) -> list[SourceItem]:
    return list(sources)
