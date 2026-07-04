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

- [ ] **A1. Export multi-year real history and run a long search (do FIRST).**
  Use `scripts/export_history.py` on Windows with MT5 open to export several
  years of real bars per symbol into `data_store/history/<SYM>_<TF>.csv`, then
  run `python main.py --mode search` over that real data. This is the single
  most important step; everything else builds on having enough real samples.
  Files: `scripts/export_history.py`, `app/runners.py::run_search`,
  `core/strategy/search.py`. No code change strictly required to START, but
  document the recommended multi-year workflow in README.

- [ ] **A2. More walk-forward segments + a locked holdout.**
  Lower `train_bars` and raise the segment count to 6-10, and reserve a final
  "quarantine"/holdout period that the search NEVER sees. A strategy is only
  promoted after it also passes on the untouched holdout.
  Files: `config/config.yaml` (`memory.walk_forward`, add `holdout_bars` /
  `min_segments`), `core/strategy/walk_forward.py` (`segments()` +
  holdout-aware `evaluate()`), `app/runners.py`.

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

## 5. Execution order (recommended)

1. **A1** - export multi-year real data + long search (prerequisite for all).
2. **A2, A3** - more segments + holdout + significance filter (kills overfitting).
3. **A4** - time-bucket min_samples + Bayesian shrinkage.
4. **A5, A6** - per-symbol model + weekend swap/gap realism.
5. **A7** - CI to lock in the gains.
6. **B1 + B3** - live council + decay monitor (offline learner -> living system).
7. **B8, B2** - recency weighting, then time x regime matrix.
8. **B7** - evolutionary search.
9. **B4, B6, B5** - overnight training, weekly journal, contrarian sensor.

Rationale: statistics first (Track A) so later adaptive ideas (Track B) learn
from trustworthy signal, not noise.

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

- Created `structure.md`: recorded the as-built structure snapshot and captured
  the expert-AI review as a prioritized two-track roadmap - Track A (statistical
  robustness: multi-year real data, more walk-forward segments + holdout,
  Wilson/bootstrap significance filter, time-bucket shrinkage, per-symbol ML,
  weekend swap/gap, CI) and Track B (living-bot ideas: strategy council, time x
  regime matrix, strategy-decay monitor, overnight training, contrarian sensor,
  weekly journal, evolutionary search, recency weighting). No source code
  changed yet; this file defines HOW the project continues from here.
