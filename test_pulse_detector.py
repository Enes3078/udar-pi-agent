#!/usr/bin/env python3
import sys
import types
import unittest

requests_module = types.ModuleType("requests")
requests_module.RequestException = Exception
requests_module.Session = object
sys.modules.setdefault("requests", requests_module)

gpiozero_module = types.ModuleType("gpiozero")
gpiozero_module.DigitalInputDevice = object
sys.modules.setdefault("gpiozero", gpiozero_module)

from udar_pi_agent import DurationCycleDetector, PulseCycleDetector


class PulseCycleDetectorTests(unittest.TestCase):
    def detector(self):
        return PulseCycleDetector(
            active_level=True,
            min_active_seconds=0.02,
            rearm_seconds=0.20,
            max_active_seconds=2.0,
            min_interval_seconds=0.20,
        )

    def test_constant_values_do_not_emit(self):
        detector = self.detector()
        for index in range(1000):
            self.assertIsNone(detector.feed(False, index / 100))

    def test_complete_stable_cycle_emits_once(self):
        detector = self.detector()
        detector.feed(False, 0.0)
        detector.feed(False, 0.3)
        detector.feed(True, 0.31)
        detector.feed(True, 0.34)
        detector.feed(False, 0.50)
        result = detector.feed(False, 0.71)
        self.assertTrue(result["accepted"])
        self.assertAlmostEqual(result["active_seconds"], 0.19)
        self.assertIsNone(detector.feed(False, 1.0))

    def test_starting_high_is_not_counted(self):
        detector = self.detector()
        detector.feed(True, 0.0)
        detector.feed(True, 1.0)
        detector.feed(False, 1.1)
        self.assertIsNone(detector.feed(False, 1.4))

    def test_short_noise_is_not_counted(self):
        detector = self.detector()
        detector.feed(False, 0.0)
        detector.feed(False, 0.3)
        detector.feed(True, 0.31)
        detector.feed(False, 0.315)
        self.assertIsNone(detector.feed(False, 0.6))

    def test_stuck_high_is_rejected_after_release(self):
        detector = self.detector()
        detector.feed(False, 0.0)
        detector.feed(False, 0.3)
        detector.feed(True, 0.31)
        detector.feed(True, 0.34)
        detector.feed(False, 3.0)
        result = detector.feed(False, 3.3)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "active_too_long")


class DurationCycleDetectorTests(unittest.TestCase):
    def detector(self):
        return DurationCycleDetector(
            active_level=True,
            start_stable_seconds=0.20,
            stop_stable_seconds=0.20,
        )

    def test_chatter_does_not_start_or_stop(self):
        detector = self.detector()
        detector.feed(False, 0.0)
        detector.feed(True, 0.10)
        detector.feed(False, 0.15)
        detector.feed(True, 0.20)
        detector.feed(False, 0.25)
        self.assertIsNone(detector.feed(False, 0.50))

    def test_stable_interval_is_reported_once(self):
        detector = self.detector()
        detector.feed(False, 0.0)
        detector.feed(False, 0.3)
        detector.feed(True, 1.0)
        started = detector.feed(True, 1.21)
        self.assertEqual(started["event"], "started")
        detector.feed(False, 11.0)
        stopped = detector.feed(False, 11.21)
        self.assertEqual(stopped["event"], "stopped")
        self.assertAlmostEqual(stopped["elapsed_seconds"], 10.0)
        self.assertIsNone(detector.feed(False, 12.0))

    def test_starting_high_requires_inactive_baseline(self):
        detector = self.detector()
        detector.feed(True, 0.0)
        detector.feed(True, 1.0)
        detector.feed(False, 2.0)
        self.assertIsNone(detector.feed(False, 2.21))
        detector.feed(True, 3.0)
        started = detector.feed(True, 3.21)
        self.assertEqual(started["event"], "started")


if __name__ == "__main__":
    unittest.main()
