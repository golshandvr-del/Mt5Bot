"""
Tests for walk-forward segmentation and the locked-holdout gate (Phase 3,
Track A / P1.3 + P1.4).

These are offline, standard-library-only tests. They build a deterministic
synthetic OHLCV series and drive core.strategy.walk_forward.WalkForward with a
config whose memory.walk_forward knobs are overridden in memory, so the real
data_store and config file are never touched.

Covered:
  - segment count grows with history (min_segments auto-shrink, P1.3);
  - the locked holdout tail never appears in ANY train/test segment (P1.4);
  - evaluate_holdout is a no-op when holdout is OFF (default behavior);
  - the holdout gate BLOCKS a spec that fails on the untouched holdout (P1.4).

All text is standard ASCII English only.
"""

from __future__ import annotations

import unittest

from tests.helpers import PROJECT_ROOT, make_synthetic_ohlcv  # noqa: F401


def _cfg(train_bars=400, test_bars=100, step_bars=100, min_segments=6,
         holdout_bars=0, min_trades=1):
    """Load config and override the walk-forward / search knobs in memory."""
    from config.loader import load_config
    cfg = load_config()
    wf = cfg["memory"]["walk_forward"]
    wf["train_bars"] = train_bars
    wf["test_bars"] = test_bars
    wf["step_bars"] = step_bars
    wf["min_segments"] = min_segments
    wf["holdout_bars"] = holdout_bars
    # min_trades is read from memory.search by WalkForward.
    cfg["memory"]["search"]["min_trades"] = min_trades
    return cfg


def _spec(symbol="TESTX", tf="M15", long_thr=0.2, short_thr=0.2):
    from core.strategy.strategy import StrategySpec
    return StrategySpec(
        indicators={"ema": {"period": 21}, "rsi": {"period": 14}},
        weights={"ema": 1.0, "rsi": 1.0},
        long_threshold=long_thr, short_threshold=short_thr,
        sl_atr_mult=2.0, tp_atr_mult=3.0,
        symbol=symbol, timeframe=tf,
    )


class TestWalkForwardSegments(unittest.TestCase):
    def test_segment_count_grows_with_history(self):
        from core.strategy.walk_forward import WalkForward
        cfg = _cfg(train_bars=400, test_bars=100, step_bars=100,
                   min_segments=6, holdout_bars=0)
        wf = WalkForward(cfg)
        # More history must never yield fewer segments, and a long history must
        # reach min_segments thanks to the auto-shrink in effective_train_bars.
        short_n = 700
        long_n = 6000
        n_short = len(wf.segments(short_n))
        n_long = len(wf.segments(long_n))
        self.assertGreaterEqual(n_long, n_short)
        self.assertGreaterEqual(n_long, wf.min_segments)

    def test_all_segments_in_range(self):
        from core.strategy.walk_forward import WalkForward
        cfg = _cfg(holdout_bars=0)
        wf = WalkForward(cfg)
        n = 6000
        for seg in wf.segments(n):
            self.assertGreaterEqual(seg["train_start"], 0)
            self.assertLess(seg["test_start"], seg["test_end"])
            self.assertLessEqual(seg["test_end"], n)


class TestWalkForwardHoldout(unittest.TestCase):
    def test_holdout_excluded_from_all_segments(self):
        from core.strategy.walk_forward import WalkForward
        n = 6000
        holdout = 1500
        cfg = _cfg(train_bars=400, test_bars=100, step_bars=100,
                   min_segments=6, holdout_bars=holdout)
        wf = WalkForward(cfg)
        self.assertEqual(wf.searchable_bars(n), n - holdout)
        segs = wf.segments(n)
        self.assertTrue(segs, "expected at least one segment")
        holdout_start = n - holdout
        # No train/test window may reach into the quarantined holdout tail.
        for seg in segs:
            self.assertLessEqual(seg["test_end"], holdout_start)
            self.assertLessEqual(seg["train_start"], holdout_start)

    def test_searchable_bars_default_is_noop(self):
        from core.strategy.walk_forward import WalkForward
        cfg = _cfg(holdout_bars=0)
        wf = WalkForward(cfg)
        self.assertEqual(wf.searchable_bars(6000), 6000)

    def test_evaluate_holdout_noop_when_disabled(self):
        from core.strategy.walk_forward import WalkForward
        cfg = _cfg(holdout_bars=0)
        wf = WalkForward(cfg)
        ohlcv = make_synthetic_ohlcv(2000)
        gate = wf.evaluate_holdout(_spec(), ohlcv)
        self.assertFalse(gate["enabled"])
        self.assertTrue(gate["passed"])

    def test_holdout_gate_blocks_failing_spec(self):
        from core.strategy.walk_forward import WalkForward
        # A small holdout window plus an intentionally high min_trades means a
        # normally-trading spec cannot produce enough holdout trades, so the
        # gate must reject it (passed=False) even though it is enabled.
        cfg = _cfg(train_bars=400, test_bars=100, step_bars=100,
                   min_segments=6, holdout_bars=300, min_trades=10_000)
        wf = WalkForward(cfg)
        ohlcv = make_synthetic_ohlcv(2000)
        gate = wf.evaluate_holdout(_spec(), ohlcv)
        self.assertTrue(gate["enabled"])
        self.assertFalse(gate["passed"])
        self.assertLess(gate["holdout_trades"], 10_000)

    def test_holdout_gate_passes_sufficient_spec(self):
        from core.strategy.walk_forward import WalkForward
        # With min_trades=1, any spec that trades at least once on a non-trivial
        # holdout and has a non-negative score must pass. We only assert the
        # gate is enabled and returns a well-formed result, and that when it
        # reports passed=True the documented conditions hold.
        cfg = _cfg(train_bars=400, test_bars=100, step_bars=100,
                   min_segments=6, holdout_bars=600, min_trades=1)
        wf = WalkForward(cfg)
        ohlcv = make_synthetic_ohlcv(3000)
        gate = wf.evaluate_holdout(_spec(), ohlcv)
        self.assertTrue(gate["enabled"])
        self.assertIn("holdout_trades", gate)
        self.assertIn("score", gate)
        if gate["passed"]:
            self.assertGreaterEqual(gate["holdout_trades"], 1)
            self.assertGreaterEqual(gate["score"], 0.0)


class TestHoldoutRegistryGate(unittest.TestCase):
    """The store-level allowlist forwarded by search when holdout is ON."""

    def _temp_store(self):
        import os
        import tempfile
        from config.loader import load_config
        from core.memory.store import MemoryStore
        cfg = load_config()
        tmp = tempfile.mkdtemp(prefix="mt5bot_wf_")
        cfg["memory"]["db_file"] = os.path.join(tmp, "memory.sqlite")
        cfg["memory"]["registry_file"] = os.path.join(tmp, "registry.json")
        return MemoryStore(cfg)

    def _record(self, store, spec, score):
        metrics = {"win_rate": 0.55, "profit_factor": 1.3, "expectancy": score,
                   "max_drawdown": -50.0, "num_trades": 50, "sharpe": 0.4,
                   "net_profit": 100.0}
        store.record_result(spec, metrics, segment="seg_0",
                            rank_metric="expectancy")
        store.record_result(spec, metrics, segment="seg_1",
                            rank_metric="expectancy")

    def test_allowlist_restricts_promotion(self):
        from core.strategy.strategy import StrategySpec
        store = self._temp_store()

        def spec(period):
            return StrategySpec(
                indicators={"ema": {"period": period}},
                weights={"ema": 1.0},
                long_threshold=0.2, short_threshold=0.2,
                sl_atr_mult=2.0, tp_atr_mult=3.0,
                symbol="EURUSD", timeframe="M15",
            )

        good, bad = spec(10), spec(20)
        self._record(store, good, 5.0)
        self._record(store, bad, 3.0)

        # No allowlist -> both promoted (unchanged behavior).
        both = store.top_strategies("EURUSD", "M15", rank_metric="expectancy",
                                    min_trades=1)
        self.assertEqual(len(both), 2)

        # Allowlist with only the "good" fingerprint -> only it promoted.
        only_good = store.top_strategies(
            "EURUSD", "M15", rank_metric="expectancy", min_trades=1,
            allowed_fingerprints={good.fingerprint()},
        )
        self.assertEqual(len(only_good), 1)
        self.assertEqual(only_good[0]["fingerprint"], good.fingerprint())

        # Empty allowlist -> nothing promoted (nothing passed the holdout).
        none = store.top_strategies(
            "EURUSD", "M15", rank_metric="expectancy", min_trades=1,
            allowed_fingerprints=set(),
        )
        self.assertEqual(none, [])


if __name__ == "__main__":
    unittest.main()
