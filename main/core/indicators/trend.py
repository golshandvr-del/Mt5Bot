"""
Trend indicators: SMA, EMA, MACD, ADX, Ichimoku, SuperTrend.

Each indicator computes named series and a [-1, +1] signal for the latest bar.
All math is pure Python (no numpy/pandas required at runtime).

All text is standard ASCII English only.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.indicators.base import Indicator, IndicatorResult
from core.indicators.registry import register_indicator


@register_indicator
class SMA(Indicator):
    name = "sma"
    category = "trend"

    @classmethod
    def default_params(cls) -> Dict[str, Any]:
        return {"period": 50}

    @classmethod
    def param_space(cls) -> Dict[str, List[Any]]:
        return {"period": [10, 20, 50, 100, 200]}

    def compute(self, ohlcv: Any) -> IndicatorResult:
        period = int(self.params["period"])
        return IndicatorResult({"sma": self._sma(ohlcv.close, period)})

    def _signal_at(self, result, ohlcv, i):
        ma = self._at(result.get("sma"), i)
        if ma is None or ma == 0 or i >= len(ohlcv.close):
            return 0.0
        price = ohlcv.close[i]
        # Above MA -> bullish, below -> bearish; scale by distance vs MA.
        diff = (price - ma) / ma
        return max(-1.0, min(1.0, diff * 50.0))


@register_indicator
class EMA(Indicator):
    name = "ema"
    category = "trend"

    @classmethod
    def default_params(cls) -> Dict[str, Any]:
        return {"period": 21}

    @classmethod
    def param_space(cls) -> Dict[str, List[Any]]:
        return {"period": [9, 12, 21, 34, 55]}

    def compute(self, ohlcv: Any) -> IndicatorResult:
        period = int(self.params["period"])
        return IndicatorResult({"ema": self._ema(ohlcv.close, period)})

    def _signal_at(self, result, ohlcv, i):
        ma = self._at(result.get("ema"), i)
        if ma is None or ma == 0 or i >= len(ohlcv.close):
            return 0.0
        diff = (ohlcv.close[i] - ma) / ma
        return max(-1.0, min(1.0, diff * 50.0))


@register_indicator
class MACD(Indicator):
    name = "macd"
    category = "trend"

    @classmethod
    def default_params(cls) -> Dict[str, Any]:
        return {"fast": 12, "slow": 26, "signal": 9}

    @classmethod
    def param_space(cls) -> Dict[str, List[Any]]:
        return {
            "fast": [8, 12, 16],
            "slow": [21, 26, 34],
            "signal": [7, 9, 11],
        }

    def compute(self, ohlcv: Any) -> IndicatorResult:
        fast = int(self.params["fast"])
        slow = int(self.params["slow"])
        sig = int(self.params["signal"])
        ema_fast = self._ema(ohlcv.close, fast)
        ema_slow = self._ema(ohlcv.close, slow)
        macd_line: List[Optional[float]] = []
        for f, s in zip(ema_fast, ema_slow):
            if f is None or s is None:
                macd_line.append(None)
            else:
                macd_line.append(f - s)
        # Signal line is EMA of the macd_line (ignoring leading None).
        clean = [v for v in macd_line if v is not None]
        sig_clean = self._ema(clean, sig)
        # Re-pad the signal line to align with the original length.
        pad = len(macd_line) - len(sig_clean)
        signal_line: List[Optional[float]] = [None] * pad + sig_clean
        hist: List[Optional[float]] = []
        for m, s in zip(macd_line, signal_line):
            if m is None or s is None:
                hist.append(None)
            else:
                hist.append(m - s)
        return IndicatorResult(
            {"macd": macd_line, "signal": signal_line, "hist": hist}
        )

    def _signal_at(self, result, ohlcv, i):
        hist = self._at(result.get("hist"), i)
        macd = self._at(result.get("macd"), i)
        if hist is None or macd is None:
            return 0.0
        # Bullish when histogram positive (macd above its signal line).
        base = 1.0 if hist > 0 else -1.0
        # Scale modestly by histogram magnitude relative to macd.
        denom = abs(macd) + 1e-9
        strength = min(1.0, abs(hist) / denom)
        return base * (0.5 + 0.5 * strength)


@register_indicator
class ADX(Indicator):
    name = "adx"
    category = "trend"

    @classmethod
    def default_params(cls) -> Dict[str, Any]:
        return {"period": 14}

    @classmethod
    def param_space(cls) -> Dict[str, List[Any]]:
        return {"period": [10, 14, 20]}

    def compute(self, ohlcv: Any) -> IndicatorResult:
        period = int(self.params["period"])
        high, low, close = ohlcv.high, ohlcv.low, ohlcv.close
        n = len(close)
        plus_dm = [0.0] * n
        minus_dm = [0.0] * n
        for i in range(1, n):
            up = high[i] - high[i - 1]
            down = low[i - 1] - low[i]
            plus_dm[i] = up if (up > down and up > 0) else 0.0
            minus_dm[i] = down if (down > up and down > 0) else 0.0
        tr = self._true_range(high, low, close)
        atr = self._wilder_smooth(tr, period)
        sm_plus = self._wilder_smooth(plus_dm, period)
        sm_minus = self._wilder_smooth(minus_dm, period)
        plus_di: List[Optional[float]] = [None] * n
        minus_di: List[Optional[float]] = [None] * n
        dx: List[Optional[float]] = [None] * n
        for i in range(n):
            if atr[i] and atr[i] != 0 and sm_plus[i] is not None and sm_minus[i] is not None:
                pdi = 100.0 * sm_plus[i] / atr[i]
                mdi = 100.0 * sm_minus[i] / atr[i]
                plus_di[i] = pdi
                minus_di[i] = mdi
                denom = pdi + mdi
                dx[i] = 100.0 * abs(pdi - mdi) / denom if denom != 0 else 0.0
        dx_clean = [v for v in dx if v is not None]
        adx_clean = self._wilder_smooth(dx_clean, period)
        pad = n - len(adx_clean)
        adx_line: List[Optional[float]] = [None] * pad + adx_clean
        return IndicatorResult(
            {"adx": adx_line, "plus_di": plus_di, "minus_di": minus_di}
        )

    def _signal_at(self, result, ohlcv, i):
        adx = self._at(result.get("adx"), i)
        pdi = self._at(result.get("plus_di"), i)
        mdi = self._at(result.get("minus_di"), i)
        if adx is None or pdi is None or mdi is None:
            return 0.0
        # Direction from DI cross; strength gated by ADX (>25 = trending).
        direction = 1.0 if pdi > mdi else -1.0
        strength = min(1.0, max(0.0, (adx - 20.0) / 30.0))
        return direction * strength


@register_indicator
class Ichimoku(Indicator):
    name = "ichimoku"
    category = "trend"

    @classmethod
    def default_params(cls) -> Dict[str, Any]:
        return {"tenkan": 9, "kijun": 26, "senkou": 52}

    def _donchian_mid(self, high: List[float], low: List[float],
                      period: int) -> List[Optional[float]]:
        n = len(high)
        out: List[Optional[float]] = [None] * n
        for i in range(n):
            if i >= period - 1:
                hh = max(high[i - period + 1: i + 1])
                ll = min(low[i - period + 1: i + 1])
                out[i] = (hh + ll) / 2.0
        return out

    def compute(self, ohlcv: Any) -> IndicatorResult:
        t = int(self.params["tenkan"])
        k = int(self.params["kijun"])
        s = int(self.params["senkou"])
        tenkan = self._donchian_mid(ohlcv.high, ohlcv.low, t)
        kijun = self._donchian_mid(ohlcv.high, ohlcv.low, k)
        span_a: List[Optional[float]] = []
        for a, b in zip(tenkan, kijun):
            if a is None or b is None:
                span_a.append(None)
            else:
                span_a.append((a + b) / 2.0)
        span_b = self._donchian_mid(ohlcv.high, ohlcv.low, s)
        return IndicatorResult(
            {"tenkan": tenkan, "kijun": kijun,
             "senkou_a": span_a, "senkou_b": span_b}
        )

    def _signal_at(self, result, ohlcv, i):
        a = self._at(result.get("senkou_a"), i)
        b = self._at(result.get("senkou_b"), i)
        if a is None or b is None or i >= len(ohlcv.close):
            return 0.0
        price = ohlcv.close[i]
        cloud_top = max(a, b)
        cloud_bottom = min(a, b)
        if price > cloud_top:
            return 1.0
        if price < cloud_bottom:
            return -1.0
        return 0.0


@register_indicator
class SuperTrend(Indicator):
    name = "supertrend"
    category = "trend"

    @classmethod
    def default_params(cls) -> Dict[str, Any]:
        return {"period": 10, "multiplier": 3.0}

    @classmethod
    def param_space(cls) -> Dict[str, List[Any]]:
        return {"period": [7, 10, 14], "multiplier": [2.0, 3.0, 4.0]}

    def compute(self, ohlcv: Any) -> IndicatorResult:
        period = int(self.params["period"])
        mult = float(self.params["multiplier"])
        high, low, close = ohlcv.high, ohlcv.low, ohlcv.close
        n = len(close)
        tr = self._true_range(high, low, close)
        atr = self._wilder_smooth(tr, period)
        st: List[Optional[float]] = [None] * n
        direction: List[Optional[int]] = [None] * n
        prev_st: Optional[float] = None
        prev_dir = 1
        for i in range(n):
            if atr[i] is None:
                continue
            hl2 = (high[i] + low[i]) / 2.0
            upper = hl2 + mult * atr[i]
            lower = hl2 - mult * atr[i]
            if prev_st is None:
                prev_st = lower
                prev_dir = 1
            if close[i] > prev_st:
                cur_dir = 1
            elif close[i] < prev_st:
                cur_dir = -1
            else:
                cur_dir = prev_dir
            cur_st = lower if cur_dir == 1 else upper
            st[i] = cur_st
            direction[i] = cur_dir
            prev_st = cur_st
            prev_dir = cur_dir
        return IndicatorResult({"supertrend": st, "direction": direction})

    def _signal_at(self, result, ohlcv, i):
        d = self._at(result.get("direction"), i)
        if d is None:
            return 0.0
        return 1.0 if d > 0 else -1.0
