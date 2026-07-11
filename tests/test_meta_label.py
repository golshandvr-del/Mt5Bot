"""
Tests for the meta-labeling win-probability veto gate (UPGRADE_PLAN U6.1).

The meta-labeler does NOT predict market direction. It predicts a much easier
question: "given the validated top strategy is about to fire here, will that
trade win?" and, at live time, becomes a VETO-ONLY quality gate that composes
with parity mode - it can BLOCK a low-probability entry the validated strategy
wanted, but can never create, flip, or resize a trade.

These tests lock the guarantees that make the gate safe to ship default-off:
  1. DISABLED gate never vetoes (byte-for-byte unchanged light path).
  2. UNTRAINED gate never vetoes (degrades gracefully, no model = no block).
  3. train() builds a usable model from enough two-class firings and the JSON
     model round-trips through save()/load() to identical predictions.
  4. train() refuses (gate stays inactive) on too-few samples or single-class
     labels - a refusing gate can never veto.
  5. should_veto() fires exactly when a trained model predicts P(win) below
     min_win_prob, and never otherwise.
  6. ENGINE integration: the gate is veto-only - it can turn an intended entry
     into a hold, but a passing/absent gate never invents a trade.
  7. The persisted feature layout is stable (guards silent feature drift).

Stdlib-only; no MT5, no network. Deterministic synthetic bars + temp files.

All text is standard ASCII English only.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from tests.helpers import PROJECT_ROOT, make_synthetic_ohlcv  # noqa: F401

from config.loader import load_config
from core.memory.store import MemoryStore
from core.decision.engine import DecisionEngine
from core.strategy.strategy import StrategySpec, Strategy
from core.strategy.meta_label import (
    MetaLabeler,
    _LogReg,
    _FEATURE_NAMES,
    _N_FEATURES,
)


SYMBOL = "XAUUSD"
TF = "M15"


def _make_spec(long_threshold=0.05, short_threshold=0.05):
    """A simple, permissive EMA-cross spec that fires often on synthetic data,
    so build_dataset() has plenty of firings to label."""
    return StrategySpec(
        indicators={"ema": {"period": 10}, "rsi": {"period": 14}},
        weights={"ema": 1.0, "rsi": 0.5},
        long_threshold=long_threshold,
        short_threshold=short_threshold,
        sl_atr_mult=2.0,
        tp_atr_mult=3.0,
        symbol=SYMBOL,
        timeframe=TF,
    )


def _make_cfg(tmpdir, enabled=True, min_win_prob=0.5, min_train_samples=20,
              model_name="meta_label.json"):
    cfg = load_config()
    cfg.setdefault("decision", {})
    cfg["decision"]["meta_label"] = {
        "enabled": enabled,
        "min_win_prob": min_win_prob,
        "min_train_samples": min_train_samples,
        "learning_rate": 0.1,
        "epochs": 120,
        "horizon": 5,
        "model_file": os.path.join(tmpdir, model_name),
    }
    cfg["project_root"] = tmpdir
    return cfg


class TestLogReg(unittest.TestCase):
    """The tiny pure-Python logistic regression underneath the gate."""

    def test_learns_a_separable_two_class_problem(self):
        # y = 1 when feature-0 is large, else 0. Trivially separable.
        X = []
        y = []
        for i in range(60):
            if i % 2 == 0:
                X.append([2.0, 0.0])
                y.append(1)
            else:
                X.append([-2.0, 0.0])
                y.append(0)
        model = _LogReg(2, lr=0.3, epochs=400)
        model.fit(X, y)
        self.assertGreater(model.predict_proba([2.0, 0.0]), 0.6)
        self.assertLess(model.predict_proba([-2.0, 0.0]), 0.4)

    def test_dict_round_trip_preserves_predictions(self):
        X = [[1.0, 0.0], [-1.0, 0.0]] * 20
        y = [1, 0] * 20
        model = _LogReg(2, lr=0.2, epochs=200)
        model.fit(X, y)
        before = model.predict_proba([0.7, 0.1])
        clone = _LogReg.from_dict(model.to_dict())
        after = clone.predict_proba([0.7, 0.1])
        self.assertAlmostEqual(before, after, places=12)

    def test_empty_fit_is_a_noop(self):
        model = _LogReg(2)
        model.fit([], [])  # must not raise
        # Untrained weights -> sigmoid(0) == 0.5
        self.assertAlmostEqual(model.predict_proba([0.0, 0.0]), 0.5, places=9)


class TestMetaLabelerGate(unittest.TestCase):

    def test_disabled_gate_never_vetoes(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _make_cfg(tmp, enabled=False)
            ohlcv = make_synthetic_ohlcv(n=400, symbol=SYMBOL, timeframe=TF)
            spec = _make_spec()
            meta = MetaLabeler(cfg)
            # Even after training, a disabled gate must never veto.
            meta.train(spec, ohlcv, horizon=5)
            veto, p = meta.should_veto(spec, ohlcv)
            self.assertFalse(veto)
            self.assertIsNone(p)

    def test_untrained_gate_never_vetoes(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _make_cfg(tmp, enabled=True)
            ohlcv = make_synthetic_ohlcv(n=400, symbol=SYMBOL, timeframe=TF)
            spec = _make_spec()
            meta = MetaLabeler(cfg)  # nothing trained/loaded
            veto, p = meta.should_veto(spec, ohlcv)
            self.assertFalse(veto)
            self.assertIsNone(p)
            # win_probability with no model returns None too.
            self.assertIsNone(meta.win_probability(spec, ohlcv))

    def test_build_dataset_labels_are_two_class_and_aligned(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _make_cfg(tmp, enabled=True)
            ohlcv = make_synthetic_ohlcv(n=500, symbol=SYMBOL, timeframe=TF)
            spec = _make_spec()
            meta = MetaLabeler(cfg)
            X, y = meta.build_dataset(spec, ohlcv, horizon=5, warmup=60)
            self.assertEqual(len(X), len(y))
            self.assertGreater(len(X), 0)
            # Every row has exactly the declared feature count.
            for row in X:
                self.assertEqual(len(row), _N_FEATURES)
            # Labels are strictly 0/1.
            self.assertTrue(all(v in (0, 1) for v in y))

    def test_train_persists_and_reloads_identically(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _make_cfg(tmp, enabled=True, min_train_samples=20)
            ohlcv = make_synthetic_ohlcv(n=600, symbol=SYMBOL, timeframe=TF)
            spec = _make_spec()
            meta = MetaLabeler(cfg)
            ok = meta.train(spec, ohlcv, horizon=5)
            self.assertTrue(ok)
            self.assertTrue(os.path.exists(cfg.get_path("decision.meta_label.model_file")))
            p_before = meta.win_probability(spec, ohlcv)
            self.assertIsNotNone(p_before)

            # Fresh instance loads the persisted model and predicts identically.
            meta2 = MetaLabeler(cfg)
            self.assertTrue(meta2.load())
            p_after = meta2.win_probability(spec, ohlcv)
            self.assertIsNotNone(p_after)
            self.assertAlmostEqual(p_before, p_after, places=10)

    def test_too_few_samples_keeps_gate_inactive(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Absurdly high sample floor -> training must refuse.
            cfg = _make_cfg(tmp, enabled=True, min_train_samples=10 ** 9)
            ohlcv = make_synthetic_ohlcv(n=400, symbol=SYMBOL, timeframe=TF)
            spec = _make_spec()
            meta = MetaLabeler(cfg)
            ok = meta.train(spec, ohlcv, horizon=5)
            self.assertFalse(ok)
            # No model persisted -> gate can never veto.
            veto, p = meta.should_veto(spec, ohlcv)
            self.assertFalse(veto)
            self.assertIsNone(p)

    def test_veto_fires_only_below_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _make_cfg(tmp, enabled=True, min_train_samples=20)
            ohlcv = make_synthetic_ohlcv(n=600, symbol=SYMBOL, timeframe=TF)
            spec = _make_spec()
            meta = MetaLabeler(cfg)
            self.assertTrue(meta.train(spec, ohlcv, horizon=5))
            p = meta.win_probability(spec, ohlcv)
            self.assertIsNotNone(p)

            # A threshold just BELOW the model's P(win) must NOT veto.
            meta.min_win_prob = max(0.0, p - 0.05)
            veto_low, _ = meta.should_veto(spec, ohlcv)
            self.assertFalse(veto_low)

            # A threshold ABOVE 1.0 forces a veto (P(win) can never reach it).
            meta.min_win_prob = 1.01
            veto_high, p_hi = meta.should_veto(spec, ohlcv)
            self.assertTrue(veto_high)
            self.assertAlmostEqual(p_hi, p, places=10)


class _StubLabeler(object):
    """A minimal meta_labeler with the interface the engine uses. `veto` and
    `p_win` are fixed so tests can prove the engine honours the gate without
    depending on a trained model."""

    def __init__(self, veto, p_win):
        self._veto = bool(veto)
        self._p = p_win

    def should_veto(self, spec, ohlcv, sig=None):
        return self._veto, self._p


def _write_registry(path, spec):
    registry = {
        "%s|%s" % (SYMBOL, TF): {
            "rank_metric": "expectancy",
            "updated_at": 0.0,
            "top": [{"spec": spec.to_dict(), "metrics": {"expectancy": 1.0}}],
        }
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(registry, handle)


class TestEngineVetoIntegration(unittest.TestCase):
    """The gate must be VETO-ONLY inside the DecisionEngine parity path: it can
    turn an intended entry into a hold, but never create/flip/resize a trade."""

    def _engine_cfg(self, tmp):
        cfg = load_config()
        cfg["memory"]["db_file"] = os.path.join(tmp, "memory.sqlite")
        cfg["memory"]["registry_file"] = os.path.join(tmp, "registry.json")
        cfg["decision"]["mode"] = "parity"
        # A permissive spec so parity WANTS to enter on the synthetic data.
        spec = StrategySpec(
            indicators={"ema": {"period": 10}},
            weights={"ema": 1.0},
            long_threshold=0.02,
            short_threshold=0.02,
            sl_atr_mult=2.0,
            tp_atr_mult=3.0,
            symbol=SYMBOL,
            timeframe=TF,
        )
        _write_registry(cfg["memory"]["registry_file"], spec)
        return cfg, spec

    def _base_action(self, tmp):
        """Action with NO meta-labeler attached (the intended parity action)."""
        cfg, _ = self._engine_cfg(tmp)
        store = MemoryStore(cfg)
        engine = DecisionEngine(cfg, memory=store)
        ohlcv = make_synthetic_ohlcv(n=600, symbol=SYMBOL, timeframe=TF)
        return engine.decide(ohlcv, SYMBOL, TF).action, ohlcv

    def test_veto_forces_hold_only_when_there_was_an_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_action, ohlcv = self._base_action(tmp)
            # This synthetic spec should produce a non-flat intended action;
            # if not, the veto test below is vacuous, so assert it up front.
            self.assertNotEqual(base_action, 0,
                                "fixture should intend an entry to veto")

            cfg, _ = self._engine_cfg(tmp)
            store = MemoryStore(cfg)
            engine = DecisionEngine(
                cfg, memory=store,
                meta_labeler=_StubLabeler(veto=True, p_win=0.10))
            dec = engine.decide(ohlcv, SYMBOL, TF)
            # Veto turns the intended entry into a hold.
            self.assertEqual(dec.action, 0)
            self.assertIn("veto_meta_label=1", " ".join(dec.reasons))
            self.assertAlmostEqual(dec.components.get("meta_win_prob"), 0.10,
                                   places=9)

    def test_passing_gate_keeps_the_intended_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_action, ohlcv = self._base_action(tmp)
            cfg, _ = self._engine_cfg(tmp)
            store = MemoryStore(cfg)
            engine = DecisionEngine(
                cfg, memory=store,
                meta_labeler=_StubLabeler(veto=False, p_win=0.90))
            dec = engine.decide(ohlcv, SYMBOL, TF)
            # A passing gate never changes the action - and never invents one.
            self.assertEqual(dec.action, base_action)
            self.assertNotIn("veto_meta_label=1", " ".join(dec.reasons))
            self.assertAlmostEqual(dec.components.get("meta_win_prob"), 0.90,
                                   places=9)

    def test_gate_never_creates_a_trade_from_a_hold(self):
        """When parity intends NO entry, even a (nonsensically) non-vetoing
        gate must not turn a hold into a trade - the gate is veto-only."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config()
            cfg["memory"]["db_file"] = os.path.join(tmp, "memory.sqlite")
            cfg["memory"]["registry_file"] = os.path.join(tmp, "registry.json")
            cfg["decision"]["mode"] = "parity"
            # Impossible thresholds => parity can never fire => always a hold.
            spec = StrategySpec(
                indicators={"ema": {"period": 10}},
                weights={"ema": 1.0},
                long_threshold=1.0,
                short_threshold=1.0,
                symbol=SYMBOL,
                timeframe=TF,
            )
            _write_registry(cfg["memory"]["registry_file"], spec)
            store = MemoryStore(cfg)
            engine = DecisionEngine(
                cfg, memory=store,
                meta_labeler=_StubLabeler(veto=False, p_win=0.99))
            ohlcv = make_synthetic_ohlcv(n=600, symbol=SYMBOL, timeframe=TF)
            dec = engine.decide(ohlcv, SYMBOL, TF)
            self.assertEqual(dec.action, 0)


class TestFeatureLayoutStability(unittest.TestCase):
    """The persisted feature order is a contract - drifting it silently would
    corrupt every previously-saved model."""

    def test_feature_names_and_count_are_frozen(self):
        self.assertEqual(_N_FEATURES, len(_FEATURE_NAMES))
        self.assertEqual(
            _FEATURE_NAMES,
            ["signal_mag", "atr_pct", "adx",
             "hour_sin", "hour_cos", "dow_sin", "dow_cos"],
        )

    def test_saved_payload_records_feature_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _make_cfg(tmp, enabled=True, min_train_samples=20)
            ohlcv = make_synthetic_ohlcv(n=600, symbol=SYMBOL, timeframe=TF)
            spec = _make_spec()
            meta = MetaLabeler(cfg)
            self.assertTrue(meta.train(spec, ohlcv, horizon=5))
            with open(cfg.get_path("decision.meta_label.model_file")) as fh:
                payload = json.load(fh)
            self.assertEqual(payload.get("feature_names"), _FEATURE_NAMES)
            self.assertIn(spec.fingerprint(), payload.get("models", {}))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
