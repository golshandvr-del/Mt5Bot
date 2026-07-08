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

import json
import os
import pickle
from typing import Any, List, Optional

from core.learning.base_model import BaseModel
from core.learning.calibration import PlattCalibrator
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
        # Phase 5: optional probability calibration (default off unless enabled).
        self.calibrate_enabled = bool(
            model_cfg.get("calibrate", False)
        ) if hasattr(model_cfg, "get") else False
        self.calibrator = PlattCalibrator()  # identity until fitted
        # Feature names (set by the training pipeline for importance export).
        self.feature_names: List[str] = []

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
    def set_feature_names(self, names: List[str]) -> None:
        """Record human-readable feature names for importance export (Phase 5)."""
        try:
            self.feature_names = list(names) if names else []
        except Exception:
            self.feature_names = []

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
                self._maybe_calibrate(X, y)
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
            self._maybe_calibrate(X, y)

    # ------------------------------------------------------------------ #
    def _maybe_calibrate(self, X: List[List[float]], y: List[int]) -> None:
        """
        Phase 5: fit the Platt calibrator on a held-out tail of the training
        data. We use the most recent ~20% of samples (time-ordered) as the
        calibration set so calibration reflects newer market behaviour. Never
        fatal: on any problem we simply keep the identity calibrator.
        """
        if not self.calibrate_enabled:
            self.calibrator = PlattCalibrator()
            return
        try:
            n = len(X)
            hold = max(30, int(n * 0.2))
            if hold >= n:
                # Not enough data to hold out; skip (keep identity).
                self.calibrator = PlattCalibrator()
                return
            cal_X = X[n - hold:]
            cal_y = y[n - hold:]
            raw = [self._raw_proba_up(x) for x in cal_X]
            outcomes = [1 if yi == 1 else 0 for yi in cal_y]
            calibr = PlattCalibrator()
            ok = calibr.fit(raw, outcomes)
            self.calibrator = calibr
            self.log.info("Calibration fitted=%s on %d held-out samples.",
                          ok, hold)
        except Exception as exc:
            self.log.error("Calibration error (%s); using identity.", exc)
            self.calibrator = PlattCalibrator()

    # ------------------------------------------------------------------ #
    def _raw_proba_up(self, x: List[float]) -> float:
        """Uncalibrated P(label == +1) straight from the backend."""
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

    def predict_proba_up(self, x: List[float]) -> float:
        if not self.is_ready():
            return 0.5
        raw = self._raw_proba_up(x)
        # Phase 5: apply calibration if it was fitted (identity otherwise).
        try:
            return float(self.calibrator.transform(raw))
        except Exception:
            return raw

    # ------------------------------------------------------------------ #
    def export_feature_importances(self, model_path: str) -> Optional[str]:
        """
        Phase 5: write feature importances (when the backend exposes them) to a
        JSON sidecar next to the model file, e.g. models/ml_classifier.pkl ->
        models/ml_classifier.pkl.importances.json. Returns the path or None.
        Zero runtime cost live; purely an offline transparency artifact.
        """
        if self.model is None:
            return None
        importances = None
        try:
            if hasattr(self.model, "feature_importances_"):
                importances = list(self.model.feature_importances_)
        except Exception:
            importances = None
        if not importances:
            return None
        try:
            names = self.feature_names
            if not names or len(names) != len(importances):
                names = ["f%d" % i for i in range(len(importances))]
            pairs = sorted(
                ({"feature": n, "importance": float(v)}
                 for n, v in zip(names, importances)),
                key=lambda d: d["importance"], reverse=True,
            )
            out_path = model_path + ".importances.json"
            directory = os.path.dirname(os.path.abspath(out_path))
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(out_path, "w") as handle:
                json.dump({"backend": self.backend_name,
                           "importances": pairs}, handle, indent=2)
            self.log.info("Wrote feature importances to %s", out_path)
            return out_path
        except Exception as exc:
            self.log.error("export_feature_importances error: %s", exc)
            return None

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
                # Phase 5 persisted extras.
                "calibrator": self.calibrator.to_dict(),
                "feature_names": self.feature_names,
            }
            with open(path, "wb") as handle:
                # Protocol 4 is readable by every Python >= 3.4, keeping model
                # files portable across the training box and a Windows 7 /
                # Python 3.8 live machine (avoids protocol-5-only pickles).
                pickle.dump(blob, handle, protocol=4)
            self.log.info("Saved MLClassifier to %s", path)
            # Phase 5: best-effort feature-importance sidecar (never fatal).
            self.export_feature_importances(path)
            return True
        except Exception as exc:
            self.log.error("save error: %s", exc)
            return False

    @staticmethod
    def _is_incompatible_pickle_error(exc: Exception) -> bool:
        """
        True when `exc` looks like a model file that was written by an
        INCOMPATIBLE version of a scientific library and therefore can never be
        unpickled in this environment (so retrying is pointless and the file
        should be quarantined + retrained).

        The classic case on the Windows 7 / Python 3.8 / NumPy 1.x live box is a
        model that was trained elsewhere on NumPy 2.x: NumPy 2 moved its C guts
        from ``numpy.core`` to the private ``numpy._core`` module, so unpickling
        raises ``ModuleNotFoundError: No module named 'numpy._core'`` (or the
        reverse when a NumPy-1 file is opened on NumPy 2). We also catch the
        analogous scikit-learn / LightGBM cross-version breakages.
        """
        msg = str(exc).lower()
        needles = (
            "numpy._core",          # NumPy 2.x file opened on NumPy 1.x
            "numpy.core",           # NumPy 1.x file opened on NumPy 2.x
            "no module named 'numpy",
            "no module named 'sklearn",
            "no module named 'lightgbm",
            "incompatible",
            "unsupported pickle protocol",
        )
        if any(n in msg for n in needles):
            return True
        # ModuleNotFoundError from a stale library layout is always terminal.
        return isinstance(exc, ModuleNotFoundError)

    def _quarantine_bad_model(self, path: str, reason: str) -> None:
        """
        Move an unloadable model file aside (``<path>.incompatible``) so the very
        next training run writes a clean file instead of tripping over the old
        one forever. Best-effort: never fatal if the rename itself fails.
        """
        try:
            bad_path = path + ".incompatible"
            # If a previous quarantine file exists, drop it first so rename works
            # on platforms (Windows) where os.replace onto an existing name is OK
            # but a plain rename is not.
            if os.path.exists(bad_path):
                try:
                    os.remove(bad_path)
                except OSError:
                    pass
            os.replace(path, bad_path)
            self.log.warning(
                "Quarantined incompatible model %s -> %s (%s). "
                "Re-run training to rebuild a clean model.",
                path, bad_path, reason,
            )
        except Exception as exc:  # pragma: no cover - defensive only
            self.log.warning(
                "Could not quarantine incompatible model %s (%s); "
                "delete it manually and retrain.", path, exc)

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
            # Phase 5 persisted extras (backward compatible with old files).
            self.calibrator = PlattCalibrator.from_dict(blob.get("calibrator"))
            self.feature_names = blob.get("feature_names", []) or []
            self.log.info("Loaded MLClassifier from %s", path)
            return self.trained
        except Exception as exc:
            # A cross-version pickle (e.g. a NumPy 2.x model on a NumPy 1.x box:
            # "No module named 'numpy._core'") can NEVER be read here, so we
            # quarantine the file and start fresh instead of failing forever.
            if self._is_incompatible_pickle_error(exc):
                self.log.error(
                    "Model %s was saved by an incompatible library version "
                    "(%s). It cannot be loaded on this machine; quarantining "
                    "it so the next 'train' run rebuilds a clean model.",
                    path, exc,
                )
                self._quarantine_bad_model(path, str(exc))
            else:
                self.log.error("load error: %s", exc)
            # Reset to a clean, un-trained state so a stale/partial blob can
            # never leak into predictions.
            self.model = None
            self.fallback = None
            self.trained = False
            return False
