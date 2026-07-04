"""
Tests for the statistical-significance layer (Track A / A3, phases P2.1-P2.4).

Covers:
  1. wilson_interval on textbook / edge cases (P2.1).
  2. bootstrap_pvalue: low for a clearly-positive PnL series, high for a
     symmetric-random series, deterministic under a fixed seed (P2.2).
  3. compute_metrics carries win_rate_ci_low and pnl_pvalue (P2.3).
  4. the memory store PROMOTION filter: a non-significant strategy is recorded
     in SQLite but never promoted to the registry, while a significant one is,
     and apply_significance=False fetches the raw ranking (P2.4).

Uses a temporary DB + registry so the real data_store is never touched.
Standard-library only; no MT5, no network.

All text is standard ASCII English only.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from tests.helpers import PROJECT_ROOT  # noqa: F401


class TestWilsonInterval(unittest.TestCase):
    def test_known_50_of_100(self):
        from core.strategy.metrics import wilson_interval
        low, high = wilson_interval(50, 100)
        # Textbook Wilson 95% interval for 50/100 is ~ (0.4038, 0.5962).
        self.assertAlmostEqual(low, 0.4038, places=3)
        self.assertAlmostEqual(high, 0.5962, places=3)
        self.assertLessEqual(low, high)

    def test_bounds_stay_in_unit_interval(self):
        from core.strategy.metrics import wilson_interval
        # All-wins and all-losses must not escape [0, 1] and must be honest for
        # small n (10/10 does NOT give a lower bound near 1.0).
        low_all, high_all = wilson_interval(10, 10)
        self.assertGreaterEqual(low_all, 0.0)
        self.assertLessEqual(high_all, 1.0)
        self.assertLess(low_all, 1.0)
        low_none, high_none = wilson_interval(0, 10)
        self.assertGreaterEqual(low_none, 0.0)
        self.assertGreater(high_none, 0.0)

    def test_edge_cases(self):
        from core.strategy.metrics import wilson_interval
        self.assertEqual(wilson_interval(5, 0), (0.0, 0.0))       # n <= 0
        self.assertEqual(wilson_interval(3, 4, z=0.0), (0.75, 0.75))  # z <= 0
        # wins clamped into [0, n].
        low, high = wilson_interval(20, 10)
        self.assertLessEqual(high, 1.0)
        self.assertGreaterEqual(low, 0.0)


class TestBootstrapPvalue(unittest.TestCase):
    def test_positive_series_low_pvalue(self):
        from core.strategy.metrics import bootstrap_pvalue
        # A clearly-positive edge: almost every trade wins.
        pnls = [5.0] * 40 + [-1.0] * 5
        p = bootstrap_pvalue(pnls, n_boot=500, seed=42)
        self.assertLess(p, 0.05)

    def test_symmetric_series_high_pvalue(self):
        from core.strategy.metrics import bootstrap_pvalue
        # Symmetric around zero: no real edge -> p-value near 0.5 (well above
        # any reasonable significance threshold).
        pnls = ([1.0, -1.0] * 40)
        p = bootstrap_pvalue(pnls, n_boot=500, seed=42)
        self.assertGreater(p, 0.20)

    def test_edge_and_determinism(self):
        from core.strategy.metrics import bootstrap_pvalue
        # Conservative edge cases.
        self.assertEqual(bootstrap_pvalue([], n_boot=100), 1.0)
        self.assertEqual(bootstrap_pvalue([1.0, 2.0], n_boot=0), 1.0)
        # Deterministic under a fixed seed.
        pnls = [2.0, -1.0, 3.0, -0.5, 1.5, -2.0, 4.0]
        a = bootstrap_pvalue(pnls, n_boot=300, seed=7)
        b = bootstrap_pvalue(pnls, n_boot=300, seed=7)
        self.assertEqual(a, b)


class TestComputeMetricsSignificance(unittest.TestCase):
    def test_metrics_carry_significance_fields(self):
        from core.strategy.metrics import compute_metrics
        pnls = [3.0] * 30 + [-1.0] * 5
        equity = []
        bal = 0.0
        for p in pnls:
            bal += p
            equity.append(bal)
        m = compute_metrics(pnls, equity, n_boot=300, seed=42)
        self.assertIn("win_rate_ci_low", m)
        self.assertIn("pnl_pvalue", m)
        # Clearly-positive series: low p-value, positive win-rate lower bound.
        self.assertLess(m["pnl_pvalue"], 0.05)
        self.assertGreater(m["win_rate_ci_low"], 0.0)

    def test_empty_series_conservative_pvalue(self):
        from core.strategy.metrics import compute_metrics
        m = compute_metrics([], [], n_boot=100)
        self.assertEqual(m["pnl_pvalue"], 1.0)
        self.assertEqual(m["win_rate_ci_low"], 0.0)


class TestRegistrySignificanceFilter(unittest.TestCase):
    """P2.4: the memory store never promotes non-significant strategies."""

    def _temp_store(self, enabled=True, max_pvalue=0.05, min_ci_low=0.0):
        from config.loader import load_config
        from core.memory.store import MemoryStore
        cfg = load_config()
        tmp = tempfile.mkdtemp(prefix="mt5bot_sig_")
        cfg["memory"]["db_file"] = os.path.join(tmp, "memory.sqlite")
        cfg["memory"]["registry_file"] = os.path.join(tmp, "registry.json")
        sig = cfg["memory"]["search"]["significance"]
        sig["enabled"] = enabled
        sig["max_pvalue"] = max_pvalue
        sig["min_winrate_ci_low"] = min_ci_low
        return MemoryStore(cfg)

    def _spec(self, period):
        from core.strategy.strategy import StrategySpec
        return StrategySpec(
            indicators={"ema": {"period": period}},
            weights={"ema": 1.0},
            long_threshold=0.2, short_threshold=0.2,
            sl_atr_mult=2.0, tp_atr_mult=3.0,
            symbol="EURUSD", timeframe="M15",
        )

    def _record(self, store, spec, expectancy, pvalue, ci_low=0.45):
        metrics = {"win_rate": 0.55, "profit_factor": 1.3,
                   "expectancy": expectancy, "max_drawdown": -50.0,
                   "num_trades": 50, "sharpe": 0.4, "net_profit": 100.0,
                   "win_rate_ci_low": ci_low, "pnl_pvalue": pvalue}
        for seg in range(2):
            store.record_result(spec, metrics, segment="seg_%d" % seg,
                                rank_metric="expectancy")

    def test_nonsignificant_recorded_but_not_promoted(self):
        store = self._temp_store(enabled=True, max_pvalue=0.05)
        good = self._spec(10)      # significant (low p-value)
        noise = self._spec(20)     # non-significant (high p-value), better score
        self._record(store, good, expectancy=5.0, pvalue=0.01)
        self._record(store, noise, expectancy=9.0, pvalue=0.40)

        # Both are RECORDED in the memory (4 result rows).
        self.assertEqual(store.stats()["results"], 4)

        # Filtered promotion keeps only the significant one, even though the
        # noise strategy has a higher raw score.
        promoted = store.top_strategies("EURUSD", "M15",
                                        rank_metric="expectancy", min_trades=1)
        self.assertEqual(len(promoted), 1)
        self.assertEqual(promoted[0]["fingerprint"], good.fingerprint())

        # Raw fetch (filter bypassed) returns both.
        raw = store.top_strategies("EURUSD", "M15", rank_metric="expectancy",
                                   min_trades=1, apply_significance=False)
        self.assertEqual(len(raw), 2)

        # update_registry inherits the filter: only the significant spec lands.
        store.update_registry("EURUSD", "M15", rank_metric="expectancy",
                              min_trades=1)
        top = store.load_registry_top("EURUSD", "M15")
        self.assertEqual(len(top), 1)
        self.assertEqual(top[0]["fingerprint"], good.fingerprint())

    def test_filter_disabled_promotes_all(self):
        store = self._temp_store(enabled=False)
        good = self._spec(10)
        noise = self._spec(20)
        self._record(store, good, expectancy=5.0, pvalue=0.01)
        self._record(store, noise, expectancy=9.0, pvalue=0.40)
        promoted = store.top_strategies("EURUSD", "M15",
                                        rank_metric="expectancy", min_trades=1)
        self.assertEqual(len(promoted), 2)

    def test_winrate_ci_low_gate(self):
        # Turn on the optional win-rate lower-bound gate; a strong p-value but a
        # weak win-rate lower bound must still be rejected.
        store = self._temp_store(enabled=True, max_pvalue=0.05, min_ci_low=0.5)
        weak_wr = self._spec(30)
        self._record(store, weak_wr, expectancy=5.0, pvalue=0.01, ci_low=0.30)
        promoted = store.top_strategies("EURUSD", "M15",
                                        rank_metric="expectancy", min_trades=1)
        self.assertEqual(len(promoted), 0)


if __name__ == "__main__":
    unittest.main()
