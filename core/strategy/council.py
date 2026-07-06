"""
Strategy council (Phase 5 / P5.1, Track B / B1): per-strategy LIVE credibility.

The offline search + walk-forward memory (Phase 3) tells us which strategies
looked good ON HISTORY. But once the bot is running, each promoted strategy
keeps producing REAL trade outcomes, and those recent outcomes are the freshest
evidence about whether its edge still holds. The council turns that stream of
recent per-strategy trade results into a LIVE credibility weight in [0, 1] using
a light multi-armed-bandit rule, so the decision engine can lean harder on
strategies that are currently working and quietly de-weight ones that have gone
cold - WITHOUT throwing away the offline ranking.

Design goals (match the project's Windows 7 / weak-CPU constraints)
-------------------------------------------------------------------
- **Pure standard library only** (``collections.deque`` + ``math``). No numpy,
  no pandas, no third-party bandit package. Runs on a minimal Python 3.8.
- **Bounded memory**: each strategy keeps only its last ``window`` (~30) trade
  outcomes in a ``deque(maxlen=window)``, so memory and CPU stay tiny no matter
  how long the bot runs.
- **Degrade gracefully**: a strategy the council has never seen returns the
  neutral ``default_weight`` (1.0), so wiring the council in never removes a
  strategy that simply has no live history yet.
- **Config-gated / OFF by default** at the engine level (see P5.3); this module
  itself just computes numbers and never sends orders.

Bandit rule (tabular UCB1, pure Python)
---------------------------------------
Each strategy is an "arm". We normalize every trade PnL into a reward in
``[0, 1]`` (a win maps toward 1, a loss toward 0) so the UCB math stays bounded
and broker-currency independent. For arm ``i`` with ``n_i`` recorded trades and
mean reward ``mu_i``, and total trades ``N`` across all arms, the UCB score is::

    ucb_i = mu_i + c * sqrt( 2 * ln(N) / n_i )

The exploration term ``c * sqrt(2 ln N / n_i)`` is large when an arm has few
samples (encouraging the engine not to prematurely bury a strategy) and shrinks
as evidence accumulates. We then map the raw UCB score to a bounded blend
weight in ``[min_weight, max_weight]`` so the council can only MODULATE the
existing ensemble, never invert it.

The council is the credibility SOURCE only. Persisting it across restarts is
P5.2 (``core/memory/store.py``); consuming the weights in the ensemble blend is
P5.3 (``core/decision/engine.py``). This file deliberately has no dependency on
either so it can be unit-tested in isolation (P5.4).

All text is standard ASCII English only.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Any, Deque, Dict, List, Optional


def _clamp(value: float, low: float, high: float) -> float:
    if value < low:
        return low
    if value > high:
        return high
    return value


class ArmStats(object):
    """
    Rolling record of one strategy's recent trade outcomes.

    Keeps only the last ``window`` normalized rewards (a ``deque`` with a
    ``maxlen``), so the credibility reflects RECENT behavior and old, stale
    outcomes fall off automatically. ``total_seen`` counts every outcome ever
    recorded (not just the ones still in the window) for reference/telemetry.
    """

    __slots__ = ("rewards", "total_seen")

    def __init__(self, window: int):
        self.rewards: Deque[float] = deque(maxlen=max(1, int(window)))
        self.total_seen: int = 0

    def record(self, reward: float) -> None:
        self.rewards.append(_clamp(float(reward), 0.0, 1.0))
        self.total_seen += 1

    @property
    def n(self) -> int:
        """Number of outcomes currently in the rolling window."""
        return len(self.rewards)

    @property
    def mean_reward(self) -> float:
        """Mean normalized reward over the current window (0.5 if empty)."""
        if not self.rewards:
            return 0.5
        return sum(self.rewards) / float(len(self.rewards))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rewards": list(self.rewards),
            "total_seen": int(self.total_seen),
        }


class StrategyCouncil(object):
    """
    Per-strategy live credibility via a tabular UCB1 bandit (pure Python).

    Usage::

        council = StrategyCouncil(cfg)                # reads decision.council.*
        council.record_outcome("fp_abc", pnl=12.5)    # after each closed trade
        w = council.weight("fp_abc")                   # blend weight in [lo, hi]
        weights = council.weights(["fp_abc", "fp_def"])# many at once

    The council never touches the network, the broker, or SQLite. It is a pure
    in-memory calculator; persistence (P5.2) will snapshot/restore it via
    ``to_dict`` / ``load_dict``.
    """

    def __init__(self, cfg: Any = None):
        self.cfg = cfg
        # ---- config with safe defaults (all optional) ----
        self.window = self._cfg_int("decision.council.window", 30, minimum=1)
        self.exploration = self._cfg_float(
            "decision.council.exploration_c", 1.0, minimum=0.0
        )
        self.min_weight = self._cfg_float(
            "decision.council.min_weight", 0.25, minimum=0.0
        )
        self.max_weight = self._cfg_float(
            "decision.council.max_weight", 1.75, minimum=0.0
        )
        if self.max_weight < self.min_weight:
            # Keep the range sane even if a user inverts the config.
            self.min_weight, self.max_weight = self.max_weight, self.min_weight
        # A strategy with NO live history yet gets this neutral weight so wiring
        # the council in never silently drops a freshly-promoted strategy.
        self.default_weight = self._cfg_float(
            "decision.council.default_weight", 1.0, minimum=0.0
        )
        # Below this many live trades an arm is still "warming up": we return the
        # neutral default_weight instead of a jumpy small-sample UCB score.
        self.min_trades = self._cfg_int(
            "decision.council.min_trades", 5, minimum=0
        )
        # PnL magnitude used to normalize a trade into a [0, 1] reward. When a
        # trade's |PnL| reaches this scale the reward saturates near 0 (loss) or
        # 1 (win). ``None``/<=0 means "use sign only" (win=1.0, loss=0.0,
        # flat=0.5), which is robust and broker-currency independent.
        self.reward_scale = self._cfg_float(
            "decision.council.reward_scale", 0.0, minimum=0.0
        )
        self._arms: Dict[str, ArmStats] = {}

    # ------------------------------------------------------------------ #
    # Config helpers (tolerate a missing cfg or missing keys).
    # ------------------------------------------------------------------ #
    def _cfg_get(self, path: str, default: Any) -> Any:
        if self.cfg is None:
            return default
        getter = getattr(self.cfg, "get_path", None)
        if callable(getter):
            try:
                return getter(path, default)
            except Exception:
                return default
        return default

    def _cfg_int(self, path: str, default: int, minimum: Optional[int] = None) -> int:
        try:
            val = int(self._cfg_get(path, default))
        except (TypeError, ValueError):
            val = default
        if minimum is not None and val < minimum:
            val = minimum
        return val

    def _cfg_float(self, path: str, default: float,
                   minimum: Optional[float] = None) -> float:
        try:
            val = float(self._cfg_get(path, default))
        except (TypeError, ValueError):
            val = default
        if minimum is not None and val < minimum:
            val = minimum
        return val

    # ------------------------------------------------------------------ #
    # Reward normalization
    # ------------------------------------------------------------------ #
    def _normalize_reward(self, pnl: float) -> float:
        """
        Map a realized trade PnL to a reward in [0, 1].

        - ``reward_scale <= 0`` (default): use the SIGN only -> win 1.0, loss
          0.0, flat 0.5. Simple, robust, and independent of account currency.
        - ``reward_scale > 0``: a smooth logistic-like squash so bigger wins /
          bigger losses move the reward further from 0.5, saturating near the
          scale. This lets a big win count for more than a scratch win.
        """
        try:
            x = float(pnl)
        except (TypeError, ValueError):
            return 0.5
        if self.reward_scale <= 0.0:
            if x > 0.0:
                return 1.0
            if x < 0.0:
                return 0.0
            return 0.5
        # tanh squash of (pnl / scale) into (-1, 1), then shift to (0, 1).
        squashed = math.tanh(x / self.reward_scale)
        return _clamp(0.5 * (squashed + 1.0), 0.0, 1.0)

    # ------------------------------------------------------------------ #
    # Recording outcomes
    # ------------------------------------------------------------------ #
    def record_outcome(self, fingerprint: str, pnl: float) -> None:
        """
        Record one CLOSED trade outcome for a strategy (by fingerprint).

        ``pnl`` is the realized profit/loss of the trade in account currency
        (or any consistent unit). It is normalized to a [0, 1] reward before
        being stored. Unknown fingerprints auto-create an arm.
        """
        if not fingerprint:
            return
        arm = self._arms.get(fingerprint)
        if arm is None:
            arm = ArmStats(self.window)
            self._arms[fingerprint] = arm
        arm.record(self._normalize_reward(pnl))

    def record_reward(self, fingerprint: str, reward: float) -> None:
        """
        Record an ALREADY-normalized reward in [0, 1] directly (bypasses PnL
        normalization). Useful for tests and for callers that compute their own
        reward. Values are clamped into [0, 1].
        """
        if not fingerprint:
            return
        arm = self._arms.get(fingerprint)
        if arm is None:
            arm = ArmStats(self.window)
            self._arms[fingerprint] = arm
        arm.record(reward)

    # ------------------------------------------------------------------ #
    # Credibility / weights
    # ------------------------------------------------------------------ #
    def _total_n(self) -> int:
        """Total outcomes across all arms currently in their windows."""
        return sum(arm.n for arm in self._arms.values())

    def ucb_score(self, fingerprint: str) -> Optional[float]:
        """
        Raw UCB1 score for a strategy, or ``None`` if it has no recorded trades.

        ucb = mean_reward + c * sqrt( 2 * ln(N) / n )
        where N is the total trades across all arms and n this arm's count.
        """
        arm = self._arms.get(fingerprint)
        if arm is None or arm.n == 0:
            return None
        total_n = self._total_n()
        # ln(N) is undefined/<=0 for N<=1; guard so a single trade gives a
        # finite (zero) exploration bonus rather than a math domain error.
        if total_n <= 1:
            exploration = 0.0
        else:
            exploration = self.exploration * math.sqrt(
                2.0 * math.log(total_n) / arm.n
            )
        return arm.mean_reward + exploration

    def weight(self, fingerprint: str) -> float:
        """
        Live credibility weight for a strategy in ``[min_weight, max_weight]``.

        - An UNKNOWN strategy or one still WARMING UP (fewer than
          ``min_trades`` live trades) returns the neutral ``default_weight`` so
          the council never penalizes a strategy that simply lacks live data.
        - Otherwise the weight is driven by the arm's MEAN REWARD (its realized
          hit-quality), mapped linearly onto ``[min_weight, max_weight]`` around
          a neutral 1.0 anchor at mean_reward == 0.5 (a coin-flip strategy):
          a consistently-winning strategy (mean -> 1.0) is boosted toward
          ``max_weight`` and a losing one (mean -> 0.0) is damped toward
          ``min_weight``.

        Why mean_reward and not the raw UCB score here? The UCB EXPLORATION term
        is the right tool when you must PICK one arm to try next, but for a
        credibility WEIGHT it would perversely boost a consistently-losing
        strategy that simply has few samples. So the weight uses the exploit
        term (mean reward), while the exploration bonus is applied only as a
        one-sided ANTI-BURIAL floor: a low-sample arm is not damped below neutral
        as aggressively, so a promising-but-young strategy is not prematurely
        starved. Winners are never inflated by exploration.
        """
        arm = self._arms.get(fingerprint)
        if arm is None or arm.n < self.min_trades:
            return self.default_weight
        mean = arm.mean_reward  # exploit term in [0, 1]
        if mean >= 0.5:
            # Winning side: boost toward max_weight. No exploration inflation.
            span = self.max_weight - 1.0
            w = 1.0 + span * _clamp((mean - 0.5) / 0.5, 0.0, 1.0)
        else:
            # Losing side: damp toward min_weight, but soften the damping for
            # low-sample arms via the UCB exploration bonus (anti-burial floor).
            span = 1.0 - self.min_weight
            damp = _clamp((0.5 - mean) / 0.5, 0.0, 1.0)
            total_n = self._total_n()
            if total_n > 1 and arm.n > 0:
                explore = self.exploration * math.sqrt(
                    2.0 * math.log(total_n) / arm.n
                )
            else:
                explore = 0.0
            # Subtract the exploration bonus from the damping (never below 0),
            # so a young arm is damped less than a well-sampled one.
            damp = _clamp(damp - explore, 0.0, 1.0)
            w = 1.0 - span * damp
        return _clamp(w, self.min_weight, self.max_weight)

    def weights(self, fingerprints: List[str]) -> Dict[str, float]:
        """Return {fingerprint: weight} for many strategies at once."""
        return {fp: self.weight(fp) for fp in fingerprints}

    def credibility(self, fingerprint: str) -> float:
        """
        A plain [0, 1] credibility = current mean reward (no exploration term),
        for telemetry / the weekly journal. Unknown/empty arms return 0.5.
        """
        arm = self._arms.get(fingerprint)
        if arm is None or arm.n == 0:
            return 0.5
        return arm.mean_reward

    def arm_summary(self, fingerprint: str) -> Dict[str, Any]:
        """Human-readable snapshot of one arm (for logging / the journal)."""
        arm = self._arms.get(fingerprint)
        if arm is None:
            return {
                "fingerprint": fingerprint,
                "n": 0,
                "total_seen": 0,
                "mean_reward": 0.5,
                "ucb": None,
                "weight": self.default_weight,
            }
        return {
            "fingerprint": fingerprint,
            "n": arm.n,
            "total_seen": arm.total_seen,
            "mean_reward": arm.mean_reward,
            "ucb": self.ucb_score(fingerprint),
            "weight": self.weight(fingerprint),
        }

    def known_fingerprints(self) -> List[str]:
        """Fingerprints the council has seen at least one outcome for."""
        return list(self._arms.keys())

    # ------------------------------------------------------------------ #
    # Persistence hooks (used by P5.2). Kept dependency-free here.
    # ------------------------------------------------------------------ #
    def to_dict(self) -> Dict[str, Any]:
        """Serialize the full council state to a plain JSON-friendly dict."""
        return {
            "window": self.window,
            "arms": {fp: arm.to_dict() for fp, arm in self._arms.items()},
        }

    def load_dict(self, data: Dict[str, Any]) -> None:
        """
        Restore council state from a dict produced by ``to_dict`` (P5.2).

        Tolerant of missing/partial data: anything malformed is skipped rather
        than raising, so a corrupt snapshot never crashes the bot - it just
        starts that arm cold.
        """
        if not isinstance(data, dict):
            return
        arms = data.get("arms", {})
        if not isinstance(arms, dict):
            return
        for fp, arm_data in arms.items():
            if not fp or not isinstance(arm_data, dict):
                continue
            arm = ArmStats(self.window)
            rewards = arm_data.get("rewards", [])
            if isinstance(rewards, list):
                for r in rewards:
                    try:
                        arm.rewards.append(_clamp(float(r), 0.0, 1.0))
                    except (TypeError, ValueError):
                        continue
            try:
                arm.total_seen = int(arm_data.get("total_seen", len(arm.rewards)))
            except (TypeError, ValueError):
                arm.total_seen = len(arm.rewards)
            self._arms[fp] = arm
