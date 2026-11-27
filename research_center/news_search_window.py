"""Publication and query boundaries for the scheduled Taiwan news edition."""
from __future__ import annotations

from collections import Counter
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import re
from zoneinfo import ZoneInfo

from .source_date_normalizer import news_publication_record


TAIPEI = ZoneInfo('Asia/Taipei')


@dataclass(frozen=True)
class NewsSearchWindow:
    scheduled_day: date

    @property
    def end(self):
        return datetime.combine(self.scheduled_day, time(18), TAIPEI)

    @property
    def start(self):
        return self.end - timedelta(days=1)

    @property
    def supplement_start(self):
        return self.end - timedelta(days=4)

    def query(self, query, *, supplement=False):
        start, end = (self.supplement_start, self.start) if supplement else (self.start, self.end)
        suffix = (f'｜原文發布時間：台灣 {start:%Y-%m-%d %H:%M} 起至 {end:%Y-%m-%d %H:%M} 前；'
                  '請附原頁發稿時間與來源，不以更新、事件或網址日期替代。')
        topic = re.sub(r'(?:截至)?20\d{2}[-/]\d{1,2}[-/]\d{1,2}|不限當日|近24小時|今日|最新', '', str(query))
        topic = ' '.join(topic.strip('（）；， ').split()).replace('（ ）', '').replace('（）', '')
        return topic[:128-len(suffix)].rstrip() + suffix

    def lane(self, source):
        record = news_publication_record(source, self.end)
        if record['publication_status'] != 'explicit':
            return 'date_pending'
        if stamp := record['publication_timestamp']:
            point = datetime.fromisoformat(stamp)
            if self.start <= point < self.end:
                return 'primary'
            return 'supplement' if self.supplement_start <= point < self.start else 'outside'
        if record['publication_time_conflict']:
            return 'date_pending'
        start = datetime.combine(date.fromisoformat(record['published_date']), time.min, TAIPEI)
        end = start + timedelta(days=1)
        if start >= self.supplement_start and end <= self.start:
            return 'supplement'
        return 'outside' if end <= self.supplement_start or start >= self.end else 'date_pending'

    def primary_evidence(self, source):
        return self.lane(source) == 'primary' and self.has_primary_provenance(source)

    def date_verification_priority(self, source):
        suffix = self.query('')
        if not any(marker.endswith(suffix) for marker in source.found_by):
            return 0
        return 2 if source.search_summary_events else 1

    @staticmethod
    def has_primary_provenance(source):
        markers = (source.get('found_by') if isinstance(source, dict) else source.found_by) or []
        return 'news_supplement_search' not in markers or 'news_primary_search' in markers

    def audit(self, sources):
        lanes = {}
        for source in sources:
            if source.url:
                lane = self.lane(source)
                lanes[source.url] = 'outside' if lane == 'primary' and not self.has_primary_provenance(source) else lane
        counts = Counter(lanes.values())
        return {'timezone': str(TAIPEI), 'start_inclusive': self.start.isoformat(),
                'end_exclusive': self.end.isoformat(), 'supplement_start_inclusive': self.supplement_start.isoformat(),
                'counts': {lane: counts[lane] for lane in ('primary', 'supplement', 'date_pending', 'outside')},
                'source_lanes': lanes}


NEWS_EDITION_WINDOW = ContextVar('scheduled_news_edition_window', default=None)
NEWS_PRIOR_EDITION_MISSED = ContextVar('scheduled_news_prior_edition_missed', default=False)
