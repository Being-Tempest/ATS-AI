import statistics
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from benchmark.arrival_robustness import arrival_times, generate_stream
from benchmark.calibration_robustness import batch_duration, batch_k, make_duration_fn
from benchmark.objective_robustness import longest_small_precise_streak, utility_values
from benchmark.replay import STUB_BATCH_REPLY, STUB_REPLY
from benchmark.robustness_common import fano_and_peak, response_states
from scheduler import Scheduler
from score_queue import QueueItem


class CalibrationTests(unittest.TestCase):
    def test_k_boundaries(self):
        self.assertEqual(batch_k("P1", 8), 2)
        self.assertEqual(batch_k("P1", 9), 3)
        self.assertEqual(batch_k("P2", 2), 2)
        self.assertEqual(batch_k("P2", 3), 3)
        self.assertEqual(batch_k("H0", 10), 2)

    def test_duration_uses_complete_covered_list(self):
        item = SimpleNamespace(item_type="batch", covered=[object()] * 9)
        duration = make_duration_fn("P1")(item, "unused")
        self.assertEqual(duration, batch_duration("P1", 9))
        precise = SimpleNamespace(item_type="danmaku", covered=None)
        self.assertEqual(make_duration_fn("P2")(precise, "unused"), 9.0)

    def test_scheduler_default_and_injected_duration(self):
        item = QueueItem(score=50, item_type="danmaku", username="u", text="t")
        default = Scheduler()
        injected = Scheduler(duration_fn=lambda item, reply: 12.25)
        invalid = Scheduler(duration_fn=lambda item, reply: 0)
        try:
            self.assertEqual(default._reply_duration(item, STUB_REPLY), 7.8)
            batch = QueueItem(score=50, item_type="batch", username="u", text="q2d")
            self.assertEqual(default._reply_duration(batch, STUB_BATCH_REPLY), 14.8)
            self.assertEqual(injected._reply_duration(item, "anything"), 12.25)
            with self.assertRaises(ValueError):
                invalid._reply_duration(item, "anything")
        finally:
            default._pool.shutdown(wait=False)
            injected._pool.shutdown(wait=False)
            invalid._pool.shutdown(wait=False)


class ArrivalTests(unittest.TestCase):
    def test_arrivals_reproducible_and_truncated(self):
        for process in ("poisson", "onoff"):
            first = arrival_times(process, 24, 1001)
            self.assertEqual(first, arrival_times(process, 24, 1001))
            self.assertTrue(first)
            self.assertTrue(all(0 < t <= 1800 for t in first))
            self.assertEqual(first, sorted(first))

    def test_drift_switches_at_fixed_times(self):
        mixes = [
            {"high": 1, "mid": 0, "low": 0},
            {"high": 0, "mid": 1, "low": 0},
            {"high": 0, "mid": 0, "low": 1},
        ]
        times = [599.99, 600.0, 1199.99, 1200.0]
        with patch("benchmark.arrival_robustness.arrival_times", return_value=times), \
                patch("benchmark.arrival_robustness.MIX_DRIFT", mixes):
            events = generate_stream("poisson", 12, 42)
        self.assertEqual([ev["tag"] for ev in events if ev["type"] == "danmaku"],
                         ["high", "mid", "mid", "low"])

    def test_fano_keeps_zero_windows(self):
        events = [
            {"type": "danmaku", "ts": 1}, {"type": "danmaku", "ts": 2},
            {"type": "danmaku", "ts": 11}, {"type": "gift", "ts": 3},
        ]
        result = fano_and_peak(events, 10)
        self.assertEqual(len(result["counts"]), 180)
        self.assertEqual(result["counts"][:4], [2, 1, 0, 0])
        expected = statistics.variance(result["counts"]) / statistics.mean(result["counts"])
        self.assertAlmostEqual(result["fano"], expected)
        self.assertEqual(result["peak_per_min"], 12)


class ObjectiveTests(unittest.TestCase):
    def test_utility_counts_unreplied_as_zero(self):
        events = [
            {"id": "a", "type": "danmaku", "text": "a"},
            {"id": "b", "type": "danmaku", "text": "b"},
            {"id": "c", "type": "danmaku", "text": "c"},
        ]
        scorebook = {"a": 80, "b": 40, "c": 20}
        values = utility_values(events, {"a": 1}, {"b": 2}, scorebook)
        self.assertAlmostEqual(values[0], 80 / 240)
        self.assertAlmostEqual(values[0.5], 100 / 240)
        self.assertAlmostEqual(values[1], 120 / 240)

    def test_gift_boundary_and_batch_breaks_streak(self):
        events = [
            {"id": "small", "type": "gift", "price": 100},
            {"id": "high", "type": "gift", "price": 101},
            {"id": "dm", "type": "danmaku", "text": "x"},
        ]
        small = {ev["id"] for ev in events if ev["type"] == "gift" and ev["price"] <= 100}
        high = {ev["id"] for ev in events if ev["type"] == "gift" and ev["price"] > 100}
        self.assertEqual(small, {"small"})
        self.assertEqual(high, {"high"})
        decisions = [
            {"t": 1, "type": "gift", "ev_id": "small"},
            {"t": 2, "type": "gift", "ev_id": "small"},
            {"t": 3, "type": "batch", "covered": [{"ev_id": "dm", "type": "danmaku"}]},
            {"t": 4, "type": "gift", "ev_id": "small"},
        ]
        self.assertEqual(longest_small_precise_streak(decisions, small), 2)

    def test_response_states_reject_duplicate_response(self):
        events = [{"id": "a", "type": "danmaku", "text": "a"}]
        decisions = [
            {"t": 1, "type": "danmaku", "ev_id": "a"},
            {"t": 2, "type": "batch", "covered": [{"ev_id": "a", "type": "danmaku"}]},
        ]
        with self.assertRaises(AssertionError):
            response_states(decisions, events)


if __name__ == "__main__":
    unittest.main()
