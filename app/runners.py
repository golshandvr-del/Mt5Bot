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
from core.learning.factory import build_active_model
from core.strategy.strategy import Strategy, StrategySpec
from core.strategy.backtester import Backtester
from core.strategy.search import StrategySearch
from core.utils.helpers import write_json
from core.utils.logger import get_logger
from core.utils import trade_log


# --------------------------------------------------------------------------- #
# Helpers shared by runners.
# --------------------------------------------------------------------------- #
def _symbols(ctx: BotContext) -> List[str]:
    # This bot is dedicated to XAUUSD, so the safety fallback (used only if
    # mt5.symbols is missing/empty in config) is XAUUSD - never a random FX pair.
    syms = ctx.cfg.get_path("mt5.symbols", ["XAUUSD"])
    return [str(s) for s in syms] if syms else ["XAUUSD"]


def _timeframe(ctx: BotContext) -> str:
    return str(ctx.cfg.get_path("mt5.timeframe", "M15"))


def _load_history(ctx: BotContext, symbol: str, timeframe: str,
                  count: Optional[int] = None) -> OHLCV:
    """Load OHLCV via the data feed (live MT5 first, then CSV fallback)."""
    return ctx.data_feed.get_ohlcv(symbol, timeframe, count)


def _per_symbol_model_file(base_model_file: str, symbol: str) -> str:
    """
    Derive a per-symbol model filename from the shared model_file (A5 / P3.3).

    Inserts the symbol before the extension so a shared "models/ml_classifier.pkl"
    becomes "models/ml_classifier_<SYMBOL>.pkl". The symbol is sanitized to keep
    only ASCII letters/digits/_/-/. so a broker symbol like "EURUSD.m" yields a
    safe filename. Falls back to appending when there is no extension.
    """
    safe = "".join(
        ch for ch in str(symbol) if ch.isalnum() or ch in ("_", "-", ".")
    ) or "SYMBOL"
    root, ext = os.path.splitext(base_model_file)
    if ext:
        return "%s_%s%s" % (root, safe, ext)
    return "%s_%s" % (base_model_file, safe)


# --------------------------------------------------------------------------- #
# TRAIN (Phase 1)
# --------------------------------------------------------------------------- #
def run_train(ctx: BotContext) -> Dict[str, Any]:
    """
    Train the active learner offline on historical bars and save it.

    Two modes, chosen by `learning.per_symbol` (A5 / P3.3, default false):
      - per_symbol=false (default): the original SHARED-model behavior. Train on
        the first symbol with enough data and persist a single model file
        (byte-identical to before).
      - per_symbol=true: train a SEPARATE learner per symbol so, e.g., XAUUSD
        does not dilute EURUSD, and save each as
        `models/<model>_<SYMBOL>.pkl` (see _per_symbol_model_file). The engine's
        per-symbol lookup is wired in P3.4; this sub-step only produces the
        per-symbol model files.
    """
    log = get_logger("app.runners.train", ctx.cfg)
    ctx.connect_mt5()  # optional; CSV works too
    tf = _timeframe(ctx)
    name = ctx.cfg.get_path("learning.active_model", "ml_classifier")
    model_file = ctx.cfg.get_path("learning.%s.model_file" % name,
                                  "models/%s.pkl" % name)

    # Read the per-symbol toggle defensively so a bad/missing value degrades to
    # the safe shared-model path (config.yaml key + docs land in P3.4).
    per_symbol = bool(ctx.cfg.get_path("learning.per_symbol", False))

    if per_symbol:
        return _run_train_per_symbol(ctx, log, tf, name, model_file)

    out_path = resolve_path(ctx.cfg, model_file)
    results: Dict[str, Any] = {"model": name, "per_symbol": False,
                               "trained_symbols": []}
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
        # Phase 5: pass feature names for importance export when supported.
        if hasattr(ctx.learner, "set_feature_names"):
            try:
                ctx.learner.set_feature_names(feat_names)
            except Exception:
                pass
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


def _run_train_per_symbol(ctx: BotContext, log: Any, tf: str, name: str,
                          model_file: str) -> Dict[str, Any]:
    """
    Train and persist one learner PER symbol (A5 / P3.3).

    A fresh learner is built for each symbol via the factory so their fitted
    state never mixes, and each is saved to `_per_symbol_model_file`. The shared
    `ctx.learner` is intentionally NOT reused here so the default light path and
    any already-loaded shared model stay untouched. Degrades gracefully: a symbol
    with too little data is skipped, not fatal.
    """
    results: Dict[str, Any] = {"model": name, "per_symbol": True,
                               "trained_symbols": [], "saved": False,
                               "model_files": {}}
    any_saved = False
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
        # Build a dedicated learner for THIS symbol only.
        learner = build_active_model(ctx.cfg)
        log.info("Training per-symbol %s on %s with %d samples...",
                 name, symbol, len(X))
        if hasattr(learner, "set_feature_names"):
            try:
                learner.set_feature_names(feat_names)
            except Exception:
                pass
        learner.fit(X, y)
        entry = {"symbol": symbol, "samples": len(X), "saved": False}
        if learner.is_ready():
            sym_file = _per_symbol_model_file(model_file, symbol)
            out_path = resolve_path(ctx.cfg, sym_file)
            ok = bool(learner.save(out_path))
            entry["saved"] = ok
            entry["model_file"] = out_path
            results["model_files"][symbol] = out_path
            any_saved = any_saved or ok
            log.info("Saved per-symbol model for %s: %s (%s)",
                     symbol, out_path, ok)
        else:
            log.warning("Learner not ready for %s; nothing saved.", symbol)
        results["trained_symbols"].append(entry)

    results["saved"] = any_saved
    if not any_saved:
        log.warning("Per-symbol training saved no models "
                    "(insufficient data for every symbol?).")
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
    # Phase 5 (user-update-request): when timing is enabled, feed the TimeStats
    # into the search so every out-of-sample trade also teaches the time/session/
    # season layer. When timing is disabled, ctx.timing is None and search runs
    # exactly as before (light path unchanged).
    time_stats = ctx.time_stats if ctx.timing is not None else None
    search = StrategySearch(ctx.cfg, ctx.memory, time_stats=time_stats)

    summary: Dict[str, Any] = {"symbols": {}}
    for symbol in _symbols(ctx):
        ohlcv = _load_history(ctx, symbol, tf)
        if len(ohlcv) < 500:
            log.warning("Not enough data for %s (%d bars); skipping search.",
                        symbol, len(ohlcv))
            continue
        log.info("Searching strategies for %s %s ...", symbol, tf)
        res = search.run(ohlcv, symbol, tf)
        entry = {
            "evaluated": res.get("evaluated", 0),
            "top": len(res.get("registry", {}).get("top", [])),
        }
        if time_stats is not None:
            entry["time_stats"] = time_stats.summary(symbol, tf)
        summary["symbols"][symbol] = entry
    summary["memory_stats"] = ctx.memory.stats()
    log.info("Search finished. Memory now holds: %s", summary["memory_stats"])
    ctx.shutdown()
    return summary


# --------------------------------------------------------------------------- #
# REBUILD-REGISTRY (recovery)
# --------------------------------------------------------------------------- #
def run_rebuild_registry(ctx: BotContext,
                         min_trades_override: Optional[int] = None,
                         disable_significance: bool = False,
                         max_pvalue_override: Optional[float] = None
                         ) -> Dict[str, Any]:
    """
    Rebuild strategy_registry.json from EXISTING stored results, no search.

    This is the recovery path when a completed search stored thousands of
    results in SQLite but the registry came out empty. Two distinct causes have
    been seen and are both handled here:

      1. "no such function: json_extract" - the ranking query itself failed on a
         SQLite build without the JSON1 extension (now fixed by a Python shim).
      2. Over-strict promotion filters - the query runs fine, but EVERY strategy
         is dropped by ``min_trades`` and/or the statistical-significance gate,
         so ``top`` stays 0 even though good candidates exist in the DB.

    To recover from (2) without editing config, callers may override the gates:
      * ``min_trades_override``   - use this min-trades instead of config.
      * ``disable_significance``  - ignore the p-value / CI significance gate.
      * ``max_pvalue_override``   - loosen the significance p-value threshold.

    When a pair still yields 0 strategies, this logs a data-driven REASON (which
    filter emptied it) so the user knows exactly which knob to turn.

    It does NOT connect to MT5 and does NOT load any price history: it only
    reads the memory DB. The (symbol, timeframe) pairs come from the DB itself
    (via known_symbol_timeframes), so it works even if config.mt5.symbols has
    since changed.
    """
    log = get_logger("app.runners.rebuild_registry", ctx.cfg)
    s = ctx.cfg.get_path("memory.search", {})
    rank_metric = s.get("rank_metric", "expectancy") if hasattr(s, "get") else "expectancy"
    try:
        cfg_min_trades = int(s.get("min_trades", 30)) if hasattr(s, "get") else 30
    except (TypeError, ValueError):
        cfg_min_trades = 30
    min_trades = cfg_min_trades if min_trades_override is None else int(min_trades_override)

    # Optionally loosen the significance gate for this recovery run only.
    if max_pvalue_override is not None:
        try:
            ctx.memory.sig_max_pvalue = float(max_pvalue_override)
        except (TypeError, ValueError):
            pass
    apply_significance = not disable_significance

    log.info("Rebuild settings: rank_metric=%s min_trades=%d "
             "significance=%s max_pvalue=%s",
             rank_metric, min_trades,
             "ON" if apply_significance else "OFF",
             getattr(ctx.memory, "sig_max_pvalue", "n/a"))

    pairs = ctx.memory.known_symbol_timeframes()
    log.info("Rebuilding registry from memory for %d symbol/timeframe pair(s).",
             len(pairs))
    summary: Dict[str, Any] = {"rebuilt": {}}
    for pair in pairs:
        symbol = pair["symbol"]
        tf = pair["timeframe"]
        section = ctx.memory.update_registry(
            symbol, tf, rank_metric=rank_metric, min_trades=min_trades,
            apply_significance=apply_significance,
        )
        n_top = len(section.get("top", []))
        summary["rebuilt"]["%s|%s" % (symbol, tf)] = {"top": n_top}
        log.info("Rebuilt %s %s -> %d strategy(ies) in registry top.",
                 symbol, tf, n_top)
        if n_top == 0:
            _log_empty_reason(ctx, log, symbol, tf, rank_metric, min_trades,
                              apply_significance)

    summary["memory_stats"] = ctx.memory.stats()
    log.info("Registry rebuild finished. Memory holds: %s",
             summary["memory_stats"])
    return summary


def _log_empty_reason(ctx: BotContext, log, symbol: str, timeframe: str,
                      rank_metric: str, min_trades: int,
                      apply_significance: bool) -> None:
    """Explain, from the data, why a pair produced 0 promoted strategies."""
    try:
        # Raw ranking with significance OFF and min_trades=1 shows how many
        # candidates exist before the gates, so we can attribute the loss.
        raw = ctx.memory.top_strategies(
            symbol, timeframe, k=10_000, rank_metric=rank_metric,
            min_trades=1, apply_significance=False,
        )
        n_raw = len(raw)
        n_trades_ok = sum(
            1 for e in raw
            if e.get("avg_trades") is not None and e["avg_trades"] >= min_trades
        )
        if n_raw == 0:
            log.warning("  REASON %s %s: no ranked candidates at all (check "
                        "that rank_metric='%s' matches what the search stored).",
                        symbol, timeframe, rank_metric)
        elif n_trades_ok == 0:
            best = max((e.get("avg_trades") or 0) for e in raw)
            log.warning("  REASON %s %s: all %d candidates average < "
                        "min_trades=%d (best avg_trades=%.1f). Re-run with "
                        "--min-trades <=%d to recover them.",
                        symbol, timeframe, n_raw, min_trades, best, int(best))
        elif apply_significance:
            log.warning("  REASON %s %s: %d candidate(s) pass min_trades but the "
                        "significance gate rejected them all (p-value too high "
                        "or missing). Re-run with --no-significance (or "
                        "--max-pvalue 0.2) to recover them.",
                        symbol, timeframe, n_trades_ok)
        else:
            log.warning("  REASON %s %s: unexpected empty result despite %d "
                        "trade-eligible candidates.", symbol, timeframe,
                        n_trades_ok)
    except Exception as exc:  # pragma: no cover - diagnostics must never crash
        log.error("  could not compute empty-registry reason: %s", exc)


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

        # U1.1/U1.2: record FULL per-trade receipts and write the audit CSVs
        # (per-trade + equity curve) so every backtest is inspectable.
        result = bt.run(Strategy(spec), ohlcv, warmup=60, record_trades=True)
        artifacts = trade_log.write_artifacts(
            result, symbol, tf, report_dir=report_dir)
        if artifacts.get("trades") is None:
            log.warning("Could not write trade CSV for %s", symbol)
        if artifacts.get("equity") is None:
            log.warning("Could not write equity CSV for %s", symbol)
        report["symbols"][symbol] = {
            "source": source,
            "spec": spec.to_dict(),
            "metrics": result.metrics,
            "num_trades": len(result.trade_pnls),
            "artifacts": artifacts,
        }
        log.info("Backtest %s (%s): %s", symbol, source, result.metrics)

    # U1.5: attach the exact effective config values used so every report is
    # reproducible from its own contents.
    report["config_snapshot"] = _backtest_config_snapshot(ctx.cfg)

    # Save the report as JSON.
    out = os.path.join(report_dir, "backtest_report.json")
    write_json(out, report)
    report["report_file"] = out
    log.info("Backtest report saved to %s", out)
    ctx.shutdown()
    return report


def _backtest_config_snapshot(cfg: Any) -> Dict[str, Any]:
    """
    Collect the effective config values that shape a backtest (U1.5).

    This makes each report self-describing: the user can see the exact costs,
    sizing and risk settings that produced the numbers without re-reading the
    live config.yaml (which may have changed since). Read defensively so a
    missing block never breaks the report.
    """
    def gp(path: str, default: Any) -> Any:
        try:
            return cfg.get_path(path, default)
        except Exception:
            return default

    return {
        "initial_balance": gp("backtest.initial_balance", 10000.0),
        "spread_points": gp("backtest.spread_points", 10),
        "commission_per_lot": gp("backtest.commission_per_lot", 7.0),
        "slippage_points": gp("backtest.slippage_points", 2),
        "fixed_lot": gp("backtest.fixed_lot", 0.10),
        "swap_long_pts": gp("backtest.swap_long_pts", 0.0),
        "swap_short_pts": gp("backtest.swap_short_pts", 0.0),
        "swap_triple_day": gp("backtest.swap_triple_day", 2),
        "model_weekend_gap": gp("backtest.model_weekend_gap", False),
        "risk_default_sl_atr_mult": gp("risk.default_sl_atr_mult", 2.0),
        "risk_default_tp_atr_mult": gp("risk.default_tp_atr_mult", 3.0),
    }


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
        # U1.4 transparency: append one JSON line per decision to
        # logs/decisions_<date>.jsonl so every paper/live decision is auditable.
        try:
            from core.utils import decision_log
            comps = getattr(decision, "components", {}) or {}
            thresholds = {
                "long": float(comps.get("_threshold_long", 0.0)),
                "short": float(comps.get("_threshold_short", 0.0)),
            }
            log_dir = resolve_path(ctx.cfg,
                                   ctx.cfg.get_path("logging.log_dir", "logs"))
            decision_log.append_decision(decision, symbol, tf,
                                         thresholds=thresholds,
                                         log_dir=log_dir)
        except Exception as exc:
            log.warning("Decision journal append failed for %s: %s",
                        symbol, exc)
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
