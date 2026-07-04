# Ideas.md - Improvement Backlog for MT5 Smart Trading Bot

> PURPOSE: This file is the living idea/backlog log for the project. Per the
> project rules, ideas are written here BEFORE any code change is made, and this
> file is updated immediately after every change. It records what we plan to do,
> why, the status, and honest notes on realism and hardware fit (Windows 7,
> CPU-only, mid-range hardware).
>
> RULES:
>   - Standard ASCII English only, everywhere.
>   - Add an idea here first, THEN implement it.
>   - Update the status the moment an idea is done.
>   - Keep it consistent with CODE_MAP.md and README.md.

Legend for status: [ ] planned   [~] in progress   [x] done   [-] rejected/deferred

> NOTE: A prioritized STRUCTURAL ROADMAP derived from an external expert-AI
> review now lives in `structure.md`. It groups the next steps into Track A
> (statistical robustness - TOP priority) and Track B (living/adaptive bot
> ideas), each mapped to the exact files to touch. Read `structure.md` for the
> execution order; keep it in sync with this file, `CODE_MAP.md`, and `README.md`.

---

## 0. Context snapshot

- Phases 1-4 are already implemented and tested offline (see CODE_MAP.md).
- This backlog focuses on PHASE 5: "upgrade and improve the previous 4 phases"
  without breaking the existing decoupled, config-driven, Windows-7-friendly
  architecture.
- Every Phase 5 upgrade must be:
    1. Optional / config-driven (default OFF or safe on weak hardware).
    2. Decoupled (no new hard dependency for the live-light path).
    3. Graceful (missing dep / offline never crashes the bot).
    4. ASCII-only and Windows 7 / Python 3.8 compatible.

---

## 1. Phase 5 - upgrades to Phase 1 (Learning core)

- [x] **Model calibration + confidence gating.** Add a light, pure-Python
  probability calibration (Platt-style / isotonic-lite) so the learner's
  `predict_proba_up` is better calibrated, and let the decision engine gate on
  confidence. Realistic and cheap on CPU.
  (Implemented as `core/learning/calibration.py`, wired into MLClassifier via
  `learning.ml_classifier.calibrate`.)
- [x] **Feature importance export.** When the backend supports it (lightgbm /
  sklearn), persist feature importances to a JSON next to the model so a human
  can see what drives predictions. Zero runtime cost live.
  (Implemented: MLClassifier writes `<model_file>.importances.json` on fit.)
- [ ] **Time-series cross-validation for training.** Replace the single
  train/all split with expanding-window CV to reduce overfitting; keep it light.
- [ ] **Model registry / versioning.** Save models with a timestamp + metrics
  sidecar so we can roll back to a previous model.

## 2. Phase 5 - upgrades to Phase 2 (Indicators)

- [x] **New pluggable indicators.** Add a few high-value, cheap indicators to
  widen combination space: `psar` (Parabolic SAR, trend), `stochrsi`
  (momentum), `dpo` (Detrended Price Oscillator), `vwma` (Volume-Weighted MA).
  All pure-Python, self-registering, added to config.
- [x] **Indicator health / NaN guards.** Central helper so a degenerate series
  (all zeros / too short) returns a neutral signal instead of noise.
  (Implemented as `Indicator.safe_signal` + used by registry blending.)
- [ ] **Multi-timeframe confirmation indicator wrapper.** Wrap any indicator to
  confirm the higher timeframe direction (config-driven).

## 3. Phase 5 - upgrades to Phase 3 (Memory & self-improvement)

- [x] **Regime tagging in memory.** Tag each backtest segment with a simple
  market regime (trend vs range via ADX, high vs low vol via ATR percentile) and
  store it, so strategy selection can prefer strategies that worked in the
  CURRENT regime. Big realism win, cheap to compute.
  (Implemented: `core/strategy/regime.py`; regime stored per result; engine can
  prefer regime-matched strategies.)
- [x] **Early-stopping / pruning in search.** Skip clearly bad specs early
  (e.g. no trades or catastrophic drawdown on the first segment) to explore more
  of the space in the same time budget on weak CPUs.
- [ ] **Bayesian-ish guided search.** Bias random sampling toward parameter
  regions that scored well (cheap surrogate, no heavy libs).
- [ ] **Memory maintenance.** Add pruning of stale/low-score strategies and a
  compaction routine for the SQLite DB.

## 4. Phase 5 - upgrades to Phase 4 (News)

- [x] **Per-symbol economic-calendar blackout hook.** Config-driven high-impact
  event windows (offline JSON the user can edit) so the bot avoids trading
  around NFP/FOMC/CPI even with no live feed. Degrades to no-op if absent.
  (Implemented: `core/news/calendar.py`; consulted by decision blackout.)
- [x] **Sentiment lexicon expansion + negation handling.** Improve the offline
  lexicon scorer (more finance terms, simple negation flip) for better signal
  with zero new dependencies.
- [ ] **Source reliability weighting.** Weight sources by a configurable trust
  score when aggregating.

## 4b. USER-UPDATE-REQUEST - Time / Session / Season awareness (Phase 5, new)

> The user asked: the robot should pay attention to the time frame and SEASON
> (trading sessions such as London/New York), and to WHICH DAY OF THE WEEK it is
> trading, because the best strategy can depend on these. These effects are NOT
> guaranteed, so the bot must RECOGNIZE this itself from data rather than assume.
>
> Design decision: build a new, decoupled `core/timing/` layer that (a) detects
> the time context of any bar (session, day-of-week, hour bucket, month/quarter
> season) with pure-Python stdlib only, and (b) LEARNS from historical trade
> outcomes which time buckets were actually favorable per symbol/timeframe, then
> feeds a light time-context signal + a "favorable window" flag into the decision
> engine. Everything is config-driven and default-safe so the live-light path on
> Windows 7 is unchanged. This is the honest, realistic version of the request:
> the edge is discovered empirically and only used when statistically supported.

- [x] **Session/time-context detector (`core/timing/session.py`).** Pure-Python,
  no deps. Given a bar UTC timestamp -> TimeContext(session set incl. overlaps,
  day_of_week, hour, hour_bucket, month, quarter, season label). Session hours
  are config-driven (`timing.sessions`) with sensible FX defaults (Sydney/Tokyo/
  London/New York, plus London-NY and Tokyo-London overlaps). Handles the input
  timezone from `general.timezone` offset (config `timing.utc_offset_hours`).
- [x] **Empirical time-bucket learning (`core/timing/time_stats.py`).** Aggregate
  historical trade PnL into per-(symbol, timeframe, bucket_type, bucket_value)
  edge statistics (count, win_rate, avg_pnl, expectancy, an edge score in
  [-1,+1]). Persist to the memory SQLite as a new `time_stats` table so it
  survives restarts. Only trust a bucket once it has >= min_samples trades; below
  that it stays neutral. This is the "recognize it itself" requirement.
- [x] **Time-context provider + signal (`core/timing/time_context.py`).**
  Combine live session detection with the learned stats to produce, for the
  latest bar: a `time_signal` in [-1,+1] (directionless confidence -> applied as
  a size/threshold modifier), a `favorable` flag, and a `blackout` flag for
  historically bad windows. Degrades to neutral when stats are insufficient or
  the feature is disabled.
- [x] **Search integration.** During strategy search / walk-forward, record each
  trade's entry-bar time buckets so `time_stats` gets populated automatically the
  more the bot explores (ties into Phase 3 memory / self-improvement).
- [x] **Decision engine integration.** Add an optional time component to the
  blend: a `timing` weight, plus a "favorable-window gate" (raise threshold or
  scale size down in historically weak windows). All config-gated, default light.
- [x] **Config additions (`timing:` section).** enabled, utc_offset_hours,
  sessions map, learning min_samples, weight, gating options; default OFF/safe.
- [x] **Tests** (`tests/test_timing.py`) for session detection boundaries,
  day/season labeling, stats aggregation + persistence, and neutral degradation.
- [x] **Features:** expose session/day/season as optional learner features so the
  ML model can also use them (config `timing.as_features`, default off).

## 5. Phase 5 - cross-cutting upgrades

- [x] **Decision engine: confidence + regime awareness + calendar blackout.**
  Blend upgrades from phases above into the engine, all config-gated so the
  default light path is unchanged.
- [x] **Decision engine: time/session awareness.** Optional `timing` component +
  favorable-window gating, all config-gated (see section 4b).
- [x] **Config additions** for every new feature, defaulting to safe/off where
  heavier.
- [x] **Tests** for all new modules (calibration, new indicators, regime,
  calendar, expanded sentiment) added to the stdlib-only suite.
- [x] **CODE_MAP.md + README.md** updated to describe Phase 5.
- [ ] **CI workflow file** (GitHub Actions) running the offline test suite on
  push (nice-to-have; does not affect Windows 7 runtime).

---

## 6. Explicitly out of scope / rejected (with reasons)

- [-] Heavy deep RL (PPO/DQN with torch) for live use: too heavy for a Win7,
  CPU-only, DDR3 box. Kept only as optional/isolated tabular Q-learning.
- [-] Literal source-code self-rewriting: unsafe and not what "self-improvement"
  should mean here; we do strategy/parameter search + walk-forward instead.
- [-] Real-time tick ML inference: unnecessary latency/CPU cost; per-bar
  decisions are sufficient and robust.

---

## 7. Change log (append newest at top)

- P2.3 (Track A / A3, code+config). Wired the two P2.1/P2.2 significance
  helpers into compute_metrics: every backtest metric dict now carries
  `win_rate_ci_low` (Wilson 95% lower bound on the win-rate) and `pnl_pvalue`
  (seeded bootstrap p-value for "mean trade PnL <= 0", deterministic under the
  global seed; conservative 1.0 on empty/n_boot<=0). Added the
  `memory.search.significance` block to config.yaml (enabled: true,
  max_pvalue: 0.05, min_winrate_ci_low: 0.0 = optional gate off by default
  since a profitable strategy can win less than half the time). Decision: keep
  the win-rate CI gate opt-in and rely on the p-value as the primary filter,
  because expectancy (not win-rate) is what the search ranks by. No behavior
  change to the registry yet; P2.4 enforces the filter in the memory store and
  P2.5 adds the formal tests.
- P2.2 (Track A / A3, code). Added bootstrap_pvalue(trade_pnls, n_boot=1000,
  seed=42) to core/strategy/metrics.py: a pure-Python, seeded (deterministic)
  bootstrap p-value for "mean trade PnL <= 0". Uses a private random.Random so
  it is reproducible under the project global seed and never disturbs global RNG
  state. Together with P2.1's Wilson interval this gives the two significance
  signals; P2.3 wires them into compute_metrics + a memory.search.significance
  config block, and P2.4 enforces the filter so non-significant strategies are
  recorded but never promoted.
- P2.1 (Track A / A3, code). Added wilson_interval(wins, n, z=1.96) to
  core/strategy/metrics.py: a pure-Python Wilson score confidence interval for
  the win-rate that stays in [0, 1] and is honest for small samples. This is the
  first brick of the P2 statistical-significance filter; P2.2 adds a bootstrap
  p-value, P2.3 wires both into compute_metrics + a config block, and P2.4
  enforces the filter so non-significant strategies are recorded but never
  promoted to the registry. Default behavior of compute_metrics is unchanged
  until P2.3.
- P1.6 (Track A / A1 + A2, docs). Phase P1 documentation sync + status flips.
  Flipped section-3 roadmap items A1 (multi-year real-data workflow) and A2
  (more walk-forward segments + locked holdout) to done with dated STATUS notes;
  A1's long search stays a user action on the Windows machine, A2's code/config/
  tests are all complete (P1.2-P1.5). Confirmed CODE_MAP.md sections 8/17 and
  README section 1a (+ the holdout config note) were already in sync from the
  P1.4/P1.5 commits, so P1.6 only added the status flips and change-log notes.
  This closes Phase P1; the next work is Phase P2 (statistical-significance
  filter: Wilson interval + bootstrap p-value in metrics, enforced in the
  registry). Offline suite still 29 tests, all green.
- P1.5 (Track A / A2, test). Added tests/test_walk_forward.py (8 stdlib-only
  tests) locking in the P1.3 segmentation and P1.4 holdout behavior: segment
  count grows with history and reaches min_segments; the holdout tail never
  leaks into any train/test window; evaluate_holdout is a no-op when disabled and
  blocks a spec that fails on the untouched holdout; the store allowlist
  restricts registry promotion. Full offline suite is now 29 tests, all green.
- P1.4 (Track A / A2, code). Locked "quarantine" holdout: the final
  memory.walk_forward.holdout_bars of history are now reserved and NEVER seen by
  the search (walk_forward.searchable_bars() bounds segments() and the 70/30
  fallback). New WalkForward.evaluate_holdout() backtests a spec on just that
  tail; search.py only lets holdout-passing fingerprints be promoted, via a new
  optional allowed_fingerprints allowlist on store.top_strategies/update_registry.
  Default holdout_bars=0 keeps everything a byte-identical no-op. Realism note:
  this is the strongest anti-overfit guard yet - a strategy must survive on data
  that could not have influenced its selection before it is trusted live. Offline
  suite still green (dedicated test lands in P1.5).
- P1.3 (Track A / A2, code). walk_forward.py segments() now produces at least
  min_segments (6-10) rolling out-of-sample windows on long histories by
  auto-shrinking the train window (new effective_train_bars(n); shrink-only, with
  a floor). This directly attacks the "~2 segments = luck-trusting" risk while
  leaving short-history and already-long-enough cases byte-identical. Realism
  note: more out-of-sample windows = a more honest generalization estimate before
  a strategy is trusted. Offline suite still green.
- P1.2 (Track A / A2, config only). Added two walk-forward knobs to config.yaml:
  memory.walk_forward.min_segments (default 6) to later request 6-10 rolling
  segments, and memory.walk_forward.holdout_bars (default 0 = off) to later
  reserve a locked quarantine holdout. Defaults chosen so current behavior stays
  byte-identical until the consuming code arrives in P1.3/P1.4. No source code
  changed; offline suite still green.
- ROADMAP EXECUTION START - P1.1 (Track A / A1, docs only). Documented the
  multi-year REAL-data workflow in README.md (new subsection 1a): export >= 5
  years of M15 bars per symbol via scripts/export_history.py on Windows with MT5
  open, expected CSV names in data_store/history/, and how to run a long
  `--mode search` afterwards. Decision/realism note: this is the highest-value
  step per the expert review (small samples are the biggest risk), but the export
  itself is a USER action on the Windows machine - the AI only prepares docs and
  must not block on it. No source code changed; offline suite still green.
- EXPERT-AI REVIEW captured as a prioritized roadmap in the new `structure.md`.
  Headline finding: the biggest current risk is STATISTICAL (small samples: ~2
  walk-forward segments, ~20 trades per time bucket), not software. Roadmap is
  split into two tracks, each task mapped to real files (no code changed yet):
    Track A (statistical robustness, do first): A1 export multi-year real data +
    long search; A2 more walk-forward segments (6-10) + a locked holdout; A3
    Wilson CI + bootstrap p-value significance filter in metrics/registry; A4
    higher time-bucket min_samples (50+) + Bayesian shrinkage; A5 per-symbol (or
    per-asset-class) ML model; A6 weekend swap + gap in the backtester (esp.
    XAUUSD); A7 GitHub Actions CI running the offline suite.
    Track B (living, adaptive bot): B1 strategy "council" (bandit live
    credibility) instead of static ensemble; B2 time-calendar x market-regime 2D
    buckets; B3 self-doubting strategy-decay monitor (KS/mean-std drift ->
    "suspect", weight 0); B4 automatic overnight training when market is closed;
    B5 contrarian strategies as a regime-change sensor; B6 human-readable weekly
    journal; B7 evolutionary search (GA over registry parents) vs pure random;
    B8 recency weighting in walk-forward score aggregation.
  Execution order and file-by-file targets are in structure.md section 3-5.
- USER-UPDATE-REQUEST (time/session/season awareness) PLANNED then STARTED:
  added Ideas section 4b; building a new decoupled `core/timing/` layer
  (session detector + empirical time-bucket learning persisted to memory +
  time-context provider) and wiring an optional, config-gated `timing` component
  into the decision engine and (optionally) the learner features. Default OFF so
  the Windows 7 live-light path is unchanged. Realistic framing: the time edge is
  discovered from historical trade outcomes, not assumed.
- Phase 1 upgrade DONE: added core/learning/calibration.py (pure-Python Platt
  calibrator); MLClassifier now optionally calibrates P(up) on a held-out tail
  (learning.ml_classifier.calibrate, default true), persists the calibrator, and
  exports feature importances to a JSON sidecar when the backend supports it
  (lightgbm/GBDT); run_train passes feature names. HistGBDT has no importances
  and gracefully skips the sidecar. Verified train/save/load/predict offline.
- Phase 2 upgrade DONE: added 4 pluggable indicators (psar, stochrsi, dpo, vwma)
  in core/indicators/extra.py; added Indicator.safe_signal() health guard; wired
  it into the decision engine fallback blend; added the new indicators to
  config.yaml (default OFF).
- Init: created Ideas.md and planned Phase 5 upgrades (this entry).
