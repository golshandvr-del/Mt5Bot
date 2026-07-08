"""
Trade / equity artifact writers (Phase U1.2 - transparency overhaul).

After any backtest the user must be able to open plain files and audit every
single decision. This module turns a BacktestResult's per-trade receipts (see
`Backtester._make_trade_record`, U1.1) and its equity curve into two
human-readable CSV files:

  backtests/trades_<SYMBOL>_<TF>_<timestamp>.csv
      one row per closed trade with the full receipt (entry/exit time and
      price, direction, SL/TP, exit reason, gross PnL, each cost component,
      swap, net PnL, running balance, and the blended signal at entry).

  backtests/equity_<SYMBOL>_<TF>_<timestamp>.csv
      the bar-indexed equity curve (point index + equity value), so a chart of
      the account balance over the run can be rebuilt from a text file.

Design rules (repo-wide): pure standard library (csv only), ASCII English only,
Windows 7 + Python 3.8 friendly, and NEVER raise into the caller - a failure to
write an artifact must not abort a trading/backtest run, it only logs a warning.

The column order is fixed and documented so `scripts/make_report.py` (U1.3) and
the offline tests (U1.6) can rely on it.
"""

from __future__ import annotations

import csv
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from core.utils.helpers import ensure_dir


# Fixed CSV schema for the per-trade file. Keeping this as a module-level
# constant lets the report renderer and the tests import the exact same order.
TRADE_CSV_COLUMNS: List[str] = [
    "trade_no",
    "entry_ts",
    "entry_time",
    "exit_ts",
    "exit_time",
    "direction",       # "long" / "short"
    "entry_price",
    "exit_price",
    "stop_price",
    "take_price",
    "exit_reason",     # sl / tp / flip / eod
    "gross_pnl",
    "cost_spread",
    "cost_slippage",
    "cost_commission",
    "cost_swap",
    "total_cost",      # spread + slippage + commission + swap
    "pnl",             # net PnL (gross - total_cost)
    "balance_after",
    "signal",          # blended signal value at entry
]

EQUITY_CSV_COLUMNS: List[str] = ["point_index", "equity"]


def _fmt_utc(ts: Any) -> str:
    """Format an epoch-seconds timestamp as 'YYYY-MM-DD HH:MM:SS' UTC.

    Returns an empty string for missing/zero/bad values so the CSV stays clean
    (the numeric *_ts column still carries the raw value for machines).
    """
    try:
        ts_int = int(ts)
    except Exception:
        return ""
    if ts_int <= 0:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts_int))
    except Exception:
        return ""


def _dir_word(direction: Any) -> str:
    """Map +1/-1 to 'long'/'short' (anything else -> 'flat')."""
    try:
        d = int(direction)
    except Exception:
        return "flat"
    if d > 0:
        return "long"
    if d < 0:
        return "short"
    return "flat"


def _trade_to_row(trade_no: int, trade: Dict[str, Any]) -> Dict[str, Any]:
    """Turn one U1.1 trade receipt dict into a fixed-schema CSV row.

    Reads every field defensively so a partial/legacy record (only the three
    legacy keys entry_ts/pnl/direction) still produces a valid row with blanks
    for the fields it lacks.
    """
    def g(key: str, default: Any = "") -> Any:
        val = trade.get(key, default)
        return default if val is None else val

    cost_spread = float(g("cost_spread", 0.0) or 0.0)
    cost_slippage = float(g("cost_slippage", 0.0) or 0.0)
    cost_commission = float(g("cost_commission", 0.0) or 0.0)
    cost_swap = float(g("cost_swap", 0.0) or 0.0)
    total_cost = cost_spread + cost_slippage + cost_commission + cost_swap

    entry_ts = g("entry_ts", 0)
    exit_ts = g("exit_ts", 0)

    return {
        "trade_no": trade_no,
        "entry_ts": entry_ts,
        "entry_time": _fmt_utc(entry_ts),
        "exit_ts": exit_ts,
        "exit_time": _fmt_utc(exit_ts),
        "direction": _dir_word(g("direction", 0)),
        "entry_price": g("entry_price", ""),
        "exit_price": g("exit_price", ""),
        "stop_price": g("stop_price", ""),
        "take_price": g("take_price", ""),
        "exit_reason": g("exit_reason", ""),
        "gross_pnl": g("gross_pnl", ""),
        "cost_spread": cost_spread,
        "cost_slippage": cost_slippage,
        "cost_commission": cost_commission,
        "cost_swap": cost_swap,
        "total_cost": total_cost,
        "pnl": g("pnl", ""),
        "balance_after": g("balance_after", ""),
        "signal": g("signal", ""),
    }


def _timestamp_tag(now: Optional[float] = None) -> str:
    """Return a filesystem-safe 'YYYYmmdd_HHMMSS' UTC tag for filenames."""
    t = time.gmtime(now if now is not None else time.time())
    return time.strftime("%Y%m%d_%H%M%S", t)


def write_trade_csv(trades: List[Dict[str, Any]], path: str) -> bool:
    """Write the per-trade receipts to `path` as CSV. Returns True on success.

    Never raises: on any error it logs nothing here (caller logs) and returns
    False so a failed artifact write cannot abort a run.
    """
    try:
        ensure_dir(os.path.dirname(path) or ".")
        with open(path, "w", newline="", encoding="ascii", errors="replace") as fh:
            writer = csv.DictWriter(fh, fieldnames=TRADE_CSV_COLUMNS)
            writer.writeheader()
            for i, trade in enumerate(trades, start=1):
                writer.writerow(_trade_to_row(i, trade))
        return True
    except Exception:
        return False


def write_equity_csv(equity_curve: List[float], path: str) -> bool:
    """Write the bar-indexed equity curve to `path` as CSV. True on success."""
    try:
        ensure_dir(os.path.dirname(path) or ".")
        with open(path, "w", newline="", encoding="ascii", errors="replace") as fh:
            writer = csv.writer(fh)
            writer.writerow(EQUITY_CSV_COLUMNS)
            for idx, value in enumerate(equity_curve):
                writer.writerow([idx, value])
        return True
    except Exception:
        return False


def write_artifacts(result: Any, symbol: str, timeframe: str,
                    report_dir: str = "backtests",
                    now: Optional[float] = None) -> Dict[str, Optional[str]]:
    """
    Write both the trade and equity CSV artifacts for a BacktestResult.

    Parameters
    ----------
    result     : a BacktestResult (needs `.trades` and `.equity_curve`).
    symbol     : e.g. "XAUUSD" (used in the file name).
    timeframe  : e.g. "M15" (used in the file name).
    report_dir : directory to write into (created if missing).
    now        : optional epoch seconds for a deterministic timestamp (tests).

    Returns a dict {"trades": <path or None>, "equity": <path or None>}. A None
    value means that artifact could not be written (already logged by caller).
    Never raises.
    """
    tag = _timestamp_tag(now)
    safe_symbol = "".join(ch for ch in str(symbol) if ch.isalnum()) or "SYMBOL"
    safe_tf = "".join(ch for ch in str(timeframe) if ch.isalnum()) or "TF"

    trades = list(getattr(result, "trades", []) or [])
    equity = list(getattr(result, "equity_curve", []) or [])

    trades_path = os.path.join(
        report_dir, "trades_%s_%s_%s.csv" % (safe_symbol, safe_tf, tag))
    equity_path = os.path.join(
        report_dir, "equity_%s_%s_%s.csv" % (safe_symbol, safe_tf, tag))

    ok_trades = write_trade_csv(trades, trades_path)
    ok_equity = write_equity_csv(equity, equity_path)

    return {
        "trades": trades_path if ok_trades else None,
        "equity": equity_path if ok_equity else None,
    }


def implied_total_cost(trades: List[Dict[str, Any]]) -> float:
    """Sum every cost component across all trade receipts (U1.6 reconciliation).

    Used by tests to assert that the costs recorded in the CSV reconcile with
    the metrics' implied total cost (sum of gross_pnl - net pnl over trades).
    """
    total = 0.0
    for t in trades:
        for key in ("cost_spread", "cost_slippage", "cost_commission",
                    "cost_swap"):
            try:
                total += float(t.get(key, 0.0) or 0.0)
            except Exception:
                pass
    return total
