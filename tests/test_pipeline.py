"""
End-to-end pipeline tests.

Exercises the full offline flow through the runners (train/search/backtest/paper)
using the project's own sample data, plus a direct DecisionEngine check on
synthetic bars. These are the "does the whole thing still work" smoke tests.

They rely on the CSV history that ships in data_store/history/ (created by
examples/generate_sample_data.py). If that history is missing, the sample-data
generator is invoked first so the tests are self-contained.

All text is standard ASCII English only.
"""

from __future__ import annotations

import os
import unittest

from tests.helpers import PROJECT_ROOT, make_synthetic_ohlcv  # noqa: F401


def _ensure_sample_data():
    """Make sure there is at least one history CSV to run the pipeline on."""
    hist_dir = os.path.join(PROJECT_ROOT, "data_store", "history")
    has_csv = os.path.isdir(hist_dir) and any(
        f.endswith(".csv") for f in os.listdir(hist_dir)
    ) if os.path.isdir(hist_dir) else False
    if has_csv:
        return
    # Generate synthetic data via the example script's function if present.
    try:
        import importlib.util
        gen = os.path.join(PROJECT_ROOT, "examples", "generate_sample_data.py")
        spec = importlib.util.spec_from_file_location("gen_sample", gen)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore
        if hasattr(mod, "main"):
            mod.main([])
    except Exception:
        pass


class TestPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import core.indicators  # noqa: F401
        _ensure_sample_data()

    def _ctx(self):
        from app.context import BotContext
        return BotContext()

    def test_decision_engine_on_synthetic(self):
        from core.decision.engine import DecisionEngine
        from core.learning.features import FeatureBuilder
        from config.loader import load_config
        cfg = load_config()
        ohlcv = make_synthetic_ohlcv(n=500)
        engine = DecisionEngine(cfg, feature_builder=FeatureBuilder(cfg))
        decision = engine.decide(ohlcv, "TESTX", "M15")
        self.assertIn(decision.action, (-1, 0, 1))
        self.assertGreaterEqual(decision.score, -1.0 - 1e-9)
        self.assertLessEqual(decision.score, 1.0 + 1e-9)
        # to_dict must be JSON-serializable friendly.
        d = decision.to_dict()
        self.assertIn("action", d)

    def test_run_paper_pass(self):
        from app import runners
        result = runners.run_once(self._ctx())
        self.assertIn("symbols", result)
        # Every symbol result must be a dict (decision or skip reason).
        for sym, payload in result["symbols"].items():
            self.assertIsInstance(payload, dict)

    def test_run_backtest_pass(self):
        from app import runners
        result = runners.run_backtest(self._ctx())
        self.assertIn("symbols", result)

    def test_run_train_pass(self):
        from app import runners
        result = runners.run_train(self._ctx())
        # 'saved' key is present whether or not the learner ended up ready.
        self.assertIn("saved", result)


if __name__ == "__main__":
    unittest.main()
