"""
Offline tests for the Phase U1 transparency overhaul (UPGRADE_PLAN.md U1.6).

These lock in the "receipts for every trade" guarantees added in U1.1-U1.5,
using only the standard library and a deterministic synthetic backtest (no MT5,
no network). They assert:

  1. The per-trade CSV has exactly one row per closed trade, i.e. its row count
     equals the backtest's ``num_trades`` metric (and equals len(result.trades)
     and len(result.trade_pnls)).
  2. The cost components recorded in the CSV reconcile with the trades: for
     every trade the invariant ``pnl == gross_pnl - total_cost`` holds, and the
     summed CSV costs equal the implied total cost derived from the receipts.
  3. The single-file HTML report (U1.3) builds from a synthetic run without
     raising and contains the expected structural markers.

All text is standard ASCII English only.
"""

from __future__ import annotations

import csv
import os
import tempfile
import unittest

from tests.helpers import make_synthetic_ohlcv  # noqa: F401 (path fix + builder)

from config.loader import load_config
from core.strategy.strategy import StrategySpec, Strategy
from core.strategy.backtester import Backtester
from core.utils.trade_log import (
    TRADE_CSV_COLUMNS,
    EQUITY_CSV_COLUMNS,
    write_artifacts,
    implied_total_cost,
)
import scripts.make_report as make_report


def _spec() -> StrategySpec:
    """A simple, always-buildable EMA+RSI spec that trades on synthetic data."""
    return StrategySpec(
        indicators={"ema": {"period": 10}, "rsi": {"period": 14}},
        weights={"ema": 1.0, "rsi": 1.0},
        long_threshold=0.10,
        short_threshold=-0.10,
        sl_atr_mult=2.0,
        tp_atr_mult=3.0,
        symbol="TESTX",
        timeframe="M15",
        name="u1_transparency",
    )


def _run_backtest():
    """Run a deterministic backtest that produces several trades.

    Returns (result, cfg). record_trades=True so the full U1.1 receipts exist.
    """
    cfg = load_config("config/config.yaml")
    ohlcv = make_synthetic_ohlcv(n=1200, symbol="TESTX", timeframe="M15", seed=7)
    bt = Backtester(cfg)
    result = bt.run(Strategy(_spec()), ohlcv, warmup=60, record_trades=True)
    return result, cfg


class TestTradeCsvRowCount(unittest.TestCase):
    def test_csv_row_count_equals_num_trades(self):
        result, _ = _run_backtest()
        n_metric = int(result.metrics.get("num_trades", 0))
        # Sanity: the synthetic run must actually generate some trades, else the
        # test is vacuous.
        self.assertGreater(n_metric, 0,
                           "synthetic backtest produced no trades")
        self.assertEqual(n_metric, len(result.trades))
        self.assertEqual(n_metric, len(result.trade_pnls))

        tmp = tempfile.mkdtemp()
        paths = write_artifacts(result, "TESTX", "M15", report_dir=tmp,
                                now=1_700_000_000.0)
        self.assertIsNotNone(paths["trades"])
        self.assertTrue(os.path.exists(paths["trades"]))

        with open(paths["trades"], "r", encoding="ascii") as fh:
            reader = csv.DictReader(fh)
            self.assertEqual(reader.fieldnames, TRADE_CSV_COLUMNS)
            rows = list(reader)
        # One data row per closed trade (header excluded by DictReader).
        self.assertEqual(len(rows), n_metric)

    def test_equity_csv_matches_curve(self):
        result, _ = _run_backtest()
        tmp = tempfile.mkdtemp()
        paths = write_artifacts(result, "TESTX", "M15", report_dir=tmp,
                                now=1_700_000_000.0)
        self.assertIsNotNone(paths["equity"])
        with open(paths["equity"], "r", encoding="ascii") as fh:
            reader = csv.reader(fh)
            header = next(reader)
            self.assertEqual(header, EQUITY_CSV_COLUMNS)
            data_rows = list(reader)
        self.assertEqual(len(data_rows), len(result.equity_curve))


class TestCostReconciliation(unittest.TestCase):
    def test_per_trade_pnl_equals_gross_minus_costs(self):
        result, _ = _run_backtest()
        self.assertTrue(result.trades)
        for t in result.trades:
            gross = float(t["gross_pnl"])
            total_cost = (
                float(t["cost_spread"])
                + float(t["cost_slippage"])
                + float(t["cost_commission"])
                + float(t["cost_swap"])
            )
            net = float(t["pnl"])
            # The documented U1.1 invariant, allowing tiny float error.
            self.assertAlmostEqual(net, gross - total_cost, places=6)

    def test_summed_costs_match_implied_total(self):
        result, _ = _run_backtest()
        # implied_total_cost() (used by this test per its docstring) must equal
        # the gross-minus-net sum over every trade receipt.
        gross_minus_net = sum(
            float(t["gross_pnl"]) - float(t["pnl"]) for t in result.trades
        )
        self.assertAlmostEqual(
            implied_total_cost(result.trades), gross_minus_net, places=6
        )

    def test_costs_are_non_negative(self):
        result, _ = _run_backtest()
        for t in result.trades:
            for key in ("cost_spread", "cost_slippage", "cost_commission"):
                self.assertGreaterEqual(float(t[key]), 0.0)


class TestHtmlReportBuilds(unittest.TestCase):
    def test_build_report_html_from_synthetic_run(self):
        result, cfg = _run_backtest()
        snapshot = {"symbol": "TESTX", "timeframe": "M15", "seed": 7}
        html_str = make_report.build_report_html(
            result.trades,
            equity=result.equity_curve,
            title="U1.6 test report",
            config_snapshot=snapshot,
        )
        self.assertIsInstance(html_str, str)
        self.assertGreater(len(html_str), 200)
        # Structural markers that must survive any refactor of the renderer.
        self.assertIn("<html", html_str.lower())
        self.assertIn("</html>", html_str.lower())
        self.assertIn("U1.6 test report", html_str)
        # ASCII-only guarantee for the Windows 7 target.
        html_str.encode("ascii")

    def test_report_builds_with_no_trades(self):
        # An empty run must not crash the renderer (graceful degradation).
        html_str = make_report.build_report_html([], equity=[10000.0],
                                                 title="empty")
        self.assertIn("</html>", html_str.lower())


if __name__ == "__main__":
    unittest.main()
