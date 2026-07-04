"""
Strategy / parameter search (Phase 3) - the memory builder.

Generates many candidate StrategySpecs by sampling indicator parameters and
blend weights, evaluates each via walk-forward backtesting, and persists every
result to the MemoryStore. After the search, it updates the JSON registry with
the top strategies per symbol/timeframe.

Two methods (config.memory.search.method):
  - "random" : sample max_trials random specs from the indicator param spaces.
  - "grid"   : enumerate a (bounded) grid over a small chosen indicator subset.

This is the realistic "learn from trial-and-error on years of data" loop: the
more it searches and stores, the better future strategy selection becomes. It
never rewrites source code.

All text is standard ASCII English only.
"""

from __future__ import annotations

import itertools
import random
from typing import Any, Dict, List, Optional

from core.indicators.registry import get_indicator_class, list_indicators
from core.strategy.strategy import StrategySpec
from core.strategy.walk_forward import WalkForward
from core.utils.logger import get_logger


# Indicators that vote on direction (ATR is excluded: it is non-directional).
_DIRECTIONAL = [
    "sma", "ema", "macd", "adx", "ichimoku", "supertrend",
    "rsi", "stoch", "cci", "williams_r", "roc",
    "bbands", "keltner", "donchian", "obv", "mfi", "vwap",
    "candle_patterns",
]


class StrategySearch(object):
    def __init__(self, cfg: Any, memory: object,
                 time_stats: Optional[object] = None):
        self.cfg = cfg
        self.log = get_logger("strategy.search", cfg)
        self.memory = memory
        # Phase 5 (user-update-request): pass the TimeStats down so every
        # out-of-sample trade also teaches the time/session/season layer.
        self.time_stats = time_stats
        self.wf = WalkForward(cfg, memory, time_stats=time_stats)
        s = cfg.get_path("memory.search", {})
        self.method = s.get("method", "random") if hasattr(s, "get") else "random"
        self.max_trials = int(s.get("max_trials", 200)) if hasattr(s, "get") else 200
        self.rank_metric = s.get("rank_metric", "expectancy") if hasattr(s, "get") else "expectancy"
        self.min_trades = int(s.get("min_trades", 30)) if hasattr(s, "get") else 30

    # ------------------------------------------------------------------ #
    def _available_directional(self) -> List[str]:
        registered = set(list_indicators())
        return [n for n in _DIRECTIONAL if n in registered]

    def _random_params(self, indicator_name: str) -> Dict[str, Any]:
        """Sample one parameter set for an indicator from its param_space."""
        cls = get_indicator_class(indicator_name)
        space = cls.param_space()
        if not space:
            return dict(cls.default_params())
        return {k: random.choice(v) for k, v in space.items()}

    def _random_spec(self, symbol: str, timeframe: str) -> StrategySpec:
        """Build one random strategy spec."""
        pool = self._available_directional()
        # Choose between 2 and 5 indicators to combine.
        k = random.randint(2, min(5, len(pool)))
        chosen = random.sample(pool, k)
        indicators: Dict[str, Dict[str, Any]] = {}
        weights: Dict[str, float] = {}
        for name in chosen:
            indicators[name] = self._random_params(name)
            weights[name] = round(random.uniform(0.5, 2.0), 2)
        long_thr = round(random.uniform(0.15, 0.5), 2)
        short_thr = round(random.uniform(0.15, 0.5), 2)
        sl = round(random.uniform(1.0, 3.0), 1)
        tp = round(random.uniform(1.5, 5.0), 1)
        return StrategySpec(
            indicators=indicators, weights=weights,
            long_threshold=long_thr, short_threshold=short_thr,
            sl_atr_mult=sl, tp_atr_mult=tp,
            symbol=symbol, timeframe=timeframe,
        )

    # ------------------------------------------------------------------ #
    def run(self, ohlcv: Any, symbol: str, timeframe: str,
            point: Optional[float] = None) -> Dict[str, Any]:
        """
        Run the configured search over the OHLCV history, persisting every
        result. Returns a summary dict including the updated registry section.
        """
        self.log.info(
            "Starting %s search: up to %d trials on %s %s (%d bars).",
            self.method, self.max_trials, symbol, timeframe, len(ohlcv.close),
        )
        seen = set()
        evaluated = 0
        # Locked holdout gate (A2 / P1.4): when memory.walk_forward.holdout_bars
        # > 0, only specs that ALSO pass on the untouched holdout tail may enter
        # the registry. We collect the passing fingerprints here and pass them
        # as an allowlist to update_registry. When the holdout is OFF the gate is
        # a no-op and allowed stays None (no filtering), keeping old behavior.
        holdout_on = int(getattr(self.wf, "holdout_bars", 0)) > 0
        allowed_fps = set() if holdout_on else None

        if self.method == "grid":
            specs = self._grid_specs(symbol, timeframe)
        else:
            specs = (self._random_spec(symbol, timeframe)
                     for _ in range(self.max_trials))

        for spec in specs:
            if evaluated >= self.max_trials:
                break
            fp = spec.fingerprint()
            if fp in seen:
                continue
            seen.add(fp)
            try:
                self.wf.evaluate(spec, ohlcv, point=point, persist=True)
                evaluated += 1
                if holdout_on:
                    gate = self.wf.evaluate_holdout(spec, ohlcv, point=point)
                    if gate.get("passed"):
                        allowed_fps.add(fp)
                if evaluated % 25 == 0:
                    self.log.info("  evaluated %d strategies...", evaluated)
            except Exception as exc:
                self.log.error("Evaluation failed for %s: %s", fp, exc)

        if holdout_on:
            self.log.info(
                "Holdout gate: %d of %d evaluated specs passed the locked holdout.",
                len(allowed_fps), evaluated,
            )
        section = self.memory.update_registry(
            symbol, timeframe, rank_metric=self.rank_metric,
            min_trades=self.min_trades, allowed_fingerprints=allowed_fps,
        )
        self.log.info(
            "Search complete: %d strategies evaluated; %d in registry top.",
            evaluated, len(section.get("top", [])),
        )
        return {"evaluated": evaluated, "registry": section}

    # ------------------------------------------------------------------ #
    def _grid_specs(self, symbol: str, timeframe: str):
        """
        Enumerate a bounded grid over a small, fixed indicator combo
        (ema + rsi + atr-based exits). Kept small to remain CPU-friendly.
        """
        ema_periods = [12, 21, 34]
        rsi_periods = [7, 14, 21]
        long_thrs = [0.2, 0.3, 0.4]
        sls = [1.5, 2.0]
        tps = [2.0, 3.0]
        for ep, rp, lt, sl, tp in itertools.product(
            ema_periods, rsi_periods, long_thrs, sls, tps
        ):
            indicators = {
                "ema": {"period": ep},
                "rsi": {"period": rp},
            }
            weights = {"ema": 1.0, "rsi": 1.0}
            yield StrategySpec(
                indicators=indicators, weights=weights,
                long_threshold=lt, short_threshold=lt,
                sl_atr_mult=sl, tp_atr_mult=tp,
                symbol=symbol, timeframe=timeframe,
            )
