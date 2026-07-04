"""
Transfer learning module (Phase 1) - OPTIONAL and isolated.

Idea: take a deep model trained on one symbol/timeframe (the "source"), freeze
its early layers, and fine-tune the last layers on a new "target" symbol with
fewer samples. This reuses learned price-pattern representations.

This module depends on the deep-learning backend (Keras/TensorFlow). If that is
unavailable (typical on Windows 7 / CPU-only), it marks itself unavailable and
the bot keeps running. Use it only in the offline training phase on a capable
machine, then ship the resulting light artifact.

All text is standard ASCII English only.
"""

from __future__ import annotations

import os
from typing import Any, List, Optional

from core.learning.base_model import BaseModel
from core.learning.dl_classifier import _try_keras
from core.utils.logger import get_logger


class TransferModel(BaseModel):
    kind = "transfer"

    def __init__(self, cfg: Any, model_cfg: Any):
        super().__init__(cfg, model_cfg)
        self.log = get_logger("learning.transfer", cfg)
        self.keras, _ = _try_keras()
        self.model = None
        self.freeze_layers = int(model_cfg.get("freeze_layers", 2)) if hasattr(model_cfg, "get") else 2
        self.epochs = int(model_cfg.get("epochs", 10)) if hasattr(model_cfg, "get") else 10
        self.source_path = model_cfg.get("source_model_file", "") if hasattr(model_cfg, "get") else ""
        if self.keras is None:
            self.available = False
            self.log.warning(
                "Transfer learning disabled: deep-learning backend missing. "
                "Expected on Windows 7 / CPU-only."
            )

    def _load_source(self):
        """Load and partially freeze the source model."""
        if not self.source_path or not os.path.exists(self.source_path):
            self.log.error("Source model not found: %s", self.source_path)
            return None
        try:
            model = self.keras.models.load_model(self.source_path)
            # Freeze the first N layers; fine-tune the rest.
            for i, layer in enumerate(model.layers):
                layer.trainable = i >= self.freeze_layers
            model.compile(
                optimizer="adam",
                loss="sparse_categorical_crossentropy",
                metrics=["accuracy"],
            )
            return model
        except Exception as exc:
            self.log.error("Failed to load/prepare source model: %s", exc)
            return None

    def fit(self, X: List[List[float]], y: List[int]) -> None:
        if not self.available or not X:
            self.trained = False
            return
        try:
            import numpy as np  # type: ignore
            self.model = self._load_source()
            if self.model is None:
                self.available = False
                return
            Xa = np.array(X, dtype="float32")
            ya = np.array([yy + 1 for yy in y], dtype="int64")
            self.model.fit(Xa, ya, epochs=self.epochs, verbose=0)
            self.trained = True
            self.log.info("Transfer fine-tuning complete on %d samples.", len(X))
        except Exception as exc:
            self.log.error("Transfer fit failed: %s", exc)
            self.available = False
            self.trained = False

    def predict_proba_up(self, x: List[float]) -> float:
        if not self.is_ready():
            return 0.5
        try:
            import numpy as np  # type: ignore
            proba = self.model.predict(np.array([x], dtype="float32"), verbose=0)[0]
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
