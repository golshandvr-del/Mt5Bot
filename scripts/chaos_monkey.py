"""
Chaos-monkey CLI (UPGRADE_PLAN Phase U6.6).

Runs the offline broker-nastiness stress harness on the CURRENT registry and
writes ONE human-readable report (backtests/chaos_report.md) plus a JSON dump
(backtests/chaos_report.json), classifying every registry strategy as
GRACEFUL / FRAGILE / SHATTERED under injected requotes, missed bars, spread
storms and partial fills.

It reads ONLY the memory DB + a price history (via DataFeed) - no search, no
live orders, no registry edits. Pure stdlib + the project's own modules;
Win7 / Py3.8 / CPU friendly. ASCII English only.

Usage
-----
    python scripts/chaos_monkey.py                       # per config
    python scripts/chaos_monkey.py --symbol XAUUSD --tf M15
    python scripts/chaos_monkey.py --force               # run even if disabled
    python scripts/chaos_monkey.py --all-nastiness       # turn every knob on
    python scripts/chaos_monkey.py --top 10 --print
    python scripts/chaos_monkey.py --config path/to/config.yaml

When ``general.chaos_monkey.enabled`` is false the run is a no-op unless
``--force`` is given. ``--force`` also turns on every nastiness switch if none
are configured, so a one-off "how robust is my registry?" check needs no config
edit.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Make the project importable when run as `python scripts/chaos_monkey.py`.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from config.loader import load_config, resolve_path  # noqa: E402
from core.data.data_feed import DataFeed  # noqa: E402
from core.memory.store import MemoryStore  # noqa: E402
from core.strategy.chaos_monkey import ChaosMonkey  # noqa: E402


def _report_md(report):
    lines = []
    counts = report.get("counts", {})
    total = sum(counts.values())
    lines.append("# Chaos-monkey report: %s %s" %
                 (report.get("symbol"), report.get("timeframe")))
    lines.append("")
    lines.append("- Created (UTC): %s" % report.get("created_at_iso"))
    lines.append("- Seed: %s   (reproducible)" % report.get("seed"))
    lines.append("- Strategies assessed: %d" % total)
    lines.append("- GRACEFUL: %d   FRAGILE: %d   SHATTERED: %d" %
                 (counts.get("GRACEFUL", 0), counts.get("FRAGILE", 0),
                  counts.get("SHATTERED", 0)))
    lines.append("")
    lines.append("## Injected nastiness")
    lines.append("")
    nz = report.get("nastiness", {})
    lines.append("| Type | On | Intensity |")
    lines.append("| ---- | -- | --------- |")
    lines.append("| Spread storm | %s | x%s |" %
                 (nz.get("spread_storm"), nz.get("spread_mult")))
    lines.append("| Requotes | %s | %s of opens, +/-%s pts |" %
                 (nz.get("requotes"), nz.get("requote_frac"),
                  nz.get("requote_points")))
    lines.append("| Missed bars | %s | %s dropped |" %
                 (nz.get("missed_bars"), nz.get("missed_frac")))
    lines.append("| Partial fills | %s | %s of fills |" %
                 (nz.get("partial_fills"), nz.get("partial_frac")))
    lines.append("")
    lines.append("Graceful floor: a strategy must keep >= %s of its clean edge "
                 "AND stay positive under chaos to be GRACEFUL." %
                 report.get("graceful_floor_mult"))
    lines.append("")
    lines.append("## Per-strategy verdicts")
    lines.append("")
    lines.append("| Fingerprint | Verdict | Clean net | Chaos net | Retained |")
    lines.append("| ----------- | ------- | --------- | --------- | -------- |")
    for r in report.get("strategies", []):
        ratio = r.get("retained_ratio")
        ratio_s = ("%.0f%%" % (100.0 * ratio)) if ratio is not None else "n/a"
        lines.append("| `%s` | %s | %s | %s | %s |" %
                     (str(r.get("fingerprint"))[:16], r.get("verdict"),
                      r.get("clean_net_profit"), r.get("chaos_net_profit"),
                      ratio_s))
    lines.append("")
    lines.append("SHATTERED strategies lose their edge the moment the broker "
                 "misbehaves - do NOT trust them live. GRACEFUL strategies keep "
                 "most of their edge through the storm.")
    lines.append("")
    return "\n".join(lines)


def _write_reports(cfg, report):
    report_dir = resolve_path(cfg, cfg.get_path("backtest.report_dir",
                                                "backtests"))
    try:
        os.makedirs(report_dir, exist_ok=True)
    except Exception:
        pass
    md_path = os.path.join(report_dir, "chaos_report.md")
    json_path = os.path.join(report_dir, "chaos_report.json")
    try:
        with open(md_path, "w", encoding="ascii", errors="replace") as fh:
            fh.write(_report_md(report))
    except Exception as exc:  # pragma: no cover - defensive
        print("WARNING: could not write chaos report md: %s" % exc)
        md_path = None
    try:
        with open(json_path, "w", encoding="ascii", errors="replace") as fh:
            json.dump(report, fh, indent=2, default=str)
    except Exception as exc:  # pragma: no cover - defensive
        print("WARNING: could not write chaos report json: %s" % exc)
        json_path = None
    return md_path, json_path


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Chaos-monkey harness - offline broker-nastiness stress "
                    "test of the registry (UPGRADE_PLAN U6.6). Never trades.")
    parser.add_argument("--config", default=None, help="path to config.yaml")
    parser.add_argument("--symbol", default=None, help="symbol (default: config)")
    parser.add_argument("--tf", "--timeframe", dest="timeframe", default=None,
                        help="timeframe (default: config)")
    parser.add_argument("--warmup", type=int, default=60,
                        help="leading bars skipped for indicator warmup")
    parser.add_argument("--top", type=int, default=0,
                        help="assess only the top N registry strategies (0=all)")
    parser.add_argument("--force", action="store_true",
                        help="run even if general.chaos_monkey.enabled is false")
    parser.add_argument("--all-nastiness", dest="all_nastiness",
                        action="store_true",
                        help="turn every nastiness switch on for this run")
    parser.add_argument("--print", dest="do_print", action="store_true",
                        help="also echo the Markdown report to stdout")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    symbol = args.symbol or cfg.get_path("trading.symbol", "XAUUSD")
    timeframe = args.timeframe or cfg.get_path("trading.timeframe", "M15")

    monkey = ChaosMonkey(cfg)

    if args.force and not monkey.ccfg.enabled:
        monkey.ccfg.enabled = True
        print("(--force: running with chaos-monkey temporarily enabled)")
    if args.all_nastiness or (args.force and not monkey.ccfg.any_nastiness()):
        monkey.ccfg.spread_storm = True
        monkey.ccfg.requotes = True
        monkey.ccfg.missed_bars = True
        monkey.ccfg.partial_fills = True
        print("(all nastiness switches ON for this run)")

    if not monkey.ccfg.enabled:
        print("Chaos-monkey is DISABLED (general.chaos_monkey.enabled=false). "
              "Nothing assessed. Use --force for a one-off check.")
        return 0
    if not monkey.ccfg.any_nastiness():
        print("Chaos-monkey is enabled but NO nastiness switch is on - the "
              "chaos run equals the clean run. Turn on at least one switch "
              "(or use --all-nastiness).")
        return 0

    memory = MemoryStore(cfg)
    feed = DataFeed(cfg)
    ohlcv = feed.get_ohlcv(symbol, timeframe)
    if ohlcv is None or len(ohlcv) < 200:
        n = 0 if ohlcv is None else len(ohlcv)
        print("Not enough price data for %s %s (%d bars). Export history first."
              % (symbol, timeframe, n))
        return 1

    top = memory.load_registry_top(symbol, timeframe) or []
    if not top:
        print("Empty registry for %s %s. Run a search + rebuild-registry first."
              % (symbol, timeframe))
        return 1

    report = monkey.assess_registry(memory, symbol, timeframe, ohlcv,
                                    warmup=args.warmup, top_n=args.top)
    md_path, json_path = _write_reports(cfg, report)

    counts = report.get("counts", {})
    print("Chaos-monkey assessed %d strategies for %s %s:" %
          (sum(counts.values()), symbol, timeframe))
    print("  GRACEFUL : %d" % counts.get("GRACEFUL", 0))
    print("  FRAGILE  : %d" % counts.get("FRAGILE", 0))
    print("  SHATTERED: %d" % counts.get("SHATTERED", 0))
    if md_path:
        print("Report written to %s" % md_path)
    if json_path:
        print("JSON written to %s" % json_path)

    if args.do_print:
        print("")
        print(_report_md(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
