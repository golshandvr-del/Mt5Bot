"""
Diagnose WHY the strategy registry comes out empty after a search.

Run this on the machine that has the populated memory DB, e.g.:

    python scripts/diagnose_registry.py
    python scripts/diagnose_registry.py --symbol XAUUSD --timeframe M15

It reads ONLY the memory DB (no MT5, no search) and prints, per
(symbol, timeframe), how many candidate strategies survive each successive
filter that top_strategies / update_registry apply:

    1. rows in `results` at all
    2. distinct fingerprints
    3. after the stored rank_metric matches the configured rank_metric
    4. after HAVING avg_trades >= min_trades
    5. after the statistical-significance gate (pnl_pvalue / win_rate_ci_low)

The first place the count drops to 0 is the culprit. This turns "the registry
is empty" into a precise, data-driven answer instead of guesswork.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Make the project importable when run as `python scripts/diagnose_registry.py`.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from config.loader import load_config  # noqa: E402
from core.memory.store import MemoryStore, _py_json_extract  # noqa: E402


def _fmt(v):
    return "None" if v is None else v


def diagnose(cfg, symbol=None, timeframe=None):
    store = MemoryStore(cfg)
    s = cfg.get_path("memory.search", {})
    rank_metric = s.get("rank_metric", "expectancy") if hasattr(s, "get") else "expectancy"
    try:
        min_trades = int(s.get("min_trades", 30)) if hasattr(s, "get") else 30
    except (TypeError, ValueError):
        min_trades = 30
    sig = s.get("significance", {}) if hasattr(s, "get") else {}
    sig_enabled = bool(sig.get("enabled", False)) if hasattr(sig, "get") else False
    max_pvalue = float(sig.get("max_pvalue", 0.05)) if hasattr(sig, "get") else 0.05
    min_ci_low = float(sig.get("min_winrate_ci_low", 0.0)) if hasattr(sig, "get") else 0.0

    print("=" * 70)
    print("CONFIG in effect:")
    print("  rank_metric            =", rank_metric)
    print("  min_trades             =", min_trades)
    print("  significance.enabled   =", sig_enabled)
    print("  significance.max_pvalue=", max_pvalue)
    print("  significance.min_ci_low=", min_ci_low)
    print("=" * 70)

    conn = store._connect()  # already has the json_extract shim registered
    conn.create_function("json_extract", 2, _py_json_extract)  # belt & braces
    cur = conn.cursor()

    if symbol and timeframe:
        pairs = [{"symbol": symbol, "timeframe": timeframe}]
    else:
        pairs = store.known_symbol_timeframes()

    for pair in pairs:
        sym, tf = pair["symbol"], pair["timeframe"]
        print("\n### %s %s" % (sym, tf))

        cur.execute(
            "SELECT COUNT(*) AS n FROM results WHERE symbol=? AND timeframe=?",
            (sym, tf),
        )
        n_rows = cur.fetchone()["n"]
        print("  1) result rows total ................ %d" % n_rows)

        cur.execute(
            "SELECT COUNT(DISTINCT fingerprint) AS n FROM results "
            "WHERE symbol=? AND timeframe=?",
            (sym, tf),
        )
        n_fps = cur.fetchone()["n"]
        print("  2) distinct strategies .............. %d" % n_fps)

        # What rank_metrics were actually stored?
        cur.execute(
            "SELECT rank_metric, COUNT(DISTINCT fingerprint) AS n FROM results "
            "WHERE symbol=? AND timeframe=? GROUP BY rank_metric",
            (sym, tf),
        )
        stored_metrics = cur.fetchall()
        print("     stored rank_metric buckets:")
        for r in stored_metrics:
            flag = "  <-- MATCHES config" if r["rank_metric"] == rank_metric else ""
            print("       %-14s : %d strategies%s"
                  % (r["rank_metric"], r["n"], flag))

        cur.execute(
            "SELECT COUNT(DISTINCT fingerprint) AS n FROM results "
            "WHERE symbol=? AND timeframe=? AND rank_metric=?",
            (sym, tf, rank_metric),
        )
        n_metric = cur.fetchone()["n"]
        print("  3) after rank_metric='%s' filter .. %d" % (rank_metric, n_metric))

        cur.execute(
            "SELECT fingerprint, "
            "  AVG(json_extract(metrics_json,'$.num_trades')) AS avg_trades, "
            "  AVG(json_extract(metrics_json,'$.pnl_pvalue')) AS avg_pvalue, "
            "  AVG(json_extract(metrics_json,'$.win_rate_ci_low')) AS avg_ci_low, "
            "  AVG(score) AS avg_score "
            "FROM results WHERE symbol=? AND timeframe=? AND rank_metric=? "
            "GROUP BY fingerprint",
            (sym, tf, rank_metric),
        )
        grouped = cur.fetchall()

        n_trades_ok = 0
        n_sig_ok = 0
        best_trades = None
        best_pvalue = None
        for r in grouped:
            at = r["avg_trades"]
            if at is not None:
                if best_trades is None or at > best_trades:
                    best_trades = at
            pv = r["avg_pvalue"]
            if pv is not None:
                if best_pvalue is None or pv < best_pvalue:
                    best_pvalue = pv
            if at is not None and at >= min_trades:
                n_trades_ok += 1
                # significance check
                pval = 1.0 if pv is None else float(pv)
                ok = pval <= max_pvalue
                if ok and min_ci_low > 0.0:
                    ci = 0.0 if r["avg_ci_low"] is None else float(r["avg_ci_low"])
                    ok = ci >= min_ci_low
                if ok:
                    n_sig_ok += 1

        print("  4) after avg_trades >= %d ........... %d  (best avg_trades seen: %s)"
              % (min_trades, n_trades_ok, _fmt(round(best_trades, 2) if best_trades is not None else None)))
        if sig_enabled:
            print("  5) after significance gate .......... %d  (best avg_pvalue seen: %s)"
                  % (n_sig_ok, _fmt(round(best_pvalue, 4) if best_pvalue is not None else None)))
        else:
            print("  5) significance gate is DISABLED .... %d" % n_trades_ok)

        # Verdict
        final = n_sig_ok if sig_enabled else n_trades_ok
        print("  => strategies that would be promoted: %d" % final)
        if final == 0:
            if n_rows == 0:
                print("     REASON: no results stored for this pair.")
            elif n_metric == 0:
                print("     REASON: stored rank_metric != config rank_metric "
                      "(step 3 dropped everything).")
            elif n_trades_ok == 0:
                print("     REASON: every strategy averages < min_trades=%d "
                      "(step 4). Lower min_trades to recover them." % min_trades)
            elif sig_enabled and n_sig_ok == 0:
                print("     REASON: significance gate rejected all "
                      "(avg_pvalue > %.3f). Disable significance or raise "
                      "max_pvalue to recover them." % max_pvalue)

    conn.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=None, help="path to config yaml")
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--timeframe", default=None)
    args = ap.parse_args()
    cfg = load_config(args.config) if args.config else load_config()
    diagnose(cfg, symbol=args.symbol, timeframe=args.timeframe)


if __name__ == "__main__":
    main()
