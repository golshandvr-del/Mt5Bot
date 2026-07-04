"""
Mode runners: one function per operating mode.

Modes (config.general.mode or the --mode CLI flag):
  train    : build features from history and train the active learner offline,
             then persist the model file. (Phase 1)
  search   : run strategy/parameter search + walk-forward, persisting results
             and the top-strategy registry into memory. (Phase 3)
  backtest : run the internal backtester on the memory-selected top strategy
             (or a default) and print/save a metrics report. (Phase 3)
  paper    : connect to MT5 (if available), pull the latest bars for each
             symbol, produce a Decision, and LOG the intended order without
             sending it. Runs one pass (or a loop via run_bot script). (Phase all)
  live     : same as paper but actually sends orders via the execution layer.

Every runner is defensive: missing data, missing MT5, or disabled features
result in clear log messages, never crashes.

All text is standard ASCII English only.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

from app.context import BotContext
from config.loader import resolve_path
from core.data.data_feed import OHLCV
from core.strategy.strategy import Strategy, StrategySpec
from core.strategy.backtester import Backtester
from core.strategy.search import StrategySearch
from core.utils.helpers import write_json
from core.utils.logger import get_logger


# --------------------------------------------------------------------------- #
# Helpers shared by runners.
# --------------------------------------------------------------------------- #
def _symbols(ctx: BotContext) -> List[str]:
    syms = ctx.cfg.get_path("mt5.symbols", ["EURUSD"])
    return [str(s) for s in syms] if syms else ["EURUSD"]


def _timeframe(ctx: BotContext) -> str:
    return str(ctx.cfg.get_path("mt5.timeframe", "M15"))


def _load_history(ctx: BotContext, symbol: str, timeframe: str,
                  count: Optional[int] = None) -> OHLCV:
    """Load OHLCV via the data feed (live MT5 first, then CSV fallback)."""
    return ctx.data_feed.get_ohlcv(symbol, timeframe, count)


# --------------------------------------------------------------------------- #
# TRAIN (Phase 1)
# --------------------------------------------------------------------------- #
def run_train(ctx: BotContext) -> Dict[str, Any]:
    """Train the active learner offline on historical bars and save it."""
    log = get_logger("app.runners.train", ctx.cfg)
    ctx.connect_mt5()  # optional; CSV works too
    tf = _timeframe(ctx)
    name = ctx.cfg.get_path("learning.active_model", "ml_classifier")
    model_file = ctx.cfg.get_path("learning.%s.model_file" % name,
                                  "models/%s.pkl" % name)
    out_path = resolve_path(ctx.cfg, model_file)

    results: Dict[str, Any] = {"model": name, "trained_symbols": []}
    for symbol in _symbols(ctx):
        ohlcv = _load_history(ctx, symbol, tf)
        if len(ohlcv) < 200:
            log.warning("Not enough data for %s (%d bars); skipping.",
                        symbol, len(ohlcv))
            continue
        X, y, feat_names = ctx.feature_builder.build_training(ohlcv)
        if not X:
            log.warning("No training samples for %s; skipping.", symbol)
            continue
        log.info("Training %s on %s with %d samples...", name, symbol, len(X))
        ctx.learner.fit(X, y)
        results["trained_symbols"].append({"symbol": symbol, "samples": len(X)})
        # Train on the first symbol with data, then persist (single shared model).
        break

    if ctx.learner.is_ready():
        ok = ctx.learner.save(out_path)
        results["saved"] = bool(ok)
        results["model_file"] = out_path
        log.info("Training complete. Saved=%s at %s", ok, out_path)
    else:
        results["saved"] = False
        log.warning("Learner not ready after training; nothing saved.")
    ctx.shutdown()
    return results


# --------------------------------------------------------------------------- #
# SEARCH (Phase 3) - the memory builder
# --------------------------------------------------------------------------- #
def run_search(ctx: BotContext) -> Dict[str, Any]:
    """Run the strategy/parameter search and persist results to memory."""
    log = get_logger("app.runners.search", ctx.cfg)
    ctx.connect_mt5()
    tf = _timeframe(ctx)
    search = StrategySearch(ctx.cfg, ctx.memory)

    summary: Dict[str, Any] = {"symbols": {}}
    for symbol in _symbols(ctx):
        ohlcv = _load_history(ctx, symbol, tf)
        if len(ohlcv) < 500:
            log.warning("Not enough data for %s (%d bars); skipping search.",
                        symbol, len(ohlcv))
            continue
        log.info("Searching strategies for %s %s ...", symbol, tf)
        res = search.run(ohlcv, symbol, tf)
        summary["symbols"][symbol] = {
            "evaluated": res.get("evaluated", 0),
            "top": len(res.get("registry", {}).get("top", [])),
        }
    summary["memory_stats"] = ctx.memory.stats()
    log.info("Search finished. Memory now holds: %s", summary["memory_stats"])
    ctx.shutdown()
    return summary


# --------------------------------------------------------------------------- #
# BACKTEST (Phase 3)
# --------------------------------------------------------------------------- #
def _default_spec(symbol: str, timeframe: str) -> StrategySpec:
    """A sensible default EMA+RSI strategy used when memory is empty."""
    return StrategySpec(
        indicators={"ema": {"period": 21}, "rsi": {"period": 14}},
        weights={"ema": 1.0, "rsi": 1.0},
        long_threshold=0.3, short_threshold=0.3,
        sl_atr_mult=2.0, tp_atr_mult=3.0,
        symbol=symbol, timeframe=timeframe,
    )


def run_backtest(ctx: BotContext) -> Dict[str, Any]:
    """Backtest the top memory strategy (or a default) per symbol and report."""
    log = get_logger("app.runners.backtest", ctx.cfg)
    ctx.connect_mt5()
    tf = _timeframe(ctx)
    bt = Backtester(ctx.cfg)
    report_dir = resolve_path(ctx.cfg, ctx.cfg.get_path("backtest.report_dir", "backtests"))

    report: Dict[str, Any] = {"timeframe": tf, "symbols": {}}
    for symbol in _symbols(ctx):
        ohlcv = _load_history(ctx, symbol, tf)
        if len(ohlcv) < 200:
            log.warning("Not enough data for %s; skipping backtest.", symbol)
            continue

        # Prefer the best memory strategy; fall back to a default.
        top = ctx.memory.load_registry_top(symbol, tf)
        if top:
            spec = StrategySpec.from_dict(top[0]["spec"])
            source = "memory_top"
        else:
            spec = _default_spec(symbol, tf)
            source = "default_ema_rsi"

        result = bt.run(Strategy(spec), ohlcv, warmup=60)
        report["symbols"][symbol] = {
            "source": source,
            "spec": spec.to_dict(),
            "metrics": result.metrics,
        }
        log.info("Backtest %s (%s): %s", symbol, source, result.metrics)

    # Save the report as JSON.
    out = os.path.join(report_dir, "backtest_report.json")
    write_json(out, report)
    report["report_file"] = out
    log.info("Backtest report saved to %s", out)
    ctx.shutdown()
    return report


# --------------------------------------------------------------------------- #
# PAPER / LIVE (all phases combined via the decision engine)
# --------------------------------------------------------------------------- #
def run_once(ctx: BotContext) -> Dict[str, Any]:
    """
    Single decision pass over all symbols: load latest bars, refresh news,
    ask the decision engine, and execute (paper=log only, live=send order).
    """
    log = get_logger("app.runners.trade", ctx.cfg)
    tf = _timeframe(ctx)
    connected = ctx.connect_mt5()

    # Refresh news once per pass (cached internally).
    if ctx.news is not None:
        try:
            ctx.news.refresh(force=False)
        except Exception as exc:
            log.warning("News refresh failed: %s", exc)

    outcomes: Dict[str, Any] = {"connected": connected, "mode": ctx.cfg.get_path("general.mode", "paper"), "symbols": {}}
    for symbol in _symbols(ctx):
        ohlcv = _load_history(ctx, symbol, tf)
        if len(ohlcv) < 100:
            log.warning("Not enough data for %s (%d bars); skipping.",
                        symbol, len(ohlcv))
            outcomes["symbols"][symbol] = {"skipped": "insufficient_data"}
            continue

        decision = ctx.engine.decide(ohlcv, symbol, tf)
        # ATR for SL/TP placement (reuse a Strategy just for its ATR helper).
        atr = None
        try:
            from core.indicators.volatility import ATR
            atr_res = ATR(params={"period": 14}).compute(ohlcv)
            atr = atr_res.last("atr")
        except Exception:
            atr = None

        exec_result = ctx.orders.execute(
            decision, symbol, atr or 0.0, ohlcv.close[-1]
        )
        news_summary = ctx.news.summary(symbol) if ctx.news is not None else {}
        outcomes["symbols"][symbol] = {
            "decision": decision.to_dict(),
            "execution": exec_result,
            "news": news_summary,
        }
        log.info(
            "%s decision=%s exec=%s",
            symbol, decision.to_dict(), exec_result.get("action"),
        )

    ctx.shutdown()
    return outcomes


def run_loop(ctx: BotContext, iterations: int = 0, sleep_seconds: int = 60) -> None:
    """
    Repeatedly run_once with a sleep between passes. iterations=0 means run
    forever (until interrupted). Intended for the run_bot script / VPS use.
    """
    log = get_logger("app.runners.loop", ctx.cfg)
    count = 0
    log.info("Starting trade loop (iterations=%s, sleep=%ss).",
             iterations or "infinite", sleep_seconds)
    try:
        while True:
            # Rebuild a fresh context each pass so config edits and new data are
            # picked up, and to avoid holding an MT5 connection during sleep.
            fresh = BotContext()
            run_once(fresh)
            count += 1
            if iterations and count >= iterations:
                log.info("Reached %d iterations; stopping loop.", iterations)
                break
            time.sleep(max(1, sleep_seconds))
    except KeyboardInterrupt:
        log.info("Trade loop interrupted by user; exiting cleanly.")


# --------------------------------------------------------------------------- #
# Dispatch by mode.
# --------------------------------------------------------------------------- #
def dispatch(mode: str, config_path: Optional[str] = None) -> Any:
    """Build a context and run the requested mode once."""
    ctx = BotContext(config_path)
    mode = (mode or ctx.cfg.get_path("general.mode", "paper")).lower()
    log = get_logger("app.runners", ctx.cfg)
    log.info("Dispatching mode: %s", mode)

    if mode == "train":
        return run_train(ctx)
    if mode == "search":
        return run_search(ctx)
    if mode == "backtest":
        return run_backtest(ctx)
    if mode in ("paper", "live"):
        return run_once(ctx)
    log.error("Unknown mode '%s'. Use train/search/backtest/paper/live.", mode)
    return {"error": "unknown_mode", "mode": mode}
