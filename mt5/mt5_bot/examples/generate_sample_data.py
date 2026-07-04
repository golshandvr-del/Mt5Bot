"""
Generate synthetic OHLCV CSV files for offline development and testing.

This lets you exercise the entire pipeline (indicators, learning, search,
backtest, decision) WITHOUT a MetaTrader5 connection or any market data. On
Windows you would instead export real history from MT5 (see scripts/export_data.py
or the README), but this generator is handy for a first run anywhere.

It writes files to data_store/history/<SYMBOL>_<TIMEFRAME>.csv with columns:
    time, open, high, low, close, volume

The series is a random walk with mild trend and volatility clustering, which is
enough to produce non-trivial indicator and strategy behavior.

All text is standard ASCII English only.
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sys
import time


def _project_root() -> str:
    # examples/ -> project root is one level up.
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def generate_series(n_bars: int, start_price: float, seed: int):
    """Return parallel lists (t, o, h, l, c, v) for a synthetic random walk."""
    random.seed(seed)
    t0 = int(time.time()) - n_bars * 900  # assume M15 spacing (900s) for time
    price = start_price
    vol = 0.0008 * start_price  # base per-bar volatility

    times, opens, highs, lows, closes, volumes = [], [], [], [], [], []
    trend = 0.0
    for i in range(n_bars):
        # Slowly drifting trend component.
        trend += random.uniform(-1, 1) * 0.00002 * start_price
        trend *= 0.995  # mean-revert the trend so it does not run away

        # Volatility clustering: vol wanders a bit.
        vol *= math.exp(random.uniform(-0.05, 0.05))
        vol = max(0.0002 * start_price, min(0.003 * start_price, vol))

        open_p = price
        change = random.gauss(0.0, 1.0) * vol + trend
        close_p = max(0.0001, open_p + change)
        high_p = max(open_p, close_p) + abs(random.gauss(0.0, 1.0)) * vol * 0.5
        low_p = min(open_p, close_p) - abs(random.gauss(0.0, 1.0)) * vol * 0.5
        volume = abs(random.gauss(1000.0, 300.0))

        times.append(t0 + i * 900)
        opens.append(round(open_p, 5))
        highs.append(round(high_p, 5))
        lows.append(round(low_p, 5))
        closes.append(round(close_p, 5))
        volumes.append(round(volume, 2))
        price = close_p
    return times, opens, highs, lows, closes, volumes


def write_csv(path: str, series) -> None:
    times, opens, highs, lows, closes, volumes = series
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    import csv
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["time", "open", "high", "low", "close", "volume"])
        for i in range(len(times)):
            writer.writerow([times[i], opens[i], highs[i], lows[i],
                             closes[i], volumes[i]])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Generate synthetic OHLCV CSVs.")
    parser.add_argument("--symbols", default="EURUSD,GBPUSD,XAUUSD",
                        help="Comma-separated symbols.")
    parser.add_argument("--timeframe", default="M15", help="Timeframe label.")
    parser.add_argument("--bars", type=int, default=6000,
                        help="Number of bars per symbol.")
    parser.add_argument("--seed", type=int, default=42, help="Random seed base.")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    root = _project_root()
    hist_dir = os.path.join(root, "data_store", "history")
    start_prices = {"EURUSD": 1.10, "GBPUSD": 1.27, "XAUUSD": 1950.0,
                    "USDJPY": 150.0, "AUDUSD": 0.66, "USDCAD": 1.35}

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    for idx, sym in enumerate(symbols):
        start = start_prices.get(sym, 1.0)
        series = generate_series(args.bars, start, args.seed + idx)
        path = os.path.join(hist_dir, "%s_%s.csv" % (sym, args.timeframe.upper()))
        write_csv(path, series)
        print("Wrote %d bars to %s" % (args.bars, path))
    print("Done. You can now run: python main.py --mode search")
    return 0


if __name__ == "__main__":
    sys.exit(main())
