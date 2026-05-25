import threading
import unittest

import backfill_service
import main


class TestBackfillBackgroundHelpers(unittest.TestCase):
    def test_format_backfill_skip_reason_known_reason(self):
        text = main._format_backfill_skip_reason("cache_complete")
        self.assertIn("快取", text)

    def test_format_backfill_skip_reason_unknown_reason(self):
        text = main._format_backfill_skip_reason("custom_reason")
        self.assertIn("custom_reason", text)

    def test_maybe_throttle_stops_when_event_set(self):
        stop_event = threading.Event()
        stop_event.set()
        throttle = backfill_service.BackfillThrottle(batch_size=1, sleep_seconds=0)
        self.assertFalse(backfill_service._maybe_throttle(1, throttle, stop_event))

    def test_maybe_throttle_allows_when_not_stopped(self):
        throttle = backfill_service.BackfillThrottle(batch_size=1, sleep_seconds=0)
        self.assertTrue(backfill_service._maybe_throttle(1, throttle, None))


if __name__ == "__main__":
    unittest.main()
