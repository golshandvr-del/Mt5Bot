# UPGRADE_PLAN.md - Quality, Transparency and Parity Overhaul

> STATUS: authoritative upgrade plan, written 2026-07-08 after a full external
> code review of the repository AND the user's real-world test report
> (150k XAUUSD M15 bars, search finished in ~6h, MT5 Strategy Tester turned
> 10,000 into ~3-4k in one year = a NET LOSS).
>
> This file complements `structure.md` (the older roadmap). Where the two
> disagree, THIS file wins for the topics it covers. Same repo rules apply:
> ASCII English only, every change optional/config-driven, Windows 7 +
> Python 3.8 + CPU-only compatible, keep the offline test suite green.

Legend: [ ] planned   [~] in progress   [x] done   [-] rejected/deferred

---

## 0. Executive diagnosis - WHY the Strategy Tester result was bad

The review found that the pipeline is architecturally solid but has FOUR
disconnects between "what was validated" and "what actually trades". Each one
alone can destroy an account; together they fully explain the observed loss.

### D1. The EA trades a DIFFERENT strategy than the one that was validated (CRITICAL)

`scripts/export_strategy_for_ea.py` only supports 6 indicators
(`ema, sma, rsi, macd, atr, adx`). The Python search freely combines 18
directional indicators (supertrend, bbands, stoch, candle_patterns, donchian,
keltner, obv, mfi, vwap, ichimoku, ...). Any unsupported indicator in the
winning spec is SILENTLY SKIPPED (only a console note) while the blend
thresholds (e.g. long_threshold=0.27) stay unchanged.

Evidence in this repo: `experts/params/XAUUSD_M15.params` contains ONLY
`ema` - meaning the promoted XAUUSD strategy was validated as a multi-indicator
blend but the EA traded a naked single-EMA signal against thresholds tuned for
the full blend. That is not the validated strategy at all; a bad year is the
expected outcome, not bad luck.

### D2. Live/paper decision path does NOT trade the validated strategy either

The search validates each StrategySpec STANDALONE: its own indicators, its own
thresholds (sampled 0.15-0.50). But the live `DecisionEngine`:
  1. averages the top-K(3) strategies' continuous signals,
  2. re-blends that with the ML learner (0.3) and news (0.2),
  3. applies a GLOBAL threshold of 0.60.
None of that composite was ever backtested. The ensemble average of three
different strategies rarely reaches +/-0.60, and when it does it fires at
moments none of the three validated strategies would have fired alone.
Walk-forward numbers therefore say NOTHING about what paper/live actually does.

### D3. The backtest is silently optimistic about execution

- Entries fill at the SIGNAL bar's close. A real EA can only act on the NEXT
  bar (the EA acts on new-bar open). On M15 gold this is a systematic
  favorable slip of up to one full bar.
- When one bar touches BOTH the stop and the take, the simulator checks SL
  first for longs but TP-vs-SL ambiguity is not modeled pessimistically in all
  cases (no intrabar path model).
- Backtest sizes with `fixed_lot` while live sizes by risk % of equity, so the
  validated equity curve has a different geometry than the live one.
- Spread is constant; real XAUUSD spread widens 3-10x at rollover/news.

### D4. Search quality was budgeted for speed, not for quality

400 random trials, n_boot=1000, holdout OFF (`holdout_bars: 0`), no
multi-seed stability check, no parameter-neighborhood robustness test, no
regime segmentation. On 150k bars a 6h run means the machine could afford
10-20x more exploration within the user's stated 24h budget. Random search
also wastes most trials far from good regions (no evolution).

### D5. Zero post-mortem transparency (the user's own top complaint)

The bot records only aggregate metrics. There is no per-trade log, no equity
curve file, no "why did I enter here" explanation, no comparison report
between internal backtest and MT5 tester. When the tester lost money the user
had NO artifact to inspect. Trust requires receipts.

---

## 1. Design principles for this upgrade

1. **Parity first.** Whatever is validated must be EXACTLY what trades - in
   the internal backtester, in paper/live, and in the MT5 EA. Any conversion
   step must be verified bar-by-bar or refuse to export.
2. **Pessimistic by default.** Every ambiguity in simulation resolves AGAINST
   the strategy (next-bar entry, SL-before-TP, gap fills, widened spread).
   A strategy that survives pessimism deserves live money; one that needs
   optimism does not.
3. **Receipts everywhere.** Every mode writes human-readable artifacts:
   per-trade CSV, equity curve, decision explanations, HTML report. The user
   must be able to answer "why did it enter HERE?" for any trade in <1 min.
4. **Spend the full compute budget.** The user accepts 24h runs. Effort knobs
   scale up; quality gates scale up with them.
5. **Fail loudly.** Silent skips (D1) are bugs. Anything dropped/degraded must
   WARN in the log AND in the artifact, or abort.

---

## Phase U1 - Transparency: receipts for every trade (do FIRST)

Goal: the user can see and audit every single simulated or live decision.
No behavior change to trading logic yet - only visibility.

- [x] U1.1 (code) `Backtester.run(record_trades=True)` becomes the default in
      backtest mode and records FULL trade records: entry_ts, exit_ts,
      direction, entry/exit price, SL, TP, exit_reason (sl/tp/flip/eod),
      pnl, costs paid (spread/commission/slippage/swap separately),
      balance_after, and the blended signal value at entry.
- [x] U1.2 (code) New `core/utils/trade_log.py`: writes
      `backtests/trades_<SYMBOL>_<TF>_<timestamp>.csv` plus
      `equity_<...>.csv` (bar-indexed equity curve). Pure stdlib csv.
- [x] U1.3 (code) New `scripts/make_report.py`: renders a single-file HTML
      report (no external deps, inline SVG for the equity/drawdown chart)
      from the trade CSV: summary table, per-month PnL, top-10 worst trades,
      exit-reason breakdown, cost share of PnL. Runs on Win7 offline.
- [x] U1.4 (code) Decision explainer: `Decision.reasons` is extended to carry
      per-component values (each strategy's signal, learner prob, news score,
      threshold used) and paper/live append one JSON line per decision to
      `logs/decisions_<date>.jsonl`. A new `scripts/explain_decisions.py`
      pretty-prints the last N decisions with WHY (which components pushed it
      over/under the threshold).
- [x] U1.5 (code) Backtest report gains a `config_snapshot` section (the
      exact effective config values used) so every artifact is reproducible.
- [x] U1.6 (test) Offline tests: trade CSV row count == num_trades metric;
      costs in the CSV sum to the metrics' implied total cost; HTML report
      builds from a synthetic run without exceptions.
- [x] U1.7 (docs) README section "Auditing a run"; sync CODE_MAP/Ideas.

Acceptance: after any backtest the user opens ONE html file and can point at
any trade and see when/why/what-it-cost. Nothing about trading changed yet.

---

## Phase U2 - Parity: validated == traded (fixes D1 + D2)

Goal: kill both parity gaps. This is the phase that directly addresses the
"robot behaved stupidly in the Strategy Tester" incident.

- [x] U2.1 (code) EA export HARD GUARD: `export_strategy_for_ea.py` gains
      `--strict` (DEFAULT ON): if the spec contains ANY indicator the EA does
      not implement, the export FAILS with a clear message listing them,
      instead of silently exporting a crippled strategy. `--allow-partial`
      keeps the old behavior for experiments, and then it also rescales the
      remaining weights AND writes a prominent warning block into the .params
      file header.
- [x] U2.2 (code) EA-compatible search mode: new config
      `memory.search.ea_compatible_only` (default false). When true the
      random/grid/evolutionary spec generators draw ONLY from the EA-supported
      indicator set, so anything promoted is exportable 1:1. RECOMMENDED
      workflow for anyone who validates in the MT5 tester.
- [x] U2.3 (code) Alternatively grow the EA: implement supertrend, bbands,
      stoch and candle_patterns(subset) in MQL5 to widen the exportable set.
      Each new EA indicator ships with a cross-check fixture (see U2.6).
- [x] U2.4 (code+config) Live parity mode: new config
      `decision.mode: "parity" | "blend"` (default "parity").
      In parity mode the engine trades the TOP-1 registry strategy EXACTLY as
      validated: same indicators, same per-spec thresholds, same SL/TP mults.
      The learner/news/timing become optional VETO-ONLY gates (they can block
      an entry, never create or resize one). "blend" keeps the current
      behavior for research.
- [x] U2.5 (code) If ensemble blending stays in use ("blend" mode), the
      ensemble composite itself MUST be walk-forward evaluated: add
      `scripts/validate_ensemble.py` that replays the exact engine blend
      (ensemble avg + weights + 0.60 threshold) through the backtester and
      writes the same artifacts as U1. No unvalidated composite may go live.
- [x] U2.6 (test) Python-vs-EA signal parity harness: `scripts/export_history`
      fixture + a small MQL5 script dump per-bar BlendedSignal values for the
      same params; a Python test asserts max abs diff < 1e-6 on the shared
      indicator set. Catches sign/edge-case drift (RSI/MACD bugs of the past).
- [x] U2.7 (docs) README: "The golden rule - only trade what was validated";
      document parity mode and the strict exporter; sync CODE_MAP/Ideas.

Acceptance: it is IMPOSSIBLE to (a) export a partial strategy without an
explicit override, and (b) run paper/live logic that was never backtested.

---

## Phase U3 - Pessimistic, realistic simulation (fixes D3)

Goal: internal numbers move much closer to MT5-tester numbers, always erring
against the strategy.

- [x] U3.1 (code+config) Next-bar execution: `backtest.fill_policy:
      "next_open"` (new DEFAULT) fills entries at the NEXT bar's open plus
      half-spread and slippage; "signal_close" keeps the old behavior for
      comparison. Exits on signal-flip also fill at next open; SL/TP still
      fill intrabar.
- [x] U3.2 (code+config) Intrabar ambiguity: `backtest.intrabar_policy:
      "pessimistic"` (new DEFAULT) - when one bar touches both SL and TP,
      count the STOP as hit first for both directions. "optimistic" and
      "midpoint" available for sensitivity analysis.
- [x] U3.3 (code+config) Session-aware spread: `backtest.spread_model:
      {base_points: 25, rollover_mult: 4.0, rollover_hours_utc: [21,23],
      news_mult: 1.0}` - spread widens during the configured rollover window
      (and optionally around news blackout windows already known to Phase 4).
      Default reproduces a constant spread when mults are 1.0.
- [x] U3.4 (code+config) Risk-based sizing in backtest: `backtest.sizing:
      "fixed_lot" | "risk_pct"` (default "risk_pct" to MATCH live). Uses the
      same formula as `RiskManager.position_size` incl. min/max lot clamping,
      so backtest and live equity curves share geometry. Includes the
      max_daily_loss circuit breaker in simulation.
- [x] U3.5 (code) Margin/stop-distance sanity: reject simulated entries whose
      SL distance is < broker min stop distance (configurable points) - the
      MT5 tester rejects those orders and the internal sim must too.
- [x] U3.6 (test) Fixtures proving: next_open fills are never better than
      signal_close fills; pessimistic intrabar never beats optimistic;
      risk_pct sizing respects min/max lot and the daily circuit breaker.
- [x] U3.7 (docs) README "Simulation realism" section + config reference;
      sync CODE_MAP/Ideas. Re-baseline: re-run search+backtest and archive the
      before/after metric deltas in `backtests/realism_baseline.md`.

Acceptance: on the same spec, internal-backtest net profit is within a
documented tolerance of (and never wildly above) the MT5 Strategy Tester.

---

## Phase U4 - Spend the budget: deep, smart search (fixes D4)

Goal: a 12-24h search that finds ROBUST strategies, not fast lucky ones.
Everything scales via config; the 6h profile remains available.

- [x] U4.1 (config) New "deep" profile documented in config.yaml comments:
      max_trials 400 -> 4000+, n_boot 1000 -> 5000, holdout_bars 0 -> 15000
      (last ~6 months of M15 locked), min_segments 10 -> 12. Add
      `memory.search.time_budget_hours: 0` (0=off) - stop cleanly and rank
      whatever was evaluated when the budget expires.
- [x] U4.2 (code) Evolutionary search (supersedes structure.md P6.5): keep an
      elite pool (top ~10%); generate 60% of new specs by mutating/crossing
      elites (jitter one param step, swap one indicator, +/-0.05 thresholds)
      and 40% fresh random for exploration. Dedup via fingerprints. Pure
      Python, CPU-light.
- [x] U4.3 (code) Multi-seed stability gate: every candidate that would enter
      the registry is re-run with 3 different bootstrap seeds and
      jittered warmup; promotion requires the rank score to stay positive in
      ALL runs. Kills knife-edge flukes cheaply (only finalists pay the cost).
- [x] U4.4 (code) Parameter-neighborhood robustness: for each finalist,
      evaluate 8 neighbors (each key param nudged one step). New metric
      `neighborhood_score` = median neighbor score; registry ranks by
      min(own_score, neighborhood_score). A strategy that dies when RSI period
      moves 14->15 is overfit BY DEFINITION and must not be promoted.
- [x] U4.5 (code) Regime-sliced validation: label each walk-forward segment
      by realized volatility tercile (low/mid/high ATR%) and trend strength
      (ADX median). Registry entries store per-regime scores; promotion
      requires not losing catastrophically in any regime (configurable floor,
      e.g. per-regime expectancy > -0.5 * overall expectancy).
- [x] U4.6 (code) Search checkpointing: persist elite pool + trial count every
      N trials so a 24h run survives a reboot and can resume with
      `--resume`. (SQLite already persists results; this adds the search
      state itself.)
- [x] U4.7 (test) Evolution respects param spaces; time budget stops cleanly;
      resume continues without re-evaluating fingerprints; neighborhood and
      regime gates provably filter a planted overfit fixture.
- [ ] U4.8 (docs) README "Deep search profile - what 24 hours buys you";
      sync CODE_MAP/Ideas/structure.md (mark P6.5/P6.7 superseded here).

Acceptance: a 24h deep search on 150k bars evaluates >= 4000 specs, and every
registry entry passed: significance + holdout + multi-seed + neighborhood +
regime floors. Fewer, but trustworthy, strategies.

---

## Phase U5 - Final validation gauntlet before any live money

Goal: a standard, scripted "gauntlet" that any candidate must pass, producing
one verdict file the user can read.

- [ ] U5.1 (code) `scripts/gauntlet.py --symbol XAUUSD --tf M15`: runs the
      full sequence on the CURRENT registry top-1: (1) full-history pessimistic
      backtest, (2) locked holdout, (3) Monte-Carlo bootstrap of trade order
      (1000 shuffles -> 5%/95% equity envelopes, max-DD distribution,
      risk-of-ruin estimate at the configured risk%), (4) cost stress
      (spread x1.5 and x2 - edge must survive x1.5), (5) worst-case start
      (equity from the worst rolling 3-month window).
- [ ] U5.2 (code) Verdict artifact `backtests/gauntlet_<fingerprint>.md`
      with PASS/FAIL per gate and the reasoning, plus the U1 HTML links.
      A FAIL anywhere prints exactly which gate and why.
- [ ] U5.3 (code+config) `general.live_requires_gauntlet: true` (default) -
      live mode refuses to start unless a PASS verdict exists for the
      registry top strategy fingerprint, and it is newer than the last search.
      Paper mode always allowed.
- [ ] U5.4 (test) Gauntlet gates fire on planted fixtures (a cost-fragile
      strategy fails gate 4; a lucky-order strategy fails gate 3).
- [ ] U5.5 (docs) README "The gauntlet - your pre-flight checklist";
      sync CODE_MAP/Ideas.

Acceptance: going live is mechanically impossible without a written,
reproducible PASS verdict. The user always knows WHY a strategy was allowed.

---

## Phase U6 - Non-linear upgrades (after U1-U5 are green)

Ideas that change the bot's nature from "strategy picker" to "adaptive
portfolio manager". Each is optional and config-gated.

- [ ] U6.1 Meta-labeling filter: train the light ML model NOT to predict
      direction, but to predict "will the top strategy's NEXT signal win?"
      using regime features (ATR%, ADX, session, day). The model becomes a
      quality gate on top of a validated strategy - this composes cleanly with
      parity mode (veto-only) and is the single highest-leverage ML use here.
- [ ] U6.2 Regime router: promote per-regime champions (from U4.5) and let a
      tiny detector (ATR%/ADX terciles, pure Python) route each bar to the
      champion of the CURRENT regime instead of averaging strategies that
      disagree. Router itself must pass U2.5 composite validation.
- [ ] U6.3 Anti-portfolio diversification: when blending top-K, penalize
      pairwise signal correlation (measured on walk-forward decisions) so the
      ensemble contains genuinely different edges, not 3 clones of one trend
      follower.
- [ ] U6.4 Trade-throttle learning: learn from the decision journal (U1.4)
      which VETOES would have saved money (news blackout, spread spike,
      regime mismatch) and auto-tighten only those gates - never invert
      signals.
- [ ] U6.5 Continuous shadow validation on the VPS: the loop re-scores the
      live strategy on the trailing 3 months every weekend; if its live-window
      score drops below the decay threshold, auto-demote to paper and email/log
      a plain-language explanation (extends the existing decay_monitor from a
      blend-weight tweak to a hard safety demotion).
- [ ] U6.6 Chaos-monkey harness: a test mode that injects broker nastiness
      into the sim (requotes, partial fills, missed bars, spread storms) and
      reports which registry strategies degrade gracefully vs shatter.

---

## 7. Execution order and effort estimate

| Order | Phase | Effort (sessions) | Depends on | Risk if skipped |
|-------|-------|-------------------|-----------|-----------------|
| 1 | U1 Transparency | 2-3 | - | keep flying blind |
| 2 | U2 Parity | 3-4 | U1 artifacts | repeat of the tester loss |
| 3 | U3 Realism | 2-3 | U1 | optimistic numbers again |
| 4 | U4 Deep search | 3-4 | U3 (search must use realistic sim) | fast lucky picks |
| 5 | U5 Gauntlet | 2 | U1+U3+U4 | live without proof |
| 6 | U6 Non-linear | open-ended | U1-U5 | (optional upside) |

Rules of engagement (same as structure.md section 6): every step keeps the
offline test suite green, stays Win7/Py3.8/CPU-only, degrades gracefully, and
updates the four docs (README, CODE_MAP, structure.md/this file, Ideas.md).

---

## 8. Change log (append newest at top)

- 2026-07-10 U4.7 DONE - Deep-search guarantee tests. New
  tests/test_search_evolution_resume.py (17 tests) proves the four required
  properties: (1) EVOLUTION RESPECTS PARAM SPACES - _random_spec / _mutate /
  _crossover / _breed_from_elites / _neighbor_specs are each hammered 200x and
  asserted to emit only pool-legal indicators and only in-param_space values
  (with ea_compatible_only the pool is a strict subset of the full research
  set); (2) TIME BUDGET STOPS CLEANLY - _budget_expired unit-checked (budget<=0
  never expires), and both the random and evolution run() paths, given a
  100000-trial budget but a 0.5s wall-clock budget over a delayed WF stub, halt
  after >=1 and <100000 trials and still return a ranked registry section;
  (3) RESUME NO RE-EVAL - checkpoint save/load round-trips seen+evaluated+elite
  pool, a wrong-run or corrupt checkpoint is ignored (start fresh), a resumed
  run provably never re-scores a pre-seeded fingerprint and continues the
  cumulative trial count from the restored offset, and a run that reaches
  max_trials clears its checkpoint; (4) GATES FILTER OVERFIT - a planted
  knife-edge spec (high own score, ~0 neighbors) gets a neighborhood median far
  below own so min(own,nb) demotes it, and a spec collapsing in one regime is
  refused by passes_regime_floor while the regime gate forces a real promotion
  allowlist in a live search run. WalkForward is stubbed (_ScriptedWF) where a
  real run would be slow/non-deterministic; all stdlib, Win7+Py3.8 friendly.
  NEXT: U4.8 (README "Deep search profile - what 24 hours buys you" + sync
  CODE_MAP/Ideas/structure.md).

- 2026-07-10 U4.6 DONE - Search checkpointing. `core/strategy/search_checkpoint.py`
  (`SearchCheckpoint`) persists search state to an atomic JSON file (temp-file +
  os.replace) alongside the SQLite results DB: the set of already-seen candidate
  fingerprints, the running trial counter, and the current elite pool. Config
  block `memory.search` gained `time_budget_hours`, `checkpoint_every` (default
  25 trials), and `checkpoint_max_scored` (cap on persisted scored-log size).
  `StrategySearch.run(..., resume=False)` and `_run_evolution` share one
  scored_log/counter; on `--resume` they load the checkpoint, seed the elite pool,
  and skip any fingerprint already evaluated. `_budget_expired(start_time)` stops
  the run cleanly once `time_budget_hours*3600` elapses (budget<=0 disables it),
  saving a final checkpoint; the checkpoint is cleared on natural completion. The
  `--resume` flag is threaded main.py -> run_search -> search.run. So a 24h deep
  run now survives a reboot and continues where it stopped without re-evaluating
  work. NEXT: U4.7 (tests for param-space respect, clean budget stop, resume
  no-re-eval, neighborhood + regime gate filtering of planted overfit).

- 2026-07-09 U4.5 DONE - Regime-sliced validation. New config block
  `memory.search.regime` (enabled / floor_mult / min_segments_per_regime /
  adx_trend_threshold, default OFF). When on, `WalkForward.evaluate` labels each
  walk-forward test segment by its realized-volatility tercile (low/mid/high,
  cut at the RUN'S OWN 33rd/66th percentiles so labels are relative to this
  instrument) combined with trend strength (median ADX >= adx_trend_threshold ->
  "trend", else "range"), giving labels like "high_range" / "low_trend".
  `_regime_scores` averages the per-segment rank scores within each regime that
  has >= min_segments_per_regime contributing segments, and
  `passes_regime_floor` fails any strategy whose worst gated-regime score drops
  below `floor_mult * overall_score` (default -0.5 * overall = "may not lose more
  than half the overall edge in any single regime"). `StrategySearch.run` folds
  this into the promotion allowlist alongside the holdout (A2) and stability
  (U4.3) gates: a regime-fragile spec is RECORDED in memory but never promoted.
  Everything is computed for free from the base evaluate() run, and with the gate
  OFF evaluate() attaches no regime fields and the search path is byte-identical
  to before. Added tests/test_regime_validation.py (6 tests incl. a collapsing-
  regime rejection and the search-gating integration). Suite -> 148 green. NEXT:
  U4.6 (search checkpointing / --resume).
- 2026-07-09 U4.4 DONE - Parameter-neighborhood robustness gate. New config
  block `memory.search.neighborhood` (enabled/n_neighbors, default OFF). When
  on, every base-positive finalist is re-scored at up to `n_neighbors` (default
  8) NEIGHBOR specs, each differing from it by ONE indicator parameter nudged a
  single step in that indicator's `param_space` (both +/-1 directions, sorted +
  deduped so enumeration is deterministic). `_neighbor_specs` builds them;
  `_neighborhood_score` returns the MEDIAN neighbor walk-forward avg_score (all
  neighbor evals use persist=False so memory is never polluted). The search then
  records `score_overrides[fp] = min(own_score, neighborhood_score)` and the
  registry ranks by that override, so a knife-edge strategy that only wins at one
  exact parameter value (its neighbors score poorly) is demoted / dropped - it
  is overfit by definition. Gate OFF => score_overrides empty => ranking
  byte-identical to before. Added tests/test_neighborhood_gate.py (5 tests incl.
  a planted-overfit fixture that the min() demotes). NEXT: U4.5 (regime-sliced
  validation).
- 2026-07-09 U4.3 DONE - Multi-seed stability gate. New config block
  `memory.search.stability` (enabled/n_seeds/warmup_jitter/require_all_positive,
  default OFF). When on, any spec that would enter the registry is re-run n_seeds
  (default 3) extra times, each with a different bootstrap seed + a warmup
  jittered by +/-warmup_jitter bars; promotion now requires the rank score to
  stay strictly positive in EVERY run, so a knife-edge fluke that only wins under
  one warmup/seed is rejected. Implemented as `_passes_stability_gate` in
  core/strategy/search.py, wired into `_eval_one`'s promotion allowlist AFTER the
  holdout gate and only for base-positive finalists (so it stays cheap).
  `WalkForward.evaluate` gained a `warmup` arg; re-runs use persist=False.
  Added tests/test_stability_gate.py (disabled passes, all-positive passes,
  one-negative rejects+short-circuits, zero rejects, jitter band/floor). Suite
  -> 138 green. NEXT: U4.4 (parameter-neighborhood robustness).
- 2026-07-09 U4.2 DONE - Evolutionary search (Phase U4, fixes diagnosis D4:
  "spend the budget on ROBUST strategies, not fast lucky ones"). New
  `memory.search.method: evolution` in core/strategy/search.py: generation 0 is
  fresh random, then each subsequent generation keeps an elite pool (top
  `evolution.elite_fraction`, default 0.10) and breeds `evolution.mutate_fraction`
  (default 0.60) of the next batch from those elites while the remaining ~40% stay
  fresh random for exploration. Operators are pure-Python and CPU-light: `_mutate`
  jitters ONE param by a single step / swaps one indicator / nudges thresholds by
  +/-0.05; `_crossover` unions two elites' indicator sets and averages shared
  weights; `_breed_from_elites` picks mutate-vs-cross. Every produced spec is
  validated against the live indicator pool and deduped by `fingerprint()` (so no
  spec is evaluated twice and the elite pool converges), and evolution honors
  `ea_compatible_only` (U2.2) so a deep run stays exportable. Config knobs added
  under `memory.search.evolution` (U4.2 config commit). Supersedes structure.md
  P6.5. Tests come in U4.7, docs in U4.8. NEXT: U4.3 (multi-seed stability gate).
- 2026-07-09 U4.1 DONE - Deep-search profile. config.yaml documents a "deep"
  profile (max_trials 400 -> 4000+, n_boot 1000 -> 5000, holdout_bars 0 ->
  15000, min_segments 10 -> 12) and adds `memory.search.time_budget_hours: 0`
  (0 = off) so a long run stops cleanly and ranks whatever was evaluated when the
  wall-clock budget expires. Config-only; the 6h profile remains the default.
- 2026-07-09 Phase U3 (Pessimistic, realistic simulation) COMPLETE (U3.1-U3.7
  all [x]). Fixes diagnosis D3: the internal backtester is no longer silently
  optimistic. All five execution knobs live under `backtest.*` and DEFAULT to
  the realistic (pessimistic) behavior, with the legacy optimistic path reachable
  by config for before/after sensitivity work. U3.1 `fill_policy: next_open`
  (entries + signal-flip exits fill at the NEXT bar open + half-spread +
  slippage; SL/TP still intrabar), U3.2 `intrabar_policy: pessimistic` (STOP
  counted first when a bar touches both SL and TP; optimistic/midpoint for
  sensitivity), U3.3 `spread_model` (session-aware spread widening in the
  rollover window; absent block => flat legacy spread), U3.4 `sizing: risk_pct`
  (risk % of simulated equity via the RiskManager formula, clamped to min/max
  lot, plus the `max_daily_loss` circuit breaker in-sim so backtest/live curves
  share geometry), U3.5 `min_stop_points` (reject entries whose SL is closer than
  the broker minimum, as the MT5 tester does). U3.6 added
  `tests/test_realism.py` (12 tests) locking every pessimism guarantee:
  next_open <= signal_close, pessimistic <= midpoint <= optimistic, rollover
  window costs more, risk_pct clamp, daily breaker cuts trades, too-tight stops
  rejected. U3.7 documented it: README "Simulation realism" section (defaults
  table + config reference + re-baseline pointer), CODE_MAP backtester notes,
  Ideas change log, and the `backtests/realism_baseline.md` before/after
  re-baseline template with an MT5 cross-check table. Offline suite 121 -> 133
  green. NEXT: Phase U4 (spend the budget - deep, smart search).

- 2026-07-09 U2.7 done - README carries "The golden rule: only trade what was
  validated" (parity vs blend, strict exporter, veto knobs) and the EA-parity
  sections; CODE_MAP documents the parity dispatch and the U2.1 hard guard.
  Phase U2 is now COMPLETE (U2.1-U2.7 all [x]). Next: Phase U3 realism.

- 2026-07-09 U2.3 + U2.6 DONE - Parity gap closed on the EA side. U2.3 grew the
  MQL5 EA (`experts/Mt5SmartBotEA.mq5`) from 5 to 8 DIRECTIONAL indicators: it
  now natively votes `supertrend` (SuperTrendDir() replays the exact Python
  recursion for bar-accurate parity), `bbands` (%B-style mean-reversion), and
  `stoch` (the same base+cross mapping as core/indicators/momentum.py), in
  addition to ema/sma/rsi/macd/adx. The exporter's EA_SUPPORTED_INDICATORS and
  the `ea_compatible_only` search voter pool were widened to match 1:1, so the
  strict exporter (U2.1) and the EA-compatible search (U2.2) both know the
  larger set. U2.6 added the Python-vs-EA parity harness so this can never
  silently drift again: `scripts/parity_fixture.py` emits a deterministic
  synthetic-bar fixture + the Python reference BlendedSignal; `experts/
  ParityDump.mq5` dumps the EA's per-bar BlendedSignal on the SAME bars; and
  `tests/test_parity_harness.py` runs two layers - LAYER 1 (CI, no MT5) diffs an
  in-Python line-by-line port of the EA math against the real Strategy (< 1e-6),
  and LAYER 2 (opt-in) diffs the real MQL5 dump if the user drops
  `tests/fixtures/parity_ea.csv` in. This is the automated regression guard for
  exactly the RSI/MACD sign bugs that caused the original tester loss. Suite
  117 -> 121 green. Phase U2 (Parity) is now COMPLETE except U2.7 docs. NEXT:
  U2.7 (docs) then Phase U3 (pessimistic simulation).
- 2026-07-09 U2.5 DONE - Validate the BLEND composite (fixes diagnosis D2 for
  the research path). New `core/strategy/composite.py::CompositeStrategy` adapts
  the exact engine blend (top-K average + global thresholds + weighted SL/TP)
  into a Backtester-scoreable Strategy, and `scripts/validate_ensemble.py`
  replays it through the SAME pessimistic backtester as the internal backtest,
  writing the U1 receipts (per-trade CSV + equity CSV + JSON summary). This means
  no unvalidated composite can silently go live in "blend" mode - it now has
  audit-able walk-forward numbers just like every individual spec. Caveat: it
  validates the PRICE-ONLY portion (memory ensemble + global thresholds + SL/TP);
  the ML learner and news score are not pure OHLCV functions and are excluded to
  avoid lookahead. In the default "parity" mode this composite is bypassed
  entirely. Added tests/test_validate_ensemble.py (6 tests). Suite 111 -> 117
  green. NEXT: U2.3 (grow the EA in MQL5) + U2.6 (Python-vs-EA parity harness).
- 2026-07-09 U2.4 DONE - Live PARITY mode (fixes diagnosis D2). New config
  `decision.mode: "parity" | "blend"` (default "parity"). In parity mode
  `DecisionEngine._decide_parity` trades the TOP-1 registry strategy EXACTLY as
  it was walk-forward validated: its own blended signal, its own long/short
  thresholds, and its own SL/TP ATR multiples - never the old global 0.60
  threshold on an unvalidated ensemble+ML+news composite. The learner, news
  blackout, and timing gate become VETO-ONLY (new `decision.parity_vetoes.*`
  switches + `decision.parity_learner_veto_level`): each can BLOCK an entry the
  validated strategy wanted, but can never create one, flip its direction, or
  resize it. With no promoted strategy for a symbol, parity stays flat (never
  guesses). `decay_monitor` still applies: parity trades the best SURVIVING
  (non-suspect) top strategy. `mode: "blend"` preserves the legacy research
  path byte-for-byte. Added tests/test_parity_mode.py (7 tests). Suite
  104 -> 111 green. NEXT: U2.5 (validate the blend composite) / U2.3 (grow EA).
- 2026-07-08 U2.2 DONE - EA-compatible search mode. New config
  `memory.search.ea_compatible_only` (default false). When true,
  `core/strategy/search.py` filters its directional voter pool to
  `_EA_SUPPORTED_DIRECTIONAL` (ema, sma, rsi, macd, adx - the directional
  subset of the exporter's EA_SUPPORTED_INDICATORS), so every promoted strategy
  exports to the MQL5 EA 1:1 with no dropped indicators. Pairs with the U2.1
  hard guard: search never even produces a strategy the exporter would reject.
  Grid path already used only ema+rsi (naturally compatible). Added
  tests/test_ea_compatible_search.py (4 tests incl. a set-drift guard). Suite
  100 -> 104 green. NEXT: U2.3 (grow the EA in MQL5) / U2.4 (live parity mode).
- 2026-07-08 U2.1 DONE - EA export HARD GUARD. `scripts/export_strategy_for_ea.py`
  gained `--strict` (DEFAULT ON): any EA-unsupported indicator now FAILS the
  export with a listing instead of silently shipping a crippled strategy.
  `--allow-partial` restores the lenient path but drops the unsupported
  indicators, RESCALES surviving weights to conserve total weight, and stamps a
  prominent WARNING block into the .params header. Added
  tests/test_ea_export_parity.py. Suite 95 -> 100 green. NEXT: U2.2
  (memory.search.ea_compatible_only).
- 2026-07-08 U1.7 DONE - Phase U1 (Transparency) fully COMPLETE. Added the
  "Auditing a run" section to README (three receipt types: per-trade + equity
  CSVs, single-file HTML report via scripts/make_report.py, decision journal +
  scripts/explain_decisions.py); synced CODE_MAP (trade_log.py, decision_log.py,
  make_report.py, explain_decisions.py) and Ideas change log. No code change;
  suite unchanged at 95 green. NEXT: U2.1 (parity - EA export hard guard).
- 2026-07-08 Phase U1 (Transparency) COMPLETE through U1.6. U1.1-U1.5 code
  landed earlier (backtester full receipts, core/utils/trade_log.py CSVs,
  scripts/make_report.py HTML, decision journal + explainer, config snapshot);
  U1.6 adds tests/test_transparency.py (offline: CSV row count == num_trades,
  per-trade pnl == gross - costs, implied_total_cost reconciliation, HTML
  report builds incl. empty-trades edge case). Suite: 95 tests green. Only
  U1.7 (docs) remains before U2.
- 2026-07-08 Initial version. Derived from full code review + the user's
  real-world XAUUSD tester loss report. Root causes D1-D5 documented;
  six phases U1-U6 defined with steps and acceptance criteria.
