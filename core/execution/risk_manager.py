"""
Risk manager (execution layer).

Responsibilities
----------------
- Compute a position size (lot) from account equity, per-trade risk fraction,
  the stop-loss distance in price, and the symbol's contract/point specs.
- Enforce hard limits from config.risk:
    * max_open_positions
    * max_daily_loss (as a fraction of the day's starting equity)
    * min_lot / max_lot clamping and volume_step rounding
- Track the day's realized starting equity so daily-loss can be evaluated even
  without a live account (paper mode uses the configured initial balance).

The sizing math is broker-aware when symbol info is available (point,
trade_contract_size, volume_step). When offline it uses safe FX defaults so the
same code path still produces a sane lot for paper trading and backtests.

All text is standard ASCII English only.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from core.utils.logger import get_logger


class RiskManager(object):
    """Position sizing and risk-limit enforcement."""

    def __init__(self, cfg: Any, connector: Optional[object] = None):
        self.cfg = cfg
        self.log = get_logger("execution.risk_manager", cfg)
        self.connector = connector

        risk = cfg.get("risk", {})
        self.risk_per_trade = float(risk.get("risk_per_trade", 0.01)) if hasattr(risk, "get") else 0.01
        self.max_open_positions = int(risk.get("max_open_positions", 3)) if hasattr(risk, "get") else 3
        self.max_daily_loss = float(risk.get("max_daily_loss", 0.05)) if hasattr(risk, "get") else 0.05
        self.min_lot = float(risk.get("min_lot", 0.01)) if hasattr(risk, "get") else 0.01
        self.max_lot = float(risk.get("max_lot", 1.0)) if hasattr(risk, "get") else 1.0

        # Daily loss tracking.
        self._day_key = self._today_key()
        self._day_start_equity = self._current_equity()
        self._realized_today = 0.0

    # ------------------------------------------------------------------ #
    @staticmethod
    def _today_key() -> str:
        return time.strftime("%Y-%m-%d", time.gmtime())

    def _current_equity(self) -> float:
        """Return live equity if connected, else the backtest initial balance."""
        if self.connector is not None and getattr(self.connector, "connected", False):
            info = self.connector.account_info()
            eq = info.get("equity")
            if eq:
                return float(eq)
        return float(self.cfg.get_path("backtest.initial_balance", 10000.0))

    def _roll_day_if_needed(self) -> None:
        key = self._today_key()
        if key != self._day_key:
            self._day_key = key
            self._day_start_equity = self._current_equity()
            self._realized_today = 0.0
            self.log.info("New trading day: start equity=%.2f", self._day_start_equity)

    # ------------------------------------------------------------------ #
    def register_realized_pnl(self, pnl: float) -> None:
        """Record a closed-trade PnL so daily-loss tracking stays accurate."""
        self._roll_day_if_needed()
        self._realized_today += float(pnl)

    def daily_loss_breached(self) -> bool:
        """True if today's realized loss exceeds max_daily_loss * start equity."""
        self._roll_day_if_needed()
        if self._day_start_equity <= 0:
            return False
        loss_limit = self.max_daily_loss * self._day_start_equity
        # Loss is a negative realized PnL.
        return (-self._realized_today) >= loss_limit

    def can_open_new(self, open_positions_count: int) -> bool:
        """Check position-count and daily-loss limits before a new entry."""
        if self.daily_loss_breached():
            self.log.warning("Daily loss limit reached; blocking new trades.")
            return False
        if open_positions_count >= self.max_open_positions:
            self.log.info(
                "Max open positions (%d) reached; blocking new trades.",
                self.max_open_positions,
            )
            return False
        return True

    # ------------------------------------------------------------------ #
    def _symbol_specs(self, symbol: str) -> Dict[str, Any]:
        """Return point, contract size, and volume step for a symbol."""
        point = 0.0001
        contract = 100000.0
        vol_step = 0.01
        if self.connector is not None and getattr(self.connector, "connected", False):
            info = self.connector.symbol_info(symbol)
            if info:
                point = float(info.get("point") or point)
                contract = float(info.get("trade_contract_size") or contract)
                vol_step = float(info.get("volume_step") or vol_step)
        else:
            # Reasonable offline defaults per instrument class.
            up = symbol.upper()
            if up.endswith("JPY"):
                point = 0.01
            elif up.startswith("XAU"):
                point = 0.01
                contract = 100.0
        return {"point": point, "contract": contract, "volume_step": vol_step}

    def _round_to_step(self, lot: float, step: float) -> float:
        if step <= 0:
            return lot
        steps = round(lot / step)
        return steps * step

    def position_size(self, symbol: str, entry_price: float,
                      stop_price: float, size_hint: float = 1.0) -> float:
        """
        Compute a lot size so that hitting the stop loses about
        risk_per_trade * equity. size_hint in [0,1] scales the risk down for
        low-confidence signals. Result is clamped and step-rounded.
        """
        self._roll_day_if_needed()
        equity = self._current_equity()
        risk_amount = equity * self.risk_per_trade * max(0.0, min(1.0, size_hint or 1.0))
        if risk_amount <= 0:
            return 0.0

        stop_distance = abs(entry_price - stop_price)
        if stop_distance <= 0:
            # Without a valid stop distance we cannot size by risk; use min lot.
            return self.min_lot

        specs = self._symbol_specs(symbol)
        contract = specs["contract"]

        # Money lost per 1.0 lot if price moves by stop_distance:
        #   loss_per_lot = stop_distance * contract_size
        loss_per_lot = stop_distance * contract
        if loss_per_lot <= 0:
            return self.min_lot

        lot = risk_amount / loss_per_lot
        lot = self._round_to_step(lot, specs["volume_step"])
        lot = max(self.min_lot, min(self.max_lot, lot))
        self.log.info(
            "Sized %s: equity=%.2f risk=%.2f stop_dist=%.5f -> lot=%.2f",
            symbol, equity, risk_amount, stop_distance, lot,
        )
        return lot
