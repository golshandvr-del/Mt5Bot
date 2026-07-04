"""
Tests for the weekend/rollover SWAP + Monday GAP model in the internal
backtester (Track A / A6, sub-step P3.7).

These lock in P3.6:
  1. A position held across a weekend pays swap, and the swap is billed as 3
     nights (Fri->Mon) times the configured rate; with swap 0.0 (default) no
     swap is charged (byte-identical old behavior).
  2. The triple-swap weekday is charged 3x while a normal midnight is 1x.
  3. When model_weekend_gap is on, a stop that sits INSIDE a modeled Monday gap
     fills at the (worse) gapped OPEN price, not at the stop price; with the gap
     model OFF (default) the same stop fills exactly at the stop price.

The tests use a tiny STUB strategy so the entry bar, stop distance, and the
exact bar geometry are fully deterministic and independent of any indicator.
Everything is standard-library only, ASCII English only.
"""

from __future__ import annotations

import datetime
import unittest

from tests.helpers import ensure_project_on_path

ensure_project_on_path()

from core.data.data_feed import OHLCV
from core.strategy.backtester import Backtester


# --------------------------------------------------------------------------- #
# Minimal stub strategy: full control over decision / atr / SL-TP multiples.
# --------------------------------------------------------------------------- #
class _StubSpec(object):
    def __init__(self, symbol, sl_atr_mult, tp_atr_mult):
        self.symbol = symbol
        self.sl_atr_mult = float(sl_atr_mult)
        self.tp_atr_mult = float(tp_atr_mult)


class _StubStrategy(object):
    """Returns caller-supplied decision + atr series verbatim."""

    def __init__(self, spec, decisions, atrs):
        self.spec = spec
        self._decisions = decisions
        self._atrs = atrs

    def decision_series(self, ohlcv):
        return list(self._decisions)

    def atr_series(self, ohlcv):
        return list(self._atrs)


def _cfg(**backtest_overrides):
    """Build a minimal DotDict-like config for the Backtester.

    Uses a plain dict-of-dict; Backtester reads via cfg.get('backtest', {}) and
    then bt.get(key, default), which works on a normal dict.
    """
    bt = {
        "initial_balance": 10000.0,
        "spread_points": 0,
        "commission_per_lot": 0.0,
        "slippage_points": 0,
        "fixed_lot": 1.0,
    }
    bt.update(backtest_overrides)

    class _Cfg(dict):
        def get(self, key, default=None):
            return dict.get(self, key, default)

    return _Cfg({"backtest": bt})


def _mk_series(rows, symbol="EURUSD", timeframe="M15"):
    """rows: list of (ts, open, high, low, close). Returns an OHLCV."""
    ohlcv = OHLCV(symbol=symbol, timeframe=timeframe)
    for (ts, o, h, l, c) in rows:
        ohlcv.append_row(ts, o, h, l, c, 100.0)
    return ohlcv


# Anchor timestamps on a known Friday so the weekend hold is unambiguous.
# 2026-01-09 is a Friday (UTC).
def _utc(y, mo, d, h=0):
    return int(datetime.datetime(y, mo, d, h, 0,
                                 tzinfo=datetime.timezone.utc).timestamp())


class TestWeekendSwap(unittest.TestCase):
    """A position held over the weekend pays swap (3 nights, Fri->Mon)."""

    def _run(self, swap_long_pts):
        # The backtester early-returns unless n > warmup + 5, so provide plenty
        # of flat Friday lead-in bars before the entry.
        warmup = 2
        # Build: flat lead-in bars, enter long on Friday, hold flat-priced bars
        # through the weekend, then close Monday. Prices are constant so the
        # ONLY PnL difference between runs is the swap.
        price = 100.0
        rows = []
        # 10 flat Friday lead-in bars (indices 0..9), entry at index 10.
        for k in range(10):
            rows.append((_utc(2026, 1, 9, 6) + k * 900,
                         price, price, price, price))
        entry_idx = len(rows)                                      # 10 Fri ENTER
        rows.append((_utc(2026, 1, 9, 6) + entry_idx * 900,
                     price, price, price, price))
        # Monday bar (crosses Sat/Sun/Mon midnights => 3 nights).
        rows.append((_utc(2026, 1, 12, 8), price, price, price, price))  # 11 Mon
        rows.append((_utc(2026, 1, 12, 9), price, price, price, price))  # 12 Mon
        ohlcv = _mk_series(rows)

        n = len(rows)
        # ATR small and non-triggering; wide SL/TP so nothing stops out.
        atrs = [1.0] * n
        # Enter long at entry_idx and simply hold; the residual close at the
        # last (Monday) bar closes it at break-even price, so the ONLY PnL is
        # the accrued weekend swap. No opposite signal (which would open a new
        # short and produce a second trade).
        decisions = [0] * n
        decisions[entry_idx] = 1

        spec = _StubSpec("EURUSD", sl_atr_mult=50.0, tp_atr_mult=50.0)
        strat = _StubStrategy(spec, decisions, atrs)
        cfg = _cfg(swap_long_pts=swap_long_pts, swap_short_pts=0.0,
                   swap_triple_day=2, model_weekend_gap=False)
        bt = Backtester(cfg)
        res = bt.run(strat, ohlcv, warmup=warmup)
        return res

    def test_no_swap_is_noop(self):
        res = self._run(swap_long_pts=0.0)
        self.assertEqual(len(res.trade_pnls), 1)
        # Flat price, zero costs, zero swap -> exactly break-even.
        self.assertAlmostEqual(res.trade_pnls[0], 0.0, places=6)

    def test_weekend_hold_pays_three_nights(self):
        # EURUSD: point 0.0001, contract 100000, lot 1.0.
        # money per night = swap_pts * point * contract * lot.
        swap_pts = 10.0
        res = self._run(swap_long_pts=swap_pts)
        self.assertEqual(len(res.trade_pnls), 1)
        point = 0.0001
        contract = 100000.0
        expected_swap = swap_pts * point * contract * 1.0 * 3.0  # 3 nights
        # PnL == -swap (flat price, no other cost).
        self.assertAlmostEqual(res.trade_pnls[0], -expected_swap, places=6)
        self.assertGreater(expected_swap, 0.0)

    def test_negative_swap_is_a_credit(self):
        res = self._run(swap_long_pts=-4.0)
        self.assertEqual(len(res.trade_pnls), 1)
        # A negative swap rate is a CREDIT, so PnL is positive.
        self.assertGreater(res.trade_pnls[0], 0.0)


class TestRolloverCounting(unittest.TestCase):
    """Unit-level checks of the rollover/weekday counter."""

    def test_friday_to_monday_is_three(self):
        fri = _utc(2026, 1, 9, 20)
        mon = _utc(2026, 1, 12, 1)
        self.assertEqual(
            Backtester._rollovers_between(fri, mon, 2), 3.0)

    def test_triple_day_charged_three(self):
        # Tue -> Wed crosses one midnight entering Wednesday (triple day 2).
        tue = _utc(2026, 1, 6, 20)
        wed = _utc(2026, 1, 7, 1)
        self.assertEqual(
            Backtester._rollovers_between(tue, wed, 2), 3.0)

    def test_ordinary_night_charged_once(self):
        # Mon -> Tue crosses one midnight entering Tuesday (not the triple day).
        mon = _utc(2026, 1, 5, 20)
        tue = _utc(2026, 1, 6, 1)
        self.assertEqual(
            Backtester._rollovers_between(mon, tue, 2), 1.0)

    def test_same_day_no_rollover(self):
        t = _utc(2026, 1, 5, 8)
        self.assertEqual(
            Backtester._rollovers_between(t, t + 3600, 2), 0.0)


class TestMondayGapFill(unittest.TestCase):
    """A stop inside a modeled Monday gap fills at the gapped open price."""

    def _build(self):
        # Long entered Friday at 100 with stop at 98 (sl_atr_mult=2, atr=1).
        # Monday opens at 96 (a gap DOWN through the stop). With the gap model
        # ON the fill should be 96 (the open); OFF it should be 98 (the stop).
        warmup = 2
        rows = []
        # Flat Friday lead-in so n > warmup + 5; entry at entry_idx.
        for k in range(10):
            rows.append((_utc(2026, 1, 9, 6) + k * 900,
                         100.0, 100.0, 100.0, 100.0))
        entry_idx = len(rows)
        rows.append((_utc(2026, 1, 9, 6) + entry_idx * 900,
                     100.0, 100.0, 100.0, 100.0))                 # Fri ENTER
        # Monday gap bar: opens 96, low 95 -> low<=stop(98) triggers; the open
        # (96) is already below the stop (98), i.e. the stop is INSIDE the gap.
        rows.append((_utc(2026, 1, 12, 8), 96.0, 96.0, 95.0, 95.5))  # Mon GAP
        rows.append((_utc(2026, 1, 12, 9), 95.5, 95.5, 95.5, 95.5))  # Mon
        ohlcv = _mk_series(rows, symbol="EURUSD")
        n = len(rows)
        atrs = [1.0] * n
        decisions = [0] * n
        decisions[entry_idx] = 1  # enter long, no opposite signal afterwards
        spec = _StubSpec("EURUSD", sl_atr_mult=2.0, tp_atr_mult=100.0)
        strat = _StubStrategy(spec, decisions, atrs)
        return strat, ohlcv, warmup

    def test_gap_off_fills_at_stop(self):
        strat, ohlcv, warmup = self._build()
        cfg = _cfg(model_weekend_gap=False, swap_long_pts=0.0)
        res = Backtester(cfg).run(strat, ohlcv, warmup=warmup)
        self.assertEqual(len(res.trade_pnls), 1)
        # Fill at stop 98: move = (98-100) = -2 * lot * contract.
        expected = (98.0 - 100.0) * 1.0 * 100000.0
        self.assertAlmostEqual(res.trade_pnls[0], expected, places=4)

    def test_gap_on_fills_at_worse_open(self):
        strat, ohlcv, warmup = self._build()
        cfg = _cfg(model_weekend_gap=True, swap_long_pts=0.0)
        res = Backtester(cfg).run(strat, ohlcv, warmup=warmup)
        self.assertEqual(len(res.trade_pnls), 1)
        # Fill at gapped open 96 (worse than the 98 stop): move = (96-100).
        expected = (96.0 - 100.0) * 1.0 * 100000.0
        self.assertAlmostEqual(res.trade_pnls[0], expected, places=4)
        # And it must be strictly worse (more negative) than the stop fill.
        stop_fill = (98.0 - 100.0) * 1.0 * 100000.0
        self.assertLess(res.trade_pnls[0], stop_fill)


if __name__ == "__main__":
    unittest.main()
