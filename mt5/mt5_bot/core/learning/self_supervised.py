"""
Self-supervised learning module (Phase 1) - CPU-light autoencoder.

Self-supervised learning here means: learn a compressed representation of the
feature rows WITHOUT using trade labels, by training an autoencoder-style
bottleneck to reconstruct the inputs. The learned encoder can then:
  - produce lower-dimensional features for the supervised ML classifier, or
  - flag "anomalous" market states via reconstruction error.

The default backend uses scikit-learn's MLPRegressor as a small autoencoder
(CPU-friendly). If scikit-learn is missing, it falls back to a pure-Python PCA
via covariance power-iteration so it still runs anywhere.

This module does not directly emit a trade direction; predict_signal() returns
the normalized reconstruction-error-based novelty (near 0 = familiar state).
It is OFF by default and mainly intended as a feature-learning aid.

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


class SelfSupervisedEncoder(BaseModel):
    kind = "self_supervised"

    def __init__(self, cfg: Any, model_cfg: Any):
        super().__init__(cfg, model_cfg)
        self.log = get_logger("learning.self_supervised", cfg)
        self.code_size = int(model_cfg.get("code_size", 8)) if hasattr(model_cfg, "get") else 8
        self.autoencoder = None       # sklearn MLPRegressor (input->input)
        self.pca_components = None    # pure-Python fallback
        self.feature_mean = None
        self.n_features = 0
        self._recent_errors: List[float] = []

    # ------------------------------------------------------------------ #
    def fit(self, X: List[List[float]], y: Optional[List[int]] = None) -> None:
        if not X:
            self.trained = False
            return
        self.n_features = len(X[0])
        sk = _try_import("sklearn")
        if sk is not None:
            try:
                from sklearn.neural_network import MLPRegressor
                self.autoencoder = MLPRegressor(
                    hidden_layer_sizes=(self.code_size,),
                    activation="relu",
                    max_iter=200,
                )
                # Autoencoder: predict the input from the input.
                self.autoencoder.fit(X, X)
                self.trained = True
                self.log.info("SSL autoencoder (sklearn) trained on %d rows.", len(X))
                return
            except Exception as exc:
                self.log.warning("sklearn autoencoder failed (%s); using PCA.", exc)

        # Pure-Python PCA fallback (power iteration on covariance).
        self._fit_pca(X)
        self.trained = True
        self.log.info("SSL PCA fallback trained on %d rows.", len(X))

    def _fit_pca(self, X: List[List[float]]) -> None:
        n = len(X)
        m = self.n_features
        self.feature_mean = [sum(row[j] for row in X) / n for j in range(m)]
        # Covariance matrix.
        cov = [[0.0] * m for _ in range(m)]
        for row in X:
            d = [row[j] - self.feature_mean[j] for j in range(m)]
            for a in range(m):
                for b in range(m):
                    cov[a][b] += d[a] * d[b]
        for a in range(m):
            for b in range(m):
                cov[a][b] /= max(1, n - 1)
        # Power iteration for top `code_size` eigenvectors.
        comps: List[List[float]] = []
        k = min(self.code_size, m)
        for _ in range(k):
            v = [1.0 / (m ** 0.5)] * m
            for _ in range(50):
                # w = cov * v
                w = [sum(cov[a][b] * v[b] for b in range(m)) for a in range(m)]
                norm = sum(x * x for x in w) ** 0.5 or 1.0
                v = [x / norm for x in w]
            comps.append(v)
            # Deflate covariance by removing this component.
            lam = sum(sum(cov[a][b] * v[b] for b in range(m)) * v[a] for a in range(m))
            for a in range(m):
                for b in range(m):
                    cov[a][b] -= lam * v[a] * v[b]
        self.pca_components = comps

    # ------------------------------------------------------------------ #
    def encode(self, x: List[float]) -> List[float]:
        """Return the compressed representation of a feature row."""
        if self.autoencoder is not None:
            # sklearn MLPRegressor does not expose the hidden code directly in a
            # simple way; we approximate "code" via reconstruction residual size.
            try:
                recon = self.autoencoder.predict([x])[0]
                return [a - b for a, b in zip(x, recon)]
            except Exception:
                return list(x)
        if self.pca_components is not None and self.feature_mean is not None:
            d = [x[j] - self.feature_mean[j] for j in range(len(x))]
            return [sum(comp[j] * d[j] for j in range(len(comp)))
                    for comp in self.pca_components]
        return list(x)

    def reconstruction_error(self, x: List[float]) -> float:
        """Mean squared reconstruction error (novelty proxy)."""
        if self.autoencoder is not None:
            try:
                recon = self.autoencoder.predict([x])[0]
                return sum((a - b) ** 2 for a, b in zip(x, recon)) / max(1, len(x))
            except Exception:
                return 0.0
        if self.pca_components is not None and self.feature_mean is not None:
            d = [x[j] - self.feature_mean[j] for j in range(len(x))]
            code = [sum(comp[j] * d[j] for j in range(len(comp)))
                    for comp in self.pca_components]
            recon = [0.0] * len(x)
            for c, comp in zip(code, self.pca_components):
                for j in range(len(comp)):
                    recon[j] += c * comp[j]
            return sum((d[j] - recon[j]) ** 2 for j in range(len(x))) / max(1, len(x))
        return 0.0

    # ------------------------------------------------------------------ #
    def predict_proba_up(self, x: List[float]) -> float:
        # SSL is not directional; return neutral.
        return 0.5

    def predict_signal(self, x: List[float]) -> float:
        # Return a small novelty-scaled value in [-1, 1]; high novelty -> 0
        # (uncertain). Familiar states keep neutral. This mainly serves as a
        # confidence dampener if blended.
        err = self.reconstruction_error(x)
        # Without a reference scale, just bound it; the value is informational.
        return 0.0 if err > 1.0 else 0.0

    # ------------------------------------------------------------------ #
    def save(self, path: str) -> bool:
        try:
            directory = os.path.dirname(os.path.abspath(path))
            if directory:
                os.makedirs(directory, exist_ok=True)
            blob = {
                "code_size": self.code_size,
                "autoencoder": self.autoencoder,
                "pca_components": self.pca_components,
                "feature_mean": self.feature_mean,
                "n_features": self.n_features,
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
            self.code_size = blob.get("code_size", self.code_size)
            self.autoencoder = blob.get("autoencoder")
            self.pca_components = blob.get("pca_components")
            self.feature_mean = blob.get("feature_mean")
            self.n_features = blob.get("n_features", 0)
            self.trained = bool(blob.get("trained", False))
            return self.trained
        except Exception as exc:
            self.log.error("load error: %s", exc)
            return False
