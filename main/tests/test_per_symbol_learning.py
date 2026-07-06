"""
Tests for per-symbol ML models (Track A / A5, P3.3 training + P3.4 lookup).

These are offline, standard-library-only tests. They train two DISTINCT learners
on two clearly-different synthetic datasets, save them via the same
_per_symbol_model_file naming used by run_train, and then verify that the
BotContext per-symbol lookup and the DecisionEngine learner_provider each resolve
the CORRECT symbol's model. All model files are written under a temp directory
(via an ABSOLUTE model_file override in an in-memory config copy), so the real
data_store / models directory is never touched.

Covered:
  - _per_symbol_model_file (context + runners) produce identical, distinct paths;
  - training two symbols writes two distinct model files on disk;
  - BotContext.learner_for returns a distinct, ready learner per trained symbol
    and falls back to the shared learner for an untrained symbol;
  - default (learning.per_symbol=false) keeps returning the shared learner and
    the engine gets NO provider (light path unchanged);
  - the engine's learner_provider actually selects the per-symbol learner whose
    prediction is used for that symbol.

All text is standard ASCII English only.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from tests.helpers import PROJECT_ROOT, make_synthetic_ohlcv  # noqa: F401


def _train_and_save(cfg, ohlcv, model_path):
    """Build a fresh learner, fit on ohlcv, and save to model_path.

    Returns (learner, saved_ok). Uses the same FeatureBuilder + active model the
    runners use, so the saved artifact matches production training.
    """
    from core.learning.factory import build_active_model
    from core.learning.features import FeatureBuilder

    fb = FeatureBuilder(cfg)
    X, y, names = fb.build_training(ohlcv)
    learner = build_active_model(cfg)
    if hasattr(learner, "set_feature_names"):
        try:
            learner.set_feature_names(names)
        except Exception:
            pass
    learner.fit(X, y)
    ok = bool(learner.save(model_path)) if learner.is_ready() else False
    return learner, ok


class TestPerSymbolModelFileNaming(unittest.TestCase):
    def test_context_and_runners_agree(self):
        # The two helpers must produce byte-identical paths so training and the
        # lookup never disagree about where a symbol's model lives.
        from app.context import BotContext
        from app.runners import _per_symbol_model_file as runner_helper

        base = "models/ml_classifier.pkl"
        for sym in ("EURUSD", "XAUUSD", "EURUSD.m", "weird/sym"):
            ctx_path = BotContext._per_symbol_model_file(base, sym)
            run_path = runner_helper(base, sym)
            self.assertEqual(ctx_path, run_path)

    def test_distinct_symbols_distinct_files(self):
        from app.context import BotContext
        base = "models/ml_classifier.pkl"
        a = BotContext._per_symbol_model_file(base, "EURUSD")
        b = BotContext._per_symbol_model_file(base, "XAUUSD")
        self.assertNotEqual(a, b)
        self.assertTrue(a.endswith("ml_classifier_EURUSD.pkl"))
        self.assertTrue(b.endswith("ml_classifier_XAUUSD.pkl"))


class TestPerSymbolLookup(unittest.TestCase):
    def setUp(self):
        # A private temp dir for all model artifacts (real models/ untouched).
        self.tmp = tempfile.mkdtemp(prefix="mt5_persym_")
        # Two clearly-different datasets so the two models learn different things.
        self.sym_a = "AAAUSD"
        self.sym_b = "BBBUSD"
        self.ohlcv_a = make_synthetic_ohlcv(n=700, symbol=self.sym_a, seed=11)
        self.ohlcv_b = make_synthetic_ohlcv(n=700, symbol=self.sym_b, seed=99)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ctx_per_symbol(self, enabled=True):
        """Build a context whose model_file points into the temp dir (absolute)."""
        from app.context import BotContext
        ctx = BotContext()
        ctx.cfg["learning"]["per_symbol"] = bool(enabled)
        # Absolute path so resolve_path uses it as-is (real models/ untouched).
        ctx.cfg["learning"]["ml_classifier"]["model_file"] = os.path.join(
            self.tmp, "ml_classifier.pkl")
        return ctx

    def _model_file_for(self, ctx, symbol):
        from config.loader import resolve_path
        name = ctx.cfg.get_path("learning.active_model", "ml_classifier")
        base = ctx.cfg.get_path("learning.%s.model_file" % name, "")
        return resolve_path(ctx.cfg, ctx._per_symbol_model_file(base, symbol))

    def test_two_symbols_two_distinct_files_and_lookup(self):
        ctx = self._ctx_per_symbol(enabled=True)
        path_a = self._model_file_for(ctx, self.sym_a)
        path_b = self._model_file_for(ctx, self.sym_b)
        self.assertNotEqual(path_a, path_b)

        # Train + save two distinct per-symbol models.
        _, ok_a = _train_and_save(ctx.cfg, self.ohlcv_a, path_a)
        _, ok_b = _train_and_save(ctx.cfg, self.ohlcv_b, path_b)
        self.assertTrue(ok_a)
        self.assertTrue(ok_b)
        self.assertTrue(os.path.exists(path_a))
        self.assertTrue(os.path.exists(path_b))

        # The lookup must return a DISTINCT, ready learner per symbol.
        la = ctx.learner_for(self.sym_a)
        lb = ctx.learner_for(self.sym_b)
        self.assertIsNot(la, lb)
        self.assertTrue(la.is_ready())
        self.assertTrue(lb.is_ready())

        # Caching: a second call returns the SAME object per symbol.
        self.assertIs(ctx.learner_for(self.sym_a), la)
        self.assertIs(ctx.learner_for(self.sym_b), lb)

    def test_untrained_symbol_falls_back_to_shared(self):
        ctx = self._ctx_per_symbol(enabled=True)
        # Train only symbol A; symbol B has no file on disk.
        path_a = self._model_file_for(ctx, self.sym_a)
        _train_and_save(ctx.cfg, self.ohlcv_a, path_a)
        # Untrained symbol must gracefully fall back to the shared learner.
        fallback = ctx.learner_for("ZZZUSD")
        self.assertIs(fallback, ctx.learner)

    def test_default_mode_uses_shared_learner_and_no_provider(self):
        ctx = self._ctx_per_symbol(enabled=False)
        # In default (shared) mode every symbol maps to the shared learner...
        self.assertIs(ctx.learner_for(self.sym_a), ctx.learner)
        self.assertIs(ctx.learner_for(self.sym_b), ctx.learner)
        # ...and the engine is given NO provider (light path unchanged).
        self.assertIsNone(ctx.engine.learner_provider)

    def test_per_symbol_mode_gives_engine_a_provider(self):
        ctx = self._ctx_per_symbol(enabled=True)
        self.assertIsNotNone(ctx.engine.learner_provider)


class TestEngineSelectsPerSymbolLearner(unittest.TestCase):
    """The engine's learner_provider must actually route to the right model."""

    def test_provider_routes_prediction_by_symbol(self):
        from core.decision.engine import DecisionEngine
        from config.loader import load_config

        cfg = load_config()

        # Two sentinel learners that report their own name via predict_signal so
        # we can prove which one the engine consulted for each symbol.
        class _Sentinel(object):
            def __init__(self, tag, value):
                self.tag = tag
                self.value = value

            def is_ready(self):
                return True

            def predict_signal(self, row):
                return self.value

        learner_x = _Sentinel("X", 0.9)
        learner_y = _Sentinel("Y", -0.9)
        registry = {"XXXUSD": learner_x, "YYYUSD": learner_y}

        def provider(symbol):
            return registry.get(symbol)

        # A minimal feature builder stub: any non-None row is fine.
        class _FB(object):
            def build_inference_row(self, ohlcv):
                return [0.0, 0.0, 0.0]

        eng = DecisionEngine(
            cfg,
            learner=None,
            feature_builder=_FB(),
            learner_provider=provider,
        )
        ohlcv = make_synthetic_ohlcv(n=300)

        # _learning_signal must reflect the symbol-specific sentinel value.
        sig_x = eng._learning_signal(ohlcv, "XXXUSD")
        sig_y = eng._learning_signal(ohlcv, "YYYUSD")
        self.assertAlmostEqual(sig_x, 0.9, places=6)
        self.assertAlmostEqual(sig_y, -0.9, places=6)

        # An unknown symbol has no per-symbol learner and no shared fallback,
        # so the learning signal is a safe neutral 0.0.
        self.assertEqual(eng._learning_signal(ohlcv, "NOPEUSD"), 0.0)


if __name__ == "__main__":
    unittest.main()
