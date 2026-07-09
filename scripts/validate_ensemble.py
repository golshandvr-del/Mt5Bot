"""
validate_ensemble.py - walk-forward validate the ENGINE BLEND composite
(UPGRADE_PLAN U2.5).

Diagnosis D2 in UPGRADE_PLAN.md: when `decision.mode: "blend"` the live/paper
engine trades the AVERAGE of the top-K registry strategies' signals, thresholded
by the GLOBAL `decision.long_threshold` / `short_threshold`. That composite is
NEVER walk-forward validated - the individual strategies are, but their blended
behaviour under a different (global) threshold is a brand-new, untested strategy.

This script closes that gap. It:

  1. loads the CURRENT registry top-K specs for a symbol/timeframe,
  2. rebuilds them as live-identical Strategy objects,
  3. wraps them in `CompositeStrategy` (the exact engine-blend average + global
     thresholds + weighted SL/TP - see core/strategy/composite.py),
  4. replays that composite through the SAME pessimistic Backtester the internal
     backtest uses, and
  5. writes the SAME U1 receipts (per-trade CSV + equity CSV) plus a JSON summary
     so the blend composite is finally inspectable and audit-able.

It reads ONLY the memory DB + a price CSV (or MT5 if a connector is wired) - no
search, no live orders. Pure stdlib + the project's own modules; Windows 7 +
Python 3.8 + CPU-only friendly.

IMPORTANT CAVEAT (printed by the run too): this validates the PRICE-ONLY portion
of the engine blend (the memory ensemble average + global thresholds + SL/TP).
It intentionally does NOT fold in the ML learner or the news score, because those
are not pure functions of the OHLCV window and cannot be replayed bar-by-bar
offline without lookahead risk. In `decision.mode: "parity"` (the default) this
whole composite is bypassed anyway, so parity users do not need this script; it
exists for anyone deliberately running "blend" mode for research.

Usage
-----
    python scripts/validate_ensemble.py
    python scripts/validate_ensemble.py --symbol XAUUSD --timeframe M15
    python scripts/validate_ensemble.py --symbol XAUUSD --tf M15 --warmup 60

All text is standard ASCII English only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Make the project importable when run as `python scripts/validate_ensemble.py`.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from config.loader import load_config, resolve_path  # noqa: E402
from core.data.data_feed import DataFeed  # noqa: E402
from core.memory.store import MemoryStore  # noqa: E402
from core.strategy.strategy import Strategy, StrategySpec  # noqa: E402
from core.strategy.backtester import Backtester  # noqa: E402
from core.strategy.composite import CompositeStrategy  # noqa: E402
from core.utils import trade_log  # noqa: E402


def _decision_thresholds(cfg):
    """Read the GLOBAL decision thresholds exactly as the engine does."""
    dec = cfg.get_path("decision", {})
    try:
        lt = float(dec.get("long_threshold", 0.6)) if hasattr(dec, "get") else 0.6
    except (TypeError, ValueError):
        lt = 0.6
    try:
        st = float(dec.get("short_threshold", 0.6)) if hasattr(dec, "get") else 0.6
    except (TypeError, ValueError):
        st = 0.6
    return lt, st


def _build_ensemble(memory, symbol, timeframe):
    """Rebuild the registry top-K as Strategy objects (engine-identical)."""
    strategies = []
    specs = []
    top = memory.load_registry_top(symbol, timeframe)
    for entry in top:
        spec_dict = entry.get("spec", {})
        if not spec_dict:
            continue
        spec = StrategySpec.from_dict(spec_dict)
        strategies.append(Strategy(spec))
        specs.append(spec.to_dict())
    return strategies, specs


def validate(cfg, symbol, timeframe, warmup=60):
    """Validate the engine-blend composite for one symbol/timeframe."""
    memory = MemoryStore(cfg)
    lt, st = _decision_thresholds(cfg)

    strategies, specs = _build_ensemble(memory, symbol, timeframe)
    if not strategies:
        print("=" * 70)
        print("No registry strategies for %s %s - nothing to validate." % (
            symbol, timeframe))
        print("Run a search (and rebuild-registry) first.")
        return {"symbol": symbol, "timeframe": timeframe,
                "error": "empty_registry"}

    # Load price history via the data feed (CSV fallback works fully offline).
    feed = DataFeed(cfg)
    ohlcv = feed.get_ohlcv(symbol, timeframe)
    if ohlcv is None or len(ohlcv) < 200:
        n = 0 if ohlcv is None else len(ohlcv)
        print("=" * 70)
        print("Not enough price data for %s %s (%d bars). Export history "
              "first (scripts/export_history.py)." % (symbol, timeframe, n))
        return {"symbol": symbol, "timeframe": timeframe,
                "error": "insufficient_data", "bars": n}

    composite = CompositeStrategy(
        strategies, symbol=symbol, timeframe=timeframe,
        long_threshold=lt, short_threshold=st,
    )

    bt = Backtester(cfg)
    result = bt.run(composite, ohlcv, warmup=warmup, record_trades=True)

    report_dir = resolve_path(
        cfg, cfg.get_path("backtest.report_dir", "backtests"))
    # Tag the artifacts so they never collide with the single-strategy backtest.
    artifacts = trade_log.write_artifacts(
        result, "%s_ENSEMBLE" % symbol, timeframe, report_dir=report_dir)

    summary = {
        "symbol": symbol,
        "timeframe": timeframe,
        "mode": "engine_blend_composite",
        "n_strategies": len(strategies),
        "long_threshold": lt,
        "short_threshold": st,
        "composite_sl_atr_mult": composite.spec.sl_atr_mult,
        "composite_tp_atr_mult": composite.spec.tp_atr_mult,
        "bars": len(ohlcv),
        "num_trades": len(getattr(result, "trade_pnls", []) or []),
        "metrics": result.metrics,
        "artifacts": artifacts,
        "strategy_specs": specs,
        "caveat": ("Price-only portion of the engine blend (memory ensemble "
                   "average + global thresholds + weighted SL/TP). ML learner "
                   "and news are NOT included - they cannot be replayed offline "
                   "without lookahead risk."),
    }

    out_path = os.path.join(
        report_dir, "ensemble_validation_%s_%s.json" % (symbol, timeframe))
    try:
        with open(out_path, "w") as handle:
            json.dump(summary, handle, indent=2, default=str)
        summary["summary_file"] = out_path
    except Exception as exc:  # pragma: no cover - defensive
        print("WARNING: could not write summary JSON: %s" % exc)

    _print_summary(summary)
    return summary


def _print_summary(summary):
    m = summary.get("metrics", {}) or {}
    print("=" * 70)
    print("ENGINE-BLEND COMPOSITE VALIDATION  (UPGRADE_PLAN U2.5)")
    print("  symbol/timeframe   :", summary["symbol"], summary["timeframe"])
    print("  strategies blended :", summary["n_strategies"])
    print("  global thresholds  : long=%.3f short=%.3f" % (
        summary["long_threshold"], summary["short_threshold"]))
    print("  composite SL/TP    : %.2f / %.2f (ATR mult)" % (
        summary["composite_sl_atr_mult"], summary["composite_tp_atr_mult"]))
    print("  bars               :", summary["bars"])
    print("  num_trades         :", summary["num_trades"])
    print("  net_profit         :", m.get("net_profit"))
    print("  expectancy         :", m.get("expectancy"))
    print("  profit_factor      :", m.get("profit_factor"))
    print("  win_rate           :", m.get("win_rate"))
    print("  max_drawdown       :", m.get("max_drawdown"))
    print("-" * 70)
    art = summary.get("artifacts", {}) or {}
    print("  trades CSV         :", art.get("trades"))
    print("  equity CSV         :", art.get("equity"))
    print("  summary JSON       :", summary.get("summary_file"))
    print("-" * 70)
    print("CAVEAT:", summary.get("caveat"))
    print("  Tip: render an HTML report with")
    print("       python scripts/make_report.py --trades <trades CSV>")
    print("=" * 70)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Walk-forward validate the engine-blend composite (U2.5).")
    parser.add_argument("--config", default=None,
                        help="Path to a config YAML (default config/config.yaml).")
    parser.add_argument("--symbol", default=None,
                        help="Symbol (default: first of mt5.symbols).")
    parser.add_argument("--timeframe", "--tf", dest="timeframe", default=None,
                        help="Timeframe (default: mt5.timeframe).")
    parser.add_argument("--warmup", type=int, default=60,
                        help="Warmup bars before scoring (default 60).")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    cfg = load_config(args.config)
    symbol = args.symbol
    if not symbol:
        syms = cfg.get_path("mt5.symbols", ["EURUSD"])
        symbol = str(syms[0]) if syms else "EURUSD"
    timeframe = args.timeframe or str(cfg.get_path("mt5.timeframe", "M15"))

    validate(cfg, symbol, timeframe, warmup=args.warmup)
    return 0


if __name__ == "__main__":
    sys.exit(main())
