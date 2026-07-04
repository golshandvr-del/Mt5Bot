"""
Phase 5 (user-update-request): Time / Session / Season awareness layer.

This decoupled, pure-Python, standard-library-only package gives the bot an
awareness of WHEN it is trading and lets it LEARN, from historical trade
outcomes, which time windows were actually favorable for a given symbol and
timeframe. The user asked for the bot to pay attention to:

  - the trading SESSION (Sydney / Tokyo / London / New York, plus overlaps),
  - the DAY OF WEEK,
  - the SEASON (month / quarter),

and, importantly, to RECOGNIZE the relevance of these itself rather than assume
it. We therefore do NOT hard-code "London is good"; instead the bot aggregates
its own backtest/search trade results per time bucket and only uses a bucket's
edge once it is supported by enough samples.

Modules
-------
- session.py      : SessionCalendar + TimeContext. Detect the time context of a
                    single bar timestamp (session set, day, hour, season).
- time_stats.py   : TimeStats. Aggregate historical trade PnL into per-bucket
                    edge statistics, persisted to the memory SQLite database.
- time_context.py : TimeContextProvider. Combine live detection with learned
                    stats to emit a light time signal / favorable / blackout flag
                    for the decision engine.

Everything here is optional and config-driven (the `timing:` section of
config.yaml). When disabled or when statistics are insufficient, every output
degrades to a neutral value so the Windows 7 CPU-only live-light path is
unaffected.

All text is standard ASCII English only.
"""

from core.timing.session import (  # noqa: F401
    TimeContext,
    SessionCalendar,
    SESSION_NAMES,
    DAY_NAMES,
)
from core.timing.time_stats import TimeStats  # noqa: F401
from core.timing.time_context import TimeContextProvider, TimeSignal  # noqa: F401
