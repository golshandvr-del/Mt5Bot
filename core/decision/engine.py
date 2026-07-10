"""
Decision engine - the layer that fuses every signal source into one action.

Inputs blended (weights and thresholds from config.decision):
  - indicators : blended signal from either
        (a) the memory-selected top strategies ensemble (Phase 3), if the
            registry has trusted strategies for this symbol/timeframe, else
        (b) the enabled stand-alone indicators (Phase 2).
  - learning   : the active learner's directional score (Phase 1), if ready.
  - news       : the aggregated news sentiment signal (Phase 4), if enabled.
  - timing     : (Phase 5, user-update-request) an optional time/session/season
        awareness. By default it is applied as a CONFIDENCE / SIZE modifier and
        an optional entry GATE (a session being profitable does not tell you the
        direction), driven ONLY by time buckets the bot has LEARNED to trust from
        its own historical trades. If timing.as_directional=true it also adds a
        directional vote with the decision.weights.timing weight.

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
      - learner       : a BaseModel (from learning.factory). May be None. Used
                        as the default/shared learner (and the fallback when a
                        per-symbol lookup is not available).
      - feature_builder : a FeatureBuilder used to make the learner's input row.
      - news_analyzer : a NewsAnalyzer (Phase 4). May be None.
      - memory        : a MemoryStore, used to load top strategies per symbol.
      - learner_provider : optional callable symbol -> learner (A5 / P3.4). When
                        supplied (e.g. BotContext.learner_for), the engine asks
                        for the deciding symbol's own learner so per-symbol ML
                        models are used. When None, the shared `learner` is used
                        for every symbol (unchanged light-path behavior).

    Keeping these injectable makes the engine testable and lets the caller
    control how heavy each component is (or skip it entirely).
    """

    def __init__(self, cfg: Any, learner: Optional[object] = None,
                 feature_builder: Optional[object] = None,
                 news_analyzer: Optional[object] = None,
                 memory: Optional[object] = None,
                 timing: Optional[object] = None,
                 learner_provider: Optional[object] = None,
                 council: Optional[object] = None,
                 decay_suspects: Optional[object] = None,
                 meta_labeler: Optional[object] = None):
        self.cfg = cfg
        self.log = get_logger("decision.engine", cfg)
        self.learner = learner
        # UPGRADE_PLAN U6.1: optional meta-labeling quality gate. When enabled
        # AND trained for the firing strategy it acts as a VETO-ONLY gate: it can
        # BLOCK a low-P(win) entry the validated strategy wanted, but never
        # creates, flips, or resizes a trade. Config-gated (decision.meta_label
        # .enabled, default OFF); when absent/disabled/untrained it never vetoes,
        # so the default path is unchanged.
        self.meta_labeler = meta_labeler
        # Optional callable symbol -> learner for per-symbol ML models (P3.4).
        self.learner_provider = learner_provider
        self.feature_builder = feature_builder
        self.news = news_analyzer
        self.memory = memory
        # Phase 5 (user-update-request): optional time/session/season provider.
        self.timing = timing
        # Phase 5 / P5.3 (Track B / B1): optional StrategyCouncil that supplies a
        # LIVE per-strategy credibility weight, used to blend the memory ensemble
        # by recent realized performance instead of a flat equal weight. It is
        # config-gated (decision.council.enabled, default OFF): when disabled or
        # absent, the ensemble blend stays the previous plain average so the
        # default light path is byte-for-byte unchanged.
        self.council = council
        self.council_enabled = bool(
            cfg.get_path("decision.council.enabled", False)
        )
        # Phase 5 / P5.6 (Track B / B3): set of strategy fingerprints the decay
        # monitor has flagged "suspect" (recent live PnL has drifted materially
        # below the walk-forward distribution they were promoted on). Suspect
        # strategies are zero-weighted (dropped) from the ensemble blend until
        # the next search re-validates them. Config-gated (decision.decay_monitor
        # .enabled) and computed by BotContext; None/empty means "flag nobody",
        # so the default light path is byte-for-byte unchanged.
        self.decay_suspects = set(decay_suspects) if decay_suspects else set()
        self.decay_enabled = bool(
            cfg.get_path("decision.decay_monitor.enabled", False)
        )

        dec = cfg.get_path("decision", {})
        # UPGRADE_PLAN U2.4: decision mode. "parity" (default) trades the
        # validated top-1 strategy exactly; "blend" is the legacy composite.
        mode_raw = dec.get("mode", "parity") if hasattr(dec, "get") else "parity"
        self.mode = str(mode_raw or "parity").strip().lower()
        if self.mode not in ("parity", "blend"):
            self.log.warning(
                "Unknown decision.mode '%s'; falling back to 'parity'.", mode_raw
            )
            self.mode = "parity"
        # Parity-mode veto switches (each can only BLOCK an entry, never make one).
        pv = dec.get("parity_vetoes", {}) if hasattr(dec, "get") else {}
        pv = pv if hasattr(pv, "get") else {}
        self.parity_veto_learner = bool(pv.get("learner", True))
        self.parity_veto_news = bool(pv.get("news", True))
        self.parity_veto_timing = bool(pv.get("timing", True))
        self.parity_learner_veto_level = float(
            dec.get("parity_learner_veto_level", 0.5)
        ) if hasattr(dec, "get") else 0.5

        weights = dec.get("weights", {}) if hasattr(dec, "get") else {}
        self.w_ind = float(weights.get("indicators", 0.5)) if hasattr(weights, "get") else 0.5
        self.w_learn = float(weights.get("learning", 0.3)) if hasattr(weights, "get") else 0.3
        self.w_news = float(weights.get("news", 0.2)) if hasattr(weights, "get") else 0.2
        self.w_timing = float(weights.get("timing", 0.0)) if hasattr(weights, "get") else 0.0
        # Whether the timing layer contributes a directional vote (default off).
        self.timing_directional = bool(cfg.get_path("timing.as_directional", False))
        # Whether unfavorable/blackout time windows block NEW entries.
        self.timing_gate = bool(cfg.get_path("timing.gate_unfavorable", False))
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
            use_council = self.council_enabled and self.council is not None
            use_decay = self.decay_enabled and bool(self.decay_suspects)
            sig_acc = 0.0     # sum of weight * signal
            sl_acc = 0.0      # sum of weight * sl_atr_mult
            tp_acc = 0.0      # sum of weight * tp_atr_mult
            w_total = 0.0     # sum of weights (== n when council is OFF)
            for strat in ensemble:
                # P5.6 (B3): a decay-suspect strategy is dropped entirely from
                # the blend (zero weight) until the next search re-validates it.
                if use_decay:
                    try:
                        if strat.spec.fingerprint() in self.decay_suspects:
                            continue
                    except Exception:
                        pass
                try:
                    sig = strat.blended_signal(ohlcv)
                except Exception:
                    continue
                # Live credibility weight (>=0). Equal weight (1.0) when the
                # council is disabled/absent, preserving the old plain average.
                w = 1.0
                if use_council:
                    try:
                        w = float(self.council.weight(strat.spec.fingerprint()))
                    except Exception:
                        w = 1.0
                    if w < 0.0:
                        w = 0.0
                sig_acc += w * sig
                sl_acc += w * strat.spec.sl_atr_mult
                tp_acc += w * strat.spec.tp_atr_mult
                w_total += w
            if w_total > 0.0:
                label = "ensemble+council" if use_council else "ensemble"
                if use_decay:
                    label += "+decay"
                return (max(-1.0, min(1.0, sig_acc / w_total)), label,
                        sl_acc / w_total, tp_acc / w_total)

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

    def _learner_for(self, symbol: str):
        """
        Resolve the learner to use for `symbol` (A5 / P3.4).

        If a learner_provider callable was injected, ask it for the symbol's
        learner (per-symbol ML); on any failure or if it returns None, fall back
        to the shared `self.learner` so the engine never crashes.
        """
        if self.learner_provider is not None:
            try:
                learner = self.learner_provider(symbol)
                if learner is not None:
                    return learner
            except Exception as exc:
                self.log.error("Per-symbol learner lookup failed for %s: %s",
                               symbol, exc)
        return self.learner

    def _learning_signal(self, ohlcv: Any, symbol: str) -> float:
        """Return the active learner's directional signal in [-1, +1] or 0.0."""
        learner = self._learner_for(symbol)
        if learner is None or self.feature_builder is None:
            return 0.0
        try:
            if not getattr(learner, "is_ready", lambda: False)():
                return 0.0
            row = self.feature_builder.build_inference_row(ohlcv)
            if row is None:
                return 0.0
            return max(-1.0, min(1.0, float(learner.predict_signal(row))))
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

    def _timing_signal(self, ohlcv: Any, symbol: str, timeframe: str):
        """
        Return the Phase 5 TimeSignal object (or None). Never raises. The
        TimeSignal is directionless by default; the engine uses its
        size_multiplier and favorable/blackout flags, and only uses its edge as
        a directional vote when timing.as_directional is enabled.
        """
        if self.timing is None:
            return None
        try:
            return self.timing.evaluate(ohlcv, symbol, timeframe)
        except Exception as exc:
            self.log.error("Timing signal error: %s", exc)
            return None

    # ------------------------------------------------------------------ #
    # Parity mode (UPGRADE_PLAN U2.4): trade the validated top-1 strategy.
    # ------------------------------------------------------------------ #
    def _decide_parity(self, ohlcv: Any, symbol: str,
                       timeframe: str) -> Decision:
        """
        Trade the TOP-1 registry strategy EXACTLY as it was validated.

        The action, per-spec long/short thresholds, and SL/TP ATR multiples all
        come from the single best strategy in memory - the same object the
        walk-forward search scored. The learner, news, and timing layers are
        applied as VETO-ONLY gates: each may BLOCK the strategy's entry, but
        none can create an entry, flip its direction, or enlarge it. This is
        what makes "validated == traded" true on the live/paper path.

        Falls back to a flat, clearly-labelled Decision when no registry
        strategy exists for this symbol/timeframe (so the bot never silently
        trades an unvalidated signal in parity mode).
        """
        reasons: List[str] = []
        components: Dict[str, float] = {}

        ensemble = self._ensemble_for(symbol, timeframe)
        # Respect the decay monitor: skip any top strategy flagged suspect so
        # parity trades the best SURVIVING validated strategy.
        strat = None
        if ensemble:
            use_decay = self.decay_enabled and bool(self.decay_suspects)
            for cand in ensemble:
                if use_decay:
                    try:
                        if cand.spec.fingerprint() in self.decay_suspects:
                            continue
                    except Exception:
                        pass
                strat = cand
                break

        if strat is None:
            reasons.append("parity=no_registry_strategy")
            components["_threshold_long"] = 0.0
            components["_threshold_short"] = 0.0
            components["_blackout"] = 0.0
            return Decision(
                action=0, score=0.0, size_hint=0.0,
                sl_atr_mult=self.default_sl, tp_atr_mult=self.default_tp,
                reasons=reasons, components=components,
            )

        # The validated signal and its OWN thresholds decide the action.
        try:
            sig = float(strat.blended_signal(ohlcv))
        except Exception as exc:
            self.log.error("Parity signal error for %s: %s", symbol, exc)
            sig = 0.0
        lt = float(strat.spec.long_threshold)
        st = float(strat.spec.short_threshold)
        sl_mult = float(strat.spec.sl_atr_mult)
        tp_mult = float(strat.spec.tp_atr_mult)

        action = 0
        if sig >= lt:
            action = 1
        elif sig <= -st:
            action = -1

        components["strategy"] = sig
        components["_threshold_long"] = lt
        components["_threshold_short"] = st
        try:
            reasons.append("parity_strategy=%s" % strat.spec.fingerprint())
        except Exception:
            reasons.append("parity_strategy=?")
        reasons.append("strategy_signal=%.3f(lt=%.2f,st=%.2f)" % (sig, lt, st))

        # ---- VETO-ONLY gates (never create or resize a trade) -------------- #
        vetoed = False

        # News blackout veto.
        blackout = False
        if self.parity_veto_news and self.news is not None:
            try:
                blackout = bool(self.news.in_blackout(symbol))
            except Exception:
                blackout = False
            if blackout:
                reasons.append("veto_news_blackout=1")

        # Timing gate veto (only blocks; direction still from the strategy).
        time_sig = self._timing_signal(ohlcv, symbol, timeframe)
        if (self.parity_veto_timing and time_sig is not None
                and time_sig.enabled
                and (time_sig.blackout or time_sig.unfavorable)):
            vetoed = True
            reasons.append("veto_time_gate=1")

        # Learner-disagreement veto: only when the learner STRONGLY opposes the
        # strategy's direction (magnitude past the configured level).
        if self.parity_veto_learner and action != 0 and (
                self.learner is not None or self.learner_provider is not None):
            learn_sig = self._learning_signal(ohlcv, symbol)
            components["learning"] = learn_sig
            lvl = self.parity_learner_veto_level
            if lvl > 0.0:
                if action == 1 and learn_sig <= -lvl:
                    vetoed = True
                    reasons.append("veto_learner=%.3f<=-%.2f" % (learn_sig, lvl))
                elif action == -1 and learn_sig >= lvl:
                    vetoed = True
                    reasons.append("veto_learner=%.3f>=%.2f" % (learn_sig, lvl))

        # U6.1 meta-labeling veto: if a trained gate exists for THIS strategy and
        # predicts P(win) < min_win_prob, block the entry. Veto-only: it never
        # creates or resizes a trade, and stays silent when disabled/untrained.
        if action != 0 and self.meta_labeler is not None:
            try:
                veto_ml, p_win = self.meta_labeler.should_veto(
                    strat.spec, ohlcv, sig=sig)
                if p_win is not None:
                    components["meta_win_prob"] = float(p_win)
                    reasons.append("meta_win_prob=%.3f" % float(p_win))
                if veto_ml:
                    vetoed = True
                    reasons.append("veto_meta_label=1")
            except Exception as exc:
                self.log.error("Meta-label veto error for %s: %s", symbol, exc)

        if blackout or vetoed:
            components["_blackout"] = 1.0
            final_action = 0
        else:
            components["_blackout"] = 0.0
            final_action = action

        # Size hint: how far past its own threshold the validated signal is
        # (parity keeps sizing on the validated signal, then the timing size
        # multiplier may scale it DOWN in learned-weak windows).
        size_hint = 0.0
        if final_action == 1 and lt < 1.0:
            size_hint = (sig - lt) / (1.0 - lt)
        elif final_action == -1 and st < 1.0:
            size_hint = (-sig - st) / (1.0 - st)
        size_hint = max(0.0, min(1.0, size_hint))
        if (final_action != 0 and time_sig is not None and time_sig.enabled):
            size_hint = max(0.0, min(1.0, size_hint * time_sig.size_multiplier))
            reasons.append("time_size_mult=%.2f" % time_sig.size_multiplier)

        return Decision(
            action=final_action,
            score=sig,
            size_hint=size_hint,
            sl_atr_mult=sl_mult,
            tp_atr_mult=tp_mult,
            reasons=reasons,
            components=components,
        )

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

        UPGRADE_PLAN U2.4: in "parity" mode (the default) this dispatches to
        `_decide_parity`, which trades the validated top-1 strategy exactly and
        applies the learner/news/timing only as veto gates. The blend path below
        runs only in "blend" mode.
        """
        if self.mode == "parity":
            return self._decide_parity(ohlcv, symbol, timeframe)

        reasons: List[str] = []
        components: Dict[str, float] = {}

        ind_sig, ind_src, sl_mult, tp_mult = self._indicator_signal(
            ohlcv, symbol, timeframe
        )
        learn_sig = self._learning_signal(ohlcv, symbol)
        news_sig = self._news_signal(symbol)
        # Phase 5 (user-update-request): time/session/season awareness.
        time_sig = self._timing_signal(ohlcv, symbol, timeframe)

        # Build the weighted blend over contributing components only.
        parts = []
        if abs(self.w_ind) > 0:
            parts.append((self.w_ind, ind_sig, "indicators(%s)" % ind_src))
            components["indicators"] = ind_sig
        if abs(self.w_learn) > 0 and (self.learner is not None
                                      or self.learner_provider is not None):
            parts.append((self.w_learn, learn_sig, "learning"))
            components["learning"] = learn_sig
        if abs(self.w_news) > 0 and self.news is not None:
            parts.append((self.w_news, news_sig, "news"))
            components["news"] = news_sig
        # Timing contributes a DIRECTIONAL vote only when explicitly enabled;
        # otherwise it acts as a size/gate modifier applied further below.
        if (self.timing_directional and abs(self.w_timing) > 0
                and time_sig is not None and time_sig.enabled):
            parts.append((self.w_timing, float(time_sig.edge), "timing"))
            components["timing"] = float(time_sig.edge)

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

        # Timing gate: optionally block NEW entries in historically weak time
        # windows (only if the bot has LEARNED to distrust this window). This is
        # applied as a blackout so it never forces a trade, only prevents one.
        if (self.timing_gate and time_sig is not None and time_sig.enabled
                and (time_sig.blackout or time_sig.unfavorable)):
            blackout = True
            reasons.append("time_gate_blackout=1")
        if time_sig is not None and time_sig.enabled:
            reasons.extend(time_sig.reasons)

        # Optional agreement rule: indicators and learning must share direction.
        agree_ok = True
        if self.require_agreement and (self.learner is not None
                                       or self.learner_provider is not None):
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

        # Phase 5: scale the size hint by the learned time-window multiplier so
        # the bot risks less in historically weaker windows and up to normal in
        # favorable ones. Neutral/unknown time -> multiplier ~1.0 (no change).
        if action != 0 and time_sig is not None and time_sig.enabled:
            size_hint = max(0.0, min(1.0, size_hint * time_sig.size_multiplier))
            reasons.append("time_size_mult=%.2f" % time_sig.size_multiplier)

        # U1.4 transparency: record the thresholds the score was tested against
        # and the blackout flag so the decision journal / explainer can show
        # exactly WHY the score did or did not cross into an action. These are
        # stored under reserved keys so they never collide with a signal source.
        components["_threshold_long"] = self.long_threshold
        components["_threshold_short"] = self.short_threshold
        components["_blackout"] = 1.0 if blackout else 0.0

        return Decision(
            action=action,
            score=score,
            size_hint=size_hint,
            sl_atr_mult=sl_mult,
            tp_atr_mult=tp_mult,
            reasons=reasons,
            components=components,
        )
