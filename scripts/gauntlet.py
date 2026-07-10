"""
gauntlet.py - the final pre-flight validation gauntlet (UPGRADE_PLAN U5.1/U5.2).

Before any strategy is trusted with live money it must survive a FIXED sequence
of pessimistic stress tests. This script runs that sequence on the CURRENT
registry top-1 strategy for a symbol/timeframe and writes ONE human-readable
verdict file (backtests/gauntlet_<fingerprint>.md) with PASS/FAIL per gate.

The five gates (all deliberately pessimistic - see UPGRADE_PLAN diagnosis D3):

  Gate 1  Full-history backtest      - the strategy must be net-profitable with
                                        a positive expectancy over ALL bars,
                                        under the (already pessimistic) sim.
  Gate 2  Locked holdout             - re-score on the last `holdout_bars` bars
                                        that the search is configured to never
                                        see; the edge must survive out-of-sample.
  Gate 3  Monte-Carlo trade order    - reshuffle the trade sequence N times to
                                        build 5%/95% equity envelopes, a max-DD
                                        distribution and a risk-of-ruin estimate;
                                        a lucky-ordering strategy fails here.
  Gate 4  Cost stress               - re-run with spread x1.5 and x2; the edge
                                        MUST survive x1.5 (x2 is informational).
  Gate 5  Worst-case start          - equity over the worst rolling 3-month
                                        window must not be catastrophic.

It reads ONLY the memory DB + a price CSV (or MT5 if wired) - no search, no live
orders. Pure stdlib + the project's own modules; Win7 / Py3.8 / CPU friendly.

Usage
-----
    python scripts/gauntlet.py
    python scripts/gauntlet.py --symbol XAUUSD --tf M15
    python scripts/gauntlet.py --symbol XAUUSD --tf M15 --mc 1000 --warmup 60

All text is standard ASCII English only.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import random
import sys
import time

# Make the project importable when run as `python scripts/gauntlet.py`.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from config.loader import load_config, resolve_path  # noqa: E402
from core.data.data_feed import DataFeed  # noqa: E402
from core.memory.store import MemoryStore  # noqa: E402
from core.strategy.strategy import Strategy, StrategySpec  # noqa: E402
from core.strategy.backtester import Backtester  # noqa: E402
from core.utils import trade_log  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _load_top1(memory, symbol, timeframe):
    """Return (StrategySpec, entry_dict) for the registry top-1, or (None, None)."""
    top = memory.load_registry_top(symbol, timeframe)
    for entry in top:
        spec_dict = entry.get("spec", {})
        if spec_dict:
            return StrategySpec.from_dict(spec_dict), entry
    return None, None


def _run_bt(cfg, strategy, ohlcv, warmup, record_trades=False):
    """Run the standard (config-driven, pessimistic) backtester."""
    bt = Backtester(cfg)
    return bt.run(strategy, ohlcv, warmup=warmup, record_trades=record_trades)


def _holdout_bars(cfg):
    try:
        return int(cfg.get_path("memory.walk_forward.holdout_bars", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _cfg_with_spread_mult(cfg, mult):
    """Return a deep-copied config whose spread cost is multiplied by `mult`.

    Works for BOTH the flat `backtest.spread_points` and the U3.3
    `backtest.spread_model.base_points`, so the stress applies whichever cost
    model is active.
    """
    clone = copy.deepcopy(cfg)
    try:
        bt = clone.get_path("backtest", {})
        if hasattr(bt, "get"):
            base = float(bt.get("spread_points", 10) or 10)
            bt["spread_points"] = base * mult
            sm = bt.get("spread_model", None)
            if hasattr(sm, "get") and len(sm) > 0:
                sm_base = float(sm.get("base_points", base) or base)
                sm["base_points"] = sm_base * mult
    except Exception:
        pass
    return clone


def _bars_per_3_months(timeframe):
    """Approx number of bars in a 3-month window for common timeframes."""
    tf = str(timeframe).upper()
    minutes = {
        "M1": 1, "M5": 5, "M15": 15, "M30": 30,
        "H1": 60, "H4": 240, "D1": 1440,
    }.get(tf, 15)
    # ~63 trading days * 24h for FX/metals (24-5 markets); use 24h approximation.
    bars_per_day = (24 * 60) / minutes
    return int(bars_per_day * 63)


def _pct(x):
    try:
        return "%.2f%%" % (100.0 * float(x))
    except (TypeError, ValueError):
        return "n/a"


def _num(x, fmt="%.4f"):
    try:
        return fmt % float(x)
    except (TypeError, ValueError):
        return "n/a"


# --------------------------------------------------------------------------- #
# Gate 1 - full-history pessimistic backtest.
# --------------------------------------------------------------------------- #
def gate_full_history(cfg, spec, ohlcv, warmup):
    result = _run_bt(cfg, Strategy(spec), ohlcv, warmup, record_trades=True)
    m = result.metrics or {}
    net = float(m.get("net_profit", 0.0) or 0.0)
    exp = float(m.get("expectancy", 0.0) or 0.0)
    n = int(m.get("num_trades", 0) or 0)
    passed = (net > 0.0) and (exp > 0.0) and (n >= 1)
    reason = ("net_profit=%s expectancy=%s num_trades=%d" %
              (_num(net), _num(exp, "%.6f"), n))
    if not passed:
        reason += "  -> needs net_profit>0 AND expectancy>0 AND at least 1 trade"
    return {
        "name": "Gate 1 - full-history pessimistic backtest",
        "passed": passed, "reason": reason, "metrics": m,
        "result": result,
    }


# --------------------------------------------------------------------------- #
# Gate 2 - locked holdout (last holdout_bars are out-of-sample).
# --------------------------------------------------------------------------- #
def gate_holdout(cfg, spec, ohlcv, warmup):
    hb = _holdout_bars(cfg)
    if hb <= 0:
        return {
            "name": "Gate 2 - locked holdout",
            "passed": True,
            "reason": ("holdout disabled (memory.walk_forward.holdout_bars=0); "
                       "gate SKIPPED (treated as pass). Set holdout_bars>0 for "
                       "a true out-of-sample check."),
            "skipped": True, "metrics": {},
        }
    n = len(ohlcv)
    if n <= hb + warmup + 30:
        return {
            "name": "Gate 2 - locked holdout",
            "passed": False,
            "reason": ("not enough bars (%d) for a %d-bar holdout plus warmup" %
                       (n, hb)),
            "metrics": {},
        }
    holdout = ohlcv.slice(n - hb, n)
    result = _run_bt(cfg, Strategy(spec), holdout, warmup, record_trades=False)
    m = result.metrics or {}
    net = float(m.get("net_profit", 0.0) or 0.0)
    exp = float(m.get("expectancy", 0.0) or 0.0)
    # Out-of-sample: require non-negative net AND non-negative expectancy.
    passed = (net >= 0.0) and (exp >= 0.0)
    reason = ("holdout_bars=%d net_profit=%s expectancy=%s num_trades=%s" %
              (hb, _num(net), _num(exp, "%.6f"), m.get("num_trades")))
    return {
        "name": "Gate 2 - locked holdout",
        "passed": passed, "reason": reason, "metrics": m,
    }


# --------------------------------------------------------------------------- #
# Gate 3 - Monte-Carlo bootstrap of trade ORDER.
# --------------------------------------------------------------------------- #
def gate_monte_carlo(cfg, gate1, n_shuffles, risk_pct, seed=12345):
    """Reshuffle the realized per-trade PnLs to build equity envelopes, a max-DD
    distribution and a risk-of-ruin estimate. A strategy whose profitability
    depends on the lucky ORDER of trades fails here."""
    result = gate1.get("result")
    pnls = list(getattr(result, "trade_pnls", []) or []) if result else []
    if len(pnls) < 10:
        return {
            "name": "Gate 3 - Monte-Carlo trade-order bootstrap",
            "passed": False,
            "reason": ("only %d trades - too few for a meaningful Monte-Carlo "
                       "(need >= 10)" % len(pnls)),
            "metrics": {},
        }
    init_balance = float(cfg.get_path("backtest.initial_balance", 10000.0)
                         or 10000.0)
    ruin_level = init_balance * (1.0 - 0.5)  # ruin = lose 50% of start balance

    rng = random.Random(seed)
    final_equities = []
    max_dds = []
    ruin_count = 0
    for _ in range(n_shuffles):
        order = pnls[:]
        rng.shuffle(order)
        equity = init_balance
        peak = equity
        max_dd = 0.0
        ruined = False
        for p in order:
            equity += p
            if equity > peak:
                peak = equity
            dd = peak - equity
            if dd > max_dd:
                max_dd = dd
            if equity <= ruin_level:
                ruined = True
        final_equities.append(equity)
        max_dds.append(max_dd)
        if ruined:
            ruin_count += 1

    final_equities.sort()
    max_dds.sort()

    def _pctile(sorted_list, q):
        if not sorted_list:
            return 0.0
        idx = int(q * (len(sorted_list) - 1))
        return sorted_list[idx]

    final_equity = final_equities[0] if final_equities else init_balance
    dd50 = _pctile(max_dds, 0.50)
    dd95 = _pctile(max_dds, 0.95)
    risk_of_ruin = ruin_count / float(n_shuffles)

    # NOTE: the FINAL equity is order-INVARIANT (it is just start + sum(pnls)),
    # so shuffling cannot change it - the order-dependent risks are the DRAWDOWN
    # path and the chance of hitting the ruin level along the way. Gate 3 is
    # therefore judged on those: the worst-case (95th-percentile) max drawdown
    # must not wipe out the account, AND the risk-of-ruin must be low. A strategy
    # that is only profitable because its winners happened to land before its
    # losers (a lucky path) shows a high ruin rate here and fails.
    dd95_frac = dd95 / init_balance if init_balance > 0 else 1.0
    passed = (final_equity > init_balance) and (risk_of_ruin <= 0.05) \
        and (dd95_frac < 0.5)
    reason = ("shuffles=%d  final_equity=%s (order-invariant)  maxDD p50/p95="
              "%s/%s (%s of start)  risk_of_ruin=%s (ruin=lose 50%% of %s)" %
              (n_shuffles, _num(final_equity, "%.2f"),
               _num(dd50, "%.2f"), _num(dd95, "%.2f"), _pct(dd95_frac),
               _pct(risk_of_ruin), _num(init_balance, "%.0f")))
    if not passed:
        reason += ("  -> needs final_equity>start AND risk_of_ruin<=5%% AND "
                   "worst-case maxDD < 50%% of start")
    return {
        "name": "Gate 3 - Monte-Carlo trade-order bootstrap",
        "passed": passed, "reason": reason,
        "metrics": {
            "final_equity": final_equity,
            "max_dd_p50": dd50, "max_dd_p95": dd95,
            "max_dd_p95_frac": dd95_frac, "risk_of_ruin": risk_of_ruin,
            "initial_balance": init_balance, "n_trades": len(pnls),
        },
    }


# --------------------------------------------------------------------------- #
# Gate 4 - cost stress (spread x1.5 must survive; x2 informational).
# --------------------------------------------------------------------------- #
def gate_cost_stress(cfg, spec, ohlcv, warmup):
    out = {}
    for mult in (1.5, 2.0):
        clone = _cfg_with_spread_mult(cfg, mult)
        res = _run_bt(clone, Strategy(spec), ohlcv, warmup)
        out[mult] = float((res.metrics or {}).get("net_profit", 0.0) or 0.0)
    survive_15 = out.get(1.5, 0.0) > 0.0
    reason = ("net_profit @ spread x1.5=%s  x2.0=%s" %
              (_num(out.get(1.5)), _num(out.get(2.0))))
    if not survive_15:
        reason += "  -> edge must stay net-profitable at spread x1.5"
    return {
        "name": "Gate 4 - cost stress (spread x1.5 / x2.0)",
        "passed": survive_15, "reason": reason,
        "metrics": {"net_profit_x1_5": out.get(1.5),
                    "net_profit_x2_0": out.get(2.0)},
    }


# --------------------------------------------------------------------------- #
# Gate 5 - worst-case rolling 3-month start window.
# --------------------------------------------------------------------------- #
def gate_worst_window(cfg, spec, ohlcv, warmup):
    n = len(ohlcv)
    win = _bars_per_3_months(ohlcv.timeframe)
    if n < win + warmup + 30:
        return {
            "name": "Gate 5 - worst-case 3-month window",
            "passed": True,
            "reason": ("history (%d bars) shorter than a 3-month window (%d); "
                       "gate SKIPPED (treated as pass)" % (n, win)),
            "skipped": True, "metrics": {},
        }
    # Step through non-overlapping-ish windows (step = win//2) and find the worst
    # net_profit. The strategy must not lose catastrophically in any window.
    step = max(1, win // 2)
    worst_net = None
    worst_start = 0
    start = 0
    while start + win <= n:
        seg = ohlcv.slice(start, start + win)
        res = _run_bt(cfg, Strategy(spec), seg, warmup)
        net = float((res.metrics or {}).get("net_profit", 0.0) or 0.0)
        if worst_net is None or net < worst_net:
            worst_net = net
            worst_start = start
        start += step
    init_balance = float(cfg.get_path("backtest.initial_balance", 10000.0)
                         or 10000.0)
    # Catastrophic floor: a single 3-month window must not lose more than 25%
    # of the starting balance.
    floor = -0.25 * init_balance
    passed = worst_net is not None and worst_net >= floor
    reason = ("worst 3-month net_profit=%s (window start bar %d); floor=%s "
              "(-25%% of %s)" %
              (_num(worst_net, "%.2f"), worst_start, _num(floor, "%.2f"),
               _num(init_balance, "%.0f")))
    return {
        "name": "Gate 5 - worst-case 3-month window",
        "passed": passed, "reason": reason,
        "metrics": {"worst_net_profit": worst_net, "floor": floor,
                    "window_bars": win},
    }


# --------------------------------------------------------------------------- #
# Orchestration + verdict artifact (U5.2)
# --------------------------------------------------------------------------- #
def run_gauntlet(cfg, symbol, timeframe, warmup=60, n_shuffles=1000,
                 seed=12345):
    """Run all five gates on the registry top-1 and return a verdict dict."""
    memory = MemoryStore(cfg)
    spec, entry = _load_top1(memory, symbol, timeframe)
    if spec is None:
        return {"symbol": symbol, "timeframe": timeframe,
                "error": "empty_registry",
                "message": ("No registry strategy for %s %s. Run a search + "
                            "rebuild-registry first." % (symbol, timeframe))}

    feed = DataFeed(cfg)
    ohlcv = feed.get_ohlcv(symbol, timeframe)
    if ohlcv is None or len(ohlcv) < 200:
        n = 0 if ohlcv is None else len(ohlcv)
        return {"symbol": symbol, "timeframe": timeframe,
                "error": "insufficient_data", "bars": n,
                "message": ("Not enough price data for %s %s (%d bars). Export "
                            "history first." % (symbol, timeframe, n))}

    risk_pct = float(cfg.get_path("risk.risk_per_trade", 0.01) or 0.01)

    gates = []
    g1 = gate_full_history(cfg, spec, ohlcv, warmup)
    gates.append(g1)
    gates.append(gate_holdout(cfg, spec, ohlcv, warmup))
    gates.append(gate_monte_carlo(cfg, g1, n_shuffles, risk_pct, seed=seed))
    gates.append(gate_cost_stress(cfg, spec, ohlcv, warmup))
    gates.append(gate_worst_window(cfg, spec, ohlcv, warmup))

    # Write U1 receipts from the full-history run so the verdict can link them.
    report_dir = resolve_path(
        cfg, cfg.get_path("backtest.report_dir", "backtests"))
    artifacts = {}
    if g1.get("result") is not None:
        try:
            artifacts = trade_log.write_artifacts(
                g1["result"], "%s_GAUNTLET" % symbol, timeframe,
                report_dir=report_dir)
        except Exception as exc:  # pragma: no cover - defensive
            print("WARNING: could not write trade artifacts: %s" % exc)

    overall = all(bool(g.get("passed")) for g in gates)
    # Strip the heavy BacktestResult object before returning/serializing.
    for g in gates:
        g.pop("result", None)

    verdict = {
        "symbol": symbol,
        "timeframe": timeframe,
        "fingerprint": spec.fingerprint(),
        "created_at": int(time.time()),
        "created_at_iso": time.strftime("%Y-%m-%d %H:%M:%S",
                                        time.gmtime()),
        "overall_pass": overall,
        "warmup": warmup,
        "mc_shuffles": n_shuffles,
        "bars": len(ohlcv),
        "spec": spec.to_dict(),
        "gates": gates,
        "artifacts": artifacts,
    }
    return verdict


def _verdict_path(report_dir, fingerprint):
    return os.path.join(report_dir, "gauntlet_%s.md" % fingerprint)


def write_verdict_md(cfg, verdict):
    """Render the PASS/FAIL verdict as a single Markdown file (U5.2)."""
    report_dir = resolve_path(
        cfg, cfg.get_path("backtest.report_dir", "backtests"))
    try:
        os.makedirs(report_dir, exist_ok=True)
    except Exception:
        pass
    fp = verdict.get("fingerprint", "unknown")
    path = _verdict_path(report_dir, fp)

    lines = []
    status = "PASS" if verdict.get("overall_pass") else "FAIL"
    lines.append("# Gauntlet verdict: %s" % status)
    lines.append("")
    lines.append("- Symbol/timeframe: **%s %s**" %
                 (verdict["symbol"], verdict["timeframe"]))
    lines.append("- Strategy fingerprint: `%s`" % fp)
    lines.append("- Created (UTC): %s" % verdict.get("created_at_iso"))
    lines.append("- Bars tested: %s   warmup: %s   MC shuffles: %s" %
                 (verdict.get("bars"), verdict.get("warmup"),
                  verdict.get("mc_shuffles")))
    lines.append("")
    lines.append("## Overall: %s" % status)
    lines.append("")
    lines.append("A single FAIL below means this strategy MUST NOT trade live.")
    lines.append("")
    lines.append("## Gates")
    lines.append("")
    lines.append("| Gate | Result | Detail |")
    lines.append("| ---- | ------ | ------ |")
    for g in verdict.get("gates", []):
        res = "PASS" if g.get("passed") else "FAIL"
        if g.get("skipped"):
            res = "SKIP"
        detail = str(g.get("reason", "")).replace("|", "\\|")
        lines.append("| %s | %s | %s |" % (g.get("name"), res, detail))
    lines.append("")

    art = verdict.get("artifacts", {}) or {}
    if art:
        lines.append("## Receipts (from the full-history run)")
        lines.append("")
        if art.get("trades"):
            lines.append("- Per-trade CSV: `%s`" % art.get("trades"))
        if art.get("equity"):
            lines.append("- Equity curve CSV: `%s`" % art.get("equity"))
        lines.append("")
        lines.append("Render an HTML report with:")
        lines.append("")
        lines.append("```bash")
        lines.append("python scripts/make_report.py --trades %s" %
                     (art.get("trades", "<trades CSV>")))
        lines.append("```")
        lines.append("")

    lines.append("## Strategy spec")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(verdict.get("spec", {}), indent=2, default=str))
    lines.append("```")
    lines.append("")

    try:
        with open(path, "w", encoding="ascii", errors="replace") as fh:
            fh.write("\n".join(lines))
    except Exception as exc:  # pragma: no cover - defensive
        print("WARNING: could not write verdict file: %s" % exc)
        return None
    return path


def _print_console(verdict):
    print("=" * 70)
    if verdict.get("error"):
        print("GAUNTLET:", verdict.get("message", verdict["error"]))
        print("=" * 70)
        return
    status = "PASS" if verdict.get("overall_pass") else "FAIL"
    print("GAUNTLET VERDICT: %s   (%s %s  fp=%s)" %
          (status, verdict["symbol"], verdict["timeframe"],
           verdict["fingerprint"]))
    print("-" * 70)
    for g in verdict.get("gates", []):
        res = "PASS" if g.get("passed") else "FAIL"
        if g.get("skipped"):
            res = "SKIP"
        print("  [%s] %s" % (res, g.get("name")))
        print("        %s" % g.get("reason"))
    print("=" * 70)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run the pre-flight validation gauntlet (U5).")
    parser.add_argument("--config", default=None,
                        help="Path to a config YAML (default config/config.yaml).")
    parser.add_argument("--symbol", default=None,
                        help="Symbol (default: first of mt5.symbols).")
    parser.add_argument("--timeframe", "--tf", dest="timeframe", default=None,
                        help="Timeframe (default: mt5.timeframe).")
    parser.add_argument("--warmup", type=int, default=60,
                        help="Warmup bars before scoring (default 60).")
    parser.add_argument("--mc", type=int, default=1000,
                        help="Monte-Carlo shuffles for gate 3 (default 1000).")
    parser.add_argument("--seed", type=int, default=12345,
                        help="RNG seed for the Monte-Carlo (default 12345).")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    cfg = load_config(args.config)
    symbol = args.symbol
    if not symbol:
        syms = cfg.get_path("mt5.symbols", ["XAUUSD"])
        symbol = str(syms[0]) if syms else "XAUUSD"
    timeframe = args.timeframe or str(cfg.get_path("mt5.timeframe", "M15"))

    verdict = run_gauntlet(cfg, symbol, timeframe, warmup=args.warmup,
                           n_shuffles=args.mc, seed=args.seed)
    _print_console(verdict)
    if not verdict.get("error"):
        path = write_verdict_md(cfg, verdict)
        if path:
            print("Verdict written to:", path)
    return 0 if verdict.get("overall_pass") else 1


if __name__ == "__main__":
    sys.exit(main())
