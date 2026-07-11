"""
Trade-throttle learning CLI (UPGRADE_PLAN Phase U6.4).

Reads the U1.4 decision journal (``logs/decisions_*.jsonl``) and reports which
VETO GATES actually saved money and which merely cost missed profit, then writes
a plain-language Markdown recommendation. It NEVER edits config, NEVER touches
the live path, and NEVER inverts a signal - applying a recommendation stays a
manual, opt-in edit. The heavy lifting lives in
``core/utils/throttle_learning.py``.

Usage
-----
    python scripts/learn_throttle.py                      # all journals in logs/
    python scripts/learn_throttle.py --date 2026-07-08    # one day
    python scripts/learn_throttle.py --file logs/decisions_2026-07-08.jsonl
    python scripts/learn_throttle.py --horizon 8 --min-events 30 --min-save-rate 0.65
    python scripts/learn_throttle.py --out backtests/throttle_report.md
    python scripts/learn_throttle.py --print            # also echo the report

Knobs default to the ``decision.throttle_learning`` config block. The analyzer
runs regardless of ``enabled`` (a report is always safe); ``enabled`` only gates
whether the bot's own scheduled runs invoke it.

Pure standard library, ASCII English only, Windows 7 + Python 3.8 friendly.
"""

from __future__ import annotations

import argparse
import glob
import os
import sys

# Make the project importable when run as `python scripts/learn_throttle.py`.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from config.loader import load_config, resolve_path  # noqa: E402
from core.utils import throttle_learning as tl  # noqa: E402


def _journal_files(args, log_dir: str):
    """Resolve the set of journal files to analyze from the CLI args."""
    if args.file:
        return [args.file]
    if args.date:
        return [os.path.join(log_dir, "decisions_%s.jsonl" % args.date)]
    return sorted(glob.glob(os.path.join(log_dir, "decisions_*.jsonl")))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Mine the decision journal for veto gates that saved money "
                    "(UPGRADE_PLAN U6.4). Report-only; never edits config.")
    parser.add_argument("--config", default=None, help="path to config.yaml")
    parser.add_argument("--log-dir", default="logs",
                        help="directory holding decisions_*.jsonl (default logs)")
    parser.add_argument("--file", default=None,
                        help="analyze this single journal file")
    parser.add_argument("--date", default=None,
                        help="analyze logs/decisions_<DATE>.jsonl (YYYY-MM-DD)")
    parser.add_argument("--horizon", type=int, default=None,
                        help="forward journal bars to score a blocked signal")
    parser.add_argument("--min-events", type=int, default=None,
                        help="min blocked events before a gate is judged")
    parser.add_argument("--min-save-rate", type=float, default=None,
                        help="save-rate a gate must clear to be tightened")
    parser.add_argument("--out", default=None,
                        help="where to write the Markdown report "
                             "(default from config throttle_learning.report_file)")
    parser.add_argument("--print", dest="do_print", action="store_true",
                        help="also echo the report to stdout")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    tlc = cfg.get_path("decision.throttle_learning", {})
    tlc = tlc if hasattr(tlc, "get") else {}

    horizon = args.horizon if args.horizon is not None else int(tlc.get("horizon", 5))
    min_events = (args.min_events if args.min_events is not None
                  else int(tlc.get("min_events", 20)))
    min_save_rate = (args.min_save_rate if args.min_save_rate is not None
                     else float(tlc.get("min_save_rate", 0.60)))
    out_path = args.out or tlc.get("report_file", "backtests/throttle_report.md")

    files = _journal_files(args, args.log_dir)
    files = [f for f in files if os.path.isfile(f)]
    if not files:
        print("No decision journal files found (looked in %s). Run the bot in "
              "paper/live mode first to populate the journal." % args.log_dir)
        return 0

    recs, report = tl.run_analysis(
        files, horizon=horizon, min_events=min_events, min_save_rate=min_save_rate)

    resolved_out = resolve_path(cfg, out_path)
    try:
        out_dir = os.path.dirname(resolved_out)
        if out_dir and not os.path.isdir(out_dir):
            os.makedirs(out_dir)
        with open(resolved_out, "w", encoding="ascii", errors="replace") as fh:
            fh.write(report)
        print("Wrote throttle-learning report to %s" % resolved_out)
    except Exception as exc:
        print("Could not write report to %s: %s" % (resolved_out, exc))

    tighten = sorted(g for g, r in recs.items() if r["action"] == "tighten")
    review = sorted(g for g, r in recs.items() if r["action"] == "review")
    print("Gates recommended TIGHTEN: %s" % (", ".join(tighten) or "(none)"))
    if review:
        print("Gates flagged REVIEW: %s" % ", ".join(review))

    if args.do_print:
        print("")
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
