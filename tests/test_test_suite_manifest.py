from __future__ import annotations

import unittest

from tools.test_suite_manifest import build_test_suite_manifest, format_test_suite_manifest


class TestSuiteManifestTests(unittest.TestCase):
    def test_manifest_has_required_layers(self) -> None:
        manifest = build_test_suite_manifest()
        suites = {entry["name"]: entry for entry in manifest["suites"]}

        self.assertEqual(manifest["schema_version"], "test_suite_manifest_v1")
        self.assertIn("fast_unit", suites)
        self.assertIn("integration", suites)
        self.assertIn("live_source", suites)
        self.assertIn("ai_smoke", suites)

    def test_live_and_ai_suites_are_manual(self) -> None:
        suites = {entry["name"]: entry for entry in build_test_suite_manifest()["suites"]}

        self.assertFalse(suites["fast_unit"]["manual"])
        self.assertFalse(suites["integration"]["manual"])
        self.assertTrue(suites["live_source"]["manual"])
        self.assertTrue(suites["ai_smoke"]["manual"])
        self.assertTrue(suites["live_source"]["requires_network"])
        self.assertTrue(suites["ai_smoke"]["requires_ai"])

    def test_formatted_manifest_lists_commands(self) -> None:
        text = format_test_suite_manifest()

        self.assertIn("fast_unit", text)
        self.assertIn("python -B -m unittest discover tests", text)
        self.assertIn("manual", text)


if __name__ == "__main__":
    unittest.main()
