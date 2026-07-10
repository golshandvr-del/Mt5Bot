"""
U5.4 - gauntlet gate tests.

Two families of tests:

  A) The five gauntlet gates (scripts/gauntlet.py) fire correctly on PLANTED
     fixtures, per the UPGRADE_PLAN acceptance:
       - a cost-fragile strategy FAILS Gate 4 (spread x1.5 wipes the edge),
       - a lucky-ORDER strategy FAILS Gate 3 (positive sum but high ruin risk),
       - a robust strategy PASSES both.
     Gate 3 is a pure function of the realized per-trade PnLs, so we feed it a
     crafted `gate1` dict. Gate 4 runs a backtest per spread multiplier, so we
     stub the tiny `_run_bt` seam with a scripted net_profit(spread) curve -
     this keeps the test deterministic and offline while still exercising the
     REAL pass/fail logic.

  B) The U5.3 live pre-flight gate (app/gauntlet_gate.py) allows/blocks live
     start correctly: missing verdict -> block, FAIL verdict -> block, stale
     verdict (older than the last registry update) -> block, current PASS ->
     allow, no promoted strategy -> allow, flag off -> allow.

All offline / stdlib only; Windows 7 + Python 3.8 friendly. ASCII only.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest

from tests.helpers import PROJECT_ROOT  # noqa: F401  (path fix side effect)

from config.loader import load_config
from core.strategy.strategy import StrategySpec
import scripts.gauntlet as G
import app.gauntlet_gate as GATE


def _dummy_spec():
    """A minimal valid StrategySpec so Strategy(spec) constructs cleanly; the
    Gate-4 tests stub _run_bt so the spec's behavior is never actually used."""
    return StrategySpec(
        indicators={"ema": {"period": 20}},
        weights={"ema": 1.0},
        long_threshold=0.2, short_threshold=0.2,
        sl_atr_mult=2.0, tp_atr_mult=3.0,
        symbol="TESTX", timeframe="M15",
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class _FakeResult(object):
    """Stand-in for BacktestResult exposing just what the gates read."""

    def __init__(self, trade_pnls, net_profit=None):
        self.trade_pnls = list(trade_pnls)
        total = sum(self.trade_pnls) if net_profit is None else net_profit
        self.metrics = {
            "net_profit": total,
            "expectancy": (total / len(self.trade_pnls)
                           if self.trade_pnls else 0.0),
            "num_trades": len(self.trade_pnls),
        }


def _gate1_from_pnls(pnls):
    """Build a Gate-1 dict shaped like gate_full_history's output."""
    res = _FakeResult(pnls)
    return {"name": "Gate 1", "passed": True, "reason": "planted",
            "metrics": res.metrics, "result": res}


# --------------------------------------------------------------------------- #
# A) Gate 3 - Monte-Carlo trade-order bootstrap
# --------------------------------------------------------------------------- #
class TestGate3MonteCarlo(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()

    def test_lucky_order_strategy_fails_gate3(self):
        """One giant win + many bleed losses: net can be >0 but almost every
        shuffle draws the account below the 50% ruin line first -> high
        risk-of-ruin -> Gate 3 must FAIL."""
        init = float(self.cfg.get_path("backtest.initial_balance", 10000.0))
        # 30 losers of -600 each (=-18000) then one +18300 winner.
        # Sum = +300 (>0), but any order that front-loads a few losers blows
        # through the -5000 (50%) ruin line, so ruin rate is high.
        pnls = [-600.0] * 30 + [18300.0]
        gate1 = _gate1_from_pnls(pnls)
        res = G.gate_monte_carlo(self.cfg, gate1, n_shuffles=500,
                                 risk_pct=0.01, seed=1)
        self.assertFalse(res["passed"],
                         "lucky-order strategy should FAIL gate 3: %s"
                         % res["reason"])
        self.assertGreater(res["metrics"]["risk_of_ruin"], 0.05)
        # sanity: final equity is order-invariant = start + sum(pnls)
        self.assertAlmostEqual(res["metrics"]["final_equity"],
                               init + sum(pnls), places=2)

    def test_robust_strategy_passes_gate3(self):
        """Many small, evenly-sized winners with a few small losers: no shuffle
        can wipe the account, ruin rate ~0 -> Gate 3 PASSES."""
        pnls = ([120.0] * 40) + ([-40.0] * 20)  # net +4000, tiny swings
        gate1 = _gate1_from_pnls(pnls)
        res = G.gate_monte_carlo(self.cfg, gate1, n_shuffles=500,
                                 risk_pct=0.01, seed=2)
        self.assertTrue(res["passed"],
                        "robust strategy should PASS gate 3: %s" % res["reason"])
        self.assertLessEqual(res["metrics"]["risk_of_ruin"], 0.05)

    def test_too_few_trades_fails_gate3(self):
        gate1 = _gate1_from_pnls([10.0, -5.0, 3.0])  # < 10 trades
        res = G.gate_monte_carlo(self.cfg, gate1, n_shuffles=100,
                                 risk_pct=0.01, seed=3)
        self.assertFalse(res["passed"])
        self.assertIn("too few", res["reason"])


# --------------------------------------------------------------------------- #
# A) Gate 4 - cost stress (spread x1.5 must survive)
# --------------------------------------------------------------------------- #
class TestGate4CostStress(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self._orig_run_bt = G._run_bt

    def tearDown(self):
        G._run_bt = self._orig_run_bt

    def _install_scripted_run_bt(self, net_by_mult):
        """Replace _run_bt with a stub that returns a scripted net_profit
        keyed off the spread multiplier baked into the (cloned) config by
        gate_cost_stress via _cfg_with_spread_mult. That helper reliably scales
        backtest.spread_model.base_points, so we recover the multiplier from it
        (falling back to the flat spread_points)."""
        base = float(self.cfg.get_path("backtest.spread_model.base_points",
                                       self.cfg.get_path(
                                           "backtest.spread_points", 25))
                     or 25)

        def _stub(cfg, strategy, ohlcv, warmup, record_trades=False):
            cur = cfg.get_path("backtest.spread_model.base_points", None)
            if cur is None:
                cur = cfg.get_path("backtest.spread_points", base)
            mult = round(float(cur) / base, 3) if base else 1.0
            net = net_by_mult.get(mult, 0.0)
            return _FakeResult([net], net_profit=net)

        G._run_bt = _stub

    def test_cost_fragile_strategy_fails_gate4(self):
        """Profitable at x1 but underwater once spread widens x1.5 -> FAIL."""
        self._install_scripted_run_bt({1.5: -50.0, 2.0: -300.0})
        res = G.gate_cost_stress(self.cfg, spec=_dummy_spec(), ohlcv=None, warmup=60)
        self.assertFalse(res["passed"],
                         "cost-fragile strategy should FAIL gate 4: %s"
                         % res["reason"])
        self.assertLessEqual(res["metrics"]["net_profit_x1_5"], 0.0)

    def test_cost_robust_strategy_passes_gate4(self):
        """Still net-positive at x1.5 -> PASS (x2.0 is informational)."""
        self._install_scripted_run_bt({1.5: 800.0, 2.0: 200.0})
        res = G.gate_cost_stress(self.cfg, spec=_dummy_spec(), ohlcv=None, warmup=60)
        self.assertTrue(res["passed"],
                        "cost-robust strategy should PASS gate 4: %s"
                        % res["reason"])
        self.assertGreater(res["metrics"]["net_profit_x1_5"], 0.0)


# --------------------------------------------------------------------------- #
# B) U5.3 live pre-flight gate (app/gauntlet_gate.py)
# --------------------------------------------------------------------------- #
class _FakeMemory(object):
    """Minimal MemoryStore stand-in: only a registry_path is needed because the
    gate reads the JSON registry file directly via read_json."""

    def __init__(self, registry_path):
        self.registry_path = registry_path


class TestLiveGauntletGate(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.tmp = tempfile.mkdtemp()
        # Point the report dir + registry at the temp dir.
        self.cfg.setdefault("backtest", {})["report_dir"] = self.tmp
        self.cfg.setdefault("general", {})["live_requires_gauntlet"] = True
        self.registry_path = os.path.join(self.tmp, "registry.json")
        self.memory = _FakeMemory(self.registry_path)
        self.symbol, self.tf, self.fp = "TESTX", "M15", "FP_TOP1"

    # -- registry / verdict fixtures ------------------------------------ #
    def _write_registry(self, updated_at, fingerprint="FP_TOP1"):
        from core.utils.helpers import write_json
        reg = {}
        if fingerprint is not None:
            reg["%s|%s" % (self.symbol, self.tf)] = {
                "rank_metric": "expectancy",
                "updated_at": updated_at,
                "top": [{"fingerprint": fingerprint,
                         "spec": {"fingerprint": fingerprint}}],
            }
        else:
            reg["%s|%s" % (self.symbol, self.tf)] = {
                "rank_metric": "expectancy", "updated_at": updated_at,
                "top": [],
            }
        write_json(self.registry_path, reg)

    def _write_verdict(self, fingerprint, overall_pass, created_at_epoch):
        path = os.path.join(self.tmp, "gauntlet_%s.md" % fingerprint)
        status = "true" if overall_pass else "false"
        with open(path, "w", encoding="ascii") as fh:
            fh.write("# Gauntlet verdict\n"
                     "- created_at_epoch: %d\n"
                     "- overall_pass: %s\n" % (int(created_at_epoch), status))
        return path

    # -- tests ---------------------------------------------------------- #
    def test_missing_verdict_blocks(self):
        self._write_registry(updated_at=1000.0)
        # no verdict file written
        result = GATE.check_live_allowed(self.cfg, self.memory,
                                         [self.symbol], self.tf)
        self.assertFalse(result.allowed)
        self.assertTrue(any("no gauntlet verdict" in r for r in result.reasons))

    def test_fail_verdict_blocks(self):
        self._write_registry(updated_at=1000.0)
        self._write_verdict(self.fp, overall_pass=False, created_at_epoch=2000)
        result = GATE.check_live_allowed(self.cfg, self.memory,
                                         [self.symbol], self.tf)
        self.assertFalse(result.allowed)
        self.assertTrue(any("FAIL" in r for r in result.reasons))

    def test_stale_verdict_blocks(self):
        # verdict predates the last search/registry update -> stale -> block
        self._write_registry(updated_at=5000.0)
        self._write_verdict(self.fp, overall_pass=True, created_at_epoch=4000)
        result = GATE.check_live_allowed(self.cfg, self.memory,
                                         [self.symbol], self.tf)
        self.assertFalse(result.allowed)
        self.assertTrue(any("STALE" in r for r in result.reasons))

    def test_current_pass_allows(self):
        self._write_registry(updated_at=1000.0)
        self._write_verdict(self.fp, overall_pass=True, created_at_epoch=1000)
        result = GATE.check_live_allowed(self.cfg, self.memory,
                                         [self.symbol], self.tf)
        self.assertTrue(result.allowed, result.reasons)

    def test_no_promoted_strategy_allows(self):
        # empty top -> nothing to trade -> does not block live start
        self._write_registry(updated_at=1000.0, fingerprint=None)
        result = GATE.check_live_allowed(self.cfg, self.memory,
                                         [self.symbol], self.tf)
        self.assertTrue(result.allowed, result.reasons)
        self.assertTrue(any("no promoted strategy" in r for r in result.reasons))

    def test_flag_off_allows_without_verdict(self):
        self.cfg["general"]["live_requires_gauntlet"] = False
        self._write_registry(updated_at=1000.0)
        # no verdict at all, but flag disabled -> allowed
        result = GATE.check_live_allowed(self.cfg, self.memory,
                                         [self.symbol], self.tf)
        self.assertTrue(result.allowed)
        self.assertTrue(any("gate disabled" in r for r in result.reasons))

    def test_parse_verdict_file_edge_cases(self):
        # unparseable / partial files -> None (treated as missing -> block)
        bad = os.path.join(self.tmp, "gauntlet_BAD.md")
        with open(bad, "w", encoding="ascii") as fh:
            fh.write("# no stamps here\n")
        self.assertIsNone(GATE.parse_verdict_file(bad))
        self.assertIsNone(GATE.parse_verdict_file(
            os.path.join(self.tmp, "does_not_exist.md")))


if __name__ == "__main__":
    unittest.main()
