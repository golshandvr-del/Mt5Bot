"""
Python-vs-EA signal parity harness (UPGRADE_PLAN.md U2.6).

WHY
---
Diagnosis D1 in UPGRADE_PLAN.md: the EA must trade the SAME blended signal the
Python search validated. A single sign/edge-case drift in one indicator (the
RSI/MACD/ADX bugs called out in Mt5SmartBotEA.mq5) silently turns a validated
winner into a live loser. This harness locks the two implementations together.

WHAT IT DOES
------------
1. Builds a DETERMINISTIC synthetic OHLCV series (same builder the tests use) so
   Python and MQL5 can be fed byte-identical bars with no broker/network in the
   loop.
2. Writes that series to
       tests/fixtures/parity_ohlcv.csv        (time,open,high,low,close,volume)
   which the companion MQL5 script (experts/ParityDump.mq5) reads bar-by-bar.
3. Evaluates a FIXED, EA-supported StrategySpec through the REAL Python
   Strategy.signal_series() and writes the per-bar blended signal to
       tests/fixtures/parity_python.csv       (bar_index,blended_signal)
4. Prints the exact params the MQL5 side must be configured with, so both sides
   evaluate the identical spec.

The MQL5 script (run once inside MetaTrader 5 on the same CSV) writes
       tests/fixtures/parity_ea.csv           (bar_index,blended_signal)
Then tests/test_parity_harness.py asserts max|python - ea| < 1e-6.

Everything here is standard-library only and Win7/Py3.8/CPU friendly.
All text is standard ASCII English only.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# The single spec both sides evaluate. ONLY EA-supported indicators (see
# scripts/export_strategy_for_ea.py::EA_SUPPORTED_INDICATORS) so the EA can
# reproduce it 1:1. Kept small and explicit for auditability.
PARITY_SPEC = {
    "indicators": {
        "ema": {"period": 20},
        "rsi": {"period": 14},
        "macd": {"fast": 12, "slow": 26, "signal": 9},
        "adx": {"period": 14},
    },
    "weights": {"ema": 1.0, "rsi": 2.0, "macd": 1.5, "adx": 1.0},
    "long_threshold": 0.3,
    "short_threshold": 0.3,
}

FIXTURE_DIR = os.path.join("tests", "fixtures")
OHLCV_CSV = "parity_ohlcv.csv"
PYTHON_CSV = "parity_python.csv"
EA_CSV = "parity_ea.csv"

# Number of synthetic bars. Enough for every indicator to warm up and produce a
# non-trivial signal history, small enough to eyeball.
N_BARS = 600
SEED = 7


def build_series(n=N_BARS, seed=SEED):
    """Deterministic OHLCV, identical to the test helper builder."""
    root = _project_root()
    if root not in sys.path:
        sys.path.insert(0, root)
    from tests.helpers import make_synthetic_ohlcv
    return make_synthetic_ohlcv(n=n, symbol="PARITY", timeframe="M15", seed=seed)


def write_ohlcv_csv(ohlcv, path):
    with open(path, "w", newline="", encoding="ascii") as fh:
        w = csv.writer(fh)
        w.writerow(["time", "open", "high", "low", "close", "volume"])
        for i in range(len(ohlcv.close)):
            w.writerow([
                ohlcv.time[i], ohlcv.open[i], ohlcv.high[i],
                ohlcv.low[i], ohlcv.close[i], ohlcv.volume[i],
            ])


def compute_python_signal(ohlcv):
    """Return the per-bar blended signal from the REAL Python Strategy."""
    root = _project_root()
    if root not in sys.path:
        sys.path.insert(0, root)
    from core.strategy.strategy import StrategySpec, Strategy
    spec = StrategySpec.from_dict(dict(PARITY_SPEC))
    strat = Strategy(spec)
    return strat.signal_series(ohlcv)


def write_python_csv(signal, path):
    with open(path, "w", newline="", encoding="ascii") as fh:
        w = csv.writer(fh)
        w.writerow(["bar_index", "blended_signal"])
        for i, s in enumerate(signal):
            w.writerow([i, "%.10f" % float(s)])


def _params_hint():
    lines = []
    lines.append("MQL5 side must be configured with the SAME spec:")
    for name, params in PARITY_SPEC["indicators"].items():
        w = PARITY_SPEC["weights"].get(name, 1.0)
        lines.append("  %-6s enabled  weight=%s  params=%s"
                     % (name, w, params))
    lines.append("  long_threshold=%s  short_threshold=%s"
                 % (PARITY_SPEC["long_threshold"],
                    PARITY_SPEC["short_threshold"]))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Generate the Python side of the EA parity fixture.")
    parser.add_argument("--bars", type=int, default=N_BARS,
                        help="number of synthetic bars (default %d)" % N_BARS)
    parser.add_argument("--seed", type=int, default=SEED,
                        help="RNG seed (default %d)" % SEED)
    args = parser.parse_args(argv)

    root = _project_root()
    fixture_dir = os.path.join(root, FIXTURE_DIR)
    os.makedirs(fixture_dir, exist_ok=True)

    ohlcv = build_series(n=args.bars, seed=args.seed)

    ohlcv_path = os.path.join(fixture_dir, OHLCV_CSV)
    write_ohlcv_csv(ohlcv, ohlcv_path)
    print("[ OK ] wrote %s (%d bars)" % (ohlcv_path, len(ohlcv.close)))

    signal = compute_python_signal(ohlcv)
    py_path = os.path.join(fixture_dir, PYTHON_CSV)
    write_python_csv(signal, py_path)
    print("[ OK ] wrote %s" % py_path)

    print("")
    print(_params_hint())
    print("")
    print("NEXT: run experts/ParityDump.mq5 in MT5 on %s to write %s,"
          % (OHLCV_CSV, EA_CSV))
    print("      then run: python -m pytest tests/test_parity_harness.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
