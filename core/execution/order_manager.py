"""
Order manager (execution layer).

Converts a Decision (from the decision engine) plus a live tick into an actual
MT5 order request, respecting the RiskManager sizing and limits. Supports:
  - "paper" mode: computes SL/TP/lot and LOGS the intended order, but never
    sends it. Safe for testing signal quality against a live feed.
  - "live" mode: sends the order via the MT5Connector.

It also provides helpers to read current open positions (for the bot's own
magic number) and to close a position.

SL/TP are derived from the Decision's ATR multiples and the strategy/engine's
latest ATR, translated into absolute prices around the current bid/ask.

All text is standard ASCII English only.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.execution.risk_manager import RiskManager
from core.utils.logger import get_logger


class OrderManager(object):
    """Places and manages orders based on decisions and risk sizing."""

    def __init__(self, cfg: Any, connector: Optional[object] = None,
                 risk_manager: Optional[RiskManager] = None,
                 memory: Optional[object] = None):
        self.cfg = cfg
        self.log = get_logger("execution.order_manager", cfg)
        self.connector = connector
        self.risk = risk_manager or RiskManager(cfg, connector)
        self.mode = cfg.get_path("general.mode", "paper")
        self.magic = int(cfg.get_path("risk.magic_number", 990011))
        self.deviation = int(cfg.get_path("risk.deviation_points", 20))
        # Phase 5 / P5.6 (Track B / B3): optional memory store used to CAPTURE
        # realized live/paper PnL per strategy so the decay monitor can detect
        # statistical expiry. Only recorded when decision.decay_monitor.enabled
        # is true; otherwise record_trade_result is a no-op and the light path
        # is byte-for-byte unchanged.
        self.memory = memory
        self.decay_enabled = bool(
            cfg.get_path("decision.decay_monitor.enabled", False)
        )

    # ------------------------------------------------------------------ #
    def open_positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return this bot's open positions (filtered by magic number)."""
        if self.connector is None:
            return []
        try:
            positions = self.connector.positions(symbol)
        except Exception:
            positions = []
        return [p for p in positions if p.get("magic") == self.magic]

    # ------------------------------------------------------------------ #
    def _current_prices(self, symbol: str, fallback_close: float) -> Dict[str, float]:
        """Return {bid, ask}. Falls back to the last close if offline."""
        bid = fallback_close
        ask = fallback_close
        if self.connector is not None and getattr(self.connector, "connected", False):
            tick = self.connector.symbol_tick(symbol)
            if tick:
                bid = float(tick.get("bid") or fallback_close)
                ask = float(tick.get("ask") or fallback_close)
        return {"bid": bid, "ask": ask}

    def _compute_levels(self, action: int, entry: float, atr: float,
                        sl_mult: float, tp_mult: float) -> Dict[str, float]:
        """Compute absolute stop-loss and take-profit prices."""
        if action == 1:  # long
            stop = entry - sl_mult * atr
            take = entry + tp_mult * atr
        else:            # short
            stop = entry + sl_mult * atr
            take = entry - tp_mult * atr
        return {"stop": stop, "take": take}

    # ------------------------------------------------------------------ #
    def execute(self, decision: Any, symbol: str, atr: float,
                last_close: float) -> Dict[str, Any]:
        """
        Turn a Decision into an order (or a logged paper order).

        Parameters
        ----------
        decision   : Decision object (action in {-1,0,+1}, sl/tp multiples...).
        symbol     : trading symbol.
        atr        : latest ATR value used to place SL/TP.
        last_close : latest close price, used as an offline fallback for prices.

        Returns a result dict describing what was done.
        """
        action = int(getattr(decision, "action", 0))
        if action == 0:
            return {"ok": True, "action": "flat", "reason": "no signal"}

        # Respect risk limits before opening anything new.
        open_now = self.open_positions(symbol)
        if not self.risk.can_open_new(len(self.open_positions())):
            return {"ok": False, "action": "blocked", "reason": "risk_limit"}

        # Avoid stacking a same-direction position on the same symbol.
        for p in open_now:
            p_dir = 1 if p.get("type") == 0 else -1  # 0=buy,1=sell in MT5
            if p_dir == action:
                return {"ok": True, "action": "hold",
                        "reason": "already in position"}

        prices = self._current_prices(symbol, last_close)
        entry = prices["ask"] if action == 1 else prices["bid"]
        if not atr or atr <= 0:
            atr = max(1e-6, last_close * 0.001)

        levels = self._compute_levels(
            action, entry, atr,
            getattr(decision, "sl_atr_mult", 2.0),
            getattr(decision, "tp_atr_mult", 3.0),
        )
        lot = self.risk.position_size(
            symbol, entry, levels["stop"],
            size_hint=getattr(decision, "size_hint", 1.0),
        )
        if lot <= 0:
            return {"ok": False, "action": "skip", "reason": "zero lot"}

        order_plan = {
            "symbol": symbol,
            "direction": "buy" if action == 1 else "sell",
            "lot": lot,
            "entry": round(entry, 6),
            "sl": round(levels["stop"], 6),
            "tp": round(levels["take"], 6),
            "score": round(getattr(decision, "score", 0.0), 4),
        }

        # Paper mode: log the intended order and stop here.
        if self.mode != "live":
            self.log.info("PAPER ORDER: %s", order_plan)
            return {"ok": True, "action": "paper", "order": order_plan}

        # Live mode: build and send the MT5 order request.
        if self.connector is None or not getattr(self.connector, "connected", False):
            self.log.error("Live mode but MT5 not connected; order skipped.")
            return {"ok": False, "action": "error", "reason": "not connected"}

        try:
            order_type = (self.connector.ORDER_TYPE_BUY if action == 1
                          else self.connector.ORDER_TYPE_SELL)
            request = {
                "action": self.connector.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": float(lot),
                "type": order_type,
                "price": float(entry),
                "sl": float(levels["stop"]),
                "tp": float(levels["take"]),
                "deviation": self.deviation,
                "magic": self.magic,
                "comment": "MT5SmartBot",
                # type_filling/type_time are broker dependent; MT5 fills sane
                # defaults when omitted on most brokers.
            }
            result = self.connector.order_send(request)
            self.log.info("LIVE ORDER result: %s | plan=%s", result, order_plan)
            return {"ok": bool(result.get("ok")), "action": "live",
                    "order": order_plan, "result": result}
        except Exception as exc:
            self.log.error("order_send failed: %s", exc)
            return {"ok": False, "action": "error", "reason": str(exc)}

    # ------------------------------------------------------------------ #
    def record_trade_result(self, fingerprint: Optional[str],
                            pnl: Optional[float]) -> None:
        """
        Phase 5 / P5.6 (Track B / B3): capture one realized trade's PnL for the
        strategy that produced it, so the decay monitor can compare the recent
        live distribution against the walk-forward reference.

        No-op unless decision.decay_monitor.enabled is true AND a memory store
        and a non-empty fingerprint are available. Degrades gracefully so a
        capture failure never affects order execution.
        """
        if not self.decay_enabled or self.memory is None:
            return
        if not fingerprint or pnl is None:
            return
        try:
            self.memory.record_live_trade(str(fingerprint), float(pnl))
        except Exception as exc:
            self.log.error("record_trade_result failed: %s", exc)

    def close_position(self, position: Dict[str, Any],
                       fingerprint: Optional[str] = None) -> Dict[str, Any]:
        """
        Close a single open position (live mode only).

        When ``fingerprint`` is supplied and the decay monitor is enabled, the
        position's realized ``profit`` is captured via record_trade_result so the
        monitor can track that strategy's live PnL distribution.
        """
        if self.mode != "live" or self.connector is None:
            self.log.info("PAPER CLOSE: %s", position)
            self.record_trade_result(fingerprint, position.get("profit"))
            return {"ok": True, "action": "paper_close"}
        try:
            symbol = position.get("symbol")
            volume = float(position.get("volume") or 0.0)
            ticket = position.get("ticket")
            # Opposite side to close.
            is_buy = position.get("type") == 0
            close_type = (self.connector.ORDER_TYPE_SELL if is_buy
                          else self.connector.ORDER_TYPE_BUY)
            tick = self.connector.symbol_tick(symbol)
            price = float(tick.get("bid") if is_buy else tick.get("ask")) if tick else 0.0
            request = {
                "action": self.connector.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": volume,
                "type": close_type,
                "position": ticket,
                "price": price,
                "deviation": self.deviation,
                "magic": self.magic,
                "comment": "MT5SmartBot close",
            }
            result = self.connector.order_send(request)
            self.log.info("LIVE CLOSE result: %s", result)
            # Capture realized PnL for the decay monitor (config-gated no-op
            # otherwise). Uses the broker-reported profit on the position.
            self.record_trade_result(fingerprint, position.get("profit"))
            return {"ok": bool(result.get("ok")), "result": result}
        except Exception as exc:
            self.log.error("close_position failed: %s", exc)
            return {"ok": False, "reason": str(exc)}
