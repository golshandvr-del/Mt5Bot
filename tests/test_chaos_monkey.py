"""
Tests for the U6.6 chaos-monkey harness (UPGRADE_PLAN Phase U6).

The chaos-monkey injects broker nastiness (requotes, missed bars, spread
storms, partial fills) into a COPY of a clean price history and classifies each
registry strategy as GRACEFUL / FRAGILE / SHATTERED by how much of its edge
survives. These tests cover:

  - the pure data injectors preserve bar validity and are seed-reproducible;
  - missed bars actually shrink the series while keeping the edge bars;
  - requotes only touch OPENs (high/low/close untouched) and stay in-bar;
  - the cost/lot config override widens spread and shrinks the effective lot;
  - the GRACEFUL / FRAGILE / SHATTERED classifier boundaries;
  - the whole thing is a NO-OP when the config gate is off (default);
  - a full registry sweep runs end-to-end and returns a well-formed report.

Offline / stdlib only (Windows 7 + Python 3.8 friendly). ASCII English only.
"""

from __future__ import annotations

import random
import unittest

from tests.helpers import make_synthetic_ohlcv


def _cfg(enabled=True, **overrides):
    from config.loader import load_config
    cfg = load_config()
    block = {
        "enabled": enabled,
        "seed": 20240607,
        "spread_storm": False,
        "spread_mult": 2.0,
        "requotes": False,
        "requote_frac": 0.15,
        "requote_points": 8.0,
        "missed_bars": False,
        "missed_frac": 0.05,
        "partial_fills": False,
        "partial_frac": 0.20,
        "partial_min_scale": 0.5,
        "graceful_floor_mult": 0.4,
        "catastrophe_mult": -0.25,
    }
    block.update(overrides)
    cfg["general"]["chaos_monkey"] = block
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


class TestConfigGate(unittest.TestCase):
    def test_default_config_is_off(self):
        from config.loader import load_config
        from core.strategy.chaos_monkey import ChaosConfig
        ccfg = ChaosConfig(load_config())
        self.assertFalse(ccfg.enabled)
        self.assertFalse(ccfg.any_nastiness())

    def test_parses_switches(self):
        from core.strategy.chaos_monkey import ChaosConfig
        ccfg = ChaosConfig(_cfg(spread_storm=True, requotes=True))
        self.assertTrue(ccfg.enabled)
        self.assertTrue(ccfg.spread_storm)
        self.assertTrue(ccfg.requotes)
        self.assertTrue(ccfg.any_nastiness())
        self.assertTrue(ccfg.any_data_nastiness())

    def test_fractions_clamped(self):
        from core.strategy.chaos_monkey import ChaosConfig
        ccfg = ChaosConfig(_cfg(requote_frac=5.0, missed_frac=-1.0))
        self.assertEqual(ccfg.requote_frac, 1.0)
        self.assertEqual(ccfg.missed_frac, 0.0)


class TestInjectors(unittest.TestCase):
    def test_missed_bars_shrinks_and_keeps_edges(self):
        from core.strategy.chaos_monkey import inject_missed_bars
        ohlcv = make_synthetic_ohlcv(500, "TESTX", "M15", seed=7)
        rng = random.Random(123)
        out = inject_missed_bars(ohlcv, 0.2, rng)
        self.assertLess(len(out), len(ohlcv))
        self.assertGreater(len(out), 0)
        # First and last bars must always survive.
        self.assertEqual(out.time[0], ohlcv.time[0])
        self.assertEqual(out.time[-1], ohlcv.time[-1])

    def test_missed_bars_noop_when_frac_zero(self):
        from core.strategy.chaos_monkey import inject_missed_bars
        ohlcv = make_synthetic_ohlcv(300, "TESTX", "M15", seed=1)
        out = inject_missed_bars(ohlcv, 0.0, random.Random(1))
        self.assertEqual(len(out), len(ohlcv))

    def test_requotes_only_touch_opens_and_stay_in_bar(self):
        from core.strategy.chaos_monkey import inject_requotes
        ohlcv = make_synthetic_ohlcv(400, "TESTX", "M15", seed=3)
        out = inject_requotes(ohlcv, 0.5, 8.0, random.Random(9))
        # high/low/close untouched.
        self.assertEqual(list(out.high), list(ohlcv.high))
        self.assertEqual(list(out.low), list(ohlcv.low))
        self.assertEqual(list(out.close), list(ohlcv.close))
        # some opens changed
        changed = sum(1 for a, b in zip(out.open, ohlcv.open) if a != b)
        self.assertGreater(changed, 0)
        # every open stays within its own [low, high]
        for i in range(len(out)):
            self.assertGreaterEqual(out.open[i], out.low[i] - 1e-9)
            self.assertLessEqual(out.open[i], out.high[i] + 1e-9)

    def test_injectors_are_seed_reproducible(self):
        from core.strategy.chaos_monkey import build_chaos_series, ChaosConfig
        ohlcv = make_synthetic_ohlcv(400, "TESTX", "M15", seed=5)
        ccfg = ChaosConfig(_cfg(requotes=True, missed_bars=True))
        a = build_chaos_series(ohlcv, ccfg, random.Random(ccfg.seed))
        b = build_chaos_series(ohlcv, ccfg, random.Random(ccfg.seed))
        self.assertEqual(list(a.open), list(b.open))
        self.assertEqual(list(a.time), list(b.time))
        self.assertEqual(len(a), len(b))


class TestConfigOverride(unittest.TestCase):
    def test_spread_storm_widens_spread(self):
        from core.strategy.chaos_monkey import ChaosConfig, chaos_config_override
        cfg = _cfg(spread_storm=True, spread_mult=3.0)
        base = float(cfg["backtest"]["spread_points"])
        ccfg = ChaosConfig(cfg)
        clone = chaos_config_override(cfg, ccfg)
        self.assertAlmostEqual(float(clone["backtest"]["spread_points"]),
                               base * 3.0)

    def test_partial_fills_shrink_lot(self):
        from core.strategy.chaos_monkey import ChaosConfig, chaos_config_override
        cfg = _cfg(partial_fills=True, partial_frac=1.0, partial_min_scale=0.5)
        base_lot = float(cfg["backtest"]["fixed_lot"])
        ccfg = ChaosConfig(cfg)
        clone = chaos_config_override(cfg, ccfg)
        # frac=1.0, min_scale=0.5 => eff = 0.5
        self.assertAlmostEqual(float(clone["backtest"]["fixed_lot"]),
                               base_lot * 0.5)

    def test_override_noop_when_off(self):
        from core.strategy.chaos_monkey import ChaosConfig, chaos_config_override
        cfg = _cfg(enabled=True)  # nothing on
        base = float(cfg["backtest"]["spread_points"])
        clone = chaos_config_override(cfg, ChaosConfig(cfg))
        self.assertEqual(float(clone["backtest"]["spread_points"]), base)


class TestClassifier(unittest.TestCase):
    def _ccfg(self, **kw):
        from core.strategy.chaos_monkey import ChaosConfig
        return ChaosConfig(_cfg(**kw))

    def test_graceful_when_edge_retained(self):
        from core.strategy.chaos_monkey import classify
        ccfg = self._ccfg()  # graceful_floor_mult=0.4
        self.assertEqual(classify(100.0, 60.0, 10000.0, ccfg), "GRACEFUL")

    def test_fragile_when_edge_shrinks_below_floor(self):
        from core.strategy.chaos_monkey import classify
        ccfg = self._ccfg()
        self.assertEqual(classify(100.0, 20.0, 10000.0, ccfg), "FRAGILE")

    def test_shattered_when_negative(self):
        from core.strategy.chaos_monkey import classify
        ccfg = self._ccfg()
        self.assertEqual(classify(100.0, -5.0, 10000.0, ccfg), "SHATTERED")

    def test_shattered_when_catastrophic(self):
        from core.strategy.chaos_monkey import classify
        ccfg = self._ccfg()
        # positive-but-catastrophe is impossible; test the catastrophe floor via
        # a negative chaos net below -25% of balance.
        self.assertEqual(classify(100.0, -3000.0, 10000.0, ccfg), "SHATTERED")

    def test_no_clean_edge_positive_chaos_is_graceful(self):
        from core.strategy.chaos_monkey import classify
        ccfg = self._ccfg()
        self.assertEqual(classify(-50.0, 10.0, 10000.0, ccfg), "GRACEFUL")

    def test_degradation_ratio(self):
        from core.strategy.chaos_monkey import degradation_ratio
        self.assertAlmostEqual(degradation_ratio(100.0, 40.0), 0.4)
        self.assertIsNone(degradation_ratio(0.0, 40.0))
        self.assertIsNone(degradation_ratio(-1.0, 40.0))


class TestAssessStrategyAndRegistry(unittest.TestCase):
    def test_assess_strategy_returns_well_formed(self):
        from core.strategy.chaos_monkey import ChaosMonkey
        ohlcv = make_synthetic_ohlcv(600, "TESTX", "M15", seed=11)
        monkey = ChaosMonkey(_cfg(spread_storm=True, requotes=True,
                                  missed_bars=True, partial_fills=True))
        r = monkey.assess_strategy(_spec(), ohlcv, warmup=60)
        self.assertIn(r["verdict"], ("GRACEFUL", "FRAGILE", "SHATTERED"))
        for key in ("fingerprint", "clean_net_profit", "chaos_net_profit",
                    "clean_num_trades", "chaos_num_trades", "chaos_bars"):
            self.assertIn(key, r)
        # missed bars => chaos series shorter than the clean series.
        self.assertLessEqual(r["chaos_bars"], len(ohlcv))

    def test_assess_strategy_reproducible(self):
        from core.strategy.chaos_monkey import ChaosMonkey
        ohlcv = make_synthetic_ohlcv(600, "TESTX", "M15", seed=11)
        cfg = _cfg(spread_storm=True, requotes=True, missed_bars=True)
        r1 = ChaosMonkey(cfg).assess_strategy(_spec(), ohlcv, warmup=60)
        r2 = ChaosMonkey(cfg).assess_strategy(_spec(), ohlcv, warmup=60)
        self.assertEqual(r1["chaos_net_profit"], r2["chaos_net_profit"])
        self.assertEqual(r1["verdict"], r2["verdict"])

    def test_assess_registry_sweep(self):
        from core.strategy.chaos_monkey import ChaosMonkey

        class _FakeMemory(object):
            def __init__(self, specs):
                self._specs = specs

            def load_registry_top(self, symbol, timeframe):
                return [{"spec": s.to_dict()} for s in self._specs]

        ohlcv = make_synthetic_ohlcv(600, "TESTX", "M15", seed=13)
        mem = _FakeMemory([_spec(), _spec()])
        monkey = ChaosMonkey(_cfg(spread_storm=True, requotes=True))
        report = monkey.assess_registry(mem, "TESTX", "M15", ohlcv, warmup=60)
        self.assertEqual(report["symbol"], "TESTX")
        counts = report["counts"]
        self.assertEqual(sum(counts.values()), len(report["strategies"]))
        self.assertEqual(len(report["strategies"]), 2)


if __name__ == "__main__":
    unittest.main()
