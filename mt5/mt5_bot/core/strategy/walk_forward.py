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
        self.rank_metric = cfg.get_path("memory.search.rank_metric", "expectancy")
        self.min_trades = int(cfg.get_path("memory.search.min_trades", 30))

    def _min_train_floor(self) -> int:
        """Smallest train window we are willing to shrink to when chasing
        min_segments. Kept generous enough to stay meaningful (never below the
        test window and never below 200 bars)."""
        floor = max(int(self.test_bars), 200)
        return floor

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
        """
        train = self.effective_train_bars(n_bars)
        out: List[Dict[str, int]] = []
        start = 0
        while True:
            train_start = start
            test_start = train_start + train
            test_end = test_start + self.test_bars
            if test_end > n_bars:
                break
            out.append(
                {"train_start": train_start, "test_start": test_start,
                 "test_end": test_end}
            )
            start += self.step_bars
        return out

    def evaluate(self, spec: StrategySpec, ohlcv: Any,
                 point: Optional[float] = None,
                 persist: bool = True) -> Dict[str, Any]:
        """
        Run walk-forward for one strategy spec over the full OHLCV history.

        Returns an aggregate dict with per-segment metrics and the average score.
        If persist=True and a MemoryStore was supplied, each segment result is
        stored so the memory can rank strategies later.
        """
        n = len(ohlcv.close)
        segs = self.segments(n)
        if not segs:
            # History too short for the configured windows: fall back to a
            # single 70/30 split so the search still produces a result.
            split = int(n * 0.7)
            segs = [{"train_start": 0, "test_start": split, "test_end": n}]

        strategy = Strategy(spec)
        seg_metrics: List[Dict[str, Any]] = []
        scores: List[float] = []
        want_trades = bool(persist and self.learn_time and self.time_stats is not None)
        for idx, seg in enumerate(segs):
            test_slice = ohlcv.slice(seg["test_start"], seg["test_end"])
            result = self.backtester.run(
                strategy, test_slice, warmup=60, point=point,
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

        avg_score = sum(scores) / len(scores) if scores else 0.0
        avg_trades = (
            sum(m.get("num_trades", 0) for m in seg_metrics) / len(seg_metrics)
            if seg_metrics else 0.0
        )
        return {
            "fingerprint": spec.fingerprint(),
            "n_segments": len(segs),
            "avg_score": avg_score,
            "avg_trades": avg_trades,
            "segments": seg_metrics,
        }
