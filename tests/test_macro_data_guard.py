from __future__ import annotations

import unittest

from research_center.macro_data_guard import build_macro_data_guard
from research_center.models import CommandRequest
from research_center.prompt_registry import build_prompt_from_request


class MacroDataGuardTests(unittest.TestCase):
    def test_flags_abnormal_tw_index_numbers(self):
        data = {
            "quantitative_market": {
                "indices": {
                    "台灣加權指數": {
                        "latest_close": 30000,
                        "latest_date": "2026-06-05",
                        "one_day_change_points": 2500,
                        "one_day_return_pct": 9.1,
                        "five_day_change_points": 6500,
                        "five_day_return_pct": 27.7,
                    }
                },
                "global_public_macro": {},
                "volatility": {},
                "official_cash_institutional_flow": {},
                "official_futures_institutional": {},
            }
        }
        guard = build_macro_data_guard(data)
        alert_types = {item["異常類型"] for item in guard["alerts"]}
        self.assertIn("台股單日點數異常", alert_types)
        self.assertIn("台股五日點數異常", alert_types)
        self.assertIn("單日漲跌幅異常", alert_types)
        self.assertTrue(any("TWSE 三大法人" in item for item in guard["missing_data"]))

    def test_macro_prompt_includes_guard_rules_and_guard_payload(self):
        request = CommandRequest(command="macro", raw_text="/macro 台股", market_scope="台股", mode="deep")
        data = {
            "market_scope": "台股",
            "report_date": "2026-06-05",
            "quantitative_market": {
                "indices": {
                    "台灣加權指數": {
                        "latest_close": 30000,
                        "latest_date": "2026-06-05",
                        "one_day_change_points": 2500,
                        "one_day_return_pct": 9.1,
                    }
                },
                "global_public_macro": {},
                "volatility": {},
                "official_cash_institutional_flow": {},
                "official_futures_institutional": {},
            },
        }
        data["macro_data_guard"] = build_macro_data_guard(data)
        prompt = build_prompt_from_request(request, data, [])
        self.assertIn("宏觀硬數據護欄", prompt)
        self.assertIn("macro_data_guard", prompt)
        self.assertIn("不得直接寫成主結論", prompt)
        self.assertIn("硬數字只能引用", prompt)


if __name__ == "__main__":
    unittest.main()
