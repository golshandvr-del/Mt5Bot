"""
MT5 connector: a thin, defensive wrapper around the MetaTrader5 Python package.

Design goals
------------
- Work on Windows where the real MetaTrader5 package is installed.
- Degrade gracefully on machines (e.g. this Linux dev box) where the package
  is missing: every method then returns safe, empty results and logs a clear
  message, so the rest of the bot can still be imported, unit-built, and
  backtested using CSV data.
- Never raise on import. Only raise on explicit connect() if the caller
  insists (raise_on_fail=True).

This file belongs to the EXECUTION/DATA layer. Strategy/learning code should
never import MetaTrader5 directly; it should go through this connector or
through the OHLCV abstraction in data_feed.py.

All text is standard ASCII English only.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.utils.logger import get_logger
from core.utils.helpers import timeframe_to_mt5


def _import_mt5():
    """Import MetaTrader5 if available; return the module or None."""
    try:
        import MetaTrader5 as mt5  # type: ignore
        return mt5
    except Exception:
        return None


class MT5Connector:
    """
    Manages the connection to a MetaTrader 5 terminal and exposes the few
    operations the bot needs: initialize, account info, symbol info, copy
    rates, ticks, order send, positions.
    """

    def __init__(self, cfg: Any):
        self.cfg = cfg
        self.log = get_logger("data.mt5_connector", cfg)
        self.mt5 = _import_mt5()
        self.connected = False
        if self.mt5 is None:
            self.log.warning(
                "MetaTrader5 package not available. Connector runs in "
                "OFFLINE mode (no live data/orders). This is expected on "
                "non-Windows machines or before install."
            )

    # ------------------------------------------------------------------ #
    # Connection lifecycle
    # ------------------------------------------------------------------ #
    def connect(self, raise_on_fail: bool = False) -> bool:
        """
        Initialize the terminal connection using config/config.yaml mt5 block.

        Returns True on success. If the package is missing or init fails,
        returns False (or raises if raise_on_fail=True).
        """
        if self.mt5 is None:
            msg = "Cannot connect: MetaTrader5 package not installed."
            self.log.error(msg)
            if raise_on_fail:
                raise RuntimeError(msg)
            return False

        mt5_cfg = self.cfg.get("mt5", {})
        kwargs: Dict[str, Any] = {}
        path = mt5_cfg.get("terminal_path", "")
        if path:
            kwargs["path"] = path
        timeout = int(mt5_cfg.get("connect_timeout_ms", 60000))
        kwargs["timeout"] = timeout

        login = int(mt5_cfg.get("login", 0) or 0)
        password = mt5_cfg.get("password", "") or ""
        server = mt5_cfg.get("server", "") or ""

        try:
            ok = self.mt5.initialize(**kwargs)
            if not ok:
                last = self.mt5.last_error()
                self.log.error("mt5.initialize failed: %s", last)
                # (-10005, 'IPC timeout') is by far the most common cause and it
                # is almost always an environment issue, not a bug. Emit a short,
                # actionable checklist so the user knows exactly what to do.
                code = None
                try:
                    code = last[0] if isinstance(last, (tuple, list)) else None
                except Exception:
                    code = None
                if code == -10005 or "ipc timeout" in str(last).lower():
                    self.log.warning(
                        "MT5 IPC timeout. This is an environment issue, not a "
                        "code bug. Checklist: (1) the MetaTrader 5 *terminal* "
                        "(terminal64.exe) must be OPEN and logged in to an "
                        "account BEFORE running the bot; (2) enable "
                        "Tools > Options > Expert Advisors > 'Allow algorithmic "
                        "trading'; (3) if you run several terminals, set "
                        "mt5.terminal_path in config.yaml to the exact "
                        "terminal64.exe you want; (4) run the bot with the same "
                        "Windows user/bitness (64-bit) as the terminal. NOTE: "
                        "'--mode train', 'search', 'rebuild-registry' and "
                        "'backtest' do NOT need MT5 at all - they run entirely "
                        "on the CSV files in data_store/history."
                    )
                if raise_on_fail:
                    raise RuntimeError("mt5.initialize failed")
                return False

            # Optional explicit login if credentials are supplied.
            if login and password and server:
                logged = self.mt5.login(login, password=password, server=server)
                if not logged:
                    self.log.error("mt5.login failed: %s", self.mt5.last_error())
                    if raise_on_fail:
                        raise RuntimeError("mt5.login failed")
                    return False

            self.connected = True
            info = self.account_info()
            self.log.info(
                "Connected to MT5. Account=%s Server=%s Balance=%s",
                info.get("login"), info.get("server"), info.get("balance"),
            )
            return True
        except Exception as exc:
            self.log.error("Exception during connect: %s", exc)
            if raise_on_fail:
                raise
            return False

    def shutdown(self) -> None:
        """Cleanly close the terminal connection."""
        if self.mt5 is not None and self.connected:
            try:
                self.mt5.shutdown()
            except Exception:
                pass
        self.connected = False

    # ------------------------------------------------------------------ #
    # Account / symbol info
    # ------------------------------------------------------------------ #
    def account_info(self) -> Dict[str, Any]:
        """Return a dict of key account fields (empty dict if offline)."""
        if self.mt5 is None or not self.connected:
            return {}
        try:
            info = self.mt5.account_info()
            if info is None:
                return {}
            return {
                "login": getattr(info, "login", None),
                "server": getattr(info, "server", None),
                "balance": getattr(info, "balance", None),
                "equity": getattr(info, "equity", None),
                "margin": getattr(info, "margin", None),
                "currency": getattr(info, "currency", None),
                "leverage": getattr(info, "leverage", None),
            }
        except Exception as exc:
            self.log.error("account_info error: %s", exc)
            return {}

    def symbol_info(self, symbol: str) -> Dict[str, Any]:
        """Return key symbol fields needed for sizing (empty if offline)."""
        if self.mt5 is None or not self.connected:
            return {}
        try:
            # Ensure the symbol is selected in Market Watch.
            self.mt5.symbol_select(symbol, True)
            info = self.mt5.symbol_info(symbol)
            if info is None:
                return {}
            return {
                "name": getattr(info, "name", symbol),
                "point": getattr(info, "point", None),
                "digits": getattr(info, "digits", None),
                "volume_min": getattr(info, "volume_min", None),
                "volume_max": getattr(info, "volume_max", None),
                "volume_step": getattr(info, "volume_step", None),
                "trade_contract_size": getattr(info, "trade_contract_size", None),
                "spread": getattr(info, "spread", None),
            }
        except Exception as exc:
            self.log.error("symbol_info error for %s: %s", symbol, exc)
            return {}

    def symbol_tick(self, symbol: str) -> Dict[str, Any]:
        """Return latest bid/ask tick (empty if offline)."""
        if self.mt5 is None or not self.connected:
            return {}
        try:
            tick = self.mt5.symbol_info_tick(symbol)
            if tick is None:
                return {}
            return {
                "bid": getattr(tick, "bid", None),
                "ask": getattr(tick, "ask", None),
                "last": getattr(tick, "last", None),
                "time": getattr(tick, "time", None),
            }
        except Exception as exc:
            self.log.error("symbol_tick error for %s: %s", symbol, exc)
            return {}

    # ------------------------------------------------------------------ #
    # Historical data
    # ------------------------------------------------------------------ #
    def copy_rates(
        self,
        symbol: str,
        timeframe: str,
        count: int,
        start_pos: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        Return a list of OHLCV dict rows for the most recent `count` bars.

        Each row: {time, open, high, low, close, tick_volume, spread, real_volume}
        Returns an empty list when offline.
        """
        if self.mt5 is None or not self.connected:
            return []
        try:
            tf = timeframe_to_mt5(timeframe, self.mt5)
            rates = self.mt5.copy_rates_from_pos(symbol, tf, start_pos, count)
            if rates is None:
                self.log.warning(
                    "copy_rates_from_pos returned None for %s %s",
                    symbol, timeframe,
                )
                return []
            rows: List[Dict[str, Any]] = []
            for r in rates:
                rows.append(
                    {
                        "time": int(r["time"]),
                        "open": float(r["open"]),
                        "high": float(r["high"]),
                        "low": float(r["low"]),
                        "close": float(r["close"]),
                        "tick_volume": float(r["tick_volume"]),
                        "spread": float(r["spread"]) if "spread" in r.dtype.names else 0.0,
                        "real_volume": float(r["real_volume"]) if "real_volume" in r.dtype.names else 0.0,
                    }
                )
            return rows
        except Exception as exc:
            self.log.error("copy_rates error for %s %s: %s", symbol, timeframe, exc)
            return []

    # ------------------------------------------------------------------ #
    # Orders / positions (used by the execution layer)
    # ------------------------------------------------------------------ #
    def positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return open positions, optionally filtered by symbol."""
        if self.mt5 is None or not self.connected:
            return []
        try:
            if symbol:
                raw = self.mt5.positions_get(symbol=symbol)
            else:
                raw = self.mt5.positions_get()
            if raw is None:
                return []
            out: List[Dict[str, Any]] = []
            for p in raw:
                out.append(
                    {
                        "ticket": getattr(p, "ticket", None),
                        "symbol": getattr(p, "symbol", None),
                        "type": getattr(p, "type", None),
                        "volume": getattr(p, "volume", None),
                        "price_open": getattr(p, "price_open", None),
                        "sl": getattr(p, "sl", None),
                        "tp": getattr(p, "tp", None),
                        "profit": getattr(p, "profit", None),
                        "magic": getattr(p, "magic", None),
                    }
                )
            return out
        except Exception as exc:
            self.log.error("positions error: %s", exc)
            return []

    def order_send(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        Send a raw MT5 order request dict. Returns a dict describing the result.
        Returns {"ok": False, "reason": "offline"} when not connected.
        """
        if self.mt5 is None or not self.connected:
            return {"ok": False, "reason": "offline"}
        try:
            result = self.mt5.order_send(request)
            if result is None:
                return {"ok": False, "reason": "order_send returned None",
                        "last_error": self.mt5.last_error()}
            retcode = getattr(result, "retcode", None)
            ok = retcode == getattr(self.mt5, "TRADE_RETCODE_DONE", 10009)
            return {
                "ok": bool(ok),
                "retcode": retcode,
                "order": getattr(result, "order", None),
                "deal": getattr(result, "deal", None),
                "price": getattr(result, "price", None),
                "comment": getattr(result, "comment", None),
            }
        except Exception as exc:
            self.log.error("order_send error: %s", exc)
            return {"ok": False, "reason": str(exc)}

    # Convenience constants resolved at runtime (None when offline).
    @property
    def ORDER_TYPE_BUY(self) -> Optional[int]:
        return getattr(self.mt5, "ORDER_TYPE_BUY", 0) if self.mt5 else None

    @property
    def ORDER_TYPE_SELL(self) -> Optional[int]:
        return getattr(self.mt5, "ORDER_TYPE_SELL", 1) if self.mt5 else None

    @property
    def TRADE_ACTION_DEAL(self) -> Optional[int]:
        return getattr(self.mt5, "TRADE_ACTION_DEAL", 1) if self.mt5 else None
