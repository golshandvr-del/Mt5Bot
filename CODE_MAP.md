# CODE_MAP.md - MT5 Smart Trading Bot

> READ THIS FILE FIRST. It is the single source of truth for the project's
> structure, every module's purpose, the data flow, and how the four phases
> connect. A future AI (or human) should be able to understand the whole
> project from this file alone, without re-reading all the source code.
>
> RULE: Keep this file in sync with the code on EVERY change. If it drifts from
> reality, fix it immediately. Standard ASCII English only, everywhere.
>
> COMPANION DOCS: `structure.md` holds the forward-looking STRUCTURAL ROADMAP
> (prioritized next steps from an expert-AI review, mapped to files); `Ideas.md`
> is the idea backlog log; `README.md` is the user guide. This CODE_MAP describes
> WHAT exists now; structure.md describes HOW the project should evolve next.
> Keep all four in sync.

---

## 1. What this project is

A modular, config-driven MetaTrader 5 (MT5) trading bot for **Windows 7 (64-bit),
CPU-only, mid-range hardware**. Architecture is **"train offline / run light"**:
heavy machine learning is optional and isolated; the live decision + execution
path is lightweight and pure-Python-friendly.

> NOTE ON REPO LAYOUT: the project lives at the REPOSITORY ROOT (moved from the
> earlier `main/` subfolder in commit `0c1cfd6 Restructure: move all project
> files from main/ to repository root`). The only file outside the app tree is
> the GitHub Actions CI workflow (`.github/workflows/ci.yml`), because GitHub
> only recognizes workflows under `.github/workflows/`. That workflow runs the
> offline test suite from the repo root (no `working-directory` needed); it ships
> nothing to the Windows 7 runtime and adds no dependency.

Four decoupled phases:

- **Phase 1 - Learning core**: swappable learners (ML, RL, DL, transfer,
  self-supervised) behind ONE interface. Heavy ones are optional/off by default.
- **Phase 2 - Indicators/tools**: a large, pluggable technical-indicator library
  (trend, momentum, volatility, volume, patterns).
- **Phase 3 - Memory & self-improvement (realistic)**: strategy/parameter SEARCH
  + WALK-FORWARD backtesting. Results persist to SQLite + a JSON registry; the
  bot auto-selects/blends the best strategies. NO literal source self-rewriting.
- **Phase 4 - News analysis**: fetch + sentiment-score market news, produce a
  per-symbol news signal fed into the decision layer. Degrades gracefully offline.

The layers are kept separate: data, indicators, strategy/learning, memory,
news, decision, execution, config.

---

## 2. Top-level layout

```
<repo root>                       <- the ENTIRE project lives here (moved from
                                     the old main/ subfolder in commit 0c1cfd6)
  install.bat                     <- ONE-CLICK Windows 7 automatic installer
  main.py                         <- CLI entry point; dispatches run modes
  requirements.txt                <- dependencies pinned for Win7 / Python 3.8
  CODE_MAP.md                     <- THIS FILE (always keep current)
  structure.md                    <- structure snapshot + prioritized ROADMAP
  Ideas.md                        <- idea backlog log (write idea before change)
  README.md                       <- user-facing guide (install/run/backtest/VPS)

  config/
    config.yaml                   <- MASTER config; controls the whole bot
    loader.py                     <- YAML loader + dot-access + fallback parser
    __init__.py

  app/
    context.py                    <- BotContext: builds/caches all components
    runners.py                    <- one function per mode (train/search/etc.)
    __init__.py

  core/
    data/
      mt5_connector.py            <- thin, defensive MetaTrader5 wrapper
      data_feed.py                <- OHLCV container + live/CSV data source
    indicators/                   <- Phase 2 (pluggable)
      base.py                     <- Indicator base class + math helpers
      registry.py                 <- register/build enabled indicators
      trend.py momentum.py volatility.py volume.py patterns.py extra.py
      __init__.py                 <- imports submodules to trigger registration
    learning/                     <- Phase 1 (swappable learners)
      base_model.py               <- BaseModel common interface
      factory.py                  <- build learner by name (+ NeutralModel)
      features.py                 <- FeatureBuilder (OHLCV -> X, y)
      calibration.py              <- probability calibration helpers
      ml_classifier.py            <- LightGBM / sklearn / pure-Python (DEFAULT)
      rl_agent.py                 <- tabular Q-learning (CPU-light, optional)
      dl_classifier.py            <- Keras MLP (HEAVY, off by default)
      transfer.py                 <- transfer learning on DL model (optional)
      self_supervised.py          <- autoencoder feature learner (optional)
    strategy/                     <- Phase 3 (search/backtest) + Track-B living-bot
      strategy.py                 <- StrategySpec (recipe) + Strategy (executable)
      metrics.py                  <- performance metrics + ranking value
      backtester.py               <- fast bar-by-bar single-position simulator
      walk_forward.py             <- rolling out-of-sample eval (+ recency weight)
      search.py                   <- random/grid/evolution strategy search (memory builder)
      council.py                  <- P5.1 StrategyCouncil: live credibility (UCB1)
      decay_monitor.py            <- P5.5 DecayMonitor: statistical strategy expiry
    memory/
      store.py                    <- SQLite + JSON registry of strategies/results
    news/                         <- Phase 4
      base.py                     <- NewsItem, SentimentBackend, NewsSource ABCs
      sentiment.py                <- lexicon (offline) + optional VADER backends
      sources.py                  <- RSS (stdlib) + optional NewsAPI sources
      aggregator.py               <- NewsAnalyzer: fetch/score/cache/aggregate
    timing/                       <- Phase 5 (user-update-request): time/season awareness
      session.py                  <- SessionCalendar + TimeContext (session/day/season)
      time_stats.py               <- TimeStats: learned per-bucket edge (persisted)
      time_context.py             <- TimeContextProvider: combine buckets -> TimeSignal
      __init__.py                 <- exports the timing public API
    decision/
      engine.py                   <- DecisionEngine: fuse all signals -> Decision
                                     (optionally gated/sized by the timing layer)
    execution/
      risk_manager.py             <- position sizing + risk limits
      order_manager.py            <- Decision -> MT5 order (paper logs / live sends)
    utils/
      logger.py                   <- rotating file + console logging setup
      helpers.py                  <- seeds, JSON I/O, timeframe mapping, math

  installer/
    install_helper.py             <- pip install w/ retries + verify + sample data
    install_vcredist.ps1          <- auto-install VC++ x64 runtime if missing

  scripts/
    run_bot.bat                   <- run launcher (finds Python, runs main.py)
    export_history.py             <- export live MT5 history to CSV for offline use
    export_strategy_for_ea.py     <- flatten best learned strategy -> EA .params

  experts/                        <- native MT5 Strategy Tester validation
    Mt5SmartBotEA.mq5             <- Expert Advisor replaying the learned blend
    README_EA.md                  <- install/compile/run guide for the EA
    params/<SYM>_<TF>.params      <- generated per-symbol EA parameter files

  tests/                          <- offline, stdlib-only test suite
    __init__.py helpers.py run_all.py
    test_config.py test_indicators.py test_learning.py
    test_memory.py test_news.py test_pipeline.py
    test_metrics_significance.py test_walk_forward.py test_timing_stats.py
    test_per_symbol_learning.py test_backtester_swap_gap.py
    test_strategy_council.py test_decay_monitor.py

  examples/
    generate_sample_data.py       <- synthetic OHLCV CSVs for offline first run

  data_store/                     <- persistent state (survives restarts)
    history/<SYMBOL>_<TF>.csv      <- OHLCV history (exported or synthetic)
    memory.sqlite                 <- Phase 3 strategy/result database
    strategy_registry.json        <- human-readable best-strategy registry
    news_cache/news_cache.json    <- cached scored news items

  models/                         <- trained model artifacts (e.g. ml_classifier.pkl)
  backtests/                      <- backtest_report.json outputs
  logs/                           <- mt5_bot.log (rotating)
```

---

## 3. Configuration (config/config.yaml + config/loader.py)

`config.yaml` is the ONLY place to enable/disable features and tune parameters.
Top-level sections:

- `general`  : mode (live/paper/backtest/search/train), `enable_heavy_compute`,
  `random_seed`, timezone.
- `logging`  : level, file logging, rotation.
- `mt5`      : connection (terminal_path/login/password/server), `symbols`,
  `timeframe`, `timeframes`, `history_bars`.
- `risk`     : `risk_per_trade`, `max_open_positions`, `max_daily_loss`, default
  SL/TP ATR multiples, min/max lot, deviation, magic number.
- `indicators`: per-indicator `{enabled, params}` toggles (Phase 2).
- `learning` : `active_model`, `per_symbol` (A5 / P3.4; default false = one
  shared model, true = per-symbol models trained + used per symbol), and
  per-learner config blocks (Phase 1).
- `memory`   : db/registry files, `walk_forward` windows (train/test/step_bars
  plus `min_segments` and `holdout_bars` for statistical robustness), `search`
  settings incl. the `significance` block (A3 / P2.3: `enabled`, `max_pvalue`,
  `min_winrate_ci_low` - gates registry promotion), `ensemble_top_k` (Phase 3).
- `news`     : enable, degrade_gracefully, cache, sentiment backend, sources,
  `signal_weight`, `blackout_minutes` (Phase 4).
- `decision` : blend `weights` (indicators/learning/news), long/short thresholds,
  `require_agreement`.
- `backtest` : initial balance, cost model, fixed lot, report dir, and the
  A6 / P3.6 weekend-swap + Monday-gap model (`swap_long_pts`, `swap_short_pts`,
  `swap_triple_day`, `model_weekend_gap`; all default to a no-op).

**loader.py**
- `load_config(path)` -> `DotDict`. Uses PyYAML if installed, else a built-in
  minimal YAML-subset parser (`_minimal_yaml_parse`) so the bot boots without
  PyYAML.
- `DotDict`: dict with attribute access + `get_path("a.b.c", default)` dotted
  lookup used everywhere.
- `resolve_path(cfg, rel)`: resolve a config path relative to project root.
- `PROJECT_ROOT` is attached to the loaded config as `cfg["project_root"]`.

---

## 4. Entry points and orchestration

### main.py
CLI launcher. Ensures the project root is importable, parses `--mode`,
`--config`, `--iterations`, `--sleep`. Builds a `BotContext`, resolves the
effective mode (CLI overrides `general.mode`), calls the matching runner in
`app/runners.py`, prints a compact JSON summary.

Modes: `train`, `search`, `backtest`, `paper`, `live`, `loop`.

### app/context.py - `BotContext`
The single assembly point. Loads config, seeds RNGs, sets up logging, then
LAZILY builds and caches shared singletons via properties:
`connector`, `data_feed`, `indicators`, `feature_builder`, `learner`, `memory`,
`news`, `risk`, `orders`, `engine`.
- `connect_mt5()` connects to MT5 if `mt5.enabled` (never fatal).
- `learner` property also tries to `load()` the persisted (shared) model file.
- `learner_for(symbol)` (A5 / P3.4): when `learning.per_symbol` is false
  (default) it returns the shared `learner`; when true it builds+caches ONE
  learner per symbol and loads that symbol's `models/<model>_<SYMBOL>.pkl`
  (via `_per_symbol_model_file`, mirroring runners.py). A missing/failed
  per-symbol file falls back to the shared learner so an untrained symbol never
  crashes. Only wired into the engine (as `learner_provider`) when per_symbol
  is on, so the default light path is byte-identical.
- `shutdown()` closes the MT5 connection.

### app/runners.py - one function per mode
- `run_train(ctx)`  : Phase 1. Two modes selected by `learning.per_symbol`
  (A5 / P3.3, default false). Shared mode (default): build (X, y) via
  FeatureBuilder for the first symbol with enough data, `learner.fit`, and
  persist ONE shared model file (unchanged behavior). Per-symbol mode
  (`learning.per_symbol=true`): loop every symbol, build a FRESH learner each
  via the factory (so their fitted state never mixes), fit, and save each to
  `models/<model>_<SYMBOL>.pkl` (via `_per_symbol_model_file`, which sanitizes
  the symbol and inserts it before the extension) so, e.g., XAUUSD does not
  dilute EURUSD. Degrades gracefully (a symbol with too little data is skipped).
  The engine's per-symbol learner LOOKUP is wired in P3.4 via
  `BotContext.learner_for` + `DecisionEngine.learner_provider`; P3.3 only
  produces the files.
- `run_search(ctx)` : Phase 3. For each symbol, load history and run
  `StrategySearch.run`, persisting results + updating the registry.
- `run_backtest(ctx)`: Phase 3. Backtest the memory-top strategy (or a default
  EMA+RSI spec) per symbol; write `backtests/backtest_report.json`.
- `run_once(ctx)`   : paper/live. Refresh news, then per symbol: load bars,
  `engine.decide`, compute ATR, `orders.execute` (paper logs / live sends).
- `run_loop(ctx,...)`: repeatedly rebuild a fresh context and `run_once`, sleeping
  between passes (VPS use). iterations=0 = forever.
- `dispatch(mode, config_path)`: build a context and run a single mode.

---

## 5. Data layer (core/data)

### data_feed.py
- `OHLCV`: lightweight parallel-list container (time/open/high/low/close/volume),
  oldest-first. Helpers: `append_row`, `from_rows`, `slice`, `to_frame` (pandas
  if present), `to_csv`, `from_csv`. `len(ohlcv)` = number of bars.
- `DataFeed(cfg, connector)`: `get_ohlcv(symbol, tf, count)` tries live MT5 first
  (if connector connected), else loads `data_store/history/<SYM>_<TF>.csv`.
  `export_live_to_csv(...)` pulls live history and writes it to CSV for offline use.

### mt5_connector.py - `MT5Connector`
Thin, defensive wrapper around the `MetaTrader5` package.
- Imports MT5 lazily; if absent (e.g. Linux dev box), runs in OFFLINE mode where
  every method returns safe empty results (never raises on import).
- `connect(raise_on_fail)`, `shutdown()`, `account_info()`, `symbol_info()`,
  `symbol_tick()`, `copy_rates(symbol, tf, count)` -> list of OHLCV dict rows,
  `positions(symbol)`, `order_send(request)`.
- Exposes `ORDER_TYPE_BUY/SELL`, `TRADE_ACTION_DEAL` (None when offline).
- Strategy/learning code must NEVER import MetaTrader5 directly; go through this.

---

## 6. Phase 2 - Indicator layer (core/indicators)

### base.py
- `IndicatorResult(dict)`: output series map with `.last(name)` helper.
- `Indicator`: base class. Subclasses set `name`, `category`, and implement
  `compute(ohlcv) -> IndicatorResult` and (usually) `signal(ohlcv) -> float`
  in [-1, +1]. Class methods `default_params()` and `param_space()` (search).
  Static math helpers: `_sma`, `_ema`, `_rolling_std`, `_true_range`,
  `_wilder_smooth`. All pure Python (no numpy required at runtime).

### registry.py
- `@register_indicator` class decorator -> global `_REGISTRY[name] = cls`.
- `get_indicator_class(name)`, `list_indicators()`.
- `build_enabled_indicators(cfg)`: instantiate only indicators enabled in
  config with their params. `build_all_indicators()`: all with defaults.

### Built-in indicators (name -> category)
- trend.py    : `sma`, `ema`, `macd`, `adx`, `ichimoku`, `supertrend`
- momentum.py : `rsi`, `stoch`, `cci`, `williams_r`, `roc`
- volatility.py: `atr`, `bbands`, `keltner`, `donchian`
- volume.py   : `obv`, `mfi`, `vwap`
- patterns.py : `candle_patterns`

`core/indicators/__init__.py` imports every submodule so all indicators
self-register on `import core.indicators`. To add one: create the class with a
unique `name`, decorate it, import the module in `__init__.py`, add it to
config.yaml -> indicators. No other code changes needed.

---

## 7. Phase 1 - Learning core (core/learning)

### base_model.py - `BaseModel`
Common interface for ALL learners so they are swappable via
`learning.active_model`:
- `fit(X, y)`, `predict_proba_up(x) -> [0,1]`, `predict_signal(x) -> [-1,+1]`
  (default derived from proba), `save(path)`, `load(path)`, `is_ready()`.
- Flags: `available` (set False if an optional backend is missing) and
  `trained`. Missing backends degrade to neutral instead of raising.
- `ModelPrediction` value object (proba_up/signal/label).

### factory.py
- `build_model(cfg, name)`: lazy-imports and builds the named learner.
- `build_active_model(cfg)`: builds `learning.active_model`; if disabled or
  unavailable, returns `NeutralModel` (always predicts 0.5 / 0.0) so the bot
  never breaks.

### features.py - `FeatureBuilder`
Turns OHLCV into learner input. `build_training(ohlcv) -> (X, y, names)` with
labels from a forward horizon vs an ATR-relative threshold (+1/0/-1).
`build_inference_row(ohlcv)` -> single latest feature row. Features = raw price
returns/range/body ratios + one scalar per enabled indicator. Keeps the learning
and indicator layers consistent.

### Learners
- **ml_classifier.py** (DEFAULT, recommended for weak HW): `MLClassifier`.
  Backend preference: LightGBM -> scikit-learn GBDT/HistGBDT -> pure-Python
  logistic regression (`_PurePythonLogReg`). Persists via pickle. `predict_proba_up`
  returns P(label==+1).
- **rl_agent.py** (optional, CPU-light): `RLAgent` tabular Q-learning over a small
  discretized state space; actions flat/long/short. Off by default.
- **dl_classifier.py** (HEAVY, off): `DLClassifier` small Keras/TF MLP. Marks
  itself unavailable if TensorFlow/Keras missing (typical on Win7).
- **transfer.py** (optional, off): `TransferModel` fine-tunes a frozen DL source
  model. Depends on the DL backend; unavailable without it.
- **self_supervised.py** (optional, off): `SelfSupervisedEncoder` autoencoder
  (sklearn MLPRegressor, else pure-Python PCA). Emits a novelty signal, mainly a
  feature-learning aid.

---

## 8. Phase 3 - Strategy, memory & self-improvement (core/strategy + core/memory)

### strategy.py
- `StrategySpec`: serializable recipe = `{indicators:{name:params}, weights,
  long/short thresholds, sl/tp ATR mults, symbol, timeframe, name}`.
  `to_dict/from_dict`, `fingerprint()` (stable sha1 for dedup/keys).
- `Strategy(spec)`: instantiates the spec's indicators once; `blended_signal`
  (weighted [-1,+1]), `decision` (+1/-1/0 via thresholds), `atr_value` for SL/TP.

### meta_label.py - `MetaLabeler` (UPGRADE_PLAN U6.1)
Meta-labeling win-probability VETO gate: does NOT predict direction, it predicts
"given the validated top strategy is about to fire HERE, will that trade win?"
- `_LogReg`: a tiny pure-Python L2-regularized logistic regression (gradient
  descent + feature standardization), `to_dict/from_dict` for JSON persistence.
- `MetaLabeler(cfg)`: holds ONE `_LogReg` per strategy `fingerprint()` in a
  single JSON file (`decision.meta_label.model_file`, atomic temp-file +
  os.replace). Features are regime/context only - `_FEATURE_NAMES` =
  `[signal_mag, atr_pct, adx, hour_sin, hour_cos, dow_sin, dow_cos]` (order is a
  persisted contract). `build_dataset(spec, ohlcv, horizon)` labels each
  historical firing win=forward `horizon`-bar move went the strategy's way;
  `train()` fits + persists (refuses on < `min_train_samples` or single-class);
  `win_probability()` scores the last bar; `should_veto()` returns
  `(veto, p_win)` and vetoes ONLY when enabled AND a trained model exists AND
  `p_win < min_win_prob`. Disabled/untrained/too-few/single-class => never veto.
  Config block `decision.meta_label` (enabled default false). Trained in train
  mode by `app/runners.train_meta_labelers` (one gate per top registry strategy).

### regime_router.py - `RegimeRouter` (UPGRADE_PLAN U6.2)
Regime router: instead of AVERAGING top-K strategies that disagree (a trend
follower + a mean-reverter cancel out exactly in chop), it routes each bar to the
single validated strategy that historically did best IN THE CURRENT REGIME, then
trades it through the normal parity path.
- `RegimeDetector(cfg)`: labels a trailing OHLCV window
  `"<low|mid|high>_<trend|range>"` using an ATR%-like realized-vol proxy
  (`_window_volatility`, bucketed by fitted terciles) plus median ADX (>=
  `memory.search.regime.adx_trend_threshold` -> "trend"). Uses the SAME maths as
  `WalkForward._label_segments` (U4.5) so live labels == validation labels.
  `fit_cutoffs(ohlcv)` learns the low/mid/high vol cutoffs from history;
  `set_cutoffs`/`cutoffs` persist them; `label(ohlcv)` labels the last window,
  `label_series(ohlcv)` gives the per-bar trailing-window labels used to backtest
  the composite identically to production. Config `decision.regime_router`
  (`detect_window` 96, `adx_period` 14). Absent fitted cutoffs -> everything
  "mid" (safe non-discriminating default).
- `RegimeRouter(cfg, memory)`: holds a per-regime champion map (regime ->
  {fingerprint, score, spec}) in ONE JSON file (`champions_file`, default
  `data_store/regime_champions.json`). `train(specs, ohlcv, backtester, point)`
  scores each candidate on ONLY each regime's bars (via `_MaskedRegimeStrategy`,
  which forces decisions/signals flat outside the kept regime) and keeps the best
  per regime, skipping regimes with < `min_bars_per_regime` (default 200) bars.
  `champion_for(regime)`, `champions()`, `is_ready()` (enabled AND has >=1
  champion), `save()`/`load()` (ASCII JSON, atomic). Fully optional
  (`decision.regime_router.enabled`, default OFF); untrained/empty -> routes to
  nobody so the engine falls back to plain parity top-1.
- `RegimeRouterStrategy`: a Strategy-compatible COMPOSITE that, per bar, takes the
  decision/signal of the current regime's champion (flat if none). Handed to the
  Backtester / `scripts/validate_ensemble.py --router` so the router's composite
  is walk-forward scored end-to-end, satisfying the U2.5 rule that no unvalidated
  composite may go live.
- Wiring: `DecisionEngine.__init__(..., regime_router=None)`; in parity mode
  `_route_to_regime_champion(ensemble, ohlcv, reasons)` prefers the current
  regime's champion over top-1 (veto-safe: falls back to top-1 when the router is
  absent/disabled/untrained, no champion for the regime, the champion is
  decay-suspect, or it is not in the ensemble). `BotContext.regime_router` lazily
  builds+loads it only when enabled. `app/runners` train mode (re)builds and
  persists the champion map when enabled (no-op otherwise).

### metrics.py
- `compute_metrics(trade_pnls, equity_curve, n_boot=1000, seed=42)` ->
  num_trades, win_rate, profit_factor, expectancy, net_profit, max_drawdown,
  sharpe (per-trade), average_win/loss, plus the significance fields (A3 /
  P2.3): `win_rate_ci_low` (Wilson 95% lower bound on the win-rate) and
  `pnl_pvalue` (seeded bootstrap p-value that mean trade PnL <= 0; n_boot<=0
  or an empty series -> conservative 1.0). These feed the
  `memory.search.significance` registry filter (P2.4).
- `rank_value(metrics, rank_metric)` -> single comparable score (max_drawdown is
  negated so "higher is better" holds for ranking).
- Statistical significance (A3 / P2.1): `wilson_interval(wins, n, z=1.96)` ->
  pure-Python Wilson score confidence interval (low, high) for the win-rate.
  Stays inside [0, 1] and is honest for small `n`. Edge cases: n<=0 -> (0,0),
  z<=0 -> (p_hat, p_hat), wins clamped to [0, n], bounds clamped to [0, 1].
  Feeds the `win_rate_ci_low` metric (P2.3) and the P2.4 registry filter.
- Statistical significance (A3 / P2.2): `bootstrap_pvalue(trade_pnls,
  n_boot=1000, seed=42)` -> pure-Python bootstrap p-value for H0 "mean trade
  PnL <= 0". Resamples the PnLs with replacement `n_boot` times and returns the
  fraction of resample means that are <= 0 (small = real edge, large = no edge).
  Deterministic via a private `random.Random(seed)` aligned with the project's
  `general.random_seed`. Conservative edge cases (empty / n_boot<=0 -> 1.0).
  Since P2.3 both helpers are wired into `compute_metrics` (see above) and the
  config gained the `memory.search.significance` block (`enabled` default true,
  `max_pvalue` 0.05, `min_winrate_ci_low` 0.0 = optional gate off); the actual
  registry enforcement lands in P2.4.

### backtester.py - `Backtester`
Fast bar-by-bar single-position simulator (NOT the MT5 tester). Enter on signal
change, exit on opposite signal / SL / TP (ATR-based). Applies spread + slippage
+ commission. Produces `BacktestResult(metrics, equity_curve, trade_pnls)`. Used
for RELATIVE ranking during search; final validation happens in the real MT5
Strategy Tester (see README).
- Weekend swap + Monday gap (A6 / P3.6): the simulator can optionally charge an
  overnight swap and model the Monday opening gap so gold/carry-sensitive pairs
  are ranked more realistically. All controlled by the new `backtest` config
  keys and default to a NO-OP (behavior byte-identical when unset). SWAP: for
  every UTC midnight a position is held across, a swap is charged in money =
  `swap_{long,short}_pts * point * contract * fixed_lot`; the `swap_triple_day`
  weekday (0=Mon..6=Sun, MT5 default Wednesday=2) is charged 3x to cover the
  weekend, every other rollover 1x. `_rollovers_between(prev_ts, cur_ts,
  triple_day)` counts the crossed midnights (each midnight billed to the day it
  ENTERS, weekday from the epoch-day index) and `_swap_money` converts nights to
  money; the accrued swap is subtracted from PnL on every close (SL/TP/opposite
  signal AND the residual close). With both swap rates 0.0 (default) no swap is
  applied. GAP: when `model_weekend_gap` is true, a bar that OPENS after a pause
  longer than ~3x the normal bar spacing (`_infer_bar_seconds` via the timeframe
  helper, else the most-common positive timestamp delta) is a "gap bar"; if a
  stop sits inside that gap the fill is at the (worse) OPEN price instead of the
  stop price - a long fills at open when `open < stop`, a short at open when
  `open > stop`. Config is read defensively (`_cfg_float`/`_cfg_int`/`_cfg_bool`
  fall back to safe defaults on bad values).
- Phase U3 pessimistic/realistic execution (fixes diagnosis D3): every knob
  DEFAULTS to the realistic (pessimistic) behavior; the legacy optimistic path
  stays reachable for sensitivity studies.
  - U3.1 `backtest.fill_policy` (`next_open` default | `signal_close`): a
    directional decision no longer fills same-bar. `pending_entry`/
    `pending_flip_exit` queue the entry / signal-flip exit and it fills at the
    NEXT bar's OPEN with an adverse `(0.5*spread + slippage) * point` shift
    (buy pays up, sell gets down) so next_open is never a better fill. SL/TP
    still fill intrabar in both modes.
  - U3.2 `backtest.intrabar_policy` (`pessimistic` default | `optimistic` |
    `midpoint`): `_resolve_ambiguous()` decides which side wins when one bar
    touches BOTH SL and TP - pessimistic books the stop first (both
    directions), optimistic the take, midpoint the average.
  - U3.3 `backtest.spread_model` (`{base_points, rollover_mult,
    rollover_hours_utc, news_mult}`): `_spread_points_at(ts)` widens the spread
    during the rollover window (`_cfg_hours` expands a `[start,end]` window,
    wrapping midnight). Absent sub-block => flat `spread_points` (byte-identical
    old cost). Cost is charged at the ENTRY bar's spread via `_entry_cost_parts`.
  - U3.4 `backtest.sizing` (`risk_pct` default | `fixed_lot`): `_sized_lot()`
    reuses the RiskManager formula (`risk.risk_per_trade` of simulated equity /
    stop-distance, clamped to `risk.min_lot`/`risk.max_lot`) so lots vary per
    trade and the backtest/live curves share geometry; swap now scales by the
    actual `lot`. The `risk.max_daily_loss` circuit breaker is enforced per UTC
    day (`use_breaker`/`breaker_tripped`): once a day's realized loss crosses
    the limit no NEW entries open that day (open positions still exit).
  - U3.5 `backtest.min_stop_points` (0 = off): `_stop_ok()` rejects entries
    whose SL sits closer than the broker minimum stop distance, exactly as the
    MT5 tester rejects them.

### walk_forward.py - `WalkForward`
Splits history into rolling (train, test) windows from `memory.walk_forward`.
`segments(n)` yields index windows; `evaluate(spec, ohlcv, persist)` backtests
each out-of-sample test segment, optionally recording each result in the memory
store, and returns aggregate avg_score/avg_trades. Falls back to a 70/30 split if
history is too short.
- Statistical robustness (A2 / P1.3): `effective_train_bars(n)` auto-SHRINKS the
  train window (never below a floor = max(test_bars, 200), never grown) so a long
  history is split into at least `memory.walk_forward.min_segments` (clamped to
  1..10, default 6) rolling out-of-sample windows instead of only ~2. When the
  configured train window already yields enough segments, or when history is too
  short to hit min_segments above the floor, the original behavior is preserved.
- Locked holdout (A2 / P1.4): `memory.walk_forward.holdout_bars` (default 0 = OFF)
  reserves the FINAL N bars as a "quarantine" the search NEVER sees.
  `searchable_bars(n) = n - holdout_bars` bounds `segments()` and the 70/30
  fallback, so no train/test window ever touches the holdout tail.
  `evaluate_holdout(spec, ohlcv, point=None)` backtests a spec on just that tail
  and returns `{enabled, passed, score, metrics, holdout_bars, holdout_trades}`;
  `passed` requires num_trades >= `memory.search.min_trades` AND a non-negative
  rank score. With holdout_bars <= 0 the gate is a no-op (enabled=False,
  passed=True) and behavior is byte-identical to before.

### search.py - `StrategySearch`
The "learn from trial-and-error" loop / memory builder.
- `run(ohlcv, symbol, tf)`: generate up to `max_trials` specs (random sampling of
  indicator params/weights/thresholds, or a bounded grid), dedup by fingerprint,
  evaluate each via `WalkForward` (persisting results), then call
  `memory.update_registry` to refresh the top strategies. Returns a summary.
  Locked holdout gate (A2 / P1.4): when `memory.walk_forward.holdout_bars > 0`,
  each evaluated spec is also run through `WalkForward.evaluate_holdout`; only
  fingerprints that pass are passed as an `allowed_fingerprints` allowlist to
  `update_registry`, so a spec that only worked in-sample is never promoted.
  When the holdout is OFF the allowlist is None and promotion is unchanged.
- EA-compatible search (UPGRADE_PLAN U2.2): when
  `memory.search.ea_compatible_only` is true (default false), the directional
  voter pool is filtered to `_EA_SUPPORTED_DIRECTIONAL` (ema, sma, rsi, macd,
  adx - the directional subset of the exporter's `EA_SUPPORTED_INDICATORS`).
  Every generated spec then exports to the MQL5 EA 1:1 with no dropped
  indicators, so it is the RECOMMENDED mode for MT5-tester-validated workflows.
  The `_available_directional()` helper applies the filter; the grid path
  already uses only ema+rsi so it is naturally compatible.
- Evolutionary search (UPGRADE_PLAN U4.2, supersedes structure.md P6.5): when
  `memory.search.method: evolution`, `run()` delegates to `_run_evolution()`.
  Generation 0 is fresh random; each later generation keeps an elite pool (top
  `memory.search.evolution.elite_fraction`, default 0.10 of specs seen so far)
  and breeds `evolution.mutate_fraction` (default 0.60) of the next batch from
  those elites, leaving ~40% fresh random for exploration. Pure-Python,
  CPU-light operators: `_mutate(parent)` jitters ONE indicator param by a single
  `param_space` step / swaps one indicator / nudges long|short thresholds by
  +/-0.05; `_crossover(a, b)` unions two elites' indicator sets and averages the
  weights of shared indicators; `_breed_from_elites()` randomly picks mutate vs
  crossover. `_jitter_params()` does the single-step param nudge. Every produced
  spec is re-validated against the live indicator pool and deduped by
  `spec.fingerprint()` (so nothing is evaluated twice and the elite pool
  converges), and evolution respects `ea_compatible_only` (U2.2) so a deep run
  stays 1:1 exportable. Knobs live under `memory.search.evolution.*`; the
  random/grid methods are unchanged.
- Multi-seed stability gate (UPGRADE_PLAN U4.3): when
  `memory.search.stability.enabled` is true (default false), any candidate that
  would ENTER the registry is re-run `stability.n_seeds` (default 3) extra times,
  each with a different bootstrap seed AND a warmup jittered by +/-
  `stability.warmup_jitter` bars (default 20, floored at 20); promotion requires
  the walk-forward rank score to stay STRICTLY positive in EVERY run
  (`require_all_positive`, default true). Implemented by
  `_passes_stability_gate(spec, ohlcv, point)` (re-runs use `persist=False` so
  memory is never polluted; the per-spec jitter offsets are seeded from the
  fingerprint so the gate is reproducible). It hooks into `_eval_one`: a spec is
  added to the `allowed_fps` promotion allowlist only after clearing BOTH the
  holdout gate (when on) AND the stability gate (when on). Because only specs
  that already scored > 0 and passed the holdout reach the re-runs, "only
  finalists pay the cost." When both holdout and stability are off, `allowed_fps`
  stays None and promotion is unfiltered (legacy behavior). `WalkForward.evaluate`
  gained a `warmup` arg so the gate can slide the warmup offset.
- Parameter-neighborhood robustness gate (UPGRADE_PLAN U4.4): when
  `memory.search.neighborhood.enabled` is true (default false), every
  base-positive finalist is additionally re-scored across up to
  `neighborhood.n_neighbors` (default 8) NEIGHBOR specs. `_neighbor_specs(spec)`
  builds them deterministically: for each indicator (sorted) and each of its
  params (sorted), it nudges the value one step (+/-1) within that indicator's
  `param_space`, skipping out-of-range and parent-identical/duplicate
  fingerprints. `_neighborhood_score(spec, ohlcv, point)` returns the MEDIAN of
  the neighbors' walk-forward `avg_score` (all neighbor evals use `persist=False`
  so memory stays clean); it returns None when the gate is off or the spec has no
  perturbable neighbor, so callers fall back to the own score. `_eval_one` then
  records `score_overrides[fp] = min(own_score, neighborhood_score)` and passes
  `score_overrides` into `update_registry`, so the registry RANKS by the robust
  minimum. A knife-edge strategy whose neighbors score poorly is demoted/dropped
  (overfit by definition). Gate off => `score_overrides` empty => ranking is
  byte-identical to before.
- Regime-sliced validation gate (UPGRADE_PLAN U4.5): when
  `memory.search.regime.enabled` is true (default false), `evaluate()` labels
  every walk-forward test segment by regime and attaches three fields to its
  result dict: `regime_labels`, `regime_scores`, `passes_regime_floor`.
  `_label_segments(test_slices)` builds a `<voltercile>_<trend|range>` label per
  segment: `_segment_volatility` (mean (high-low)/close, an ATR%-like proxy)
  bucketed via `_terciles`/`_vol_tercile` into low/mid/high at the run's own
  p33/p66, combined with `_segment_trend_strength` (median ADX) thresholded at
  `regime.adx_trend_threshold` (default 25). `_regime_scores(labels, scores)`
  averages the per-segment rank scores within each regime holding
  `>= regime.min_segments_per_regime` segments. `passes_regime_floor(overall,
  regime_scores)` fails any strategy whose worst gated-regime score falls below
  `regime.floor_mult * overall` (default -0.5). `StrategySearch.run` treats this
  as a promotion filter alongside holdout+stability (adds `regime` to the gate
  list and the allowlist), so a regime-fragile spec is recorded but never
  promoted. Gate off => no regime fields attached => search path unchanged.

### council.py - `StrategyCouncil` (Phase 5 / P5.1, Track B / B1)
A pure-stdlib tabular UCB1 bandit that learns a LIVE per-strategy credibility
from each strategy's own recent realized trade outcomes.
- `ArmStats(window)`: one strategy's rolling window (default 30) of normalized
  rewards (a `deque`) plus a `total_seen` counter; `to_dict/load_dict` for
  persistence. `mean_reward` is the exploit term.
- `StrategyCouncil(cfg)`: reads `decision.council.*` (window, min_trades,
  exploration_c, default/min/max weight, reward_scale). `record_outcome(fp, pnl)`
  normalizes the trade into [0,1] (SIGN only when reward_scale=0 -> win=1/loss=0,
  currency-independent) and appends it. `weight(fp)` maps the arm's mean reward
  onto `[min_weight, max_weight]` around a neutral 1.0 anchor, and applies the
  UCB exploration term ONLY as a one-sided ANTI-BURIAL floor on the losing side
  (so a young, low-sample arm is damped less than a well-sampled one; winners are
  never inflated by exploration). Unknown / still-warming-up arms (n < min_trades)
  return the neutral `default_weight`. Also `weights(fps)`, `credibility(fp)`,
  `arm_summary(fp)`, and `to_dict/load_dict` (used by MemoryStore.save/load_council).
  Consumed by `DecisionEngine` (P5.3) only when `decision.council.enabled` is true.

### memory/store.py - `MemoryStore` (persistence)
SQLite DB (`data_store/memory.sqlite`) + JSON registry
(`data_store/strategy_registry.json`). Survives restarts.
- Tables: `strategies(fingerprint PK, symbol, timeframe, spec_json, created_at)`,
  `results(id, fingerprint, symbol, timeframe, segment, rank_metric,
  metrics_json, score, created_at)`, and (Phase 5 / P5.2)
  `council(fingerprint PK, rewards_json, total_seen, updated_at)` for the live
  strategy-council credibility.
- Live strategy council persistence (Phase 5 / P5.2): `save_council(council)`
  snapshots each arm's rolling reward window into the `council` table
  (INSERT OR REPLACE keyed on fingerprint), and `load_council(council)` restores
  it via the council's `to_dict()`/`load_dict()` hooks. Both are wrapped in
  try/except (log + no-op on any failure) so a DB problem never crashes the live
  decision path; a missing table / empty DB simply leaves the council cold.
- `record_strategy`, `record_result`, `top_strategies(...)` (averages score
  across walk-forward segments, filtered by min avg trades; optional
  `allowed_fingerprints` allowlist restricts promotion to holdout-passing specs,
  A2 / P1.4), `update_registry(...)` (writes top-K per symbol|timeframe; forwards
  `allowed_fingerprints`), `load_registry_top(...)` (fast read for the decision
  engine), `stats()`.
- Statistical-significance registry filter (A3 / P2.4): `__init__` reads
  `memory.search.significance` (`enabled` default true, `max_pvalue` 0.05,
  `min_winrate_ci_low` 0.0) defensively. `top_strategies` now also averages the
  per-result `pnl_pvalue` and `win_rate_ci_low` (P2.3) across a strategy's
  segments (via `json_extract`) and, via the `_is_significant` helper, drops any
  strategy whose average p-value > `max_pvalue` (or, when `min_winrate_ci_low`
  > 0, whose average Wilson lower bound < it) when the filter is enabled and the
  new `apply_significance` flag is True (default). A missing p-value (legacy
  results predating P2.3) is treated as the conservative 1.0. `update_registry`
  inherits the filter since it delegates to `top_strategies`, so a strategy that
  cannot be separated from random is still RECORDED in SQLite but is never
  PROMOTED to the JSON registry. `apply_significance=False` fetches the raw
  ranking.

The more the bot searches, the richer this memory; strategy SELECTION improves
over time. It does NOT rewrite its own source code.

---

## 9. Phase 4 - News layer (core/news)

### base.py
- `NewsItem` (title/summary/link/published_ts, plus assigned score/symbols;
  `text()`, `to_dict/from_dict`).
- `SentimentResult`, `SentimentBackend` ABC (`score(text) -> SentimentResult`).
- `NewsSource` ABC (`fetch() -> [NewsItem]`).
- (Note: an unused `NewsAnalyzer` stub name also appears here; the real analyzer
  is in aggregator.py.)

### sentiment.py
- `LexiconSentiment` (DEFAULT): offline word-list scorer, NO dependencies.
- `VaderSentiment` (optional): uses `vaderSentiment` if installed.
- `build_sentiment(cfg)` picks the backend from `news.sentiment_backend`.

### sources.py
- `RssSource` (DEFAULT): fetches/parses RSS/Atom with stdlib urllib + regex; short
  timeout; returns [] on any failure (graceful).
- `NewsApiSource` (optional): newsapi.org via HTTPS GET when an api_key is set.
- `build_sources(cfg)` builds the enabled sources.

### aggregator.py - `NewsAnalyzer`
- `refresh(force)`: fetch (using an on-disk cache within TTL), score sentiment,
  tag relevant symbols by a keyword map (`_DEFAULT_SYMBOL_KEYWORDS`, overridable
  via `news.symbol_keywords`). Never raises.
- `get_signal(symbol) -> [-1,+1]`: time-decayed, relevance-weighted aggregate.
- `in_blackout(symbol)`: True if fresh, strongly-polarized news is within
  `blackout_minutes` (decision engine avoids new entries then).
- `summary(symbol)`: small dict for logging.
- Degrades to neutral 0.0 when disabled/offline/no matches.

---

## 10. Decision layer (core/decision/engine.py)

### `Decision`
Value object: `action` (+1/-1/0), `score` [-1,+1], `size_hint` [0,1],
`sl_atr_mult`, `tp_atr_mult`, `reasons`, `components`. `to_dict()` for logging.

### `DecisionEngine`
Constructed with cfg + optional `learner`, `feature_builder`, `news_analyzer`,
`memory`, `timing`, `learner_provider`, and `meta_labeler`. `learner_provider`
(A5 / P3.4) is an optional callable `symbol -> learner`; when supplied (BotContext
passes `ctx.learner_for` only while `learning.per_symbol` is true) the engine uses
the DECIDING symbol's own ML model, falling back to the shared `learner` on any
failure. `meta_labeler` (U6.1, a `MetaLabeler`) is supplied by BotContext ONLY
when `decision.meta_label.enabled` is true; in the parity path, after an entry is
intended the engine calls `meta_labeler.should_veto(spec, ohlcv, sig)` and, on a
veto, blocks the entry (reason `veto_meta_label=1`, records `meta_win_prob`). It
is strictly VETO-ONLY: it can turn an intended entry into a hold but never
create/flip/resize a trade, and it stays silent when disabled or untrained.

**Decision mode (UPGRADE_PLAN U2.4)** - `decision.mode` selects the whole
decision path:
- `"parity"` (**DEFAULT**): `decide()` dispatches to `_decide_parity`, which
  trades the **top-1** registry strategy for the symbol/timeframe EXACTLY as it
  was walk-forward validated - its own `blended_signal`, its own
  `long_threshold` / `short_threshold`, and its own `sl_atr_mult` / `tp_atr_mult`
  (never the global `decision.long_threshold`). The learner, news blackout, and
  timing gate are applied as **veto-only** gates (config `decision.parity_vetoes.
  {learner,news,timing}`, all default true; `decision.parity_learner_veto_level`
  default 0.5): each may only BLOCK the entry, never create/flip/resize it. Decay
  monitor still applies (parity picks the best non-suspect top strategy). With no
  promoted strategy the decision is flat + reason `parity=no_registry_strategy`.
  Fixes diagnosis D2 (validated == traded).
- `"blend"` (legacy research): the weighted-composite path below.

`decide(ohlcv, symbol, tf) -> Decision` (blend mode):
1. **Indicator signal**: prefer the memory-selected top-strategy ENSEMBLE for
   this symbol/timeframe (loaded via `memory.load_registry_top`), else an
   equal-weight blend of enabled stand-alone indicators. Also yields SL/TP mults.
2. **Learning signal**: `_learner_for(symbol)` resolves the per-symbol learner
   (or the shared one), then its `predict_signal` on the latest feature row
   (0.0 if the resolved learner is not ready).
3. **News signal**: `news.get_signal(symbol)` (0.0 if disabled).
4. Weighted blend using `decision.weights`, re-normalized over only the
   components that actually contributed. Optional news blackout and
   indicator/learning agreement rule. Threshold into an action; size_hint scales
   with how far score exceeds the threshold.

5. **Timing signal** (Phase 5, optional): if a `timing` provider is supplied and
   `timing.enabled=true`, `_timing_signal` computes a `TimeSignal` (learned edge +
   size multiplier + favorable/blackout flags) for the current bar's
   session/day/season. Depending on config it can (a) act as a CONFIDENCE / SIZE
   modifier, (b) add a directional vote (`timing.as_directional`, weight
   `decision.weights.timing`), and/or (c) GATE new entries in unfavorable windows
   (`timing.gate_unfavorable`). Default OFF, so the live-light path is unchanged.

Defensive: any missing/failed component contributes 0.0 and is dropped from the
blend, so the bot still decides on weak hardware with most features off.

---

## 10b. Phase 5 - Timing / session / season layer (core/timing)

Motivation (user-update-request): trading edge can depend on the active FX
session (Sydney/Tokyo/London/New York and their overlaps), the day of week, the
hour, and the season. The bot must DISCOVER whether such an edge exists from its
own historical trade outcomes rather than assume it. Pure-Python stdlib only
(datetime), so it runs on a minimal Windows 7 Python install. Default OFF.

### session.py - `SessionCalendar` + `TimeContext`
- `TimeContext`: value object for one bar - active `sessions`, a `session_label`
  (prefers overlaps: `london_newyork_overlap`, `tokyo_london_overlap`),
  `day_of_week` (0=Mon..6=Sun), `hour` (UTC), `month`, `quarter`, `season`.
- `SessionCalendar(cfg)`: converts a bar epoch to UTC using
  `timing.timestamp_is_utc` / `timing.utc_offset_hours`, then maps it to a
  `TimeContext`. Session windows are config-driven (`timing.sessions`) and may
  wrap past midnight.

### time_stats.py - `TimeStats`
- Learns and PERSISTS a per-bucket edge from realized trade outcomes. Buckets are
  keyed by type (session/day/hour/month/quarter/season) and value. Each bucket
  aggregates trade count + mean outcome -> an edge in [-1,+1]. Trusts a bucket
  only after `timing.learning.min_samples` trades (default 50 since P3.1).
  Survives restarts via the memory store.
- Bayesian shrinkage (A4 / P3.1): the bucket edge is multiplied by
  `n / (n + timing.learning.shrinkage)`, pulling small buckets toward a neutral
  0 edge in proportion to how few samples they have, so a rare bucket cannot
  hallucinate a strong time pattern. `shrinkage` is DECOUPLED from `min_samples`
  (the trust threshold) and defaults to `min_samples` to preserve the old
  `n / (n + min_samples)` behavior; `shrinkage <= 0` disables shrinkage (raw
  edge). `_edge_from_row(row, min_samples, shrinkage=None)` implements it;
  `shrinkage=None` reproduces the pre-P3.1 formula. Config is read defensively
  (bad/missing values fall back to the safe defaults).

### time_context.py - `TimeContextProvider` + `TimeSignal`
- `evaluate(ohlcv, symbol, tf) -> TimeSignal`: builds the `TimeContext`, looks up
  each bucket's learned edge in `TimeStats`, combines them using
  `timing.bucket_weights`, and returns a `TimeSignal` with the combined `edge`,
  an `enabled` flag, a position-`size_mult` (mapped from the edge into
  `[min_size_mult, max_size_mult]`), and favorable/blackout flags derived from
  `timing.favorable_threshold` / `timing.blackout_threshold`.

Wiring: built lazily by `BotContext.timing` only when enabled; passed into
`DecisionEngine`; `FeatureBuilder` can optionally add session/day/season columns
when `timing.as_features=true`; `StrategySearch`/`WalkForward` feed realized
trade outcomes back into `TimeStats` so the time edge is learned empirically.

---

## 11. Execution layer (core/execution)

### risk_manager.py - `RiskManager`
- `position_size(symbol, entry, stop, size_hint)`: lot so that hitting the stop
  loses about `risk_per_trade * equity` (scaled by size_hint); broker-aware when
  symbol info available, else safe FX/gold/JPY defaults; clamped to min/max lot
  and rounded to volume step.
- Limits: `can_open_new(open_count)` enforces `max_open_positions` and
  `daily_loss_breached()` (tracks day-start equity + realized PnL via
  `register_realized_pnl`).

### order_manager.py - `OrderManager`
- `execute(decision, symbol, atr, last_close)`: compute entry (bid/ask or last
  close offline), SL/TP prices from ATR mults, lot from RiskManager. In `paper`
  mode LOGS the intended order and returns; in `live` mode builds and sends the
  MT5 order request via the connector. Avoids stacking same-direction positions
  and respects risk limits.
- `open_positions(symbol)` (filtered by magic number), `close_position(pos)`.

---

## 12. Utilities (core/utils)

- **logger.py**: `configure_logging(...)` (console + rotating file, ASCII format,
  configured once); `get_logger(name, cfg)` factory used everywhere.
- **helpers.py**: `set_global_seed`, `safe_div`, `clamp`, `is_finite_number`,
  `timeframe_to_mt5` (+ fallback table so no MT5 import needed), `timeframe_seconds`,
  `read_json`/`write_json` (never crash), `ensure_dir`.
- **trade_log.py** (U1 transparency): audit "receipts" for a backtest.
  `write_trade_csv(trades, path)` writes one row per closed trade (entry/exit
  time, direction, entry/exit price, SL, TP, `exit_reason` sl/tp/flip/eod, gross
  pnl, cost split spread/commission/slippage/swap, `balance_after`, entry
  `signal`); `write_equity_csv(equity_curve, path)` writes the bar-indexed equity
  curve; `write_artifacts(result, symbol, timeframe, ...)` writes both under
  `backtests/` with a `_timestamp_tag()` filename and returns the paths;
  `implied_total_cost(trades)` sums the per-trade cost fields. Stdlib `csv` only.
- **decision_log.py** (U1 transparency): per-decision journal for paper/live.
  `decision_to_record(...)` flattens a decision into a JSON-safe dict (per-strategy
  signals, learner probability, news score, threshold, final action);
  `append_decision(decision, symbol, timeframe, ...)` appends one JSON line to
  `logs/decisions_<YYYY-MM-DD>.jsonl` (UTC date). Stdlib `json` only; append-only
  so a crash never corrupts prior lines.

---

## 13. Installer & scripts

- **install.bat** (project root): ONE-CLICK Windows 7 installer. Finds a usable
  Python 3.8.x (py launcher / PATH / previously bot-installed copy); if none,
  downloads and silently installs Python 3.8.10 x64 (per-user, no admin) via
  PowerShell. Runs `installer/install_vcredist.ps1` to ensure the VC++ x64
  runtime, then runs `installer/install_helper.py`. Prints next-step commands.
- **installer/install_helper.py**: checks Python version, ensures pip
  (ensurepip bootstrap), best-effort upgrades pip/setuptools/wheel, installs
  `requirements.txt` (bulk first, then per-package with retries so an OPTIONAL
  wheel failure never blocks REQUIRED ones), verifies by importing deps + the
  bot's own package, generates sample data if none. Exit 0 = verified,
  1 = required dep missing, 2 = no pip.
- **installer/install_vcredist.ps1**: checks the registry for the VC++ 14.x x64
  runtime; downloads + silently installs `vc_redist.x64.exe` if missing. Never
  fatal to the caller.
- **scripts/run_bot.bat**: locates Python (same order) and runs `main.py`
  optionally with a mode argument (paper/live/search/backtest/train/loop).
- **scripts/export_history.py**: connects to MT5 and exports history to
  `data_store/history/*.csv` for offline search/backtest.
- **scripts/export_strategy_for_ea.py**: reads the best strategy per
  symbol/timeframe from `data_store/strategy_registry.json` and flattens it into
  `experts/params/<SYMBOL>_<TF>.params` (simple key=value) for the MT5 EA. Only
  EA-supported indicators (`EA_SUPPORTED_INDICATORS`: ema, sma, rsi, macd, atr,
  adx) are runnable. Keep that list in sync with the EA's `ApplyParam()`.
  PARITY HARD GUARD (U2.1): `--strict` is the DEFAULT - if the chosen strategy
  uses ANY unsupported indicator the export FAILS (writes nothing) with a message
  listing the offenders, so a crippled strategy can never be shipped silently.
  `--allow-partial` (experiments only) drops the unsupported indicators, RESCALES
  the surviving supported weights to conserve total weight
  (`_flatten_spec` returns `(lines, skipped, rescaled)`), and stamps a prominent
  `!! WARNING: PARTIAL / DEGRADED EXPORT` block into the `.params` header. Helper
  `_unsupported_indicators(spec)` lists the offenders. If EVERY indicator is
  unsupported the export fails even with `--allow-partial`. Covered by
  `tests/test_ea_export_parity.py`.
- **scripts/make_report.py** (U1 transparency): turns a trade CSV into ONE
  self-contained `.html` audit report (inline SVG charts, no external deps or
  internet). argparse: positional `trades_csv`, `--equity`, `--out`, `--title`;
  `main(argv=None)`. Report has a summary table, equity/drawdown chart, per-month
  PnL, 10 worst trades, exit-reason breakdown, and cost-vs-gross share.
- **scripts/explain_decisions.py** (U1 transparency): pretty-prints the decision
  journal with the WHY per decision. argparse: `--n` (default 20), `--date`,
  `--file`, `--log-dir` (default "logs"), `--symbol`. Reads
  `logs/decisions_*.jsonl` and shows which components pushed the score over/under
  the entry threshold (so a "no trade" is as explainable as a trade).
- **scripts/gauntlet.py** (U5.1/U5.2 - the pre-flight gauntlet): runs a FIXED
  sequence of five pessimistic stress tests on the registry TOP-1 strategy for a
  symbol/tf and writes ONE verdict `backtests/gauntlet_<fingerprint>.md`.
  argparse: `--symbol`, `--tf`, `--mc` (Monte-Carlo shuffles, default 1000),
  `--warmup`; `main(argv)` returns 0 on PASS / 1 on FAIL. Gates:
  `gate_full_history` (net profit + positive expectancy over all bars),
  `gate_holdout` (re-score on the locked `holdout_bars` tail),
  `gate_monte_carlo` (reshuffle trade order -> 5/95 equity envelopes, max-DD
  distribution, `risk_of_ruin`; PASS needs final_equity>start AND
  risk_of_ruin<=5%), `gate_cost_stress` (spread x1.5 must survive, x2 is
  informational, via `_cfg_with_spread_mult` which scales BOTH flat
  `spread_points` and the U3.3 `spread_model.base_points`), and
  `gate_worst_window` (worst rolling 3-month equity not catastrophic).
  `run_gauntlet` aggregates to `overall_pass`; `write_verdict_md` stamps the two
  machine-parseable lines (`created_at_epoch`, `overall_pass`) the live gate
  reads. Uses the same pessimistic `Backtester` (Phase U3) - no search, no live
  orders; pure stdlib + project modules.
- **examples/generate_sample_data.py**: writes synthetic OHLCV CSVs so the whole
  pipeline runs offline with no MT5.

### app/gauntlet_gate.py - live pre-flight gate (U5.3)
`general.live_requires_gauntlet` (default true) makes LIVE mode refuse to start
unless a PASS gauntlet verdict exists for the registry top-1 of every traded
symbol/tf AND is newer than the last search (registry `updated_at`).
`parse_verdict_file` reads the two stamped lines without re-running anything;
`check_symbol` returns (allowed, reason) - a symbol with no promoted strategy
does NOT block (nothing to trade), a missing/FAIL/STALE verdict blocks;
`check_live_allowed` aggregates across symbols (live allowed only if ALL pass)
and, when the flag is off, always allows with a note. Fails LOUDLY: any checking
error is treated as a BLOCK so a parsing bug never opens un-vetted live trading.
Wired into `app/runners.py` (run_once/run_loop dispatch) and `main.py`; paper /
backtest / search / train are never gated.

### Native MT5 Strategy Tester validation (experts/)
- **experts/Mt5SmartBotEA.mq5**: a self-contained MQL5 Expert Advisor that
  replays the learned blended-indicator logic inside the native MT5 Strategy
  Tester for authoritative, tick-accurate validation. It loads a `.params` file
  from the terminal's `MQL5\Files` (via `InpParamsFile`), falling back to its own
  input parameters when absent. Acts once per new bar; enters on the blended
  score crossing long/short thresholds; ATR-based SL/TP; risk-based lot sizing;
  closes on signal flip. Supported indicators: ema, sma, rsi, macd, adx (+ atr
  for SL/TP). It mirrors the INDICATOR component of the Python decision engine,
  not the ML/news blend.
- **experts/README_EA.md**: step-by-step install/compile/run guide and the exact
  indicator->signal mapping.

### Tests (tests/)
- Offline, standard-library-only suite (no MT5, no network needed):
  - `helpers.py`: path fix + deterministic synthetic OHLCV builder.
  - `test_config.py`: config loads, dotted access, path resolution.
  - `test_indicators.py`: registry populated, signals bounded [-1,+1], series
    aligned to bars, build-from-config.
  - `test_learning.py`: FeatureBuilder shapes, active model fit/predict bounds,
    NeutralModel fallback.
  - `test_memory.py`: record/aggregate/rank + JSON registry persist and reload
    (restart simulation) using a temp DB.
  - `test_walk_forward.py`: segment count grows with history (P1.3 auto-shrink);
    the locked holdout tail (P1.4) never appears in any train/test segment;
    `evaluate_holdout` is a no-op when disabled and blocks a spec that fails on
    the untouched holdout; the store `allowed_fingerprints` allowlist restricts
    registry promotion (none/subset/empty).
  - `test_metrics_significance.py` (A3 / P2.5): wilson_interval textbook + edge
    cases; bootstrap_pvalue low for a positive series, high for a symmetric one,
    deterministic under a fixed seed; compute_metrics carries win_rate_ci_low /
    pnl_pvalue; and the memory store PROMOTION filter (P2.4) records a
    non-significant strategy but never promotes it, promotes the significant one,
    honors the optional win-rate lower-bound gate, and is a no-op when disabled.
  - `test_timing_stats.py` (A4 / P3.2): time-bucket Bayesian shrinkage lock-in.
    The pure `TimeStats._edge_from_row` formula heavily shrinks a 5-sample
    bucket (< 15% of raw edge) while a 500-sample bucket stays near raw (> 85%);
    the `trusted` flag tracks min_samples only; `shrinkage=None` reproduces the
    pre-P3.1 n/(n+min_samples) formula and `shrinkage <= 0` disables damping; an
    empty bucket is neutral. The full record_trades -> bucket_edge round-trip
    against a temp DB shows the shrinkage gap end to end, survives a fresh
    TimeStats instance (restart simulation), and honors config shrinkage=0.
  - `test_per_symbol_learning.py` (A5 / P3.5): per-symbol ML lock-in.
    `BotContext._per_symbol_model_file` and `app.runners._per_symbol_model_file`
    produce byte-identical, distinct paths per symbol (incl. broker "EURUSD.m").
    Two symbols train on two clearly-different synthetic datasets and save into a
    private temp dir (real models/ untouched); `learner_for` then returns a
    DISTINCT, ready, CACHED learner per trained symbol, falls back to the shared
    learner for an untrained symbol, and default mode keeps the shared learner
    with NO engine provider (light path unchanged) while per_symbol=true supplies
    one. A sentinel-learner provider proves the engine's `_learning_signal`
    routes each symbol to the right model (0.9 vs -0.9) and an unknown symbol
    yields a neutral 0.0.
  - `test_backtester_swap_gap.py` (A6 / P3.7): weekend-swap + Monday-gap lock-in
    for the P3.6 backtester model. A deterministic `_StubStrategy` (fixed
    decision + ATR series) on a flat-priced Friday->Monday series isolates the
    swap: swap 0.0 (default) is break-even no-op, a 10-pt long swap held
    Fri->Mon is charged exactly 3 nights (swap_pts * point * contract * lot * 3),
    and a negative swap rate is a credit (positive PnL). Unit checks of
    `Backtester._rollovers_between` (Fri->Mon=3, ordinary night=1, Tue->Wed into
    the triple day=3, same-day=0). A long stopped by a Monday bar that gaps DOWN
    through the stop fills at the stop (98.0) with `model_weekend_gap` OFF and at
    the worse gapped OPEN (96.0) with it ON.
  - `test_realism.py` (U3.6): locks the Phase U3 pessimism guarantees with
    deterministic `_StubStrategy` fixtures - `next_open` fills are never better
    than `signal_close`; `pessimistic <= midpoint <= optimistic` on a bar that
    touches both SL and TP; a trade entered inside the rollover window costs more
    (and a flat spread is identical without a `spread_model`); `risk_pct` sizing
    clamps to min/max lot; the `max_daily_loss` circuit breaker cuts the trade
    count; and too-tight stops are rejected by `min_stop_points` (12 tests).
  - `test_meta_label.py` (U6.1): the meta-labeling veto gate. `_LogReg` learns a
    separable two-class problem and round-trips through `to_dict/from_dict`
    identically; a disabled OR untrained OR too-few-samples OR single-class gate
    NEVER vetoes; `train()` persists and a fresh `MetaLabeler` reloads to
    identical `win_probability`; `should_veto` fires exactly when
    `P(win) < min_win_prob`; ENGINE integration proves it is VETO-ONLY (a forced
    veto turns an intended parity entry into a hold, a passing/absent gate leaves
    the action unchanged, and it can never turn a hold into a trade); the
    persisted `_FEATURE_NAMES` layout is frozen (14 tests).
  - `test_news.py`: lexicon sentiment bounds, offline/disabled graceful neutral.
  - `test_pipeline.py`: DecisionEngine on synthetic data + run_once/backtest/
    train end-to-end on sample CSVs.
  - `run_all.py`: discovers and runs everything; non-zero exit on failure.
  Run: `python -m unittest discover -s tests -v` or `python tests/run_all.py`.

### Continuous integration (.github/workflows/ci.yml, repo root)
- `.github/workflows/ci.yml` (A7 / P4.1): a tiny GitHub Actions workflow named
  `offline-tests` that, on every push and pull request, checks out the repo,
  sets up Python 3.8 (matching the Windows 7 target), and runs
  `python tests/run_all.py` from the repo ROOT (the whole project now lives at
  the root, so no `working-directory` is needed). It uses only stdlib (no MT5,
  no network, no heavy deps) so it mirrors the local offline gate exactly and
  has ZERO effect on the Windows 7 runtime. It lives at the repo root because
  GitHub requires workflows under `.github/workflows/`. A byte-for-byte
  reference copy of the body is kept at `ci_workflow_template.yml` for provenance.
  STATUS (2026-07-06, SIXTH session): DONE. The workflow file
  `.github/workflows/ci.yml` is PRESENT, tracked, and CORRECT on GitHub
  (verified via the Contents API: name `ci.yml`, sha `3a9c55c...`, 1568 bytes;
  YAML parses, name = offline-tests). It runs `python tests/run_all.py` at the
  repo root, matching the local suite (64 tests, all green). The GitHub App
  lacks the `actions` read scope, so the assistant cannot poll the Actions API
  from the sandbox (`gh run list` -> 403) to observe a run - an OBSERVABILITY
  limit, not a code problem. structure.md P4.1 and P4.2 and A7 are all flipped
  to [x]; the README carries the CI badge + note (P4.2). Phase P4 is complete.
  (History: the workflow was first added via the GitHub web UI in commit
  `e602990 Create ci.yml` to bypass the App `workflows`-permission push blocker,
  then updated in `419cdf4 Update ci.yml` after the project moved to the repo
  root in `0c1cfd6`.)

---

## 14. requirements.txt (Windows 7 / Python 3.8 pins)

Pinned to the newest releases that still ship cp38 win_amd64 wheels usable on
Windows 7:
- Core: `MetaTrader5==5.0.45` (Windows only), `numpy==1.24.4`, `pandas==1.5.3`,
  `PyYAML==6.0.1`, `python-dateutil==2.8.2`, `requests==2.31.0` (+ certifi/urllib3/
  charset-normalizer/idna).
- Optional light ML: `scikit-learn==1.3.2`, `scipy==1.10.1`, `joblib`,
  `threadpoolctl`, `lightgbm==3.3.5`.
- Optional news: `vaderSentiment==3.3.2`, `feedparser==6.0.11`.
- HEAVY DL (`tensorflow`) is intentionally commented out; do not put it on a
  Windows 7 live machine. Keep this file in sync with install_helper.py's
  CORE_IMPORTS / OPTIONAL_IMPORTS classification.

---

## 15. End-to-end data flow (how the phases connect)

```
config.yaml
    |
    v
BotContext (app/context.py) --- builds/caches all components
    |
    +--> DataFeed (MT5 live OR CSV) -----------------------------\
    +--> Indicators (Phase 2, pluggable) ----------------\        |
    +--> Learner (Phase 1, active_model) ----\           |        |
    +--> MemoryStore (Phase 3) --------\      |           |        |
    +--> NewsAnalyzer (Phase 4) --\    |      |           |        |
                                  |    |      |           |        |
                                  v    v      v           v        v
                            DecisionEngine (core/decision/engine.py)
                                  |  blends: indicators/ensemble +
                                  |          learning + news
                                  v
                               Decision (action, score, SL/TP, size)
                                  |
                                  v
                       OrderManager + RiskManager (core/execution)
                                  |
                       paper: log order   |   live: MT5 order_send
```

Offline "self-improvement" loop (Phase 3), run separately:
```
history CSV --> StrategySearch --> WalkForward --> Backtester --> metrics
                                        |                            |
                                        v                            v
                                  MemoryStore (SQLite)  <----  update_registry
                                        |
                                        v
                          strategy_registry.json (top-K per symbol|TF)
                                        |
                                        v
                          consumed by DecisionEngine at live time
```

---

## 16. Conventions & invariants (do not break)

1. **ASCII only** everywhere (code, comments, docs, strings, filenames).
2. **Entire project lives at the repository ROOT** (moved from the old `main/`
   subfolder in commit `0c1cfd6`); the only file outside the app tree is
   `.github/workflows/ci.yml`, which GitHub requires under `.github/workflows/`.
3. **Config-driven**: no hard-coded feature flags; read from config.yaml.
4. **Graceful degradation**: missing MT5 / missing optional dep / offline news
   must NOT crash the bot; components return neutral/empty and log a warning.
5. **Windows 7 + CPU-only**: default config keeps heavy learners OFF; live path
   must work with pure-Python fallbacks.
6. **Layer isolation**: only `core/data/mt5_connector.py` imports MetaTrader5.
7. **Persistence** lives in `data_store/` and must survive restarts.
8. **Keep this CODE_MAP.md and README.md updated on every change.**

---

## 17. Current status / next steps

- Phases 1-4, data/decision/execution layers, config, logging, memory, installer,
  scripts, requirements, and this CODE_MAP are IMPLEMENTED.
- README.md is now WRITTEN (full Windows 7 install/run/backtest/VPS guide,
  hardware notes, and honest limitations).
- A native MT5 Strategy Tester Expert Advisor is now INCLUDED
  (`experts/Mt5SmartBotEA.mq5` + `experts/README_EA.md`) together with the
  `scripts/export_strategy_for_ea.py` exporter that feeds it the learned
  strategy.
- A formal, offline, stdlib-only TEST SUITE is now INCLUDED under `tests/`
  (64 tests covering config, indicators, learning, memory, news, walk-forward /
  holdout, statistical significance, time-bucket shrinkage, per-symbol ML, the
  weekend-swap / Monday-gap backtester model, and the full pipeline). All pass
  offline without MT5 or a network.
- Verified: the offline pipeline runs (`python main.py --mode paper/train/
  backtest/search`) using CSV data, a loaded ML model, the memory ensemble, and
  the news layer; and `python tests/run_all.py` is green (64 tests).
- Phase 5 TIMING layer (user-update-request) is IMPLEMENTED under `core/timing/`
  (SessionCalendar/TimeContext, TimeStats learned per-bucket edge, and
  TimeContextProvider/TimeSignal). It is wired (optional, default OFF) into the
  decision engine (confidence/size modifier + optional directional vote + entry
  gate), the feature builder (`timing.as_features`), and the search/walk-forward
  outcome feedback. Session/day/season awareness is discovered empirically.
  Verified offline: timing context + signal compute correctly and all tests pass.
- Possible future work: richer per-currency news attribution; more indicators in
  the EA (supertrend/bbands) so the exporter can pass them through; optional
  annualized/risk-adjusted metrics; a self-contained CI workflow file; dedicated
  unit tests for the timing layer; expose timing session windows in the EA.
- REPO NOTE (2026-07-06, fourth session): the project now lives in the
  `golshandvr-del/Mt5Bot` GitHub repo on branch `main`. Its full commit history
  was migrated this session from the earlier `golshandvr-del/MtBot` repo
  (byte-identical content, so nothing was lost); `Mt5Bot` is the authoritative
  primary repo going forward.
- FOLDER-MOVE NOTE (2026-07-06, fifth session): at the user's request the entire
  project was MOVED from the old `mt5/mt5_bot/` directory to a single top-level
  `main/` directory (via `git mv mt5/mt5_bot main`, so file history is
  preserved), and the now-empty `mt5/` parent was removed. This aligns the repo
  with the roadmap invariant "the entire project lives ONLY inside main/". No
  source code changed - all code uses project-root-relative paths, so it runs
  unchanged from the new location (offline suite: 64 tests green from `main/`).
  Every path reference in the docs (CODE_MAP.md, structure.md, Ideas.md,
  README.md, experts/README_EA.md), the `main/ci_workflow_template.yml`
  reference copy (now `working-directory: main`), and `.gitignore` were updated
  from `mt5/mt5_bot` to `main` and PUSHED. The LIVE CI workflow
  `.github/workflows/ci.yml` also needed its `working-directory` changed to
  `main`, but the assistant's push of that edit was REJECTED by the GitHub App
  `workflows`-permission gap (rolled back to keep HEAD == origin); the USER made
  that one-line fix in the GitHub web UI (commit `33b0360 Update ci.yml`), so the
  live workflow now correctly uses `working-directory: main`. Historical
  change-log entries that mention the old path describe events as they happened;
  the path token was updated for consistency since the files now live under
  `main/`.
- ROOT-MOVE NOTE (later, commit `0c1cfd6 Restructure: move all project files
  from main/ to repository root`): the `main/` subfolder wrapper was removed and
  the entire project now lives at the REPOSITORY ROOT. The CI workflow was
  updated accordingly (`419cdf4`) to run at the repo root with no
  `working-directory`. This CODE_MAP layout tree (section 2), the REPO-LAYOUT
  note (top), and invariant 2 (section 16) were resynced to the root layout;
  the historical `main/`-era notes above are retained as provenance.
- ROADMAP PROGRESS: Phases P1, P2, and P3 of `structure.md` are now COMPLETE.
  Phase P1 (Track A items A1 + A2 - honest evaluation: multi-year real-data
  workflow documented, more walk-forward segments via `min_segments`
  auto-shrink, and a locked holdout gate wired into search + registry promotion,
  all locked in by `tests/test_walk_forward.py`). The actual multi-year export +
  long search (A1) is a user action on the Windows machine.
  Phase P2 (Track A item A3 - statistical-significance filter: `wilson_interval`
  + `bootstrap_pvalue` in metrics, `win_rate_ci_low` + `pnl_pvalue` in
  compute_metrics, the `memory.search.significance` config block, and the store
  record-but-never-promote filter, all locked in by
  `tests/test_metrics_significance.py`). Phase P3 (robust context modeling) is
  now COMPLETE: P3.1 time-bucket Bayesian shrinkage + higher trust threshold,
  P3.2 its test, P3.3 per-symbol ML TRAINING (run_train writes
  models/<model>_<SYMBOL>.pkl), P3.4 the per-symbol learner LOOKUP
  (`BotContext.learner_for` + `DecisionEngine.learner_provider`, config key
  `learning.per_symbol` default false), P3.5 the two-symbol distinct-model
  test (`tests/test_per_symbol_learning.py`), P3.6 the weekend-swap + Monday-gap
  model in the backtester, P3.7 its test
  (`tests/test_backtester_swap_gap.py`), and P3.8 the A4/A5/A6 status flips
  (docs-only) are all done. Phase P4 (CI safety net, A7) is now COMPLETE. The
  `offline-tests` workflow (`.github/workflows/ci.yml`) is live on GitHub and
  runs `python tests/run_all.py` at the repo root on push/PR under Python 3.8
  (stdlib-only, zero Windows-7-runtime impact); it was added via the GitHub web
  UI (`e602990`) to bypass the App `workflows`-permission blocker and updated in
  `419cdf4` after the project moved to the repo root (`0c1cfd6`). P4.1, P4.2 and
  A7 are all flipped to [x]; README carries the CI badge + note. The assistant
  cannot observe Actions run results from the sandbox (App lacks `actions` read
  scope), but CI mirrors the green local suite exactly. See structure.md
  section 5. Phase P5 (living adaptive core, B1/B3) is now IN PROGRESS: the
  STRATEGY COUNCIL half (B1) is DONE - P5.1 `core/strategy/council.py`
  (pure-Python UCB1 bandit for per-strategy live credibility), P5.2 persistence
  in `core/memory/store.py` (new `council` table + save/load_council), P5.3
  consumption in `core/decision/engine.py` (credibility-weighted ensemble blend,
  `decision.council.*` config default OFF, wired through `BotContext.council`),
  and P5.4 `tests/test_strategy_council.py` (8 tests: loser weight decays toward
  the floor, weights persist across a simulated restart, engine blend tilts to
  the winner) are all flipped to [x]. Full offline suite now 72 tests, all green.
  NEXT: the DECAY-MONITOR half (B3) - P5.5 `core/strategy/decay_monitor.py`
  (per-registry-strategy statistical-expiry: recent live/paper PnL vs its
  walk-forward distribution), P5.6 wiring in order_manager + store
  (`decision.decay_monitor.*` default OFF), P5.7 its test, P5.8 doc sync +
  B1/B3 flips.
- PRIORITIZED NEXT STEPS: see `structure.md`. An expert-AI review flagged the
  biggest current risk as STATISTICAL (small samples), not software. The roadmap
  there sequences Track A (multi-year real data, more walk-forward segments +
  holdout, Wilson/bootstrap significance filter, time-bucket shrinkage,
  per-symbol ML, weekend swap/gap, CI) before Track B (strategy council, time x
  regime buckets, decay monitor, overnight training, contrarian sensor, weekly
  journal, evolutionary search, recency weighting). Follow structure.md order.
```
