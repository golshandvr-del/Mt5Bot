"""
Decision engine - the layer that fuses every signal source into one action.

Inputs blended (weights and thresholds from config.decision):
  - indicators : blended signal from either
        (a) the memory-selected top strategies ensemble (Phase 3), if the
            registry has trusted strategies for this symbol/timeframe, else
        (b) the enabled stand-alone indicators (Phase 2).
  - learning   : the active learner's directional score (Phase 1), if ready.
  - news       : the aggregated news sentiment signal (Phase 4), if enabled.

Output:
  Decision(action, score, size_hint, sl_atr_mult, tp_atr_mult, reasons)
    action : +1 long, -1 short, 0 flat.

The engine is deliberately defensive: any missing/failed component contributes
0.0 and is simply dropped from the (re-normalized) weighted blend, so the bot
still produces sensible decisions on weak hardware with most features disabled.

All text is standard ASCII English only.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.indicators.registry import build_enabled_indicators
from core.strategy.strategy import Strategy, StrategySpec
from core.utils.logger import get_logger


class Decision(object):
    """Structured output of the decision engine for one symbol/bar."""

    __slots__ = ("action", "score", "size_hint", "sl_atr_mult", "tp_atr_mult",
                 "reasons", "components")

    def __init__(self, action: int = 0, score: float = 0.0,
                 size_hint: float = 1.0, sl_atr_mult: float = 2.0,
                 tp_atr_mult: float = 3.0,
                 reasons: Optional[List[str]] = None,
                 components: Optional[Dict[str, float]] = None):
        self.action = int(action)          # +1 long, -1 short, 0 flat
        self.score = float(score)          # final blended score in [-1, +1]
        self.size_hint = float(size_hint)  # 0..1 confidence multiplier for sizing
        self.sl_atr_mult = float(sl_atr_mult)
        self.tp_atr_mult = float(tp_atr_mult)
        self.reasons = reasons or []
        self.components = components or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "score": round(self.score, 4),
            "size_hint": round(self.size_hint, 4),
            "sl_atr_mult": self.sl_atr_mult,
            "tp_atr_mult": self.tp_atr_mult,
            "reasons": self.reasons,
            "components": {k: round(v, 4) for k, v in self.components.items()},
        }


class DecisionEngine(object):
    """
    Fuses indicator, learning, and news signals into one Decision.

    Constructed with the loaded config plus optional, already-built components:
      - learner       : a BaseModel (from learning.factory). May be None.
      - feature_builder : a FeatureBuilder used to make the learner's input row.
      - news_analyzer : a NewsAnalyzer (Phase 4). May be None.
      - memory        : a MemoryStore, used to load top strategies per symbol.

    Keeping these injectable makes the engine testable and lets the caller
    control how heavy each component is (or skip it entirely).
    """

    def __init__(self, cfg: Any, learner: Optional[object] = None,
                 feature_builder: Optional[object] = None,
                 news_analyzer: Optional[object] = None,
                 memory: Optional[object] = None):
        self.cfg = cfg
        self.log = get_logger("decision.engine", cfg)
        self.learner = learner
        self.feature_builder = feature_builder
        self.news = news_analyzer
        self.memory = memory

        dec = cfg.get_path("decision", {})
        weights = dec.get("weights", {}) if hasattr(dec, "get") else {}
        self.w_ind = float(weights.get("indicators", 0.5)) if hasattr(weights, "get") else 0.5
        self.w_learn = float(weights.get("learning", 0.3)) if hasattr(weights, "get") else 0.3
        self.w_news = float(weights.get("news", 0.2)) if hasattr(weights, "get") else 0.2
        self.long_threshold = float(dec.get("long_threshold", 0.6)) if hasattr(dec, "get") else 0.6
        self.short_threshold = float(dec.get("short_threshold", 0.6)) if hasattr(dec, "get") else 0.6
        self.require_agreement = bool(dec.get("require_agreement", False)) if hasattr(dec, "get") else False

        # Default risk exits (used when no memory strategy provides them).
        self.default_sl = float(cfg.get_path("risk.default_sl_atr_mult", 2.0))
        self.default_tp = float(cfg.get_path("risk.default_tp_atr_mult", 3.0))

        # Fallback stand-alone indicators (Phase 2).
        self._indicators = build_enabled_indicators(cfg)
        # Cache of memory-loaded strategy ensembles per (symbol, timeframe).
        self._ensemble_cache: Dict[str, List[Strategy]] = {}

    # ------------------------------------------------------------------ #
    # Component signals.
    # ------------------------------------------------------------------ #
    def _ensemble_for(self, symbol: str, timeframe: str) -> List[Strategy]:
        """Load and cache the memory top-strategy ensemble for symbol/timeframe."""
        key = "%s|%s" % (symbol, timeframe)
        if key in self._ensemble_cache:
            return self._ensemble_cache[key]
        strategies: List[Strategy] = []
        if self.memory is not None:
            try:
                top = self.memory.load_registry_top(symbol, timeframe)
                for entry in top:
                    spec_dict = entry.get("spec", {})
                    if not spec_dict:
                        continue
                    spec = StrategySpec.from_dict(spec_dict)
                    strategies.append(Strategy(spec))
            except Exception as exc:
                self.log.error("Ensemble load failed for %s: %s", key, exc)
        self._ensemble_cache[key] = strategies
        return strategies

    def _indicator_signal(self, ohlcv: Any, symbol: str,
                          timeframe: str) -> (float, str, float, float):
        """
        Return (signal, source_label, sl_atr_mult, tp_atr_mult).

        Prefers the memory-selected top-strategies ensemble when available;
        otherwise blends the stand-alone enabled indicators equally.
        """
        ensemble = self._ensemble_for(symbol, timeframe)
        if ensemble:
            total = 0.0
            sl_acc = 0.0
            tp_acc = 0.0
            n = 0
            for strat in ensemble:
                try:
                    total += strat.blended_signal(ohlcv)
                    sl_acc += strat.spec.sl_atr_mult
                    tp_acc += strat.spec.tp_atr_mult
                    n += 1
                except Exception:
                    continue
            if n > 0:
                return (max(-1.0, min(1.0, total / n)), "ensemble",
                        sl_acc / n, tp_acc / n)

        # Fallback: equal-weight blend of stand-alone enabled indicators.
        if not self._indicators:
            return (0.0, "none", self.default_sl, self.default_tp)
        acc = 0.0
        n = 0
        for name, ind in self._indicators.items():
            try:
                # Phase 5: use the health-guarded wrapper so degenerate/NaN
                # series contribute a neutral 0.0 instead of noise.
                acc += ind.safe_signal(ohlcv)
                n += 1
            except Exception:
                continue
        sig = (acc / n) if n > 0 else 0.0
        return (max(-1.0, min(1.0, sig)), "indicators",
                self.default_sl, self.default_tp)

    def _learning_signal(self, ohlcv: Any) -> float:
        """Return the active learner's directional signal in [-1, +1] or 0.0."""
        if self.learner is None or self.feature_builder is None:
            return 0.0
        try:
            if not getattr(self.learner, "is_ready", lambda: False)():
                return 0.0
            row = self.feature_builder.build_inference_row(ohlcv)
            if row is None:
                return 0.0
            return max(-1.0, min(1.0, float(self.learner.predict_signal(row))))
        except Exception as exc:
            self.log.error("Learning signal error: %s", exc)
            return 0.0

    def _news_signal(self, symbol: str) -> float:
        """Return the news sentiment signal in [-1, +1] or 0.0."""
        if self.news is None:
            return 0.0
        try:
            return max(-1.0, min(1.0, float(self.news.get_signal(symbol))))
        except Exception as exc:
            self.log.error("News signal error: %s", exc)
            return 0.0

    # ------------------------------------------------------------------ #
    # Public: produce a decision.
    # ------------------------------------------------------------------ #
    def decide(self, ohlcv: Any, symbol: str, timeframe: str) -> Decision:
        """
        Blend all available signals into a Decision for the latest bar.

        Steps:
          1. Gather component signals (indicators/ensemble, learning, news).
          2. Weighted-average using config weights, re-normalized over only the
             components that actually contributed (non-None).
          3. Apply optional news blackout and agreement rules.
          4. Threshold the final score into an action and derive a size hint.
        """
        reasons: List[str] = []
        components: Dict[str, float] = {}

        ind_sig, ind_src, sl_mult, tp_mult = self._indicator_signal(
            ohlcv, symbol, timeframe
        )
        learn_sig = self._learning_signal(ohlcv)
        news_sig = self._news_signal(symbol)

        # Build the weighted blend over contributing components only.
        parts = []
        if abs(self.w_ind) > 0:
            parts.append((self.w_ind, ind_sig, "indicators(%s)" % ind_src))
            components["indicators"] = ind_sig
        if abs(self.w_learn) > 0 and self.learner is not None:
            parts.append((self.w_learn, learn_sig, "learning"))
            components["learning"] = learn_sig
        if abs(self.w_news) > 0 and self.news is not None:
            parts.append((self.w_news, news_sig, "news"))
            components["news"] = news_sig

        weight_total = sum(abs(w) for w, _, _ in parts)
        if weight_total <= 0.0:
            score = 0.0
        else:
            score = sum(w * s for w, s, _ in parts) / weight_total
        score = max(-1.0, min(1.0, score))

        for w, s, label in parts:
            reasons.append("%s=%.3f(w=%.2f)" % (label, s, w))

        # News blackout: block NEW entries during fresh high-impact news.
        blackout = False
        if self.news is not None:
            try:
                blackout = bool(self.news.in_blackout(symbol))
            except Exception:
                blackout = False
        if blackout:
            reasons.append("news_blackout=1")

        # Optional agreement rule: indicators and learning must share direction.
        agree_ok = True
        if self.require_agreement and self.learner is not None:
            if ind_sig != 0.0 and learn_sig != 0.0:
                agree_ok = (ind_sig > 0) == (learn_sig > 0)
            reasons.append("agreement_ok=%d" % int(agree_ok))

        # Threshold into an action.
        action = 0
        if not blackout and agree_ok:
            if score >= self.long_threshold:
                action = 1
            elif score <= -self.short_threshold:
                action = -1

        # Size hint scales with how far the score exceeds the threshold.
        size_hint = 0.0
        if action == 1 and self.long_threshold < 1.0:
            size_hint = (score - self.long_threshold) / (1.0 - self.long_threshold)
        elif action == -1 and self.short_threshold < 1.0:
            size_hint = (-score - self.short_threshold) / (1.0 - self.short_threshold)
        size_hint = max(0.0, min(1.0, size_hint))

        return Decision(
            action=action,
            score=score,
            size_hint=size_hint,
            sl_atr_mult=sl_mult,
            tp_atr_mult=tp_mult,
            reasons=reasons,
            components=components,
        )
