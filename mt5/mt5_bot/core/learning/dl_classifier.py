"""
Optional deep-learning classifier (Phase 1) - small Keras/TensorFlow MLP.

THIS MODULE IS OFF BY DEFAULT and clearly isolated. Deep learning is HEAVY and
TensorFlow generally does NOT support Windows 7. Only enable this if you train
on a separate, more capable machine (train-offline / run-light architecture).

If TensorFlow (or Keras) is not importable, the model marks itself unavailable
and the bot keeps running with other learners. The decision engine never
crashes because of a missing deep-learning backend.

All text is standard ASCII English only.
"""

from __future__ import annotations

import os
from typing import Any, List, Optional

from core.learning.base_model import BaseModel
from core.utils.logger import get_logger


def _try_keras():
    """Return a (keras, source) tuple if a Keras-like API is available."""
    try:
        from tensorflow import keras  # type: ignore
        return keras, "tensorflow.keras"
    except Exception:
        pass
    try:
        import keras  # type: ignore
        return keras, "keras"
    except Exception:
        return None, None


class DLClassifier(BaseModel):
    kind = "dl_classifier"

    def __init__(self, cfg: Any, model_cfg: Any):
        super().__init__(cfg, model_cfg)
        self.log = get_logger("learning.dl_classifier", cfg)
        self.keras, src = _try_keras()
        self.model = None
        self.n_features = 0
        self.epochs = int(model_cfg.get("epochs", 20)) if hasattr(model_cfg, "get") else 20
        self.batch_size = int(model_cfg.get("batch_size", 64)) if hasattr(model_cfg, "get") else 64
        if self.keras is None:
            self.available = False
            self.log.warning(
                "Deep-learning backend not available (TensorFlow/Keras not "
                "installed). DLClassifier disabled. This is expected on "
                "Windows 7 / CPU-only setups."
            )
        else:
            self.log.info("DLClassifier using backend: %s", src)

    def _build_network(self, n_features: int):
        keras = self.keras
        model = keras.Sequential(
            [
                keras.layers.Input(shape=(n_features,)),
                keras.layers.Dense(32, activation="relu"),
                keras.layers.Dense(16, activation="relu"),
                # 3 classes mapped to indices 0,1,2 == labels -1,0,+1.
                keras.layers.Dense(3, activation="softmax"),
            ]
        )
        model.compile(
            optimizer="adam",
            loss="sparse_categorical_crossentropy",
            metrics=["accuracy"],
        )
        return model

    def fit(self, X: List[List[float]], y: List[int]) -> None:
        if not self.available or not X:
            self.trained = False
            return
        try:
            import numpy as np  # type: ignore
            self.n_features = len(X[0])
            Xa = np.array(X, dtype="float32")
            # Map labels -1,0,1 -> 0,1,2.
            ya = np.array([yy + 1 for yy in y], dtype="int64")
            self.model = self._build_network(self.n_features)
            self.model.fit(
                Xa, ya, epochs=self.epochs, batch_size=self.batch_size, verbose=0
            )
            self.trained = True
            self.log.info("DLClassifier trained on %d samples.", len(X))
        except Exception as exc:
            self.log.error("DLClassifier fit failed: %s", exc)
            self.available = False
            self.trained = False

    def predict_proba_up(self, x: List[float]) -> float:
        if not self.is_ready():
            return 0.5
        try:
            import numpy as np  # type: ignore
            proba = self.model.predict(np.array([x], dtype="float32"), verbose=0)[0]
            # Index 2 == label +1.
            return float(proba[2])
        except Exception as exc:
            self.log.error("predict_proba_up error: %s", exc)
            return 0.5

    def save(self, path: str) -> bool:
        if not self.is_ready():
            return False
        try:
            directory = os.path.dirname(os.path.abspath(path))
            if directory:
                os.makedirs(directory, exist_ok=True)
            self.model.save(path)
            return True
        except Exception as exc:
            self.log.error("save error: %s", exc)
            return False

    def load(self, path: str) -> bool:
        if not self.available:
            return False
        try:
            if not os.path.exists(path):
                return False
            self.model = self.keras.models.load_model(path)
            self.trained = True
            return True
        except Exception as exc:
            self.log.error("load error: %s", exc)
            return False
