"""
Data feed abstraction.

Provides an OHLCV container and helpers to obtain price data either from a live
MT5 connection or from local CSV files (for fully offline backtesting and
development on machines without MetaTrader5).

Why a custom container instead of forcing pandas everywhere?
- pandas IS used when available (fast, convenient), but the indicator layer is
  written to operate on plain Python lists too, so the bot can still run on a
  minimal Windows 7 Python install if a heavy wheel fails to build.

The OHLCV object exposes parallel lists: time, open, high, low, close, volume.
If pandas is installed, .to_frame() returns a DataFrame for convenience.

All text is standard ASCII English only.
"""

from __future__ import annotations

import csv
import os
from typing import Any, Dict, List, Optional

from core.utils.logger import get_logger


def _try_pandas():
    try:
        import pandas as pd  # type: ignore
        return pd
    except Exception:
        return None


class OHLCV:
    """
    Lightweight Open/High/Low/Close/Volume series container.

    Attributes are parallel lists of equal length, oldest-first (index 0 is the
    oldest bar, index -1 is the most recent), matching typical backtest order.
    """

    __slots__ = ("symbol", "timeframe", "time", "open", "high", "low",
                 "close", "volume")

    def __init__(self, symbol: str = "", timeframe: str = ""):
        self.symbol = symbol
        self.timeframe = timeframe
        self.time: List[int] = []
        self.open: List[float] = []
        self.high: List[float] = []
        self.low: List[float] = []
        self.close: List[float] = []
        self.volume: List[float] = []

    def __len__(self) -> int:
        return len(self.close)

    def append_row(self, t: int, o: float, h: float, l: float,
                   c: float, v: float) -> None:
        self.time.append(int(t))
        self.open.append(float(o))
        self.high.append(float(h))
        self.low.append(float(l))
        self.close.append(float(c))
        self.volume.append(float(v))

    @classmethod
    def from_rows(cls, rows: List[Dict[str, Any]], symbol: str = "",
                  timeframe: str = "") -> "OHLCV":
        """Build an OHLCV from a list of dict rows (as MT5Connector returns)."""
        obj = cls(symbol, timeframe)
        for r in rows:
            vol = r.get("tick_volume", r.get("volume", 0.0))
            obj.append_row(
                r.get("time", 0),
                r.get("open", 0.0),
                r.get("high", 0.0),
                r.get("low", 0.0),
                r.get("close", 0.0),
                vol,
            )
        return obj

    def slice(self, start: int, end: Optional[int] = None) -> "OHLCV":
        """Return a new OHLCV covering [start, end) by index."""
        end = len(self) if end is None else end
        obj = OHLCV(self.symbol, self.timeframe)
        obj.time = self.time[start:end]
        obj.open = self.open[start:end]
        obj.high = self.high[start:end]
        obj.low = self.low[start:end]
        obj.close = self.close[start:end]
        obj.volume = self.volume[start:end]
        return obj

    def to_frame(self):
        """Return a pandas DataFrame if pandas is installed, else None."""
        pd = _try_pandas()
        if pd is None:
            return None
        return pd.DataFrame(
            {
                "time": self.time,
                "open": self.open,
                "high": self.high,
                "low": self.low,
                "close": self.close,
                "volume": self.volume,
            }
        )

    def to_csv(self, path: str) -> bool:
        """Write the series to a CSV file. Returns True on success."""
        try:
            directory = os.path.dirname(os.path.abspath(path))
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(path, "w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["time", "open", "high", "low", "close", "volume"])
                for i in range(len(self)):
                    writer.writerow([
                        self.time[i], self.open[i], self.high[i],
                        self.low[i], self.close[i], self.volume[i],
                    ])
            return True
        except Exception:
            return False

    @classmethod
    def from_csv(cls, path: str, symbol: str = "", timeframe: str = "") -> "OHLCV":
        """
        Load an OHLCV from a CSV with header row containing at least
        time, open, high, low, close, and one of volume/tick_volume.

        Time may be a unix integer or an ISO/MT5 datetime string; both are
        accepted (string times are kept as-is in a parallel index if not int).
        """
        obj = cls(symbol, timeframe)
        if not os.path.exists(path):
            return obj
        with open(path, "r", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            # Normalize header keys to lowercase for robustness.
            for raw in reader:
                row = {k.lower().strip(): v for k, v in raw.items()}
                t_raw = row.get("time", row.get("date", "0"))
                try:
                    t_val = int(float(t_raw))
                except Exception:
                    # Keep an incremental index if the timestamp is non-numeric.
                    t_val = len(obj.time)
                vol_raw = row.get("volume", row.get("tick_volume", "0"))
                try:
                    obj.append_row(
                        t_val,
                        float(row.get("open", 0.0)),
                        float(row.get("high", 0.0)),
                        float(row.get("low", 0.0)),
                        float(row.get("close", 0.0)),
                        float(vol_raw or 0.0),
                    )
                except Exception:
                    continue
        return obj


class DataFeed:
    """
    Unified entry point to obtain OHLCV data.

    Priority:
      1. If a connected MT5Connector is supplied, pull live history.
      2. Otherwise look for a local CSV under data_store/history/.

    This lets the same code run live on Windows and offline in development.
    """

    def __init__(self, cfg: Any, connector: Optional[object] = None):
        self.cfg = cfg
        self.connector = connector
        self.log = get_logger("data.data_feed", cfg)
        self.history_dir = os.path.join(
            cfg.get("project_root", "."), "data_store", "history"
        )

    def _csv_path(self, symbol: str, timeframe: str) -> str:
        fname = "%s_%s.csv" % (symbol.upper(), timeframe.upper())
        return os.path.join(self.history_dir, fname)

    def get_ohlcv(self, symbol: str, timeframe: str,
                  count: Optional[int] = None) -> OHLCV:
        """
        Return OHLCV for symbol/timeframe.

        Tries the live connector first (if connected), then a local CSV.
        """
        count = count or int(self.cfg.get_path("mt5.history_bars", 5000))

        # 1) Live MT5.
        if self.connector is not None and getattr(self.connector, "connected", False):
            rows = self.connector.copy_rates(symbol, timeframe, count)
            if rows:
                self.log.info(
                    "Loaded %d live bars for %s %s", len(rows), symbol, timeframe
                )
                return OHLCV.from_rows(rows, symbol, timeframe)
            self.log.warning(
                "No live bars for %s %s; falling back to CSV.", symbol, timeframe
            )

        # 2) Local CSV.
        path = self._csv_path(symbol, timeframe)
        if os.path.exists(path):
            obj = OHLCV.from_csv(path, symbol, timeframe)
            if count and len(obj) > count:
                obj = obj.slice(len(obj) - count, len(obj))
            self.log.info(
                "Loaded %d CSV bars for %s %s from %s",
                len(obj), symbol, timeframe, path,
            )
            return obj

        self.log.warning(
            "No data found for %s %s (no live connection and no CSV at %s).",
            symbol, timeframe, path,
        )
        return OHLCV(symbol, timeframe)

    def export_live_to_csv(self, symbol: str, timeframe: str,
                           count: Optional[int] = None) -> Optional[str]:
        """
        Pull live history and save it to data_store/history as CSV so it can be
        reused offline for backtesting and strategy search. Returns the path.
        """
        ohlcv = self.get_ohlcv(symbol, timeframe, count)
        if len(ohlcv) == 0:
            return None
        path = self._csv_path(symbol, timeframe)
        if ohlcv.to_csv(path):
            self.log.info("Exported %d bars to %s", len(ohlcv), path)
            return path
        return None
