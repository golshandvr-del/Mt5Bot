"""
Base classes and the common interface for the pluggable indicator layer.

Every indicator subclasses Indicator and implements compute(ohlcv) -> dict of
named output series (each a list aligned to the input bars; leading values that
cannot be computed are filled with None).

Indicators also expose a signal(ohlcv) helper returning a single float in
[-1, +1] for the most recent bar:
    +1 = strong bullish, 0 = neutral, -1 = strong bearish.
The decision engine blends these per-indicator signals.

All math is implemented in pure Python so the bot does not require numpy or
pandas at runtime, though they are used when present for speed/convenience.

All text is standard ASCII English only.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class IndicatorResult(dict):
    """
    Container for indicator output. It is a plain dict of {series_name: list},
    with a convenience .last(name) accessor for the most recent value.
    """

    def last(self, name: str) -> Optional[float]:
        series = self.get(name)
        if not series:
            return None
        # Return the last non-None value from the end.
        for value in reversed(series):
            if value is not None:
                return value
        return None


class Indicator:
    """
    Base class for all indicators.

    Subclasses set:
      name        : unique short string used in config and the registry.
      category    : one of "trend", "momentum", "volatility", "volume",
                    "pattern".
    and implement compute(ohlcv) and signal(ohlcv).
    """

    name: str = "base"
    category: str = "generic"

    def __init__(self, params: Optional[Dict[str, Any]] = None):
        # Merge provided params over the subclass default_params().
        self.params: Dict[str, Any] = dict(self.default_params())
        if params:
            self.params.update(params)

    @classmethod
    def default_params(cls) -> Dict[str, Any]:
        """Override to provide default parameters for the indicator."""
        return {}

    @classmethod
    def param_space(cls) -> Dict[str, List[Any]]:
        """
        Override to declare the search space used by the Phase 3 strategy
        search. Maps parameter name -> list of candidate values.
        Empty means the indicator has no tunable parameters.
        """
        return {}

    def compute(self, ohlcv: Any) -> IndicatorResult:
        """Compute output series. Must be overridden."""
        raise NotImplementedError

    def signal(self, ohlcv: Any) -> float:
        """
        Return a single signal in [-1, +1] for the latest bar.

        The default implementation computes the indicator series ONCE and reads
        the signal at the last bar via _signal_at(). Subclasses normally only
        override compute() and _signal_at(); overriding signal() directly is
        also allowed for special cases (e.g. ATR which is non-directional).
        """
        n = len(ohlcv.close) if hasattr(ohlcv, "close") else 0
        if n == 0:
            return 0.0
        try:
            result = self.compute(ohlcv)
        except Exception:
            return 0.0
        return self._signal_at(result, ohlcv, n - 1)

    def safe_signal(self, ohlcv: Any) -> float:
        """
        Phase 5: robust wrapper around signal() with health guards.

        Returns a neutral 0.0 (instead of noise) when the input is degenerate:
          - too few bars to be meaningful,
          - a flat/constant close series (no information),
          - a non-finite (NaN/inf) signal value.
        This protects the decision blend from garbage-in situations while never
        raising. Callers that want raw behaviour can still use signal() directly.
        """
        close = getattr(ohlcv, "close", None)
        n = len(close) if close is not None else 0
        if n < 2:
            return 0.0
        # Detect a completely flat series (all values equal) -> no information.
        first = close[0]
        if all(abs(c - first) < 1e-12 for c in close):
            return 0.0
        try:
            value = float(self.signal(ohlcv))
        except Exception:
            return 0.0
        # Guard against NaN / inf (NaN != NaN is the classic test).
        if value != value or value in (float("inf"), float("-inf")):
            return 0.0
        return max(-1.0, min(1.0, value))

    def _signal_at(self, result: "IndicatorResult", ohlcv: Any, i: int) -> float:
        """
        Return the signal in [-1, +1] for bar index i, given a PRE-COMPUTED
        IndicatorResult (the full series). This is the single place each
        indicator encodes its trading interpretation, so it can be reused both
        for the latest-bar signal() and for the whole-history signal_series()
        without recomputing the series per bar.

        Default is neutral (0.0). Directional indicators override this.
        """
        return 0.0

    def signal_series(self, ohlcv: Any) -> List[float]:
        """
        Return a per-bar signal series in [-1, +1] aligned to the input bars.

        This is the FAST path used by the backtester and walk-forward: it calls
        compute() exactly once (O(n)) and then evaluates _signal_at() for every
        bar (O(n)), turning what used to be an O(n^2) growing-window loop into a
        linear pass. Numerically identical to calling signal() on each prefix
        because every indicator's interpretation depends only on the values of
        its own output series up to bar i (and the close at bar i).
        """
        n = len(ohlcv.close) if hasattr(ohlcv, "close") else 0
        out: List[float] = [0.0] * n
        if n == 0:
            return out
        try:
            result = self.compute(ohlcv)
        except Exception:
            return out
        for i in range(n):
            try:
                out[i] = self._signal_at(result, ohlcv, i)
            except Exception:
                out[i] = 0.0
        return out

    # ------------------------------------------------------------------ #
    # Small helper: last non-None value in a series at or before index i.
    # ------------------------------------------------------------------ #
    @staticmethod
    def _at(series: Optional[List[Optional[float]]], i: int) -> Optional[float]:
        """Value of a series at index i (None-safe, bounds-safe)."""
        if not series or i < 0 or i >= len(series):
            return None
        return series[i]

    # ------------------------------------------------------------------ #
    # Shared numeric helpers (pure Python).
    # ------------------------------------------------------------------ #
    @staticmethod
    def _sma(values: List[float], period: int) -> List[Optional[float]]:
        out: List[Optional[float]] = [None] * len(values)
        if period <= 0:
            return out
        running = 0.0
        for i, v in enumerate(values):
            running += v
            if i >= period:
                running -= values[i - period]
            if i >= period - 1:
                out[i] = running / period
        return out

    @staticmethod
    def _ema(values: List[float], period: int) -> List[Optional[float]]:
        out: List[Optional[float]] = [None] * len(values)
        if period <= 0 or len(values) == 0:
            return out
        alpha = 2.0 / (period + 1.0)
        ema: Optional[float] = None
        for i, v in enumerate(values):
            if ema is None:
                # Seed with the first value (common, stable choice).
                ema = v
            else:
                ema = alpha * v + (1.0 - alpha) * ema
            # Only emit once we have at least `period` samples for stability.
            if i >= period - 1:
                out[i] = ema
        return out

    @staticmethod
    def _rolling_std(values: List[float], period: int) -> List[Optional[float]]:
        out: List[Optional[float]] = [None] * len(values)
        if period <= 1:
            return out
        for i in range(len(values)):
            if i >= period - 1:
                window = values[i - period + 1: i + 1]
                mean = sum(window) / period
                var = sum((x - mean) ** 2 for x in window) / period
                out[i] = var ** 0.5
        return out

    @staticmethod
    def _true_range(high: List[float], low: List[float],
                    close: List[float]) -> List[float]:
        tr: List[float] = [0.0] * len(close)
        for i in range(len(close)):
            if i == 0:
                tr[i] = high[i] - low[i]
            else:
                tr[i] = max(
                    high[i] - low[i],
                    abs(high[i] - close[i - 1]),
                    abs(low[i] - close[i - 1]),
                )
        return tr

    @staticmethod
    def _wilder_smooth(values: List[float], period: int) -> List[Optional[float]]:
        """Wilder's smoothing (used by ATR, ADX, RSI)."""
        out: List[Optional[float]] = [None] * len(values)
        if period <= 0 or len(values) < period:
            return out
        # First value is a simple average of the first `period` items.
        first = sum(values[:period]) / period
        out[period - 1] = first
        prev = first
        for i in range(period, len(values)):
            prev = (prev * (period - 1) + values[i]) / period
            out[i] = prev
        return out
