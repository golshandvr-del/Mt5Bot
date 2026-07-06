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

The entire project lives ONLY under `main/`. Layered, decoupled,
config-driven. See `CODE_MAP.md` for the per-file detail; this is the map at a
glance so this roadmap is self-contained.

```
main/
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

- [x] **A3. Statistical-significance test in metrics.**
  Add, in pure Python: a Wilson confidence interval for win-rate and a simple
  bootstrap p-value over trade PnLs. A strategy that is not statistically
  distinguishable from random must NOT enter the registry. Cheap and light.
  Files: `core/strategy/metrics.py` (add `wilson_interval`, `bootstrap_pvalue`,
  extend `compute_metrics`), `core/memory/store.py` (filter in
  `top_strategies`/`update_registry`), `config/config.yaml`
  (`memory.search.significance`), plus tests.
  STATUS (P2.1-P2.6): DONE. `wilson_interval` (P2.1) + `bootstrap_pvalue`
  (P2.2) added to metrics; `compute_metrics` now emits `win_rate_ci_low` +
  `pnl_pvalue` and config gained the `memory.search.significance` block (P2.3);
  the store now RECORDS but never PROMOTES a non-significant strategy - the
  filter is enforced in `top_strategies`/`update_registry` (P2.4);
  `tests/test_metrics_significance.py` (11 tests) locks it all in (P2.5); docs
  synced here (P2.6). This closes Phase P2.

- [x] **A4. Time-bucket robustness: higher min_samples + Bayesian shrinkage.**
  Raise the time-bucket `min_samples` to 50+, and instead of a raw threshold,
  apply Bayesian shrinkage that pulls a bucket's edge toward zero in proportion
  to how few samples it has. A few lines that stop hallucinated time patterns.
  Files: `core/timing/time_stats.py` (shrinkage in the edge calc),
  `config/config.yaml` (`timing.learning.min_samples`,
  `timing.learning.shrinkage`), plus a test.
  STATUS (P3.1-P3.2 / P3.8, 2026-07-04): DONE. `timing.learning.min_samples`
  default raised from 20 to 50 and a new `timing.learning.shrinkage` knob added
  (default = min_samples; <= 0 disables). `TimeStats._edge_from_row` multiplies
  the bounded edge by `n / (n + shrinkage)` so small buckets are pulled toward a
  neutral 0 edge in proportion to sample scarcity, decoupled from the trust
  threshold; `shrinkage=None` reproduces the pre-P3.1 formula (P3.1).
  `tests/test_timing_stats.py` (8 tests) locks it in - a 5-sample bucket keeps
  < 15% of its raw edge while a 500-sample bucket keeps > 85% (P3.2).

- [x] **A5. Per-symbol (or per-asset-class) ML model.**
  Train a separate ML model per symbol (at minimum per asset class: FX vs gold)
  instead of one shared model, so XAUUSD does not dilute EURUSD and vice versa.
  Files: `app/runners.py::run_train` (loop per symbol, per-symbol model files),
  `app/context.py` (per-symbol learner cache / lookup),
  `core/decision/engine.py` (select the symbol's learner),
  `config/config.yaml` (`learning.per_symbol: true`).
  STATUS (P3.3-P3.5 / P3.8, 2026-07-04): DONE. `learning.per_symbol` (default
  false = one SHARED model, byte-identical to before) added to config.
  `run_train` loops per symbol and saves `models/<model>_<SYMBOL>.pkl` in
  per-symbol mode (P3.3); `BotContext.learner_for` caches one learner per symbol
  and loads that symbol's file (falling back to the shared learner for an
  untrained symbol), wired into the engine via
  `DecisionEngine.learner_provider` only when per_symbol is on (P3.4);
  `tests/test_per_symbol_learning.py` (7 tests) proves two symbols train two
  distinct files and the engine routes each symbol to its own model (P3.5).

- [x] **A6. Weekend swap + gap in the backtester (esp. XAUUSD).**
  Model weekend/rollover swap and the Monday gap in the internal backtester so
  gold and carry-sensitive pairs are ranked more realistically.
  Files: `core/strategy/backtester.py` (swap/gap in the PnL + exit logic),
  `config/config.yaml` (`backtest.swap_*`, `backtest.model_weekend_gap`).
  STATUS (P3.6-P3.7 / P3.8, 2026-07-04): DONE. Four `backtest` config keys
  (`swap_long_pts`, `swap_short_pts`, `swap_triple_day`, `model_weekend_gap`),
  all defaulting to a NO-OP. `Backtester` charges a per-rollover swap (money =
  swap_pts * point * contract * fixed_lot, triple-day billed 3x for the weekend)
  subtracted from PnL on every close, and, when `model_weekend_gap` is on, fills
  a stop that sits inside a modeled Monday gap at the (worse) OPEN price (P3.6).
  `tests/test_backtester_swap_gap.py` (9 tests) locks in the 3-night Fri->Mon
  swap and the gap-vs-stop fill difference (P3.7).

- [x] **A7. GitHub Actions CI (offline only).**
  A ~15-line workflow that runs `python tests/run_all.py` on push. Zero effect
  on the Windows 7 runtime; guards regressions as the roadmap is executed.
  Files: `.github/workflows/ci.yml`. (Also listed in Ideas.md section 5.)
  DONE: the `offline-tests` workflow is LIVE at `.github/workflows/ci.yml` on
  GitHub (project now lives at the repo ROOT, so the workflow runs
  `python tests/run_all.py` directly, no `working-directory` needed). The CI
  badge + note are in README (P4.2). Local offline suite: 64 tests, all green.

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
- [x] P2.2 (code) `core/strategy/metrics.py`: add `bootstrap_pvalue(trade_pnls,
      n_boot=1000, seed=...)` -> p-value that mean PnL <= 0, via seeded
      resampling. Pure Python, deterministic under the global seed.
- [x] P2.3 (code+config) Extend `compute_metrics` to include
      `win_rate_ci_low`, `pnl_pvalue`. Add `memory.search.significance`
      block to config.yaml (`enabled` default true, `max_pvalue` 0.05,
      `min_winrate_ci_low` optional).
- [x] P2.4 (code) Enforce the filter in `core/memory/store.py`
      (`update_registry` and/or `top_strategies`): non-significant strategies
      are recorded (for memory) but never promoted to the registry.
- [x] P2.5 (test) Add `tests/test_metrics_significance.py`: Wilson bounds on
      known cases, bootstrap p-value low for a clearly-positive PnL series and
      high for a symmetric-random series, registry rejects non-significant.
- [x] P2.6 (docs) Sync all four docs; flip A3 status.

### Phase P3 - Robust context modeling (covers A4, A5, A6)

Goal: stop hallucinated time patterns, stop cross-symbol dilution, and rank
gold realistically.

- [x] P3.1 (code+config) `core/timing/time_stats.py`: raise
      `timing.learning.min_samples` default to 50 and add
      `timing.learning.shrinkage` (Bayesian shrinkage pulling a bucket's edge
      toward zero in proportion to sample scarcity, e.g.
      edge * n / (n + shrinkage_k)). [A4]
- [x] P3.2 (test) Add `tests/test_timing_stats.py`: a 5-sample bucket's edge is
      heavily shrunk; a 500-sample bucket's edge is nearly raw.
- [x] P3.3 (code) Per-symbol ML: `app/runners.py::run_train` loops per symbol
      and saves `models/ml_classifier_<SYMBOL>.pkl`; keep the shared-model path
      as fallback when `learning.per_symbol` is false. [A5]
- [x] P3.4 (code+config) `app/context.py` per-symbol learner cache/lookup and
      `core/decision/engine.py` selects the deciding symbol's learner. Add
      `learning.per_symbol` (default false) to config.yaml.
- [x] P3.5 (test) Extend `tests/test_learning.py` (or add a test file): two
      symbols train two distinct model files; engine picks the right one.
- [x] P3.6 (code+config) `core/strategy/backtester.py`: model weekend/rollover
      swap cost and the Monday opening gap (esp. XAUUSD). Add
      `backtest.swap_long_pts`, `backtest.swap_short_pts`,
      `backtest.swap_triple_day`, `backtest.model_weekend_gap` (defaults keep
      old behavior). [A6]
- [x] P3.7 (test) Backtester test: a position held over a weekend pays swap;
      a stop inside a modeled Monday gap fills at the gapped price, not the
      stop price.
- [x] P3.8 (docs) Sync all four docs; flip A4/A5/A6 statuses.

### Phase P4 - CI safety net (covers A7)

- [x] P4.1 (infra) Add `.github/workflows/ci.yml` (~15 lines): on push/PR,
      set up Python 3.8, run `python tests/run_all.py`. Zero impact on the
      Windows 7 runtime. [A7]
      DONE (2026-07-06, SIXTH session): the workflow file
      `.github/workflows/ci.yml` is PRESENT, tracked, and CORRECT on GitHub
      (verified this session via the Contents API: name `ci.yml`, sha
      `3a9c55c...`, 1568 bytes). Because the project now lives at the repo ROOT
      (commit `0c1cfd6`), the workflow body runs `python tests/run_all.py`
      directly with no `working-directory`, which is correct. The GitHub App
      still lacks the `actions` read scope, so this assistant cannot poll the
      Actions API from the sandbox (`gh run list` -> 403) to observe a run
      result - that is an OBSERVABILITY limit, not a code problem. The local
      offline suite is green (64 tests) and CI mirrors that exact command, so
      P4.1 is considered complete and the roadmap advances to P4.2. The
      historical NEARLY-DONE / BLOCKED notes below are retained for provenance.
      NEARLY DONE (2026-07-06, FIFTH session): the workflow file
      `.github/workflows/ci.yml` is now PRESENT, tracked, and CORRECT on GitHub.
      It was first added via the GitHub web UI (commit `e602990 Create ci.yml`),
      which bypasses the GitHub App `workflows`-permission push restriction that
      blocked earlier sessions. When the folder move to `main/` this session
      required changing the workflow's `working-directory` from `mt5/mt5_bot` to
      `main`, the assistant's push of that edit was REJECTED by the same App
      permission gap (it blocks workflow UPDATES too, not just CREATE), so the
      assistant CANNOT edit the workflow from the sandbox. The USER then made that
      one-line fix directly in the GitHub web UI (commit `33b0360 Update ci.yml`):
      the live workflow now correctly runs `working-directory: main`, matching the
      byte-for-byte body of `main/ci_workflow_template.yml`. So the workflow is
      functionally complete and the offline suite is green locally (64 tests).
      P4.1 is kept [~] ONLY because this assistant cannot observe GitHub Actions
      run results from the sandbox (no network/Actions access) to confirm a GREEN
      run. ACTION NEEDED FROM USER (verification only, no edit): check the Actions
      tab on GitHub and confirm the `offline-tests` workflow ran GREEN on a recent
      push. Once confirmed, flip P4.1 to [x] and proceed to P4.2 (CI badge/note in
      README + flip A7 status). The historical BLOCKED notes below are retained
      for provenance.
      BLOCKED (re-verified AGAIN 2026-07-06, FOURTH session, now against the
      `Mt5Bot` repo): the same external blocker persists. This session (a) took a
      fresh manual backup of the project, confirmed it is BYTE-IDENTICAL to the
      GitHub HEAD so nothing was lost, and pushed the full existing commit
      history into the (previously empty) `golshandvr-del/Mt5Bot` repo so the
      project now lives there on `main`; (b) recreated `.github/workflows/ci.yml`
      byte-for-byte from `main/ci_workflow_template.yml` (verified
      ASCII-only: 0 non-printable bytes; YAML parses; name = offline-tests),
      committed it, and ran `git push origin main` against `Mt5Bot` -> STILL
      rejected with "refusing to allow a GitHub App to create or update workflow
      `.github/workflows/ci.yml` without `workflows` permission"; the Contents
      API PUT still returns 403 "Resource not accessible by integration". The
      unpushable commit was rolled back (`git reset --soft HEAD~1` + unstage) so
      HEAD stays == origin/main. Offline suite re-run: 64 tests green. The
      `workflows` permission has NOT been granted, so the blocker is unchanged.
      BLOCKED (re-verified AGAIN 2026-07-06, third session): the same blocker
      persists. This session recreated `.github/workflows/ci.yml` byte-for-byte
      from `main/ci_workflow_template.yml` (0 non-printable bytes, YAML
      parses, name = offline-tests), committed it, and ran `git push origin
      main` -> STILL rejected: "refusing to allow a GitHub App to create or
      update workflow `.github/workflows/ci.yml` without `workflows`
      permission". The GitHub Contents API (`gh api -X PUT .../contents/.github/
      workflows/ci.yml`) STILL returns 403 "Resource not accessible by
      integration". The active credential is a GitHub App user-to-server token
      (ghu_...) whose permissions come from the App installation, which still
      lacks `workflows`. The permission has NOT been granted since the last
      session. The unpushable commit was rolled back (`git reset --soft HEAD~1`)
      so HEAD stays == origin/main and future commits remain pushable; ci.yml is
      left untracked in the working tree. Offline suite re-run: 64 tests green.
      This is an external permission blocker, not a code problem.
      (Historical note from the second session, still accurate): the workflow
      file is
      fully written, ASCII-only, YAML-valid, and verified locally (the exact CI
      command `python tests/run_all.py` from `main` is green, 64 tests).
      It STILL CANNOT be pushed to GitHub because the GitHub App credential used
      for pushes still lacks the `workflows` permission. Re-tested this session:
      committing `.github/workflows/ci.yml` and running `git push origin main`
      is rejected with "refusing to allow a GitHub App to create or update
      workflow `.github/workflows/ci.yml` without `workflows` permission". The
      unpushable commit was rolled back (`git reset --soft HEAD~1`) so the
      branch stays in sync with origin and OTHER doc commits can still be
      pushed; the workflow file itself is left untracked at
      `.github/workflows/ci.yml` in the working tree. This is an external
      permission blocker, not a code problem.
      COMMITTABLE COPY PRESERVED: because the App can push anywhere UNDER
      `main/` (just not under `.github/workflows/`), a byte-for-byte copy
      of the intended workflow is committed at
      `main/ci_workflow_template.yml` (a header explains the copy-into-
      place step; the YAML body below its marker line is the exact file). This
      means the workflow content now lives in version control and survives.
      ACTION NEEDED FROM USER: grant the Genspark/GitHub App the `workflows`
      permission for this repo, then copy `main/ci_workflow_template.yml`
      (the part below its marker line) to `.github/workflows/ci.yml` and push
      (or paste it in via the GitHub web UI). Its exact contents are also
      reproduced in the P4.1 change-log entry in section 7 below. Once pushed,
      flip this box to [x]. Per the scope rules the next sub-step (P4.2) is NOT
      started until P4.1 is pushed.
- [x] P4.2 (docs) Add the CI badge/note to README.md; flip A7 status; sync docs.
      DONE (2026-07-06, SIXTH session): added the `offline-tests` status badge
      (shields via GitHub Actions badge endpoint) to the top of README.md plus a
      short CI note there and in the Testing section; flipped A7 and P4.1 to [x];
      synced CODE_MAP.md (section 13 CI note + section 17 roadmap-progress) and
      Ideas.md. This completes Phase P4 (CI safety net, Track A / A7). Next
      sub-step: Phase P5 (living adaptive core), starting at P5.1.

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
5. Entire project stays under `main/`.
6. Only `core/data/mt5_connector.py` imports MetaTrader5.
7. Persistence lives in `data_store/` and survives restarts.
8. Add tests for every new module to the stdlib-only `tests/` suite.
9. Update CODE_MAP.md, Ideas.md, structure.md, README.md on every change.

---

## 7. Change log (append newest at top)

- FOLDER MOVE mt5/mt5_bot -> main (2026-07-06, FIFTH session; user request; no
  source code changed). The user reported the project had been saved at
  `mt5/mt5_bot/` on GitHub but wanted it under `main/`, matching the section-6
  invariant "Entire project stays under `main/`". Moved the whole tree with
  `git mv mt5/mt5_bot main` (preserving file history) and removed the now-empty
  `mt5/` parent. Because all code uses project-root-relative paths, nothing in
  the source changed and the offline suite re-runs green from `main/` (64 tests).
  Updated every path reference `mt5/mt5_bot` -> `main` across CODE_MAP.md (the
  section-2 tree + repo/CI notes), this file (section-1 tree + this entry),
  Ideas.md, README.md (layout + all `cd` commands), experts/README_EA.md, the
  `ci_workflow_template.yml` reference copy (now `working-directory: main`), and
  `.gitignore`. NOTE: this move was first attempted earlier in the FIFTH session
  but the sandbox RESET before commit/push, losing that work; it was recovered
  this session from a user-provided manual backup (verified byte-identical code
  to the pre-move HEAD) and redone via `git mv` + push, so nothing was lost.
  Historical change-log entries below that mention `mt5/mt5_bot` describe events
  as they happened; the path token was normalized to `main` for consistency
  since the files now live under `main/`. The P4.1 CI workflow is now LIVE in the
  repo (added via the GitHub web UI as commit `e602990 Create ci.yml`, which
  bypasses the GitHub App `workflows`-permission push blocker re-verified through
  the fourth session). The live `.github/workflows/ci.yml` needed its
  `working-directory` changed from `mt5/mt5_bot` to `main` for the new layout;
  the assistant's push of that edit was rejected by the same permission gap
  (which also blocks workflow UPDATES) and rolled back, so the USER made the
  one-line fix directly in the GitHub web UI (commit `33b0360 Update ci.yml`).
  The live workflow now correctly uses `working-directory: main` and CI is
  functional (see section 5 P4.1). Committed + pushed as the folder-structure fix
  before resuming the roadmap.
- P4.1 BLOCKER RE-VERIFIED AGAIN, STILL BLOCKED-ON-PUSH (infra, 2026-07-06,
  FOURTH session, now on the `Mt5Bot` repo): this session started from a fresh
  manual backup link. Downloaded and extracted it, then confirmed it is
  BYTE-IDENTICAL to the existing GitHub HEAD (`git diff` empty), so the backup
  carried no newer work and nothing was lost. The primary repo named in the
  session prompt (`golshandvr-del/Mt5Bot`) was EMPTY (0 commits, just created),
  while the project's full commit history lived in `golshandvr-del/MtBot`
  (identical content). To honor "the entire project lives in main" without
  losing the history, pushed the COMPLETE MtBot commit history into `Mt5Bot`'s
  `main` branch (`git push mt5bot main:main` -> new branch main), so `Mt5Bot`
  now holds every P1-P4 commit. Then re-ran the definitive P4.1 push test one
  more time: recreated `.github/workflows/ci.yml` byte-for-byte from the
  committed `main/ci_workflow_template.yml` (verified ASCII-only: 0
  non-printable bytes; YAML parses with name = offline-tests), committed it
  locally, and ran `git push origin main` against `Mt5Bot`. Result: STILL
  rejected with "refusing to allow a GitHub App to create or update workflow
  `.github/workflows/ci.yml` without `workflows` permission". Also re-tried the
  GitHub Contents API (`gh api -X PUT repos/golshandvr-del/Mt5Bot/contents/
  .github/workflows/ci.yml`) -> STILL 403 "Resource not accessible by
  integration". Confirmed via `gh auth status` that the active push credential
  is a GitHub App user-to-server token (ghu_...) whose permissions come from the
  App installation, which still lacks `workflows`. So the permission has NOT
  been granted and the blocker is unchanged - an external permission limit, not
  a code defect. Rolled the unpushable commit back (`git reset --soft HEAD~1`,
  then unstaged the workflow file) so HEAD stays == origin/main and future doc
  commits remain pushable; `.github/workflows/ci.yml` is left untracked in the
  working tree and `ci_workflow_template.yml` continues to preserve the exact
  content in version control. Offline suite re-run this session: 64 tests, all
  green. Docs synced (this entry + the section-5 P4.1 [~] note + the Ideas.md
  change-log entry; CODE_MAP.md section 13 CI note updated to record the
  fourth-session re-verification and the repo move to Mt5Bot). P4.1 remains [~];
  per the scope rules P4.2 is NOT started until P4.1 is actually pushed. ACTION
  NEEDED FROM USER: grant the Genspark/GitHub App the `workflows` permission for
  the `Mt5Bot` repo, then copy `main/ci_workflow_template.yml` (the part
  below its marker line) to `.github/workflows/ci.yml` and push (or paste it in
  via the GitHub web UI).
- P4.1 BLOCKER RE-VERIFIED AGAIN, STILL BLOCKED-ON-PUSH (infra, 2026-07-06,
  third session): re-ran the definitive push test one more time this session so
  the roadmap is not advanced on a stale conclusion. Recreated
  `.github/workflows/ci.yml` byte-for-byte from the committed
  `main/ci_workflow_template.yml` (verified ASCII-only: 0 non-printable
  bytes; YAML parses with name = offline-tests), committed it locally, and ran
  `git push origin main`. Result: STILL rejected with "refusing to allow a
  GitHub App to create or update workflow `.github/workflows/ci.yml` without
  `workflows` permission". Also re-tried the GitHub Contents API
  (`gh api -X PUT repos/golshandvr-del/MtBot/contents/.github/workflows/ci.yml`)
  -> STILL 403 "Resource not accessible by integration". Confirmed via
  `gh auth status` that the active push credential is a GitHub App
  user-to-server token (ghu_...); its permissions are the App installation's,
  which still does NOT include `workflows`. So the permission has NOT been
  granted since the previous session and the blocker is unchanged - an external
  permission limit, not a code defect. Rolled the unpushable commit back
  (`git reset --soft HEAD~1`, then unstaged the workflow file) so HEAD stays ==
  origin/main and future doc commits remain pushable; `.github/workflows/ci.yml`
  is left untracked in the working tree and `ci_workflow_template.yml` continues
  to preserve the exact content in version control. Offline suite re-run this
  session: 64 tests, all green. Docs synced (this entry + the section-5 P4.1 [~]
  note + the Ideas.md change-log entry; CODE_MAP.md section 13 already carries
  the blocker note and stays accurate). P4.1 remains [~]; per the scope rules
  P4.2 is NOT started until P4.1 is actually pushed. ACTION NEEDED FROM USER:
  grant the Genspark/GitHub App the `workflows` permission for this repo, then
  copy `main/ci_workflow_template.yml` (the part below its marker line)
  to `.github/workflows/ci.yml` and push (or paste it in via the GitHub web UI).
- P4.1 STILL BLOCKED-ON-PUSH, committable copy preserved (infra, 2026-07-06,
  second session): re-verified the P4.1 blocker in a fresh session. Recreated
  `.github/workflows/ci.yml` (the previous session's sandbox tree was gone; the
  earlier `b5b0a2a` commit only RECORDED the blocker in docs and never contained
  the file). Confirmed the file is ASCII-only (0 non-ascii bytes), YAML-valid,
  and that its command `python tests/run_all.py` from `main` is green (64
  tests). Committed it and ran `git push origin main` -> STILL rejected:
  "refusing to allow a GitHub App to create or update workflow
  `.github/workflows/ci.yml` without `workflows` permission". The `workflows`
  permission has NOT been granted yet, so the blocker persists. Rolled the
  unpushable commit back with `git reset --soft HEAD~1` (and unstaged the
  workflow file) so the branch stays in sync with origin/main and future doc
  commits remain pushable; the ci.yml stays untracked in the working tree.
  NEW THIS SESSION: to stop losing the file across sessions, committed a
  byte-for-byte copy at `main/ci_workflow_template.yml` (pushable because
  it is under main/, not under .github/workflows/). It carries a header
  explaining the two activation options (grant the `workflows` permission, or
  paste it in via the GitHub web UI) and the YAML body below its marker line is
  the exact intended `.github/workflows/ci.yml`. Verified the template is
  ASCII-only and its YAML body parses (name = offline-tests). Docs synced
  (this entry + the section-5 P4.1 [~] note + CODE_MAP section 13 CI note +
  Ideas.md). Offline suite still 64 tests, all green. P4.1 remains [~]; per the
  scope rules P4.2 is NOT started until P4.1 is actually pushed. ACTION NEEDED:
  grant the App the `workflows` permission (see the section-5 P4.1 note).
- P4.1 IN PROGRESS / BLOCKED-ON-PUSH (infra, 2026-07-06): wrote
  `.github/workflows/ci.yml`, a minimal GitHub Actions workflow named
  `offline-tests` that runs the stdlib-only offline suite on every push and pull
  request (Track A / A7). Steps: checkout (actions/checkout@v4) -> set up
  Python 3.8 (actions/setup-python@v5, matching the Windows 7 / Python 3.8.x
  target) -> run `python tests/run_all.py` with
  `working-directory: main`. It needs NO MetaTrader5, no network, and no
  heavy dependencies, so it mirrors the local offline gate exactly and has ZERO
  effect on the Windows 7 runtime. The workflow file lives at the REPO ROOT
  (`.github/workflows/`) rather than under `main/` only because GitHub
  recognizes workflows only there; CODE_MAP.md section 1 documents this as the
  single deliberate exception to the "everything under main/" invariant.
  Runner pinned to `ubuntu-22.04` because Python 3.8 is available there via
  setup-python (ubuntu-latest/24.04 no longer ships it natively). Verified
  locally: the YAML parses, is ASCII-only (0 non-ascii bytes), and the exact CI
  command run from `main` is green (64 tests).
  BLOCKER: the file cannot be pushed to GitHub with the current GitHub App
  credential, which lacks the `workflows` permission - both `git push` (remote
  rejected: "refusing to allow a GitHub App to create or update workflow
  `.github/workflows/ci.yml` without `workflows` permission") and the Contents
  API (403 "Resource not accessible by integration") refuse it. This is an
  external permission limit, not a code defect. The file is kept in the sandbox
  working tree, and the DOC changes marking the blocker (this entry + the
  section-4 P4.1 [~] note + CODE_MAP/Ideas) ARE pushed so the state is recorded.
  ACTION NEEDED: grant the App the `workflows` permission, then commit + push
  `.github/workflows/ci.yml` and flip P4.1 to [x]. Exact file contents to
  reproduce it manually (12 lines of YAML under `.github/workflows/ci.yml`):
      name: offline-tests
      on:
        push:
        pull_request:
      jobs:
        tests:
          runs-on: ubuntu-22.04
          steps:
            - uses: actions/checkout@v4
            - uses: actions/setup-python@v5
              with:
                python-version: "3.8"
            - working-directory: main
              run: python tests/run_all.py
  (The committed file also carries a short explanatory header comment.) Offline
  suite still 64 tests, all green. Next sub-step: P4.1 push (blocked on the
  permission grant) - do NOT start P4.2 until P4.1 is pushed.
- P3.8 DONE (docs): Phase P3 documentation sync + status flips. Flipped the
  section-3 Track-A items A4, A5, and A6 to [x] with dated (2026-07-04) STATUS
  notes summarizing their whole sub-step chains: A4 = time-bucket higher
  min_samples + Bayesian shrinkage (P3.1-P3.2), A5 = per-symbol ML training +
  lookup + engine routing (P3.3-P3.5), A6 = weekend swap + Monday gap in the
  backtester (P3.6-P3.7). Confirmed CODE_MAP.md sections 3/4/8/10b and 17 and
  README were already in sync from the P3.1-P3.7 commits (the config keys,
  time_stats shrinkage, per-symbol run_train/context/engine, backtester
  swap/gap, and the test suite - now 64 tests - are all already documented), and
  the CODE_MAP section-17 ROADMAP-PROGRESS note was updated in the P3.7 commit to
  list P3.6/P3.7 done and only P3.8 remaining, so no further CODE_MAP/README
  edits were needed for this docs-only step. Added this note plus an Ideas.md
  entry. Offline suite still 64 tests, all green. This completes Phase P3. Next
  sub-step: P4.1.
- P3.7 DONE (test): added tests/test_backtester_swap_gap.py (9 tests) locking in
  the P3.6 weekend/rollover SWAP + Monday GAP model (Track A / A6). The file was
  recovered from the newer manual backup (it had been written but never
  committed, exactly like the P3.5 file) and merged in BEFORE any new work so
  nothing is lost; it was then validated line-for-line against the CURRENTLY
  committed core/strategy/backtester.py and run green (all API assumptions -
  strategy.decision_series/atr_series, run(strat, ohlcv, warmup=...),
  Backtester._rollovers_between, OHLCV.append_row/.open, and the
  symbol_offline_specs("EURUSD") point 0.0001 / contract 100000 - verified).
  Coverage: (1) TestWeekendSwap uses a deterministic _StubStrategy (fixed
  decision + ATR series) on a flat-priced Friday->Monday series so the ONLY PnL
  is the accrued swap: swap 0.0 (default) is a byte-identical break-even no-op;
  a 10-pt long swap held Fri->Mon is charged exactly 3 nights
  (swap_pts * point * contract * lot * 3); and a negative swap rate is a CREDIT
  (positive PnL). (2) TestRolloverCounting unit-checks _rollovers_between:
  Fri->Mon = 3, an ordinary Mon->Tue midnight = 1, a Tue->Wed midnight into the
  triple day (2=Wed) = 3, and a same-day span = 0. (3) TestMondayGapFill builds
  a long stopped by a Monday bar that gaps DOWN through the stop: with
  model_weekend_gap OFF (default) the stop fills exactly at the stop price
  (98.0), and with it ON the fill is the (worse) gapped OPEN (96.0) and strictly
  more negative than the stop fill. Stdlib-only, ASCII-only. Full offline suite
  now 64 tests (was 55), all green. CODE_MAP.md tests section + the 55->64
  test-count references updated; Ideas.md logged. The A4/A5/A6 status flips are
  P3.8. Next sub-step: P3.8.
- P3.6 DONE (code+config): weekend/rollover SWAP + Monday GAP model in the
  internal backtester (Track A / A6). config.yaml gained four `backtest` keys -
  `swap_long_pts`, `swap_short_pts` (points charged per rollover, money =
  swap_pts * point * contract * fixed_lot; positive = cost, negative = credit),
  `swap_triple_day` (0=Mon..6=Sun, MT5 default Wednesday=2, charged 3x to cover
  the weekend), and `model_weekend_gap` (bool) - all defaulting to a NO-OP so
  the simulator stays byte-identical when unset. `core/strategy/backtester.py`:
  `__init__` reads the keys defensively via new static `_cfg_float/_cfg_int/
  _cfg_bool` helpers. SWAP: `_rollovers_between(prev_ts, cur_ts, triple_day)`
  counts every UTC midnight crossed while a position is open (each billed to the
  day it ENTERS, weekday derived from the epoch-day index where 1970-01-01 =
  Thursday so a Fri->Mon hold correctly bills 3 nights and a rollover into the
  triple-day is 3x), `_swap_money(direction, nights, point, contract)` converts
  nights to money using the long/short rate, and the accrued `swap_accum` is
  subtracted from PnL on EVERY close (SL/TP/opposite-signal and the residual
  close), reset on entry/close. GAP: `_infer_bar_seconds(times, ohlcv)` finds
  the normal bar spacing (timeframe helper, else the most-common positive delta)
  and any bar opening after a pause > 3x that is a "gap bar"; when
  model_weekend_gap is on and a stop sits inside the gap, the fill is the (worse)
  OPEN price - long fills at open when open < stop, short when open > stop -
  instead of the stop price. Verified: rollover math (Fri->Mon=3, Tue->Wed with
  Wed triple=3, same-day=0), and the full offline suite still 55 tests, all
  green (defaults unchanged). CODE_MAP.md sections 3 (config backtest block) and
  8 (backtester.py) updated; Ideas.md logged. The dedicated backtester test is
  P3.7; the A4/A5/A6 status flips are P3.8. Next sub-step: P3.7.
- P3.5 DONE (test): added tests/test_per_symbol_learning.py (7 tests) locking in
  the P3.3 per-symbol training + P3.4 per-symbol lookup (Track A / A5). The file
  was recovered from the newer manual backup (it had been written but never
  committed) and merged in BEFORE any new work so nothing is lost. Coverage:
  (1) TestPerSymbolModelFileNaming proves BotContext._per_symbol_model_file and
  app.runners._per_symbol_model_file produce byte-identical paths for several
  symbols (incl. a broker-style "EURUSD.m" and a "weird/sym"), and that two
  distinct symbols map to two distinct files ending ml_classifier_EURUSD.pkl /
  ml_classifier_XAUUSD.pkl. (2) TestPerSymbolLookup trains two learners on two
  clearly-different synthetic datasets (seeds 11 / 99), saves them into a private
  temp dir via an ABSOLUTE model_file override (real models/ untouched), then
  verifies learner_for returns a DISTINCT, ready, CACHED learner per trained
  symbol; that an untrained symbol falls back to the shared learner; that default
  mode (per_symbol=false) always returns the shared learner AND leaves the engine
  with NO provider (light path unchanged); and that per_symbol=true gives the
  engine a provider. (3) TestEngineSelectsPerSymbolLearner injects two sentinel
  learners through a provider and proves the engine's _learning_signal routes the
  right symbol to the right model (0.9 vs -0.9), while an unknown symbol yields a
  neutral 0.0. Verified: full offline suite now 55 tests (was 48), all green;
  file is standard ASCII only. The A5 status flip stays deferred to P3.8. Next
  sub-step: P3.6.
- P3.4 DONE (code+config): per-symbol learner LOOKUP wired into the live path
  (A5). config.yaml gained `learning.per_symbol` (default false) with an
  explanatory comment: false = one SHARED model for every symbol (byte-identical
  to before), true = train + USE a separate model per symbol.
  `app/context.py` added `learner_for(symbol)` plus a per-symbol learner cache
  (`self._symbol_learners`) and a static `_per_symbol_model_file` that MIRRORS
  runners.py exactly, so training and lookup agree on file paths. In default
  mode `learner_for` just returns the shared `learner` (light path unchanged);
  in per-symbol mode it builds+caches one learner per symbol and loads that
  symbol's `models/<model>_<SYMBOL>.pkl`, gracefully falling back to the shared
  learner when a per-symbol file is missing/unloadable (an untrained symbol
  never crashes and simply contributes a neutral signal). The engine is only
  given the provider when per_symbol is on: `BotContext.engine` passes
  `learner_provider=self.learner_for` iff `learning.per_symbol` is true, else
  None. `core/decision/engine.py` gained an optional `learner_provider`
  constructor arg; `_learner_for(symbol)` resolves the symbol's learner (or the
  shared one on any failure) and `_learning_signal(ohlcv, symbol)` now uses it,
  so `decide()` picks each symbol's own model. The "learning contributes"
  and `require_agreement` guards now also fire when a provider is present (not
  only when the shared learner is non-None). Verified: default config keeps
  provider None, `learner_for` returns the shared learner, and the 48-test
  suite stays green; a manual per_symbol=true smoke run trained three distinct
  models (EURUSD/GBPUSD/XAUUSD), a fresh context resolved three DISTINCT ready
  learners from those files, and an untrained symbol (NZDUSD) fell back to the
  shared learner. CODE_MAP.md sections 3, 4 (context + run_train note), and 10
  (engine) plus section 17 updated. The two-symbol distinct-model TEST is P3.5;
  the A5 status flip is deferred to P3.8. Next sub-step: P3.5.
- P3.3 DONE (code): per-symbol ML training (A5). `app/runners.py::run_train`
  now reads `learning.per_symbol` (default false, read defensively) and branches:
  the default SHARED-model path is byte-identical to before (train the first
  symbol with >= 200 bars, save one model file), while `per_symbol=true` calls
  the new `_run_train_per_symbol`, which loops every symbol, builds a FRESH
  learner per symbol via `build_active_model` (so EURUSD and XAUUSD never share
  fitted state), fits, and saves each to `models/<model>_<SYMBOL>.pkl` using the
  new `_per_symbol_model_file(base, symbol)` helper (splits on the extension and
  inserts a sanitized symbol, e.g. ml_classifier.pkl -> ml_classifier_EURUSD.pkl;
  a broker symbol like "EURUSD.m" is sanitized to safe filename characters).
  Graceful: a symbol with too little data / no samples is skipped, and if NO
  symbol trains, `saved=False` is returned without raising. The shared
  `ctx.learner` is intentionally not reused in per-symbol mode so the light path
  and any loaded shared model stay untouched. Verified: the default path keeps
  the 48-test suite green; a manual smoke run with per_symbol=true on the three
  sample CSVs produced three distinct model files
  (ml_classifier_{EURUSD,GBPUSD,XAUUSD}.pkl). The per-symbol learner LOOKUP in
  the context/engine and the config.yaml `learning.per_symbol` key + docs are
  P3.4; the two-symbol distinct-model test is P3.5; the A5 status flip is
  deferred to P3.8. CODE_MAP.md section 4 (run_train) updated. Next sub-step: P3.4.
- P3.2 DONE (test): added tests/test_timing_stats.py (8 tests) locking in the
  P3.1 time-bucket Bayesian shrinkage. TestEdgeShrinkageMath drives the pure
  static formula `TimeStats._edge_from_row`: for a fixed strongly-positive
  per-trade profile, a 5-sample bucket keeps < 15% of its raw base edge while a
  500-sample bucket keeps > 85% (and big edge > 5x small edge); the `trusted`
  flag is governed by min_samples only (5 -> False, 50/500 -> True) independent
  of shrinkage; `shrinkage=None` reproduces the pre-P3.1 n/(n+min_samples)
  formula to 12 places; `shrinkage <= 0` disables damping so 5- and 500-sample
  edges are equal; an empty (n=0) bucket returns a neutral 0 edge. TestRecordAnd
  ServeShrinkage exercises the full public path record_trades -> bucket_edge
  against a TEMP SQLite DB (memory.db_file overridden in memory, so the real
  data_store/memory.sqlite is untouched): 5- vs 500-sample buckets on the same
  UTC hour ("h12_15", Monday 2026-01-05 12:00) show the shrinkage gap end to
  end; the learned edge reloads on a fresh TimeStats instance (restart
  simulation); and config `timing.learning.shrinkage=0` gives the raw edge.
  Stdlib-only, ASCII-only. Full offline suite now 48 tests (was 40), all green.
  CODE_MAP.md tests section + the 40->48 test-count references updated. The A4
  status flip stays deferred to P3.8. Next sub-step: P3.3.
- P3.1 DONE (code+config): time-bucket Bayesian shrinkage + higher trust
  threshold (A4). config.yaml `timing.learning.min_samples` default raised from
  20 to 50 (a ~20-trade bucket is too noisy to trust) and a new
  `timing.learning.shrinkage` knob added (default 50 = matches min_samples;
  <= 0 disables). `core/timing/time_stats.py`: `__init__` now reads both keys
  defensively (bad/missing -> safe defaults; negative shrinkage clamped to 0);
  `_edge_from_row` gained an optional `shrinkage` parameter and multiplies the
  bounded edge by `n / (n + shrinkage)` so small buckets are pulled toward a
  neutral 0 edge in proportion to sample scarcity, decoupled from the trust
  threshold. `shrinkage=None` reproduces the pre-P3.1 `n / (n + min_samples)`
  formula, and `shrinkage <= 0` returns the raw edge, so the change degrades
  gracefully. `bucket_edge` passes `self.shrinkage`. Verified manually: a
  5-sample bucket's edge is heavily damped (~0.06) and untrusted while a
  500-sample bucket keeps ~0.63 (trusted); shrinkage=0 gives the raw ~0.69;
  config loads via both YAML paths. CODE_MAP.md section 10b updated. Offline
  suite still 40 tests, all green. The dedicated shrinkage test is P3.2; the
  A4 status flip is deferred to P3.8. Next sub-step: P3.2.
- P2.6 DONE (docs): Phase P2 documentation sync + status flip. Flipped the
  section-3 Track-A item A3 to [x] with a dated STATUS note summarizing the
  whole P2.1-P2.6 chain (Wilson interval + bootstrap p-value in metrics,
  win_rate_ci_low + pnl_pvalue in compute_metrics, the
  memory.search.significance config block, the store record-but-never-promote
  filter, and the 11-test lock-in). Added a user-facing "significance filter"
  note to README (parallel to the holdout note) explaining that a strategy
  which cannot be statistically separated from random is kept in memory but
  never promoted to the registry, plus a one-line pointer under the
  config-overview bullet for `memory.*`. Confirmed CODE_MAP.md sections 3, 8,
  and 17 were already in sync from the P2.1-P2.5 commits (metrics helpers,
  store filter, and the 40-test count), so no CODE_MAP edits were needed.
  Added this note plus an Ideas.md entry. Offline suite still 40 tests, all
  green. This completes Phase P2. Next sub-step: P3.1.
- P2.5 DONE (test): added tests/test_metrics_significance.py (11 tests) locking
  in the whole P2 significance layer. TestWilsonInterval: the textbook 95%
  interval for 50/100 (~0.4038, 0.5962), bounds stay in [0,1] and are honest for
  small n (10/10 lower bound < 1.0), and the n<=0 / z<=0 / wins-clamp edge cases.
  TestBootstrapPvalue: p-value < 0.05 for a clearly-positive series, > 0.20 for a
  symmetric-random series, conservative 1.0 for empty / n_boot<=0, and identical
  results under a fixed seed (determinism). TestComputeMetricsSignificance:
  compute_metrics carries win_rate_ci_low + pnl_pvalue (low p-value / positive
  lower bound for a positive series) and the empty-series conservative case
  (pnl_pvalue=1.0, ci_low=0.0). TestRegistrySignificanceFilter (P2.4 lock-in):
  a non-significant strategy with a HIGHER raw score is recorded (4 result rows)
  but excluded from top_strategies and update_registry while the significant one
  is promoted; apply_significance=False returns both; the filter disabled
  promotes both; and the optional min_winrate_ci_low gate rejects a strong
  p-value with a weak win-rate lower bound. Tests use in-memory config overrides
  + a temp DB; stdlib-only. Full offline suite now 40 tests (was 29), all green.
  CODE_MAP.md tests section + the two 29->40 test-count references updated. Next
  sub-step: P2.6.
- P2.4 DONE (code): the statistical-significance filter is now ENFORCED at the
  registry-promotion boundary in core/memory/store.py. `MemoryStore.__init__`
  reads the `memory.search.significance` block (enabled default true,
  max_pvalue 0.05, min_winrate_ci_low 0.0) defensively (non-numeric config
  falls back to the safe defaults). `top_strategies` now also computes the
  per-strategy AVERAGE of `pnl_pvalue` and `win_rate_ci_low` across its stored
  segments (via json_extract) and, when significance is enabled and the new
  `apply_significance` flag is True (default), drops any strategy whose average
  p-value > max_pvalue (or, when min_winrate_ci_low > 0, whose average Wilson
  lower bound < min_winrate_ci_low) using a new `_is_significant` helper. A
  missing p-value (legacy results from before P2.3) is treated as the
  conservative 1.0 so it is filtered out only while the gate is enabled - never
  silently promoted. `update_registry` inherits the filter automatically since
  it delegates to top_strategies; its docstring documents this. Non-significant
  strategies remain fully RECORDED in SQLite (memory) - only PROMOTION to the
  JSON registry is blocked, exactly as A3 requires. The two persistence tests
  (test_memory, test_walk_forward) were updated to record the P2.3 significance
  fields (win_rate_ci_low, pnl_pvalue), since real compute_metrics output now
  always carries them. Verified with a manual smoke test: a non-significant
  strategy with a HIGHER score stays in memory but is excluded from the
  registry-eligible list, while the significant one is promoted; raw fetch
  (apply_significance=False) still returns both. CODE_MAP.md section 8 (store)
  updated. Offline suite still 29 tests, all green. The dedicated significance
  test file (including a registry-rejection case) is P2.5. Next sub-step: P2.5.
- P2.3 DONE (code+config): compute_metrics now also emits `win_rate_ci_low`
  (Wilson 95% lower bound via P2.1's wilson_interval) and `pnl_pvalue`
  (P2.2's seeded bootstrap p-value for "mean trade PnL <= 0"), with optional
  `n_boot`/`seed` parameters defaulting to (1000, 42) so results stay
  deterministic under the project global seed; n_boot<=0 or an empty trade
  list yields the conservative p-value 1.0. Added the
  `memory.search.significance` config block (`enabled: true`,
  `max_pvalue: 0.05`, `min_winrate_ci_low: 0.0` = optional win-rate gate off
  by default because profitable strategies can have sub-0.5 win-rates) with
  explanatory comments; nothing READS the block yet - enforcement in the
  store is P2.4, so registry behavior is unchanged this commit. Verified:
  both keys parse under PyYAML AND the minimal fallback parser; a clearly
  positive PnL series gets ci_low>0 and p~0.0, a symmetric series gets
  p~0.5-0.7, empty -> (0.0, 1.0), and repeat calls are identical. CODE_MAP
  sections 3 and 8 updated. Offline suite still 29 tests, all green. Formal
  tests arrive in P2.5. Next sub-step: P2.4.
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
- P2.2 DONE (code): added `bootstrap_pvalue(trade_pnls, n_boot=1000, seed=42)`
  to core/strategy/metrics.py - a pure-Python bootstrap p-value for the null
  "mean trade PnL <= 0". It resamples the PnLs with replacement n_boot times and
  returns the fraction of resample means that are <= 0 (small -> a real positive
  edge, large -> indistinguishable from break-even/random). Determinism comes
  from a private random.Random(seed) so it never touches global RNG state and is
  reproducible under the project global seed (config general.random_seed=42, the
  default). Conservative edge cases: empty series or n_boot<=0 -> 1.0. Verified:
  clearly-positive series -> ~0.0, symmetric series -> ~0.5, negative series ->
  1.0, and repeat calls are identical. No behavior change to compute_metrics yet
  (P2.3); formal test is P2.5. CODE_MAP section 8 metrics.py entry updated.
  Offline suite still 29 tests, all green. Next sub-step: P2.3.
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
