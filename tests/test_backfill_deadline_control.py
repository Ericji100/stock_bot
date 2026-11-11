"""Offline backfill cancellation tests; no filesystem fixtures or HTTP calls."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date
import threading
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from research_center.ai_execution_control import (
    CONTROL, ExecutionControl, WorkStopped, check_work, source_checkpoint,
    checkpoint_sources,
)
from stock_ai_bot.backfill import backfill_service as backfill
from stock_ai_bot.backfill.deadline_control import backfill_scope
from stock_ai_bot.strategies import chip_strategies as chip


REPORT_DATE = date(2026, 11, 10)


def pool():
    return {
        code: backfill.BackfillCandidate(code=code, symbol=f"{code}.TW")
        for code in ("2330", "2317", "2454")
    }


@contextmanager
def fake_deadline(seconds=5):
    now = [0.0]
    control = ExecutionControl(seconds, clock=lambda: now[0])
    token = CONTROL.set(control)
    try:
        yield control, now
    finally:
        CONTROL.reset(token)


class BackfillDeadlineTests(unittest.TestCase):
    def test_expired_parent_blocks_all_work(self):
        with fake_deadline(0), patch.object(backfill, "load_stock_universe") as load:
            with self.assertRaises(WorkStopped):
                backfill.run_full_backfill(REPORT_DATE)
        load.assert_not_called()

    def test_screening_stops_before_next_stock_after_deadline(self):
        universe = [MagicMock(code=code, symbol=f"{code}.TW") for code in pool()]
        with fake_deadline() as (_, now):
            def fetch(*args, **kwargs):
                now[0] = 6.0
                raise RuntimeError("request exhausted its budget")

            with patch.object(backfill, "load_recent_revenue_history", return_value={}), \
                 patch.object(backfill, "load_price_metrics", return_value={}), \
                 patch.object(backfill, "fetch_daily_history", side_effect=fetch) as fetch_mock, \
                 patch.object(backfill, "load_market_risk_map") as risk, \
                 patch.object(backfill, "warmup_valuation_history_cache") as valuation:
                with self.assertRaises(WorkStopped):
                    backfill.warmup_market_screening_cache(universe, REPORT_DATE, True)
        self.assertEqual(fetch_mock.call_count, 1)
        risk.assert_not_called()
        valuation.assert_not_called()

    def test_research_thread_inherits_context_deadline_and_checkpoint(self):
        trace = ContextVar("backfill_test_trace", default=None)
        trace_token = trace.set("owned-worker")
        observed = []
        sources = []
        try:
            with fake_deadline(2) as (parent, _), source_checkpoint(sources.append):
                def collect(request, progress=None):
                    observed.append((trace.get(), CONTROL.get(), threading.current_thread()))
                    self.assertLessEqual(CONTROL.get().remaining(), 2)
                    checkpoint_sources(["cached-source"])
                    return {}

                with patch.object(backfill, "collect_research_data", side_effect=collect):
                    result = backfill.warmup_research_structured_data(
                        {"2330": pool()["2330"]}, REPORT_DATE, True,
                    )
            self.assertEqual(result, (1, [], [], 0))
            self.assertEqual(sources, [["cached-source"]])
            self.assertEqual(observed[0][0], "owned-worker")
            child = observed[0][1]
            while child.parent is not None:
                child = child.parent
            self.assertIs(child, parent)
            self.assertFalse(observed[0][2].daemon)
            self.assertFalse(observed[0][2].is_alive())
        finally:
            trace.reset(trace_token)

    def test_stop_during_collection_keeps_success_and_starts_no_next_stock(self):
        stop = threading.Event()
        successes = {}
        workers = []

        def collect(request, progress=None):
            workers.append(threading.current_thread())
            successes[request.target] = "saved structured cache"
            stop.set()
            progress("source finished")
            self.fail("collection continued past the cancellation checkpoint")

        with patch.object(backfill, "collect_research_data", side_effect=collect) as collect_mock:
            with self.assertRaises(WorkStopped):
                backfill.warmup_research_structured_data(pool(), REPORT_DATE, True, stop_event=stop)
        self.assertEqual(collect_mock.call_count, 1)
        self.assertEqual(successes, {"2330": "saved structured cache"})
        self.assertFalse(workers[0].is_alive())

    def test_stock_timeout_drains_before_starting_next_stock(self):
        order = []
        workers = []

        def collect(request, progress=None):
            workers.append(threading.current_thread())
            order.append((request.target, "start"))
            if request.target == "2330":
                if not CONTROL.get().event.wait(2):
                    raise AssertionError("stock deadline did not signal the worker")
            order.append((request.target, "exit"))
            return {}

        with patch.object(backfill, "collect_research_data", side_effect=collect):
            count, hits, warnings, timeouts = backfill.warmup_research_structured_data(
                {code: pool()[code] for code in ("2330", "2317")},
                REPORT_DATE, True, timeout_sec=0.1,
            )
        self.assertEqual((count, hits, timeouts), (1, [], 1))
        self.assertTrue(warnings)
        self.assertEqual(order, [("2330", "start"), ("2330", "exit"),
                                 ("2317", "start"), ("2317", "exit")])
        self.assertTrue(all(not worker.is_alive() for worker in workers))

    def test_total_budget_bounds_worker_and_leaves_later_stocks(self):
        remaining = []

        def collect(request, progress=None):
            remaining.append(CONTROL.get().remaining())
            if not CONTROL.get().event.wait(2):
                raise AssertionError("total deadline did not signal the worker")
            check_work()

        with patch.object(backfill, "collect_research_data", side_effect=collect) as collect_mock:
            count, hits, warnings, timeouts = backfill.warmup_research_structured_data(
                pool(), REPORT_DATE, True, timeout_sec=1, max_total_sec=0.15,
            )
        self.assertEqual((count, hits, timeouts), (0, [], 0))
        self.assertEqual(collect_mock.call_count, 1)
        self.assertLessEqual(remaining[0], 0.15)
        self.assertTrue(warnings)

    def test_cancelled_policy_holds_lock_until_worker_exits(self):
        stop = threading.Event()
        entered = threading.Event()
        draining = threading.Event()
        release = threading.Event()
        decisions = []
        errors = []
        workers = []

        def collect(request, progress=None):
            workers.append(threading.current_thread())
            entered.set()
            if not CONTROL.get().event.wait(2):
                raise AssertionError("worker did not receive cancellation")
            draining.set()
            if not release.wait(2):
                raise AssertionError("test did not release worker")
            check_work()

        def full(*args, **kwargs):
            backfill.warmup_research_structured_data(pool(), REPORT_DATE, True, stop_event=stop)
            self.fail("cancelled warmup returned normally")

        def run():
            try:
                decisions.append(backfill.run_backfill_if_needed(REPORT_DATE, True, stop_event=stop))
            except BaseException as exc:
                errors.append(exc)

        with patch.object(backfill, "is_market_data_available", return_value=(True, "ready")), \
             patch.object(backfill, "run_full_backfill", side_effect=full), \
             patch.object(backfill, "collect_research_data", side_effect=collect), \
             patch.object(backfill, "write_backfill_complete_marker") as marker:
            owner = threading.Thread(target=run)
            owner.start()
            try:
                self.assertTrue(entered.wait(2))
                stop.set()
                self.assertTrue(draining.wait(2))
                self.assertTrue(owner.is_alive())
                self.assertTrue(backfill.is_backfill_running())
                decision = backfill.run_backfill_if_needed(REPORT_DATE, True)
                self.assertEqual(decision.reason, "already_running")
                self.assertEqual(len(workers), 1)
            finally:
                stop.set()
                release.set()
                owner.join(3)
            marker.assert_not_called()
        self.assertFalse(owner.is_alive())
        self.assertFalse(backfill.is_backfill_running())
        self.assertFalse(errors)
        self.assertEqual(decisions[0].status, "stopped")
        self.assertFalse(workers[0].is_alive())

    def test_inherited_stop_after_full_run_never_calls_marker(self):
        with fake_deadline() as (control, _):
            def full(*args, **kwargs):
                control.stop()
                return backfill.BackfillResult(REPORT_DATE)

            with patch.object(backfill, "is_market_data_available", return_value=(True, "ready")), \
                 patch.object(backfill, "run_full_backfill", side_effect=full), \
                 patch.object(backfill, "write_backfill_complete_marker") as marker:
                decision = backfill.run_backfill_if_needed(REPORT_DATE, True)
        self.assertEqual(decision.status, "stopped")
        marker.assert_not_called()
        self.assertFalse(backfill.is_backfill_running())

    def test_marker_rechecks_cancellation_after_payload_preparation(self):
        stop = threading.Event()
        root = MagicMock()

        def build_dag(*args, **kwargs):
            stop.set()
            return {}

        with patch.object(backfill, "BACKFILL_MARKER_ROOT", root), \
             patch.object(backfill, "build_backfill_dag", side_effect=build_dag), \
             patch.object(backfill, "build_backfill_priority_plan", return_value={}), \
             patch.object(backfill, "summarize_backfill_dag", return_value={}), \
             patch("stock_ai_bot.data_sources.data_source_manager.FinMindQuotaManager"), \
             patch("stock_ai_bot.data_sources.data_source_manager.FugleRateLimiter"), \
             patch("stock_ai_bot.data_sources.data_source_manager.SourceHealthManager"), \
             patch("builtins.open") as opened, \
             patch.object(backfill, "register_artifact") as register:
            with self.assertRaises(WorkStopped):
                backfill.write_backfill_complete_marker(
                    REPORT_DATE, backfill.BackfillResult(REPORT_DATE), stop_event=stop,
                )
        opened.assert_not_called()
        register.assert_not_called()

    def test_gross_margin_preserves_successful_cache_on_stop(self):
        stop = threading.Event()
        metrics = {}

        def fetch(symbol, cache):
            cache[symbol] = [42]
            stop.set()
            return [42]

        with patch("stock_ai_bot.scanning.stock_scanner._load_gross_margin_cache", return_value=metrics), \
             patch("stock_ai_bot.scanning.stock_scanner._save_gross_margin_cache") as save, \
             patch.object(backfill, "load_gross_margin_series", side_effect=fetch) as fetch_mock:
            with self.assertRaises(WorkStopped):
                backfill.warmup_gross_margin_cache(pool(), stop_event=stop)
        self.assertEqual(fetch_mock.call_count, 1)
        save.assert_called_once_with({"2330.TW": [42]})


class ChipBackfillDeadlineTests(unittest.TestCase):
    def test_bounding_never_increases_existing_client_timeout(self):
        client = MagicMock()
        client.timeout = chip.httpx.Timeout(2, connect=1)
        with fake_deadline(7), backfill_scope():
            chip._fetch_json(client, "https://offline.invalid")
        timeout = client.get.call_args.kwargs["timeout"]
        self.assertEqual(timeout.connect, 1)
        self.assertEqual(timeout.read, 2)

    def test_request_timeout_uses_remaining_parent_budget_only_in_backfill(self):
        client = MagicMock()
        client.get.return_value.status_code = 200
        with fake_deadline(7):
            with backfill_scope(), patch.object(chip, "_wait_for_source_slot"), \
                 patch.object(chip, "_is_source_available", return_value=True):
                chip._fetch_source_json(client, "twse_t86", "https://offline.invalid")
                self.assertEqual(client.get.call_args.kwargs["timeout"], 7)
                chip._fetch_json(client, "https://offline.invalid")
                self.assertEqual(client.get.call_args.kwargs["timeout"], 7)
            chip._fetch_json(client, "https://offline.invalid")
            self.assertNotIn("timeout", client.get.call_args.kwargs)

    def test_deadline_during_source_throttle_prevents_request(self):
        client = MagicMock()
        with fake_deadline() as (_, now), backfill_scope():
            def expire(delay):
                now[0] = 6

            with patch.dict(chip.SOURCE_LAST_REQUEST_AT, {"twse_t86": chip.time.monotonic()}), \
                 patch.object(chip, "backfill_sleep", side_effect=expire), \
                 patch.object(chip, "_is_source_available", return_value=True):
                with self.assertRaises(WorkStopped):
                    chip._fetch_source_json(client, "twse_t86", "https://offline.invalid")
        client.get.assert_not_called()

    def test_stop_after_failed_stock_prevents_retry_and_preserves_first_cache(self):
        stop = threading.Event()
        first = pd.DataFrame([{"code": "2317"}])
        attempts = []

        def fetch(client, target_date, code, market, **kwargs):
            attempts.append(code)
            if len(attempts) == 1:
                return first
            stop.set()
            raise RuntimeError("source failed while cancelled")

        with backfill_scope(stop_event=stop), \
             patch.object(chip, "_is_source_available", return_value=True), \
             patch.object(chip, "_fetch_finmind_net_buy_for_stock", side_effect=fetch), \
             patch.object(chip, "_save_daily_chip_cache") as save:
            with self.assertRaises(WorkStopped):
                chip._fetch_finmind_net_buy_for_codes(
                    MagicMock(), REPORT_DATE, {code: "TWSE" for code in pool()},
                )
        self.assertEqual(attempts, ["2317", "2330"])
        save.assert_called_once_with(REPORT_DATE, first)

    def test_cancelled_direct_chip_warmup_starts_no_collection(self):
        stop = threading.Event()
        stop.set()
        with patch.object(chip, "build_market_context") as build:
            with self.assertRaises(WorkStopped):
                chip.warmup_chip_data_cache(REPORT_DATE, stop_event=stop)
        build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
