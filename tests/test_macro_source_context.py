import unittest

from research_center.data_services import _macro_official_sources, _official_sources, _renumber_source_items


class MacroSourceContextTests(unittest.TestCase):
    def test_macro_official_sources_are_renumbered_without_duplicate_ids(self):
        base = _official_sources()
        macro = _renumber_source_items(_macro_official_sources(), start=len(base) + 1)
        source_ids = [item.source_id for item in [*base, *macro]]
        titles = " ".join(item.title for item in macro)

        self.assertEqual(len(source_ids), len(set(source_ids)))
        self.assertIn("台灣期貨交易所", titles)
        self.assertIn("中央銀行", titles)
        self.assertIn("聯準會", titles)


if __name__ == "__main__":
    unittest.main()
