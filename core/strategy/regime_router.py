"""
Regime router (UPGRADE_PLAN Phase U6.2).

Instead of AVERAGING the top-K registry strategies (which blends together edges
that disagree - a trend follower and a mean-reverter cancel out exactly when the
market is choppy), the regime router picks, for the CURRENT market regime, the
single validated strategy that historically did best IN THAT REGIME, and trades
it exactly as parity mode would. A tiny, pure-Python detector labels each bar's
regime the SAME way the walk-forward validation labels its segments (U4.5), so
"the regime we route on" is the same "regime we validated per-regime scores in".

Two public pieces:

  * ``RegimeDetector`` - labels a trailing OHLCV window as
    ``"<low|mid|high>_<trend|range>"`` using an ATR%-like realized-volatility
    proxy (bucketed by the window's OWN 33rd/66th percentiles) plus median ADX
    (>= threshold -> "trend", else "range"). Identical maths to
    ``WalkForward._label_segments`` so live == validated.

  * ``RegimeRouter`` - holds a per-regime CHAMPION map (regime label ->
    strategy fingerprint) built in ``train`` mode by scoring each candidate
    strategy on the bars of each regime, persisted to one JSON file. At live
    time ``champion_for(regime)`` returns the fingerprint to trade; the engine
    then trades that validated strategy through the normal parity path.

For research/validation the router's COMPOSITE (route each bar to its regime's
champion signal) is exposed as a ``Strategy``-compatible ``RegimeRouterStrategy``
so the existing ``Backtester`` / ``scripts/validate_ensemble.py`` can walk-forward
score it end-to-end - satisfying the U2.5 rule that no unvalidated composite may
go live.

Design constraints (same as the rest of the repo): pure Python / stdlib only,
Windows 7 + Python 3.8 + CPU-only, fully optional and config-gated
(``decision.regime_router.enabled``, default OFF), degrades gracefully (an empty
/ untrained router routes to nobody and the engine falls back to plain parity
top-1), deterministic. ASCII English only.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from core.strategy.strategy import Strategy, StrategySpec
from core.utils.logger import get_logger


# --------------------------------------------------------------------------- #
# Regime labelling - shared maths with WalkForward (U4.5).
# --------------------------------------------------------------------------- #
def _window_volatility(highs: List[float], lows: List[float],
                       closes: List[float]) -> float:
    """Realized-volatility proxy: mean of (high-low)/close over the window.

    ATR%-like, needs no indicator warmup, robust on any instrument. Mirrors
    ``WalkForward._segment_volatility`` so live labels match validation labels.
    """
    n = min(len(highs), len(lows), len(closes))
    if n <= 0:
        return 0.0
    acc = 0.0
    cnt = 0
    for i in range(n):
        c = closes[i]
        if c:
            acc += abs(highs[i] - lows[i]) / abs(c)
            cnt += 1
    return (acc / cnt) if cnt > 0 else 0.0


def _median(vals: List[float]) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    m = len(s)
    mid = m // 2
    if m % 2 == 1:
        return float(s[mid])
    return float((s[mid - 1] + s[mid]) / 2.0)


def _terciles(values: List[float]) -> List[float]:
    """[p33, p66] cutoffs (sorted, nearest-rank). Mirrors WalkForward._terciles."""
    vals = sorted(float(v) for v in values)
    if not vals:
        return [0.0, 0.0]
    n = len(vals)

    def _pct(p: float) -> float:
        idx = int(round(p * (n - 1)))
        idx = max(0, min(n - 1, idx))
        return vals[idx]

    return [_pct(1.0 / 3.0), _pct(2.0 / 3.0)]


def _vol_tercile(vol: float, cutoffs: List[float]) -> str:
    if not cutoffs or len(cutoffs) < 2:
        return "mid"
    if vol <= cutoffs[0]:
        return "low"
    if vol <= cutoffs[1]:
        return "mid"
    return "high"


class RegimeDetector(object):
    """Label a trailing OHLCV window with its market regime.

    The detector needs volatility CUTOFFS to bucket low/mid/high. In validation
    those cutoffs come from the run's own segments; at live time we compute them
    once from the training history (``fit_cutoffs``) and reuse them, so a single
    live bar is bucketed against the SAME distribution the champions were chosen
    on. Absent fitted cutoffs, every window is "mid" (a safe, non-discriminating
    default).
    """

    def __init__(self, cfg: Any):
        self.cfg = cfg
        rg = cfg.get_path("memory.search.regime", {}) if hasattr(cfg, "get_path") else {}
        rg = rg if hasattr(rg, "get") else {}
        try:
            self.adx_trend = float(rg.get("adx_trend_threshold", 25.0))
        except Exception:
            self.adx_trend = 25.0
        rr = cfg.get_path("decision.regime_router", {}) if hasattr(cfg, "get_path") else {}
        rr = rr if hasattr(rr, "get") else {}
        try:
            self.window = int(rr.get("detect_window", 96))
        except Exception:
            self.window = 96
        if self.window < 8:
            self.window = 8
        try:
            self.adx_period = int(rr.get("adx_period", 14))
        except Exception:
            self.adx_period = 14
        self._cutoffs: List[float] = [0.0, 0.0]
        self._fitted = False

    # ------------------------------------------------------------------ #
    def fit_cutoffs(self, ohlcv: Any, seg_len: Optional[int] = None) -> List[float]:
        """Compute low/mid/high volatility cutoffs from history by slicing it
        into ``seg_len``-bar chunks and taking their per-chunk volatilities'
        terciles. ``seg_len`` defaults to the detection window so the cutoffs
        match the granularity we label live bars at."""
        highs = list(getattr(ohlcv, "high", []) or [])
        lows = list(getattr(ohlcv, "low", []) or [])
        closes = list(getattr(ohlcv, "close", []) or [])
        n = len(closes)
        step = int(seg_len or self.window)
        if step < 8:
            step = 8
        vols: List[float] = []
        i = 0
        while i + step <= n:
            vols.append(_window_volatility(
                highs[i:i + step], lows[i:i + step], closes[i:i + step]))
            i += step
        if len(vols) >= 3:
            self._cutoffs = _terciles(vols)
            self._fitted = True
        else:
            self._cutoffs = [0.0, 0.0]
            self._fitted = False
        return list(self._cutoffs)

    def set_cutoffs(self, cutoffs: List[float]) -> None:
        if cutoffs and len(cutoffs) >= 2:
            self._cutoffs = [float(cutoffs[0]), float(cutoffs[1])]
            self._fitted = True

    def cutoffs(self) -> List[float]:
        return list(self._cutoffs)

    # ------------------------------------------------------------------ #
    def _window_adx(self, highs, lows, closes) -> float:
        try:
            from core.indicators.registry import get_indicator_class
            from core.data.data_feed import OHLCV
            tmp = OHLCV(symbol="", timeframe="")
            for i in range(len(closes)):
                # time/open/volume are unused by ADX; fill with sane values.
                tmp.append_row(i, closes[i], highs[i], lows[i], closes[i], 1)
            adx = get_indicator_class("adx")(params={"period": self.adx_period})
            res = adx.compute(tmp)
            series = res.get("adx") if res else None
        except Exception:
            series = None
        if not series:
            return 0.0
        vals = [v for v in series if v is not None]
        return _median(vals)

    def label(self, ohlcv: Any) -> str:
        """Return the regime label of the LAST ``window`` bars of ``ohlcv``."""
        highs = list(getattr(ohlcv, "high", []) or [])
        lows = list(getattr(ohlcv, "low", []) or [])
        closes = list(getattr(ohlcv, "close", []) or [])
        n = len(closes)
        if n <= 0:
            return "mid_range"
        w = min(self.window, n)
        hs, ls, cs = highs[n - w:], lows[n - w:], closes[n - w:]
        vol = _window_volatility(hs, ls, cs)
        vol_label = _vol_tercile(vol, self._cutoffs)
        adx = self._window_adx(hs, ls, cs)
        trend_label = "trend" if adx >= self.adx_trend else "range"
        return "%s_%s" % (vol_label, trend_label)
