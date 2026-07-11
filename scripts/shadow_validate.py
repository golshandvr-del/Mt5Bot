"""
Continuous shadow-validation CLI (UPGRADE_PLAN Phase U6.5).

Runs the HARD safety layer OFFLINE (e.g. every weekend on the VPS): it
re-scores every live strategy on its trailing window of realized live/paper
trades against the walk-forward reference distribution it was promoted on and,
for any strategy that has decayed below the decay threshold WITH enough live
evidence, DEMOTES it to paper. A demoted fingerprint is then refused REAL
orders by the OrderManager until a fresh search re-validates it. A recovered
strategy is auto-cleared when ``clear_on_pass`` is on.

The heavy lifting lives in ``core/strategy/shadow_validation.py``; this script
just wires config + memory into it, runs it, and prints a plain-language
summary. It NEVER promotes, edits, or trades - it can only pull a decayed edge
OFF live money (a strictly conservative, one-way-safe action).

Usage
-----
    python scripts/shadow_validate.py                 # run per config
    python scripts/shadow_validate.py --print         # also echo the report
    python scripts/shadow_validate.py --force         # run even if disabled
    python scripts/shadow_validate.py --list          # just list demotions
    python scripts/shadow_validate.py --config path/to/config.yaml

Knobs default to the ``decision.shadow_validation`` config block. When the gate
(``decision.shadow_validation.enabled``) is off the run is a pure no-op unless
``--force`` is given (handy for a manual weekend check without flipping config).

Pure standard library, ASCII English only, Windows 7 + Python 3.8 friendly.
"""

from __future__ import annotations

import argparse
import os
import sys

# Make the project importable when run as `python scripts/shadow_validate.py`.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from config.loader import load_config  # noqa: E402
from core.memory.store import MemoryStore  # noqa: E402
from core.strategy.shadow_validation import ShadowValidator  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Continuous shadow validation - hard-demote decayed live "
                    "strategies to paper (UPGRADE_PLAN U6.5). One-way safe: "
                    "never promotes, edits, or trades.")
    parser.add_argument("--config", default=None, help="path to config.yaml")
    parser.add_argument("--print", dest="do_print", action="store_true",
                        help="also echo the Markdown report to stdout")
    parser.add_argument("--force", action="store_true",
                        help="run even if decision.shadow_validation.enabled is "
                             "false (temporary in-memory override)")
    parser.add_argument("--list", dest="list_only", action="store_true",
                        help="only list currently demoted fingerprints and exit")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    memory = MemoryStore(cfg)
    validator = ShadowValidator(cfg, memory)

    if args.list_only:
        demotions = validator.load_demotions()
        if not demotions:
            print("No strategies are currently demoted.")
            return 0
        print("Currently demoted (refused live orders):")
        for fp, meta in demotions.items():
            reason = meta.get("reason", "") if isinstance(meta, dict) else ""
            print("  %s  %s" % (str(fp)[:16], reason))
        return 0

    if args.force and not validator.enabled:
        validator.enabled = True
        print("(--force: running with shadow validation temporarily enabled)")

    result = validator.run()
    if not result.get("enabled"):
        print("Shadow validation is DISABLED "
              "(decision.shadow_validation.enabled=false). Nothing assessed. "
              "Use --force to run a one-off check.")
        return 0

    demoted = result.get("demoted", [])
    cleared = result.get("cleared", [])
    print("DEMOTED to paper this run: %s"
          % (", ".join(f[:16] for f in demoted) or "(none)"))
    print("CLEARED back to live this run: %s"
          % (", ".join(f[:16] for f in cleared) or "(none)"))
    print("Report written to %s" % validator.report_file)

    if args.do_print:
        print("")
        print(result.get("report", ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
