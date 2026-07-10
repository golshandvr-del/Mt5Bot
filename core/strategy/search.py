"""
Strategy / parameter search (Phase 3) - the memory builder.

Generates many candidate StrategySpecs by sampling indicator parameters and
blend weights, evaluates each via walk-forward backtesting, and persists every
result to the MemoryStore. After the search, it updates the JSON registry with
the top strategies per symbol/timeframe.

Three methods (config.memory.search.method):
  - "random"    : sample max_trials random specs from the indicator param spaces.
  - "grid"      : enumerate a (bounded) grid over a small chosen indicator subset.
  - "evolution" : (U4.2) keep an elite pool of the best specs seen so far and
                  generate ~60% of new candidates by MUTATING/CROSSING elites
                  (jitter one param step, swap one indicator, +/-0.05 on the
                  thresholds) and ~40% fresh random for exploration. Pure Python,
                  CPU-light, and it dedups via fingerprints so no spec is ever
                  evaluated twice. Supersedes structure.md P6.5.

When config.memory.search.ea_compatible_only is true (U2.2) both methods draw
directional voters ONLY from the EA-supported set (ema, sma, rsi, macd, adx),
so any promoted strategy is exportable to the MQL5 EA 1:1 with no dropped
indicators. Default false keeps the full research indicator set.

This is the realistic "learn from trial-and-error on years of data" loop: the
more it searches and stores, the better future strategy selection becomes. It
never rewrites source code.

All text is standard ASCII English only.
"""

from __future__ import annotations

import itertools
import random
from typing import Any, Dict, List, Optional

from core.indicators.registry import get_indicator_class, list_indicators
from core.strategy.strategy import StrategySpec
from core.strategy.walk_forward import WalkForward
from core.utils.logger import get_logger


# Indicators that vote on direction (ATR is excluded: it is non-directional).
_DIRECTIONAL = [
    "sma", "ema", "macd", "adx", "ichimoku", "supertrend",
    "rsi", "stoch", "cci", "williams_r", "roc",
    "bbands", "keltner", "donchian", "obv", "mfi", "vwap",
    "candle_patterns",
]

# EA-compatible DIRECTIONAL subset (U2.2, widened by U2.3). Mirrors the
# exporter's EA_SUPPORTED_INDICATORS = (ema, sma, rsi, macd, atr, adx,
# supertrend, bbands, stoch); atr is dropped here because it is a
# non-directional exits-only indicator and never appears in _DIRECTIONAL.
# U2.3 grew the EA to also run supertrend, bbands and stoch (all directional
# voters), so they are now safe to draw when ea_compatible_only is true. When
# that flag is on the search draws voters ONLY from this list, so any promoted
# strategy exports to the MQL5 EA 1:1 with zero dropped indicators.
_EA_SUPPORTED_DIRECTIONAL = [
    "ema", "sma", "rsi", "macd", "adx", "supertrend", "bbands", "stoch",
]


class StrategySearch(object):
    def __init__(self, cfg: Any, memory: object,
                 time_stats: Optional[object] = None):
        self.cfg = cfg
        self.log = get_logger("strategy.search", cfg)
        self.memory = memory
        # Phase 5 (user-update-request): pass the TimeStats down so every
        # out-of-sample trade also teaches the time/session/season layer.
        self.time_stats = time_stats
        self.wf = WalkForward(cfg, memory, time_stats=time_stats)
        s = cfg.get_path("memory.search", {})
        self.method = s.get("method", "random") if hasattr(s, "get") else "random"
        self.max_trials = int(s.get("max_trials", 200)) if hasattr(s, "get") else 200
        self.rank_metric = s.get("rank_metric", "expectancy") if hasattr(s, "get") else "expectancy"
        self.min_trades = int(s.get("min_trades", 30)) if hasattr(s, "get") else 30
        # U2.2: restrict the search to the EA-exportable indicator set so that
        # anything promoted can be run in the MT5 EA 1:1 (no dropped voters).
        self.ea_compatible_only = bool(
            s.get("ea_compatible_only", False)) if hasattr(s, "get") else False
        if self.ea_compatible_only:
            self.log.info(
                "EA-compatible search ON: voters restricted to %s.",
                ", ".join(_EA_SUPPORTED_DIRECTIONAL),
            )
        # U4.2 evolutionary-search knobs. elite_fraction = top slice kept as
        # parents; mutate_fraction = share of each new generation bred from
        # elites (the rest is fresh random for exploration). Both have safe
        # defaults so "evolution" works even without extra config; the other
        # methods ignore them entirely.
        evo = s.get("evolution", {}) if hasattr(s, "get") else {}
        self.evo_elite_fraction = float(
            evo.get("elite_fraction", 0.10)) if hasattr(evo, "get") else 0.10
        self.evo_mutate_fraction = float(
            evo.get("mutate_fraction", 0.60)) if hasattr(evo, "get") else 0.60

        # U4.3 multi-seed stability gate. Only candidates that would enter the
        # registry pay the cost, so it stays cheap. Default OFF preserves old
        # behavior. When on, a finalist is re-run n_seeds extra times (each with
        # a different bootstrap seed + jittered warmup); promotion requires the
        # rank score to stay strictly positive in every run.
        stab = s.get("stability", {}) if hasattr(s, "get") else {}
        self.stability_enabled = bool(
            stab.get("enabled", False)) if hasattr(stab, "get") else False
        self.stability_n_seeds = int(
            stab.get("n_seeds", 3)) if hasattr(stab, "get") else 3
        self.stability_warmup_jitter = int(
            stab.get("warmup_jitter", 20)) if hasattr(stab, "get") else 20
        self.stability_require_all_positive = bool(
            stab.get("require_all_positive", True)) \
            if hasattr(stab, "get") else True
        if self.stability_enabled:
            self.log.info(
                "Stability gate ON: finalists re-run %d extra seed(s), "
                "warmup jitter +/-%d bars, require_all_positive=%s.",
                self.stability_n_seeds, self.stability_warmup_jitter,
                self.stability_require_all_positive,
            )

        # U4.4 parameter-neighborhood robustness gate. Only finalists pay the
        # cost. When on, each finalist is re-scored at up to n_neighbors
        # neighboring parameter sets (each nudges ONE key param one step); the
        # registry then ranks by min(own_score, median_neighbor_score) so an
        # overfit knife-edge peak is demoted below a broad robust plateau.
        # Default OFF preserves the rank-by-own-score behavior.
        nb = s.get("neighborhood", {}) if hasattr(s, "get") else {}
        self.neighborhood_enabled = bool(
            nb.get("enabled", False)) if hasattr(nb, "get") else False
        self.neighborhood_n = int(
            nb.get("n_neighbors", 8)) if hasattr(nb, "get") else 8
        if self.neighborhood_enabled:
            self.log.info(
                "Neighborhood gate ON: finalists re-scored at up to %d "
                "neighbor(s); registry ranks by min(own, median neighbor).",
                self.neighborhood_n,
            )

    # ------------------------------------------------------------------ #
    def _passes_stability_gate(self, spec: StrategySpec, ohlcv: Any,
                               point: Optional[float] = None) -> bool:
        """U4.3: re-run a would-be-promoted spec under different bootstrap seeds
        and jittered warmups; require its rank score to stay strictly positive in
        EVERY run. A strategy that flips negative when the warmup slides a few
        bars, or under a different resample seed, is a knife-edge fluke and must
        not be promoted. Returns True (pass) when the gate is disabled.

        Cheap by design: only specs that already cleared the base score / holdout
        reach here. All re-runs use persist=False so memory is never polluted.
        """
        if not self.stability_enabled or self.stability_n_seeds <= 0:
            return True
        base_warmup = 60
        # Deterministic per-spec jitter offsets so the gate is reproducible: seed
        # a local RNG from the spec fingerprint. Offsets straddle the base warmup.
        rng = random.Random(hash(spec.fingerprint()) & 0x7FFFFFFF)
        jitter = max(0, int(self.stability_warmup_jitter))
        for i in range(int(self.stability_n_seeds)):
            if jitter > 0:
                offset = rng.randint(-jitter, jitter)
            else:
                offset = 0
            warmup = max(20, base_warmup + offset)
            try:
                res = self.wf.evaluate(spec, ohlcv, point=point,
                                       persist=False, warmup=warmup)
            except Exception as exc:
                self.log.error("Stability re-run failed for %s: %s",
                               spec.fingerprint(), exc)
                return False
            score = float(res.get("avg_score", 0.0)) if res else 0.0
            if self.stability_require_all_positive and score <= 0.0:
                self.log.info(
                    "  [stability] %s REJECTED: re-run %d (warmup=%d) "
                    "score=%.4f <= 0.",
                    spec.fingerprint(), i + 1, warmup, score)
                return False
        return True

    # ------------------------------------------------------------------ #
    def _neighbor_specs(self, spec: StrategySpec) -> List[StrategySpec]:
        """U4.4: build up to ``neighborhood_n`` neighbors of a finalist, each
        differing from it by ONE key parameter nudged a single step in its own
        param_space (both the -1 and +1 directions are offered).

        Enumeration is DETERMINISTIC (sorted indicators, sorted param keys) so
        the neighborhood is reproducible for a given spec. A neighbor that would
        be identical to the parent (e.g. the param already sits at an edge and
        the step is clamped back) is skipped. Only the indicator parameters are
        perturbed here - thresholds/exits are the strategy's declared operating
        point, while the param values are what an overfit search latches onto.
        """
        neighbors: List[StrategySpec] = []
        seen_fps = {spec.fingerprint()}
        limit = max(0, int(self.neighborhood_n))
        if limit == 0:
            return neighbors
        for name in sorted(spec.indicators.keys()):
            try:
                cls = get_indicator_class(name)
            except Exception:
                continue
            space = cls.param_space()
            if not space:
                continue
            cur_params = spec.indicators[name]
            for key in sorted(space.keys()):
                choices = list(space[key])
                cur = cur_params.get(key)
                if cur not in choices or len(choices) < 2:
                    continue
                idx = choices.index(cur)
                for step in (-1, 1):
                    j = idx + step
                    if j < 0 or j >= len(choices):
                        continue
                    new_params = dict(cur_params)
                    new_params[key] = choices[j]
                    new_indicators = {
                        k: dict(v) for k, v in spec.indicators.items()
                    }
                    new_indicators[name] = new_params
                    child = StrategySpec(
                        indicators=new_indicators,
                        weights=dict(spec.weights),
                        long_threshold=spec.long_threshold,
                        short_threshold=spec.short_threshold,
                        sl_atr_mult=spec.sl_atr_mult,
                        tp_atr_mult=spec.tp_atr_mult,
                        symbol=spec.symbol, timeframe=spec.timeframe,
                    )
                    fp = child.fingerprint()
                    if fp in seen_fps:
                        continue
                    seen_fps.add(fp)
                    neighbors.append(child)
                    if len(neighbors) >= limit:
                        return neighbors
        return neighbors

    def _neighborhood_score(self, spec: StrategySpec, ohlcv: Any,
                            point: Optional[float] = None) -> Optional[float]:
        """U4.4: median walk-forward score across a finalist's parameter
        neighbors. Returns None when the gate is off or the spec has no
        perturbable neighbor (so callers fall back to the own score). All
        neighbor evaluations use persist=False so memory is never polluted.
        """
        if not self.neighborhood_enabled or self.neighborhood_n <= 0:
            return None
        neighbors = self._neighbor_specs(spec)
        if not neighbors:
            return None
        scores: List[float] = []
        for child in neighbors:
            try:
                res = self.wf.evaluate(child, ohlcv, point=point, persist=False)
            except Exception as exc:
                self.log.error("Neighbor eval failed for %s: %s",
                               child.fingerprint(), exc)
                continue
            scores.append(float(res.get("avg_score", 0.0)) if res else 0.0)
        if not scores:
            return None
        scores.sort()
        n = len(scores)
        mid = n // 2
        if n % 2 == 1:
            median = scores[mid]
        else:
            median = (scores[mid - 1] + scores[mid]) / 2.0
        return median

    # ------------------------------------------------------------------ #
    def _available_directional(self) -> List[str]:
        registered = set(list_indicators())
        pool = [n for n in _DIRECTIONAL if n in registered]
        if self.ea_compatible_only:
            pool = [n for n in pool if n in _EA_SUPPORTED_DIRECTIONAL]
        return pool

    def _random_params(self, indicator_name: str) -> Dict[str, Any]:
        """Sample one parameter set for an indicator from its param_space."""
        cls = get_indicator_class(indicator_name)
        space = cls.param_space()
        if not space:
            return dict(cls.default_params())
        return {k: random.choice(v) for k, v in space.items()}

    def _random_spec(self, symbol: str, timeframe: str) -> StrategySpec:
        """Build one random strategy spec."""
        pool = self._available_directional()
        # Choose between 2 and 5 indicators to combine.
        k = random.randint(2, min(5, len(pool)))
        chosen = random.sample(pool, k)
        indicators: Dict[str, Dict[str, Any]] = {}
        weights: Dict[str, float] = {}
        for name in chosen:
            indicators[name] = self._random_params(name)
            weights[name] = round(random.uniform(0.5, 2.0), 2)
        long_thr = round(random.uniform(0.15, 0.5), 2)
        short_thr = round(random.uniform(0.15, 0.5), 2)
        sl = round(random.uniform(1.0, 3.0), 1)
        tp = round(random.uniform(1.5, 5.0), 1)
        return StrategySpec(
            indicators=indicators, weights=weights,
            long_threshold=long_thr, short_threshold=short_thr,
            sl_atr_mult=sl, tp_atr_mult=tp,
            symbol=symbol, timeframe=timeframe,
        )

    # ------------------------------------------------------------------ #
    # U4.2 evolutionary operators. All pure Python, CPU-light, and every
    # produced spec is validated against the current indicator pool so a mutated
    # child can never reference an indicator the search is not allowed to use.
    # ------------------------------------------------------------------ #
    def _jitter_params(self, indicator_name: str,
                       params: Dict[str, Any]) -> Dict[str, Any]:
        """Nudge ONE parameter of an indicator by a single step in its space.

        For each param the class exposes an ordered list of legal values; we
        move to an adjacent index (one step up or down, clamped to the ends).
        If the current value is not in the space (or the space is empty) we
        fall back to a fresh random draw so the child stays legal.
        """
        cls = get_indicator_class(indicator_name)
        space = cls.param_space()
        new = dict(params)
        if not space:
            return new
        key = random.choice(list(space.keys()))
        choices = list(space[key])
        if not choices:
            return new
        cur = new.get(key)
        if cur in choices:
            idx = choices.index(cur)
            step = random.choice([-1, 1])
            idx = max(0, min(len(choices) - 1, idx + step))
            new[key] = choices[idx]
        else:
            new[key] = random.choice(choices)
        return new

    def _mutate(self, parent: StrategySpec, symbol: str,
                timeframe: str) -> StrategySpec:
        """Produce a child by applying ONE random mutation to a parent spec.

        Mutation menu (each equally likely):
          - jitter one param of one active indicator by a single step,
          - swap one active indicator for a different one from the pool,
          - nudge the long/short thresholds by +/-0.05,
          - nudge the SL or TP ATR multiple by +/-0.5.
        Weights are preserved for swapped-in indicators (given a neutral 1.0).
        """
        pool = self._available_directional()
        indicators = {k: dict(v) for k, v in parent.indicators.items()}
        weights = dict(parent.weights)
        long_thr = parent.long_threshold
        short_thr = parent.short_threshold
        sl = parent.sl_atr_mult
        tp = parent.tp_atr_mult

        choice = random.choice(["param", "swap", "thresh", "exits"])
        if choice == "param" and indicators:
            name = random.choice(list(indicators.keys()))
            indicators[name] = self._jitter_params(name, indicators[name])
        elif choice == "swap" and pool:
            # Drop one active indicator and add a different one from the pool.
            candidates = [n for n in pool if n not in indicators]
            if candidates and indicators:
                drop = random.choice(list(indicators.keys()))
                add = random.choice(candidates)
                indicators.pop(drop, None)
                weights.pop(drop, None)
                indicators[add] = self._random_params(add)
                weights[add] = 1.0
        elif choice == "thresh":
            long_thr = round(min(0.9, max(0.05, long_thr
                             + random.choice([-0.05, 0.05]))), 2)
            short_thr = round(min(0.9, max(0.05, short_thr
                              + random.choice([-0.05, 0.05]))), 2)
        else:  # exits
            sl = round(min(5.0, max(0.5, sl + random.choice([-0.5, 0.5]))), 1)
            tp = round(min(8.0, max(0.5, tp + random.choice([-0.5, 0.5]))), 1)

        # Guard: never emit an empty indicator set.
        if not indicators and pool:
            name = random.choice(pool)
            indicators[name] = self._random_params(name)
            weights[name] = 1.0

        return StrategySpec(
            indicators=indicators, weights=weights,
            long_threshold=long_thr, short_threshold=short_thr,
            sl_atr_mult=sl, tp_atr_mult=tp,
            symbol=symbol, timeframe=timeframe,
        )

    def _crossover(self, a: StrategySpec, b: StrategySpec, symbol: str,
                   timeframe: str) -> StrategySpec:
        """Breed a child by combining the indicator sets of two elite parents.

        We take the union of both parents' active indicators, then randomly keep
        each one (biased to keep, so children stay informative), inheriting the
        param set and weight from whichever parent supplied it. Thresholds and
        exits are averaged. The child is always non-empty and legal.
        """
        pool = set(self._available_directional())
        names = [n for n in (set(a.indicators) | set(b.indicators)) if n in pool]
        random.shuffle(names)
        indicators: Dict[str, Dict[str, Any]] = {}
        weights: Dict[str, float] = {}
        for name in names:
            if random.random() < 0.7:  # bias toward keeping a shared trait
                src = a if name in a.indicators else b
                indicators[name] = dict(src.indicators[name])
                weights[name] = float(src.weights.get(name, 1.0))
        if not indicators:  # fall back to one random parent trait
            src, other = (a, b) if a.indicators else (b, a)
            picks = list(src.indicators.keys()) or list(pool)
            name = random.choice(picks)
            base = src.indicators.get(name) or self._random_params(name)
            indicators[name] = dict(base)
            weights[name] = float(src.weights.get(name, 1.0))

        long_thr = round((a.long_threshold + b.long_threshold) / 2.0, 2)
        short_thr = round((a.short_threshold + b.short_threshold) / 2.0, 2)
        sl = round((a.sl_atr_mult + b.sl_atr_mult) / 2.0, 1)
        tp = round((a.tp_atr_mult + b.tp_atr_mult) / 2.0, 1)
        return StrategySpec(
            indicators=indicators, weights=weights,
            long_threshold=long_thr, short_threshold=short_thr,
            sl_atr_mult=sl, tp_atr_mult=tp,
            symbol=symbol, timeframe=timeframe,
        )

    def _breed_from_elites(self, elites: List[StrategySpec], symbol: str,
                           timeframe: str) -> StrategySpec:
        """Make one child from the elite pool: mutate a single elite, or cross
        two of them. Falls back to a fresh random spec if the pool is empty."""
        if not elites:
            return self._random_spec(symbol, timeframe)
        if len(elites) >= 2 and random.random() < 0.5:
            a, b = random.sample(elites, 2)
            return self._crossover(a, b, symbol, timeframe)
        return self._mutate(random.choice(elites), symbol, timeframe)

    # ------------------------------------------------------------------ #
    def run(self, ohlcv: Any, symbol: str, timeframe: str,
            point: Optional[float] = None) -> Dict[str, Any]:
        """
        Run the configured search over the OHLCV history, persisting every
        result. Returns a summary dict including the updated registry section.
        """
        self.log.info(
            "Starting %s search: up to %d trials on %s %s (%d bars).",
            self.method, self.max_trials, symbol, timeframe, len(ohlcv.close),
        )
        seen = set()
        evaluated = 0
        # Locked holdout gate (A2 / P1.4): when memory.walk_forward.holdout_bars
        # > 0, only specs that ALSO pass on the untouched holdout tail may enter
        # the registry. We collect the passing fingerprints here and pass them
        # as an allowlist to update_registry. When the holdout is OFF the gate is
        # a no-op and allowed stays None (no filtering), keeping old behavior.
        holdout_on = int(getattr(self.wf, "holdout_bars", 0)) > 0
        # An active promotion allowlist is needed when EITHER the holdout gate
        # (A2/P1.4) OR the U4.3 stability gate is on. The U4.4 neighborhood gate
        # does not filter (it re-ranks), so it does not force an allowlist. When
        # all filters are off, allowed_fps stays None so promotion is unfiltered
        # (legacy behavior).
        regime_on = bool(getattr(self.wf, "regime_enabled", False))
        gating_on = holdout_on or self.stability_enabled or regime_on
        allowed_fps = set() if gating_on else None
        # U4.4: per-fingerprint ranking-score override = min(own_score, median
        # neighbor score). Populated only for finalists when the gate is on;
        # forwarded to update_registry so the registry ranks by robustness.
        score_overrides: Dict[str, float] = {}

        def _eval_one(spec: StrategySpec):
            """Evaluate + persist one spec once; return its avg_score (or None
            on dedup/error). Updates evaluated / seen / allowed_fps in-place via
            closure. A spec is added to allowed_fps only if it clears BOTH the
            holdout gate (when on) AND the stability gate (when on). When the
            U4.4 neighborhood gate is on, a passing finalist also gets a
            robustness-adjusted ranking override recorded in score_overrides."""
            fp = spec.fingerprint()
            if fp in seen:
                return None
            seen.add(fp)
            try:
                res = self.wf.evaluate(spec, ohlcv, point=point, persist=True)
                score = float(res.get("avg_score", 0.0)) if res else 0.0
                if gating_on:
                    ok = True
                    if holdout_on:
                        gate = self.wf.evaluate_holdout(spec, ohlcv, point=point)
                        ok = bool(gate.get("passed"))
                    # U4.5 regime floor: reject a spec that collapses in any
                    # single regime (e.g. loses badly in high-vol range) even if
                    # its overall score is fine. The verdict was computed for
                    # free by the base evaluate() run above (res). A spec with no
                    # gated regimes (too few segments) passes by construction.
                    if ok and regime_on:
                        ok = bool(res.get("passes_regime_floor", True))
                    # Only pay for the stability re-runs if the spec is still a
                    # live finalist (passed holdout+regime, if any) AND scored
                    # > 0 in the base run; a base-negative spec can never be
                    # promoted.
                    if ok and score > 0.0 and self.stability_enabled:
                        ok = self._passes_stability_gate(spec, ohlcv, point=point)
                    if ok:
                        allowed_fps.add(fp)
                # U4.4 neighborhood robustness re-ranking. Compute only for a
                # still-eligible, base-positive finalist so the cost stays low.
                # A spec eligible for promotion is: gated -> in allowed_fps;
                # ungated -> any base-positive spec (they all compete on rank).
                if self.neighborhood_enabled and score > 0.0:
                    eligible = (fp in allowed_fps) if gating_on else True
                    if eligible:
                        nb = self._neighborhood_score(spec, ohlcv, point=point)
                        if nb is not None:
                            score_overrides[fp] = min(score, nb)
                return score
            except Exception as exc:
                self.log.error("Evaluation failed for %s: %s", fp, exc)
                return None

        if self.method == "evolution":
            evaluated = self._run_evolution(
                symbol, timeframe, _eval_one)
        else:
            if self.method == "grid":
                specs = self._grid_specs(symbol, timeframe)
            else:
                specs = (self._random_spec(symbol, timeframe)
                         for _ in range(self.max_trials))
            for spec in specs:
                if evaluated >= self.max_trials:
                    break
                score = _eval_one(spec)
                if score is None:
                    continue
                evaluated += 1
                if evaluated % 25 == 0:
                    self.log.info("  evaluated %d strategies...", evaluated)

        if gating_on:
            gates = []
            if holdout_on:
                gates.append("holdout")
            if regime_on:
                gates.append("regime")
            if self.stability_enabled:
                gates.append("stability")
            self.log.info(
                "Promotion gate (%s): %d of %d evaluated specs are eligible.",
                "+".join(gates), len(allowed_fps), evaluated,
            )
        if self.neighborhood_enabled:
            self.log.info(
                "Neighborhood re-ranking: robustness-adjusted score computed "
                "for %d finalist(s).", len(score_overrides),
            )
        # U4.4: pass the min(own, neighbor) overrides so the registry ranks by
        # robustness. When the gate is off, score_overrides is empty and the
        # override is a no-op (ranking is byte-identical to before).
        section = self.memory.update_registry(
            symbol, timeframe, rank_metric=self.rank_metric,
            min_trades=self.min_trades, allowed_fingerprints=allowed_fps,
            score_overrides=(score_overrides or None),
        )
        self.log.info(
            "Search complete: %d strategies evaluated; %d in registry top.",
            evaluated, len(section.get("top", [])),
        )
        return {"evaluated": evaluated, "registry": section}

    # ------------------------------------------------------------------ #
    def _run_evolution(self, symbol: str, timeframe: str, eval_one) -> int:
        """Evolutionary search loop (U4.2).

        Seeds generation 0 with fresh random specs, evaluates them, then breeds
        each subsequent generation from the top ``elite_fraction`` seen so far:
        ``mutate_fraction`` of children come from mutating/crossing elites and
        the rest are fresh random for exploration. Every child is deduped by
        fingerprint (handled inside eval_one), so the elite pool steadily
        concentrates on what actually scores well without wasting CPU on repeats.

        ``eval_one(spec) -> score|None`` evaluates + persists one spec and
        returns its avg_score (or None if it was a duplicate / errored).
        Returns the number of specs actually evaluated.
        """
        elite_frac = min(0.9, max(0.01, self.evo_elite_fraction))
        mutate_frac = min(1.0, max(0.0, self.evo_mutate_fraction))
        # Generation size: a modest batch keeps memory tiny and lets the elite
        # pool refresh often. Scale gently with the budget.
        gen_size = max(10, min(50, self.max_trials // 8 or 10))
        scored: List[tuple] = []  # (score, spec) for every successfully scored
        evaluated = 0
        # Guard against an infinite loop if almost everything dedups: cap the
        # number of generation attempts relative to the trial budget.
        attempts = 0
        max_attempts = self.max_trials * 4 + gen_size * 4

        while evaluated < self.max_trials and attempts < max_attempts:
            # Build this generation's candidate specs.
            elites = self._elite_specs(scored, elite_frac)
            batch: List[StrategySpec] = []
            for _ in range(gen_size):
                if evaluated + len(batch) >= self.max_trials:
                    break
                if elites and random.random() < mutate_frac:
                    batch.append(self._breed_from_elites(elites, symbol, timeframe))
                else:
                    batch.append(self._random_spec(symbol, timeframe))
            if not batch:
                break
            for spec in batch:
                attempts += 1
                score = eval_one(spec)
                if score is None:
                    continue
                scored.append((score, spec))
                evaluated += 1
                if evaluated % 25 == 0:
                    self.log.info(
                        "  [evolution] evaluated %d strategies (pool=%d)...",
                        evaluated, len(scored))
                if evaluated >= self.max_trials:
                    break
        self.log.info(
            "[evolution] done: %d evaluated, best avg_score=%.4f.",
            evaluated,
            max((s for s, _ in scored), default=0.0),
        )
        return evaluated

    def _elite_specs(self, scored: List[tuple], elite_frac: float
                     ) -> List[StrategySpec]:
        """Return the top ``elite_frac`` specs (by avg_score) seen so far."""
        if not scored:
            return []
        ordered = sorted(scored, key=lambda t: t[0], reverse=True)
        k = max(1, int(len(ordered) * elite_frac))
        return [spec for _, spec in ordered[:k]]

    # ------------------------------------------------------------------ #
    def _grid_specs(self, symbol: str, timeframe: str):
        """
        Enumerate a bounded grid over a small, fixed indicator combo
        (ema + rsi + atr-based exits). Kept small to remain CPU-friendly.
        """
        ema_periods = [12, 21, 34]
        rsi_periods = [7, 14, 21]
        long_thrs = [0.2, 0.3, 0.4]
        sls = [1.5, 2.0]
        tps = [2.0, 3.0]
        for ep, rp, lt, sl, tp in itertools.product(
            ema_periods, rsi_periods, long_thrs, sls, tps
        ):
            indicators = {
                "ema": {"period": ep},
                "rsi": {"period": rp},
            }
            weights = {"ema": 1.0, "rsi": 1.0}
            yield StrategySpec(
                indicators=indicators, weights=weights,
                long_threshold=lt, short_threshold=lt,
                sl_atr_mult=sl, tp_atr_mult=tp,
                symbol=symbol, timeframe=timeframe,
            )
