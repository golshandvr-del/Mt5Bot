"""
Offline tests for the multi-seed stability gate (UPGRADE_PLAN.md U4.3).

The gate re-runs a would-be-promoted spec several times with a different
bootstrap seed + jittered warmup each time and requires the rank score to stay
strictly positive in EVERY run, so a knife-edge fluke (positive under one
warmup/seed, negative under a neighbor) is never promoted.

These tests exercise the gate LOGIC in isolation by stubbing the WalkForward
re-runs with a scripted sequence of scores - no MT5, no network, fast. They
assert:

  1. Gate DISABLED -> always passes (legacy behavior preserved).
  2. All re-runs positive -> pass.
  3. Any single re-run <= 0 -> reject.
  4. The gate calls evaluate exactly n_seeds times with persist=False and a
     warmup that respects the configured jitter band (and never below the floor).

All text is standard ASCII English only.
"""

from __future__ import annotations

import unittest

from tests.helpers import make_synthetic_ohlcv  # noqa: F401 (path fix)

from config.loader import load_config
from core.strategy.search import StrategySearch
from core.strategy.strategy import StrategySpec


class _ScriptedWF(object):
    """Stub WalkForward: returns avg_scores from a scripted list, recording the
    warmup + persist of every call."""

    def __init__(self, scores):
        self._scores = list(scores)
        self.calls = []  # list of (warmup, persist)
        self.holdout_bars = 0

    def evaluate(self, spec, ohlcv, point=None, persist=True, warmup=60):
        self.calls.append((warmup, persist))
        score = self._scores.pop(0) if self._scores else 0.0
        return {"avg_score": score}


def _spec():
    return StrategySpec(
        indicators={"ema": {"period": 10}, "rsi": {"period": 14}},
        weights={"ema": 1.0, "rsi": 1.0},
        long_threshold=0.2, short_threshold=0.2,
        sl_atr_mult=2.0, tp_atr_mult=3.0,
        symbol="TESTX", timeframe="M15",
    )


def _make_search(stability, wf_scores):
    # Use the real config wiring, then override the stability knobs + swap the
    # WalkForward for a scripted stub so we test the gate LOGIC deterministically.
    cfg = load_config()
    search = StrategySearch(cfg, memory=None)
    search.stability_enabled = bool(stability.get("enabled", False))
    search.stability_n_seeds = int(stability.get("n_seeds", 3))
    search.stability_warmup_jitter = int(stability.get("warmup_jitter", 20))
    search.stability_require_all_positive = bool(
        stability.get("require_all_positive", True))
    search.wf = _ScriptedWF(wf_scores)
    return search


class TestStabilityGate(unittest.TestCase):
    def test_disabled_always_passes(self):
        s = _make_search({"enabled": False}, wf_scores=[-99.0])
        self.assertTrue(s._passes_stability_gate(_spec(), make_synthetic_ohlcv()))
        # No re-runs performed when disabled.
        self.assertEqual(len(s.wf.calls), 0)

    def test_all_positive_passes(self):
        s = _make_search(
            {"enabled": True, "n_seeds": 3, "warmup_jitter": 20},
            wf_scores=[0.5, 0.4, 0.6])
        self.assertTrue(s._passes_stability_gate(_spec(), make_synthetic_ohlcv()))
        self.assertEqual(len(s.wf.calls), 3)
        # Stability re-runs must never persist to memory.
        self.assertTrue(all(persist is False for (_, persist) in s.wf.calls))

    def test_one_negative_rejects(self):
        s = _make_search(
            {"enabled": True, "n_seeds": 3, "warmup_jitter": 20},
            wf_scores=[0.5, -0.01, 0.6])
        self.assertFalse(s._passes_stability_gate(_spec(), make_synthetic_ohlcv()))
        # Should short-circuit on the failing 2nd run (no 3rd call).
        self.assertEqual(len(s.wf.calls), 2)

    def test_zero_score_rejects(self):
        s = _make_search(
            {"enabled": True, "n_seeds": 2, "warmup_jitter": 0},
            wf_scores=[0.0])
        self.assertFalse(s._passes_stability_gate(_spec(), make_synthetic_ohlcv()))

    def test_warmup_jitter_band_and_floor(self):
        s = _make_search(
            {"enabled": True, "n_seeds": 5, "warmup_jitter": 20},
            wf_scores=[1.0, 1.0, 1.0, 1.0, 1.0])
        self.assertTrue(s._passes_stability_gate(_spec(), make_synthetic_ohlcv()))
        for warmup, _ in s.wf.calls:
            # base 60 +/- 20 -> [40, 80]; floor of 20 always respected.
            self.assertGreaterEqual(warmup, 20)
            self.assertGreaterEqual(warmup, 40)
            self.assertLessEqual(warmup, 80)


if __name__ == "__main__":
    unittest.main()
