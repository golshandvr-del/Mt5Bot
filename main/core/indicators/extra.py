"""
Phase 5 additional pluggable indicators.

These widen the indicator combination space for the Phase 3 strategy search while
staying pure-Python and cheap on CPU (Windows 7 friendly):

  - psar     : Parabolic SAR              (trend / stop-and-reverse)
  - stochrsi : Stochastic RSI             (momentum)
  - dpo      : Detrended Price Oscillator (cycle / momentum)
  - vwma     : Volume-Weighted Moving Avg (trend confirmed by volume)

Each follows the same contract as the other indicators: subclass Indicator,
implement compute(ohlcv) and _signal_at(result, ohlcv, i), self-register via the
@register_indicator decorator, and are activated from config.yaml. They are
imported in core/indicators/__init__.py for their registration side effects.

All text is standard ASCII English only.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.indicators.base import Indicator, IndicatorResult
from core.indicators.registry import register_indicator


@register_indicator
class ParabolicSAR(Indicator):
    """
    Parabolic Stop-And-Reverse. Emits the SAR series and a trend direction
    (+1 rising / -1 falling). Signal is simply the current direction, giving a
    clean trend-following contribution to the blend.
    """

    name = "psar"
    category = "trend"

    @classmethod
    def default_params(cls) -> Dict[str, Any]:
        # step = acceleration increment, max_step = acceleration cap.
        return {"step": 0.02, "max_step": 0.2}

    @classmethod
    def param_space(cls) -> Dict[str, List[Any]]:
        return {"step": [0.01, 0.02, 0.04], "max_step": [0.1, 0.2, 0.3]}

    def compute(self, ohlcv: Any) -> IndicatorResult:
        step = float(self.params["step"])
        max_step = float(self.params["max_step"])
        high, low = ohlcv.high, ohlcv.low
        n = len(high)
        sar: List[Optional[float]] = [None] * n
        direction: List[Optional[int]] = [None] * n
        if n < 2:
            return IndicatorResult({"psar": sar, "direction": direction})

        # Initialize: assume an up-trend to start; ep = extreme point.
        up = True
        af = step
        ep = high[0]
        sar_val = low[0]
        sar[0] = sar_val
        direction[0] = 1
        for i in range(1, n):
            prev_sar = sar_val
            sar_val = prev_sar + af * (ep - prev_sar)
            if up:
                # SAR cannot be above the last two lows.
                sar_val = min(sar_val, low[i - 1], low[i - 2] if i >= 2 else low[i - 1])
                if low[i] < sar_val:
                    # Flip to down-trend.
                    up = False
                    sar_val = ep
                    ep = low[i]
                    af = step
                else:
                    if high[i] > ep:
                        ep = high[i]
                        af = min(max_step, af + step)
            else:
                # SAR cannot be below the last two highs.
                sar_val = max(sar_val, high[i - 1], high[i - 2] if i >= 2 else high[i - 1])
                if high[i] > sar_val:
                    # Flip to up-trend.
                    up = True
                    sar_val = ep
                    ep = high[i]
                    af = step
                else:
                    if low[i] < ep:
                        ep = low[i]
                        af = min(max_step, af + step)
            sar[i] = sar_val
            direction[i] = 1 if up else -1
        return IndicatorResult({"psar": sar, "direction": direction})

    def _signal_at(self, result, ohlcv, i):
        d = self._at(result.get("direction"), i)
        if d is None:
            return 0.0
        return 1.0 if d > 0 else -1.0


@register_indicator
class StochRSI(Indicator):
    """
    Stochastic RSI: the stochastic oscillator applied to the RSI series. It is a
    fast momentum measure. Signal: oversold (<0.2) is bullish, overbought (>0.8)
    is bearish, scaled linearly in between.
    """

    name = "stochrsi"
    category = "momentum"

    @classmethod
    def default_params(cls) -> Dict[str, Any]:
        return {"rsi_period": 14, "stoch_period": 14}

    @classmethod
    def param_space(cls) -> Dict[str, List[Any]]:
        return {"rsi_period": [9, 14, 21], "stoch_period": [9, 14, 21]}

    def _rsi_series(self, close: List[float], period: int) -> List[Optional[float]]:
        n = len(close)
        gains = [0.0] * n
        losses = [0.0] * n
        for i in range(1, n):
            change = close[i] - close[i - 1]
            gains[i] = change if change > 0 else 0.0
            losses[i] = -change if change < 0 else 0.0
        avg_gain = self._wilder_smooth(gains, period)
        avg_loss = self._wilder_smooth(losses, period)
        rsi: List[Optional[float]] = [None] * n
        for i in range(n):
            g, l = avg_gain[i], avg_loss[i]
            if g is None or l is None:
                continue
            if l == 0:
                rsi[i] = 100.0
            else:
                rs = g / l
                rsi[i] = 100.0 - (100.0 / (1.0 + rs))
        return rsi

    def compute(self, ohlcv: Any) -> IndicatorResult:
        rsi_period = int(self.params["rsi_period"])
        stoch_period = int(self.params["stoch_period"])
        rsi = self._rsi_series(ohlcv.close, rsi_period)
        n = len(rsi)
        stoch: List[Optional[float]] = [None] * n
        for i in range(n):
            if i < stoch_period - 1:
                continue
            window = [rsi[j] for j in range(i - stoch_period + 1, i + 1)
                      if rsi[j] is not None]
            if len(window) < 2:
                continue
            lo = min(window)
            hi = max(window)
            cur = rsi[i]
            if cur is None or hi == lo:
                continue
            stoch[i] = (cur - lo) / (hi - lo)  # in [0, 1]
        return IndicatorResult({"stochrsi": stoch, "rsi": rsi})

    def _signal_at(self, result, ohlcv, i):
        v = self._at(result.get("stochrsi"), i)
        if v is None:
            return 0.0
        # Map [0,1] to [+1,-1]: low value (oversold) -> bullish.
        return max(-1.0, min(1.0, (0.5 - v) * 2.0))


@register_indicator
class DPO(Indicator):
    """
    Detrended Price Oscillator: price minus a displaced SMA, isolating short
    cycles by removing the longer trend. Positive DPO -> price above the cycle
    mean (bullish short-term), negative -> bearish.
    """

    name = "dpo"
    category = "momentum"

    @classmethod
    def default_params(cls) -> Dict[str, Any]:
        return {"period": 20}

    @classmethod
    def param_space(cls) -> Dict[str, List[Any]]:
        return {"period": [10, 20, 30]}

    def compute(self, ohlcv: Any) -> IndicatorResult:
        period = int(self.params["period"])
        close = ohlcv.close
        n = len(close)
        sma = self._sma(close, period)
        shift = period // 2 + 1
        dpo: List[Optional[float]] = [None] * n
        for i in range(n):
            j = i - shift
            if j < 0 or sma[j] is None:
                continue
            dpo[i] = close[i] - sma[j]
        return IndicatorResult({"dpo": dpo})

    def _signal_at(self, result, ohlcv, i):
        v = self._at(result.get("dpo"), i)
        if v is None or i >= len(ohlcv.close):
            return 0.0
        price = ohlcv.close[i]
        if price == 0:
            return 0.0
        # Normalize by price so the signal is scale-free; amplify modestly.
        return max(-1.0, min(1.0, (v / price) * 100.0))


@register_indicator
class VWMA(Indicator):
    """
    Volume-Weighted Moving Average: an SMA where each price is weighted by its
    bar volume, so high-volume bars pull the average more. Signal: price above
    VWMA is bullish, below is bearish, scaled by relative distance.
    """

    name = "vwma"
    category = "trend"

    @classmethod
    def default_params(cls) -> Dict[str, Any]:
        return {"period": 20}

    @classmethod
    def param_space(cls) -> Dict[str, List[Any]]:
        return {"period": [10, 20, 50]}

    def compute(self, ohlcv: Any) -> IndicatorResult:
        period = int(self.params["period"])
        close = ohlcv.close
        volume = getattr(ohlcv, "volume", None)
        n = len(close)
        vwma: List[Optional[float]] = [None] * n
        if not volume or len(volume) != n:
            # No usable volume -> fall back to a plain SMA so the indicator is
            # never useless (still returns a valid, aligned series).
            return IndicatorResult({"vwma": self._sma(close, period)})
        for i in range(n):
            if i < period - 1:
                continue
            num = 0.0
            den = 0.0
            for j in range(i - period + 1, i + 1):
                num += close[j] * volume[j]
                den += volume[j]
            if den > 0:
                vwma[i] = num / den
        return IndicatorResult({"vwma": vwma})

    def _signal_at(self, result, ohlcv, i):
        ma = self._at(result.get("vwma"), i)
        if ma is None or ma == 0 or i >= len(ohlcv.close):
            return 0.0
        diff = (ohlcv.close[i] - ma) / ma
        return max(-1.0, min(1.0, diff * 50.0))
