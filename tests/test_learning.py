"""
Tests for the learning core (Phase 1).

Verifies the FeatureBuilder produces usable (X, y) data and that the active
learner (or its neutral/pure-Python fallback) can fit and predict within the
documented bounds. No heavy backends are required; the ML classifier falls back
to a pure-Python model when LightGBM/scikit-learn are absent.

All text is standard ASCII English only.
"""

from __future__ import annotations

import unittest

from tests.helpers import PROJECT_ROOT, make_synthetic_ohlcv  # noqa: F401


class TestLearning(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import core.indicators  # noqa: F401 (register indicators for features)
        from config.loader import load_config
        cls.cfg = load_config()
        cls.ohlcv = make_synthetic_ohlcv(n=700)

    def _feature_builder(self):
        from core.learning.features import FeatureBuilder
        return FeatureBuilder(self.cfg)

    def test_feature_builder_training_shapes(self):
        fb = self._feature_builder()
        X, y, names = fb.build_training(self.ohlcv)
        self.assertGreater(len(X), 50)
        self.assertEqual(len(X), len(y))
        # Every row has the same feature count.
        widths = {len(row) for row in X}
        self.assertEqual(len(widths), 1)

    def test_inference_row_matches_width(self):
        fb = self._feature_builder()
        X, y, names = fb.build_training(self.ohlcv)
        row = fb.build_inference_row(self.ohlcv)
        self.assertEqual(len(row), len(X[0]))

    def test_active_model_fit_predict(self):
        from core.learning.factory import build_active_model
        fb = self._feature_builder()
        X, y, names = fb.build_training(self.ohlcv)
        model = build_active_model(self.cfg)
        # Fit should never raise even if the backend degrades to pure-Python.
        model.fit(X, y)
        proba = model.predict_proba_up(X[-1])
        self.assertGreaterEqual(proba, 0.0)
        self.assertLessEqual(proba, 1.0)
        signal = model.predict_signal(X[-1])
        self.assertGreaterEqual(signal, -1.0 - 1e-9)
        self.assertLessEqual(signal, 1.0 + 1e-9)

    def test_neutral_model_when_disabled(self):
        # Build a config copy with learning disabled to exercise NeutralModel.
        from core.learning.factory import build_model
        model = build_model(self.cfg, "none")
        proba = model.predict_proba_up([0.0, 0.0, 0.0])
        self.assertAlmostEqual(proba, 0.5, places=6)


if __name__ == "__main__":
    unittest.main()
