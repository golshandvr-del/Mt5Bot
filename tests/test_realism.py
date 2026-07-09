"""
Tests for Phase U3 - PESSIMISTIC, REALISTIC SIMULATION (UPGRADE_PLAN U3.6).

These lock in the "pessimistic by default" guarantees of the internal
backtester so a future refactor can never quietly make it optimistic again.
The plan (U3.6) requires proving:

  1. next_open fills are NEVER better than signal_close fills.
  2. pessimistic intrabar resolution NEVER beats optimistic (and midpoint sits
     between the two).
  3. session-aware spread widening only ever COSTS more (never less).
  4. risk_pct sizing respects the min/max lot clamps.
  5. the max_daily_loss circuit breaker stops NEW entries after the day's
     realized loss crosses the limit.
  6. the U3.5 broker minimum-stop-distance check rejects too-tight entries.

Every test uses a tiny STUB strategy so the entry bar, stop distance and the
exact bar geometry are fully deterministic and independent of any indicator.
Standard library + unittest only, ASCII English only.
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
# It deliberately does NOT implement signal_series() so we also exercise the
# backtester's defensive read of the entry signal (recorded as 0.0 then).
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


def _cfg(backtest=None, risk=None):
    """Build a minimal dict-based config the Backtester can read."""
    bt = {
        "initial_balance": 10000.0,
        "spread_points": 0,
        "commission_per_lot": 0.0,
        "slippage_points": 0,
        "fixed_lot": 1.0,
    }
    if backtest:
        bt.update(backtest)
    rk = {
        "risk_per_trade": 0.01,
        "min_lot": 0.01,
        "max_lot": 1.0,
        "max_daily_loss": 0.0,
    }
    if risk:
        rk.update(risk)

    class _Cfg(dict):
        def get(self, key, default=None):
            return dict.get(self, key, default)

    return _Cfg({"backtest": bt, "risk": rk})


def _mk_series(rows, symbol="EURUSD", timeframe="M15"):
    """rows: list of (ts, open, high, low, close). Returns an OHLCV."""
    ohlcv = OHLCV(symbol=symbol, timeframe=timeframe)
    for (ts, o, h, l, c) in rows:
        ohlcv.append_row(ts, o, h, l, c, 100.0)
    return ohlcv


def _utc(y, mo, d, h=0):
    return int(datetime.datetime(y, mo, d, h, 0,
                                 tzinfo=datetime.timezone.utc).timestamp())


# --------------------------------------------------------------------------- #
# U3.1: next_open fills are never better than signal_close fills.
# --------------------------------------------------------------------------- #
class TestNextOpenNeverBetter(unittest.TestCase):
    """A next-bar-open entry must never produce more PnL than same-bar-close."""

    def _build(self):
        # Long entered on the signal bar; the price then RISES. A same-bar-close
        # entry buys cheaper (at the signal bar close) than a next-bar-open entry
        # (at the higher next open + half-spread + slippage), so signal_close is
        # the more favorable fill. The position is then closed at end-of-data.
        warmup = 2
        rows = []
        # Flat lead-in so n > warmup + 5.
        base = _utc(2026, 1, 6, 6)
        for k in range(8):
            rows.append((base + k * 900, 100.0, 100.0, 100.0, 100.0))
        entry_idx = len(rows)  # signal fires here
        rows.append((base + entry_idx * 900, 100.0, 100.0, 100.0, 100.0))  # ENTER
        # Price climbs afterwards; wide SL/TP so nothing stops out.
        rows.append((base + (entry_idx + 1) * 900, 101.0, 101.5, 100.8, 101.2))
        rows.append((base + (entry_idx + 2) * 900, 101.5, 102.0, 101.3, 101.8))
        ohlcv = _mk_series(rows)
        n = len(rows)
        atrs = [1.0] * n
        decisions = [0] * n
        decisions[entry_idx] = 1  # enter long, no opposite signal after
        spec = _StubSpec("EURUSD", sl_atr_mult=50.0, tp_atr_mult=50.0)
        return _StubStrategy(spec, decisions, atrs), ohlcv, warmup

    def _run(self, fill_policy):
        strat, ohlcv, warmup = self._build()
        cfg = _cfg(backtest={
            "fill_policy": fill_policy,
            "sizing": "fixed_lot",
            "spread_points": 10,
            "slippage_points": 2,
        })
        return Backtester(cfg).run(strat, ohlcv, warmup=warmup)

    def test_next_open_not_better_than_signal_close(self):
        close_res = self._run("signal_close")
        open_res = self._run("next_open")
        self.assertEqual(len(close_res.trade_pnls), 1)
        self.assertEqual(len(open_res.trade_pnls), 1)
        # next_open must be <= signal_close (never a more favorable outcome).
        self.assertLessEqual(open_res.trade_pnls[0],
                             close_res.trade_pnls[0] + 1e-9)
        # And in this rising-price scenario it must be strictly worse.
        self.assertLess(open_res.trade_pnls[0], close_res.trade_pnls[0])


# --------------------------------------------------------------------------- #
# U3.2: pessimistic intrabar never beats optimistic; midpoint sits between.
# --------------------------------------------------------------------------- #
class TestIntrabarPessimism(unittest.TestCase):
    """When one bar touches BOTH SL and TP, pessimistic <= midpoint <= optimistic."""

    def _build(self):
        # Long entered at 100, stop at 98 (sl_atr_mult=2, atr=1), take at 104
        # (tp_atr_mult=4). One later bar's range spans 97..105, touching BOTH the
        # stop (98) and the take (104). Pessimistic books the stop (a loss);
        # optimistic books the take (a win).
        warmup = 2
        base = _utc(2026, 1, 6, 6)
        rows = []
        for k in range(8):
            rows.append((base + k * 900, 100.0, 100.0, 100.0, 100.0))
        entry_idx = len(rows)
        rows.append((base + entry_idx * 900, 100.0, 100.0, 100.0, 100.0))  # ENTER
        # Ambiguous bar: low 97 (<98 stop), high 105 (>104 take).
        rows.append((base + (entry_idx + 1) * 900, 100.0, 105.0, 97.0, 100.0))
        rows.append((base + (entry_idx + 2) * 900, 100.0, 100.0, 100.0, 100.0))
        ohlcv = _mk_series(rows)
        n = len(rows)
        atrs = [1.0] * n
        decisions = [0] * n
        decisions[entry_idx] = 1
        spec = _StubSpec("EURUSD", sl_atr_mult=2.0, tp_atr_mult=4.0)
        return _StubStrategy(spec, decisions, atrs), ohlcv, warmup

    def _run(self, policy):
        strat, ohlcv, warmup = self._build()
        # Use signal_close + fixed_lot + zero costs so ONLY the intrabar policy
        # moves the number.
        cfg = _cfg(backtest={
            "fill_policy": "signal_close",
            "sizing": "fixed_lot",
            "intrabar_policy": policy,
            "spread_points": 0,
            "slippage_points": 0,
        })
        return Backtester(cfg).run(strat, ohlcv, warmup=warmup)

    def test_pessimistic_never_beats_optimistic(self):
        pess = self._run("pessimistic").trade_pnls[0]
        opt = self._run("optimistic").trade_pnls[0]
        mid = self._run("midpoint").trade_pnls[0]
        # pessimistic (stop first) must be a loss; optimistic (take first) a win.
        self.assertLess(pess, opt)
        # midpoint sits strictly between the two.
        self.assertLessEqual(pess, mid + 1e-9)
        self.assertLessEqual(mid, opt + 1e-9)
        # Exact values with zero costs, lot 1, contract 100000:
        #   pessimistic exits at 98 -> (98-100)*100000 = -200000
        #   optimistic  exits at 104 -> (104-100)*100000 = +400000
        self.assertAlmostEqual(pess, (98.0 - 100.0) * 100000.0, places=3)
        self.assertAlmostEqual(opt, (104.0 - 100.0) * 100000.0, places=3)


# --------------------------------------------------------------------------- #
# U3.3: session-aware spread widening only ever costs more.
# --------------------------------------------------------------------------- #
class TestSessionSpread(unittest.TestCase):
    """A trade entered inside the rollover window must cost >= the flat spread."""

    def _build(self, entry_hour):
        warmup = 2
        # Entry at a controllable UTC hour so we can put it in/out of the
        # rollover window. Flat prices so the ONLY PnL is the spread cost.
        base = _utc(2026, 1, 6, entry_hour)
        rows = []
        for k in range(8):
            rows.append((base + k * 900, 100.0, 100.0, 100.0, 100.0))
        entry_idx = len(rows)
        rows.append((base + entry_idx * 900, 100.0, 100.0, 100.0, 100.0))
        rows.append((base + (entry_idx + 1) * 900, 100.0, 100.0, 100.0, 100.0))
        rows.append((base + (entry_idx + 2) * 900, 100.0, 100.0, 100.0, 100.0))
        ohlcv = _mk_series(rows)
        n = len(rows)
        atrs = [1.0] * n
        decisions = [0] * n
        decisions[entry_idx] = 1
        spec = _StubSpec("EURUSD", sl_atr_mult=50.0, tp_atr_mult=50.0)
        return _StubStrategy(spec, decisions, atrs), ohlcv, warmup

    def _run(self, entry_hour, spread_model=None):
        strat, ohlcv, warmup = self._build(entry_hour)
        bt = {
            "fill_policy": "signal_close",
            "sizing": "fixed_lot",
            "slippage_points": 0,
            "commission_per_lot": 0.0,
            "spread_points": 10,
        }
        if spread_model is not None:
            bt["spread_model"] = spread_model
        return Backtester(_cfg(backtest=bt)).run(strat, ohlcv, warmup=warmup)

    def test_rollover_spread_costs_more(self):
        model = {
            "base_points": 10,
            "rollover_mult": 4.0,
            "rollover_hours_utc": [21, 23],
        }
        # The entry bar sits 8 bars (2 hours) after the base hour, so pick base
        # hours that land the ENTRY inside vs outside the [21,23] window.
        # base 6  -> entry at hour 8  (OUTSIDE) ; base 21 -> entry at hour 23 (IN).
        out_res = self._run(6, spread_model=model)
        in_res = self._run(21, spread_model=model)
        self.assertEqual(len(out_res.trade_pnls), 1)
        self.assertEqual(len(in_res.trade_pnls), 1)
        # Flat price: PnL is purely -spread_cost, so the in-window trade must be
        # MORE negative (cost more) than the out-of-window trade.
        self.assertLess(in_res.trade_pnls[0], out_res.trade_pnls[0])

    def test_flat_spread_when_no_model(self):
        # With no spread_model sub-block the spread is constant regardless of the
        # entry hour, so both trades cost exactly the same.
        a = self._run(6).trade_pnls[0]
        b = self._run(21).trade_pnls[0]
        self.assertAlmostEqual(a, b, places=6)


# --------------------------------------------------------------------------- #
# U3.4: risk_pct sizing respects the min/max lot clamp.
# --------------------------------------------------------------------------- #
class TestRiskPctSizingClamp(unittest.TestCase):
    """Computed lot is clamped into [min_lot, max_lot]."""

    def _sized(self, balance, entry, stop, risk):
        cfg = _cfg(backtest={"sizing": "risk_pct"}, risk=risk)
        bt = Backtester(cfg)
        return bt._sized_lot(balance, entry, stop, contract=100000.0)

    def test_clamped_to_max_lot(self):
        # Huge risk budget + tiny stop -> raw lot far above max -> clamp to max.
        risk = {"risk_per_trade": 0.5, "min_lot": 0.01, "max_lot": 0.50}
        lot = self._sized(10000.0, 100.0, 99.999, risk)
        self.assertAlmostEqual(lot, 0.50, places=6)

    def test_clamped_to_min_lot(self):
        # Tiny risk budget + huge stop -> raw lot far below min -> clamp to min.
        risk = {"risk_per_trade": 0.0001, "min_lot": 0.01, "max_lot": 0.50}
        lot = self._sized(10000.0, 100.0, 50.0, risk)
        self.assertAlmostEqual(lot, 0.01, places=6)

    def test_in_range_value_matches_formula(self):
        # risk_amount = 10000 * 0.01 = 100; stop_distance = 1.0;
        # loss_per_lot = 1.0 * 100000 = 100000; lot = 100 / 100000 = 0.001,
        # which is below min_lot 0.0001? No: 0.001 > 0.0001, so it stays.
        risk = {"risk_per_trade": 0.01, "min_lot": 0.0001, "max_lot": 10.0}
        lot = self._sized(10000.0, 100.0, 99.0, risk)
        self.assertAlmostEqual(lot, 100.0 / (1.0 * 100000.0), places=8)

    def test_fixed_lot_mode_ignores_risk(self):
        cfg = _cfg(backtest={"sizing": "fixed_lot", "fixed_lot": 0.07})
        bt = Backtester(cfg)
        lot = bt._sized_lot(10000.0, 100.0, 99.0, contract=100000.0)
        self.assertAlmostEqual(lot, 0.07, places=6)


# --------------------------------------------------------------------------- #
# U3.4: the max_daily_loss circuit breaker stops NEW entries.
# --------------------------------------------------------------------------- #
class TestDailyCircuitBreaker(unittest.TestCase):
    """After the day's realized loss crosses the limit, no new entries open."""

    def _build(self):
        # Three losing long trades all on the SAME UTC day. With a tight daily
        # loss limit, the breaker should trip after the first loss and block the
        # later entries, so fewer trades happen than without the breaker.
        warmup = 2
        base = _utc(2026, 1, 6, 6)
        rows = []
        for k in range(8):
            rows.append((base + k * 900, 100.0, 100.0, 100.0, 100.0))
        n_lead = len(rows)
        decisions = [0] * n_lead
        # Pattern: enter long, stop out, flat, enter long, stop out, ...
        # Each "enter" bar is flat @100; the following bar gaps down to stop out.
        for _ in range(3):
            ei = len(rows)
            rows.append((base + ei * 900, 100.0, 100.0, 100.0, 100.0))  # ENTER
            decisions.append(1)
            # Stop-out bar: drops through stop (98 with sl_atr_mult=2, atr=1).
            rows.append((base + (ei + 1) * 900, 100.0, 100.0, 96.0, 96.5))
            decisions.append(0)
            # Flat recovery bar so the next entry is a fresh position.
            rows.append((base + (ei + 2) * 900, 100.0, 100.0, 100.0, 100.0))
            decisions.append(0)
        ohlcv = _mk_series(rows)
        atrs = [1.0] * len(rows)
        spec = _StubSpec("EURUSD", sl_atr_mult=2.0, tp_atr_mult=100.0)
        return _StubStrategy(spec, decisions, atrs), ohlcv, warmup

    def _run(self, max_daily_loss):
        strat, ohlcv, warmup = self._build()
        cfg = _cfg(
            backtest={
                "fill_policy": "signal_close",
                "sizing": "risk_pct",
                "spread_points": 0,
                "slippage_points": 0,
            },
            risk={
                "risk_per_trade": 0.02,
                "min_lot": 0.01,
                "max_lot": 1.0,
                "max_daily_loss": max_daily_loss,
            },
        )
        return Backtester(cfg).run(strat, ohlcv, warmup=warmup)

    def test_breaker_reduces_trade_count(self):
        # No breaker (0.0 disables it) -> all three trades happen.
        no_breaker = self._run(0.0)
        # Tight breaker -> trips after the first loss, blocking later entries.
        with_breaker = self._run(0.01)
        self.assertEqual(len(no_breaker.trade_pnls), 3)
        self.assertLess(len(with_breaker.trade_pnls),
                        len(no_breaker.trade_pnls))
        self.assertGreaterEqual(len(with_breaker.trade_pnls), 1)


# --------------------------------------------------------------------------- #
# U3.5: broker minimum-stop-distance rejects too-tight entries.
# --------------------------------------------------------------------------- #
class TestMinStopDistance(unittest.TestCase):
    """An entry whose SL is closer than min_stop_points is never opened."""

    def _build(self):
        warmup = 2
        base = _utc(2026, 1, 6, 6)
        rows = []
        for k in range(8):
            rows.append((base + k * 900, 100.0, 100.0, 100.0, 100.0))
        entry_idx = len(rows)
        rows.append((base + entry_idx * 900, 100.0, 100.0, 100.0, 100.0))
        rows.append((base + (entry_idx + 1) * 900, 101.0, 101.5, 100.8, 101.2))
        rows.append((base + (entry_idx + 2) * 900, 101.5, 102.0, 101.3, 101.8))
        ohlcv = _mk_series(rows)
        n = len(rows)
        atrs = [1.0] * n
        decisions = [0] * n
        decisions[entry_idx] = 1
        # sl_atr_mult=2, atr=1 -> stop distance 2.0 price = 20000 points on FX
        # (point 0.0001). We'll set min_stop_points above/below that.
        spec = _StubSpec("EURUSD", sl_atr_mult=2.0, tp_atr_mult=50.0)
        return _StubStrategy(spec, decisions, atrs), ohlcv, warmup

    def _run(self, min_stop_points):
        strat, ohlcv, warmup = self._build()
        cfg = _cfg(backtest={
            "fill_policy": "signal_close",
            "sizing": "fixed_lot",
            "min_stop_points": min_stop_points,
        })
        return Backtester(cfg).run(strat, ohlcv, warmup=warmup)

    def test_disabled_by_default(self):
        # 0 (default) never rejects: the trade opens normally.
        res = self._run(0)
        self.assertEqual(len(res.trade_pnls), 1)

    def test_too_tight_is_rejected(self):
        # stop distance = 2.0 price / 0.0001 point = 20000 points. Require
        # 30000 -> the entry is rejected, no trade.
        res = self._run(30000)
        self.assertEqual(len(res.trade_pnls), 0)

    def test_wide_enough_is_accepted(self):
        # Require 10000 points (< the 20000-point stop distance) -> accepted.
        res = self._run(10000)
        self.assertEqual(len(res.trade_pnls), 1)


if __name__ == "__main__":
    unittest.main()
