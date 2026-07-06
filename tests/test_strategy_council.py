"""
Tests for the live strategy council (Phase 5, Track B / B1; sub-step P5.4).

These are offline, standard-library-only tests. They exercise three layers:

  1. The bandit weight math (StrategyCouncil.weight): a strategy that keeps
     LOSING sees its weight decay toward the `min_weight` floor; a consistent
     WINNER is boosted toward `max_weight`; a coin-flip strategy stays near the
     neutral 1.0 anchor; and an UNKNOWN or still-WARMING-UP strategy (fewer than
     `min_trades` live trades) uses the neutral default weight so the council
     never penalizes a strategy that simply lacks live data.

  2. The record -> save -> load round-trip against a TEMP SQLite DB (via
     MemoryStore.save_council / load_council) so the real
     data_store/memory.sqlite is never touched. This confirms the live
     credibility PERSISTS across a simulated restart (a fresh council + fresh
     store on the same DB restore the exact same weights).

  3. The engine consumption (DecisionEngine._indicator_signal): with the council
     ON, the memory ensemble blend tilts toward the strategy with the better
     recent record; with the council OFF (default) the blend is the previous
     plain equal-weight average, byte-for-byte.

All text is standard ASCII English only.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from tests.helpers import PROJECT_ROOT  # noqa: F401


def _council(enabled=True, min_trades=1, window=30):
    """Build a StrategyCouncil off a real config with test-friendly knobs."""
    from config.loader import load_config
    from core.strategy.council import StrategyCouncil

    cfg = load_config()
    cfg["decision"]["council"]["enabled"] = enabled
    cfg["decision"]["council"]["min_trades"] = min_trades
    cfg["decision"]["council"]["window"] = window
    return StrategyCouncil(cfg), cfg


class TestCouncilWeightMath(unittest.TestCase):
    """The core bandit weight behavior (no DB, no engine)."""

    def test_loser_decays_toward_floor(self):
        floor = 0.25
        # A pure loser is damped below neutral, and as MORE losing samples
        # accumulate its weight decays MONOTONICALLY toward the floor (the UCB
        # anti-burial bonus, which softens damping for young arms, shrinks as the
        # sample count grows). We use a large window so samples actually pile up.
        from config.loader import load_config
        from core.strategy.council import StrategyCouncil

        cfg = load_config()
        cfg["decision"]["council"]["enabled"] = True
        cfg["decision"]["council"]["min_trades"] = 5
        cfg["decision"]["council"]["window"] = 2000
        cfg["decision"]["council"]["min_weight"] = floor

        weights = []
        for n in (10, 100, 1000):
            council = StrategyCouncil(cfg)
            for _ in range(n):
                council.record_outcome("LOSER", -5.0)
            weights.append(council.weight("LOSER"))

        # Every sampled weight is a genuine penalty (below neutral, above floor).
        for w in weights:
            self.assertLess(w, 1.0)
            self.assertGreaterEqual(w, floor)
        # Monotonic decay toward the floor as evidence of losing mounts.
        self.assertGreater(weights[0], weights[1])
        self.assertGreater(weights[1], weights[2])
        # With heavy evidence the weight is close to the floor.
        self.assertLess(weights[2], floor + 0.15)

    def test_winner_boosts_toward_cap(self):
        council, cfg = _council(min_trades=5)
        cap = float(cfg["decision"]["council"]["max_weight"])
        for _ in range(30):
            council.record_outcome("WINNER", 5.0)
        w = council.weight("WINNER")
        self.assertGreater(w, 1.0)
        self.assertAlmostEqual(w, cap, places=6)

    def test_coinflip_stays_neutral(self):
        council, _ = _council(min_trades=5)
        for i in range(40):
            council.record_outcome("FLIP", 5.0 if i % 2 == 0 else -5.0)
        w = council.weight("FLIP")
        self.assertAlmostEqual(w, 1.0, places=2)

    def test_unknown_and_warming_up_are_neutral(self):
        council, cfg = _council(min_trades=5)
        default = float(cfg["decision"]["council"]["default_weight"])
        # Never seen -> neutral.
        self.assertEqual(council.weight("NEVER_SEEN"), default)
        # Seen only a few times (< min_trades) -> still neutral even if losing.
        for _ in range(4):
            council.record_outcome("YOUNG", -5.0)
        self.assertEqual(council.weight("YOUNG"), default)

    def test_young_loser_less_damped_than_seasoned_loser(self):
        """The UCB anti-burial bonus softens damping for low-sample arms."""
        council, _ = _council(min_trades=3)
        for _ in range(40):
            council.record_outcome("SEASONED", -5.0)
        for _ in range(4):
            council.record_outcome("YOUNG", -5.0)
        self.assertGreaterEqual(council.weight("YOUNG"), council.weight("SEASONED"))


class TestCouncilPersistence(unittest.TestCase):
    """Live credibility must survive a simulated restart via the memory store."""

    def _store(self, cfg):
        from core.memory.store import MemoryStore

        tmp = tempfile.mkdtemp(prefix="mt5bot_council_")
        cfg["memory"]["db_file"] = os.path.join(tmp, "memory.sqlite")
        cfg["memory"]["registry_file"] = os.path.join(tmp, "registry.json")
        return MemoryStore(cfg)

    def test_weights_persist_across_restart(self):
        from core.strategy.council import StrategyCouncil

        council, cfg = _council(min_trades=5)
        store = self._store(cfg)
        for _ in range(12):
            council.record_outcome("WIN", 5.0)
            council.record_outcome("LOSS", -5.0)
        w_win = council.weight("WIN")
        w_loss = council.weight("LOSS")
        self.assertGreater(w_win, 1.0)
        self.assertLess(w_loss, 1.0)

        # Persist, then simulate a restart: brand-new store + council on the
        # SAME db file, fed only from disk.
        store.save_council(council)

        store2 = self._store_reuse(cfg)
        council2 = StrategyCouncil(cfg)
        # Before loading, the fresh council knows nothing.
        self.assertEqual(council2.weight("WIN"),
                         float(cfg["decision"]["council"]["default_weight"]))
        store2.load_council(council2)
        # After loading, the exact weights are restored.
        self.assertAlmostEqual(council2.weight("WIN"), w_win, places=6)
        self.assertAlmostEqual(council2.weight("LOSS"), w_loss, places=6)
        # total_seen is preserved too.
        self.assertEqual(council2.arm_summary("WIN")["total_seen"], 12)

    def _store_reuse(self, cfg):
        """A second MemoryStore pointing at the SAME db_file already in cfg."""
        from core.memory.store import MemoryStore

        return MemoryStore(cfg)


class TestCouncilInEngineBlend(unittest.TestCase):
    """The engine ensemble blend uses council weights only when enabled."""

    def _engine_with_ensemble(self, enabled):
        from config.loader import load_config
        from core.decision.engine import DecisionEngine
        from core.strategy.council import StrategyCouncil
        from core.strategy.strategy import Strategy, StrategySpec

        cfg = load_config()
        cfg["decision"]["council"]["enabled"] = enabled
        cfg["decision"]["council"]["min_trades"] = 1
        council = StrategyCouncil(cfg)
        engine = DecisionEngine(cfg, council=council if enabled else None)

        # Two strategies with DISTINCT fingerprints and opposite constant
        # signals: A is bullish (+1), B is bearish (-1).
        spec_a = StrategySpec(indicators={"rsi": {}}, weights={"rsi": 1.0},
                              symbol="X", timeframe="M15", name="A")
        spec_b = StrategySpec(indicators={"sma": {}}, weights={"sma": 1.0},
                              symbol="X", timeframe="M15", name="B")
        self.assertNotEqual(spec_a.fingerprint(), spec_b.fingerprint())
        strat_a, strat_b = Strategy(spec_a), Strategy(spec_b)
        strat_a.blended_signal = lambda ohlcv: 1.0
        strat_b.blended_signal = lambda ohlcv: -1.0
        engine._ensemble_cache["X|M15"] = [strat_a, strat_b]
        return engine, council, spec_a, spec_b

    def test_council_off_is_plain_average(self):
        engine, _, _, _ = self._engine_with_ensemble(enabled=False)
        sig, label, _sl, _tp = engine._indicator_signal(None, "X", "M15")
        # Opposite equal-weight signals average to ~0, labeled plain "ensemble".
        self.assertAlmostEqual(sig, 0.0, places=9)
        self.assertEqual(label, "ensemble")

    def test_council_on_tilts_toward_winner(self):
        engine, council, spec_a, spec_b = self._engine_with_ensemble(enabled=True)
        # Equal credibility first -> blend still ~0.
        sig0, label0, _, _ = engine._indicator_signal(None, "X", "M15")
        self.assertAlmostEqual(sig0, 0.0, places=6)
        self.assertEqual(label0, "ensemble+council")
        # Make A a consistent winner and B a consistent loser.
        for _ in range(20):
            council.record_outcome(spec_a.fingerprint(), 5.0)
            council.record_outcome(spec_b.fingerprint(), -5.0)
        sig1, label1, _, _ = engine._indicator_signal(None, "X", "M15")
        # Now the bullish (A) strategy carries more weight -> blend tilts > 0.
        self.assertGreater(sig1, 0.1)
        self.assertEqual(label1, "ensemble+council")


if __name__ == "__main__":
    unittest.main()
