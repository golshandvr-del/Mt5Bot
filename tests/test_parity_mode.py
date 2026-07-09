"""
Tests for parity decision mode (UPGRADE_PLAN U2.4).

Parity mode makes the live/paper decision path trade the VALIDATED top-1
registry strategy EXACTLY: its own blended signal, its own long/short
thresholds, and its own SL/TP ATR multiples. The learner / news / timing
layers may only VETO (block) an entry - never create one, flip its direction,
or resize it. This directly fixes diagnosis D2 (the old blend path traded an
unvalidated composite against a global threshold).

Stdlib-only; no MT5, no network. A fake top strategy is injected into a temp
registry file so the tests are fast and deterministic.

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
from core.strategy.strategy import StrategySpec


SYMBOL = "XAUUSD"
TF = "M15"


def _write_registry(path, spec):
    """Write a one-entry registry JSON with `spec` as the top strategy."""
    registry = {
        "%s|%s" % (SYMBOL, TF): {
            "rank_metric": "expectancy",
            "updated_at": 0.0,
            "top": [{"spec": spec.to_dict(), "metrics": {"expectancy": 1.0}}],
        }
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(registry, handle)


def _make_cfg(tmp, mode="parity"):
    cfg = load_config()
    cfg["memory"]["db_file"] = os.path.join(tmp, "memory.sqlite")
    cfg["memory"]["registry_file"] = os.path.join(tmp, "registry.json")
    cfg["decision"]["mode"] = mode
    return cfg


class TestParityMode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="parity_test_")
        # A concrete spec with DISTINCTIVE thresholds and SL/TP so we can prove
        # the engine uses the spec's own values, not the global config defaults.
        self.spec = StrategySpec(
            indicators={"ema": {"period": 20}},
            weights={"ema": 1.0},
            long_threshold=0.10,
            short_threshold=0.10,
            sl_atr_mult=2.3,
            tp_atr_mult=4.7,
            symbol=SYMBOL,
            timeframe=TF,
        )

    def _engine(self, mode="parity", **kwargs):
        cfg = _make_cfg(self.tmp, mode=mode)
        for k, v in kwargs.items():
            cfg["decision"][k] = v
        _write_registry(cfg["memory"]["registry_file"], self.spec)
        store = MemoryStore(cfg)
        return DecisionEngine(cfg, memory=store), cfg

    # ------------------------------------------------------------------ #
    def test_mode_defaults_to_parity(self):
        cfg = load_config()
        # The shipped config must default to parity so validated == traded.
        self.assertEqual(str(cfg["decision"].get("mode", "")).lower(), "parity")

    def test_parity_uses_spec_thresholds_and_exits(self):
        """The decision must carry the SPEC's SL/TP, not the global defaults."""
        engine, cfg = self._engine()
        ohlcv = make_synthetic_ohlcv(n=600, symbol=SYMBOL, timeframe=TF)
        dec = engine.decide(ohlcv, SYMBOL, TF)
        # Exits come straight from the validated spec.
        self.assertAlmostEqual(dec.sl_atr_mult, 2.3, places=6)
        self.assertAlmostEqual(dec.tp_atr_mult, 4.7, places=6)
        # The thresholds recorded for the explainer are the spec's own.
        self.assertAlmostEqual(dec.components["_threshold_long"], 0.10, places=6)
        self.assertAlmostEqual(dec.components["_threshold_short"], 0.10, places=6)
        # The parity strategy fingerprint is logged for transparency.
        joined = " ".join(dec.reasons)
        self.assertIn("parity_strategy=", joined)
        self.assertIn(self.spec.fingerprint(), joined)

    def test_action_matches_strategy_signal(self):
        """Parity action = strategy signal vs its OWN thresholds."""
        engine, cfg = self._engine()
        ohlcv = make_synthetic_ohlcv(n=600, symbol=SYMBOL, timeframe=TF)
        sig = engine._ensemble_for(SYMBOL, TF)[0].blended_signal(ohlcv)
        dec = engine.decide(ohlcv, SYMBOL, TF)
        if sig >= 0.10:
            self.assertEqual(dec.action, 1)
        elif sig <= -0.10:
            self.assertEqual(dec.action, -1)
        else:
            self.assertEqual(dec.action, 0)
        self.assertAlmostEqual(dec.score, sig, places=6)

    def test_no_registry_strategy_is_flat(self):
        """With no validated strategy, parity mode must stay flat (never guess)."""
        cfg = _make_cfg(self.tmp, mode="parity")
        # Empty registry file.
        with open(cfg["memory"]["registry_file"], "w", encoding="utf-8") as h:
            json.dump({}, h)
        store = MemoryStore(cfg)
        engine = DecisionEngine(cfg, memory=store)
        ohlcv = make_synthetic_ohlcv(n=400, symbol=SYMBOL, timeframe=TF)
        dec = engine.decide(ohlcv, SYMBOL, TF)
        self.assertEqual(dec.action, 0)
        self.assertIn("parity=no_registry_strategy", " ".join(dec.reasons))

    def test_news_blackout_vetoes_entry(self):
        """A news blackout must be able to BLOCK a parity entry."""
        engine, cfg = self._engine()
        ohlcv = make_synthetic_ohlcv(n=600, symbol=SYMBOL, timeframe=TF)

        # Force a long-signalling spec so there IS an entry to veto.
        class _Blackout(object):
            def in_blackout(self, symbol):
                return True

            def get_signal(self, symbol):
                return 0.0
        engine.news = _Blackout()
        engine.parity_veto_news = True
        dec = engine.decide(ohlcv, SYMBOL, TF)
        self.assertEqual(dec.action, 0)
        self.assertEqual(dec.components["_blackout"], 1.0)
        self.assertIn("veto_news_blackout=1", " ".join(dec.reasons))

    def test_veto_never_creates_a_trade(self):
        """
        When the strategy itself is flat (signal inside its thresholds), no veto
        gate may turn that into a trade. Vetoes can only block.
        """
        # Use an unreachable threshold so the strategy is always flat.
        self.spec.long_threshold = 2.0
        self.spec.short_threshold = 2.0
        engine, cfg = self._engine()
        ohlcv = make_synthetic_ohlcv(n=600, symbol=SYMBOL, timeframe=TF)
        dec = engine.decide(ohlcv, SYMBOL, TF)
        self.assertEqual(dec.action, 0)

    def test_blend_mode_still_works(self):
        """mode='blend' must keep the legacy composite path (no parity reasons)."""
        engine, cfg = self._engine(mode="blend")
        ohlcv = make_synthetic_ohlcv(n=600, symbol=SYMBOL, timeframe=TF)
        dec = engine.decide(ohlcv, SYMBOL, TF)
        joined = " ".join(dec.reasons)
        self.assertNotIn("parity_strategy=", joined)
        # Blend mode reports the ensemble/indicator source label instead.
        self.assertIn("indicators(", joined)


if __name__ == "__main__":
    unittest.main()
