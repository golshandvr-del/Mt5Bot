"""
Internal backtest engine (Phase 3).

This is a lightweight, bar-by-bar simulator used for strategy/parameter search
and walk-forward evaluation. It is NOT the MT5 Strategy Tester; it is the bot's
own fast offline evaluator so the search loop can score thousands of strategies
without launching the terminal. The README explains how to also validate the
final chosen strategy inside the real MT5 Strategy Tester on Windows.

Model
-----
- Single position at a time (long/short/flat) per symbol.
- Enter when the strategy decision changes to +1 (long) or -1 (short).
- Exit on opposite signal, on stop-loss, or on take-profit (ATR-based).
- Costs: spread + commission + slippage applied on entry and exit.
- Position size: fixed lot from config.backtest.fixed_lot.

PnL is computed in "price units * contract" approximated by treating one lot as
a fixed notional; because the search only needs RELATIVE ranking of strategies,
exact broker accounting is not required here. Final validation happens in MT5.

All text is pure Python; ASCII English only.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.strategy.strategy import Strategy, StrategySpec
from core.strategy.metrics import compute_metrics
from core.utils.helpers import symbol_offline_specs
from core.utils.logger import get_logger


class BacktestResult(object):
    """Holds the outcome of a single backtest run."""

    def __init__(self, metrics: Dict[str, Any], equity_curve: List[float],
                 trade_pnls: List[float],
                 trades: Optional[List[Dict[str, Any]]] = None):
        self.metrics = metrics
        self.equity_curve = equity_curve
        self.trade_pnls = trade_pnls
        # Phase 5 (user-update-request): optional per-trade records with the
        # entry-bar timestamp so the timing layer can attribute PnL to time
        # buckets (session/day/season).
        #
        # Phase U1.1 (transparency overhaul): when record_trades=True each item
        # now carries the FULL trade receipt so the user can audit any trade:
        #   entry_ts, exit_ts, direction (+1/-1), entry_price, exit_price,
        #   stop_price, take_price, exit_reason (sl/tp/flip/eod),
        #   pnl, gross_pnl, cost_spread, cost_slippage, cost_commission,
        #   cost_swap, balance_after, signal (blended signal value at entry).
        # The legacy keys (entry_ts, pnl, direction) are still present so the
        # existing timing layer keeps working unchanged. Empty unless
        # record_trades=True was requested.
        self.trades: List[Dict[str, Any]] = trades or []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metrics": self.metrics,
            "num_equity_points": len(self.equity_curve),
            "num_trades": len(self.trade_pnls),
        }


class Backtester(object):
    """Bar-by-bar single-position simulator."""

    def __init__(self, cfg: Any):
        self.cfg = cfg
        self.log = get_logger("strategy.backtester", cfg)
        bt = cfg.get("backtest", {})
        self.initial_balance = float(bt.get("initial_balance", 10000.0)) if hasattr(bt, "get") else 10000.0
        self.spread_points = float(bt.get("spread_points", 10)) if hasattr(bt, "get") else 10.0
        self.commission = float(bt.get("commission_per_lot", 7.0)) if hasattr(bt, "get") else 7.0
        self.slippage_points = float(bt.get("slippage_points", 2)) if hasattr(bt, "get") else 2.0
        self.fixed_lot = float(bt.get("fixed_lot", 0.10)) if hasattr(bt, "get") else 0.10
        # Notional per lot used to scale price moves into PnL (relative ranking).
        self.contract = 100000.0
        # Point size assumption when symbol info is unavailable.
        self.default_point = 0.0001
        # ------------------------------------------------------------------ #
        # Weekend / rollover swap + Monday gap model (A6 / P3.6). Defaults are
        # a no-op so the backtester behaves byte-identically when unset. Read
        # defensively (bad/missing values fall back to the safe defaults).
        # ------------------------------------------------------------------ #
        self.swap_long_pts = self._cfg_float(bt, "swap_long_pts", 0.0)
        self.swap_short_pts = self._cfg_float(bt, "swap_short_pts", 0.0)
        self.swap_triple_day = self._cfg_int(bt, "swap_triple_day", 2)
        self.model_weekend_gap = self._cfg_bool(bt, "model_weekend_gap", False)

    @staticmethod
    def _cfg_float(bt: Any, key: str, default: float) -> float:
        """Read a float from the backtest config block, safe on bad values."""
        try:
            if hasattr(bt, "get"):
                return float(bt.get(key, default))
        except Exception:
            pass
        return float(default)

    @staticmethod
    def _cfg_int(bt: Any, key: str, default: int) -> int:
        """Read an int from the backtest config block, safe on bad values."""
        try:
            if hasattr(bt, "get"):
                return int(bt.get(key, default))
        except Exception:
            pass
        return int(default)

    @staticmethod
    def _cfg_bool(bt: Any, key: str, default: bool) -> bool:
        """Read a bool from the backtest config block, safe on bad values."""
        if not hasattr(bt, "get"):
            return bool(default)
        val = bt.get(key, default)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.strip().lower() in ("1", "true", "yes", "on")
        try:
            return bool(val)
        except Exception:
            return bool(default)

    @staticmethod
    def _infer_bar_seconds(times: List[int], ohlcv: Any) -> int:
        """
        Estimate the normal per-bar spacing in seconds so a weekend gap (a much
        larger pause) can be told apart from a normal bar (A6 / P3.6). Prefers
        the timeframe helper for the OHLCV's timeframe; falls back to the most
        common positive delta between consecutive timestamps; 0 if unknown.
        """
        tf = getattr(ohlcv, "timeframe", "") or ""
        if tf:
            try:
                from core.utils.helpers import timeframe_seconds
                secs = int(timeframe_seconds(tf))
                if secs > 0:
                    return secs
            except Exception:
                pass
        # Fall back to the median-ish (most common) positive delta.
        deltas: Dict[int, int] = {}
        limit = min(len(times), 200)
        for i in range(1, limit):
            try:
                d = int(times[i]) - int(times[i - 1])
            except Exception:
                continue
            if d > 0:
                deltas[d] = deltas.get(d, 0) + 1
        if not deltas:
            return 0
        return max(deltas.items(), key=lambda kv: kv[1])[0]

    @staticmethod
    def _rollovers_between(prev_ts: int, cur_ts: int, triple_day: int) -> float:
        """
        Count the swap "nights" charged between two bar timestamps.

        A swap is charged for every UTC day boundary (midnight) crossed while a
        position is held. The weekday that carries the weekend (triple_day, MT5
        convention Wednesday=2) is charged 3x; every other rollover is 1x. If a
        span crosses several midnights (e.g. over a weekend) each one is
        counted. Returns 0.0 when no midnight is crossed or timestamps are bad.
        """
        try:
            prev_ts = int(prev_ts)
            cur_ts = int(cur_ts)
        except Exception:
            return 0.0
        if cur_ts <= prev_ts:
            return 0.0
        prev_day = prev_ts // 86400
        cur_day = cur_ts // 86400
        if cur_day <= prev_day:
            return 0.0
        total = 0.0
        # Each crossed midnight belongs to the day being ENTERED. Weekday of a
        # day index d (days since epoch, 1970-01-01 = Thursday = weekday 3).
        for d in range(int(prev_day) + 1, int(cur_day) + 1):
            weekday = (d + 3) % 7  # 0=Mon .. 6=Sun
            total += 3.0 if weekday == triple_day else 1.0
        return total

    def _round_trip_cost(self, point: float, contract: float) -> float:
        """Approximate per-trade cost in PnL units (spread+slippage+commission)."""
        pts = (self.spread_points + 2.0 * self.slippage_points) * point
        cost_price = pts * self.fixed_lot * contract
        commission = self.commission * self.fixed_lot * 2.0
        return cost_price + commission

    def _cost_breakdown(self, point: float, contract: float) -> Dict[str, float]:
        """
        Split the round-trip cost into its named components (U1.1 transparency).

        Returns a dict with separate spread / slippage / commission money
        amounts (round-trip, i.e. entry + exit). Their sum equals
        `_round_trip_cost` exactly so the per-trade CSV can attribute every
        dollar of cost to a source and still reconcile with the metrics.
        """
        spread_money = self.spread_points * point * self.fixed_lot * contract
        # Slippage is applied on BOTH entry and exit (2x), matching the
        # round-trip cost formula above.
        slippage_money = 2.0 * self.slippage_points * point * self.fixed_lot * contract
        commission_money = self.commission * self.fixed_lot * 2.0
        return {
            "spread": spread_money,
            "slippage": slippage_money,
            "commission": commission_money,
        }

    def _swap_money(self, direction: int, nights: float,
                    point: float, contract: float) -> float:
        """
        Money charged (positive) or credited (negative) for holding a position
        over `nights` rollovers (A6 / P3.6). Uses the long/short swap point
        rates converted to money via point * contract * lot. Returns 0.0 when
        swap is not modeled (both rates 0.0) so old behavior is preserved.
        """
        if nights <= 0.0:
            return 0.0
        pts = self.swap_long_pts if direction == 1 else self.swap_short_pts
        if pts == 0.0:
            return 0.0
        return pts * point * contract * self.fixed_lot * nights

    @staticmethod
    def _make_trade_record(entry_ts: int, exit_ts: int, direction: int,
                           entry_price: float, exit_price: float,
                           stop_price: float, take_price: float,
                           exit_reason: str, pnl: float, gross_pnl: float,
                           cost_parts: Dict[str, float], swap: float,
                           balance_after: float,
                           signal: float) -> Dict[str, Any]:
        """
        Build one FULL per-trade receipt (U1.1 transparency).

        The dict keeps the three legacy keys (entry_ts, pnl, direction) that the
        Phase 5 timing layer already reads, and adds the full audit fields. The
        cost components (spread/slippage/commission) plus swap are broken out
        separately so a report can show "how much of my PnL went to costs".
        By construction: pnl == gross_pnl - (cost_spread + cost_slippage +
        cost_commission) - cost_swap.
        """
        return {
            # --- legacy keys (do not remove: timing layer depends on them) ---
            "entry_ts": int(entry_ts),
            "pnl": float(pnl),
            "direction": int(direction),
            # --- U1.1 full receipt ---
            "exit_ts": int(exit_ts),
            "entry_price": float(entry_price),
            "exit_price": float(exit_price),
            "stop_price": float(stop_price),
            "take_price": float(take_price),
            "exit_reason": str(exit_reason),
            "gross_pnl": float(gross_pnl),
            "cost_spread": float(cost_parts.get("spread", 0.0)),
            "cost_slippage": float(cost_parts.get("slippage", 0.0)),
            "cost_commission": float(cost_parts.get("commission", 0.0)),
            "cost_swap": float(swap),
            "balance_after": float(balance_after),
            "signal": float(signal),
        }

    def run(self, strategy: Strategy, ohlcv: Any,
            warmup: int = 60, point: Optional[float] = None,
            contract: Optional[float] = None,
            record_trades: bool = False) -> BacktestResult:
        """
        Run the backtest over the OHLCV series.

        warmup   : number of leading bars skipped so indicators are stable.
        point    : symbol point size. If not given it is inferred from the
                   strategy/ohlcv symbol (FX 0.0001, JPY/metals 0.01).
        contract : notional units per 1.0 lot. If not given it is inferred from
                   the symbol (FX/JPY 100000, metals 100). Making these
                   symbol-aware keeps reported PnL sensible per instrument;
                   RELATIVE ranking across strategies is unaffected.
        record_trades : when True, also collect per-trade records carrying the
                   ENTRY-bar timestamp and PnL so the Phase 5 timing layer can
                   attribute outcomes to time buckets. Kept optional so the hot
                   search loop stays as light as possible when not needed.
        """
        # Infer symbol specs so gold/JPY do not report absurd PnL magnitudes.
        symbol = getattr(strategy.spec, "symbol", "") or getattr(ohlcv, "symbol", "")
        specs = symbol_offline_specs(symbol)
        if point is None:
            point = specs["point"]
        if contract is None:
            contract = specs["contract"]
        self.contract = float(contract)

        close = ohlcv.close
        high = ohlcv.high
        low = ohlcv.low
        times = getattr(ohlcv, "time", None) or []
        n = len(close)
        if n <= warmup + 5:
            return BacktestResult(compute_metrics([], [self.initial_balance]),
                                  [self.initial_balance], [], [])

        # -------------------------------------------------------------- #
        # PERFORMANCE: precompute the full per-bar decision and ATR series
        # ONCE. Previously this loop rebuilt a growing OHLCV slice and
        # recomputed every indicator from scratch on each bar, which was
        # O(n^2) per indicator and far too slow on the target Windows 7
        # hardware. Now it is a single O(n) pass. The per-bar decision is
        # computed only from data up to and including bar i (no lookahead),
        # so this is numerically equivalent to the old growing-window loop.
        # -------------------------------------------------------------- #
        decision_series = strategy.decision_series(ohlcv)
        atr_series = strategy.atr_series(ohlcv)
        # U1.1: the blended signal value AT ENTRY is part of the trade receipt.
        # Stub strategies used in tests may not implement signal_series(); read
        # it defensively so those keep working (signal recorded as 0.0 then).
        signal_series: List[float] = []
        if record_trades:
            try:
                signal_series = list(strategy.signal_series(ohlcv))
            except Exception:
                signal_series = []

        balance = self.initial_balance
        equity_curve: List[float] = [balance]
        trade_pnls: List[float] = []
        trades: List[Dict[str, Any]] = []

        position = 0          # 0 flat, +1 long, -1 short
        entry_price = 0.0
        entry_ts = 0          # entry-bar timestamp (for the timing layer)
        entry_signal = 0.0    # blended signal value at entry (U1.1 receipt)
        stop_price = 0.0
        take_price = 0.0
        swap_accum = 0.0      # money charged so far for holding this position
        cost = self._round_trip_cost(point, self.contract)
        # U1.1: the named cost components (spread/slippage/commission) so each
        # trade receipt can attribute every dollar of cost. Their sum == cost.
        cost_parts = self._cost_breakdown(point, self.contract)
        # Detect a weekend/holiday gap: a bar whose gap from the previous bar is
        # noticeably larger than one normal bar spacing (A6 / P3.6). Only used
        # when model_weekend_gap is on; otherwise stops fill exactly at the stop.
        tf_seconds = self._infer_bar_seconds(times, ohlcv)
        gap_threshold = tf_seconds * 3 if tf_seconds > 0 else 0

        for i in range(warmup, n):
            decision = decision_series[i] if i < len(decision_series) else 0
            atr = atr_series[i] if (i < len(atr_series) and atr_series[i]) else None
            if atr is None:
                atr = close[i] * 0.001

            cur_ts = times[i] if i < len(times) else 0
            prev_ts = times[i - 1] if 0 < i <= len(times) else 0
            # A gap bar is one that opens after an unusually long pause (weekend).
            is_gap_bar = (
                self.model_weekend_gap and gap_threshold > 0
                and prev_ts and cur_ts and (cur_ts - prev_ts) > gap_threshold
            )

            # Accrue overnight swap for every rollover crossed while holding.
            if position != 0 and prev_ts and cur_ts:
                nights = self._rollovers_between(prev_ts, cur_ts,
                                                 self.swap_triple_day)
                if nights > 0.0:
                    swap_accum += self._swap_money(position, nights,
                                                   point, self.contract)

            # Manage an open position first (check SL/TP using this bar range).
            if position != 0:
                exit_now = False
                exit_price = close[i]
                exit_reason = ""     # "sl" / "tp" / "flip" (U1.1 receipt)
                if position == 1:
                    if low[i] <= stop_price:
                        # Monday-gap model: if the bar OPENS below the stop, the
                        # stop fills at the (worse) open, not at the stop price.
                        if is_gap_bar and ohlcv.open[i] < stop_price:
                            exit_price = ohlcv.open[i]
                        else:
                            exit_price = stop_price
                        exit_now = True
                        exit_reason = "sl"
                    elif high[i] >= take_price:
                        exit_price = take_price
                        exit_now = True
                        exit_reason = "tp"
                    elif decision == -1:
                        exit_now = True
                        exit_reason = "flip"
                else:  # short
                    if high[i] >= stop_price:
                        # For a short, a gap UP through the stop fills worse (at
                        # the higher open) than the stop price.
                        if is_gap_bar and ohlcv.open[i] > stop_price:
                            exit_price = ohlcv.open[i]
                        else:
                            exit_price = stop_price
                        exit_now = True
                        exit_reason = "sl"
                    elif low[i] <= take_price:
                        exit_price = take_price
                        exit_now = True
                        exit_reason = "tp"
                    elif decision == 1:
                        exit_now = True
                        exit_reason = "flip"

                if exit_now:
                    move = (exit_price - entry_price) * position
                    gross_pnl = move * self.fixed_lot * self.contract
                    pnl = gross_pnl - cost - swap_accum
                    balance += pnl
                    trade_pnls.append(pnl)
                    equity_curve.append(balance)
                    if record_trades:
                        trades.append(self._make_trade_record(
                            entry_ts=entry_ts,
                            exit_ts=cur_ts,
                            direction=position,
                            entry_price=entry_price,
                            exit_price=exit_price,
                            stop_price=stop_price,
                            take_price=take_price,
                            exit_reason=exit_reason,
                            pnl=pnl,
                            gross_pnl=gross_pnl,
                            cost_parts=cost_parts,
                            swap=swap_accum,
                            balance_after=balance,
                            signal=entry_signal,
                        ))
                    position = 0
                    swap_accum = 0.0

            # Enter a new position if flat and a directional decision appears.
            if position == 0 and decision != 0:
                position = decision
                entry_price = close[i]
                entry_ts = times[i] if i < len(times) else 0
                entry_signal = (signal_series[i]
                                if i < len(signal_series) else 0.0)
                swap_accum = 0.0
                if position == 1:
                    stop_price = entry_price - strategy.spec.sl_atr_mult * atr
                    take_price = entry_price + strategy.spec.tp_atr_mult * atr
                else:
                    stop_price = entry_price + strategy.spec.sl_atr_mult * atr
                    take_price = entry_price - strategy.spec.tp_atr_mult * atr

        # Close any residual position at the last close.
        if position != 0:
            move = (close[-1] - entry_price) * position
            gross_pnl = move * self.fixed_lot * self.contract
            pnl = gross_pnl - cost - swap_accum
            balance += pnl
            trade_pnls.append(pnl)
            equity_curve.append(balance)
            if record_trades:
                last_ts = times[-1] if times else 0
                trades.append(self._make_trade_record(
                    entry_ts=entry_ts,
                    exit_ts=last_ts,
                    direction=position,
                    entry_price=entry_price,
                    exit_price=close[-1],
                    stop_price=stop_price,
                    take_price=take_price,
                    exit_reason="eod",
                    pnl=pnl,
                    gross_pnl=gross_pnl,
                    cost_parts=cost_parts,
                    swap=swap_accum,
                    balance_after=balance,
                    signal=entry_signal,
                ))

        metrics = compute_metrics(trade_pnls, equity_curve)
        return BacktestResult(metrics, equity_curve, trade_pnls, trades)
