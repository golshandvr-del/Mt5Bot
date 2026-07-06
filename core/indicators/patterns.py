"""
Candlestick pattern detection.

Implements a compact set of widely used single- and multi-bar candle patterns
in pure Python (no TA-Lib dependency, which is hard to install on Windows 7):
  - Bullish/Bearish Engulfing
  - Hammer / Hanging Man
  - Shooting Star / Inverted Hammer
  - Doji
  - Morning Star / Evening Star (3-bar)

The signal() method returns a [-1, +1] score from the most recent detected
pattern (bullish positive, bearish negative).

All text is standard ASCII English only.
"""

from __future__ import annotations

from typing import Any, Dict, List

from core.indicators.base import Indicator, IndicatorResult
from core.indicators.registry import register_indicator


def _body(o: float, c: float) -> float:
    return abs(c - o)


def _range(h: float, l: float) -> float:
    return max(1e-9, h - l)


@register_indicator
class CandlePatterns(Indicator):
    name = "candle_patterns"
    category = "pattern"

    def compute(self, ohlcv: Any) -> IndicatorResult:
        o, h, l, c = ohlcv.open, ohlcv.high, ohlcv.low, ohlcv.close
        n = len(c)
        # Each output is a list of strings/None describing the detected pattern.
        patterns: List[Any] = [None] * n
        scores: List[float] = [0.0] * n

        for i in range(n):
            body = _body(o[i], c[i])
            rng = _range(h[i], l[i])
            upper_wick = h[i] - max(o[i], c[i])
            lower_wick = min(o[i], c[i]) - l[i]
            bullish = c[i] > o[i]

            label = None
            score = 0.0

            # Doji: very small body.
            if body <= 0.1 * rng:
                label = "doji"
                score = 0.0

            # Hammer / Hanging man: small body at top, long lower wick.
            elif lower_wick >= 2.0 * body and upper_wick <= 0.3 * body:
                label = "hammer"
                score = 0.6  # bullish reversal signal in downtrend

            # Shooting star / inverted hammer: long upper wick.
            elif upper_wick >= 2.0 * body and lower_wick <= 0.3 * body:
                label = "shooting_star"
                score = -0.6

            # Engulfing patterns require the previous bar.
            if i >= 1:
                prev_bull = c[i - 1] > o[i - 1]
                prev_body = _body(o[i - 1], c[i - 1])
                if (bullish and not prev_bull and c[i] >= o[i - 1]
                        and o[i] <= c[i - 1] and body > prev_body):
                    label = "bullish_engulfing"
                    score = 0.8
                elif (not bullish and prev_bull and o[i] >= c[i - 1]
                      and c[i] <= o[i - 1] and body > prev_body):
                    label = "bearish_engulfing"
                    score = -0.8

            # Morning / Evening star require two previous bars.
            if i >= 2:
                first_bull = c[i - 2] > o[i - 2]
                mid_small = _body(o[i - 1], c[i - 1]) <= 0.4 * _body(o[i - 2], c[i - 2]) + 1e-9
                if (not first_bull and mid_small and bullish
                        and c[i] > (o[i - 2] + c[i - 2]) / 2.0):
                    label = "morning_star"
                    score = 0.9
                elif (first_bull and mid_small and not bullish
                      and c[i] < (o[i - 2] + c[i - 2]) / 2.0):
                    label = "evening_star"
                    score = -0.9

            patterns[i] = label
            scores[i] = score

        return IndicatorResult({"pattern": patterns, "score": scores})

    def _signal_at(self, result, ohlcv, i):
        scores = result.get("score")
        if not scores or i < 0 or i >= len(scores):
            return 0.0
        return float(scores[i])
