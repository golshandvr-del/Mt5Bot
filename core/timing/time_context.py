"""
Time-context provider (Phase 5, user-update-request).

This is the piece the decision engine talks to. Given the latest bar of an
OHLCV series for a symbol/timeframe, it:

  1. detects the bar's TimeContext (session/day/hour/season) via SessionCalendar,
  2. looks up the LEARNED edge for each of that bar's time buckets (TimeStats),
  3. combines the TRUSTED bucket edges into a single, bounded time signal plus
     "favorable" / "unfavorable" / "blackout" flags.

Important honesty note (matches the user's request):
  The bot does NOT assume London/NY/etc. is good. The time signal is derived
  ONLY from buckets that have accumulated enough historical trades to be trusted
  (>= timing.learning.min_samples). Until then, the time signal is neutral (0.0)
  and does not bias decisions. This is the "recognize it itself" behavior.

How the decision engine uses the output:
  - The time signal is DIRECTIONLESS by nature (a session being profitable does
    not say "go long"), so by default it is applied as a CONFIDENCE / SIZE
    modifier and an optional entry GATE, not as a directional vote. A config
    switch (timing.as_directional) can instead feed it as a directional vote,
    but that is off by default because it is less principled.

Everything degrades to neutral when disabled or under-sampled, keeping the
Windows 7 live-light path unchanged.

All text is standard ASCII English only.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.timing.session import SessionCalendar
from core.timing.time_stats import TimeStats
from core.utils.logger import get_logger


class TimeSignal(object):
    """
    Structured time-context signal for one symbol/bar.

    Attributes
    ----------
    enabled     : whether the timing layer is active at all.
    context     : the TimeContext dict for the bar (or {} if unavailable).
    edge        : combined, bounded edge in [-1, +1] from TRUSTED buckets only.
    favorable   : True if edge >= favorable_threshold.
    unfavorable : True if edge <= -favorable_threshold.
    blackout    : True if a strongly negative, well-sampled window suggests
                  avoiding NEW entries (edge <= -blackout_threshold).
    size_multiplier : suggested position-size multiplier in
                      [min_size_mult, max_size_mult] derived from edge.
    reasons     : short human-readable strings for logging.
    trusted_buckets : list of (bucket_type, bucket_value, edge) actually used.
    """

    __slots__ = ("enabled", "context", "edge", "favorable", "unfavorable",
                 "blackout", "size_multiplier", "reasons", "trusted_buckets")

    def __init__(self):
        self.enabled = False
        self.context: Dict[str, Any] = {}
        self.edge = 0.0
        self.favorable = False
        self.unfavorable = False
        self.blackout = False
        self.size_multiplier = 1.0
        self.reasons: List[str] = []
        self.trusted_buckets: List[Any] = []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "edge": round(self.edge, 4),
            "favorable": self.favorable,
            "unfavorable": self.unfavorable,
            "blackout": self.blackout,
            "size_multiplier": round(self.size_multiplier, 4),
            "session": self.context.get("session_label", ""),
            "day": self.context.get("day_name", ""),
            "season": self.context.get("season", ""),
            "reasons": self.reasons,
        }


class TimeContextProvider(object):
    """
    Produces a TimeSignal from the latest bar, using learned time-bucket edges.

    Config (`timing:` section, all optional):
      timing.enabled              (bool, default False)
      timing.weight               (float, default 0.0) - directional weight used
                                   ONLY if timing.as_directional is true.
      timing.as_directional       (bool, default False)
      timing.favorable_threshold  (float, default 0.15)
      timing.blackout_threshold   (float, default 0.5)
      timing.gate_unfavorable     (bool, default False) - if true, block NEW
                                   entries in unfavorable/blackout windows.
      timing.min_size_mult        (float, default 0.5)
      timing.max_size_mult        (float, default 1.0)
      timing.bucket_weights       (map) - relative weight per bucket_type when
                                   combining edges (default: session/day heavier).
      timing.learning.min_samples (int, default 20) - via TimeStats.
    """

    _DEFAULT_BUCKET_WEIGHTS: Dict[str, float] = {
        "session": 1.0,
        "day": 0.8,
        "hour": 0.6,
        "month": 0.4,
        "quarter": 0.3,
        "season": 0.3,
    }

    def __init__(self, cfg: Any, time_stats: Optional[TimeStats] = None):
        self.cfg = cfg
        self.log = get_logger("timing.provider", cfg)
        self.enabled = bool(cfg.get_path("timing.enabled", False))
        self.as_directional = bool(cfg.get_path("timing.as_directional", False))
        self.weight = float(cfg.get_path("timing.weight", 0.0))
        self.favorable_threshold = float(
            cfg.get_path("timing.favorable_threshold", 0.15)
        )
        self.blackout_threshold = float(
            cfg.get_path("timing.blackout_threshold", 0.5)
        )
        self.gate_unfavorable = bool(
            cfg.get_path("timing.gate_unfavorable", False)
        )
        self.min_size_mult = float(cfg.get_path("timing.min_size_mult", 0.5))
        self.max_size_mult = float(cfg.get_path("timing.max_size_mult", 1.0))

        # Per-bucket-type combining weights (override via config).
        self.bucket_weights = dict(self._DEFAULT_BUCKET_WEIGHTS)
        cfg_bw = cfg.get_path("timing.bucket_weights", None)
        if cfg_bw and hasattr(cfg_bw, "items"):
            for k, v in cfg_bw.items():
                try:
                    self.bucket_weights[str(k)] = float(v)
                except Exception:
                    continue

        self.calendar = SessionCalendar(cfg)
        # Reuse an injected TimeStats or build one (shares the memory DB).
        self.stats = time_stats if time_stats is not None else TimeStats(cfg)

    # ------------------------------------------------------------------ #
    def _combine_edges(self, edges: Dict[str, Dict[str, Any]]) -> Any:
        """
        Weighted-average of TRUSTED bucket edges (untrusted buckets ignored).

        Returns (combined_edge, trusted_list). combined_edge is 0.0 if no bucket
        is trusted yet -> the timing layer stays neutral until it has learned.
        """
        num = 0.0
        den = 0.0
        trusted: List[Any] = []
        for bt, info in edges.items():
            if not info or not info.get("trusted", False):
                continue
            w = float(self.bucket_weights.get(bt, 0.5))
            if w <= 0.0:
                continue
            e = float(info.get("edge", 0.0))
            num += w * e
            den += w
            trusted.append((bt, info.get("n", 0), round(e, 4)))
        combined = (num / den) if den > 0 else 0.0
        return max(-1.0, min(1.0, combined)), trusted

    def _size_multiplier(self, edge: float) -> float:
        """
        Map edge in [-1, +1] to a size multiplier in
        [min_size_mult, max_size_mult]. Neutral edge (0) maps to the midpoint
        biased toward 1.0 so an unknown/neutral time does not shrink size.
        """
        lo = self.min_size_mult
        hi = self.max_size_mult
        if hi < lo:
            lo, hi = hi, lo
        # edge -1 -> lo, 0 -> ~1.0 (clamped into range), +1 -> hi.
        if edge >= 0:
            mult = 1.0 + edge * (hi - 1.0)
        else:
            mult = 1.0 + edge * (1.0 - lo)   # edge negative shrinks toward lo
        return max(lo, min(hi, mult))

    # ------------------------------------------------------------------ #
    def evaluate(self, ohlcv: Any, symbol: str, timeframe: str) -> TimeSignal:
        """
        Build the TimeSignal for the latest bar of `ohlcv`.

        Never raises: on any problem it returns a neutral, disabled-looking
        signal so the decision engine is unaffected.
        """
        sig = TimeSignal()
        if not self.enabled:
            return sig
        try:
            ctx = self.calendar.context_from_ohlcv(ohlcv, index=-1)
            if ctx is None:
                return sig
            sig.enabled = True
            sig.context = ctx.to_dict()

            edges = self.stats.context_edges(symbol, timeframe, ctx)
            combined, trusted = self._combine_edges(edges)
            sig.edge = combined
            sig.trusted_buckets = trusted
            sig.favorable = combined >= self.favorable_threshold
            sig.unfavorable = combined <= -self.favorable_threshold
            sig.blackout = combined <= -self.blackout_threshold
            sig.size_multiplier = self._size_multiplier(combined)

            sig.reasons.append(
                "session=%s day=%s season=%s edge=%.3f trusted=%d"
                % (ctx.session_label, ctx.day_name, ctx.season,
                   combined, len(trusted))
            )
            if sig.blackout:
                sig.reasons.append("time_blackout=1")
            elif sig.unfavorable:
                sig.reasons.append("time_unfavorable=1")
            elif sig.favorable:
                sig.reasons.append("time_favorable=1")
            return sig
        except Exception as exc:
            self.log.error("Time context evaluate failed: %s", exc)
            return TimeSignal()

    def directional_signal(self, ohlcv: Any, symbol: str,
                           timeframe: str) -> float:
        """
        Optional directional contribution for the decision blend. Returns 0.0
        unless timing.as_directional is true (default off), in which case it
        returns the combined edge (still driven only by trusted buckets).
        """
        if not (self.enabled and self.as_directional):
            return 0.0
        return float(self.evaluate(ohlcv, symbol, timeframe).edge)
