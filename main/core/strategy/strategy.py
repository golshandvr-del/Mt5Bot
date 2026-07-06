"""
Strategy definition (Phase 3).

A Strategy is a reproducible, serializable recipe that converts market data into
a directional signal. It is defined by a StrategySpec:
  - which indicators are active and with what parameters,
  - the per-indicator blend weights,
  - long/short thresholds,
  - risk parameters (SL/TP in ATR multiples).

The strategy search (search.py) generates many StrategySpecs, the backtester
scores them, and the best ones are persisted in the memory store. At live time
the decision engine can load a blended ensemble of the top strategies.

StrategySpec is intentionally a plain dict-friendly object so it serializes to
JSON / SQLite cleanly.

All text is standard ASCII English only.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from core.indicators.registry import get_indicator_class
from core.indicators.volatility import ATR


class StrategySpec(object):
    """Serializable description of a strategy."""

    def __init__(
        self,
        indicators: Dict[str, Dict[str, Any]],
        weights: Dict[str, float],
        long_threshold: float = 0.3,
        short_threshold: float = 0.3,
        sl_atr_mult: float = 2.0,
        tp_atr_mult: float = 3.0,
        symbol: str = "",
        timeframe: str = "",
        name: str = "",
    ):
        # indicators: {indicator_name: {param: value, ...}}
        self.indicators = indicators
        # weights: {indicator_name: weight}; normalized at blend time.
        self.weights = weights
        self.long_threshold = float(long_threshold)
        self.short_threshold = float(short_threshold)
        self.sl_atr_mult = float(sl_atr_mult)
        self.tp_atr_mult = float(tp_atr_mult)
        self.symbol = symbol
        self.timeframe = timeframe
        self.name = name or self.fingerprint()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "indicators": self.indicators,
            "weights": self.weights,
            "long_threshold": self.long_threshold,
            "short_threshold": self.short_threshold,
            "sl_atr_mult": self.sl_atr_mult,
            "tp_atr_mult": self.tp_atr_mult,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "name": self.name,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StrategySpec":
        return cls(
            indicators=data.get("indicators", {}),
            weights=data.get("weights", {}),
            long_threshold=data.get("long_threshold", 0.3),
            short_threshold=data.get("short_threshold", 0.3),
            sl_atr_mult=data.get("sl_atr_mult", 2.0),
            tp_atr_mult=data.get("tp_atr_mult", 3.0),
            symbol=data.get("symbol", ""),
            timeframe=data.get("timeframe", ""),
            name=data.get("name", ""),
        )

    def fingerprint(self) -> str:
        """Stable short hash identifying this spec (for dedup in memory)."""
        payload = json.dumps(
            {
                "indicators": self.indicators,
                "weights": self.weights,
                "lt": self.long_threshold,
                "st": self.short_threshold,
                "sl": self.sl_atr_mult,
                "tp": self.tp_atr_mult,
                "symbol": self.symbol,
                "timeframe": self.timeframe,
            },
            sort_keys=True,
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


class Strategy(object):
    """
    Executable strategy built from a StrategySpec. Instantiates the indicator
    objects once and produces a blended signal per bar.
    """

    def __init__(self, spec: StrategySpec):
        self.spec = spec
        self._indicators = {}
        for name, params in spec.indicators.items():
            try:
                cls = get_indicator_class(name)
                self._indicators[name] = cls(params=dict(params))
            except Exception:
                # Skip unknown indicators rather than fail the whole strategy.
                continue
        self._atr = ATR(params={"period": 14})

    def blended_signal(self, ohlcv: Any) -> float:
        """
        Return the weighted blend of indicator signals in [-1, +1] for the
        most recent bar.
        """
        total_w = 0.0
        acc = 0.0
        for name, ind in self._indicators.items():
            w = float(self.spec.weights.get(name, 1.0))
            if w == 0.0:
                continue
            try:
                s = ind.signal(ohlcv)
            except Exception:
                s = 0.0
            acc += w * s
            total_w += abs(w)
        if total_w == 0.0:
            return 0.0
        return max(-1.0, min(1.0, acc / total_w))

    def decision(self, ohlcv: Any) -> int:
        """
        Map the blended signal to a discrete decision:
          +1 = go long, -1 = go short, 0 = stay flat.
        """
        s = self.blended_signal(ohlcv)
        if s >= self.spec.long_threshold:
            return 1
        if s <= -self.spec.short_threshold:
            return -1
        return 0

    def atr_value(self, ohlcv: Any) -> Optional[float]:
        """Latest ATR value, used for SL/TP placement and sizing."""
        res = self._atr.compute(ohlcv)
        return res.last("atr")

    # ------------------------------------------------------------------ #
    # Fast whole-history evaluation (used by the backtester / walk-forward).
    # These compute every indicator's series ONCE (O(n)) and then blend per
    # bar (O(n)), instead of recomputing indicators on a growing window each
    # bar (which was O(n^2) and unusable on weak hardware).
    # ------------------------------------------------------------------ #
    def signal_series(self, ohlcv: Any) -> List[float]:
        """
        Return the per-bar blended signal in [-1, +1] aligned to the input bars.

        Numerically equivalent to calling blended_signal() on each growing
        prefix, but far faster because each indicator's signal_series() calls
        compute() only once.
        """
        n = len(ohlcv.close) if hasattr(ohlcv, "close") else 0
        blended: List[float] = [0.0] * n
        if n == 0:
            return blended

        # Collect (weight, per-bar signal series) for each active indicator.
        contributions: List[tuple] = []
        total_w = 0.0
        for name, ind in self._indicators.items():
            w = float(self.spec.weights.get(name, 1.0))
            if w == 0.0:
                continue
            try:
                series = ind.signal_series(ohlcv)
            except Exception:
                series = [0.0] * n
            contributions.append((w, series))
            total_w += abs(w)

        if total_w == 0.0:
            return blended

        for i in range(n):
            acc = 0.0
            for w, series in contributions:
                s = series[i] if i < len(series) else 0.0
                acc += w * s
            val = acc / total_w
            if val > 1.0:
                val = 1.0
            elif val < -1.0:
                val = -1.0
            blended[i] = val
        return blended

    def decision_series(self, ohlcv: Any) -> List[int]:
        """
        Return the per-bar discrete decision series (+1/-1/0), applying the
        long/short thresholds to the blended signal series.
        """
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
        """Full ATR series (aligned to bars) for SL/TP placement per bar."""
        res = self._atr.compute(ohlcv)
        atr = res.get("atr")
        if not atr:
            return [None] * (len(ohlcv.close) if hasattr(ohlcv, "close") else 0)
        return atr
