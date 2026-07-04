"""
Feature engineering for the learning core.

The FeatureBuilder turns an OHLCV series into:
  - X : a list of feature rows (one per usable bar),
  - y : integer labels (+1 up, 0 flat, -1 down) based on forward returns,
  - feature_names : the column names (for debugging / model introspection).

Features are derived from the enabled indicators (so the learning layer and the
indicator layer stay consistent) plus a few raw price/return features. Everything
is pure Python lists; numpy is used internally by the ML backends if present.

Labeling
--------
For each bar i, look `horizon` bars ahead and compute the forward return.
If forward_return > +threshold_atr * ATR -> label +1 (up)
If forward_return < -threshold_atr * ATR -> label -1 (down)
otherwise                                 -> label 0 (flat)
ATR-relative thresholds adapt the labels to each symbol's volatility.

All text is standard ASCII English only.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from core.indicators.base import Indicator
from core.indicators.volatility import ATR
from core.utils.logger import get_logger


class FeatureBuilder(object):
    """Builds feature matrices and labels from OHLCV data."""

    # Names of the optional Phase 5 time features (cyclical encodings so the
    # model can learn intraday/weekly/seasonal structure without huge one-hots).
    _TIME_FEATURE_NAMES: List[str] = [
        "t_hour_sin", "t_hour_cos",
        "t_dow_sin", "t_dow_cos",
        "t_month_sin", "t_month_cos",
        "t_in_london", "t_in_newyork", "t_in_tokyo", "t_in_sydney",
    ]

    def __init__(self, cfg: Any, indicators: Optional[Dict[str, Indicator]] = None):
        self.cfg = cfg
        self.log = get_logger("learning.features", cfg)
        self.indicators = indicators or {}
        # ATR is always needed for volatility-relative labels and features.
        self._atr = ATR(params={"period": 14})
        # Phase 5 (user-update-request): optionally add time/session/season
        # features so the ML model can also exploit calendar effects. Off by
        # default so the light path and existing models are unaffected.
        self.use_time_features = bool(cfg.get_path("timing.as_features", False))
        self._calendar = None
        if self.use_time_features:
            try:
                from core.timing.session import SessionCalendar
                self._calendar = SessionCalendar(cfg)
            except Exception:
                self.use_time_features = False

    # ------------------------------------------------------------------ #
    # Feature extraction for a single bar index.
    # ------------------------------------------------------------------ #
    def _precompute(self, ohlcv: Any) -> Dict[str, Any]:
        """Compute every indicator's series once for the whole window."""
        cache: Dict[str, Any] = {}
        for name, ind in self.indicators.items():
            try:
                cache[name] = ind.compute(ohlcv)
            except Exception:
                cache[name] = None
        cache["__atr__"] = self._atr.compute(ohlcv)
        return cache

    def feature_names(self) -> List[str]:
        """Stable list of feature column names."""
        names = ["ret1", "ret3", "ret5", "range_atr", "body_ratio"]
        for name, ind in sorted(self.indicators.items()):
            # One scalar feature per indicator: its latest signal-like value.
            names.append("ind_%s" % name)
        if self.use_time_features:
            names.extend(self._TIME_FEATURE_NAMES)
        return names

    def _time_features(self, ts: Any) -> List[float]:
        """
        Build the optional cyclical time features for a bar timestamp. Returns a
        neutral all-zero vector if the timestamp cannot be parsed, so training
        never breaks on bad data.
        """
        zeros = [0.0] * len(self._TIME_FEATURE_NAMES)
        if self._calendar is None:
            return zeros
        ctx = self._calendar.context(ts)
        if ctx is None:
            return zeros
        two_pi = 2.0 * math.pi
        hour_ang = two_pi * (ctx.hour / 24.0)
        dow_ang = two_pi * (ctx.day_of_week / 7.0)
        month_ang = two_pi * ((ctx.month - 1) / 12.0)
        active = set(ctx.sessions)
        return [
            math.sin(hour_ang), math.cos(hour_ang),
            math.sin(dow_ang), math.cos(dow_ang),
            math.sin(month_ang), math.cos(month_ang),
            1.0 if "london" in active else 0.0,
            1.0 if "newyork" in active else 0.0,
            1.0 if "tokyo" in active else 0.0,
            1.0 if "sydney" in active else 0.0,
        ]

    def _row_features(self, ohlcv: Any, i: int, cache: Dict[str, Any]) -> Optional[List[float]]:
        """Build the feature row for bar index i, or None if not computable."""
        close = ohlcv.close
        high = ohlcv.high
        low = ohlcv.low
        open_ = ohlcv.open
        if i < 6:
            return None

        atr_series = cache["__atr__"].get("atr")
        atr = atr_series[i] if atr_series and atr_series[i] is not None else None
        if atr is None or atr == 0:
            return None

        # Raw price-derived features.
        ret1 = (close[i] - close[i - 1]) / close[i - 1] if close[i - 1] else 0.0
        ret3 = (close[i] - close[i - 3]) / close[i - 3] if close[i - 3] else 0.0
        ret5 = (close[i] - close[i - 5]) / close[i - 5] if close[i - 5] else 0.0
        bar_range = high[i] - low[i]
        range_atr = bar_range / atr if atr else 0.0
        body = abs(close[i] - open_[i])
        body_ratio = body / bar_range if bar_range > 0 else 0.0

        row: List[float] = [ret1, ret3, ret5, range_atr, body_ratio]

        # Indicator-derived features: take the indicator's primary output at i.
        for name, ind in sorted(self.indicators.items()):
            res = cache.get(name)
            value = 0.0
            if res is not None:
                # Prefer a primary series matching the indicator name; otherwise
                # use the first available numeric series at index i.
                series = None
                if name in res and isinstance(res[name], list):
                    series = res[name]
                else:
                    for key, val in res.items():
                        if isinstance(val, list):
                            series = val
                            break
                if series is not None and i < len(series) and series[i] is not None:
                    try:
                        value = float(series[i])
                    except Exception:
                        value = 0.0
            row.append(value)

        # Phase 5: optional time/session/season features for the bar.
        if self.use_time_features:
            times = getattr(ohlcv, "time", None)
            ts = times[i] if (times and i < len(times)) else None
            row.extend(self._time_features(ts))

        return row

    # ------------------------------------------------------------------ #
    # Public: build training data and a single inference row.
    # ------------------------------------------------------------------ #
    def build_training(self, ohlcv: Any) -> Tuple[List[List[float]], List[int], List[str]]:
        """
        Build (X, y, feature_names) for supervised training.

        Labels use the forward horizon and ATR threshold from config.
        Bars near the end (without a full forward window) are dropped.
        """
        learn_cfg = self.cfg.get_path("learning.ml_classifier", {})
        horizon = int(learn_cfg.get("label_horizon", 5)) if hasattr(learn_cfg, "get") else 5
        thr = float(learn_cfg.get("label_threshold_atr", 0.5)) if hasattr(learn_cfg, "get") else 0.5

        cache = self._precompute(ohlcv)
        atr_series = cache["__atr__"].get("atr")
        close = ohlcv.close
        n = len(close)

        X: List[List[float]] = []
        y: List[int] = []
        for i in range(n - horizon):
            row = self._row_features(ohlcv, i, cache)
            if row is None:
                continue
            atr = atr_series[i] if atr_series and atr_series[i] is not None else None
            if atr is None or atr == 0:
                continue
            forward = close[i + horizon] - close[i]
            up_thr = thr * atr
            if forward > up_thr:
                label = 1
            elif forward < -up_thr:
                label = -1
            else:
                label = 0
            X.append(row)
            y.append(label)
        self.log.info("Built %d training samples (%d features).",
                      len(X), len(self.feature_names()))
        return X, y, self.feature_names()

    def build_inference_row(self, ohlcv: Any) -> Optional[List[float]]:
        """Build the single feature row for the most recent bar."""
        cache = self._precompute(ohlcv)
        return self._row_features(ohlcv, len(ohlcv.close) - 1, cache)
