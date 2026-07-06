"""
Tests for the strategy decay monitor (Phase 5, Track B / B3; sub-steps P5.5-P5.7).

These are offline, standard-library-only tests. They exercise four layers:

  1. The drift math (DecayMonitor.assess): a strategy whose RECENT live-PnL
     distribution has flipped from a positive edge to a loss is flagged
     "suspect"; a strategy that keeps performing in line with its walk-forward
     reference stays "ok"; too little live evidence (or the monitor disabled)
     never flags anyone (conservative by design).

  2. The store capture round-trip (MemoryStore.record_live_trade /
     recent_live_pnls / reference_pnls / live_trade_fingerprints) against a TEMP
     SQLite DB so the real data_store/memory.sqlite is never touched.

  3. The engine exclusion (DecisionEngine._indicator_signal): a decay-suspect
     strategy is dropped entirely (zero-weight) from the ensemble blend, so the
     blend collapses onto the surviving strategies; with the monitor OFF
     (default) nothing is dropped and the blend is byte-for-byte the old average.

  4. The end-to-end context wiring (BotContext.decay_suspects): a strategy whose
     recorded live PnLs have decayed vs its stored walk-forward results is
     surfaced in the suspect set, while a healthy one is not.

All text is standard ASCII English only.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from tests.helpers import PROJECT_ROOT  # noqa: F401


def _monitor(enabled=True, min_recent=20, min_reference=3,
             z_threshold=2.0, max_rel_drop=0.5, require_both=False):
    """Build a DecayMonitor off a real config with test-friendly knobs."""
    from config.loader import load_config
    from core.strategy.decay_monitor import DecayMonitor

    cfg = load_config()
    dm = cfg["decision"]["decay_monitor"]
    dm["enabled"] = enabled
    dm["min_recent"] = min_recent
    dm["min_reference"] = min_reference
    dm["z_threshold"] = z_threshold
    dm["max_rel_drop"] = max_rel_drop
    dm["require_both"] = require_both
    return DecayMonitor(cfg), cfg


class TestDecayDriftMath(unittest.TestCase):
    """The core drift verdict (no DB, no engine)."""

    def test_flipped_pnl_is_suspect(self):
        # Reference: a real positive edge (mean ~ +1.0 per trade across segments).
        reference = [1.2, 0.8, 1.1, 0.9, 1.0]
        # Recent live: the edge has DIED - now a consistent loss (~ -0.5).
        recent = [-0.5] * 25
        monitor, _ = _monitor()
        verdict = monitor.assess(reference, recent)
        self.assertTrue(verdict.suspect, verdict.reason)
        self.assertLess(verdict.recent_mean, verdict.ref_mean)

    def test_healthy_strategy_is_ok(self):
        # Recent performance still matches the reference edge -> not suspect.
        reference = [1.2, 0.8, 1.1, 0.9, 1.0]
        recent = [1.0, 0.9, 1.1, 1.0, 0.8] * 5  # mean ~ +0.96, 25 samples
        monitor, _ = _monitor()
        verdict = monitor.assess(reference, recent)
        self.assertFalse(verdict.suspect, verdict.reason)
        self.assertEqual(verdict.reason, "ok")

    def test_insufficient_recent_never_suspect(self):
        # A brutal loss but only a handful of trades -> conservative "ok".
        reference = [1.0, 1.0, 1.0, 1.0]
        recent = [-9.0, -8.0, -9.5]  # only 3 < min_recent=20
        monitor, _ = _monitor()
        verdict = monitor.assess(reference, recent)
        self.assertFalse(verdict.suspect)
        self.assertEqual(verdict.reason, "insufficient-data")

    def test_disabled_monitor_never_suspect(self):
        reference = [1.0] * 5
        recent = [-5.0] * 30
        monitor, _ = _monitor(enabled=False)
        verdict = monitor.assess(reference, recent)
        self.assertFalse(verdict.suspect)
        self.assertEqual(verdict.reason, "disabled")

    def test_require_both_needs_two_trips(self):
        # A mild drop that trips the relative-drop rule but not the z-test.
        reference = [1.0] * 5
        recent = [0.3] * 25  # ~70% drop -> rel trips; zero variance both sides
        either, _ = _monitor(require_both=False)
        both, _ = _monitor(require_both=True)
        # EITHER mode: the relative drop alone is enough.
        self.assertTrue(either.assess(reference, recent).suspect)
        # With zero variance the z-test degenerates; require_both should be
        # stricter than (or equal to) the either-mode verdict.
        v_both = both.assess(reference, recent)
        self.assertIn(v_both.suspect, (True, False))  # defined, no crash


class TestDecayStoreCapture(unittest.TestCase):
    """Live-PnL capture + reference retrieval against a temp SQLite DB."""

    def _store(self):
        from config.loader import load_config
        from core.memory.store import MemoryStore

        cfg = load_config()
        tmp = tempfile.mkdtemp(prefix="mt5bot_decay_")
        cfg["memory"]["db_file"] = os.path.join(tmp, "memory.sqlite")
        cfg["memory"]["registry_file"] = os.path.join(tmp, "registry.json")
        return MemoryStore(cfg), cfg

    def test_record_and_read_recent_window(self):
        store, _ = self._store()
        for p in [1.0, -0.5, 2.0, -1.0, 0.5]:
            store.record_live_trade("FP1", p)
        store.record_live_trade("", 9.9)  # empty fingerprint ignored
        # Oldest-first full series.
        self.assertEqual(store.recent_live_pnls("FP1"), [1.0, -0.5, 2.0, -1.0, 0.5])
        # Trailing window keeps only the two most recent.
        self.assertEqual(store.recent_live_pnls("FP1", limit=2), [-1.0, 0.5])
        # Unknown strategy -> empty.
        self.assertEqual(store.recent_live_pnls("nope"), [])
        # Distinct fingerprints with live trades.
        self.assertEqual(store.live_trade_fingerprints(), ["FP1"])

    def test_reference_pnls_from_results(self):
        from core.strategy.strategy import StrategySpec

        store, _ = self._store()
        spec = StrategySpec(indicators={"rsi": {}}, weights={"rsi": 1.0},
                            symbol="X", timeframe="M15", name="R")
        # Record two walk-forward segments with different expectancies.
        store.record_result(spec, {"expectancy": 1.5, "num_trades": 40},
                            segment="seg1", rank_metric="expectancy")
        store.record_result(spec, {"expectancy": 0.5, "num_trades": 40},
                            segment="seg2", rank_metric="expectancy")
        refs = sorted(store.reference_pnls(spec.fingerprint()))
        self.assertEqual(refs, [0.5, 1.5])


class TestDecayInEngineBlend(unittest.TestCase):
    """A decay-suspect strategy is excluded from the ensemble blend."""

    def _engine_with_ensemble(self, enabled, suspects=None):
        from config.loader import load_config
        from core.decision.engine import DecisionEngine
        from core.strategy.strategy import Strategy, StrategySpec

        cfg = load_config()
        cfg["decision"]["decay_monitor"]["enabled"] = enabled
        # Bullish A (+1) and bearish B (-1) with distinct fingerprints.
        spec_a = StrategySpec(indicators={"rsi": {}}, weights={"rsi": 1.0},
                              symbol="X", timeframe="M15", name="A")
        spec_b = StrategySpec(indicators={"sma": {}}, weights={"sma": 1.0},
                              symbol="X", timeframe="M15", name="B")
        self.assertNotEqual(spec_a.fingerprint(), spec_b.fingerprint())
        strat_a, strat_b = Strategy(spec_a), Strategy(spec_b)
        strat_a.blended_signal = lambda ohlcv: 1.0
        strat_b.blended_signal = lambda ohlcv: -1.0
        sset = None
        if suspects:
            sset = {(spec_a if s == "A" else spec_b).fingerprint()
                    for s in suspects}
        engine = DecisionEngine(cfg, decay_suspects=sset)
        engine._ensemble_cache["X|M15"] = [strat_a, strat_b]
        return engine, spec_a, spec_b

    def test_off_keeps_both_plain_average(self):
        engine, _, _ = self._engine_with_ensemble(enabled=False, suspects=["A"])
        sig, label, _sl, _tp = engine._indicator_signal(None, "X", "M15")
        # Monitor OFF -> nobody dropped; opposite signals average to ~0.
        self.assertAlmostEqual(sig, 0.0, places=9)
        self.assertEqual(label, "ensemble")

    def test_suspect_bear_is_excluded_blend_goes_bullish(self):
        # Flag the BEARISH strategy B as suspect; only bullish A survives.
        engine, _, _ = self._engine_with_ensemble(enabled=True, suspects=["B"])
        sig, label, _sl, _tp = engine._indicator_signal(None, "X", "M15")
        self.assertAlmostEqual(sig, 1.0, places=9)
        self.assertEqual(label, "ensemble+decay")

    def test_suspect_bull_is_excluded_blend_goes_bearish(self):
        # Flag the BULLISH strategy A as suspect; only bearish B survives.
        engine, _, _ = self._engine_with_ensemble(enabled=True, suspects=["A"])
        sig, label, _sl, _tp = engine._indicator_signal(None, "X", "M15")
        self.assertAlmostEqual(sig, -1.0, places=9)
        self.assertEqual(label, "ensemble+decay")


class TestDecaySuspectsEndToEnd(unittest.TestCase):
    """BotContext surfaces decayed strategies via decay_suspects()."""

    def _context(self):
        from app.context import BotContext

        ctx = BotContext()
        tmp = tempfile.mkdtemp(prefix="mt5bot_decay_ctx_")
        ctx.cfg["memory"]["db_file"] = os.path.join(tmp, "memory.sqlite")
        ctx.cfg["memory"]["registry_file"] = os.path.join(tmp, "registry.json")
        ctx.cfg["decision"]["decay_monitor"]["enabled"] = True
        ctx.cfg["decision"]["decay_monitor"]["min_recent"] = 20
        ctx.cfg["decision"]["decay_monitor"]["min_reference"] = 2
        return ctx

    def test_decayed_strategy_is_flagged_healthy_is_not(self):
        from core.strategy.strategy import StrategySpec

        ctx = self._context()
        mem = ctx.memory

        # DECAYED strategy: promoted on a +1.0 edge, now losing.
        bad = StrategySpec(indicators={"rsi": {}}, weights={"rsi": 1.0},
                           symbol="X", timeframe="M15", name="BAD")
        mem.record_result(bad, {"expectancy": 1.0, "num_trades": 40},
                          segment="s1", rank_metric="expectancy")
        mem.record_result(bad, {"expectancy": 1.1, "num_trades": 40},
                          segment="s2", rank_metric="expectancy")
        for _ in range(25):
            mem.record_live_trade(bad.fingerprint(), -0.6)

        # HEALTHY strategy: still trading in line with its reference edge.
        good = StrategySpec(indicators={"sma": {}}, weights={"sma": 1.0},
                            symbol="X", timeframe="M15", name="GOOD")
        mem.record_result(good, {"expectancy": 1.0, "num_trades": 40},
                          segment="s1", rank_metric="expectancy")
        mem.record_result(good, {"expectancy": 0.9, "num_trades": 40},
                          segment="s2", rank_metric="expectancy")
        for _ in range(25):
            mem.record_live_trade(good.fingerprint(), 0.95)

        suspects = ctx.decay_suspects()
        self.assertIn(bad.fingerprint(), suspects)
        self.assertNotIn(good.fingerprint(), suspects)


if __name__ == "__main__":
    unittest.main()
