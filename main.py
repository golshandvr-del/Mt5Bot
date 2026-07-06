"""
MT5 Smart Trading Bot - main entry point.

Usage
-----
    python main.py                      # use mode from config.general.mode
    python main.py --mode paper         # single paper-trading decision pass
    python main.py --mode live          # single live pass (sends orders)
    python main.py --mode backtest      # internal backtest report
    python main.py --mode search        # Phase 3 strategy search (memory build)
    python main.py --mode train         # Phase 1 offline learner training
    python main.py --mode loop          # continuous paper/live loop (VPS)
    python main.py --config other.yaml  # use an alternate config file

The heavy lifting lives in app/runners.py and app/context.py. This file only
parses arguments, ensures the project root is importable, and prints a compact
JSON summary of the run so it is easy to inspect from a console or a scheduler.

All text is standard ASCII English only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def _ensure_project_on_path() -> None:
    """Make sure the project root (this file's directory) is importable."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description="MT5 Smart Trading Bot launcher."
    )
    parser.add_argument(
        "--mode",
        default=None,
        help="Run mode: train, search, backtest, paper, live, loop. "
             "Defaults to config.general.mode.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to a config YAML file (defaults to config/config.yaml).",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=0,
        help="For --mode loop: number of passes (0 = run forever).",
    )
    parser.add_argument(
        "--sleep",
        type=int,
        default=60,
        help="For --mode loop: seconds to sleep between passes.",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    _ensure_project_on_path()
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    # Imports happen after the path fix so 'core'/'config'/'app' resolve.
    from app.context import BotContext
    from app import runners

    # Resolve the effective mode (CLI overrides config).
    ctx = BotContext(args.config)
    mode = (args.mode or ctx.cfg.get_path("general.mode", "paper")).lower()

    if mode == "loop":
        # Continuous trading loop uses its own fresh contexts each pass.
        runners.run_loop(ctx, iterations=args.iterations,
                         sleep_seconds=args.sleep)
        return 0

    if mode == "train":
        result = runners.run_train(ctx)
    elif mode == "search":
        result = runners.run_search(ctx)
    elif mode == "backtest":
        result = runners.run_backtest(ctx)
    elif mode in ("paper", "live"):
        result = runners.run_once(ctx)
    else:
        print("Unknown mode '%s'. Use train/search/backtest/paper/live/loop."
              % mode)
        return 2

    # Print a compact summary so schedulers/consoles can capture it.
    try:
        print(json.dumps(result, indent=2, default=str))
    except Exception:
        print(str(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
