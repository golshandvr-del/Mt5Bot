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
