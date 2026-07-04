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
                 trade_pnls: List[float]):
        self.metrics = metrics
        self.equity_curve = equity_curve
        self.trade_pnls = trade_pnls

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

    def _round_trip_cost(self, point: float, contract: float) -> float:
        """Approximate per-trade cost in PnL units (spread+slippage+commission)."""
        pts = (self.spread_points + 2.0 * self.slippage_points) * point
        cost_price = pts * self.fixed_lot * contract
        commission = self.commission * self.fixed_lot * 2.0
        return cost_price + commission

    def run(self, strategy: Strategy, ohlcv: Any,
            warmup: int = 60, point: Optional[float] = None,
            contract: Optional[float] = None) -> BacktestResult:
        """
        Run the backtest over the OHLCV series.

        warmup   : number of leading bars skipped so indicators are stable.
        point    : symbol point size. If not given it is inferred from the
                   strategy/ohlcv symbol (FX 0.0001, JPY/metals 0.01).
        contract : notional units per 1.0 lot. If not given it is inferred from
                   the symbol (FX/JPY 100000, metals 100). Making these
                   symbol-aware keeps reported PnL sensible per instrument;
                   RELATIVE ranking across strategies is unaffected.
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
        n = len(close)
        if n <= warmup + 5:
            return BacktestResult(compute_metrics([], [self.initial_balance]),
                                  [self.initial_balance], [])

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

        balance = self.initial_balance
        equity_curve: List[float] = [balance]
        trade_pnls: List[float] = []

        position = 0          # 0 flat, +1 long, -1 short
        entry_price = 0.0
        stop_price = 0.0
        take_price = 0.0
        cost = self._round_trip_cost(point, self.contract)

        for i in range(warmup, n):
            decision = decision_series[i] if i < len(decision_series) else 0
            atr = atr_series[i] if (i < len(atr_series) and atr_series[i]) else None
            if atr is None:
                atr = close[i] * 0.001

            # Manage an open position first (check SL/TP using this bar range).
            if position != 0:
                exit_now = False
                exit_price = close[i]
                if position == 1:
                    if low[i] <= stop_price:
                        exit_price = stop_price
                        exit_now = True
                    elif high[i] >= take_price:
                        exit_price = take_price
                        exit_now = True
                    elif decision == -1:
                        exit_now = True
                else:  # short
                    if high[i] >= stop_price:
                        exit_price = stop_price
                        exit_now = True
                    elif low[i] <= take_price:
                        exit_price = take_price
                        exit_now = True
                    elif decision == 1:
                        exit_now = True

                if exit_now:
                    move = (exit_price - entry_price) * position
                    pnl = move * self.fixed_lot * self.contract - cost
                    balance += pnl
                    trade_pnls.append(pnl)
                    equity_curve.append(balance)
                    position = 0

            # Enter a new position if flat and a directional decision appears.
            if position == 0 and decision != 0:
                position = decision
                entry_price = close[i]
                if position == 1:
                    stop_price = entry_price - strategy.spec.sl_atr_mult * atr
                    take_price = entry_price + strategy.spec.tp_atr_mult * atr
                else:
                    stop_price = entry_price + strategy.spec.sl_atr_mult * atr
                    take_price = entry_price - strategy.spec.tp_atr_mult * atr

        # Close any residual position at the last close.
        if position != 0:
            move = (close[-1] - entry_price) * position
            pnl = move * self.fixed_lot * self.contract - cost
            balance += pnl
            trade_pnls.append(pnl)
            equity_curve.append(balance)

        metrics = compute_metrics(trade_pnls, equity_curve)
        return BacktestResult(metrics, equity_curve, trade_pnls)
