"""
CompositeStrategy - a Strategy-compatible adapter over the ENGINE BLEND
(UPGRADE_PLAN U2.5).

Diagnosis D2 in UPGRADE_PLAN.md says the live/paper "blend" path trades a
composite that was never walk-forward validated: the average of the top-K
registry strategies' continuous signals, thresholded by the GLOBAL
`decision.long_threshold` / `short_threshold`. Parity mode (U2.4) sidesteps
that by trading a single validated strategy. But if a user deliberately keeps
`decision.mode: "blend"` for research, that composite MUST still be validatable.

This module exposes exactly that composite as an object the existing
`Backtester` can score, so `scripts/validate_ensemble.py` can replay the blend
through the same pessimistic simulator and emit the same U1 receipts. It
reproduces ONLY the memory-ensemble portion of the engine blend (the part that
depends purely on price): the equal/council-free average of the top-K strategy
signals, with the engine's global thresholds and a weighted-average SL/TP.

It intentionally does NOT fold in the ML learner or news, because those are not
price-only functions of the OHLCV window and cannot be replayed bar-by-bar in a
pure-offline backtest without lookahead risk. The learner/news contribution is
therefore reported as an explicit caveat by the script, not silently ignored.

Pure Python / stdlib only; Windows 7 + Python 3.8 friendly.

All text is standard ASCII English only.
"""

from __future__ import annotations

from typing import Any, List, Optional

from core.strategy.strategy import Strategy, StrategySpec


class _CompositeSpec(object):
    """
    Minimal spec shim so the Backtester can read symbol + weighted SL/TP.

    The Backtester only touches `spec.symbol`, `spec.sl_atr_mult`,
    `spec.tp_atr_mult` (and, for receipts, tolerates a fingerprint()), so this
    lightweight object is enough - we do not want a full StrategySpec because
    the composite has no single indicator set.
    """

    def __init__(self, symbol: str, timeframe: str,
                 sl_atr_mult: float, tp_atr_mult: float,
                 long_threshold: float, short_threshold: float):
        self.symbol = symbol
        self.timeframe = timeframe
        self.sl_atr_mult = float(sl_atr_mult)
        self.tp_atr_mult = float(tp_atr_mult)
        self.long_threshold = float(long_threshold)
        self.short_threshold = float(short_threshold)

    def fingerprint(self) -> str:
        return "composite:%s|%s" % (self.symbol, self.timeframe)


class CompositeStrategy(object):
    """
    Strategy-compatible object whose per-bar signal is the ENGINE-BLEND average
    of the top-K validated strategies, thresholded by the global decision
    thresholds. Drop-in for `Backtester.run(strategy=...)`.
    """

    def __init__(self, strategies: List[Strategy], symbol: str, timeframe: str,
                 long_threshold: float, short_threshold: float):
        # Filter to strategies that actually built at least one indicator.
        self._strategies = [s for s in strategies if s is not None]
        # Weighted-average SL/TP across the ensemble (matches the engine, which
        # averages each strategy's own sl/tp mults). Empty -> safe defaults.
        if self._strategies:
            n = float(len(self._strategies))
            sl = sum(float(s.spec.sl_atr_mult) for s in self._strategies) / n
            tp = sum(float(s.spec.tp_atr_mult) for s in self._strategies) / n
        else:
            sl, tp = 2.0, 3.0
        self.spec = _CompositeSpec(
            symbol=symbol, timeframe=timeframe,
            sl_atr_mult=sl, tp_atr_mult=tp,
            long_threshold=long_threshold, short_threshold=short_threshold,
        )
        # Reuse the first strategy's ATR helper for SL/TP placement; if the
        # ensemble is empty, build a stand-alone ATR via a throwaway Strategy.
        if self._strategies:
            self._atr_source = self._strategies[0]
        else:
            self._atr_source = Strategy(StrategySpec(
                indicators={}, weights={}, symbol=symbol, timeframe=timeframe))

    # ------------------------------------------------------------------ #
    # Strategy-compatible surface used by the Backtester.
    # ------------------------------------------------------------------ #
    def signal_series(self, ohlcv: Any) -> List[float]:
        """Per-bar ENGINE-BLEND signal: plain average of the top-K signals."""
        n = len(ohlcv.close) if hasattr(ohlcv, "close") else 0
        if n == 0 or not self._strategies:
            return [0.0] * n
        acc = [0.0] * n
        used = 0
        for strat in self._strategies:
            try:
                series = strat.signal_series(ohlcv)
            except Exception:
                continue
            used += 1
            for i in range(n):
                acc[i] += series[i] if i < len(series) else 0.0
        if used == 0:
            return [0.0] * n
        out: List[float] = [0.0] * n
        for i in range(n):
            v = acc[i] / used
            out[i] = 1.0 if v > 1.0 else (-1.0 if v < -1.0 else v)
        return out

    def decision_series(self, ohlcv: Any) -> List[int]:
        """Apply the GLOBAL long/short thresholds to the blended signal."""
        sig = self.signal_series(ohlcv)
        lt = self.spec.long_threshold
        st = self.spec.short_threshold
        out: List[int] = [0] * len(sig)
        for i, s in enumerate(sig):
            if s >= lt:
                out[i] = 1
            elif s <= -st:
                out[i] = -1
        return out

    def atr_series(self, ohlcv: Any) -> List[Optional[float]]:
        return self._atr_source.atr_series(ohlcv)

    def atr_value(self, ohlcv: Any) -> Optional[float]:
        return self._atr_source.atr_value(ohlcv)
