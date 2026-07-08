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

- [ ] U1.1 (code) `Backtester.run(record_trades=True)` becomes the default in
      backtest mode and records FULL trade records: entry_ts, exit_ts,
      direction, entry/exit price, SL, TP, exit_reason (sl/tp/flip/eod),
      pnl, costs paid (spread/commission/slippage/swap separately),
      balance_after, and the blended signal value at entry.
- [ ] U1.2 (code) New `core/utils/trade_log.py`: writes
      `backtests/trades_<SYMBOL>_<TF>_<timestamp>.csv` plus
      `equity_<...>.csv` (bar-indexed equity curve). Pure stdlib csv.
- [ ] U1.3 (code) New `scripts/make_report.py`: renders a single-file HTML
      report (no external deps, inline SVG for the equity/drawdown chart)
      from the trade CSV: summary table, per-month PnL, top-10 worst trades,
      exit-reason breakdown, cost share of PnL. Runs on Win7 offline.
- [ ] U1.4 (code) Decision explainer: `Decision.reasons` is extended to carry
      per-component values (each strategy's signal, learner prob, news score,
      threshold used) and paper/live append one JSON line per decision to
      `logs/decisions_<date>.jsonl`. A new `scripts/explain_decisions.py`
      pretty-prints the last N decisions with WHY (which components pushed it
      over/under the threshold).
- [ ] U1.5 (code) Backtest report gains a `config_snapshot` section (the
      exact effective config values used) so every artifact is reproducible.
- [ ] U1.6 (test) Offline tests: trade CSV row count == num_trades metric;
      costs in the CSV sum to the metrics' implied total cost; HTML report
      builds from a synthetic run without exceptions.
- [ ] U1.7 (docs) README section "Auditing a run"; sync CODE_MAP/Ideas.

Acceptance: after any backtest the user opens ONE html file and can point at
any trade and see when/why/what-it-cost. Nothing about trading changed yet.

---

## Phase U2 - Parity: validated == traded (fixes D1 + D2)

Goal: kill both parity gaps. This is the phase that directly addresses the
"robot behaved stupidly in the Strategy Tester" incident.

- [ ] U2.1 (code) EA export HARD GUARD: `export_strategy_for_ea.py` gains
      `--strict` (DEFAULT ON): if the spec contains ANY indicator the EA does
      not implement, the export FAILS with a clear message listing them,
      instead of silently exporting a crippled strategy. `--allow-partial`
      keeps the old behavior for experiments, and then it also rescales the
      remaining weights AND writes a prominent warning block into the .params
      file header.
- [ ] U2.2 (code) EA-compatible search mode: new config
      `memory.search.ea_compatible_only` (default false). When true the
      random/grid/evolutionary spec generators draw ONLY from the EA-supported
      indicator set, so anything promoted is exportable 1:1. RECOMMENDED
      workflow for anyone who validates in the MT5 tester.
- [ ] U2.3 (code) Alternatively grow the EA: implement supertrend, bbands,
      stoch and candle_patterns(subset) in MQL5 to widen the exportable set.
      Each new EA indicator ships with a cross-check fixture (see U2.6).
- [ ] U2.4 (code+config) Live parity mode: new config
      `decision.mode: "parity" | "blend"` (default "parity").
      In parity mode the engine trades the TOP-1 registry strategy EXACTLY as
      validated: same indicators, same per-spec thresholds, same SL/TP mults.
      The learner/news/timing become optional VETO-ONLY gates (they can block
      an entry, never create or resize one). "blend" keeps the current
      behavior for research.
- [ ] U2.5 (code) If ensemble blending stays in use ("blend" mode), the
      ensemble composite itself MUST be walk-forward evaluated: add
      `scripts/validate_ensemble.py` that replays the exact engine blend
      (ensemble avg + weights + 0.60 threshold) through the backtester and
      writes the same artifacts as U1. No unvalidated composite may go live.
- [ ] U2.6 (test) Python-vs-EA signal parity harness: `scripts/export_history`
      fixture + a small MQL5 script dump per-bar BlendedSignal values for the
      same params; a Python test asserts max abs diff < 1e-6 on the shared
      indicator set. Catches sign/edge-case drift (RSI/MACD bugs of the past).
- [ ] U2.7 (docs) README: "The golden rule - only trade what was validated";
      document parity mode and the strict exporter; sync CODE_MAP/Ideas.

Acceptance: it is IMPOSSIBLE to (a) export a partial strategy without an
explicit override, and (b) run paper/live logic that was never backtested.

---

## Phase U3 - Pessimistic, realistic simulation (fixes D3)

Goal: internal numbers move much closer to MT5-tester numbers, always erring
against the strategy.

- [ ] U3.1 (code+config) Next-bar execution: `backtest.fill_policy:
      "next_open"` (new DEFAULT) fills entries at the NEXT bar's open plus
      half-spread and slippage; "signal_close" keeps the old behavior for
      comparison. Exits on signal-flip also fill at next open; SL/TP still
      fill intrabar.
- [ ] U3.2 (code+config) Intrabar ambiguity: `backtest.intrabar_policy:
      "pessimistic"` (new DEFAULT) - when one bar touches both SL and TP,
      count the STOP as hit first for both directions. "optimistic" and
      "midpoint" available for sensitivity analysis.
- [ ] U3.3 (code+config) Session-aware spread: `backtest.spread_model:
      {base_points: 25, rollover_mult: 4.0, rollover_hours_utc: [21,23],
      news_mult: 1.0}` - spread widens during the configured rollover window
      (and optionally around news blackout windows already known to Phase 4).
      Default reproduces a constant spread when mults are 1.0.
- [ ] U3.4 (code+config) Risk-based sizing in backtest: `backtest.sizing:
      "fixed_lot" | "risk_pct"` (default "risk_pct" to MATCH live). Uses the
      same formula as `RiskManager.position_size` incl. min/max lot clamping,
      so backtest and live equity curves share geometry. Includes the
      max_daily_loss circuit breaker in simulation.
- [ ] U3.5 (code) Margin/stop-distance sanity: reject simulated entries whose
      SL distance is < broker min stop distance (configurable points) - the
      MT5 tester rejects those orders and the internal sim must too.
- [ ] U3.6 (test) Fixtures proving: next_open fills are never better than
      signal_close fills; pessimistic intrabar never beats optimistic;
      risk_pct sizing respects min/max lot and the daily circuit breaker.
- [ ] U3.7 (docs) README "Simulation realism" section + config reference;
      sync CODE_MAP/Ideas. Re-baseline: re-run search+backtest and archive the
      before/after metric deltas in `backtests/realism_baseline.md`.

Acceptance: on the same spec, internal-backtest net profit is within a
documented tolerance of (and never wildly above) the MT5 Strategy Tester.

---

## Phase U4 - Spend the budget: deep, smart search (fixes D4)

Goal: a 12-24h search that finds ROBUST strategies, not fast lucky ones.
Everything scales via config; the 6h profile remains available.

- [ ] U4.1 (config) New "deep" profile documented in config.yaml comments:
      max_trials 400 -> 4000+, n_boot 1000 -> 5000, holdout_bars 0 -> 15000
      (last ~6 months of M15 locked), min_segments 10 -> 12. Add
      `memory.search.time_budget_hours: 0` (0=off) - stop cleanly and rank
      whatever was evaluated when the budget expires.
- [ ] U4.2 (code) Evolutionary search (supersedes structure.md P6.5): keep an
      elite pool (top ~10%); generate 60% of new specs by mutating/crossing
      elites (jitter one param step, swap one indicator, +/-0.05 thresholds)
      and 40% fresh random for exploration. Dedup via fingerprints. Pure
      Python, CPU-light.
- [ ] U4.3 (code) Multi-seed stability gate: every candidate that would enter
      the registry is re-run with 3 different bootstrap seeds and
      jittered warmup; promotion requires the rank score to stay positive in
      ALL runs. Kills knife-edge flukes cheaply (only finalists pay the cost).
- [ ] U4.4 (code) Parameter-neighborhood robustness: for each finalist,
      evaluate 8 neighbors (each key param nudged one step). New metric
      `neighborhood_score` = median neighbor score; registry ranks by
      min(own_score, neighborhood_score). A strategy that dies when RSI period
      moves 14->15 is overfit BY DEFINITION and must not be promoted.
- [ ] U4.5 (code) Regime-sliced validation: label each walk-forward segment
      by realized volatility tercile (low/mid/high ATR%) and trend strength
      (ADX median). Registry entries store per-regime scores; promotion
      requires not losing catastrophically in any regime (configurable floor,
      e.g. per-regime expectancy > -0.5 * overall expectancy).
- [ ] U4.6 (code) Search checkpointing: persist elite pool + trial count every
      N trials so a 24h run survives a reboot and can resume with
      `--resume`. (SQLite already persists results; this adds the search
      state itself.)
- [ ] U4.7 (test) Evolution respects param spaces; time budget stops cleanly;
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

- 2026-07-08 Initial version. Derived from full code review + the user's
  real-world XAUUSD tester loss report. Root causes D1-D5 documented;
  six phases U1-U6 defined with steps and acceptance criteria.
