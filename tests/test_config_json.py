from __future__ import annotations

import json
import unittest
from pathlib import Path


class ConfigJsonTests(unittest.TestCase):
    def test_config_json_is_valid(self):
        payload = json.loads(Path("config.json").read_text(encoding="utf-8"))
        self.assertIsInstance(payload, dict)


if __name__ == "__main__":
    unittest.main()
