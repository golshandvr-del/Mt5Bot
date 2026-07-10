"""
Offline tests for the parameter-neighborhood robustness gate
(UPGRADE_PLAN.md U4.4).

For each finalist the search evaluates up to n_neighbors specs that each differ
by ONE indicator parameter nudged a single step in that indicator's param_space.
`neighborhood_score` is the MEDIAN walk-forward score of those neighbors and the
registry ranks by min(own_score, neighborhood_score). A strategy that only wins
at one exact parameter value (an overfit knife-edge) therefore gets demoted
because its neighbors score poorly.

These tests exercise the gate LOGIC in isolation:

  1. Gate DISABLED / n_neighbors=0 -> neighborhood_score is None (legacy path).
  2. `_neighbor_specs` builds only one-step-different, in-param-space, deduped
     neighbors (never the parent itself, never an out-of-range value).
  3. `_neighborhood_score` returns the MEDIAN of the neighbor scores.
  4. A planted overfit fixture (parent scores high, all neighbors score low)
     yields a neighborhood median far below the parent -> min() would demote it.

WalkForward is stubbed so the tests are deterministic, fast, and need no MT5.

All text is standard ASCII English only.
"""

from __future__ import annotations

import unittest

from tests.helpers import make_synthetic_ohlcv  # noqa: F401 (path fix)

from config.loader import load_config
from core.strategy.search import StrategySearch
from core.strategy.strategy import StrategySpec


class _ScriptedWF(object):
    """Stub WalkForward that returns avg_score by neighbor fingerprint.

    `scores_by_fp` maps a spec fingerprint -> avg_score; unknown specs get
    `default`. Records every evaluated fingerprint and its persist flag.
    """

    def __init__(self, scores_by_fp=None, default=0.0):
        self._scores = dict(scores_by_fp or {})
        self._default = default
        self.calls = []  # (fingerprint, persist)
        self.holdout_bars = 0

    def evaluate(self, spec, ohlcv, point=None, persist=True, warmup=60):
        fp = spec.fingerprint()
        self.calls.append((fp, persist))
        return {"avg_score": self._scores.get(fp, self._default)}


def _spec(ema_period=21, rsi_period=14):
    return StrategySpec(
        indicators={"ema": {"period": ema_period}, "rsi": {"period": rsi_period}},
        weights={"ema": 1.0, "rsi": 1.0},
        long_threshold=0.2, short_threshold=0.2,
        sl_atr_mult=2.0, tp_atr_mult=3.0,
        symbol="TESTX", timeframe="M15",
    )


def _make_search(enabled=True, n_neighbors=8, wf=None):
    cfg = load_config()
    search = StrategySearch(cfg, memory=None)
    search.neighborhood_enabled = bool(enabled)
    search.neighborhood_n = int(n_neighbors)
    if wf is not None:
        search.wf = wf
    return search


class TestNeighborhoodGate(unittest.TestCase):
    def test_disabled_returns_none(self):
        s = _make_search(enabled=False, wf=_ScriptedWF())
        self.assertIsNone(
            s._neighborhood_score(_spec(), make_synthetic_ohlcv()))
        self.assertEqual(len(s.wf.calls), 0)

    def test_zero_neighbors_returns_none(self):
        s = _make_search(enabled=True, n_neighbors=0, wf=_ScriptedWF())
        self.assertIsNone(
            s._neighborhood_score(_spec(), make_synthetic_ohlcv()))
        self.assertEqual(len(s.wf.calls), 0)

    def test_neighbors_are_one_step_and_in_space(self):
        s = _make_search(enabled=True, n_neighbors=8, wf=_ScriptedWF())
        parent = _spec(ema_period=21, rsi_period=14)
        neighbors = s._neighbor_specs(parent)
        self.assertTrue(neighbors)
        parent_fp = parent.fingerprint()
        ema_space = [9, 12, 21, 34, 55]
        rsi_space = [7, 14, 21]
        seen = set()
        for child in neighbors:
            fp = child.fingerprint()
            # Never the parent, and every neighbor unique (deduped).
            self.assertNotEqual(fp, parent_fp)
            self.assertNotIn(fp, seen)
            seen.add(fp)
            ep = child.indicators["ema"]["period"]
            rp = child.indicators["rsi"]["period"]
            self.assertIn(ep, ema_space)
            self.assertIn(rp, rsi_space)
            # Exactly ONE parameter differs from the parent.
            diffs = int(ep != 21) + int(rp != 14)
            self.assertEqual(diffs, 1)

    def test_neighborhood_score_is_median(self):
        parent = _spec(ema_period=21, rsi_period=14)
        s = _make_search(enabled=True, n_neighbors=8, wf=_ScriptedWF())
        neighbors = s._neighbor_specs(parent)
        # Script deterministic ascending scores so the median is predictable.
        scores = {}
        vals = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8][:len(neighbors)]
        for child, v in zip(neighbors, vals):
            scores[child.fingerprint()] = v
        s.wf = _ScriptedWF(scores_by_fp=scores)
        med = s._neighborhood_score(parent, make_synthetic_ohlcv())
        vals_sorted = sorted(vals)
        n = len(vals_sorted)
        expected = (vals_sorted[n // 2] if n % 2 == 1
                    else (vals_sorted[n // 2 - 1] + vals_sorted[n // 2]) / 2.0)
        self.assertAlmostEqual(med, expected, places=9)
        # Neighbor evaluations must never persist to memory.
        self.assertTrue(all(p is False for (_, p) in s.wf.calls))

    def test_overfit_fixture_is_demoted(self):
        """Parent scores high but all neighbors score ~0 -> median << parent,
        so min(own, neighborhood) would demote the overfit spec."""
        parent = _spec(ema_period=21, rsi_period=14)
        s = _make_search(enabled=True, n_neighbors=8, wf=_ScriptedWF(default=0.01))
        own_score = 0.9
        med = s._neighborhood_score(parent, make_synthetic_ohlcv())
        self.assertIsNotNone(med)
        self.assertLess(med, own_score)
        # The registry ranks by the min of the two, i.e. the neighborhood value.
        self.assertAlmostEqual(min(own_score, med), med, places=9)


if __name__ == "__main__":
    unittest.main()
