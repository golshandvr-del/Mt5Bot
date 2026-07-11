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
9. [The gauntlet: your pre-flight checklist](#the-gauntlet-your-pre-flight-checklist)
10. [Shadow validation: pulling a dead edge off live money](#shadow-validation-pulling-a-dead-edge-off-live-money)
11. [Chaos-monkey: does your edge survive a bad broker week?](#chaos-monkey-does-your-edge-survive-a-bad-broker-week)
12. [Exporting history and backtesting in MT5](#exporting-history-and-backtesting-in-mt5)
13. [Deploying on a VPS](#deploying-on-a-vps)
14. [Testing](#testing)
15. [Hardware notes and limitations](#hardware-notes-and-limitations)
16. [Honest notes: what is realistic](#honest-notes-what-is-realistic)
17. [Risk disclaimer](#risk-disclaimer)

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
- **Portability note (Windows 7 / old SQLite):** the memory store no longer
  depends on the SQLite **JSON1** extension. Ranking queries used to call the
  built-in `json_extract`, which is missing on some older SQLite builds and
  caused `no such function: json_extract` - the search would finish and store
  all its results, yet the registry came out empty (`"top": 0`). The store now
  registers its own equivalent `json_extract` on every connection, so ranking
  works on every SQLite build. If you hit this on an earlier version, just run
  `python main.py --mode rebuild-registry` once to populate the registry from
  the results you already collected - no need to re-run the search.

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
python main.py --mode search --resume  # continue an interrupted deep search (U4.6)
python main.py --mode rebuild-registry  # rebuild the registry from stored memory
python main.py --mode train    # Phase 1 offline learner training
python main.py --mode loop     # continuous paper/live loop (for a VPS)
python main.py --config other.yaml   # use an alternate config file
```

`--mode loop` accepts `--iterations N` (0 = forever) and `--sleep SECONDS`.

`--mode rebuild-registry` regenerates `strategy_registry.json` from the results
already stored in the memory DB (`data_store/memory.sqlite`), **without** running
a new search or connecting to MT5. Use it if a search finished but the registry
came out empty (for example after the old `no such function: json_extract`
error on a SQLite build without the JSON1 extension - see below): the thousands
of already-evaluated results are turned into a populated registry in seconds.

If a rebuild still reports `0 strategies`, it is **not** a crash - it means the
promotion filters rejected every candidate. The run now logs a `REASON ...` line
telling you which filter emptied it, and you can loosen the gates for the rebuild
**without editing `config.yaml`**:

```bash
# See exactly which filter empties each symbol (read-only, no changes):
python scripts/diagnose_registry.py

# Recover strategies the significance gate rejected (missing/high p-value):
python main.py --mode rebuild-registry --no-significance

# Recover strategies that simply did not trade enough:
python main.py --mode rebuild-registry --min-trades 10

# Or loosen the significance p-value threshold instead of disabling it:
python main.py --mode rebuild-registry --max-pvalue 0.2
```

These flags only affect the rebuild run; the stored results and `config.yaml`
are left untouched.

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

## Auditing a run

Every backtest and every paper/live decision now leaves human-readable
"receipts" so you can answer "why did it enter HERE, and what did it cost?" for
any trade. All artifacts are plain files under `backtests/` and `logs/`, written
with the Python standard library only (they open on a bare Windows 7 install
with no extra tools).

**1. Per-trade CSV + equity curve (from `--mode backtest`).**
A backtest records a FULL receipt per trade and writes two CSV files:

```
backtests/trades_<SYMBOL>_<TF>_<timestamp>.csv   # one row per closed trade
backtests/equity_<SYMBOL>_<TF>_<timestamp>.csv   # bar-indexed equity curve
```

The trades CSV has one row per trade with: entry/exit time, direction,
entry/exit price, SL, TP, `exit_reason` (`sl` / `tp` / `flip` / `eod`), gross
pnl, the cost paid split into `spread` / `commission` / `slippage` / `swap`,
`balance_after`, and the blended `signal` value at entry. The backtest report
(`backtests/backtest_report.json`) also carries a `config_snapshot` of the exact
effective settings used, so any run is reproducible.

**2. Single-file HTML report (`scripts/make_report.py`).**
Turn the trade CSV into ONE self-contained `.html` file (inline SVG charts, no
external dependencies or internet):

```bash
python scripts/make_report.py backtests/trades_XAUUSD_M15_<timestamp>.csv \
    --equity backtests/equity_XAUUSD_M15_<timestamp>.csv \
    --out backtests/report_XAUUSD.html --title "XAUUSD M15"
```

The report shows a summary table, an equity/drawdown chart, per-month PnL, the
10 worst trades, the exit-reason breakdown, and what share of gross PnL the costs
ate. Double-click the resulting `.html` to open it in any browser.

**3. Decision journal (paper/live).**
In `paper` and `live` mode the engine appends one JSON line per decision to
`logs/decisions_<YYYY-MM-DD>.jsonl` (UTC date), including each component's
contribution (every strategy's signal, the learner probability, the news score,
and the threshold used). Pretty-print the most recent decisions with the WHY:

```bash
python scripts/explain_decisions.py --n 20            # newest journal, last 20
python scripts/explain_decisions.py --date 2026-07-08 # a specific day
python scripts/explain_decisions.py --symbol XAUUSD   # filter by symbol
```

Each printed decision shows which components pushed the score over (or kept it
under) the entry threshold, so a "no trade" is as explainable as a trade.

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
- `decision.mode` (**`parity`** default, or `blend`), `decision.weights`
  (blend mode only) and entry thresholds. See the golden rule below.
- `backtest.*`: initial balance, cost model, sizing, report dir, and the
  **Phase U3 realism knobs** (`fill_policy`, `intrabar_policy`, `spread_model`,
  `sizing`, `min_stop_points`) - see "Simulation realism" below.

To run heavier on a capable training machine, set
`general.enable_heavy_compute: true` and enable `dl_classifier` / `rl_agent`.
Keep them **off** on the Windows 7 live machine.

---

## The golden rule: only trade what was validated

The single most important setting for anyone who validates in the MT5 Strategy
Tester is `decision.mode`:

- **`parity` (default, recommended).** The live/paper engine trades the **top-1
  strategy from the registry EXACTLY as it was walk-forward validated**: that
  strategy's own indicators, its own long/short thresholds, and its own SL/TP
  ATR multiples. The ML learner, news, and timing layers become **veto-only
  gates** - each may *block* an entry the validated strategy wanted (e.g. a news
  blackout, a learned-weak time window, or a learner that *strongly* disagrees),
  but none can ever create a new entry, flip its direction, or resize it. If no
  strategy has been promoted for a symbol, parity mode simply stays **flat**
  rather than guess. This guarantees *validated == traded*.
- **`blend` (legacy / research).** The engine averages the top-K strategies'
  continuous signals, re-blends that with the learner (`decision.weights.learning`)
  and news (`decision.weights.news`), and applies the global
  `decision.long_threshold` / `short_threshold` (default 0.60). This composite
  is powerful for research but is **not itself walk-forward validated**, so it
  can fire at moments none of the underlying strategies would have. Use it only
  when you understand that gap.

Veto behaviour in parity mode is tunable under `decision.parity_vetoes`
(`learner` / `news` / `timing`, all on by default) and
`decision.parity_learner_veto_level` (how strongly the learner must disagree
before it blocks a trade; default `0.5`, `0` disables the learner veto).

### The meta-labeling quality gate (optional, `decision.meta_label`)

There is one more, higher-leverage veto you can turn on: a **meta-labeling
filter**. Instead of predicting market *direction* (every indicator already
votes on that), it learns a much easier, more useful thing - *"given the
validated top strategy is about to fire **here**, will that particular trade
win?"* - from regime/context features only (volatility as ATR%, trend strength
as ADX, the FX session via hour-of-day, and the day of week, plus how strong
the firing was). It is a tiny pure-Python logistic regression, trained per
strategy fingerprint during `train` mode from that strategy's own historical
firings, and persisted to `models/meta_label.json`.

At decision time it is **veto-only**, exactly like the other parity gates: when
its predicted win-probability for the current firing drops below
`decision.meta_label.min_win_prob` (default `0.5`) it *blocks* the entry the
validated strategy wanted - it can never create, flip, or resize a trade. It is
**OFF by default** and degrades gracefully: a disabled, untrained, single-class,
or too-few-samples (`min_train_samples`, default 50) gate never vetoes, so the
light path is byte-for-byte unchanged until you deliberately enable it. To use
it: set `decision.meta_label.enabled: true`, run `train` once to fit the gate,
then run `paper`/`live` as usual (`decision.mode: parity`). Every decision then
logs `meta_win_prob=..` (and `veto_meta_label=1` when it blocks) for the
auditing tools. This is UPGRADE_PLAN item U6.1.

> Why this exists: a real tester run turned 10,000 into ~3-4k in a year because
> the live path (old blend) traded a composite that was never backtested, while
> the EA silently traded a crippled single-indicator version of the winner.
> Parity mode + the strict EA exporter (below) close both gaps. See
> `UPGRADE_PLAN.md` (diagnoses D1/D2) for the full story.

---

## Simulation realism - why the internal numbers are now pessimistic

The internal backtester used to be **silently optimistic** about execution
(diagnosis D3 in `UPGRADE_PLAN.md`): it filled entries at the signal bar's
*close*, never modelled the intrabar SL-vs-TP race pessimistically, used a
constant spread, and sized with a fixed lot while live sized by risk %. Those
four gaps made the internal equity curve look far better than the MT5 Strategy
Tester's. Phase U3 closes all four. **Every knob below now defaults to the
realistic (pessimistic) behavior**; the legacy optimistic behavior stays
reachable by explicit config for before/after sensitivity studies.

All settings live under `backtest.*` in `config/config.yaml`:

| Knob | Default (realistic) | Legacy / alternatives | What it does |
|------|--------------------|-----------------------|--------------|
| `fill_policy` | `next_open` | `signal_close` | **U3.1** Entries and signal-flip exits fill at the **next bar's open** + half-spread + slippage - what a real EA can actually do (it only acts on a new bar). `signal_close` restores same-bar-close fills. SL/TP always fill intrabar in both modes. |
| `intrabar_policy` | `pessimistic` | `optimistic`, `midpoint` | **U3.2** When one bar's range touches **both** the SL and the TP, `pessimistic` counts the **stop first** (worst case) for both longs and shorts. `optimistic` counts the take first; `midpoint` averages the two (sensitivity only). |
| `spread_model` | present (widens) | omit for constant spread | **U3.3** Session-aware spread. `base_points` is the normal spread; it is multiplied by `rollover_mult` during `rollover_hours_utc` (a list, or a `[start, end]` inclusive window that may wrap past midnight). `news_mult` is reserved for news-window widening. Omit the whole sub-block to keep a constant `spread_points`. |
| `sizing` | `risk_pct` | `fixed_lot` | **U3.4** `risk_pct` sizes each entry by `risk.risk_per_trade` of the **simulated equity** using the *same* formula as `RiskManager.position_size` (clamped to `risk.min_lot` / `risk.max_lot`), so the backtest and live equity curves share geometry. It also enforces the `risk.max_daily_loss` **circuit breaker** in simulation (no new entries once a day's realized loss crosses the limit). `fixed_lot` restores the constant `fixed_lot`. |
| `min_stop_points` | `0` (off) | any point count | **U3.5** Simulated entries whose SL sits **closer** than this many points are **rejected** (never opened), exactly as the MT5 tester rejects orders inside the broker's stop level. Set to your broker's XAUUSD stops-level (often ~30-50 points). |

Guarantees locked by `tests/test_realism.py` (U3.6): `next_open` fills are
**never** better than `signal_close`; `pessimistic <= midpoint <= optimistic`;
the rollover window only ever **costs more**; `risk_pct` respects the min/max
lot clamp; the daily breaker cuts the trade count; too-tight stops are rejected.

**Re-baseline.** After enabling the realistic defaults, re-run search + backtest
and archive the before/after metric deltas in
`backtests/realism_baseline.md` (a template is committed there) so you have a
written record of how much the pessimism moved the numbers on *your* data.

---

## Deep search profile - what 24 hours buys you

The defaults in `config.yaml` are the fast **~6h research profile** (a few
hundred random trials) - enough to sanity-check the pipeline. But the whole
point of the Phase U4 overhaul is that you can point the bot at years of real
gold history and let it grind for **12-24 hours** to surface strategies that are
robust, not lucky. Diagnosis **D4** in `UPGRADE_PLAN.md` was "the search is too
shallow": a short random sweep finds knife-edge overfits that die in the MT5
Strategy Tester. The deep profile fixes that with five cooperating mechanisms.

### Turning it on

All of these live under `memory.search` / `memory.walk_forward` in
`config/config.yaml`. The comments there are authoritative; the summary:

| Knob | Fast (default) | Deep (24h) | Why |
|------|----------------|-----------|-----|
| `memory.search.method` | `random` | `evolution` | **U4.2** Keep an elite pool and *breed* the next generation (mutate one param step, swap one indicator, nudge thresholds/exits, or cross two elites); ~60% bred, ~40% fresh random. Dedups by fingerprint so no spec is scored twice. Converges on what works instead of blind sampling. |
| `memory.search.max_trials` | `400` | `4000+` | Give evolution room to explore. On the deep profile the **wall-clock budget**, not the trial count, is the real stop. |
| `memory.search.time_budget_hours` | `0` (off) | `12`..`24` | **U4.1** Wall-clock cap. The run stops **cleanly** the moment the budget elapses - it ranks whatever it evaluated and writes the registry normally, so a long run never needs babysitting. |
| `memory.search.checkpoint.checkpoint_every` | `25` | `25` | **U4.6** Every N trials the search persists its state (seen fingerprints + trial count + elite pool) to `data_store/search_ckpt_<SYMBOL>_<TF>.json`. A reboot mid-run loses nothing. |
| `memory.search.stability.enabled` | `false` | `true` | **U4.3** Every would-be-promoted finalist is re-run `n_seeds` extra times (new bootstrap seed + jittered warmup); promotion requires a strictly positive rank score in **every** run. A spec that flips negative when the warmup slides a few bars is a fluke and is dropped. |
| `memory.search.neighborhood.enabled` | `false` | `true` | **U4.4** Each finalist is re-scored at up to `n_neighbors` parameter sets that each differ by ONE step; the registry ranks by `min(own_score, median_neighbor_score)`, so a knife-edge peak is demoted below a broad, robust plateau. |
| `memory.search.regime.enabled` | `false` | `true` | **U4.5** Each walk-forward segment is labelled by volatility tercile (low/mid/high) x trend/range, and a candidate is refused promotion if it **collapses in any single regime** (score below `floor_mult` x overall). No more "only makes money in a trending market" surprises. |
| `memory.walk_forward.min_segments` | `10` | `12+` | More out-of-sample windows = more regimes each strategy must survive. |

### Surviving a reboot: `--resume`

Because a 24h run *will* eventually hit a Windows update or a power blip, the
search checkpoints itself (**U4.6**). To continue an interrupted run exactly
where it stopped:

```bash
python main.py --mode search --resume
```

The resumed run restores the already-seen fingerprints (so **nothing is
re-evaluated**), continues the cumulative trial count and time budget, and
re-seeds the evolutionary elite pool so it keeps converging instead of starting
cold. A run that reaches `max_trials` finished cleanly and **clears** its
checkpoint automatically; only an interrupted run leaves one behind for
`--resume`. A corrupt or foreign checkpoint is ignored (the run just starts
fresh) - a bad checkpoint can never crash a long run.

### What the 24 hours actually buys

- **Depth**: evolution turns thousands of trials into a *guided* walk toward
  higher-scoring regions, not an unfocused random sweep.
- **Robustness, three ways**: the stability (U4.3), neighborhood (U4.4) and
  regime (U4.5) gates all sit on the **promotion** path. A strategy only enters
  the registry if it is stable across seeds, robust to small parameter nudges,
  **and** does not blow up in any single market regime. Fragile overfits are
  recorded in memory (for learning) but never promoted, never traded.
- **No wasted compute**: fingerprint dedup + checkpoint/resume mean a
  multi-day run is fully restartable and never re-scores work it already did.

These guarantees are locked by `tests/test_search_evolution_resume.py` (U4.7):
the evolutionary operators only ever emit in-param-space, pool-legal specs; the
time budget stops both the random and evolution paths cleanly before
`max_trials`; a resumed run provably never re-evaluates a seen fingerprint; and
a planted knife-edge / regime-collapsing fixture is provably demoted / rejected
by the neighborhood and regime gates.

> **The deep profile is not "trade this now".** It is "find candidates worth
> sending to the gauntlet (Phase U5) and then the MT5 Strategy Tester". Nothing
> here bypasses the golden rule: only trade what was validated end-to-end.

---

## The gauntlet: your pre-flight checklist

A strategy that looks good in search still has not *earned* live money. Phase U5
adds a single scripted **gauntlet** - a fixed sequence of pessimistic stress
tests - that the current registry **top-1** strategy must pass before the bot
will let you go live. It produces one human-readable verdict file you can read
in under a minute.

### Running it

```bash
python scripts/gauntlet.py --symbol XAUUSD --tf M15
# more Monte-Carlo shuffles / a custom warmup:
python scripts/gauntlet.py --symbol XAUUSD --tf M15 --mc 1000 --warmup 60
```

It reads only the memory registry + your price history (no search, no live
orders) and uses the *same pessimistic backtester* as everything else (Phase
U3), so its numbers are honest. It exits `0` on PASS and `1` on FAIL, so you can
chain it in a script.

### The five gates (all deliberately pessimistic)

| # | Gate | What it proves | Fails when |
|---|------|----------------|-----------|
| 1 | **Full-history backtest** | Net-profitable with positive expectancy over ALL bars under the pessimistic sim. | The raw edge is not there. |
| 2 | **Locked holdout** | Re-scored on the last `memory.walk_forward.holdout_bars` bars the search never saw - the edge survives out-of-sample. | It only worked on data it was tuned on. |
| 3 | **Monte-Carlo trade order** | Reshuffles the trade sequence (default 1000x) into 5%/95% equity envelopes, a max-drawdown distribution and a **risk-of-ruin** estimate. Requires final equity > start AND risk-of-ruin <= 5%. | The result depended on a *lucky ordering* of trades. |
| 4 | **Cost stress** | Re-runs with spread x1.5 and x2. The edge **must** survive x1.5 (x2 is informational). | The edge is really just an under-costed illusion. |
| 5 | **Worst-case start** | Equity over the worst rolling 3-month window is not catastrophic. | A bad entry point would have wiped you out. |

### The verdict file

The run writes `backtests/gauntlet_<fingerprint>.md` with a PASS/FAIL line per
gate, the reasoning, and the headline numbers (plus two machine-parseable
stamps: `created_at_epoch` and `overall_pass`). A FAIL prints exactly which gate
and why. Keep these verdicts - they are your written record of *why* a strategy
was ever allowed to trade.

### The live gate (mechanically enforced)

`general.live_requires_gauntlet` (**default `true`**) makes **live mode refuse to
start** unless a PASS verdict exists for the top-1 strategy of every traded
symbol/timeframe *and* that verdict is **newer than the last search** that built
the registry (`app/gauntlet_gate.py`). This closes the last loophole: a strategy
re-promoted after its last gauntlet run is treated as un-vetted until you re-run
the gauntlet. **Paper / backtest / search / train are never blocked**, and a
symbol with no promoted strategy does not block (there is nothing to trade). The
gate *fails loudly*: any error while checking is treated as a BLOCK, so a parsing
bug can never quietly let un-vetted money trade. Set the flag to `false` only if
you accept full responsibility for going live without a verdict.

> Going live is therefore **mechanically impossible** without a written,
> reproducible PASS - and you always know *why* a strategy was allowed.

---

## Shadow validation: pulling a dead edge off live money

The gauntlet proves a strategy *before* it trades. **Shadow validation (U6.5)**
watches it *while* it trades and yanks it off live money the moment it stops
working. It is the **hard** layer on top of the soft `decay_monitor`: the soft
monitor only zero-weights a drifting strategy inside a blend; shadow validation
pulls it off **real orders** entirely.

Run it OFFLINE - e.g. a weekend cron on the VPS:

```bash
python scripts/shadow_validate.py            # run per config
python scripts/shadow_validate.py --print    # also echo the report
python scripts/shadow_validate.py --list     # just list demoted strategies
python scripts/shadow_validate.py --force    # one-off run even if disabled
```

For every live strategy it re-scores the trailing live window
(`decision.shadow_validation.window` trades) against the walk-forward reference
distribution the strategy was promoted on, reusing the **same** `DecayMonitor`
maths - so "shadow-suspect" is exactly "decay-suspect". A strategy is **demoted
to paper** only when it has decayed below the decay threshold **and** has at
least `min_live_trades` of live evidence (never a demotion on noise). The
fingerprint is written to `data_store/demotions.json` with a plain-language
reason, and a Markdown report lands at `backtests/shadow_report.md`. A strategy
whose live window later **recovers** is auto-cleared when `clear_on_pass` is on.

The demotion is mechanically enforced on the live path: `OrderManager` refuses a
**real** order for any demoted fingerprint and forces it to paper instead (it
short-circuits before the broker connector is ever touched). Demotion is
**one-way safe** - shadow validation can only pull a decayed edge *off* live
money; it never promotes, edits, or trades. It is config-gated and
**default OFF** (`decision.shadow_validation.enabled`): with the gate off the
run is a no-op and the live path is byte-for-byte unchanged.

> A demoted strategy stays on paper until a fresh search **re-validates** it -
> a dead edge can never silently keep bleeding real money.

---

## Chaos-monkey: does your edge survive a bad broker week?

The gauntlet and shadow validation both assume the broker behaves *normally*.
Real brokers do not: they requote your entry, drop bars from the feed, blow the
spread wide open at news/rollover, and fill only part of your requested size.
The **chaos-monkey harness (U6.6)** answers one blunt question:

> *If the broker misbehaves for a week, does my edge survive or evaporate?*

It is a **purely offline stress diagnostic** - it never trades, never promotes,
never edits the registry. It re-scores every registry strategy twice: once on
the **clean** price history, and once on a **copy** of that history into which
"broker nastiness" has been injected, then classifies each strategy:

- **GRACEFUL** - keeps `>= graceful_floor_mult` of its clean net profit (40% by
  default) **and** stays positive under chaos. Trust it.
- **FRAGILE** - still positive but loses more than the graceful floor of its
  edge. A warning: the edge is thinner than it looks.
- **SHATTERED** - goes non-positive (or below the catastrophe floor) the moment
  the broker misbehaves. **Do not trust it live.**

Four independently switchable nastiness injectors (all **default OFF**):

| Injector | What it models | How it is injected |
| -------- | -------------- | ------------------ |
| `spread_storm` | spread widens 2x+ at news/rollover | multiplies the cost model's spread |
| `requotes` | broker requotes your entry to a worse price | jitters a fraction of bar *opens* (+/- points) |
| `missed_bars` | data-feed gap / a bar the EA never saw | drops a fraction of non-edge bars |
| `partial_fills` | broker fills only part of your size | scales the effective lot down |

Every random choice is driven by a fixed `seed`, so a chaos verdict regenerates
**byte-for-byte** - a SHATTERED verdict is reproducible, not a one-off unlucky
roll.

### Running it

```bash
python scripts/chaos_monkey.py                    # run per config
python scripts/chaos_monkey.py --symbol XAUUSD --tf M15
python scripts/chaos_monkey.py --force            # one-off run even if disabled
python scripts/chaos_monkey.py --all-nastiness    # turn every injector on
python scripts/chaos_monkey.py --top 10 --print   # top-10 only, echo the report
```

With `general.chaos_monkey.enabled: false` (the default) the harness is a pure
no-op. `--force` runs it anyway, and if no injector is configured it turns them
all on - so a one-off "how robust is my registry?" check needs no config edit.
It writes one human-readable report to `backtests/chaos_report.md` (plus a
`chaos_report.json`) with a per-strategy verdict table and the exact injected
nastiness. All knobs live under `general.chaos_monkey` in `config.yaml`
(intensities, classification thresholds); everything is Win7 / Py3.8 / CPU-only
and stdlib-only.

> Chaos-monkey is a **diagnostic, not a gate** - it never blocks a run. Use it
> to *stop trusting* strategies that only look good under a polite broker.

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

**The EA trades the same signal the search validated (parity).** The EA now
implements **eight directional indicators natively** - `ema`, `sma`, `rsi`,
`macd`, `adx`, `supertrend`, `bbands`, and `stoch` - each mapped bar-for-bar to
the exact Python signal math (`core/indicators/*.py`). Two guards keep this
honest:

- **Strict export (default).** `scripts/export_strategy_for_ea.py` refuses to
  write a params file if the winning strategy uses ANY indicator the EA cannot
  reproduce, instead of silently shipping a crippled version. Pass
  `--allow-partial` only for experiments; it then rescales the surviving weights
  and stamps a loud WARNING into the `.params` header. To guarantee every
  promoted strategy is exportable 1:1, set `memory.search.ea_compatible_only:
  true` so the search only ever draws from the EA-supported set.
- **Automated parity harness.** `tests/test_parity_harness.py` diffs a
  line-by-line Python port of the EA's `BlendedSignal()` against the real
  `Strategy` on a shared fixture and fails if they drift by more than `1e-6`, so
  a sign/edge-case bug on one side (the class of bug behind the original tester
  loss) is caught in CI. For an end-to-end check against the *real* compiled
  MQL5, run `experts/ParityDump.mq5` in MT5 on the fixture produced by
  `python scripts/parity_fixture.py`, drop its output at
  `tests/fixtures/parity_ea.csv`, and re-run the test - it then diffs the actual
  EA output bar-by-bar.

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
