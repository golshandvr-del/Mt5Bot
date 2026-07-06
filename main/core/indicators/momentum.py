"""
Momentum indicators: RSI, Stochastic, CCI, Williams %R, ROC.

Each computes named series and a [-1, +1] signal for the latest bar.
Pure Python math.

All text is standard ASCII English only.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.indicators.base import Indicator, IndicatorResult
from core.indicators.registry import register_indicator


@register_indicator
class RSI(Indicator):
    name = "rsi"
    category = "momentum"

    @classmethod
    def default_params(cls) -> Dict[str, Any]:
        return {"period": 14}

    @classmethod
    def param_space(cls) -> Dict[str, List[Any]]:
        return {"period": [7, 14, 21]}

    def compute(self, ohlcv: Any) -> IndicatorResult:
        period = int(self.params["period"])
        close = ohlcv.close
        n = len(close)
        gains = [0.0] * n
        losses = [0.0] * n
        for i in range(1, n):
            change = close[i] - close[i - 1]
            gains[i] = max(0.0, change)
            losses[i] = max(0.0, -change)
        avg_gain = self._wilder_smooth(gains, period)
        avg_loss = self._wilder_smooth(losses, period)
        rsi: List[Optional[float]] = [None] * n
        for i in range(n):
            if avg_gain[i] is None or avg_loss[i] is None:
                continue
            if avg_loss[i] == 0:
                rsi[i] = 100.0
            else:
                rs = avg_gain[i] / avg_loss[i]
                rsi[i] = 100.0 - (100.0 / (1.0 + rs))
        return IndicatorResult({"rsi": rsi})

    def _signal_at(self, result, ohlcv, i):
        rsi = self._at(result.get("rsi"), i)
        if rsi is None:
            return 0.0
        # Mean-reversion reading: oversold (<30) bullish, overbought (>70) bearish.
        if rsi <= 30.0:
            return min(1.0, (30.0 - rsi) / 30.0 + 0.5)
        if rsi >= 70.0:
            return -min(1.0, (rsi - 70.0) / 30.0 + 0.5)
        # Mild trend bias around 50.
        return (rsi - 50.0) / 50.0 * 0.3


@register_indicator
class Stochastic(Indicator):
    name = "stoch"
    category = "momentum"

    @classmethod
    def default_params(cls) -> Dict[str, Any]:
        return {"k": 14, "d": 3, "smooth": 3}

    @classmethod
    def param_space(cls) -> Dict[str, List[Any]]:
        return {"k": [9, 14, 21], "d": [3, 5], "smooth": [1, 3]}

    def compute(self, ohlcv: Any) -> IndicatorResult:
        k_period = int(self.params["k"])
        d_period = int(self.params["d"])
        smooth = int(self.params["smooth"])
        high, low, close = ohlcv.high, ohlcv.low, ohlcv.close
        n = len(close)
        raw_k: List[Optional[float]] = [None] * n
        for i in range(n):
            if i >= k_period - 1:
                hh = max(high[i - k_period + 1: i + 1])
                ll = min(low[i - k_period + 1: i + 1])
                rng = hh - ll
                raw_k[i] = 50.0 if rng == 0 else 100.0 * (close[i] - ll) / rng
        # Smooth %K.
        k_clean = [v for v in raw_k if v is not None]
        k_smoothed_clean = self._sma(k_clean, smooth)
        pad = n - len(k_smoothed_clean)
        k_line: List[Optional[float]] = [None] * pad + k_smoothed_clean
        # %D is SMA of %K.
        kk = [v for v in k_line if v is not None]
        d_clean = self._sma(kk, d_period)
        pad2 = n - len(d_clean)
        d_line: List[Optional[float]] = [None] * pad2 + d_clean
        return IndicatorResult({"k": k_line, "d": d_line})

    def _signal_at(self, result, ohlcv, i):
        k = self._at(result.get("k"), i)
        d = self._at(result.get("d"), i)
        if k is None or d is None:
            return 0.0
        base = 0.0
        if k < 20.0:
            base = 0.7
        elif k > 80.0:
            base = -0.7
        # Add cross direction.
        cross = 0.3 if k > d else -0.3
        return max(-1.0, min(1.0, base + cross))


@register_indicator
class CCI(Indicator):
    name = "cci"
    category = "momentum"

    @classmethod
    def default_params(cls) -> Dict[str, Any]:
        return {"period": 20}

    @classmethod
    def param_space(cls) -> Dict[str, List[Any]]:
        return {"period": [14, 20, 30]}

    def compute(self, ohlcv: Any) -> IndicatorResult:
        period = int(self.params["period"])
        high, low, close = ohlcv.high, ohlcv.low, ohlcv.close
        n = len(close)
        tp = [(high[i] + low[i] + close[i]) / 3.0 for i in range(n)]
        sma_tp = self._sma(tp, period)
        cci: List[Optional[float]] = [None] * n
        for i in range(n):
            if sma_tp[i] is None:
                continue
            window = tp[i - period + 1: i + 1]
            mean = sma_tp[i]
            mad = sum(abs(x - mean) for x in window) / period
            cci[i] = 0.0 if mad == 0 else (tp[i] - mean) / (0.015 * mad)
        return IndicatorResult({"cci": cci})

    def _signal_at(self, result, ohlcv, i):
        cci = self._at(result.get("cci"), i)
        if cci is None:
            return 0.0
        if cci <= -100.0:
            return min(1.0, (abs(cci) - 100.0) / 100.0 + 0.5)
        if cci >= 100.0:
            return -min(1.0, (cci - 100.0) / 100.0 + 0.5)
        return cci / 100.0 * 0.3


@register_indicator
class WilliamsR(Indicator):
    name = "williams_r"
    category = "momentum"

    @classmethod
    def default_params(cls) -> Dict[str, Any]:
        return {"period": 14}

    @classmethod
    def param_space(cls) -> Dict[str, List[Any]]:
        return {"period": [7, 14, 21]}

    def compute(self, ohlcv: Any) -> IndicatorResult:
        period = int(self.params["period"])
        high, low, close = ohlcv.high, ohlcv.low, ohlcv.close
        n = len(close)
        wr: List[Optional[float]] = [None] * n
        for i in range(n):
            if i >= period - 1:
                hh = max(high[i - period + 1: i + 1])
                ll = min(low[i - period + 1: i + 1])
                rng = hh - ll
                wr[i] = -50.0 if rng == 0 else -100.0 * (hh - close[i]) / rng
        return IndicatorResult({"williams_r": wr})

    def _signal_at(self, result, ohlcv, i):
        wr = self._at(result.get("williams_r"), i)
        if wr is None:
            return 0.0
        # -80..-100 oversold (bullish), 0..-20 overbought (bearish).
        if wr <= -80.0:
            return 0.7
        if wr >= -20.0:
            return -0.7
        return 0.0


@register_indicator
class ROC(Indicator):
    name = "roc"
    category = "momentum"

    @classmethod
    def default_params(cls) -> Dict[str, Any]:
        return {"period": 12}

    @classmethod
    def param_space(cls) -> Dict[str, List[Any]]:
        return {"period": [9, 12, 25]}

    def compute(self, ohlcv: Any) -> IndicatorResult:
        period = int(self.params["period"])
        close = ohlcv.close
        n = len(close)
        roc: List[Optional[float]] = [None] * n
        for i in range(n):
            if i >= period and close[i - period] != 0:
                roc[i] = 100.0 * (close[i] - close[i - period]) / close[i - period]
        return IndicatorResult({"roc": roc})

    def _signal_at(self, result, ohlcv, i):
        roc = self._at(result.get("roc"), i)
        if roc is None:
            return 0.0
        return max(-1.0, min(1.0, roc / 5.0))
