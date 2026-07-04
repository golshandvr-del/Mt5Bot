# structure.md - Project Structure and Upgrade Roadmap

> READ THIS AFTER `CODE_MAP.md`. Where `CODE_MAP.md` explains WHAT the code is
> today (the single source of truth for the current architecture), this file
> (`structure.md`) is the forward-looking STRUCTURAL ROADMAP: it records the
> current structure at a glance and, most importantly, the prioritized plan for
> HOW the project must evolve next.
>
> This roadmap is derived from an external expert-AI review of the project. The
> project should continue to be developed by following the priorities and tasks
> in this file, keeping it in sync with `CODE_MAP.md`, `Ideas.md`, and
> `README.md` on every change.
>
> RULES (same as the rest of the project):
>   - Standard ASCII English only, everywhere (code, comments, docs, filenames).
>   - Update an idea/task status here the moment it is started/finished.
>   - Keep this file consistent with CODE_MAP.md, Ideas.md, and README.md.
>   - Every upgrade stays optional / config-driven, decoupled, graceful, and
>     Windows 7 + Python 3.8 + CPU-only compatible.

Legend for status: [ ] planned   [~] in progress   [x] done   [-] rejected/deferred

---

## 0. How the three docs relate

| File          | Answers                                    | Update when              |
|---------------|--------------------------------------------|--------------------------|
| `CODE_MAP.md` | WHAT the code is now (authoritative map)   | any code change          |
| `Ideas.md`    | Idea backlog log (write idea BEFORE change)| before + after a change  |
| `structure.md`| Structure snapshot + prioritized ROADMAP   | when direction/plan moves |
| `README.md`   | User-facing install/run/backtest/VPS guide | user-visible changes     |

If any of these drift from the real code, fix the drift immediately.

---

## 1. Current structure snapshot (as-built)

The entire project lives ONLY under `mt5/mt5_bot/`. Layered, decoupled,
config-driven. See `CODE_MAP.md` for the per-file detail; this is the map at a
glance so this roadmap is self-contained.

```
mt5/mt5_bot/
  main.py                 CLI entry (train/search/backtest/paper/live/loop)
  install.bat             one-click Windows 7 installer
  requirements.txt        Win7 / Python 3.8 pinned deps
  CODE_MAP.md             authoritative architecture map
  Ideas.md                idea backlog log
  structure.md            THIS FILE (structure + roadmap)
  README.md               user guide

  config/                 config.yaml (master switches) + loader.py
  app/                    context.py (assembly) + runners.py (one per mode)
  core/
    data/                 mt5_connector.py, data_feed.py
    indicators/           Phase 2 pluggable indicators (trend/momentum/
                          volatility/volume/patterns/extra)
    learning/             Phase 1 swappable learners (ml/rl/dl/transfer/ssl)
                          + features.py + calibration.py
    strategy/             Phase 3 strategy.py / backtester.py /
                          walk_forward.py / search.py / metrics.py
    memory/               Phase 3 store.py (SQLite + JSON registry)
    news/                 Phase 4 base/sentiment/sources/aggregator
    timing/               Phase 5 session/time_stats/time_context
    decision/             engine.py (signal fusion -> Decision)
    execution/            risk_manager.py + order_manager.py
    utils/                logger.py + helpers.py

  installer/              install_helper.py + install_vcredist.ps1
  scripts/               run_bot.bat, export_history.py,
                          export_strategy_for_ea.py
  experts/               Mt5SmartBotEA.mq5 + README_EA.md + params/
  tests/                 offline stdlib-only suite + run_all.py
  examples/              generate_sample_data.py
  data_store/            history CSVs, memory.sqlite, strategy_registry.json,
                          news_cache/
  models/                trained model artifacts
  backtests/             backtest_report.json
  logs/                  rotating mt5_bot.log
```

Phase status today (from CODE_MAP.md section 17):

- Phases 1-4: implemented + offline-tested.
- Phase 5 (timing/session/season, "user-update-request"): implemented, wired
  optional/default-OFF into decision engine + features + search feedback.
- Selected Phase 5 quality upgrades already done: model calibration + feature
  importance export; new indicators (psar/stochrsi/dpo/vwma) + safe_signal;
  regime tagging; early-stopping/pruning in search; economic-calendar blackout;
  sentiment lexicon expansion.

---

## 2. Expert-AI review - headline conclusion

The expert review concluded the project is engineering-sound and, for a personal
bot, above expectations: realistic self-improvement (search + walk-forward +
persisted memory), light enough for Windows 7, and a genuinely LEARNED (not
assumed) Phase 5 time/session layer.

The BIGGEST CURRENT RISK IS STATISTICAL, NOT SOFTWARE:
sample sizes are too small (only ~2 walk-forward segments; ~20 trades per time
bucket), so the bot risks trusting lucky/random patterns.

Therefore the number-one priority is: export multi-year REAL data, increase the
number of walk-forward segments, and add a statistical-significance filter.
After that, the "live council" (idea 1) and "decay monitor" (idea 3) upgrade the
bot from an offline learner into a live, adaptive system.

---

## 3. ROADMAP - Track A: statistical robustness (TOP PRIORITY)

> This entire track exists to fix the biggest risk (small-sample overfitting).
> Do this BEFORE the non-linear ideas in Track B.

- [x] **A1. Export multi-year real history and run a long search (do FIRST).**
  Use `scripts/export_history.py` on Windows with MT5 open to export several
  years of real bars per symbol into `data_store/history/<SYM>_<TF>.csv`, then
  run `python main.py --mode search` over that real data. This is the single
  most important step; everything else builds on having enough real samples.
  Files: `scripts/export_history.py`, `app/runners.py::run_search`,
  `core/strategy/search.py`. No code change strictly required to START, but
  document the recommended multi-year workflow in README.
  STATUS (P1.1 / P1.6): the recommended multi-year export + long-search
  workflow is now documented in README (section 1a). The actual export and
  long search are USER actions on the Windows machine (need MT5 + real data),
  so the roadmap prerequisite is DONE while the run itself stays user-driven.

- [x] **A2. More walk-forward segments + a locked holdout.**
  Lower `train_bars` and raise the segment count to 6-10, and reserve a final
  "quarantine"/holdout period that the search NEVER sees. A strategy is only
  promoted after it also passes on the untouched holdout.
  Files: `config/config.yaml` (`memory.walk_forward`, add `holdout_bars` /
  `min_segments`), `core/strategy/walk_forward.py` (`segments()` +
  holdout-aware `evaluate()`), `app/runners.py`.
  STATUS (P1.2-P1.5 / P1.6): DONE. `min_segments` + `holdout_bars` added to
  config (P1.2); `effective_train_bars` auto-shrinks the train window to reach
  `min_segments` (P1.3); the locked holdout tail is quarantined from every
  train/test segment and `evaluate_holdout` + the store `allowed_fingerprints`
  allowlist gate registry promotion (P1.4); `tests/test_walk_forward.py`
  (8 tests) locks all of this in (P1.5).

- [ ] **A3. Statistical-significance test in metrics.**
  Add, in pure Python: a Wilson confidence interval for win-rate and a simple
  bootstrap p-value over trade PnLs. A strategy that is not statistically
  distinguishable from random must NOT enter the registry. Cheap and light.
  Files: `core/strategy/metrics.py` (add `wilson_interval`, `bootstrap_pvalue`,
  extend `compute_metrics`), `core/memory/store.py` (filter in
  `top_strategies`/`update_registry`), `config/config.yaml`
  (`memory.search.significance`), plus tests.

- [ ] **A4. Time-bucket robustness: higher min_samples + Bayesian shrinkage.**
  Raise the time-bucket `min_samples` to 50+, and instead of a raw threshold,
  apply Bayesian shrinkage that pulls a bucket's edge toward zero in proportion
  to how few samples it has. A few lines that stop hallucinated time patterns.
  Files: `core/timing/time_stats.py` (shrinkage in the edge calc),
  `config/config.yaml` (`timing.learning.min_samples`,
  `timing.learning.shrinkage`), plus a test.

- [ ] **A5. Per-symbol (or per-asset-class) ML model.**
  Train a separate ML model per symbol (at minimum per asset class: FX vs gold)
  instead of one shared model, so XAUUSD does not dilute EURUSD and vice versa.
  Files: `app/runners.py::run_train` (loop per symbol, per-symbol model files),
  `app/context.py` (per-symbol learner cache / lookup),
  `core/decision/engine.py` (select the symbol's learner),
  `config/config.yaml` (`learning.per_symbol: true`).

- [ ] **A6. Weekend swap + gap in the backtester (esp. XAUUSD).**
  Model weekend/rollover swap and the Monday gap in the internal backtester so
  gold and carry-sensitive pairs are ranked more realistically.
  Files: `core/strategy/backtester.py` (swap/gap in the PnL + exit logic),
  `config/config.yaml` (`backtest.swap_*`, `backtest.model_weekend_gap`).

- [ ] **A7. GitHub Actions CI (offline only).**
  A ~15-line workflow that runs `python tests/run_all.py` on push. Zero effect
  on the Windows 7 runtime; guards regressions as the roadmap is executed.
  Files: `.github/workflows/ci.yml`. (Also listed in Ideas.md section 5.)

---

## 4. ROADMAP - Track B: non-linear "living, adaptive bot" ideas

> After Track A gives us trustworthy samples, these turn the bot from an offline
> learner into a live, self-doubting, adaptive system. All optional / config-
> gated / pure-Python / Win7-safe. Numbering matches the expert review's ideas.

- [ ] **B1. "Strategy council" instead of a plain ensemble (live credibility).**
  Today the top-3 strategies are blended by a static average. Instead give each
  strategy a LIVE credibility that updates from its recent performance (e.g.
  last ~30 trades) via a light multi-armed-bandit rule (tabular UCB or Exp3,
  pure Python). A recently-bad strategy quiets itself automatically WITHOUT a
  new search. This is live self-learning, not just offline.
  Files: new `core/strategy/council.py` (bandit weights), consumed in
  `core/decision/engine.py` ensemble blend; persist live credibility in
  `core/memory/store.py` (new column/table) so it survives restarts;
  `config/config.yaml` (`decision.council.*`, default OFF).

- [ ] **B2. Time-calendar x market-regime = 2D matrix.**
  We already have time buckets (Phase 5) and regime tags (trend/range). Combine
  them: "trending London Tuesdays" may behave very differently from "ranging
  London Tuesdays". Extend the `time_stats` bucket key with a `regime` column.
  Use STRONG shrinkage (see A4) because combined buckets have fewer samples.
  Files: `core/timing/time_stats.py` (composite bucket key incl. regime),
  `core/strategy/regime.py` (feed regime at trade time),
  `core/strategy/walk_forward.py` (pass regime when recording trades),
  `config/config.yaml` (`timing.use_regime_buckets`, default OFF), plus a test.

- [ ] **B3. Self-doubting bot - strategy-decay monitoring.**
  Give each registry strategy a "statistical expiry": if the distribution of its
  recent live/paper trade PnL drifts away from its walk-forward distribution
  (simple KS test, or a mean/std comparison), the bot flags it "suspect" and
  zeroes its weight until the next search. A bot that knows "I stopped working"
  is worth more than one that only "learns".
  Files: new `core/strategy/decay_monitor.py`, consumed by
  `core/decision/engine.py` (skip/downweight suspect strategies); recent live
  PnL captured via `core/execution/order_manager.py` + `core/memory/store.py`;
  `config/config.yaml` (`decision.decay_monitor.*`, default OFF), plus a test.

- [ ] **B4. Automatic overnight training on the same weak machine.**
  Make `--mode loop` schedule-aware: when the market is closed (weekend) or the
  session is dead, run a time-boxed `search` so the bot wakes up with a richer
  memory. "It dreams and practices in its sleep." Pure scheduling; CPU-light.
  Files: `app/runners.py::run_loop` (detect closed/dead session -> budgeted
  search), reuse `core/timing/session.py` for session detection,
  `config/config.yaml` (`general.overnight_training.*`, default OFF).

- [ ] **B5. Contrarian strategies as a regime-change sensor.**
  Paper-trade a few deliberately INVERTED versions of the best strategies. If an
  inverted strategy suddenly starts profiting, that is an early regime-change
  alarm no single indicator gives you.
  Files: new `core/strategy/contrarian.py` (invert signal of top specs),
  virtual paper accounting via the existing `Backtester`/paper path, surfaced in
  the decision engine as a warning flag; `config/config.yaml`
  (`decision.contrarian_sensor.*`, default OFF).

- [ ] **B6. Human-readable weekly journal.**
  Each decision already carries `components` + `reasons`. Add a small script that
  emits a weekly plain-text report: "This week I took 12 trades, won 7. My best
  session was London. The news signal blocked entry twice, both correctly." This
  human-in-the-loop report transforms trust and debugging.
  Files: new `scripts/weekly_journal.py` reading logs + `data_store/`
  (memory/registry) and writing `data_store/reports/YYYY-Www.txt`; optional
  hook from `app/runners.py`.

- [ ] **B7. Evolution instead of pure random search.**
  Replace (or augment) 200 independent random specs with a light genetic
  algorithm: take the registry's top specs as "parents", then mutate/crossover
  their parameters. Concentrates search around good regions and is fully
  CPU-friendly (only the ORDER of spec generation changes, not eval cost).
  Files: `core/strategy/search.py` (add `method: "evolution"` alongside random/
  grid), read parents from `core/memory/store.py::top_strategies`,
  `config/config.yaml` (`memory.search.method`, `memory.search.evolution.*`).

- [ ] **B8. Recency weighting in walk-forward.**
  Newer history segments should count more in the final score: a strategy that
  was great 3 years ago but mediocre last year is more dangerous than the
  reverse. Add a simple decay factor when aggregating segment scores.
  Files: `core/strategy/walk_forward.py` (recency-weighted `avg_score`),
  `core/memory/store.py::top_strategies` (recency-weighted AVG),
  `config/config.yaml` (`memory.walk_forward.recency_decay`), plus a test.

---

## 5. PHASED EXECUTION PLAN (authoritative work order)

> This section turns the Track A / Track B roadmap above into concrete,
> commit-sized sub-steps. It is the AUTHORITATIVE work order: any AI or human
> continuing this project MUST execute phases strictly in order (P1 -> P7) and
> sub-steps strictly in order inside each phase.
>
> DEFINITION OF DONE for EVERY sub-step (no exceptions):
>   1. Code/doc change implemented and consistent with the invariants (sec. 6).
>   2. Offline test suite passes: `python tests/run_all.py` (stdlib-only, fast).
>   3. Status checkbox below flipped to [x] and the change-log (sec. 7) updated.
>   4. CODE_MAP.md / Ideas.md / README.md updated if affected.
>   5. `git commit` AND `git push` executed for THIS sub-step alone.
> A sub-step without its own pushed commit is NOT done, and the next sub-step
> MUST NOT be started.

Rationale for the order: statistics first (Track A -> P1..P4) so the later
adaptive ideas (Track B -> P5..P7) learn from trustworthy signal, not noise.

### Phase P1 - Real data + honest evaluation (covers A1, A2)

Goal: kill the single biggest risk (2 walk-forward segments = luck-trusting).

- [x] P1.1 (docs) Document the multi-year REAL-data workflow in README.md:
      how to run `scripts/export_history.py` on Windows with MT5 open,
      recommended >= 5 years of M15 bars per symbol, expected CSV names in
      `data_store/history/`, and how to launch a long
      `python main.py --mode search` afterwards. NOTE: the actual export run
      is a USER action on the Windows machine; the AI only prepares
      docs/scripts and must not block on it. [A1]
- [x] P1.2 (config) Add `memory.walk_forward.min_segments` (default 6) and
      `memory.walk_forward.holdout_bars` (default 0 = off) to config.yaml with
      comments. Unset/zero values must keep today's behavior byte-identical.
      [A2]
- [x] P1.3 (code) Upgrade `core/strategy/walk_forward.py::segments()` to
      produce `min_segments` (6-10) rolling segments by auto-shrinking
      train_bars when history length allows; keep the existing 70/30 fallback
      for short history. [A2]
- [x] P1.4 (code) Locked holdout: reserve the FINAL `holdout_bars` of history
      that the search NEVER sees. Add `WalkForward.evaluate_holdout(spec,
      ohlcv)`; in `core/strategy/search.py` + `core/memory/store.py::
      update_registry`, only promote a strategy to the registry if it also
      passes on the untouched holdout. [A2]
- [x] P1.5 (test) Add `tests/test_walk_forward.py`: segment count grows with
      history, holdout bars never appear in any train/test segment, holdout
      gate blocks a failing spec.
- [x] P1.6 (docs) Sync CODE_MAP.md sections 8/17, Ideas.md, README.md; flip
      A1/A2 statuses in section 3 above.

### Phase P2 - Statistical significance filter (covers A3)

Goal: a strategy that cannot be statistically separated from randomness must
never enter the registry.

- [x] P2.1 (code) `core/strategy/metrics.py`: add `wilson_interval(wins, n,
      z=1.96)` returning (low, high) for win-rate. Pure Python.
- [ ] P2.2 (code) `core/strategy/metrics.py`: add `bootstrap_pvalue(trade_pnls,
      n_boot=1000, seed=...)` -> p-value that mean PnL <= 0, via seeded
      resampling. Pure Python, deterministic under the global seed.
- [ ] P2.3 (code+config) Extend `compute_metrics` to include
      `win_rate_ci_low`, `pnl_pvalue`. Add `memory.search.significance`
      block to config.yaml (`enabled` default true, `max_pvalue` 0.05,
      `min_winrate_ci_low` optional).
- [ ] P2.4 (code) Enforce the filter in `core/memory/store.py`
      (`update_registry` and/or `top_strategies`): non-significant strategies
      are recorded (for memory) but never promoted to the registry.
- [ ] P2.5 (test) Add `tests/test_metrics_significance.py`: Wilson bounds on
      known cases, bootstrap p-value low for a clearly-positive PnL series and
      high for a symmetric-random series, registry rejects non-significant.
- [ ] P2.6 (docs) Sync all four docs; flip A3 status.

### Phase P3 - Robust context modeling (covers A4, A5, A6)

Goal: stop hallucinated time patterns, stop cross-symbol dilution, and rank
gold realistically.

- [ ] P3.1 (code+config) `core/timing/time_stats.py`: raise
      `timing.learning.min_samples` default to 50 and add
      `timing.learning.shrinkage` (Bayesian shrinkage pulling a bucket's edge
      toward zero in proportion to sample scarcity, e.g.
      edge * n / (n + shrinkage_k)). [A4]
- [ ] P3.2 (test) Add `tests/test_timing_stats.py`: a 5-sample bucket's edge is
      heavily shrunk; a 500-sample bucket's edge is nearly raw.
- [ ] P3.3 (code) Per-symbol ML: `app/runners.py::run_train` loops per symbol
      and saves `models/ml_classifier_<SYMBOL>.pkl`; keep the shared-model path
      as fallback when `learning.per_symbol` is false. [A5]
- [ ] P3.4 (code+config) `app/context.py` per-symbol learner cache/lookup and
      `core/decision/engine.py` selects the deciding symbol's learner. Add
      `learning.per_symbol` (default false) to config.yaml.
- [ ] P3.5 (test) Extend `tests/test_learning.py` (or add a test file): two
      symbols train two distinct model files; engine picks the right one.
- [ ] P3.6 (code+config) `core/strategy/backtester.py`: model weekend/rollover
      swap cost and the Monday opening gap (esp. XAUUSD). Add
      `backtest.swap_long_pts`, `backtest.swap_short_pts`,
      `backtest.swap_triple_day`, `backtest.model_weekend_gap` (defaults keep
      old behavior). [A6]
- [ ] P3.7 (test) Backtester test: a position held over a weekend pays swap;
      a stop inside a modeled Monday gap fills at the gapped price, not the
      stop price.
- [ ] P3.8 (docs) Sync all four docs; flip A4/A5/A6 statuses.

### Phase P4 - CI safety net (covers A7)

- [ ] P4.1 (infra) Add `.github/workflows/ci.yml` (~15 lines): on push/PR,
      set up Python 3.8, run `python tests/run_all.py`. Zero impact on the
      Windows 7 runtime. [A7]
- [ ] P4.2 (docs) Add the CI badge/note to README.md; flip A7 status; sync docs.

### Phase P5 - Living adaptive core (covers B1, B3)

Goal: upgrade from "offline learner" to "live, self-doubting system".

- [ ] P5.1 (code) New `core/strategy/council.py`: per-strategy LIVE credibility
      from its recent (~30) trade outcomes via a light bandit rule (tabular UCB
      or Exp3, pure Python). [B1]
- [ ] P5.2 (code) Persist live credibility in `core/memory/store.py` (new
      table or column) so it survives restarts.
- [ ] P5.3 (code+config) Consume council weights in the
      `core/decision/engine.py` ensemble blend instead of the static average.
      Add `decision.council.*` to config.yaml, default OFF.
- [ ] P5.4 (test) Council test: a strategy that keeps losing sees its weight
      decay toward zero; weights persist across a simulated restart.
- [ ] P5.5 (code) New `core/strategy/decay_monitor.py`: per-registry-strategy
      "statistical expiry" - compare recent live/paper PnL distribution vs its
      walk-forward distribution (simple KS or mean/std drift); mark drifted
      strategies "suspect". [B3]
- [ ] P5.6 (code+config) Wire it: `core/execution/order_manager.py` +
      `core/memory/store.py` capture realized PnL per strategy; engine skips /
      zero-weights suspect strategies until the next search. Add
      `decision.decay_monitor.*`, default OFF.
- [ ] P5.7 (test) Decay test: a strategy whose recent PnL distribution flips
      gets flagged suspect and excluded from the blend.
- [ ] P5.8 (docs) Sync all four docs; flip B1/B3 statuses.

### Phase P6 - Smarter evaluation and search (covers B8, B2, B7)

- [ ] P6.1 (code+config) Recency weighting: newer walk-forward segments count
      more in the final score (`memory.walk_forward.recency_decay`, default
      1.0 = today's behavior) in `walk_forward.py` aggregate and
      `store.py::top_strategies`. [B8]
- [ ] P6.2 (test) Recency test: with decay < 1, a recently-good strategy
      outranks an anciently-good one with identical raw averages.
- [ ] P6.3 (code+config) Time x regime 2D buckets: extend the
      `core/timing/time_stats.py` bucket key with a `regime` component
      (trend/range) fed from the regime tagger at trade-record time; apply the
      STRONG shrinkage from P3.1 (combined buckets are small). Add
      `timing.use_regime_buckets`, default OFF. [B2]
- [ ] P6.4 (test) Regime-bucket test: composite key recorded/loaded correctly;
      shrinkage keeps tiny buckets near zero edge.
- [ ] P6.5 (code+config) Evolutionary search: `core/strategy/search.py` gains
      `memory.search.method: "evolution"` - take registry top specs as parents,
      mutate/crossover parameters, keep random immigrants for diversity; eval
      cost per spec is unchanged. [B7]
- [ ] P6.6 (test) Evolution test: children stay inside each param space and
      differ from parents; the method plugs into the existing search loop.
- [ ] P6.7 (docs) Sync all four docs; flip B8/B2/B7 statuses.

### Phase P7 - Autonomy and human-in-the-loop (covers B4, B6, B5)

- [ ] P7.1 (code+config) Overnight training: make `app/runners.py::run_loop`
      schedule-aware via `core/timing/session.py` - when the market is closed
      (weekend) or the session is dead, run a TIME-BOXED search so the bot
      "dreams and practices in its sleep". Add
      `general.overnight_training.*` (enabled, budget_minutes), default OFF.
      [B4]
- [ ] P7.2 (test) Scheduler test: closed-market detection triggers the
      budgeted-search branch; open market never does.
- [ ] P7.3 (code) New `scripts/weekly_journal.py`: read logs + memory/registry
      and emit a plain-text human report to `data_store/reports/YYYY-Www.txt`
      ("This week I took 12 trades, won 7. Best session: London. News blocked
      entry twice, both correctly."). Optional hook from run_loop. [B6]
- [ ] P7.4 (code+config) Contrarian sensor: new `core/strategy/contrarian.py`
      paper-trades INVERTED copies of the top strategies virtually; if an
      inverted strategy starts profiting, raise a regime-change warning flag
      consumed by the decision engine. Add `decision.contrarian_sensor.*`,
      default OFF. [B5]
- [ ] P7.5 (test) Contrarian test: inverted spec produces mirrored signals;
      sustained inverted profit sets the warning flag.
- [ ] P7.6 (docs+final) Sync all four docs; flip B4/B6/B5 statuses; final
      consistency pass over CODE_MAP.md / structure.md / Ideas.md / README.md
      (a "Phase 5-style" polish review of everything added in P1-P7).

---

## 6. Invariants every roadmap task MUST keep (do not break)

1. Optional / config-driven: every new feature defaults OFF or safe on weak HW.
2. Decoupled: no new hard dependency on the live-light path.
3. Graceful: missing dep / offline / short history never crashes the bot.
4. ASCII-only; Windows 7 + Python 3.8 + CPU-only compatible; pure-Python
   preferred (SQLite from stdlib, no heavy libs for new logic).
5. Entire project stays under `mt5/mt5_bot/`.
6. Only `core/data/mt5_connector.py` imports MetaTrader5.
7. Persistence lives in `data_store/` and survives restarts.
8. Add tests for every new module to the stdlib-only `tests/` suite.
9. Update CODE_MAP.md, Ideas.md, structure.md, README.md on every change.

---

## 7. Change log (append newest at top)

- P1.5 DONE (test): added tests/test_walk_forward.py (8 tests) covering P1.3 +
  P1.4. TestWalkForwardSegments: segment count grows with history and reaches
  min_segments (auto-shrink), all segment windows in range. TestWalkForwardHoldout:
  searchable_bars() = n - holdout_bars (and =n when OFF), the quarantined holdout
  tail never appears in any train/test segment, evaluate_holdout() is a no-op when
  disabled (enabled=False, passed=True), the gate BLOCKS a spec that cannot make
  enough holdout trades (high min_trades -> passed=False), and a well-formed pass
  case honors the documented conditions. TestHoldoutRegistryGate: the store
  allowed_fingerprints allowlist promotes both with None, only the subset when a
  set is given, and nothing for an empty set. Tests use in-memory config overrides
  and a temp DB so real data_store/config are untouched; stdlib-only. Full offline
  suite now 29 tests, all green. CODE_MAP.md tests section + test-count references
  (21 -> 29) updated. Next sub-step: P1.6.
- P2.1 DONE (code): added `wilson_interval(wins, n, z=1.96)` to
  core/strategy/metrics.py - a pure-Python Wilson score confidence interval
  (low, high) for the win-rate. It stays inside [0, 1] and is honest for small
  n (the exact small-sample regime Track A targets). Edge cases handled: n<=0
  -> (0.0, 0.0); z<=0 -> (p_hat, p_hat); wins clamped to [0, n]; bounds clamped
  to [0, 1] with low<=high. Verified against the textbook 95% interval for
  50/100 (~0.4038, 0.5962) and the 0/n, n/n, and clamp edge cases. No behavior
  change to compute_metrics/rank_value yet (that arrives in P2.3); the formal
  test is P2.5. CODE_MAP section 8 metrics.py entry updated. Offline suite still
  29 tests, all green. Next sub-step: P2.2.
- P1.6 DONE (docs): Phase P1 documentation sync + status flips. Flipped the
  section-3 Track-A items A1 and A2 to [x] with dated STATUS notes: A1's
  multi-year export + long-search workflow is documented in README (P1.1) and
  the actual run is a user action on the Windows machine; A2's segments +
  holdout code/config/tests are complete (P1.2-P1.5). Confirmed CODE_MAP.md
  section 8 (walk_forward / search / store holdout wording) and section 17
  (29-test count, walk-forward/holdout status) are already in sync from the
  P1.4/P1.5 commits, and README section 1a + the holdout config note already
  cover the user-visible A1/A2 changes, so no further edits were needed there;
  added this P1.6 note plus an Ideas.md entry. Offline suite still 29 tests,
  all green. This completes Phase P1. Next sub-step: P2.1.
- P1.4 DONE (code): locked holdout gate. walk_forward.py now reads
  `memory.walk_forward.holdout_bars` (default 0 = OFF), added
  `searchable_bars(n) = n - holdout_bars` and made `segments()` + the 70/30
  fallback split only the searchable portion, so no train/test window ever
  touches the final holdout tail. Added `evaluate_holdout(spec, ohlcv,
  point=None)` that backtests a spec on just the holdout tail and returns
  `{enabled, passed, score, metrics, holdout_bars, holdout_trades}`; passed
  requires num_trades >= min_trades AND score >= 0 (conservative). search.py::run
  now runs the gate per evaluated spec when holdout is ON and passes the passing
  fingerprints as an `allowed_fingerprints` allowlist to update_registry.
  store.py `top_strategies`/`update_registry` gained an optional
  `allowed_fingerprints` filter (None = unchanged behavior; empty set = promote
  nothing; a set restricts promotion to those specs). With holdout_bars=0 the
  entire gate is a byte-identical no-op. Verified manually: holdout OFF ->
  searchable=n and gate passes; holdout=1200 on n=6000 -> searchable=4800 and all
  test windows stay below index 4800; store allowlist filters correctly (none /
  subset / empty). CODE_MAP.md section 8 (walk_forward + search + store) updated.
  Dedicated test file is P1.5. Offline suite still green (21 tests). Next
  sub-step: P1.5.
- P1.3 DONE (code): walk_forward.py now auto-shrinks the train window to hit
  `memory.walk_forward.min_segments` (clamped 1..10, default 6) rolling
  out-of-sample segments on long histories. Added `effective_train_bars(n)` (uses
  t_max = n - test_bars - (min_segments-1)*step_bars; only ever shrinks, never
  below floor = max(test_bars, 200)) and made `segments()` use it. Behavior is
  preserved when the configured train already yields enough segments and when
  history is too short (segments()=0 -> evaluate()'s 70/30 fallback). Verified:
  n=6000 now gives 6 segments (was 4), n>=8000 unchanged, n=125000 -> 162
  segments, all test windows in range. CODE_MAP.md section 8 updated. Dedicated
  test file is P1.5. Offline suite still green (21 tests). Next sub-step: P1.4.
- P1.2 DONE (config only): added `memory.walk_forward.min_segments` (default 6)
  and `memory.walk_forward.holdout_bars` (default 0 = off) to config.yaml with
  explanatory comments. No code reads them yet (that lands in P1.3 segments and
  P1.4 holdout), so with these defaults the walk-forward behavior is byte-
  identical to before. Verified both keys load via the loader (PyYAML and the
  minimal fallback parser). CODE_MAP.md section-3 memory description updated to
  list the new keys. Offline suite still green (21 tests). Next sub-step: P1.3.
- P1.1 DONE (docs only, no source code changed): added a "Recommended multi-year
  real-data workflow (do this first)" subsection (1a) to README.md under
  "Exporting history and backtesting in MT5". It documents running
  scripts/export_history.py on Windows with the MT5 terminal open, targeting >= 5
  years of M15 bars per symbol (~125k bars; use --bars 150000), the exact output
  CSV names in data_store/history/<SYMBOL>_<TF>.csv, how to force the terminal to
  download older bars, and how to launch a long `python main.py --mode search`
  afterwards plus the backtest/train/paper follow-ups. Note kept explicit that
  the actual export is a USER action on the Windows machine; the AI only prepared
  the docs. A1/A2 section-3 status flips remain deferred to P1.6. Offline suite
  still green (21 tests). Next sub-step: P1.2.
- Rewrote section 5 into the PHASED EXECUTION PLAN: the two-track roadmap
  (A1-A7, B1-B8) is now broken into 7 ordered phases (P1-P7) of small,
  commit-sized sub-steps, each with an explicit definition-of-done that
  REQUIRES a passing offline test run plus a dedicated `git commit` AND
  `git push` before the next sub-step may start. Phase order: P1 real data +
  segments/holdout, P2 significance filter, P3 shrinkage/per-symbol/swap-gap,
  P4 CI, P5 council + decay monitor, P6 recency/regime-buckets/evolution,
  P7 overnight training + journal + contrarian sensor. No source code changed.
- Created `structure.md`: recorded the as-built structure snapshot and captured
  the expert-AI review as a prioritized two-track roadmap - Track A (statistical
  robustness: multi-year real data, more walk-forward segments + holdout,
  Wilson/bootstrap significance filter, time-bucket shrinkage, per-symbol ML,
  weekend swap/gap, CI) and Track B (living-bot ideas: strategy council, time x
  regime matrix, strategy-decay monitor, overnight training, contrarian sensor,
  weekly journal, evolutionary search, recency weighting). No source code
  changed yet; this file defines HOW the project continues from here.
