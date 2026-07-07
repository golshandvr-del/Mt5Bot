# MT5 Smart Trading Bot

[![offline-tests](https://github.com/golshandvr-del/Mt5Bot/actions/workflows/ci.yml/badge.svg)](https://github.com/golshandvr-del/Mt5Bot/actions/workflows/ci.yml)

> **CI:** every push and pull request runs the stdlib-only offline test suite
> (`python tests/run_all.py`) on Python 3.8 via GitHub Actions (workflow
> `offline-tests`, defined in `.github/workflows/ci.yml`). The badge above shows
> the latest run status. The CI is offline-only (no MetaTrader5, no network, no
> heavy deps) and has **zero** effect on the Windows 7 runtime; it simply guards
> against regressions as the roadmap is executed.

A modular, config-driven MetaTrader 5 (MT5) trading bot designed for
**Windows 7 (64-bit), CPU-only, mid-range hardware**. It follows a
**"train offline / run light"** architecture: heavy machine learning is
optional and fully isolated, while the live decision + execution path stays
lightweight and pure-Python friendly so it runs comfortably on weak machines.

> This README is the user-facing guide. For the full internal architecture
> (every module, function, and data-flow between phases) read
> [`CODE_MAP.md`](CODE_MAP.md). For the prioritized development ROADMAP (the
> expert-reviewed plan for what to build next and in which order) read
> [`structure.md`](structure.md). Keep these files in sync on every change.

---

## Table of contents

1. [Goals](#goals)
2. [Features by phase](#features-by-phase)
3. [Project layout](#project-layout)
4. [Requirements](#requirements)
5. [Install on Windows 7 (one click)](#install-on-windows-7-one-click)
6. [Manual install](#manual-install)
7. [How to run](#how-to-run)
8. [Configuration](#configuration)
9. [Exporting history and backtesting in MT5](#exporting-history-and-backtesting-in-mt5)
10. [Deploying on a VPS](#deploying-on-a-vps)
11. [Testing](#testing)
12. [Hardware notes and limitations](#hardware-notes-and-limitations)
13. [Honest notes: what is realistic](#honest-notes-what-is-realistic)
14. [Risk disclaimer](#risk-disclaimer)

---

## Goals

- Provide a **complete, installable** MT5 trading bot that actually runs on old
  hardware (Windows 7, Intel 4th gen, DDR3, no GPU).
- Keep every capability **modular, optional, and swappable** behind a common
  interface so features can be enabled/disabled from a single config file.
- **Degrade gracefully**: a missing MT5 terminal, a missing optional dependency,
  or an offline network must never crash the bot. Components return neutral
  results and log a warning.
- Learn from **years of historical data** through realistic
  strategy/parameter **search + walk-forward backtesting**, persisting learned
  knowledge so it survives restarts.

---

## Features by phase

The bot is organized into four decoupled phases. Each phase can be tuned or
turned off in `config/config.yaml`.

### Phase 1 - Learning core (`core/learning/`)
Swappable learners behind one `BaseModel` interface, selected via
`learning.active_model`:

| Learner            | Backend(s)                                   | Weight   | Default |
|--------------------|----------------------------------------------|----------|---------|
| `ml_classifier`    | LightGBM -> scikit-learn GBDT -> pure-Python | light    | **ON**  |
| `rl_agent`         | tabular Q-learning                           | CPU-light| off     |
| `dl_classifier`    | Keras/TensorFlow MLP                          | heavy    | off     |
| `transfer`         | fine-tune a frozen DL source model           | heavy    | off     |
| `self_supervised`  | autoencoder (sklearn) / pure-Python PCA      | optional | off     |

Missing heavy backends automatically fall back to lighter ones (and finally to a
pure-Python model), so the bot always has a working learner.

Per-symbol models (`learning.per_symbol`, default `false`): by default one shared
model is trained and used for every symbol. Set it to `true` to train and use a
SEPARATE model per symbol (saved as `models/<model>_<SYMBOL>.pkl`) so, for
example, XAUUSD's very different volatility does not dilute EURUSD and vice versa.
Run `python main.py --mode train` after enabling it to produce the per-symbol
files; if a symbol has no trained file yet, the decision engine falls back to the
shared model for that symbol.

### Phase 2 - Indicators / tools (`core/indicators/`)
A large, **pluggable** technical-indicator library. Each indicator produces a
`[-1, +1]` signal and can be enabled/tuned in config:

- **Trend**: `sma`, `ema`, `macd`, `adx`, `ichimoku`, `supertrend`
- **Momentum**: `rsi`, `stoch`, `cci`, `williams_r`, `roc`
- **Volatility**: `atr`, `bbands`, `keltner`, `donchian`
- **Volume**: `obv`, `mfi`, `vwap`
- **Patterns**: `candle_patterns`

Adding a new indicator is a self-registration one-liner (see `CODE_MAP.md`
section 6).

### Phase 3 - Memory & self-improvement (`core/strategy/` + `core/memory/`)
A **realistic** self-improvement loop (NOT literal source-code self-rewriting):

- `StrategySearch` generates many indicator/parameter/threshold combinations.
- Each combination is evaluated with **walk-forward** out-of-sample testing on
  historical data.
- Results (win rate, profit factor, expectancy, max drawdown, Sharpe, ...) are
  **persisted** to SQLite (`data_store/memory.sqlite`) and a human-readable JSON
  registry (`data_store/strategy_registry.json`).
- The decision engine automatically **selects and blends the top-K** strategies
  per symbol/timeframe. The more the bot searches, the richer its memory and the
  better its future strategy selection. Knowledge survives restarts.
- Optional **recency weighting** (`memory.walk_forward.recency_decay`, off by
  default) lets newer walk-forward segments count more than older ones when
  scoring/ranking strategies, so an edge that has quietly stopped working fades
  behind one that is good now. See **Configuration** for details.

### Phase 4 - News analysis (`core/news/`)
Fetches market news, scores sentiment, and feeds a per-symbol news signal into
the decision layer:

- **Sources**: RSS (stdlib, no key) and optional NewsAPI (with a key).
- **Sentiment**: offline lexicon scorer (default, no deps) or optional VADER.
- Time-decayed, relevance-weighted aggregation into a `[-1, +1]` signal, plus an
  optional **news blackout** window around fresh high-impact items.
- Degrades to a neutral signal when disabled/offline.

### Phase 5 - Timing / session / season awareness (`core/timing/`)
Optional layer (default OFF) that lets the bot recognize whether its edge depends
on WHEN it trades - the active FX session (Sydney / Tokyo / London / New York and
their overlaps), the day of week, the hour, the month/quarter, and the season.

- **Empirical, not assumed**: the bot LEARNS a per-time-bucket edge from its own
  realized historical trade outcomes (`TimeStats`, persisted in the memory store).
  A bucket's edge is trusted only after enough trades (`timing.learning.min_samples`).
- **Pure stdlib** (Python `datetime` only), so it runs on a minimal Windows 7
  install with no extra dependencies.
- **Config-driven session windows** (`timing.sessions`) and a broker-time to UTC
  conversion (`timing.timestamp_is_utc` / `timing.utc_offset_hours`).
- **Three ways to use it** (all optional): a confidence / position-size modifier,
  an entry GATE that blocks trades in unfavorable/blackout windows
  (`timing.gate_unfavorable`), and/or a directional vote (`timing.as_directional`).
  It can also add session/day/season columns to the ML features
  (`timing.as_features`).

All phases meet in the **decision engine** (`core/decision/engine.py`), which
blends indicators/ensemble + learning + news (and, when enabled, the timing
signal) into a single `Decision`, then the **execution layer**
(`core/execution/`) sizes and (in live mode) sends the order.

---

## Project layout

```
main/                   The ENTIRE project lives ONLY here
  install.bat            One-click Windows 7 installer
  main.py               CLI entry point (train/search/backtest/paper/live/loop)
  requirements.txt      Dependencies pinned for Windows 7 / Python 3.8
  README.md             This file
  CODE_MAP.md           Full internal architecture map (read this for details)
  structure.md          Structure snapshot + prioritized development roadmap
  Ideas.md              Idea backlog log

  config/               config.yaml (master config) + loader
  app/                  BotContext (assembly) + runners (one per mode)
  core/
    data/               MT5 connector + OHLCV data feed
    indicators/         Phase 2 pluggable indicators
    learning/           Phase 1 swappable learners
    strategy/           Phase 3 search / backtest / walk-forward
    memory/             Phase 3 SQLite + JSON persistence
    news/               Phase 4 news + sentiment
    timing/             Phase 5 session / day / season awareness (optional)
    decision/           Signal fusion -> Decision
    execution/          Risk manager + order manager
    utils/              Logging + helpers
  installer/            install_helper.py + install_vcredist.ps1
  scripts/              run_bot.bat + export_history.py
  experts/              MT5 Expert Advisor (.mq5) for the native Strategy Tester
  examples/             generate_sample_data.py (synthetic offline data)
  tests/                Offline smoke/unit tests
  data_store/           Persistent state (history CSVs, memory DB, news cache)
  models/               Trained model artifacts
  backtests/            Backtest reports
  logs/                 Rotating log file
```

---

## Requirements

- **OS**: Windows 7 64-bit (also runs on newer Windows). The offline pipeline
  (search/backtest/train/paper on CSV) also runs on Linux/macOS for development.
- **Python**: 3.8.x (the last CPython line with official Windows 7 wheels).
- **MetaTrader 5** terminal (64-bit) installed and logged in, for live/paper
  data and order execution. Not needed for offline search/backtest on CSV.
- Dependencies are pinned in `requirements.txt` to versions that ship
  Windows-7-compatible `cp38 win_amd64` wheels.

---

## Install on Windows 7 (one click)

1. Copy the whole `mt5` folder to the target machine.
2. Open the `main` folder.
3. Double-click **`install.bat`** (or run it from a Command Prompt).

`install.bat` will automatically:

- Find a usable **Python 3.8.x** (py launcher / PATH / a copy it installed
  before); if none is found it **downloads and silently installs Python 3.8.10
  x64** (per-user, no admin).
- Ensure the **Visual C++ x64 runtime** via `installer/install_vcredist.ps1`
  (downloads and installs it if missing).
- Run `installer/install_helper.py`, which:
  - bootstraps `pip` if needed,
  - installs `requirements.txt` (bulk first, then per-package with retries so an
    OPTIONAL wheel failure never blocks a REQUIRED one),
  - **verifies** the install by importing core deps and the bot package,
  - generates synthetic sample data if no history exists yet.

If anything fails, the script prints a clear message and continues where safe.
Optional packages (LightGBM, VADER, feedparser) are allowed to fail; the bot
falls back to pure-Python implementations.

---

## Manual install

If you prefer to install by hand (or on Linux/macOS for development):

```bash
cd main
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
# Generate synthetic history so everything runs offline:
python examples/generate_sample_data.py
```

On non-Windows machines, the `MetaTrader5` package is skipped automatically
(it is marked `platform_system == "Windows"` in `requirements.txt`), and the
connector runs in OFFLINE mode using the CSV files in `data_store/history/`.

---

## How to run

All commands are run from inside `main`.

```bash
python main.py                 # use the mode from config.general.mode
python main.py --mode paper    # single decision pass, logs intended orders only
python main.py --mode live     # single pass, sends real MT5 orders (careful!)
python main.py --mode backtest # internal walk-forward backtest report
python main.py --mode search   # Phase 3 strategy/parameter search (build memory)
python main.py --mode train    # Phase 1 offline learner training
python main.py --mode loop     # continuous paper/live loop (for a VPS)
python main.py --config other.yaml   # use an alternate config file
```

`--mode loop` accepts `--iterations N` (0 = forever) and `--sleep SECONDS`.

On Windows you can also double-click **`scripts\run_bot.bat`** (optionally with a
mode argument), which locates Python and launches `main.py`.

**Typical first-time offline workflow:**

```bash
python examples/generate_sample_data.py   # or export real history (below)
python main.py --mode search              # explore strategies -> memory
python main.py --mode train               # train the light ML model
python main.py --mode backtest            # sanity-check performance
python main.py --mode paper               # dry-run decisions
```

---

## Configuration

`config/config.yaml` is the single source of truth. Highlights:

- `general.mode` and `general.enable_heavy_compute` (keep **false** on weak HW).
- `mt5.*`: connection, `symbols`, `timeframe(s)`, `history_bars`.
- `risk.*`: `risk_per_trade`, `max_open_positions`, `max_daily_loss`, SL/TP ATR
  multiples, lot limits, magic number.
- `indicators.*`: per-indicator `{enabled, params}` toggles.
- `learning.active_model`, `learning.per_symbol` (default false; true = a
  separate ML model per symbol), and per-learner blocks.
- `memory.*`: walk-forward windows (incl. `min_segments` / `holdout_bars` /
  `recency_decay`), search settings (incl. the `significance` promotion
  filter), `ensemble_top_k`.
- `news.*`: sources, sentiment backend, `signal_weight`, `blackout_minutes`.
- `decision.weights` (indicators / learning / news) and entry thresholds.
- `backtest.*`: initial balance, cost model, fixed lot, report dir.

To run heavier on a capable training machine, set
`general.enable_heavy_compute: true` and enable `dl_classifier` / `rl_agent`.
Keep them **off** on the Windows 7 live machine.

---

## Exporting history and backtesting in MT5

The bot's internal backtester is a **fast relative-ranking** simulator used
during search. For **authoritative** validation, use the native MT5 Strategy
Tester on Windows.

### 1. Export real history for offline search/backtest

Run this on Windows with the MT5 terminal open and logged in:

```bash
python scripts/export_history.py                 # config symbols/timeframe
python scripts/export_history.py --symbols EURUSD,XAUUSD --timeframe M15 --bars 20000
python scripts/export_history.py --all-timeframes
```

CSVs land in `data_store/history/<SYMBOL>_<TF>.csv` and are consumed by
`--mode search` and `--mode backtest`, even with the terminal closed.

### 1a. Recommended multi-year real-data workflow (do this first)

The synthetic sample data is only for a first offline smoke run. For any results
you intend to trust, the single most important step is to feed the bot **several
years of REAL history**. Small samples make the search trust lucky, random
patterns; a long real history is what makes walk-forward, the significance
filter, and the time-bucket learning meaningful.

**Step 1 - export a long history on Windows (a USER action).**
Run this on the Windows machine with the **MT5 terminal open and logged in** (the
exporter needs a live terminal to pull bars; it cannot run offline):

```bash
cd main
python scripts\export_history.py --symbols EURUSD,GBPUSD,XAUUSD --timeframe M15 --bars 150000
```

- **Aim for at least 5 years of M15 bars per symbol.** As a rule of thumb, M15
  has ~96 bars per trading day and the FX week is ~5 days, so roughly:
  - 1 year  ~= 25,000 bars
  - 3 years ~= 75,000 bars
  - **5 years ~= 125,000 bars** (use `--bars 150000` to be safe)
  Your broker only serves the history it actually stores; if you get fewer bars
  than requested, scroll the chart back in MT5 first (open the symbol chart, press
  Home / page up to force the terminal to download older bars) and re-run.
- The exporter writes one file per symbol/timeframe with these exact names in
  `data_store/history/`:
  - `EURUSD_M15.csv`
  - `GBPUSD_M15.csv`
  - `XAUUSD_M15.csv`
  (pattern: `<SYMBOL>_<TF>.csv`, upper-case symbol, MT5 timeframe label). Export
  additional timeframes with `--all-timeframes` or a specific `--timeframe`.
- Without any flags, `python scripts\export_history.py` uses the symbols,
  timeframe, and `mt5.history_bars` from `config/config.yaml`. To make the config
  default long, raise `mt5.history_bars` (e.g. to `150000`) before exporting.

**Step 2 - run a long search over the real data (offline, terminal can be
closed).** Once the CSVs exist, the heavy exploration runs with no terminal and
no network:

```bash
python main.py --mode search
```

This can legitimately take a long time on a weak CPU because it walk-forward
evaluates many strategy/parameter combinations across the full multi-year
history. Let it finish; it persists everything to `data_store/memory.sqlite` and
refreshes `data_store/strategy_registry.json` as it goes, so progress is not
lost. Tune the effort/breadth in `config/config.yaml` under `memory.search`
(e.g. `max_trials`) and the evaluation windows under `memory.walk_forward`.

> Optional but recommended with multi-year data: set
> `memory.walk_forward.holdout_bars` to reserve the FINAL N bars of history as a
> locked "quarantine" that the search never sees (e.g. `holdout_bars: 15000`,
> roughly the last ~6 months of M15). A strategy is then only promoted to the
> registry if it ALSO passes on this untouched holdout, which is the strongest
> guard against picking strategies that only looked good in-sample. The default
> `0` disables the holdout and keeps the previous behavior.

> Statistical-significance filter (on by default): with real multi-year data the
> search evaluates each strategy across many walk-forward segments, so it can
> tell a genuine edge from a lucky streak. Under `memory.search.significance`
> the bot computes a bootstrap p-value on the trade PnLs (and a Wilson lower
> bound on the win-rate) and only PROMOTES a strategy to
> `strategy_registry.json` when its averaged p-value is `<= max_pvalue`
> (default `0.05` = 95% confidence). A strategy that cannot be statistically
> separated from random is still RECORDED in `data_store/memory.sqlite` (so the
> memory keeps growing) but is never promoted, so the live decision engine only
> ever blends strategies that passed this filter. Set
> `memory.search.significance.enabled: false` to keep the previous behavior, or
> raise `min_winrate_ci_low` (default `0.0` = off) to also require a minimum
> win-rate lower bound.

> Recency weighting (off by default): `memory.walk_forward.recency_decay`
> controls how much newer walk-forward segments count versus older ones when the
> per-segment scores are averaged into a strategy's final score (both in the
> search aggregate and in the registry re-ranking used to pick the top-K). The
> i-th oldest segment gets weight `recency_decay ** (last_index - i)`, so the
> most recent segment always has weight `1.0` and older ones fade geometrically.
> The default `1.0` is OFF (plain average, behavior byte-identical to before); a
> value in `(0, 1)` such as `0.9` down-weights older segments so a strategy that
> was great years ago but has quietly stopped working ranks below one that is
> good *now*. Values `<= 0` or `> 1` are clamped back to `1.0`. This pairs well
> with multi-year data, where the oldest segments may reflect a market regime
> that no longer exists.

**Step 3 - sanity-check, then train and dry-run:**

```bash
python main.py --mode backtest   # internal walk-forward report on the real data
python main.py --mode train      # train the light ML model on the real data
python main.py --mode paper      # dry-run decisions using the learned memory
```

> This "train offline / run light" split means you can do Steps 1-3 on a
> capable machine and then copy just `data_store/` and `models/` to the weak
> Windows 7 live/VPS box, which only ever runs the light paper/live path.

### 2. Validate a strategy in the native MT5 Strategy Tester

An Expert Advisor is provided in `experts/Mt5SmartBotEA.mq5`:

1. Copy `experts/Mt5SmartBotEA.mq5` into your terminal's
   `MQL5/Experts/` folder (open it via *File -> Open Data Folder* in MT5).
2. Open **MetaEditor**, open the file, and press **Compile** (F7).
3. In MT5 open **View -> Strategy Tester**, pick `Mt5SmartBotEA`, choose the
   symbol/timeframe/date range, and run.
4. The EA reads the top strategy recipe exported by the Python side
   (`data_store/strategy_registry.json`, converted to a simple `.set`/params by
   `scripts/export_strategy_for_ea.py`) so the tester evaluates the same logic.

See `experts/README_EA.md` for the exact steps and parameter mapping.

> The MT5 Strategy Tester models spread, swaps, and real tick/OHLC data far more
> faithfully than any lightweight Python simulator. Always confirm there before
> going live.

---

## Deploying on a VPS

1. Rent a **Windows VPS** (a small 1-2 vCPU / 2-4 GB instance is enough for the
   run-light path). Windows Server 2016+ or Windows 10/11 works; Windows 7 works
   if you must match the target OS.
2. Install the **MT5 terminal**, log into your broker account, and enable
   *Tools -> Options -> Expert Advisors -> Allow automated trading* /
   *Allow DLL imports* if your broker needs it.
3. Copy the `mt5` folder to the VPS and run `install.bat`.
4. Configure `config/config.yaml` (set `general.mode: live` only when you are
   confident; keep `mt5.enabled: true`).
5. Start the continuous loop:
   ```bash
   python main.py --mode loop --sleep 60
   ```
   or schedule `scripts\run_bot.bat loop` with **Task Scheduler** at boot.
6. Keep the MT5 terminal running and logged in; the Python bot attaches to it.

Tip: run heavy `--mode search` / `--mode train` on a **separate** capable
machine, then copy `data_store/` and `models/` to the VPS. This is the core of
the "train offline / run light" design.

---

## Testing

A lightweight, dependency-free test suite lives in `tests/`. It exercises the
offline pipeline end to end (config, indicators, learning fallback, memory,
news degradation, decision, and each run mode) without needing MT5 or a network.

```bash
cd main
python -m unittest discover -s tests -v
# or
python tests/run_all.py
```

The tests are designed to pass with only the standard library (optional deps
like LightGBM/pandas make them stronger but are not required).

This exact command (`python tests/run_all.py` on Python 3.8) is also run
automatically in **GitHub Actions** on every push and pull request via the
`offline-tests` workflow (`.github/workflows/ci.yml`), so regressions are caught
in CI as well as locally. See the status badge at the top of this file.

---

## Hardware notes and limitations

- Default config keeps **all heavy learners OFF**. The live path uses indicators
  + the light ML classifier (with a pure-Python fallback) + news.
- Indicator math is **pure Python** (no NumPy required at runtime), so signals
  compute even if scientific wheels fail to install.
- LightGBM is preferred but optional; without it the bot uses scikit-learn and
  then a pure-Python logistic regression.
- TensorFlow/Keras generally **do not support Windows 7** and are heavy; the DL
  and transfer modules are off by default and clearly isolated. Only install
  TensorFlow on a separate capable training machine if you really need it.

---

## Honest notes: what is realistic

- This bot **does not rewrite its own source code**. "Self-improvement" means it
  searches many strategy/parameter combinations, walk-forward tests them,
  remembers what worked, and blends the best. That is a realistic, robust form
  of machine-driven improvement.
- A profitable backtest does **not** guarantee future profit. Markets change;
  overfitting is real. Use walk-forward results, out-of-sample periods, and the
  native MT5 tester, and treat drawdown seriously.
- News sentiment from free RSS is a **weak, noisy** signal. It is blended with a
  small weight and can be disabled. Do not expect it to predict spikes.
- The internal Python backtester is for **relative ranking during search**, not
  for final go-live decisions. Validate in the MT5 Strategy Tester.
- Start in **paper** mode, then a **demo** account, and only then consider live
  with small size.

---

## Risk disclaimer

Trading foreign exchange, metals, and CFDs carries a high level of risk and may
not be suitable for all investors. You can lose more than your initial deposit.
This software is provided for research and educational purposes **as-is**,
without any warranty. You are solely responsible for any trading decisions and
losses. Test thoroughly on a demo account before risking real capital.
