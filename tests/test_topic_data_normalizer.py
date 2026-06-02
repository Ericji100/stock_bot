import unittest

from research_center.topic_data_normalizer import (
    normalize_string_list,
    normalize_text_tree,
    to_traditional_text,
)


class TopicDataNormalizerTests(unittest.TestCase):
    def test_to_traditional_text_converts_common_topic_terms(self):
        text = to_traditional_text("网络交换器资料中心客户营收占比供应链风险")
        self.assertEqual(text, "網路交換器資料中心客戶營收佔比供應鏈風險")

    def test_normalize_string_list_flattens_field_value_wrappers(self):
        value = [
            "既有產品",
            {"value": ["网络交换器", "资料中心网通设备"], "status": "candidate"},
            {"value": "客户名单"},
            {"status": "missing"},
        ]
        self.assertEqual(
            normalize_string_list(value),
            ["既有產品", "網路交換器", "資料中心網通設備", "客戶名單"],
        )

    def test_normalize_text_tree_recurses_nested_values(self):
        data = {"risk_notes": ["客户验证不足"], "evidence": [{"content": "供应链资料不足"}]}
        self.assertEqual(
            normalize_text_tree(data),
            {"risk_notes": ["客戶驗證不足"], "evidence": [{"content": "供應鏈資料不足"}]},
        )


if __name__ == "__main__":
    unittest.main()
