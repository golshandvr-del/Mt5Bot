"""
Shared test helpers.

Ensures the project root is importable from any test module, and provides a
small synthetic OHLCV builder so tests never depend on external data files.

All text is standard ASCII English only.
"""

from __future__ import annotations

import math
import os
import sys


def ensure_project_on_path() -> str:
    """Insert the project root (parent of tests/) onto sys.path; return it."""
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    if root not in sys.path:
        sys.path.insert(0, root)
    return root


# Make imports work as soon as this module is imported.
PROJECT_ROOT = ensure_project_on_path()


def make_synthetic_ohlcv(n: int = 800, symbol: str = "TESTX",
                         timeframe: str = "M15", seed: int = 7):
    """
    Build a deterministic synthetic OHLCV series with a mild trend + noise.

    Uses only the standard library so it works everywhere. Returns an OHLCV
    instance from core.data.data_feed.
    """
    from core.data.data_feed import OHLCV  # imported lazily after path fix

    import random
    rng = random.Random(seed)

    ohlcv = OHLCV(symbol=symbol, timeframe=timeframe)
    price = 100.0
    t = 1_600_000_000  # arbitrary epoch start
    step = 900  # 15 minutes in seconds
    for i in range(n):
        # Gentle deterministic wave + random walk so indicators have structure.
        drift = 0.02 * math.sin(i / 40.0)
        shock = rng.uniform(-0.35, 0.35)
        price = max(1.0, price + drift + shock)
        high = price + abs(rng.uniform(0.05, 0.5))
        low = price - abs(rng.uniform(0.05, 0.5))
        open_ = price + rng.uniform(-0.2, 0.2)
        close = price + rng.uniform(-0.2, 0.2)
        high = max(high, open_, close)
        low = min(low, open_, close)
        vol = rng.randint(100, 1000)
        ohlcv.append_row(t + i * step, open_, high, low, close, vol)
    return ohlcv
