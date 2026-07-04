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
    def __init__(self, cfg: Any, memory: Optional[object] = None):
        self.cfg = cfg
        self.log = get_logger("strategy.walk_forward", cfg)
        self.memory = memory
        self.backtester = Backtester(cfg)
        wf = cfg.get_path("memory.walk_forward", {})
        self.train_bars = int(wf.get("train_bars", 3000)) if hasattr(wf, "get") else 3000
        self.test_bars = int(wf.get("test_bars", 750)) if hasattr(wf, "get") else 750
        self.step_bars = int(wf.get("step_bars", 750)) if hasattr(wf, "get") else 750
        self.rank_metric = cfg.get_path("memory.search.rank_metric", "expectancy")
        self.min_trades = int(cfg.get_path("memory.search.min_trades", 30))

    def segments(self, n_bars: int) -> List[Dict[str, int]]:
        """Yield rolling (train_start, test_start, test_end) index windows."""
        out: List[Dict[str, int]] = []
        start = 0
        while True:
            train_start = start
            test_start = train_start + self.train_bars
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
        for idx, seg in enumerate(segs):
            test_slice = ohlcv.slice(seg["test_start"], seg["test_end"])
            result = self.backtester.run(strategy, test_slice, warmup=60, point=point)
            seg_metrics.append(result.metrics)
            if persist and self.memory is not None:
                self.memory.record_result(
                    spec, result.metrics, segment="seg_%d" % idx,
                    rank_metric=self.rank_metric,
                )
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
