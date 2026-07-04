"""
Lightweight ML classifier (Phase 1, the recommended learner for weak hardware).

Backends, in order of preference:
  1. LightGBM (config: backend = "lightgbm") - fast gradient boosting, CPU only.
  2. scikit-learn GradientBoosting / HistGradientBoosting (backend = "sklearn_gbdt").
  3. Pure-Python logistic-regression fallback (no third-party deps) so the bot
     still trains and predicts even on a minimal Python install.

The classifier predicts a 3-class label (-1, 0, +1) but the interface exposes
predict_proba_up() = P(label == +1), which the decision engine consumes.

All text is standard ASCII English only.
"""

from __future__ import annotations

import os
import pickle
from typing import Any, List, Optional

from core.learning.base_model import BaseModel
from core.utils.logger import get_logger


def _try_import(name: str):
    try:
        return __import__(name)
    except Exception:
        return None


class _PurePythonLogReg(object):
    """
    Minimal multinomial-ish logistic regression for 3 classes (-1,0,+1),
    trained with simple gradient descent. Used only when no ML library exists.
    This is intentionally simple; install LightGBM for real performance.
    """

    def __init__(self, n_features: int, lr: float = 0.05, epochs: int = 200):
        self.classes = [-1, 0, 1]
        self.lr = lr
        self.epochs = epochs
        # One weight vector + bias per class.
        self.w = [[0.0] * n_features for _ in self.classes]
        self.b = [0.0 for _ in self.classes]
        self.n_features = n_features

    @staticmethod
    def _softmax(scores: List[float]) -> List[float]:
        m = max(scores)
        exps = [pow(2.718281828, s - m) for s in scores]
        total = sum(exps) or 1.0
        return [e / total for e in exps]

    def _scores(self, x: List[float]) -> List[float]:
        out = []
        for k in range(len(self.classes)):
            s = self.b[k]
            wk = self.w[k]
            for j in range(self.n_features):
                s += wk[j] * x[j]
            out.append(s)
        return out

    def fit(self, X: List[List[float]], y: List[int]) -> None:
        idx = {c: i for i, c in enumerate(self.classes)}
        for _ in range(self.epochs):
            for xi, yi in zip(X, y):
                probs = self._softmax(self._scores(xi))
                target = idx.get(yi, 1)
                for k in range(len(self.classes)):
                    err = probs[k] - (1.0 if k == target else 0.0)
                    for j in range(self.n_features):
                        self.w[k][j] -= self.lr * err * xi[j]
                    self.b[k] -= self.lr * err

    def predict_proba(self, x: List[float]) -> List[float]:
        return self._softmax(self._scores(x))


class MLClassifier(BaseModel):
    kind = "ml_classifier"

    def __init__(self, cfg: Any, model_cfg: Any):
        super().__init__(cfg, model_cfg)
        self.log = get_logger("learning.ml_classifier", cfg)
        self.backend_name = model_cfg.get("backend", "lightgbm") if hasattr(model_cfg, "get") else "lightgbm"
        self.model = None
        self.fallback = None
        self.n_features = 0
        # Map model class index -> label for libraries that reorder classes.
        self._class_order: List[int] = [-1, 0, 1]

    # ------------------------------------------------------------------ #
    def _build_backend(self, n_features: int):
        """Instantiate the best available backend estimator."""
        mc = self.model_cfg
        n_est = int(mc.get("n_estimators", 300)) if hasattr(mc, "get") else 300
        lr = float(mc.get("learning_rate", 0.05)) if hasattr(mc, "get") else 0.05
        depth = int(mc.get("max_depth", 6)) if hasattr(mc, "get") else 6

        if self.backend_name == "lightgbm":
            lgb = _try_import("lightgbm")
            if lgb is not None:
                self.log.info("Using LightGBM backend.")
                return lgb.LGBMClassifier(
                    n_estimators=n_est,
                    learning_rate=lr,
                    max_depth=depth,
                    num_leaves=31,
                    subsample=0.9,
                    colsample_bytree=0.9,
                    n_jobs=1,            # single thread is fine on weak CPUs
                    verbose=-1,
                )
            self.log.warning("LightGBM not available; trying scikit-learn.")

        sk = _try_import("sklearn")
        if sk is not None:
            try:
                from sklearn.ensemble import HistGradientBoostingClassifier
                self.log.info("Using sklearn HistGradientBoostingClassifier.")
                return HistGradientBoostingClassifier(
                    max_iter=n_est, learning_rate=lr, max_depth=depth
                )
            except Exception:
                from sklearn.ensemble import GradientBoostingClassifier
                self.log.info("Using sklearn GradientBoostingClassifier.")
                return GradientBoostingClassifier(
                    n_estimators=n_est, learning_rate=lr, max_depth=depth
                )

        self.log.warning(
            "No ML library found; falling back to pure-Python logistic "
            "regression. Install LightGBM for real performance."
        )
        self.fallback = _PurePythonLogReg(n_features)
        return None

    # ------------------------------------------------------------------ #
    def fit(self, X: List[List[float]], y: List[int]) -> None:
        if not X:
            self.log.error("No training data provided to MLClassifier.fit.")
            self.trained = False
            return
        self.n_features = len(X[0])
        self.model = self._build_backend(self.n_features)

        if self.model is not None:
            try:
                self.model.fit(X, y)
                # Record class order for proba mapping.
                if hasattr(self.model, "classes_"):
                    self._class_order = list(self.model.classes_)
                self.trained = True
                self.log.info("MLClassifier trained on %d samples.", len(X))
                return
            except Exception as exc:
                self.log.error("Backend fit failed (%s); using fallback.", exc)
                self.model = None
                self.fallback = _PurePythonLogReg(self.n_features)

        # Pure-Python fallback path.
        if self.fallback is not None:
            self.fallback.fit(X, y)
            self._class_order = [-1, 0, 1]
            self.trained = True
            self.log.info("Fallback logistic model trained on %d samples.", len(X))

    # ------------------------------------------------------------------ #
    def predict_proba_up(self, x: List[float]) -> float:
        if not self.is_ready():
            return 0.5
        try:
            if self.model is not None:
                proba = self.model.predict_proba([x])[0]
                # Find probability mass on class +1.
                for cls, p in zip(self._class_order, proba):
                    if cls == 1:
                        return float(p)
                return 0.5
            if self.fallback is not None:
                proba = self.fallback.predict_proba(x)
                # fallback order is fixed [-1, 0, 1].
                return float(proba[2])
        except Exception as exc:
            self.log.error("predict_proba_up error: %s", exc)
        return 0.5

    # ------------------------------------------------------------------ #
    def save(self, path: str) -> bool:
        try:
            directory = os.path.dirname(os.path.abspath(path))
            if directory:
                os.makedirs(directory, exist_ok=True)
            blob = {
                "backend_name": self.backend_name,
                "n_features": self.n_features,
                "class_order": self._class_order,
                "model": self.model,
                "fallback": self.fallback,
                "trained": self.trained,
            }
            with open(path, "wb") as handle:
                # Protocol 4 is readable by every Python >= 3.4, keeping model
                # files portable across the training box and a Windows 7 /
                # Python 3.8 live machine (avoids protocol-5-only pickles).
                pickle.dump(blob, handle, protocol=4)
            self.log.info("Saved MLClassifier to %s", path)
            return True
        except Exception as exc:
            self.log.error("save error: %s", exc)
            return False

    def load(self, path: str) -> bool:
        try:
            if not os.path.exists(path):
                self.log.warning("Model file not found: %s", path)
                return False
            with open(path, "rb") as handle:
                blob = pickle.load(handle)
            self.backend_name = blob.get("backend_name", self.backend_name)
            self.n_features = blob.get("n_features", 0)
            self._class_order = blob.get("class_order", [-1, 0, 1])
            self.model = blob.get("model")
            self.fallback = blob.get("fallback")
            self.trained = bool(blob.get("trained", False))
            self.log.info("Loaded MLClassifier from %s", path)
            return self.trained
        except Exception as exc:
            self.log.error("load error: %s", exc)
            return False
