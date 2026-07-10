"""
gauntlet.py - the final pre-flight validation gauntlet (UPGRADE_PLAN U5.1/U5.2).

Before any strategy is trusted with live money it must survive a FIXED sequence
of pessimistic stress tests. This script runs that sequence on the CURRENT
registry top-1 strategy for a symbol/timeframe and writes ONE human-readable
verdict file (backtests/gauntlet_<fingerprint>.md) with PASS/FAIL per gate.

The five gates (all deliberately pessimistic - see UPGRADE_PLAN diagnosis D3):

  Gate 1  Full-history backtest      - the strategy must be net-profitable with
                                        a positive expectancy over ALL bars,
                                        under the (already pessimistic) sim.
  Gate 2  Locked holdout             - re-score on the last `holdout_bars` bars
                                        that the search is configured to never
                                        see; the edge must survive out-of-sample.
  Gate 3  Monte-Carlo trade order    - reshuffle the trade sequence N times to
                                        build 5%/95% equity envelopes, a max-DD
                                        distribution and a risk-of-ruin estimate;
                                        a lucky-ordering strategy fails here.
  Gate 4  Cost stress               - re-run with spread x1.5 and x2; the edge
                                        MUST survive x1.5 (x2 is informational).
  Gate 5  Worst-case start          - equity over the worst rolling 3-month
                                        window must not be catastrophic.

It reads ONLY the memory DB + a price CSV (or MT5 if wired) - no search, no live
orders. Pure stdlib + the project's own modules; Win7 / Py3.8 / CPU friendly.

Usage
-----
    python scripts/gauntlet.py
    python scripts/gauntlet.py --symbol XAUUSD --tf M15
    python scripts/gauntlet.py --symbol XAUUSD --tf M15 --mc 1000 --warmup 60

All text is standard ASCII English only.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import random
import sys
import time

# Make the project importable when run as `python scripts/gauntlet.py`.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from config.loader import load_config, resolve_path  # noqa: E402
from core.data.data_feed import DataFeed  # noqa: E402
from core.memory.store import MemoryStore  # noqa: E402
from core.strategy.strategy import Strategy, StrategySpec  # noqa: E402
from core.strategy.backtester import Backtester  # noqa: E402
from core.utils import trade_log  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _load_top1(memory, symbol, timeframe):
    """Return (StrategySpec, entry_dict) for the registry top-1, or (None, None)."""
    top = memory.load_registry_top(symbol, timeframe)
    for entry in top:
        spec_dict = entry.get("spec", {})
        if spec_dict:
            return StrategySpec.from_dict(spec_dict), entry
    return None, None


def _run_bt(cfg, strategy, ohlcv, warmup, record_trades=False):
    """Run the standard (config-driven, pessimistic) backtester."""
    bt = Backtester(cfg)
    return bt.run(strategy, ohlcv, warmup=warmup, record_trades=record_trades)


def _holdout_bars(cfg):
    try:
        return int(cfg.get_path("memory.walk_forward.holdout_bars", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _cfg_with_spread_mult(cfg, mult):
    """Return a deep-copied config whose spread cost is multiplied by `mult`.

    Works for BOTH the flat `backtest.spread_points` and the U3.3
    `backtest.spread_model.base_points`, so the stress applies whichever cost
    model is active.
    """
    clone = copy.deepcopy(cfg)
    try:
        bt = clone.get_path("backtest", {})
        if hasattr(bt, "get"):
            base = float(bt.get("spread_points", 10) or 10)
            bt["spread_points"] = base * mult
            sm = bt.get("spread_model", None)
            if hasattr(sm, "get") and len(sm) > 0:
                sm_base = float(sm.get("base_points", base) or base)
                sm["base_points"] = sm_base * mult
    except Exception:
        pass
    return clone


def _bars_per_3_months(timeframe):
    """Approx number of bars in a 3-month window for common timeframes."""
    tf = str(timeframe).upper()
    minutes = {
        "M1": 1, "M5": 5, "M15": 15, "M30": 30,
        "H1": 60, "H4": 240, "D1": 1440,
    }.get(tf, 15)
    # ~63 trading days * 24h for FX/metals (24-5 markets); use 24h approximation.
    bars_per_day = (24 * 60) / minutes
    return int(bars_per_day * 63)


def _pct(x):
    try:
        return "%.2f%%" % (100.0 * float(x))
    except (TypeError, ValueError):
        return "n/a"


def _num(x, fmt="%.4f"):
    try:
        return fmt % float(x)
    except (TypeError, ValueError):
        return "n/a"
