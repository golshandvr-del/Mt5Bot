"""
Performance metrics for backtests (Phase 3).

Given a list of closed trades (each with a profit-and-loss value) and an equity
curve, compute the standard set of metrics used to rank strategies in memory:
  - num_trades
  - win_rate
  - profit_factor      = gross_profit / gross_loss
  - expectancy         = average PnL per trade
  - max_drawdown       = largest peak-to-trough equity drop (fraction)
  - sharpe             = mean(returns) / std(returns) (per-trade, unannualized)
  - net_profit
  - average_win / average_loss

It also provides two pure-Python building blocks for the statistical-
significance filter (Phase P2 / A3), used to keep small-sample strategies
honest:
  - `wilson_interval(wins, n, z)`  Wilson score confidence interval for win-rate
  - `bootstrap_pvalue(trade_pnls, n_boot, seed)`  seeded bootstrap p-value that
    the mean trade PnL is <= 0 (no positive edge)

All pure Python.

All text is standard ASCII English only.
"""

from __future__ import annotations

import random
from typing import Any, Dict, List, Tuple


def wilson_interval(wins: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    """
    Wilson score confidence interval for a binomial proportion (win-rate).

    Given `wins` successes out of `n` trials, return the (low, high) bounds of
    the confidence interval for the true win-rate at confidence level implied by
    `z` (default z=1.96 ~ 95%). Unlike the naive normal interval, the Wilson
    interval stays inside [0, 1] and behaves sensibly for small `n`, which is
    exactly the small-sample regime this project needs to be honest about.

    Pure Python (uses only ** for the square root). Edge cases:
      - n <= 0            -> (0.0, 0.0) (no information).
      - wins clamped to [0, n] defensively.
      - z <= 0            -> the point estimate (p_hat, p_hat).
    The returned bounds are always clamped to [0.0, 1.0] with low <= high.
    """
    if n <= 0:
        return (0.0, 0.0)
    if wins < 0:
        wins = 0
    if wins > n:
        wins = n

    p_hat = wins / n
    if z <= 0.0:
        return (p_hat, p_hat)

    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p_hat + z2 / (2.0 * n)) / denom
    margin = (z / denom) * ((p_hat * (1.0 - p_hat) / n
                             + z2 / (4.0 * n * n)) ** 0.5)

    low = center - margin
    high = center + margin
    # Clamp to a valid probability range; keep low <= high defensively.
    low = 0.0 if low < 0.0 else (1.0 if low > 1.0 else low)
    high = 0.0 if high < 0.0 else (1.0 if high > 1.0 else high)
    if low > high:
        low, high = high, low
    return (low, high)


def bootstrap_pvalue(trade_pnls: List[float], n_boot: int = 1000,
                     seed: int = 42) -> float:
    """
    Bootstrap p-value for the null hypothesis "mean trade PnL <= 0".

    We resample the observed trade PnLs WITH REPLACEMENT `n_boot` times, each
    resample the same size as the original series, and compute each resample's
    mean. The p-value is the fraction of bootstrap means that are <= 0, i.e. how
    often the strategy shows NO positive edge under resampling. A small p-value
    (e.g. <= 0.05) means the positive edge is unlikely to be a fluke; a large
    p-value means the strategy is not distinguishable from break-even/random.

    Deterministic: uses a private `random.Random(seed)` so results never depend
    on global RNG state and are reproducible under the project's global seed
    (config `general.random_seed`, default 42). Pure Python.

    Edge cases (conservative - "cannot prove an edge" -> p-value 1.0):
      - empty series or n_boot <= 0 -> 1.0.
      - a single non-positive trade  -> 1.0 (never < 0.5 without evidence).
    """
    n = len(trade_pnls)
    if n == 0 or n_boot <= 0:
        return 1.0

    rng = random.Random(seed)
    pnls = list(trade_pnls)
    le_zero = 0
    for _ in range(n_boot):
        total = 0.0
        for _ in range(n):
            total += pnls[rng.randrange(n)]
        if (total / n) <= 0.0:
            le_zero += 1
    return le_zero / n_boot


def compute_metrics(trade_pnls: List[float],
                    equity_curve: List[float]) -> Dict[str, Any]:
    """Compute the metric dictionary for a finished backtest."""
    n = len(trade_pnls)
    wins = [p for p in trade_pnls if p > 0]
    losses = [p for p in trade_pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)  # positive number
    net_profit = sum(trade_pnls)

    win_rate = (len(wins) / n) if n > 0 else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (
        float(gross_profit) if gross_profit > 0 else 0.0
    )
    expectancy = (net_profit / n) if n > 0 else 0.0
    average_win = (gross_profit / len(wins)) if wins else 0.0
    average_loss = (-gross_loss / len(losses)) if losses else 0.0

    # Max drawdown from the equity curve.
    max_dd = 0.0
    peak = equity_curve[0] if equity_curve else 0.0
    for value in equity_curve:
        if value > peak:
            peak = value
        if peak > 0:
            dd = (peak - value) / peak
            if dd > max_dd:
                max_dd = dd

    # Per-trade Sharpe (unannualized): mean / std of trade PnLs.
    sharpe = 0.0
    if n > 1:
        mean = net_profit / n
        var = sum((p - mean) ** 2 for p in trade_pnls) / (n - 1)
        std = var ** 0.5
        sharpe = (mean / std) if std > 0 else 0.0

    return {
        "num_trades": n,
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 4),
        "expectancy": round(expectancy, 6),
        "net_profit": round(net_profit, 4),
        "max_drawdown": round(max_dd, 4),
        "sharpe": round(sharpe, 4),
        "average_win": round(average_win, 4),
        "average_loss": round(average_loss, 4),
    }


def rank_value(metrics: Dict[str, Any], rank_metric: str) -> float:
    """
    Return a single comparable score for ranking. For max_drawdown, lower is
    better, so it is negated. For all other metrics, higher is better.
    """
    value = float(metrics.get(rank_metric, 0.0) or 0.0)
    if rank_metric == "max_drawdown":
        return -value
    return value
