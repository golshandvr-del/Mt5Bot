"""
Export MT5 history to CSV for offline strategy search / backtesting.

Run this ON WINDOWS with the MetaTrader5 terminal open and logged in. It uses
the bot's own connector + data feed to pull historical bars for the configured
symbols/timeframes and writes them to data_store/history/<SYMBOL>_<TF>.csv.

Those CSVs are then used by:
  - python main.py --mode search     (Phase 3 strategy/parameter search)
  - python main.py --mode backtest   (internal walk-forward report)
even when the terminal is closed, so the heavy exploration can run offline.

Usage
-----
    python scripts/export_history.py
    python scripts/export_history.py --symbols EURUSD,XAUUSD --timeframe M15 --bars 20000
    python scripts/export_history.py --all-timeframes

All text is standard ASCII English only.
"""

from __future__ import annotations

import argparse
import os
import sys


def _project_root() -> str:
    # scripts/ -> project root is one level up.
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv=None) -> int:
    root = _project_root()
    if root not in sys.path:
        sys.path.insert(0, root)

    from app.context import BotContext

    parser = argparse.ArgumentParser(
        description="Export MT5 history to CSV for offline use."
    )
    parser.add_argument("--config", default=None, help="Path to a config YAML.")
    parser.add_argument("--symbols", default=None,
                        help="Comma-separated symbols (default: config mt5.symbols).")
    parser.add_argument("--timeframe", default=None,
                        help="Single timeframe label (default: config mt5.timeframe).")
    parser.add_argument("--all-timeframes", action="store_true",
                        help="Export every timeframe listed under mt5.timeframes.")
    parser.add_argument("--bars", type=int, default=None,
                        help="Bars per symbol (default: config mt5.history_bars).")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    ctx = BotContext(args.config)
    connected = ctx.connect_mt5()
    if not connected:
        print("[ERROR] Could not connect to MetaTrader5. Open the terminal, "
              "log in, and ensure the MetaTrader5 Python package is installed.")
        print("        This exporter needs a live terminal to pull history.")
        ctx.shutdown()
        return 1

    # Resolve symbols / timeframes / bar count.
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = [str(s) for s in ctx.cfg.get_path("mt5.symbols", ["EURUSD"])]

    if args.all_timeframes:
        timeframes = [str(t) for t in ctx.cfg.get_path("mt5.timeframes", ["M15"])]
    elif args.timeframe:
        timeframes = [args.timeframe.upper()]
    else:
        timeframes = [str(ctx.cfg.get_path("mt5.timeframe", "M15"))]

    bars = args.bars or int(ctx.cfg.get_path("mt5.history_bars", 5000))

    print("Exporting history: symbols=%s timeframes=%s bars=%d"
          % (symbols, timeframes, bars))
    exported = 0
    for symbol in symbols:
        for tf in timeframes:
            path = ctx.data_feed.export_live_to_csv(symbol, tf, bars)
            if path:
                print("  [ OK ] %s %s -> %s" % (symbol, tf, path))
                exported += 1
            else:
                print("  [WARN] No data exported for %s %s" % (symbol, tf))

    ctx.shutdown()
    print("Done. Exported %d file(s). You can now run:" % exported)
    print("    python main.py --mode search")
    return 0 if exported > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
