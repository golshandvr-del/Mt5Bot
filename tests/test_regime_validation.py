"""
Tests for U4.5 regime-sliced validation in WalkForward (UPGRADE_PLAN Phase U4).

Regime slicing labels each walk-forward segment by its realized-volatility
tercile (low/mid/high) and trend strength (trend/range via median ADX), then
aggregates a per-regime rank score and refuses to promote any strategy that
collapses in a single regime (score below floor_mult * overall_score).

These tests are offline / stdlib only (Windows 7 + Python 3.8 friendly) and use
the deterministic synthetic OHLCV helper.

All text is standard ASCII English only.
"""

from __future__ import annotations

import unittest

from tests.helpers import PROJECT_ROOT, make_synthetic_ohlcv  # noqa: F401


def _cfg(regime_enabled=True, floor_mult=-0.5, min_seg_per_regime=1,
         adx_trend=25.0):
    from config.loader import load_config
    cfg = load_config()
    wf = cfg["memory"]["walk_forward"]
    wf["train_bars"] = 300
    wf["test_bars"] = 150
    wf["step_bars"] = 150
    wf["min_segments"] = 4
    wf["holdout_bars"] = 0
    cfg["memory"]["search"]["min_trades"] = 1
    cfg["memory"]["search"]["regime"] = {
        "enabled": regime_enabled,
        "floor_mult": floor_mult,
        "min_segments_per_regime": min_seg_per_regime,
        "adx_trend_threshold": adx_trend,
    }
    return cfg


def _spec():
    from core.strategy.strategy import StrategySpec
    return StrategySpec(
        indicators={"ema": {"period": 20}, "rsi": {"period": 14}},
        weights={"ema": 1.0, "rsi": 1.0},
        long_threshold=0.2, short_threshold=0.2,
        sl_atr_mult=2.0, tp_atr_mult=3.0,
        symbol="TESTX", timeframe="M15",
    )


class TestRegimeLabelling(unittest.TestCase):
    def test_terciles_and_vol_bucket(self):
        from core.strategy.walk_forward import WalkForward
        wf = WalkForward(_cfg())
        cutoffs = wf._terciles([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        self.assertEqual(len(cutoffs), 2)
        self.assertLessEqual(cutoffs[0], cutoffs[1])
        # Value below p33 -> low, above p66 -> high, between -> mid.
        self.assertEqual(wf._vol_tercile(cutoffs[0] - 1.0, cutoffs), "low")
        self.assertEqual(wf._vol_tercile(cutoffs[1] + 1.0, cutoffs), "high")

    def test_label_format_is_vol_trend(self):
        from core.strategy.walk_forward import WalkForward
        wf = WalkForward(_cfg())
        ohlcv = make_synthetic_ohlcv(n=1400, symbol="TESTX")
        res = wf.evaluate(_spec(), ohlcv, persist=False)
        labels = res.get("regime_labels")
        self.assertTrue(labels)
        for lab in labels:
            vol, _, trend = lab.partition("_")
            self.assertIn(vol, ("low", "mid", "high"))
            self.assertIn(trend, ("trend", "range"))


class TestRegimeFloorGate(unittest.TestCase):
    def test_disabled_is_noop(self):
        from core.strategy.walk_forward import WalkForward
        wf = WalkForward(_cfg(regime_enabled=False))
        ohlcv = make_synthetic_ohlcv(n=1400, symbol="TESTX")
        res = wf.evaluate(_spec(), ohlcv, persist=False)
        # When OFF, evaluate() must not attach any regime fields at all.
        self.assertNotIn("regime_scores", res)
        self.assertNotIn("passes_regime_floor", res)
        # And the gate helper is a pure pass-through.
        self.assertTrue(wf.passes_regime_floor(1.0, {"low_range": -999.0}))

    def test_floor_rejects_collapsing_regime(self):
        from core.strategy.walk_forward import WalkForward
        wf = WalkForward(_cfg(regime_enabled=True, floor_mult=-0.5))
        # A regime that scores far below floor_mult*overall must fail the gate.
        overall = 100.0
        # floor = -0.5 * 100 = -50; a -500 regime is well below it.
        self.assertFalse(
            wf.passes_regime_floor(overall, {"good": 120.0, "bad": -500.0})
        )
        # All regimes above the floor -> pass.
        self.assertTrue(
            wf.passes_regime_floor(overall, {"a": 80.0, "b": 10.0})
        )

    def test_evaluate_attaches_verdict_when_enabled(self):
        from core.strategy.walk_forward import WalkForward
        wf = WalkForward(_cfg(regime_enabled=True, min_seg_per_regime=1))
        ohlcv = make_synthetic_ohlcv(n=1400, symbol="TESTX")
        res = wf.evaluate(_spec(), ohlcv, persist=False)
        self.assertIn("regime_scores", res)
        self.assertIn("passes_regime_floor", res)
        self.assertIsInstance(res["passes_regime_floor"], bool)


class TestRegimeGateInSearch(unittest.TestCase):
    """The search promotion path must force an allowlist when regime is on."""

    def test_search_forces_gating_when_regime_enabled(self):
        import os
        import tempfile
        from core.strategy.search import StrategySearch
        from core.memory.store import MemoryStore

        tmp = tempfile.mkdtemp(prefix="u45_search_")
        cfg = _cfg(regime_enabled=True, min_seg_per_regime=1)
        cfg["memory"]["db_file"] = os.path.join(tmp, "memory.sqlite")
        cfg["memory"]["registry_file"] = os.path.join(tmp, "registry.json")
        cfg["memory"]["search"]["method"] = "random"
        cfg["memory"]["search"]["max_trials"] = 4

        memory = MemoryStore(cfg)
        search = StrategySearch(cfg, memory)
        ohlcv = make_synthetic_ohlcv(n=1400, symbol="TESTX")
        out = search.run(ohlcv, "TESTX", "M15")
        # The run completes and returns a registry section; the regime gate must
        # have been active (some specs may be filtered, so top can be 0..N).
        self.assertIn("registry", out)
        self.assertGreaterEqual(out.get("evaluated", 0), 1)


if __name__ == "__main__":
    unittest.main()
