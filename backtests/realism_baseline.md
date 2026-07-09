# Realism re-baseline (Phase U3)

> Purpose: record how much the Phase U3 pessimistic-simulation defaults moved the
> internal backtest numbers on YOUR data, so "before" (optimistic legacy) and
> "after" (realistic default) are on the record and the internal curve can be
> compared to the real MT5 Strategy Tester.
>
> This file is a TEMPLATE. Re-run search + backtest twice on the same history
> (once with the legacy optimistic config, once with the realistic defaults),
> then paste the metrics into the tables below. Keep it in git so every
> re-baseline is auditable. ASCII English only.

## How to reproduce

The realistic defaults (`config/config.yaml` -> `backtest.*`) are:

```yaml
backtest:
  fill_policy: "next_open"        # was: signal_close (optimistic)
  intrabar_policy: "pessimistic"  # was: optimistic
  sizing: "risk_pct"              # was: fixed_lot
  min_stop_points: 0              # set to your broker stops-level, e.g. 40
  spread_model:                   # was: constant spread_points
    base_points: 25
    rollover_mult: 4.0
    rollover_hours_utc: [21, 23]
    news_mult: 1.0
```

To produce the "before" (legacy optimistic) column, temporarily set:

```yaml
backtest:
  fill_policy: "signal_close"
  intrabar_policy: "optimistic"
  sizing: "fixed_lot"
  min_stop_points: 0
  # remove / comment out the spread_model sub-block so spread stays constant
```

Run each configuration end to end (same symbol, timeframe, and history):

```
python main.py --mode search    --symbol XAUUSD --tf M15
python main.py --mode backtest  --symbol XAUUSD --tf M15
python scripts/make_report.py --trades backtests/trades_XAUUSD_M15_<ts>.csv
```

Open the generated single-file HTML report for each run and copy the summary.

## Metric deltas (fill in per instrument)

### XAUUSD M15

| Metric | Legacy optimistic (before) | Realistic pessimistic (after) | Delta |
|--------|---------------------------:|------------------------------:|------:|
| Net profit         |  |  |  |
| Profit factor      |  |  |  |
| Expectancy / trade |  |  |  |
| Win rate           |  |  |  |
| Max drawdown       |  |  |  |
| Num trades         |  |  |  |
| Total costs paid   |  |  |  |

### (add more symbols / timeframes as needed)

| Metric | Before | After | Delta |
|--------|-------:|------:|------:|
| Net profit |  |  |  |

## Cross-check against the MT5 Strategy Tester

After exporting the promoted strategy to the EA (`scripts/export_strategy_for_ea.py`,
strict mode) and running the native MT5 Strategy Tester on the SAME history,
record the tester's headline numbers here and confirm the internal "after"
column is within a sensible tolerance of - and never wildly above - the tester.

| Metric | Internal (realistic) | MT5 Strategy Tester | Within tolerance? |
|--------|---------------------:|--------------------:|:-----------------:|
| Net profit    |  |  |  |
| Profit factor |  |  |  |
| Max drawdown  |  |  |  |

Acceptance (UPGRADE_PLAN U3): on the same spec, internal-backtest net profit is
within a documented tolerance of - and never wildly above - the MT5 Strategy
Tester. Note the tolerance you consider acceptable and the observed gap.

---

_Last re-baseline run: (date) — (who) — (git commit)_
