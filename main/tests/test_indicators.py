"""
Tests for the indicator layer (Phase 2).

Checks that indicators self-register, build from config, compute output series
aligned to the input, and produce a bounded [-1, +1] signal on synthetic data.

All text is standard ASCII English only.
"""

from __future__ import annotations

import unittest

from tests.helpers import PROJECT_ROOT, make_synthetic_ohlcv  # noqa: F401


class TestIndicators(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Importing the package triggers self-registration of all indicators.
        import core.indicators  # noqa: F401
        cls.ohlcv = make_synthetic_ohlcv(n=600)

    def test_registry_not_empty(self):
        from core.indicators.registry import list_indicators
        names = list_indicators()
        self.assertGreater(len(names), 5)
        # A few essentials must exist.
        for essential in ("ema", "rsi", "atr", "macd"):
            self.assertIn(essential, names)

    def test_signals_are_bounded(self):
        from core.indicators.registry import get_indicator_class, list_indicators
        for name in list_indicators():
            cls = get_indicator_class(name)
            try:
                ind = cls()
                sig = ind.signal(self.ohlcv)
            except Exception as exc:  # pragma: no cover - report which indicator
                self.fail("Indicator %s raised: %s" % (name, exc))
            if sig is None:
                continue
            self.assertGreaterEqual(sig, -1.0 - 1e-9,
                                    "%s signal below -1" % name)
            self.assertLessEqual(sig, 1.0 + 1e-9,
                                 "%s signal above +1" % name)

    def test_compute_series_length(self):
        from core.indicators.registry import get_indicator_class
        ema = get_indicator_class("ema")(params={"period": 21})
        res = ema.compute(self.ohlcv)
        # The primary series should align to the number of bars.
        series = res.get("ema") or next(iter(res.values()), [])
        self.assertEqual(len(series), len(self.ohlcv))

    def test_build_enabled_from_config(self):
        from config.loader import load_config
        from core.indicators.registry import build_enabled_indicators
        cfg = load_config()
        enabled = build_enabled_indicators(cfg)
        self.assertGreater(len(enabled), 0)


if __name__ == "__main__":
    unittest.main()
