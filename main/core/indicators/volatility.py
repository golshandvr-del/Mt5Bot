"""
Volatility indicators: ATR, Bollinger Bands, Keltner Channels, Donchian.

ATR is special: it is used widely for position sizing and SL/TP, so its
compute() output is consumed directly by the risk and strategy layers.

All math is pure Python.

All text is standard ASCII English only.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.indicators.base import Indicator, IndicatorResult
from core.indicators.registry import register_indicator


@register_indicator
class ATR(Indicator):
    name = "atr"
    category = "volatility"

    @classmethod
    def default_params(cls) -> Dict[str, Any]:
        return {"period": 14}

    @classmethod
    def param_space(cls) -> Dict[str, List[Any]]:
        return {"period": [10, 14, 20]}

    def compute(self, ohlcv: Any) -> IndicatorResult:
        period = int(self.params["period"])
        tr = self._true_range(ohlcv.high, ohlcv.low, ohlcv.close)
        atr = self._wilder_smooth(tr, period)
        return IndicatorResult({"atr": atr, "tr": tr})

    def signal(self, ohlcv: Any) -> float:
        # ATR is non-directional; it does not vote on direction.
        return 0.0


@register_indicator
class BollingerBands(Indicator):
    name = "bbands"
    category = "volatility"

    @classmethod
    def default_params(cls) -> Dict[str, Any]:
        return {"period": 20, "std": 2.0}

    @classmethod
    def param_space(cls) -> Dict[str, List[Any]]:
        return {"period": [14, 20, 30], "std": [1.5, 2.0, 2.5]}

    def compute(self, ohlcv: Any) -> IndicatorResult:
        period = int(self.params["period"])
        nstd = float(self.params["std"])
        close = ohlcv.close
        mid = self._sma(close, period)
        sd = self._rolling_std(close, period)
        upper: List[Optional[float]] = []
        lower: List[Optional[float]] = []
        for m, s in zip(mid, sd):
            if m is None or s is None:
                upper.append(None)
                lower.append(None)
            else:
                upper.append(m + nstd * s)
                lower.append(m - nstd * s)
        return IndicatorResult({"middle": mid, "upper": upper, "lower": lower})

    def _signal_at(self, result, ohlcv, i):
        upper = self._at(result.get("upper"), i)
        lower = self._at(result.get("lower"), i)
        mid = self._at(result.get("middle"), i)
        if upper is None or lower is None or mid is None or i >= len(ohlcv.close):
            return 0.0
        price = ohlcv.close[i]
        # Mean reversion: touching lower band bullish, upper band bearish.
        if price <= lower:
            return 0.7
        if price >= upper:
            return -0.7
        # Mild bias toward the band the price is closer to leaving.
        band = upper - lower
        if band == 0:
            return 0.0
        position = (price - mid) / (band / 2.0)
        return -max(-1.0, min(1.0, position)) * 0.3


@register_indicator
class Keltner(Indicator):
    name = "keltner"
    category = "volatility"

    @classmethod
    def default_params(cls) -> Dict[str, Any]:
        return {"period": 20, "atr_mult": 2.0}

    @classmethod
    def param_space(cls) -> Dict[str, List[Any]]:
        return {"period": [14, 20], "atr_mult": [1.5, 2.0, 2.5]}

    def compute(self, ohlcv: Any) -> IndicatorResult:
        period = int(self.params["period"])
        mult = float(self.params["atr_mult"])
        mid = self._ema(ohlcv.close, period)
        tr = self._true_range(ohlcv.high, ohlcv.low, ohlcv.close)
        atr = self._wilder_smooth(tr, period)
        upper: List[Optional[float]] = []
        lower: List[Optional[float]] = []
        for m, a in zip(mid, atr):
            if m is None or a is None:
                upper.append(None)
                lower.append(None)
            else:
                upper.append(m + mult * a)
                lower.append(m - mult * a)
        return IndicatorResult({"middle": mid, "upper": upper, "lower": lower})

    def _signal_at(self, result, ohlcv, i):
        upper = self._at(result.get("upper"), i)
        lower = self._at(result.get("lower"), i)
        if upper is None or lower is None or i >= len(ohlcv.close):
            return 0.0
        price = ohlcv.close[i]
        # Breakout interpretation: close above upper bullish, below lower bearish.
        if price > upper:
            return 0.7
        if price < lower:
            return -0.7
        return 0.0


@register_indicator
class Donchian(Indicator):
    name = "donchian"
    category = "volatility"

    @classmethod
    def default_params(cls) -> Dict[str, Any]:
        return {"period": 20}

    @classmethod
    def param_space(cls) -> Dict[str, List[Any]]:
        return {"period": [10, 20, 55]}

    def compute(self, ohlcv: Any) -> IndicatorResult:
        period = int(self.params["period"])
        high, low = ohlcv.high, ohlcv.low
        n = len(ohlcv.close)
        upper: List[Optional[float]] = [None] * n
        lower: List[Optional[float]] = [None] * n
        mid: List[Optional[float]] = [None] * n
        for i in range(n):
            if i >= period - 1:
                hh = max(high[i - period + 1: i + 1])
                ll = min(low[i - period + 1: i + 1])
                upper[i] = hh
                lower[i] = ll
                mid[i] = (hh + ll) / 2.0
        return IndicatorResult({"upper": upper, "lower": lower, "middle": mid})

    def _signal_at(self, result, ohlcv, i):
        upper = self._at(result.get("upper"), i)
        lower = self._at(result.get("lower"), i)
        if upper is None or lower is None or i >= len(ohlcv.close):
            return 0.0
        price = ohlcv.close[i]
        # Channel breakout strategy.
        if price >= upper:
            return 0.8
        if price <= lower:
            return -0.8
        return 0.0
