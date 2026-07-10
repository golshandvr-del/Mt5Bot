"""
Walk-forward evaluation (Phase 3).

Splits a long OHLCV history into rolling (train, test) windows and evaluates a
strategy on each out-of-sample test segment. This is the honest way to estimate
how a strategy would have generalized over years of data, instead of overfitting
one in-sample backtest.

Layout (using config.memory.walk_forward):
  train_bars : context window (not used to "fit" indicator-only strategies, but
               reserved for ML-based strategies and to mirror real deployment).
  test_bars  : the out-of-sample window scored and stored in memory.
  step_bars  : how far the window advances each iteration.

For each test segment we run the internal Backtester and record the metrics in
the MemoryStore under a segment label, so top_strategies() can average across
segments for a robust ranking.

All text is standard ASCII English only.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.strategy.strategy import Strategy, StrategySpec
from core.strategy.backtester import Backtester
from core.utils.logger import get_logger


class WalkForward(object):
    def __init__(self, cfg: Any, memory: Optional[object] = None,
                 time_stats: Optional[object] = None):
        self.cfg = cfg
        self.log = get_logger("strategy.walk_forward", cfg)
        self.memory = memory
        # Phase 5 (user-update-request): optional TimeStats to learn which time
        # buckets were favorable from every out-of-sample trade. Only active
        # when timing is enabled AND a TimeStats is supplied, so the light
        # search path is unchanged by default.
        self.time_stats = time_stats
        self.learn_time = (
            time_stats is not None
            and bool(cfg.get_path("timing.enabled", False))
        )
        self.backtester = Backtester(cfg)
        wf = cfg.get_path("memory.walk_forward", {})
        self.train_bars = int(wf.get("train_bars", 3000)) if hasattr(wf, "get") else 3000
        self.test_bars = int(wf.get("test_bars", 750)) if hasattr(wf, "get") else 750
        self.step_bars = int(wf.get("step_bars", 750)) if hasattr(wf, "get") else 750
        # Statistical robustness (A2 / P1.3): aim for at least this many rolling
        # segments. When the configured train_bars would yield fewer, the
        # segmenter auto-shrinks train_bars (down to a safe floor) so that long
        # histories get 6-10 out-of-sample windows instead of just ~2. Clamped
        # to a sane [1, 10] range; short history still uses the 70/30 fallback.
        self.min_segments = int(wf.get("min_segments", 6)) if hasattr(wf, "get") else 6
        if self.min_segments < 1:
            self.min_segments = 1
        if self.min_segments > 10:
            self.min_segments = 10
        # Locked holdout (A2 / P1.4): the FINAL holdout_bars of history are
        # reserved as a "quarantine" window that the search NEVER sees. The
        # walk-forward segmenter only splits the searchable portion
        # (n - holdout_bars); a strategy is later promoted only if it also
        # passes on this untouched holdout (evaluate_holdout()). Default 0 = OFF
        # so the walk-forward behavior stays byte-identical.
        self.holdout_bars = int(wf.get("holdout_bars", 0)) if hasattr(wf, "get") else 0
        if self.holdout_bars < 0:
            self.holdout_bars = 0
        # Recency weighting (B8 / P6.1): newer walk-forward segments can count
        # more than older ones when aggregating the per-segment scores. Read
        # defensively; a bad/missing value or anything outside (0, 1] falls back
        # to 1.0 = plain average (byte-identical to before).
        self.recency_decay = self._read_recency_decay(wf)
        self.rank_metric = cfg.get_path("memory.search.rank_metric", "expectancy")
        self.min_trades = int(cfg.get_path("memory.search.min_trades", 30))
        # Regime-sliced validation (U4.5). When enabled, each walk-forward
        # segment is labelled by its realized-volatility tercile and its trend
        # strength (ADX), per-regime scores are aggregated, and the promotion
        # gate requires no regime to fall below floor_mult * overall score.
        # Default OFF keeps the previous behavior.
        rg = cfg.get_path("memory.search.regime", {})
        rg = rg if hasattr(rg, "get") else {}
        self.regime_enabled = bool(rg.get("enabled", False))
        try:
            self.regime_floor_mult = float(rg.get("floor_mult", -0.5))
        except (TypeError, ValueError):
            self.regime_floor_mult = -0.5
        try:
            self.regime_min_segments = int(rg.get("min_segments_per_regime", 2))
        except (TypeError, ValueError):
            self.regime_min_segments = 2
        if self.regime_min_segments < 1:
            self.regime_min_segments = 1
        # ADX threshold separating a 'trend' segment from a 'range' one. ADX>=25
        # is the classic Wilder trending threshold; configurable for other
        # instruments/timeframes.
        try:
            self.regime_adx_trend = float(rg.get("adx_trend_threshold", 25.0))
        except (TypeError, ValueError):
            self.regime_adx_trend = 25.0

    # ------------------------------------------------------------------ #
    # Regime labelling (U4.5). Pure-Python, no external deps.
    # ------------------------------------------------------------------ #
    @staticmethod
    def _segment_volatility(ohlcv_slice: Any) -> float:
        """Realized volatility proxy for a segment: mean of (high-low)/close.

        This is an ATR%-like measure that needs no indicator warmup and is
        robust on any instrument (it is a fraction of price). Returns 0.0 on an
        empty/degenerate slice.
        """
        highs = getattr(ohlcv_slice, "high", None) or []
        lows = getattr(ohlcv_slice, "low", None) or []
        closes = getattr(ohlcv_slice, "close", None) or []
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

    def _segment_trend_strength(self, ohlcv_slice: Any) -> float:
        """Median ADX over a segment (trend strength). Falls back to 0.0 when
        ADX cannot be computed (too-short slice / degenerate data), which the
        caller treats as 'range' - the conservative label."""
        try:
            from core.indicators.registry import get_indicator_class
            adx = get_indicator_class("adx")(params={"period": 14})
            res = adx.compute(ohlcv_slice)
            series = res.get("adx") if res else None
        except Exception:
            series = None
        if not series:
            return 0.0
        vals = [v for v in series if v is not None]
        if not vals:
            return 0.0
        vals.sort()
        m = len(vals)
        mid = m // 2
        if m % 2 == 1:
            return float(vals[mid])
        return float((vals[mid - 1] + vals[mid]) / 2.0)

    @staticmethod
    def _vol_tercile(vol: float, cutoffs: List[float]) -> str:
        """Map a segment volatility to low/mid/high using two cutoffs (the
        33rd/66th percentiles computed across the run's segments)."""
        if not cutoffs or len(cutoffs) < 2:
            return "mid"
        if vol <= cutoffs[0]:
            return "low"
        if vol <= cutoffs[1]:
            return "mid"
        return "high"

    @staticmethod
    def _terciles(values: List[float]) -> List[float]:
        """Return [p33, p66] cutoffs of a list of floats (sorted, nearest-rank).
        Empty/short lists yield equal cutoffs so everything lands in 'mid'."""
        vals = sorted(float(v) for v in values)
        if not vals:
            return [0.0, 0.0]
        n = len(vals)

        def _pct(p):
            idx = int(round(p * (n - 1)))
            idx = max(0, min(n - 1, idx))
            return vals[idx]
        return [_pct(1.0 / 3.0), _pct(2.0 / 3.0)]

    def _regime_scores(self, seg_labels: List[str],
                       scores: List[float]) -> Dict[str, float]:
        """Average the per-segment rank scores within each regime label. Only
        regimes with >= regime_min_segments contributing segments are returned
        (a regime seen too rarely is not trustworthy enough to gate on)."""
        buckets: Dict[str, List[float]] = {}
        for label, score in zip(seg_labels, scores):
            buckets.setdefault(label, []).append(float(score))
        out: Dict[str, float] = {}
        for label, vals in buckets.items():
            if len(vals) >= self.regime_min_segments:
                out[label] = sum(vals) / len(vals)
        return out

    def passes_regime_floor(self, overall_score: float,
                            regime_scores: Dict[str, float]) -> bool:
        """U4.5 promotion gate: no gated regime may score below
        floor_mult * overall_score. Disabled / empty -> pass (no-op)."""
        if not self.regime_enabled or not regime_scores:
            return True
        floor = self.regime_floor_mult * float(overall_score)
        for label, score in regime_scores.items():
            if score < floor:
                return False
        return True

    def _min_train_floor(self) -> int:
        """Smallest train window we are willing to shrink to when chasing
        min_segments. Kept generous enough to stay meaningful (never below the
        test window and never below 200 bars)."""
        floor = max(int(self.test_bars), 200)
        return floor

    @staticmethod
    def _read_recency_decay(wf: Any) -> float:
        """Parse memory.walk_forward.recency_decay defensively.

        Returns a float in (0, 1]; anything missing, non-numeric, <= 0, or > 1
        falls back to 1.0 (recency weighting OFF = plain average).
        """
        raw = wf.get("recency_decay", 1.0) if hasattr(wf, "get") else 1.0
        try:
            d = float(raw)
        except (TypeError, ValueError):
            return 1.0
        if d <= 0.0 or d > 1.0:
            return 1.0
        return d

    def recency_weighted_mean(self, scores: List[float],
                              decay: Optional[float] = None) -> float:
        """Aggregate per-segment scores (OLDEST first, NEWEST last) into one
        number, weighting newer segments more via a geometric decay.

        The i-th score (0 = oldest) gets weight decay ** (last_index - i), so
        the most recent segment always has weight 1.0 and older segments fade.
        With decay == 1.0 (default) every weight is 1.0, i.e. the plain average,
        so behavior is byte-identical to before. An empty list returns 0.0.
        """
        if not scores:
            return 0.0
        d = self.recency_decay if decay is None else float(decay)
        if d <= 0.0 or d > 1.0:
            d = 1.0
        last = len(scores) - 1
        num = 0.0
        wsum = 0.0
        for i, s in enumerate(scores):
            w = d ** (last - i)
            num += w * float(s)
            wsum += w
        return num / wsum if wsum > 0 else 0.0

    def searchable_bars(self, n_bars: int) -> int:
        """Number of leading bars the search is allowed to see.

        The final holdout_bars are quarantined off the END of history so the
        search (and thus every walk-forward segment) never touches them. With
        holdout_bars = 0 (default) this returns n_bars unchanged, so behavior
        is byte-identical to before. Never returns less than 0.
        """
        n = int(n_bars) - int(self.holdout_bars)
        return n if n > 0 else 0

    def effective_train_bars(self, n_bars: int) -> int:
        """Pick a train window that yields at least min_segments segments when
        the history is long enough, otherwise the configured train_bars.

        The number of rolling segments for a given train window t is
        floor((n - t - test_bars) / step_bars) + 1. Solving for the largest t
        that still gives min_segments (m) segments:
            t_max = n - test_bars - (m - 1) * step_bars
        We only ever SHRINK (never grow) train_bars, and never below the floor.
        """
        configured = int(self.train_bars)
        m = int(self.min_segments)
        if m <= 1 or self.step_bars <= 0:
            return configured
        t_max = n_bars - int(self.test_bars) - (m - 1) * int(self.step_bars)
        if t_max >= configured:
            # History already long enough at the configured train window.
            return configured
        floor = self._min_train_floor()
        if t_max < floor:
            # Cannot reach min_segments without going below the floor; keep the
            # configured window and let segments()/evaluate() fall back.
            return configured
        return int(t_max)

    def segments(self, n_bars: int) -> List[Dict[str, int]]:
        """Yield rolling (train_start, test_start, test_end) index windows.

        Uses effective_train_bars() so long histories are split into at least
        min_segments out-of-sample windows (auto-shrinking train_bars). Short
        histories keep the original single-window behavior (and evaluate()
        applies the 70/30 fallback when no full window fits).

        Only the searchable portion (n_bars - holdout_bars) is segmented; the
        quarantined holdout tail is never included in any train/test window.
        """
        n_search = self.searchable_bars(n_bars)
        train = self.effective_train_bars(n_search)
        out: List[Dict[str, int]] = []
        start = 0
        while True:
            train_start = start
            test_start = train_start + train
            test_end = test_start + self.test_bars
            if test_end > n_search:
                break
            out.append(
                {"train_start": train_start, "test_start": test_start,
                 "test_end": test_end}
            )
            start += self.step_bars
        return out

    def evaluate(self, spec: StrategySpec, ohlcv: Any,
                 point: Optional[float] = None,
                 persist: bool = True,
                 warmup: int = 60) -> Dict[str, Any]:
        """
        Run walk-forward for one strategy spec over the full OHLCV history.

        Returns an aggregate dict with per-segment metrics and the average score.
        If persist=True and a MemoryStore was supplied, each segment result is
        stored so the memory can rank strategies later.

        ``warmup`` (default 60) is the number of leading bars each segment skips
        so indicators are stable. The U4.3 stability gate re-runs a finalist with
        a JITTERED warmup (via this arg) to prove its edge is not a knife-edge
        artifact of one particular warmup offset. Stability re-runs pass
        persist=False so they never pollute memory.
        """
        n = len(ohlcv.close)
        segs = self.segments(n)
        if not segs:
            # History too short for the configured windows: fall back to a
            # single 70/30 split so the search still produces a result. The
            # split is taken over the searchable portion only, so the holdout
            # tail is still never seen by the search.
            n_search = self.searchable_bars(n)
            if n_search <= 0:
                n_search = n
            split = int(n_search * 0.7)
            segs = [{"train_start": 0, "test_start": split, "test_end": n_search}]

        strategy = Strategy(spec)
        seg_metrics: List[Dict[str, Any]] = []
        scores: List[float] = []
        # U4.5: cache each segment's test slice so we can label it by regime
        # AFTER the run (the volatility terciles need all segments' vols first).
        test_slices: List[Any] = []
        want_trades = bool(persist and self.learn_time and self.time_stats is not None)
        for idx, seg in enumerate(segs):
            test_slice = ohlcv.slice(seg["test_start"], seg["test_end"])
            test_slices.append(test_slice)
            result = self.backtester.run(
                strategy, test_slice, warmup=warmup, point=point,
                record_trades=want_trades,
            )
            seg_metrics.append(result.metrics)
            if persist and self.memory is not None:
                self.memory.record_result(
                    spec, result.metrics, segment="seg_%d" % idx,
                    rank_metric=self.rank_metric,
                )
            # Phase 5: attribute this segment's trades to their time buckets so
            # the bot LEARNS which sessions/days/seasons were favorable.
            if want_trades and result.trades:
                try:
                    self.time_stats.record_trades(
                        spec.symbol, spec.timeframe, result.trades
                    )
                except Exception as exc:
                    self.log.error("time_stats.record_trades failed: %s", exc)
            from core.strategy.metrics import rank_value
            scores.append(rank_value(result.metrics, self.rank_metric))

        # B8 / P6.1: newer segments weigh more when recency_decay < 1.0. With
        # the default 1.0 this is exactly the plain mean, so nothing changes.
        avg_score = self.recency_weighted_mean(scores)
        avg_trades = (
            sum(m.get("num_trades", 0) for m in seg_metrics) / len(seg_metrics)
            if seg_metrics else 0.0
        )
        out = {
            "fingerprint": spec.fingerprint(),
            "n_segments": len(segs),
            "avg_score": avg_score,
            "avg_trades": avg_trades,
            "segments": seg_metrics,
        }
        # U4.5: regime-sliced scores + the promotion-gate verdict. Only computed
        # when the regime gate is enabled, so the default fast path is untouched.
        if self.regime_enabled and test_slices:
            seg_labels = self._label_segments(test_slices)
            regime_scores = self._regime_scores(seg_labels, scores)
            out["regime_labels"] = seg_labels
            out["regime_scores"] = regime_scores
            out["passes_regime_floor"] = self.passes_regime_floor(
                avg_score, regime_scores
            )
        return out

    def _label_segments(self, test_slices: List[Any]) -> List[str]:
        """U4.5: label each segment as '<voltercile>_<trend|range>'.

        Volatility is bucketed into low/mid/high by the run's own 33rd/66th
        percentile cutoffs (so the labels are relative to THIS instrument's
        regimes), and trend strength is 'trend' when the segment's median ADX is
        >= the configured threshold, else 'range'. Pure helper; no side effects.
        """
        vols = [self._segment_volatility(s) for s in test_slices]
        cutoffs = self._terciles(vols)
        labels: List[str] = []
        for i, sl in enumerate(test_slices):
            vol_label = self._vol_tercile(vols[i], cutoffs)
            adx = self._segment_trend_strength(sl)
            trend_label = "trend" if adx >= self.regime_adx_trend else "range"
            labels.append("%s_%s" % (vol_label, trend_label))
        return labels

    # ------------------------------------------------------------------ #
    def evaluate_holdout(self, spec: StrategySpec, ohlcv: Any,
                         point: Optional[float] = None) -> Dict[str, Any]:
        """Backtest a strategy on the locked holdout tail (A2 / P1.4).

        The holdout is the FINAL holdout_bars of history, which the search
        (segments()/evaluate()) never touches. A strategy is only promoted to
        the registry if it also "passes" here, giving an honest out-of-sample
        check on data that could not have been overfit.

        Returns a dict:
            {enabled, passed, score, metrics, holdout_bars, holdout_trades}
        When holdout_bars <= 0 the holdout is OFF: enabled=False and passed=True
        (i.e. the gate is a no-op), so behavior is unchanged by default.

        "passed" means the strategy produced at least min_trades holdout trades
        and a non-negative rank score on the holdout window. This is deliberately
        conservative and cheap; it filters out specs that only worked in-sample.
        """
        from core.strategy.metrics import rank_value

        n = len(ohlcv.close)
        h = int(self.holdout_bars)
        if h <= 0 or n <= 0:
            return {
                "enabled": False, "passed": True, "score": 0.0,
                "metrics": {}, "holdout_bars": 0, "holdout_trades": 0,
            }
        # Clamp the holdout so a train remnant always precedes it; if history is
        # too short to hold both, fall back to disabling the gate (pass-through)
        # rather than blocking every strategy.
        start = n - h
        if start <= 0:
            return {
                "enabled": False, "passed": True, "score": 0.0,
                "metrics": {}, "holdout_bars": h, "holdout_trades": 0,
            }
        holdout_slice = ohlcv.slice(start, n)
        strategy = Strategy(spec)
        try:
            result = self.backtester.run(
                strategy, holdout_slice, warmup=60, point=point,
                record_trades=False,
            )
        except Exception as exc:
            self.log.error("evaluate_holdout failed for %s: %s",
                           spec.fingerprint(), exc)
            return {
                "enabled": True, "passed": False, "score": 0.0,
                "metrics": {}, "holdout_bars": h, "holdout_trades": 0,
            }
        metrics = result.metrics
        score = rank_value(metrics, self.rank_metric)
        n_trades = int(metrics.get("num_trades", 0) or 0)
        passed = (n_trades >= self.min_trades) and (score >= 0.0)
        return {
            "enabled": True,
            "passed": bool(passed),
            "score": float(score),
            "metrics": metrics,
            "holdout_bars": h,
            "holdout_trades": n_trades,
        }
