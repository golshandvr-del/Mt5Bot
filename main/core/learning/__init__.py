# Learning core package (Phase 1).
#
# Provides a common interface (BaseModel) and several swappable, optional
# learners: lightweight ML (LightGBM/sklearn), tabular RL, optional deep MLP,
# transfer learning, and a self-supervised autoencoder feature learner.
#
# Heavy backends (deep learning, transfer) are optional and only imported when
# enabled, so the bot still runs on a CPU-only Windows 7 machine.
from core.learning.base_model import BaseModel, ModelPrediction  # noqa: F401
from core.learning.features import FeatureBuilder  # noqa: F401
from core.learning.factory import build_active_model, build_model  # noqa: F401
