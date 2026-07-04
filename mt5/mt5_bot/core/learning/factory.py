"""
Learning-model factory.

Builds the requested learner from config/config.yaml -> learning. Keeps heavy
backends optional by importing each learner lazily only when requested.

Public functions
----------------
build_model(cfg, name)  : build a specific learner by name.
build_active_model(cfg) : build the learner named by learning.active_model.

If a learner cannot be created (missing optional dependency), the factory
returns a "none" placeholder that always predicts neutral, so the bot still
runs.

All text is standard ASCII English only.
"""

from __future__ import annotations

from typing import Any, Optional

from core.learning.base_model import BaseModel
from core.utils.logger import get_logger


class NeutralModel(BaseModel):
    """Always-neutral placeholder used when no learner is active/available."""

    kind = "none"

    def fit(self, X, y) -> None:
        self.trained = True

    def predict_proba_up(self, x) -> float:
        return 0.5

    def predict_signal(self, x) -> float:
        return 0.0

    def save(self, path: str) -> bool:
        return True

    def load(self, path: str) -> bool:
        self.trained = True
        return True


def _model_cfg_for(cfg: Any, name: str) -> Any:
    return cfg.get_path("learning.%s" % name, {})


def build_model(cfg: Any, name: str) -> BaseModel:
    """Build a specific learner by name."""
    log = get_logger("learning.factory", cfg)
    name = (name or "none").lower()

    if name in ("none", ""):
        return NeutralModel(cfg, {})

    if name == "ml_classifier":
        from core.learning.ml_classifier import MLClassifier
        return MLClassifier(cfg, _model_cfg_for(cfg, name))

    if name == "rl_agent":
        from core.learning.rl_agent import RLAgent
        return RLAgent(cfg, _model_cfg_for(cfg, name))

    if name == "dl_classifier":
        from core.learning.dl_classifier import DLClassifier
        return DLClassifier(cfg, _model_cfg_for(cfg, name))

    if name == "transfer":
        from core.learning.transfer import TransferModel
        return TransferModel(cfg, _model_cfg_for(cfg, name))

    if name == "self_supervised":
        from core.learning.self_supervised import SelfSupervisedEncoder
        return SelfSupervisedEncoder(cfg, _model_cfg_for(cfg, name))

    log.warning("Unknown learner '%s'; using NeutralModel.", name)
    return NeutralModel(cfg, {})


def build_active_model(cfg: Any) -> BaseModel:
    """Build the learner selected by learning.active_model in config."""
    name = cfg.get_path("learning.active_model", "ml_classifier")
    model = build_model(cfg, name)
    # If the chosen learner is disabled or unavailable, fall back to neutral.
    enabled = cfg.get_path("learning.%s.enabled" % name, True)
    if not enabled or not getattr(model, "available", True):
        log = get_logger("learning.factory", cfg)
        log.warning(
            "Active model '%s' is disabled or unavailable; using NeutralModel.",
            name,
        )
        return NeutralModel(cfg, {})
    return model
