"""
Offline tests for the regime router (UPGRADE_PLAN.md U6.2).

The router promotes a per-regime champion (ATR%/ADX terciles) and routes each
bar to the champion of the CURRENT regime instead of averaging strategies that
disagree; its composite (RegimeRouterStrategy) is walk-forward scorable so it can
pass the U2.5 "no unvalidated composite goes live" rule.

These tests use only a deterministic synthetic series + a temp file (no MT5, no
network). They assert:

  1. train() picks a champion per sufficiently-populated regime, save()/load()
     round-trips the champion map, and is_ready() reflects it.
  2. RegimeRouterStrategy is Strategy-compatible (decision_series / signal_series
     / atr_series all return one value per bar) so the Backtester can score it.
  3. An untrained/empty router routes to flat (never trades) - the graceful
     fallback the DecisionEngine relies on.

All text is standard ASCII English only.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from tests.helpers import make_synthetic_ohlcv  # noqa: F401 (path fix)

from config.loader import load_config
from core.strategy.strategy import StrategySpec
from core.strategy.backtester import Backtester
from core.strategy.regime_router import RegimeRouter, RegimeRouterStrategy


def _cfg():
    cfg = load_config()
    return cfg


def _specs():
    return [
        StrategySpec(indicators={"ema": {"period": 10}, "rsi": {"period": 14}},
                     weights={"ema": 1.0, "rsi": 1.0},
                     long_threshold=0.1, short_threshold=0.1,
                     sl_atr_mult=2.0, tp_atr_mult=3.0,
                     symbol="TESTX", timeframe="M15", name="a"),
        StrategySpec(indicators={"sma": {"period": 20}, "adx": {"period": 14}},
                     weights={"sma": 1.0, "adx": 1.0},
                     long_threshold=0.15, short_threshold=0.15,
                     sl_atr_mult=1.5, tp_atr_mult=2.5,
                     symbol="TESTX", timeframe="M15", name="b"),
    ]


class TestRegimeRouter(unittest.TestCase):
    def setUp(self):
        self.cfg = _cfg()
        self.ohlcv = make_synthetic_ohlcv(n=1200)
        self.bt = Backtester(self.cfg)

    def _router(self, min_bars=50):
        r = RegimeRouter(self.cfg)
        r.enabled = True
        r.min_bars_per_regime = min_bars
        return r

    def test_train_saves_and_loads(self):
        r = self._router()
        champions = r.train(_specs(), self.ohlcv, self.bt)
        self.assertTrue(champions, "expected at least one regime champion")
        # Every champion entry must name a fingerprint + spec.
        for regime, entry in champions.items():
            self.assertIn("fingerprint", entry)
            self.assertIn("spec", entry)
            self.assertIsNotNone(r.champion_for(regime))
        self.assertTrue(r.is_ready())

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "champions.json")
            r.save(path)
            self.assertTrue(os.path.exists(path))
            r2 = self._router()
            self.assertTrue(r2.load(path))
            self.assertEqual(set(r2.champions().keys()),
                             set(champions.keys()))

    def test_router_strategy_is_backtestable(self):
        r = self._router()
        r.train(_specs(), self.ohlcv, self.bt)
        composite = RegimeRouterStrategy(r)
        n = len(self.ohlcv.close)
        dec = composite.decision_series(self.ohlcv)
        sig = composite.signal_series(self.ohlcv)
        atr = composite.atr_series(self.ohlcv)
        self.assertEqual(len(dec), n)
        self.assertEqual(len(sig), n)
        self.assertEqual(len(atr), n)
        # Every decision is a legal position code.
        self.assertTrue(all(d in (-1, 0, 1) for d in dec))
        # The composite runs through the pessimistic backtester without raising.
        result = self.bt.run(composite, self.ohlcv, warmup=60, record_trades=True)
        self.assertIsNotNone(result.metrics)

    def test_empty_router_routes_flat(self):
        composite = RegimeRouterStrategy(self._router())  # no train()
        dec = composite.decision_series(self.ohlcv)
        self.assertTrue(all(d == 0 for d in dec),
                        "untrained router must never trade")


if __name__ == "__main__":
    unittest.main()
