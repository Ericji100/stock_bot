from __future__ import annotations

import unittest

from research_center.command_parser import parse_command_text
from research_center.news_event_service import attach_news_events, build_news_events_from_context


class NewsEventServiceTests(unittest.TestCase):
    def test_build_news_events_from_news_context(self):
        request = parse_command_text("/research 2330")
        data = {
            "stock": {"code": "2330"},
            "news_context": {
                "items": [
                    {"title": "台積電營收創高", "url": "https://example.com/a", "published_at": "2026-05-20", "summary": "營收 EPS"},
                    {"title": "庫存風險升高", "url": "https://example.com/b", "published_at": "2026-05-21", "summary": "risk"},
                ]
            },
        }

        events = build_news_events_from_context(request, data)

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["target"], "2330")
        self.assertEqual(events[0]["event_type"], "news_financial")
        self.assertEqual(events[1]["event_type"], "news_risk")

    def test_attach_news_events_writes_summary(self):
        request = parse_command_text("/macro global")
        data = {"news_context": {"items": [{"title": "Fed rate news"}]}}

        attach_news_events(request, data)

        self.assertEqual(data["news_event_summary"]["schema_version"], "news_event_v1")
        self.assertEqual(data["news_event_summary"]["event_count"], 1)


if __name__ == "__main__":
    unittest.main()
