"""
Reinforcement learning agent (Phase 1) - tabular Q-learning.

This is the CPU-light RL option suitable for Windows 7 without a GPU. It avoids
deep RL entirely. The agent discretizes a few features into a small state space
and learns a Q-table mapping state -> action (flat / long / short).

It is OFF by default in config. When enabled, it is trained offline on
historical bars (a simple single-position trading environment) and then queried
for a directional signal at inference time.

Reward model (simplified): taking a directional action earns the next-bar
return in that direction (long: +ret, short: -ret), flat earns 0 minus a tiny
holding incentive. This is a teaching/initial implementation, not a production
trading policy.

All text is standard ASCII English only.
"""

from __future__ import annotations

import os
import pickle
import random
from typing import Any, Dict, List, Optional, Tuple

from core.learning.base_model import BaseModel
from core.utils.logger import get_logger


# Actions.
ACTION_FLAT = 0
ACTION_LONG = 1
ACTION_SHORT = 2
ACTIONS = [ACTION_FLAT, ACTION_LONG, ACTION_SHORT]


def _discretize(value: float, edges: List[float]) -> int:
    """Map a continuous value into a bucket index using sorted edges."""
    for i, e in enumerate(edges):
        if value < e:
            return i
    return len(edges)


class RLAgent(BaseModel):
    kind = "rl_agent"

    def __init__(self, cfg: Any, model_cfg: Any):
        super().__init__(cfg, model_cfg)
        self.log = get_logger("learning.rl_agent", cfg)
        self.q: Dict[Tuple[int, ...], List[float]] = {}
        self.gamma = float(model_cfg.get("gamma", 0.95)) if hasattr(model_cfg, "get") else 0.95
        self.alpha = float(model_cfg.get("alpha", 0.1)) if hasattr(model_cfg, "get") else 0.1
        self.epsilon = float(model_cfg.get("epsilon", 0.1)) if hasattr(model_cfg, "get") else 0.1
        self.episodes = int(model_cfg.get("episodes", 50)) if hasattr(model_cfg, "get") else 50
        # Feature discretization edges (kept tiny to bound the state space).
        # We use the first three features from FeatureBuilder: ret1, ret3, ret5.
        self._edges = [-0.002, 0.0, 0.002]

    # ------------------------------------------------------------------ #
    def _state(self, x: List[float]) -> Tuple[int, ...]:
        """Build a small discrete state from the first few features."""
        if len(x) < 3:
            x = list(x) + [0.0] * (3 - len(x))
        return (
            _discretize(x[0], self._edges),
            _discretize(x[1], self._edges),
            _discretize(x[2], self._edges),
        )

    def _q_row(self, state: Tuple[int, ...]) -> List[float]:
        if state not in self.q:
            self.q[state] = [0.0, 0.0, 0.0]
        return self.q[state]

    # ------------------------------------------------------------------ #
    def fit(self, X: List[List[float]], y: List[int]) -> None:
        """
        Train the Q-table. y is unused for RL (kept for interface symmetry).
        We derive next-bar returns from feature column 0 (ret1) shifted by one,
        which approximates the realized return after acting at each step.
        """
        if not X or len(X) < 10:
            self.log.error("Not enough data to train RLAgent.")
            self.trained = False
            return

        # Use feature 0 (ret1) of the NEXT row as the realized return of acting now.
        rewards_long = [row[0] for row in X]  # next-step return proxy

        for _ in range(self.episodes):
            for t in range(len(X) - 1):
                state = self._state(X[t])
                qrow = self._q_row(state)
                # Epsilon-greedy action selection.
                if random.random() < self.epsilon:
                    action = random.choice(ACTIONS)
                else:
                    action = max(range(3), key=lambda a: qrow[a])
                # Reward based on the realized next return.
                nxt_ret = rewards_long[t + 1]
                if action == ACTION_LONG:
                    reward = nxt_ret
                elif action == ACTION_SHORT:
                    reward = -nxt_ret
                else:
                    reward = -abs(nxt_ret) * 0.05  # small cost to stay flat
                next_state = self._state(X[t + 1])
                next_q = self._q_row(next_state)
                best_next = max(next_q)
                # Q-learning update.
                qrow[action] += self.alpha * (
                    reward + self.gamma * best_next - qrow[action]
                )
        self.trained = True
        self.log.info("RLAgent trained: %d states learned.", len(self.q))

    # ------------------------------------------------------------------ #
    def predict_signal(self, x: List[float]) -> float:
        if not self.is_ready():
            return 0.0
        state = self._state(x)
        qrow = self.q.get(state)
        if qrow is None:
            return 0.0
        action = max(range(3), key=lambda a: qrow[a])
        if action == ACTION_LONG:
            return 1.0
        if action == ACTION_SHORT:
            return -1.0
        return 0.0

    def predict_proba_up(self, x: List[float]) -> float:
        # Convert the signed signal into a pseudo-probability for the interface.
        sig = self.predict_signal(x)
        return 0.5 + 0.5 * sig

    # ------------------------------------------------------------------ #
    def save(self, path: str) -> bool:
        try:
            directory = os.path.dirname(os.path.abspath(path))
            if directory:
                os.makedirs(directory, exist_ok=True)
            blob = {
                "q": {",".join(map(str, k)): v for k, v in self.q.items()},
                "edges": self._edges,
                "gamma": self.gamma,
                "trained": self.trained,
            }
            with open(path, "wb") as handle:
                pickle.dump(blob, handle, protocol=4)
            return True
        except Exception as exc:
            self.log.error("save error: %s", exc)
            return False

    def load(self, path: str) -> bool:
        try:
            if not os.path.exists(path):
                return False
            with open(path, "rb") as handle:
                blob = pickle.load(handle)
            self.q = {
                tuple(int(p) for p in k.split(",")): v
                for k, v in blob.get("q", {}).items()
            }
            self._edges = blob.get("edges", self._edges)
            self.gamma = blob.get("gamma", self.gamma)
            self.trained = bool(blob.get("trained", False))
            return self.trained
        except Exception as exc:
            self.log.error("load error: %s", exc)
            return False
