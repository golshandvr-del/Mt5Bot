"""
Small, dependency-light helper functions shared across the project.

Includes:
- seeding for reproducibility,
- safe numeric helpers,
- timeframe name -> MT5 constant mapping (with a fallback table so code that
  does not import MetaTrader5 still works),
- JSON read/write helpers that never crash the bot.

All text is standard ASCII English only.
"""

from __future__ import annotations

import json
import math
import os
import random
from typing import Any, Dict, Optional


def set_global_seed(seed: int) -> None:
    """Seed Python and (if present) numpy RNGs for reproducible runs."""
    random.seed(seed)
    try:
        import numpy as np  # type: ignore
        np.random.seed(seed)
    except Exception:
        pass


def safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    """Divide without raising on zero denominator."""
    try:
        if denominator == 0:
            return default
        return numerator / denominator
    except Exception:
        return default


def clamp(value: float, low: float, high: float) -> float:
    """Constrain value to the inclusive range [low, high]."""
    return max(low, min(high, value))


def is_finite_number(value: Any) -> bool:
    """True if value is a real, finite number."""
    try:
        return isinstance(value, (int, float)) and math.isfinite(float(value))
    except Exception:
        return False


# -----------------------------------------------------------------------------
# Timeframe mapping
# -----------------------------------------------------------------------------
# MT5 integer constants for timeframes. We keep an explicit fallback table so
# modules that do not (or cannot) import MetaTrader5 still convert names.
# The numbers match the MetaTrader5 package constants.
_MT5_TIMEFRAME_FALLBACK: Dict[str, int] = {
    "M1": 1,
    "M2": 2,
    "M3": 3,
    "M4": 4,
    "M5": 5,
    "M6": 6,
    "M10": 10,
    "M12": 12,
    "M15": 15,
    "M20": 20,
    "M30": 30,
    "H1": 16385,
    "H2": 16386,
    "H3": 16387,
    "H4": 16388,
    "H6": 16390,
    "H8": 16392,
    "H12": 16396,
    "D1": 16408,
    "W1": 32769,
    "MN1": 49153,
}

# Approximate number of seconds per timeframe (for time math in backtests).
TIMEFRAME_SECONDS: Dict[str, int] = {
    "M1": 60,
    "M2": 120,
    "M3": 180,
    "M4": 240,
    "M5": 300,
    "M6": 360,
    "M10": 600,
    "M12": 720,
    "M15": 900,
    "M20": 1200,
    "M30": 1800,
    "H1": 3600,
    "H2": 7200,
    "H3": 10800,
    "H4": 14400,
    "H6": 21600,
    "H8": 28800,
    "H12": 43200,
    "D1": 86400,
    "W1": 604800,
    "MN1": 2592000,
}


def timeframe_to_mt5(name: str, mt5_module: Optional[object] = None) -> int:
    """
    Convert a timeframe string like "M15" to the MT5 integer constant.

    If the MetaTrader5 module is provided, prefer its real constant
    (e.g. mt5.TIMEFRAME_M15); otherwise use the internal fallback table.
    """
    name = (name or "M15").upper()
    if mt5_module is not None:
        attr = "TIMEFRAME_%s" % name
        if hasattr(mt5_module, attr):
            return int(getattr(mt5_module, attr))
    if name in _MT5_TIMEFRAME_FALLBACK:
        return _MT5_TIMEFRAME_FALLBACK[name]
    raise ValueError("Unknown timeframe: %s" % name)


def timeframe_seconds(name: str) -> int:
    """Return approximate seconds per bar for a timeframe name."""
    name = (name or "M15").upper()
    return TIMEFRAME_SECONDS.get(name, 900)


# -----------------------------------------------------------------------------
# Symbol specifications (offline defaults)
# -----------------------------------------------------------------------------
def symbol_offline_specs(symbol: str) -> Dict[str, float]:
    """
    Return reasonable offline (point, contract) defaults for a trading symbol,
    used by the internal backtester and risk sizing when a live MT5 symbol_info
    is not available. These are approximations only; the real MT5 Strategy
    Tester uses exact broker specs.

    Conventions
    -----------
    - point:    smallest price increment used to scale point-based costs.
    - contract: notional units per 1.0 lot, used to convert a price move into
                money (money = price_move * contract * lot).

    Instrument classes:
      * XAU/XAG (metals): point 0.01, contract 100 (1 lot = 100 oz).
      * *JPY pairs:       point 0.01, contract 100000.
      * Generic FX:       point 0.0001, contract 100000.
    Anything unrecognized falls back to the generic FX profile.
    """
    up = (symbol or "").upper()
    point = 0.0001
    contract = 100000.0
    if up.startswith("XAU") or up.startswith("XAG"):
        point = 0.01
        contract = 100.0
    elif up.endswith("JPY"):
        point = 0.01
        contract = 100000.0
    return {"point": point, "contract": contract}


# -----------------------------------------------------------------------------
# JSON helpers (never crash the bot)
# -----------------------------------------------------------------------------
def read_json(path: str, default: Any = None) -> Any:
    """Read JSON from disk; return default on any error."""
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return default


def write_json(path: str, data: Any) -> bool:
    """Write JSON to disk atomically-ish; return True on success."""
    try:
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True, default=str)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def ensure_dir(path: str) -> None:
    """Create a directory tree if it does not exist (no error if it does)."""
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass
