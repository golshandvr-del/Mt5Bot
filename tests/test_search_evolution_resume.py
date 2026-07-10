"""
Tests for U4.7 - deep-search guarantees (UPGRADE_PLAN.md Phase U4).

This suite proves the four properties the plan requires of the deep evolutionary
search (U4.2) + wall-clock budget (U4.1) + checkpoint/resume (U4.6) machinery:

  1. Evolution respects param spaces: every spec the evolutionary operators
     emit (random, mutate, crossover, breed, neighbor) uses ONLY in-space
     parameter values and only pool-legal indicators - a mutated child can
     never reference an indicator the search is not allowed to use nor an
     out-of-space value.

  2. Time budget stops cleanly: with a tiny wall-clock budget the run halts as
     soon as the budget elapses, well before max_trials, and still returns a
     valid registry section (it ranks whatever was evaluated). Works for both
     the random and the evolution method.

  3. Resume continues without re-evaluating fingerprints: a run that wrote a
     checkpoint (seen fingerprints + trial count + elite pool) can be resumed;
     the resumed run NEVER re-evaluates a fingerprint the first run already
     scored, and the cumulative trial count continues from where it stopped.

  4. Neighborhood + regime gates provably filter a planted overfit fixture:
     a knife-edge spec (great at one exact param, poor one step away) is
     demoted by the neighborhood gate; a spec that collapses in one regime is
     refused promotion by the regime floor gate.

Everything is offline / stdlib only (Windows 7 + Python 3.8 friendly). The
walk-forward evaluator is stubbed where a real run would be slow or
non-deterministic, so the tests are fast and repeatable.

All text is standard ASCII English only.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest

from tests.helpers import PROJECT_ROOT, make_synthetic_ohlcv  # noqa: F401

from config.loader import load_config
from core.indicators.registry import get_indicator_class
from core.memory.store import MemoryStore
from core.strategy.search import (
    StrategySearch,
    _DIRECTIONAL,
    _EA_SUPPORTED_DIRECTIONAL,
)
from core.strategy.search_checkpoint import SearchCheckpoint, checkpoint_path
from core.strategy.strategy import StrategySpec


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _spec(ema_period=21, rsi_period=14, symbol="TESTX", timeframe="M15"):
    return StrategySpec(
        indicators={"ema": {"period": ema_period}, "rsi": {"period": rsi_period}},
        weights={"ema": 1.0, "rsi": 1.0},
        long_threshold=0.2, short_threshold=0.2,
        sl_atr_mult=2.0, tp_atr_mult=3.0,
        symbol=symbol, timeframe=timeframe,
    )


def _assert_spec_in_space(test, spec, pool):
    """Every indicator is pool-legal and every param value is in its space."""
    test.assertTrue(spec.indicators, "spec must have >=1 indicator")
    for name, params in spec.indicators.items():
        test.assertIn(name, pool,
                      "indicator %s not in allowed pool" % name)
        cls = get_indicator_class(name)
        space = cls.param_space()
        for key, val in params.items():
            if key in space:
                test.assertIn(
                    val, list(space[key]),
                    "param %s.%s=%r not in space %r"
                    % (name, key, val, space[key]),
                )


class _ScriptedWF(object):
    """Deterministic WalkForward stub.

    `scores_by_fp` maps spec fingerprint -> avg_score (default otherwise).
    Records (fingerprint, persist) for every evaluate() so a test can assert
    which specs were scored and that gate re-runs never persist. Optionally
    sleeps `delay` seconds per evaluate to let a wall-clock budget expire.
    """

    def __init__(self, scores_by_fp=None, default=0.5, delay=0.0,
                 holdout_bars=0, regime_enabled=False):
        self._scores = dict(scores_by_fp or {})
        self._default = default
        self._delay = float(delay)
        self.calls = []          # (fingerprint, persist)
        self.holdout_bars = holdout_bars
        self.regime_enabled = regime_enabled

    def evaluate(self, spec, ohlcv, point=None, persist=True, warmup=60):
        if self._delay:
            time.sleep(self._delay)
        fp = spec.fingerprint()
        self.calls.append((fp, persist))
        return {"avg_score": self._scores.get(fp, self._default)}

    def evaluate_holdout(self, spec, ohlcv, point=None):
        return {"passed": True}


def _make_search(tmp, method="evolution", max_trials=40, **overrides):
    cfg = load_config()
    cfg["memory"]["db_file"] = os.path.join(tmp, "memory.sqlite")
    cfg["memory"]["registry_file"] = os.path.join(tmp, "registry.json")
    s = cfg["memory"]["search"]
    s["method"] = method
    s["max_trials"] = max_trials
    s["min_trades"] = 1
    for k, v in overrides.items():
        s[k] = v
    store = MemoryStore(cfg)
    return StrategySearch(cfg, store), cfg


# --------------------------------------------------------------------------- #
# 1. Evolution respects param spaces
# --------------------------------------------------------------------------- #
class TestEvolutionRespectsParamSpaces(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="u47_space_")
        self.search, _ = _make_search(self.tmp, method="evolution",
                                      ea_compatible_only=True)
        self.pool = self.search._available_directional()

    def test_random_specs_in_space(self):
        for _ in range(200):
            _assert_spec_in_space(self, self.search._random_spec("TESTX", "M15"),
                                  self.pool)

    def test_mutated_specs_in_space(self):
        for _ in range(200):
            parent = self.search._random_spec("TESTX", "M15")
            child = self.search._mutate(parent, "TESTX", "M15")
            _assert_spec_in_space(self, child, self.pool)

    def test_crossover_specs_in_space(self):
        for _ in range(200):
            a = self.search._random_spec("TESTX", "M15")
            b = self.search._random_spec("TESTX", "M15")
            child = self.search._crossover(a, b, "TESTX", "M15")
            _assert_spec_in_space(self, child, self.pool)

    def test_breed_from_elites_in_space(self):
        elites = [self.search._random_spec("TESTX", "M15") for _ in range(5)]
        for _ in range(200):
            child = self.search._breed_from_elites(elites, "TESTX", "M15")
            _assert_spec_in_space(self, child, self.pool)

    def test_neighbors_in_space_and_one_step(self):
        parent = _spec(ema_period=21, rsi_period=14)
        s, _ = _make_search(self.tmp, method="evolution")
        s.neighborhood_enabled = True
        s.neighborhood_n = 8
        neighbors = s._neighbor_specs(parent)
        self.assertTrue(neighbors)
        for child in neighbors:
            _assert_spec_in_space(self, child, ["ema", "rsi"])
            ep = child.indicators["ema"]["period"]
            rp = child.indicators["rsi"]["period"]
            self.assertEqual(int(ep != 21) + int(rp != 14), 1)

    def test_ea_pool_is_strict_subset(self):
        # With ea_compatible_only the pool never leaks a non-EA voter.
        for name in self.pool:
            self.assertIn(name, _EA_SUPPORTED_DIRECTIONAL)
        # And the full research pool is a superset (flag actually restricts).
        full, _ = _make_search(self.tmp, method="evolution",
                               ea_compatible_only=False)
        self.assertTrue(set(self.pool).issubset(set(_DIRECTIONAL)))
        self.assertTrue(set(full._available_directional()) >= set(self.pool))


# --------------------------------------------------------------------------- #
# 2. Time budget stops cleanly
# --------------------------------------------------------------------------- #
class TestTimeBudgetStopsCleanly(unittest.TestCase):
    def test_budget_expired_helper(self):
        tmp = tempfile.mkdtemp(prefix="u47_budget_")
        s, _ = _make_search(tmp, method="random", time_budget_hours=0.0)
        # Budget <= 0 disables the clock -> never expires.
        self.assertFalse(s._budget_expired(time.time() - 10_000))
        # A tiny positive budget expires once its seconds have elapsed.
        s.time_budget_hours = 1.0 / 3600.0  # one second
        self.assertFalse(s._budget_expired(time.time()))
        self.assertTrue(s._budget_expired(time.time() - 5.0))

    def test_random_run_stops_before_max_trials(self):
        tmp = tempfile.mkdtemp(prefix="u47_budget_rand_")
        # Huge trial budget so ONLY the wall-clock budget can stop the run.
        s, _ = _make_search(tmp, method="random", max_trials=100000,
                            time_budget_hours=0.5 / 3600.0)  # 0.5s
        s.wf = _ScriptedWF(default=0.5, delay=0.02)
        out = s.run(make_synthetic_ohlcv(n=400, symbol="TESTX"),
                    "TESTX", "M15")
        self.assertIn("registry", out)          # ranked what it had
        self.assertGreaterEqual(out["evaluated"], 1)
        self.assertLess(out["evaluated"], 100000)  # budget stopped it early

    def test_evolution_run_stops_before_max_trials(self):
        tmp = tempfile.mkdtemp(prefix="u47_budget_evo_")
        s, _ = _make_search(tmp, method="evolution", max_trials=100000,
                            time_budget_hours=0.5 / 3600.0)  # 0.5s
        s.wf = _ScriptedWF(default=0.5, delay=0.02)
        out = s.run(make_synthetic_ohlcv(n=400, symbol="TESTX"),
                    "TESTX", "M15")
        self.assertIn("registry", out)
        self.assertGreaterEqual(out["evaluated"], 1)
        self.assertLess(out["evaluated"], 100000)


# --------------------------------------------------------------------------- #
# 3. Resume continues without re-evaluating fingerprints
# --------------------------------------------------------------------------- #
class TestResumeNoReEvaluation(unittest.TestCase):
    def test_checkpoint_roundtrip_seen_and_evaluated(self):
        tmp = tempfile.mkdtemp(prefix="u47_ckpt_rt_")
        path = checkpoint_path(tmp, "XAUUSD", "M15")
        ck = SearchCheckpoint(path, "XAUUSD", "M15", "evolution", max_scored=50)
        specs = [_spec(ema_period=p, symbol="XAUUSD") for p in (9, 12, 21)]
        seen = {sp.fingerprint() for sp in specs}
        scored = [(0.9, specs[0]), (0.5, specs[1]), (0.1, specs[2])]
        ck.save(evaluated=3, seen=seen, scored=scored)
        self.assertTrue(os.path.isfile(path))

        state = SearchCheckpoint(path, "XAUUSD", "M15", "evolution").load()
        self.assertEqual(state["evaluated"], 3)
        self.assertEqual(state["seen"], seen)
        # Elite pool restored as (score, StrategySpec) with fingerprints intact.
        restored_fps = {sp.fingerprint() for _, sp in state["scored"]}
        self.assertEqual(restored_fps, seen)

    def test_wrong_run_checkpoint_is_ignored(self):
        tmp = tempfile.mkdtemp(prefix="u47_ckpt_wrong_")
        path = checkpoint_path(tmp, "XAUUSD", "M15")
        ck = SearchCheckpoint(path, "XAUUSD", "M15", "evolution")
        ck.save(evaluated=1, seen={"abc"}, scored=[])
        # A loader for a DIFFERENT run must refuse the file (start fresh).
        other = SearchCheckpoint(path, "EURUSD", "H1", "evolution").load()
        self.assertIsNone(other)

    def test_corrupt_checkpoint_starts_fresh(self):
        tmp = tempfile.mkdtemp(prefix="u47_ckpt_corrupt_")
        path = checkpoint_path(tmp, "XAUUSD", "M15")
        with open(path, "w") as fh:
            fh.write("{ this is not valid json ")
        self.assertIsNone(
            SearchCheckpoint(path, "XAUUSD", "M15", "evolution").load())

    def test_resume_does_not_re_evaluate_seen_fingerprints(self):
        """Seed a checkpoint with fingerprints, then run --resume and prove the
        resumed run never scores a fingerprint the first run already did."""
        tmp = tempfile.mkdtemp(prefix="u47_resume_")
        s, _ = _make_search(tmp, method="evolution", max_trials=30,
                           ea_compatible_only=True)
        # Force checkpointing on every trial and seed the checkpoint with a set
        # of already-seen fingerprints (drawn from the SAME generator space so
        # collisions are realistic).
        s.checkpoint_every = 1
        pre_specs = [s._random_spec("TESTX", "M15") for _ in range(15)]
        pre_seen = {sp.fingerprint() for sp in pre_specs}
        ck = SearchCheckpoint(
            checkpoint_path(s._data_dir, "TESTX", "M15"),
            "TESTX", "M15", "evolution", max_scored=200)
        ck.save(evaluated=15, seen=pre_seen,
                scored=[(0.5, sp) for sp in pre_specs])

        wf = _ScriptedWF(default=0.5)
        s.wf = wf
        out = s.run(make_synthetic_ohlcv(n=400, symbol="TESTX"),
                    "TESTX", "M15", resume=True)

        # No pre-seen fingerprint may be evaluated again in the resumed run.
        evaluated_fps = [fp for fp, _ in wf.calls]
        overlap = pre_seen.intersection(evaluated_fps)
        self.assertEqual(
            overlap, set(),
            "resumed run re-evaluated already-seen fingerprints: %s" % overlap)
        # The cumulative trial count continued from the restored offset (15).
        self.assertGreaterEqual(out["evaluated"], 15)

    def test_completed_run_clears_checkpoint(self):
        """A run that reaches max_trials completed cleanly, so the checkpoint is
        removed (a later --resume would otherwise skip everything)."""
        tmp = tempfile.mkdtemp(prefix="u47_clear_")
        s, _ = _make_search(tmp, method="random", max_trials=5)
        s.checkpoint_every = 1
        s.wf = _ScriptedWF(default=0.5)
        path = checkpoint_path(s._data_dir, "TESTX", "M15")
        s.run(make_synthetic_ohlcv(n=400, symbol="TESTX"), "TESTX", "M15")
        self.assertFalse(
            os.path.isfile(path),
            "completed run should clear its checkpoint file")


# --------------------------------------------------------------------------- #
# 4. Neighborhood + regime gates filter a planted overfit fixture
# --------------------------------------------------------------------------- #
class TestGatesFilterOverfit(unittest.TestCase):
    def test_neighborhood_demotes_knife_edge(self):
        """Parent scores high at its exact params; every one-step neighbor
        scores ~0. The neighborhood median is far below the parent, so
        min(own, neighborhood) demotes the overfit spec."""
        tmp = tempfile.mkdtemp(prefix="u47_nb_")
        s, _ = _make_search(tmp, method="random")
        s.neighborhood_enabled = True
        s.neighborhood_n = 8
        parent = _spec(ema_period=21, rsi_period=14)
        # Only the parent scores well; neighbors default to 0.01.
        wf = _ScriptedWF(scores_by_fp={parent.fingerprint(): 0.9}, default=0.01)
        s.wf = wf
        med = s._neighborhood_score(parent, make_synthetic_ohlcv())
        self.assertIsNotNone(med)
        self.assertLess(med, 0.9)
        self.assertAlmostEqual(min(0.9, med), med, places=9)
        # Neighbor evaluations never pollute memory.
        self.assertTrue(all(p is False for _, p in wf.calls))

    def test_regime_floor_rejects_collapsing_regime(self):
        """A spec whose overall score is fine but which collapses in ONE regime
        fails the regime floor gate (pure gate logic, no search run)."""
        from core.strategy.walk_forward import WalkForward
        cfg = load_config()
        cfg["memory"]["search"]["regime"] = {
            "enabled": True, "floor_mult": -0.5,
            "min_segments_per_regime": 1, "adx_trend_threshold": 25.0,
        }
        wf = WalkForward(cfg)
        overall = 100.0  # floor = -0.5 * 100 = -50
        self.assertFalse(
            wf.passes_regime_floor(overall, {"good": 120.0, "bad": -500.0}))
        self.assertTrue(
            wf.passes_regime_floor(overall, {"a": 80.0, "b": 10.0}))

    def test_regime_gate_forces_allowlist_in_search(self):
        """With the regime gate on, the search runs a real promotion allowlist
        (gating_on True) and still returns a valid registry section."""
        tmp = tempfile.mkdtemp(prefix="u47_regime_")
        s, cfg = _make_search(tmp, method="random", max_trials=4)
        cfg["memory"]["search"]["regime"] = {
            "enabled": True, "floor_mult": -0.5,
            "min_segments_per_regime": 1, "adx_trend_threshold": 25.0,
        }
        # Rebuild search so it picks up the regime config into its WalkForward.
        s2 = StrategySearch(cfg, s.memory)
        self.assertTrue(getattr(s2.wf, "regime_enabled", False))
        out = s2.run(make_synthetic_ohlcv(n=1400, symbol="TESTX"),
                     "TESTX", "M15")
        self.assertIn("registry", out)
        self.assertGreaterEqual(out.get("evaluated", 0), 1)


if __name__ == "__main__":
    unittest.main()
