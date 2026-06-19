from pathlib import Path
import tempfile
import unittest

from tools.encoding_health_check import scan_files


class EncodingHealthCheckTest(unittest.TestCase):
    def test_detects_utf8_decode_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bad_file = root / "bad_text.txt"
            bad_file.write_bytes(b"valid\n\xff\xfe broken")

            report = scan_files(root, ["bad_text.txt"])

        self.assertFalse(report.ok)
        self.assertEqual(len(report.utf8_errors), 1)
        self.assertEqual(report.utf8_errors[0].path, "bad_text.txt")

    def test_detects_non_allowlisted_mojibake_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bad_file = root / "docs" / "bad.md"
            bad_file.parent.mkdir()
            bad_file.write_text("來源標題：é¦–é  - TWSE\n", encoding="utf-8")

            report = scan_files(root, ["docs/bad.md"])

        self.assertFalse(report.ok)
        self.assertEqual(len(report.suspicious_marker_issues), 1)
        self.assertEqual(report.suspicious_marker_issues[0].path, "docs/bad.md")
        self.assertEqual(report.suspicious_marker_issues[0].marker, "é¦")

    def test_allowlisted_mojibake_marker_does_not_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            test_file = root / "tests" / "fixture.py"
            test_file.parent.mkdir()
            test_file.write_text('sample = "é¦–é  - TWSE"\n', encoding="utf-8")

            report = scan_files(root, ["tests/fixture.py"])

        self.assertTrue(report.ok)
        self.assertEqual(report.allowed_marker_count, 1)
        self.assertEqual(report.suspicious_marker_issues, [])


if __name__ == "__main__":
    unittest.main()
