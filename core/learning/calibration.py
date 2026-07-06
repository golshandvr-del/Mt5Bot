"""
Phase 5 - lightweight probability calibration for learners.

Raw classifier probabilities (especially from boosted trees) are often poorly
calibrated: a predicted 0.8 may not really mean an 80% chance. Better-calibrated
probabilities make the decision engine's confidence gating trustworthy.

This module implements a tiny, pure-Python Platt-style (logistic) calibrator:
given pairs of (raw_probability, binary_outcome) it fits a 1-D logistic map
    calibrated = sigmoid(a * raw + b)
via a few passes of gradient descent. It has NO third-party dependencies and is
extremely cheap, so it fits the Windows 7 / CPU-only constraint.

It is OPTIONAL and config-driven (learning.ml_classifier.calibrate). When off or
when there is not enough data, calibration falls back to the identity map, so
behaviour is unchanged.

All text is standard ASCII English only.
"""

from __future__ import annotations

import math
from typing import List, Optional


def _sigmoid(z: float) -> float:
    # Numerically stable logistic function.
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


class PlattCalibrator(object):
    """
    One-dimensional logistic calibrator: p_cal = sigmoid(a * p_raw + b).

    fitted == False means "identity" (returns the raw probability unchanged),
    which keeps the pipeline safe when there is too little data to calibrate.
    """

    __slots__ = ("a", "b", "fitted")

    def __init__(self, a: float = 1.0, b: float = 0.0, fitted: bool = False):
        self.a = float(a)
        self.b = float(b)
        self.fitted = bool(fitted)

    # ------------------------------------------------------------------ #
    def fit(self, raw_probs: List[float], outcomes: List[int],
            epochs: int = 300, lr: float = 0.1, min_samples: int = 30) -> bool:
        """
        Fit the logistic map from raw probabilities to binary outcomes.

        raw_probs : predicted P(up) values in [0, 1].
        outcomes  : matching binary labels (1 = up happened, 0 = not).
        Returns True if a real calibration was fitted, False if it fell back to
        identity (too little / degenerate data).
        """
        n = min(len(raw_probs), len(outcomes))
        if n < int(min_samples):
            self.a, self.b, self.fitted = 1.0, 0.0, False
            return False
        # Need both classes present to learn anything meaningful.
        pos = sum(1 for o in outcomes[:n] if o == 1)
        if pos == 0 or pos == n:
            self.a, self.b, self.fitted = 1.0, 0.0, False
            return False

        a, b = 1.0, 0.0
        for _ in range(int(epochs)):
            grad_a = 0.0
            grad_b = 0.0
            for i in range(n):
                p = raw_probs[i]
                # Guard input to [0,1].
                if p < 0.0:
                    p = 0.0
                elif p > 1.0:
                    p = 1.0
                z = a * p + b
                pred = _sigmoid(z)
                err = pred - float(1 if outcomes[i] == 1 else 0)
                grad_a += err * p
                grad_b += err
            a -= lr * grad_a / n
            b -= lr * grad_b / n
        self.a, self.b, self.fitted = a, b, True
        return True

    # ------------------------------------------------------------------ #
    def transform(self, raw_prob: float) -> float:
        """Map a single raw probability to a calibrated probability."""
        if not self.fitted:
            return float(raw_prob)
        p = raw_prob
        if p < 0.0:
            p = 0.0
        elif p > 1.0:
            p = 1.0
        return _sigmoid(self.a * p + self.b)

    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict:
        return {"a": self.a, "b": self.b, "fitted": self.fitted}

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "PlattCalibrator":
        if not data:
            return cls()
        return cls(
            a=float(data.get("a", 1.0)),
            b=float(data.get("b", 0.0)),
            fitted=bool(data.get("fitted", False)),
        )
