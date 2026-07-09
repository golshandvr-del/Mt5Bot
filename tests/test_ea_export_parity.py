"""
Offline tests for the EA-export parity HARD GUARD (UPGRADE_PLAN.md U2.1).

These lock in the "you cannot silently export a crippled strategy" guarantee:

  1. STRICT mode (the DEFAULT) refuses to export a spec that uses ANY indicator
     the EA cannot run, and writes NO .params file.
  2. A clean spec (only EA-supported indicators) exports fine in strict mode.
  3. --allow-partial drops the unsupported indicators, RESCALES the surviving
     weights so total weight is conserved, and stamps a prominent WARNING block
     into the .params header.
  4. A spec whose indicators are ALL unsupported fails even with --allow-partial
     (nothing exportable).

Standard library + a temp dir only; no MT5, no network.
All text is standard ASCII English only.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from tests.helpers import make_synthetic_ohlcv  # noqa: F401 (path fix)

import scripts.export_strategy_for_ea as exp


def _registry(spec):
    return {"TESTX|M15": {"top": [{"spec": spec}]}}


class TestEaExportParity(unittest.TestCase):
    def _out_path(self, out_dir):
        return os.path.join(out_dir, "TESTX_M15.params")

    def test_strict_refuses_unsupported(self):
        # NOTE: ichimoku is deliberately an indicator the EA still does NOT
        # implement (unlike supertrend/bbands/stoch, which U2.3 added). Using a
        # genuinely unsupported indicator keeps this guard test honest as the
        # EA's native set grows.
        spec = {
            "indicators": {"ema": {"period": 20}, "ichimoku": {"tenkan": 9}},
            "weights": {"ema": 1.0, "ichimoku": 3.0},
        }
        with tempfile.TemporaryDirectory() as out_dir:
            ok = exp._export_one({}, _registry(spec), "TESTX", "M15", out_dir,
                                 strict=True)
            self.assertFalse(ok, "strict export must fail on unsupported indic.")
            self.assertFalse(os.path.exists(self._out_path(out_dir)),
                             "no .params must be written when strict fails")

    def test_strict_allows_clean_spec(self):
        spec = {
            "indicators": {"ema": {"period": 20}, "rsi": {"period": 14}},
            "weights": {"ema": 1.0, "rsi": 2.0},
        }
        with tempfile.TemporaryDirectory() as out_dir:
            ok = exp._export_one({}, _registry(spec), "TESTX", "M15", out_dir,
                                 strict=True)
            self.assertTrue(ok)
            path = self._out_path(out_dir)
            self.assertTrue(os.path.exists(path))
            text = open(path, "r", encoding="ascii").read()
            self.assertNotIn("WARNING", text)
            self.assertIn("ind.ema.enabled=1", text)
            self.assertIn("ind.rsi.enabled=1", text)

    def test_allow_partial_rescales_and_warns(self):
        spec = {
            "indicators": {"ema": {"period": 20}, "ichimoku": {"tenkan": 9}},
            "weights": {"ema": 1.0, "ichimoku": 3.0},
        }
        with tempfile.TemporaryDirectory() as out_dir:
            ok = exp._export_one({}, _registry(spec), "TESTX", "M15", out_dir,
                                 strict=False)
            self.assertTrue(ok)
            path = self._out_path(out_dir)
            text = open(path, "r", encoding="ascii").read()
            # Prominent warning present, and it names the dropped indicator.
            self.assertIn("WARNING", text)
            self.assertIn("ichimoku", text)
            # ichimoku must NOT be emitted as a runnable indicator.
            self.assertNotIn("ind.ichimoku.enabled", text)
            # ema weight rescaled to conserve total weight: 1.0 -> 4.0.
            self.assertIn("ind.ema.weight=4.0", text)

    def test_all_unsupported_fails_even_partial(self):
        spec = {
            "indicators": {"ichimoku": {"tenkan": 9}, "vwap": {"period": 20}},
            "weights": {"ichimoku": 1.0, "vwap": 1.0},
        }
        with tempfile.TemporaryDirectory() as out_dir:
            ok = exp._export_one({}, _registry(spec), "TESTX", "M15", out_dir,
                                 strict=False)
            self.assertFalse(ok, "cannot export when NOTHING is EA-supported")
            self.assertFalse(os.path.exists(self._out_path(out_dir)))

    def test_flatten_conserves_total_weight(self):
        spec = {
            "indicators": {"ema": {"period": 20}, "rsi": {"period": 14},
                           "ichimoku": {"tenkan": 9}},
            "weights": {"ema": 1.0, "rsi": 1.0, "ichimoku": 2.0},
        }
        lines, skipped, rescaled = exp._flatten_spec(spec)
        self.assertEqual(skipped, ["ichimoku"])
        self.assertTrue(rescaled)
        total = 0.0
        for ln in lines:
            if ".weight=" in ln:
                total += float(ln.split("=", 1)[1])
        # Original total = 1+1+2 = 4; surviving supported weights must sum to 4.
        self.assertAlmostEqual(total, 4.0, places=9)


if __name__ == "__main__":
    unittest.main()
