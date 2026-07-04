"""
Volume indicators: OBV (On-Balance Volume), MFI (Money Flow Index), VWAP.

Note on data: MT5 typically provides tick_volume (number of price changes)
rather than true traded volume for forex. These indicators use whatever volume
the data layer supplies. They degrade gracefully if volume is all zeros.

All math is pure Python.

All text is standard ASCII English only.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.indicators.base import Indicator, IndicatorResult
from core.indicators.registry import register_indicator


@register_indicator
class OBV(Indicator):
    name = "obv"
    category = "volume"

    def compute(self, ohlcv: Any) -> IndicatorResult:
        close, vol = ohlcv.close, ohlcv.volume
        n = len(close)
        obv: List[float] = [0.0] * n
        for i in range(1, n):
            if close[i] > close[i - 1]:
                obv[i] = obv[i - 1] + vol[i]
            elif close[i] < close[i - 1]:
                obv[i] = obv[i - 1] - vol[i]
            else:
                obv[i] = obv[i - 1]
        return IndicatorResult({"obv": obv})

    def _signal_at(self, result, ohlcv, i):
        obv = result.get("obv")
        if not obv or i < 9 or i >= len(obv):
            return 0.0
        # Compare current OBV against its trailing 10-bar SMA (volume trend).
        recent = obv[i - 9: i + 1]
        avg = sum(recent) / len(recent)
        if avg == 0:
            return 0.0
        diff = (obv[i] - avg) / (abs(avg) + 1e-9)
        return max(-1.0, min(1.0, diff))


@register_indicator
class MFI(Indicator):
    name = "mfi"
    category = "volume"

    @classmethod
    def default_params(cls) -> Dict[str, Any]:
        return {"period": 14}

    @classmethod
    def param_space(cls) -> Dict[str, List[Any]]:
        return {"period": [7, 14, 21]}

    def compute(self, ohlcv: Any) -> IndicatorResult:
        period = int(self.params["period"])
        high, low, close, vol = ohlcv.high, ohlcv.low, ohlcv.close, ohlcv.volume
        n = len(close)
        tp = [(high[i] + low[i] + close[i]) / 3.0 for i in range(n)]
        pos_flow = [0.0] * n
        neg_flow = [0.0] * n
        for i in range(1, n):
            raw = tp[i] * vol[i]
            if tp[i] > tp[i - 1]:
                pos_flow[i] = raw
            elif tp[i] < tp[i - 1]:
                neg_flow[i] = raw
        mfi: List[Optional[float]] = [None] * n
        for i in range(n):
            if i >= period:
                pos = sum(pos_flow[i - period + 1: i + 1])
                neg = sum(neg_flow[i - period + 1: i + 1])
                if neg == 0:
                    mfi[i] = 100.0
                else:
                    ratio = pos / neg
                    mfi[i] = 100.0 - (100.0 / (1.0 + ratio))
        return IndicatorResult({"mfi": mfi})

    def _signal_at(self, result, ohlcv, i):
        mfi = self._at(result.get("mfi"), i)
        if mfi is None:
            return 0.0
        if mfi <= 20.0:
            return 0.7
        if mfi >= 80.0:
            return -0.7
        return (50.0 - mfi) / 50.0 * 0.3


@register_indicator
class VWAP(Indicator):
    name = "vwap"
    category = "volume"

    def compute(self, ohlcv: Any) -> IndicatorResult:
        # Rolling (cumulative) VWAP over the whole loaded window.
        high, low, close, vol = ohlcv.high, ohlcv.low, ohlcv.close, ohlcv.volume
        n = len(close)
        vwap: List[Optional[float]] = [None] * n
        cum_pv = 0.0
        cum_v = 0.0
        for i in range(n):
            tp = (high[i] + low[i] + close[i]) / 3.0
            cum_pv += tp * vol[i]
            cum_v += vol[i]
            vwap[i] = cum_pv / cum_v if cum_v > 0 else None
        return IndicatorResult({"vwap": vwap})

    def _signal_at(self, result, ohlcv, i):
        vwap = self._at(result.get("vwap"), i)
        if vwap is None or vwap == 0 or i >= len(ohlcv.close):
            return 0.0
        diff = (ohlcv.close[i] - vwap) / vwap
        return max(-1.0, min(1.0, diff * 50.0))
