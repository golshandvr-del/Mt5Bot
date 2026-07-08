"""
Pretty-print the last N decisions from the decision journal (Phase U1.4).

In paper/live mode the bot appends one JSON line per decision to
`logs/decisions_<YYYY-MM-DD>.jsonl` (see core/utils/decision_log.py). This
script reads that journal and prints, for the most recent N decisions, a plain
explanation of WHY each one crossed (or failed to cross) into an action:

    - the final score vs the long/short thresholds,
    - which component (indicators / learning / news / timing) pushed the score
      up or down, ranked by contribution,
    - whether a news/timing blackout blocked an otherwise-valid entry.

Usage
-----
    python scripts/explain_decisions.py                 # last 20 from today
    python scripts/explain_decisions.py --n 50
    python scripts/explain_decisions.py --date 2026-07-08
    python scripts/explain_decisions.py --file logs/decisions_2026-07-08.jsonl
    python scripts/explain_decisions.py --symbol XAUUSD

Pure standard library, ASCII English only, Windows 7 friendly.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional


# Component keys that are metadata, not signal sources, in the components dict.
_META_KEYS = {"_threshold_long", "_threshold_short", "_blackout"}


def _latest_journal(log_dir: str) -> Optional[str]:
    """Return the newest decisions_*.jsonl in log_dir, or None."""
    matches = sorted(glob.glob(os.path.join(log_dir, "decisions_*.jsonl")))
    return matches[-1] if matches else None


def load_records(path: str) -> List[Dict[str, Any]]:
    """Read a jsonl journal into a list of dicts, skipping bad lines."""
    records: List[Dict[str, Any]] = []
    with open(path, "r", encoding="ascii", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception:
                continue
    return records


def _action_word(action: int) -> str:
    return {1: "LONG", -1: "SHORT", 0: "FLAT"}.get(int(action), "?")


def explain_record(rec: Dict[str, Any]) -> str:
    """Build a multi-line human explanation for one decision record."""
    comps = dict(rec.get("components", {}) or {})
    score = float(rec.get("score", 0.0))
    action = int(rec.get("action", 0))
    lt = float(rec.get("threshold_long", comps.get("_threshold_long", 0.0)))
    st = float(rec.get("threshold_short", comps.get("_threshold_short", 0.0)))
    blackout = float(comps.get("_blackout", 0.0)) > 0.0

    # Rank the real signal sources by absolute contribution to the score.
    sources = {k: v for k, v in comps.items() if k not in _META_KEYS}
    ranked = sorted(sources.items(), key=lambda kv: -abs(float(kv[1] or 0.0)))

    head = ("%s  %-8s %-4s  action=%-5s score=%+.3f  (long>=%.2f, short<=-%.2f)"
            % (rec.get("time", ""), rec.get("symbol", ""),
               rec.get("timeframe", ""), _action_word(action), score, lt, st))

    lines = [head]
    if ranked:
        parts = ["%s=%+.3f" % (k, float(v or 0.0)) for k, v in ranked]
        lines.append("    components: " + ", ".join(parts))

    # WHY explanation.
    if action == 0:
        if blackout:
            lines.append("    WHY flat: blackout active (news/timing) blocked "
                         "any new entry.")
        elif score >= lt or score <= -st:
            lines.append("    WHY flat: score crossed a threshold but an "
                         "agreement/blackout gate vetoed the entry.")
        else:
            gap_long = lt - score
            gap_short = st + score  # distance below the short threshold
            nearer = "long" if gap_long <= gap_short else "short"
            gap = gap_long if nearer == "long" else gap_short
            lines.append("    WHY flat: score %+.3f did not reach the %s "
                         "threshold (short by %.3f)." % (score, nearer, gap))
    else:
        side = "long" if action == 1 else "short"
        thr = lt if action == 1 else st
        lines.append("    WHY %s: score %+.3f crossed the %s threshold %.2f; "
                     "size_hint=%.2f." % (side.upper(), score, side, thr,
                                          float(rec.get("size_hint", 0.0))))
    reasons = rec.get("reasons", []) or []
    if reasons:
        lines.append("    reasons: " + "; ".join(str(r) for r in reasons))
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Pretty-print recent decisions from the journal.")
    parser.add_argument("--n", type=int, default=20,
                        help="How many of the most recent decisions to show.")
    parser.add_argument("--date", default=None,
                        help="Journal date YYYY-MM-DD (default: newest file).")
    parser.add_argument("--file", default=None,
                        help="Explicit journal path (overrides --date).")
    parser.add_argument("--log-dir", default="logs",
                        help="Directory holding decisions_*.jsonl (default logs).")
    parser.add_argument("--symbol", default=None,
                        help="Only show decisions for this symbol.")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if args.file:
        path = args.file
    elif args.date:
        path = os.path.join(args.log_dir, "decisions_%s.jsonl" % args.date)
    else:
        path = _latest_journal(args.log_dir)

    if not path or not os.path.exists(path):
        print("No decision journal found (looked in %s). Run paper/live first."
              % (args.file or args.log_dir))
        return 2

    records = load_records(path)
    if args.symbol:
        records = [r for r in records
                   if str(r.get("symbol", "")) == args.symbol]
    if not records:
        print("Journal %s has no matching decisions." % path)
        return 0

    tail = records[-args.n:] if args.n > 0 else records
    print("Decision journal: %s (%d records, showing %d)"
          % (path, len(records), len(tail)))
    print("=" * 72)
    for rec in tail:
        print(explain_record(rec))
        print("-" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
