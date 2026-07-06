"""
Common interface for every learning module (Phase 1).

All learners (ML classifier, RL agent, DL classifier, transfer model,
self-supervised encoder) implement this interface so the decision engine can
swap them via config.yaml -> learning.active_model without code changes.

Contract
--------
fit(X, y)             : train on a feature matrix X and label vector y.
predict_proba_up(x)   : return P(price goes up) in [0, 1] for one feature row.
predict_signal(x)     : return a signal in [-1, +1] for one feature row.
save(path) / load(path): persist and restore the model.
is_ready()            : True if the model can produce predictions.

A learner that cannot train (e.g. missing optional dependency) must set
self.available = False and degrade to neutral predictions (0.5 / 0.0) instead
of raising, so the live bot keeps running.

All text is standard ASCII English only.
"""

from __future__ import annotations

from typing import Any, List, Optional


class ModelPrediction(object):
    """Simple structured prediction returned by higher-level helpers."""

    __slots__ = ("proba_up", "signal", "label")

    def __init__(self, proba_up: float = 0.5, signal: float = 0.0,
                 label: int = 0):
        self.proba_up = proba_up    # probability that price rises
        self.signal = signal        # [-1, +1] directional score
        self.label = label          # -1, 0, +1 discrete decision


class BaseModel(object):
    """Abstract base class for all learners."""

    # Short identifier, e.g. "ml_classifier".
    kind: str = "base"

    def __init__(self, cfg: Any, model_cfg: Any):
        self.cfg = cfg
        self.model_cfg = model_cfg
        # Set False in subclasses when an optional backend is unavailable.
        self.available = True
        # Set True after a successful fit() or load().
        self.trained = False

    # ------------------------------------------------------------------ #
    # Training / inference (override in subclasses).
    # ------------------------------------------------------------------ #
    def fit(self, X: List[List[float]], y: List[int]) -> None:
        raise NotImplementedError

    def predict_proba_up(self, x: List[float]) -> float:
        """Return probability in [0,1] that price goes up. Neutral = 0.5."""
        raise NotImplementedError

    def predict_signal(self, x: List[float]) -> float:
        """
        Convert the up-probability into a [-1, +1] signal by default.
        Subclasses (e.g. RL) may override with their own policy output.
        """
        p = self.predict_proba_up(x)
        return max(-1.0, min(1.0, (p - 0.5) * 2.0))

    # ------------------------------------------------------------------ #
    # Persistence (override if a backend needs a special format).
    # ------------------------------------------------------------------ #
    def save(self, path: str) -> bool:
        raise NotImplementedError

    def load(self, path: str) -> bool:
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    # Status.
    # ------------------------------------------------------------------ #
    def is_ready(self) -> bool:
        return bool(self.available and self.trained)

    def describe(self) -> str:
        return "%s(available=%s, trained=%s)" % (
            self.kind, self.available, self.trained
        )
