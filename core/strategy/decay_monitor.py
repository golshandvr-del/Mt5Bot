"""
Strategy decay monitor (Phase 5 / P5.5, Track B / B3): statistical expiry.

A promoted strategy was validated on HISTORICAL walk-forward data. Markets
change, so an edge that was real in backtest can silently DECAY in live/paper
trading. This module watches, per registry strategy, whether its RECENT realized
PnL distribution has drifted away from the walk-forward distribution it was
promoted on, and flags drifted strategies as "suspect" so the decision engine
can zero-weight / skip them until the next search re-validates them (wired in
P5.6).

Design goals (consistent with the rest of the bot):

  * Pure standard library only (no numpy/scipy) so it runs on the Win7 / Py3.8
    target and inside the offline CI.
  * Config-gated and default OFF (`decision.decay_monitor.enabled`), so the
    light path is byte-for-byte unchanged until a user opts in.
  * Conservative: with too little live evidence it returns "ok" (never expires a
    strategy on noise), and it degrades gracefully (any error -> "ok").

The drift test is intentionally simple and robust for small samples: a
two-sample z-test on the MEAN trade PnL (Welch-style, using each sample's own
variance), plus a relative mean-drop guard. A strategy is "suspect" when its
recent mean PnL has dropped materially below the reference AND that drop is
statistically unlikely to be noise (z beyond the configured threshold). Either
the statistical OR the hard relative-drop rule can trip the flag, whichever the
user wants to rely on; both are configurable.

All text is standard ASCII English only.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from core.utils.logger import get_logger


def _mean(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _variance(xs: List[float], mean: Optional[float] = None) -> float:
    """Sample variance (n-1). Returns 0.0 for fewer than 2 points."""
    n = len(xs)
    if n < 2:
        return 0.0
    m = _mean(xs) if mean is None else mean
    return sum((x - m) ** 2 for x in xs) / (n - 1)


class DecayVerdict(object):
    """The outcome of assessing one strategy for statistical decay."""

    def __init__(self, suspect: bool, reason: str,
                 ref_mean: float, recent_mean: float,
                 z: float, ref_n: int, recent_n: int):
        self.suspect = bool(suspect)
        self.reason = reason
        self.ref_mean = ref_mean
        self.recent_mean = recent_mean
        self.z = z
        self.ref_n = ref_n
        self.recent_n = recent_n

    def to_dict(self) -> Dict[str, Any]:
        return {
            "suspect": self.suspect,
            "reason": self.reason,
            "ref_mean": self.ref_mean,
            "recent_mean": self.recent_mean,
            "z": self.z,
            "ref_n": self.ref_n,
            "recent_n": self.recent_n,
        }

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return ("DecayVerdict(suspect=%r, reason=%r, ref_mean=%.4f, "
                "recent_mean=%.4f, z=%.3f, ref_n=%d, recent_n=%d)" % (
                    self.suspect, self.reason, self.ref_mean,
                    self.recent_mean, self.z, self.ref_n, self.recent_n))


class DecayMonitor(object):
    """
    Per-strategy statistical-expiry detector.

    Config (all under `decision.decay_monitor`, all optional / defaulted):
      * enabled          (bool, default False) - master switch; the caller is
        responsible for skipping this entirely when disabled, but `assess` also
        honors it so a disabled monitor always returns a non-suspect verdict.
      * min_recent        (int, default 20)   - need at least this many recent
        trades before any verdict other than "ok".
      * min_reference     (int, default 20)   - need at least this many reference
        (walk-forward) trades to compare against.
      * z_threshold       (float, default 2.0) - |z| beyond which the mean drop is
        considered statistically real.
      * max_rel_drop      (float, default 0.5) - if the recent mean is below
        (1 - max_rel_drop) * ref_mean (for a positive-edge strategy), that alone
        trips "suspect". 0 disables the hard rule.
      * require_both      (bool, default False) - if True, BOTH the z-test and the
        relative-drop rule must trip; if False, EITHER trips the flag.
    """

    def __init__(self, cfg: Any = None):
        self.cfg = cfg
        self.log = get_logger("strategy.decay_monitor", cfg)

        def _cfg(path, default):
            if cfg is None or not hasattr(cfg, "get_path"):
                return default
            try:
                return cfg.get_path(path, default)
            except Exception:
                return default

        self.enabled = bool(_cfg("decision.decay_monitor.enabled", False))
        self.min_recent = int(_cfg("decision.decay_monitor.min_recent", 20))
        self.min_reference = int(_cfg("decision.decay_monitor.min_reference", 20))
        self.z_threshold = float(_cfg("decision.decay_monitor.z_threshold", 2.0))
        self.max_rel_drop = float(_cfg("decision.decay_monitor.max_rel_drop", 0.5))
        self.require_both = bool(_cfg("decision.decay_monitor.require_both", False))

    # ------------------------------------------------------------------ #
    def drift_z(self, reference: List[float], recent: List[float]) -> float:
        """
        Two-sample (Welch) z-score for "recent mean < reference mean".

        Negative z means the recent mean is BELOW the reference (decay); positive
        means it improved. Returns 0.0 when either side lacks the variance/size to
        judge (treated as "no evidence of drift").
        """
        n_ref, n_rec = len(reference), len(recent)
        if n_ref < 2 or n_rec < 2:
            return 0.0
        m_ref, m_rec = _mean(reference), _mean(recent)
        v_ref = _variance(reference, m_ref)
        v_rec = _variance(recent, m_rec)
        se2 = v_ref / n_ref + v_rec / n_rec
        if se2 <= 0.0:
            # Zero pooled variance: fall back to a pure sign of the mean change.
            if m_rec < m_ref:
                return -float("inf")
            if m_rec > m_ref:
                return float("inf")
            return 0.0
        return (m_rec - m_ref) / math.sqrt(se2)

    def assess(self, reference: List[float], recent: List[float]) -> DecayVerdict:
        """
        Judge one strategy. `reference` = its walk-forward per-trade PnLs (the
        distribution it was promoted on); `recent` = its recent live/paper
        per-trade PnLs. Returns a DecayVerdict; `.suspect` is True only when there
        is enough evidence AND the configured rule(s) trip.
        """
        reference = [float(x) for x in (reference or [])]
        recent = [float(x) for x in (recent or [])]
        m_ref, m_rec = _mean(reference), _mean(recent)
        n_ref, n_rec = len(reference), len(recent)

        # Master switch / insufficient evidence -> never suspect.
        if not self.enabled:
            return DecayVerdict(False, "disabled", m_ref, m_rec, 0.0, n_ref, n_rec)
        if n_rec < self.min_recent or n_ref < self.min_reference:
            return DecayVerdict(False, "insufficient-data", m_ref, m_rec, 0.0,
                                n_ref, n_rec)

        z = self.drift_z(reference, recent)
        # Statistical rule: recent mean is significantly BELOW the reference.
        stat_trip = z <= -abs(self.z_threshold)

        # Hard relative-drop rule (only meaningful for a positive-edge strategy).
        rel_trip = False
        if self.max_rel_drop > 0.0 and m_ref > 0.0:
            threshold = (1.0 - self.max_rel_drop) * m_ref
            rel_trip = m_rec < threshold

        if self.require_both:
            suspect = stat_trip and rel_trip
        else:
            suspect = stat_trip or rel_trip

        if suspect:
            parts = []
            if stat_trip:
                parts.append("z=%.2f<=-%.2f" % (z, abs(self.z_threshold)))
            if rel_trip:
                parts.append("mean %.4f<%.0f%% of ref %.4f" % (
                    m_rec, (1.0 - self.max_rel_drop) * 100.0, m_ref))
            reason = "suspect: " + "; ".join(parts)
        else:
            reason = "ok"
        return DecayVerdict(suspect, reason, m_ref, m_rec, z, n_ref, n_rec)

    def is_suspect(self, reference: List[float], recent: List[float]) -> bool:
        """Convenience boolean wrapper around `assess`."""
        try:
            return self.assess(reference, recent).suspect
        except Exception as exc:
            self.log.error("decay_monitor assess failed: %s", exc)
            return False
