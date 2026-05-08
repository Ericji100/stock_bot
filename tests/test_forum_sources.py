from __future__ import annotations

import unittest

from research_center.forum_service import _collect_forum_search_fallback, _collect_cmoney, _extract_stock_code, _site_query
from research_center.source_rank import rank_source


class FakeResponse:
    def __init__(self, text="", payload=None):
        self.text = text
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload or {}


class FakeClient:
    def __init__(self):
        self.posts = []
        self.gets = []

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        if "social.cmoney.tw/forum/stock/2330" in url:
            return FakeResponse('<title>台積電(2330) 今日股價與討論-股市爆料同學會</title><meta name="description" content="台積電 個股討論">')
        return FakeResponse("")

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return FakeResponse(
            payload={
                "organic": [
                    {
                        "title": "PTT Stock 2330",
                        "link": "https://www.ptt.cc/bbs/Stock/M.1.html",
                        "snippet": "討論摘要",
                    }
                ]
            }
        )


class ForumSourcesTests(unittest.TestCase):
    def test_cmoney_stock_discussion_url_from_code(self):
        items = _collect_cmoney(FakeClient(), "2330 台積電", 4)
        self.assertEqual(items[0]["url"], "https://social.cmoney.tw/forum/stock/2330?tab=discuss")
        self.assertIn("股市爆料同學會", items[0]["title"])

    def test_forum_serper_fallback_uses_site_query(self):
        client = FakeClient()
        items = _collect_forum_search_fallback(client, "2330 台積電", "PTT Stock", 2, "serper")
        self.assertEqual(len(items), 1)
        self.assertIn("site:ptt.cc/bbs/Stock", client.posts[0][1]["json"]["q"])
        self.assertIn("Serper site: fallback", items[0]["snippet"])

    def test_cmoney_is_ranked_as_forum_source(self):
        self.assertEqual(rank_source("https://social.cmoney.tw/forum/stock/2330"), "Level 4")
        self.assertEqual(_extract_stock_code("5425 台半"), "5425")
        self.assertIn("social.cmoney.tw", _site_query("理財寶股市爆料同學會", "2330"))


if __name__ == "__main__":
    unittest.main()
