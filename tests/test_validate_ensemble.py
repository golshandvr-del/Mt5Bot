"""
Tests for the engine-blend composite validation (UPGRADE_PLAN U2.5).

Covers the two U2.5 deliverables that closed diagnosis D2's "the blend composite
is never validated" gap:

  1. core/strategy/composite.py CompositeStrategy - the Strategy-compatible
     adapter that reproduces the engine blend (plain average of the top-K
     strategy signals + the GLOBAL long/short thresholds + weighted SL/TP), and
  2. scripts/validate_ensemble.py validate() - which rebuilds the registry
     top-K, wraps them in the composite, replays it through the pessimistic
     Backtester and writes the U1 receipts.

All offline / stdlib only; Windows 7 + Python 3.8 friendly.

All text is standard ASCII English only.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from tests.helpers import PROJECT_ROOT, make_synthetic_ohlcv  # noqa: F401

from config.loader import load_config
from core.strategy.strategy import Strategy, StrategySpec
from core.strategy.composite import CompositeStrategy

SYMBOL = "TESTX"
TF = "M15"


def _spec(period, sl, tp, lt=0.2, st=0.2):
    return StrategySpec(
        indicators={"ema": {"period": period}},
        weights={"ema": 1.0},
        long_threshold=lt, short_threshold=st,
        sl_atr_mult=sl, tp_atr_mult=tp,
        symbol=SYMBOL, timeframe=TF,
    )


class TestCompositeStrategy(unittest.TestCase):
    def setUp(self):
        self.ohlcv = make_synthetic_ohlcv(n=600, symbol=SYMBOL, timeframe=TF)
        self.strats = [
            Strategy(_spec(10, 2.0, 3.0)),
            Strategy(_spec(20, 2.5, 4.0)),
            Strategy(_spec(30, 3.0, 5.0)),
        ]

    def test_weighted_sl_tp_is_the_ensemble_average(self):
        comp = CompositeStrategy(self.strats, SYMBOL, TF,
                                 long_threshold=0.6, short_threshold=0.6)
        # (2.0 + 2.5 + 3.0) / 3 == 2.5 ; (3.0 + 4.0 + 5.0) / 3 == 4.0
        self.assertAlmostEqual(comp.spec.sl_atr_mult, 2.5, places=6)
        self.assertAlmostEqual(comp.spec.tp_atr_mult, 4.0, places=6)

    def test_signal_is_plain_average_of_members(self):
        comp = CompositeStrategy(self.strats, SYMBOL, TF,
                                 long_threshold=0.6, short_threshold=0.6)
        blended = comp.signal_series(self.ohlcv)
        members = [s.signal_series(self.ohlcv) for s in self.strats]
        # Spot-check several bars: composite == clamped mean of the members.
        n = len(blended)
        self.assertEqual(n, len(self.ohlcv.close))
        for i in (100, 200, 300, 400, 500):
            mean = sum(m[i] for m in members) / len(members)
            mean = max(-1.0, min(1.0, mean))
            self.assertAlmostEqual(blended[i], mean, places=6)

    def test_decision_uses_global_thresholds(self):
        # A very high threshold => almost nothing fires; a very low one => more.
        strict = CompositeStrategy(self.strats, SYMBOL, TF,
                                   long_threshold=0.99, short_threshold=0.99)
        loose = CompositeStrategy(self.strats, SYMBOL, TF,
                                  long_threshold=0.01, short_threshold=0.01)
        n_strict = sum(1 for d in strict.decision_series(self.ohlcv) if d != 0)
        n_loose = sum(1 for d in loose.decision_series(self.ohlcv) if d != 0)
        self.assertGreaterEqual(n_loose, n_strict)

    def test_empty_ensemble_is_flat_and_safe(self):
        comp = CompositeStrategy([], SYMBOL, TF,
                                 long_threshold=0.6, short_threshold=0.6)
        sig = comp.signal_series(self.ohlcv)
        self.assertEqual(len(sig), len(self.ohlcv.close))
        self.assertTrue(all(s == 0.0 for s in sig))
        # Safe default SL/TP so a backtest never divides by zero.
        self.assertGreater(comp.spec.sl_atr_mult, 0.0)
        self.assertGreater(comp.spec.tp_atr_mult, 0.0)


class TestValidateEnsembleScript(unittest.TestCase):
    """End-to-end: validate() rebuilds registry -> composite -> backtest -> CSVs."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="u25_val_")
        self.cfg = load_config()
        # Isolate everything under the temp dir: the DataFeed derives its history
        # dir from project_root/data_store/history, so we point project_root here.
        self.cfg["project_root"] = self.tmp
        self.cfg["memory"]["db_file"] = os.path.join(self.tmp, "memory.sqlite")
        self.cfg["memory"]["registry_file"] = os.path.join(self.tmp,
                                                           "registry.json")
        self.cfg["backtest"]["report_dir"] = self.tmp

        # Write a synthetic price CSV where the DataFeed will look for it.
        history_dir = os.path.join(self.tmp, "data_store", "history")
        os.makedirs(history_dir, exist_ok=True)
        ohlcv = make_synthetic_ohlcv(n=1200, symbol=SYMBOL, timeframe=TF)
        csv_path = os.path.join(history_dir, "%s_%s.csv" % (SYMBOL, TF))
        ohlcv.to_csv(csv_path)

        # Write a 3-strategy registry directly (no search needed).
        specs = [_spec(10, 2.0, 3.0), _spec(20, 2.5, 4.0), _spec(30, 3.0, 5.0)]
        registry = {
            "%s|%s" % (SYMBOL, TF): {
                "rank_metric": "expectancy",
                "updated_at": 0.0,
                "top": [{"spec": s.to_dict(),
                         "metrics": {"expectancy": 1.0}} for s in specs],
            }
        }
        with open(self.cfg["memory"]["registry_file"], "w",
                  encoding="utf-8") as handle:
            json.dump(registry, handle)

    def test_validate_produces_metrics_and_artifacts(self):
        from scripts.validate_ensemble import validate
        summary = validate(self.cfg, SYMBOL, TF, warmup=60)
        self.assertNotIn("error", summary)
        self.assertEqual(summary["n_strategies"], 3)
        self.assertIn("metrics", summary)
        self.assertIn("num_trades", summary)
        # Artifacts must actually exist on disk.
        art = summary["artifacts"]
        self.assertTrue(art["trades"] and os.path.exists(art["trades"]))
        self.assertTrue(art["equity"] and os.path.exists(art["equity"]))
        self.assertTrue(os.path.exists(summary["summary_file"]))
        # The caveat about ML/news must be present (transparency requirement).
        self.assertIn("learner", summary["caveat"].lower())

    def test_empty_registry_reports_error_not_crash(self):
        from scripts.validate_ensemble import validate
        # Blank the registry.
        with open(self.cfg["memory"]["registry_file"], "w",
                  encoding="utf-8") as handle:
            json.dump({}, handle)
        summary = validate(self.cfg, SYMBOL, TF, warmup=60)
        self.assertEqual(summary.get("error"), "empty_registry")


if __name__ == "__main__":
    unittest.main()
