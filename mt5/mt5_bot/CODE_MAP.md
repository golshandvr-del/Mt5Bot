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
mt5/                              <- required top folder
  mt5_bot/                        <- the ENTIRE project lives ONLY here
    install.bat                   <- ONE-CLICK Windows 7 automatic installer
    main.py                       <- CLI entry point; dispatches run modes
    requirements.txt              <- dependencies pinned for Win7 / Python 3.8
    CODE_MAP.md                   <- THIS FILE (always keep current)
    structure.md                  <- structure snapshot + prioritized ROADMAP
    Ideas.md                      <- idea backlog log (write idea before change)
    README.md                     <- user-facing guide (install/run/backtest/VPS)

    config/
      config.yaml                 <- MASTER config; controls the whole bot
      loader.py                   <- YAML loader + dot-access + fallback parser
      __init__.py

    app/
      context.py                  <- BotContext: builds/caches all components
      runners.py                  <- one function per mode (train/search/etc.)
      __init__.py

    core/
      data/
        mt5_connector.py          <- thin, defensive MetaTrader5 wrapper
        data_feed.py              <- OHLCV container + live/CSV data source
      indicators/                 <- Phase 2 (pluggable)
        base.py                   <- Indicator base class + math helpers
        registry.py               <- register/build enabled indicators
        trend.py momentum.py volatility.py volume.py patterns.py
        __init__.py               <- imports submodules to trigger registration
      learning/                   <- Phase 1 (swappable learners)
        base_model.py             <- BaseModel common interface
        factory.py                <- build learner by name (+ NeutralModel)
        features.py               <- FeatureBuilder (OHLCV -> X, y)
        ml_classifier.py          <- LightGBM / sklearn / pure-Python (DEFAULT)
        rl_agent.py               <- tabular Q-learning (CPU-light, optional)
        dl_classifier.py          <- Keras MLP (HEAVY, off by default)
        transfer.py               <- transfer learning on DL model (optional)
        self_supervised.py        <- autoencoder feature learner (optional)
      strategy/                   <- Phase 3 (search/backtest)
        strategy.py               <- StrategySpec (recipe) + Strategy (executable)
        metrics.py                <- performance metrics + ranking value
        backtester.py             <- fast bar-by-bar single-position simulator
        walk_forward.py           <- rolling out-of-sample evaluation
        search.py                 <- random/grid strategy search (memory builder)
      memory/
        store.py                  <- SQLite + JSON registry of strategies/results
      news/                       <- Phase 4
        base.py                   <- NewsItem, SentimentBackend, NewsSource ABCs
        sentiment.py              <- lexicon (offline) + optional VADER backends
        sources.py                <- RSS (stdlib) + optional NewsAPI sources
        aggregator.py             <- NewsAnalyzer: fetch/score/cache/aggregate
      timing/                     <- Phase 5 (user-update-request): time/season awareness
        session.py                <- SessionCalendar + TimeContext (session/day/season)
        time_stats.py             <- TimeStats: learned per-bucket edge (persisted)
        time_context.py           <- TimeContextProvider: combine buckets -> TimeSignal
        __init__.py               <- exports the timing public API
      decision/
        engine.py                 <- DecisionEngine: fuse all signals -> Decision
                                     (optionally gated/sized by the timing layer)
      execution/
        risk_manager.py           <- position sizing + risk limits
        order_manager.py          <- Decision -> MT5 order (paper logs / live sends)
      utils/
        logger.py                 <- rotating file + console logging setup
        helpers.py                <- seeds, JSON I/O, timeframe mapping, math

    installer/
      install_helper.py           <- pip install w/ retries + verify + sample data
      install_vcredist.ps1        <- auto-install VC++ x64 runtime if missing

    scripts/
      run_bot.bat                 <- run launcher (finds Python, runs main.py)
      export_history.py           <- export live MT5 history to CSV for offline use
      export_strategy_for_ea.py   <- flatten best learned strategy -> EA .params

    experts/                      <- native MT5 Strategy Tester validation
      Mt5SmartBotEA.mq5           <- Expert Advisor replaying the learned blend
      README_EA.md                <- install/compile/run guide for the EA
      params/<SYM>_<TF>.params    <- generated per-symbol EA parameter files

    tests/                        <- offline, stdlib-only test suite
      __init__.py helpers.py run_all.py
      test_config.py test_indicators.py test_learning.py
      test_memory.py test_news.py test_pipeline.py

    examples/
      generate_sample_data.py     <- synthetic OHLCV CSVs for offline first run

    data_store/                   <- persistent state (survives restarts)
      history/<SYMBOL>_<TF>.csv    <- OHLCV history (exported or synthetic)
      memory.sqlite               <- Phase 3 strategy/result database
      strategy_registry.json      <- human-readable best-strategy registry
      news_cache/news_cache.json  <- cached scored news items

    models/                       <- trained model artifacts (e.g. ml_classifier.pkl)
    backtests/                    <- backtest_report.json outputs
    logs/                         <- mt5_bot.log (rotating)
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
- `learning` : `active_model` + per-learner config blocks (Phase 1).
- `memory`   : db/registry files, `walk_forward` windows (train/test/step_bars
  plus `min_segments` and `holdout_bars` for statistical robustness), `search`
  settings, `ensemble_top_k` (Phase 3).
- `news`     : enable, degrade_gracefully, cache, sentiment backend, sources,
  `signal_weight`, `blackout_minutes` (Phase 4).
- `decision` : blend `weights` (indicators/learning/news), long/short thresholds,
  `require_agreement`.
- `backtest` : initial balance, cost model, fixed lot, report dir.

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
- `learner` property also tries to `load()` the persisted model file.
- `shutdown()` closes the MT5 connection.

### app/runners.py - one function per mode
- `run_train(ctx)`  : Phase 1. For each symbol, build (X, y) via FeatureBuilder,
  `learner.fit`, then persist the model file. Trains on the first symbol with
  enough data (single shared model).
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

### metrics.py
- `compute_metrics(trade_pnls, equity_curve)` -> num_trades, win_rate,
  profit_factor, expectancy, net_profit, max_drawdown, sharpe (per-trade),
  average_win/loss.
- `rank_value(metrics, rank_metric)` -> single comparable score (max_drawdown is
  negated so "higher is better" holds for ranking).

### backtester.py - `Backtester`
Fast bar-by-bar single-position simulator (NOT the MT5 tester). Enter on signal
change, exit on opposite signal / SL / TP (ATR-based). Applies spread + slippage
+ commission. Produces `BacktestResult(metrics, equity_curve, trade_pnls)`. Used
for RELATIVE ranking during search; final validation happens in the real MT5
Strategy Tester (see README).

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

### memory/store.py - `MemoryStore` (persistence)
SQLite DB (`data_store/memory.sqlite`) + JSON registry
(`data_store/strategy_registry.json`). Survives restarts.
- Tables: `strategies(fingerprint PK, symbol, timeframe, spec_json, created_at)`
  and `results(id, fingerprint, symbol, timeframe, segment, rank_metric,
  metrics_json, score, created_at)`.
- `record_strategy`, `record_result`, `top_strategies(...)` (averages score
  across walk-forward segments, filtered by min avg trades; optional
  `allowed_fingerprints` allowlist restricts promotion to holdout-passing specs,
  A2 / P1.4), `update_registry(...)` (writes top-K per symbol|timeframe; forwards
  `allowed_fingerprints`), `load_registry_top(...)` (fast read for the decision
  engine), `stats()`.

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
`memory`. `decide(ohlcv, symbol, tf) -> Decision`:
1. **Indicator signal**: prefer the memory-selected top-strategy ENSEMBLE for
   this symbol/timeframe (loaded via `memory.load_registry_top`), else an
   equal-weight blend of enabled stand-alone indicators. Also yields SL/TP mults.
2. **Learning signal**: active learner's `predict_signal` on the latest feature
   row (0.0 if learner not ready).
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
  only after `timing.learning.min_samples` trades. Survives restarts via the
  memory store.

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
  adx) are exported; others are skipped with a note. Keep that list in sync with
  the EA's `ApplyParam()`.
- **examples/generate_sample_data.py**: writes synthetic OHLCV CSVs so the whole
  pipeline runs offline with no MT5.

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
  - `test_news.py`: lexicon sentiment bounds, offline/disabled graceful neutral.
  - `test_pipeline.py`: DecisionEngine on synthetic data + run_once/backtest/
    train end-to-end on sample CSVs.
  - `run_all.py`: discovers and runs everything; non-zero exit on failure.
  Run: `python -m unittest discover -s tests -v` or `python tests/run_all.py`.

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
2. **Entire project stays under `mt5/mt5_bot/`**; nothing else in that folder.
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
  (21 tests covering config, indicators, learning, memory, news, and the full
  pipeline). All pass offline without MT5 or a network.
- Verified: the offline pipeline runs (`python main.py --mode paper/train/
  backtest/search`) using CSV data, a loaded ML model, the memory ensemble, and
  the news layer; and `python tests/run_all.py` is green (21 tests).
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
- PRIORITIZED NEXT STEPS: see `structure.md`. An expert-AI review flagged the
  biggest current risk as STATISTICAL (small samples), not software. The roadmap
  there sequences Track A (multi-year real data, more walk-forward segments +
  holdout, Wilson/bootstrap significance filter, time-bucket shrinkage,
  per-symbol ML, weekend swap/gap, CI) before Track B (strategy council, time x
  regime buckets, decay monitor, overnight training, contrarian sensor, weekly
  journal, evolutionary search, recency weighting). Follow structure.md order.
```
