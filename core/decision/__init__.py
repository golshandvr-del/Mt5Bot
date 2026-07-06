# Decision engine package.
#
# Combines the four signal sources into one trade decision:
#   1. Indicator layer (Phase 2) - blended enabled-indicator signals, and/or the
#      memory-selected top strategies ensemble (Phase 3).
#   2. Learning core (Phase 1) - the active ML/RL/DL learner's directional score.
#   3. News layer (Phase 4) - aggregated sentiment signal.
# The weighted blend, thresholds, and agreement rules come from config.decision.
from core.decision.engine import DecisionEngine, Decision  # noqa: F401
