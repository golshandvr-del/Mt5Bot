"""
Decision journal writer (Phase U1.4 - transparency overhaul).

In paper/live mode every decision the engine makes is appended as ONE JSON line
to `logs/decisions_<YYYY-MM-DD>.jsonl` (UTC date). This gives the user a durable,
machine- and human-readable record of WHY the bot did (or did not) act on each
bar, which `scripts/explain_decisions.py` (U1.4) later pretty-prints.

Each line carries:
  ts          : epoch seconds when the decision was logged
  time        : 'YYYY-MM-DD HH:MM:SS' UTC (human readable)
  symbol      : e.g. "XAUUSD"
  timeframe   : e.g. "M15"
  action      : +1 long / -1 short / 0 flat
  score       : final blended score in [-1, +1]
  size_hint   : sizing confidence 0..1
  sl_atr_mult / tp_atr_mult
  components  : {indicators, learning, news, timing, ...} raw per-source values
  threshold_long / threshold_short : the thresholds the score was tested against
  reasons     : the engine's human-readable reason strings

Design rules (repo-wide): pure standard library (json only), ASCII English only,
Windows 7 + Python 3.8 friendly, and NEVER raise into the caller - a failure to
append a journal line must not disrupt trading; it is swallowed and reported via
the returned bool.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Optional

from core.utils.helpers import ensure_dir


def _utc_date(now: Optional[float] = None) -> str:
    t = time.gmtime(now if now is not None else time.time())
    return time.strftime("%Y-%m-%d", t)


def _utc_stamp(now: Optional[float] = None) -> str:
    t = time.gmtime(now if now is not None else time.time())
    return time.strftime("%Y-%m-%d %H:%M:%S", t)


def decision_to_record(decision: Any, symbol: str, timeframe: str,
                       thresholds: Optional[Dict[str, float]] = None,
                       now: Optional[float] = None) -> Dict[str, Any]:
    """
    Build the journal record dict from a Decision object.

    thresholds : optional {"long": float, "short": float} used by the engine for
                 this decision. Recorded so a reader can see exactly what the
                 score was compared against (the core of "why over/under").
    Reads every attribute defensively so a partial/stub Decision still yields a
    valid record.
    """
    def attr(name: str, default: Any) -> Any:
        val = getattr(decision, name, default)
        return default if val is None else val

    thresholds = thresholds or {}
    t = now if now is not None else time.time()
    comps = attr("components", {}) or {}
    # Round component values for a compact, readable line.
    comp_out = {}
    for k, v in dict(comps).items():
        try:
            comp_out[str(k)] = round(float(v), 4)
        except Exception:
            comp_out[str(k)] = v

    return {
        "ts": int(t),
        "time": _utc_stamp(t),
        "symbol": str(symbol),
        "timeframe": str(timeframe),
        "action": int(attr("action", 0)),
        "score": round(float(attr("score", 0.0)), 4),
        "size_hint": round(float(attr("size_hint", 0.0)), 4),
        "sl_atr_mult": float(attr("sl_atr_mult", 0.0)),
        "tp_atr_mult": float(attr("tp_atr_mult", 0.0)),
        "components": comp_out,
        "threshold_long": float(thresholds.get("long", 0.0)),
        "threshold_short": float(thresholds.get("short", 0.0)),
        "reasons": list(attr("reasons", []) or []),
    }


def append_decision(decision: Any, symbol: str, timeframe: str,
                    thresholds: Optional[Dict[str, float]] = None,
                    log_dir: str = "logs",
                    now: Optional[float] = None) -> bool:
    """
    Append one JSON line describing `decision` to logs/decisions_<date>.jsonl.

    Returns True on success, False on any error (never raises). The file is
    opened in append mode so concurrent passes accumulate lines in order.
    """
    try:
        record = decision_to_record(decision, symbol, timeframe,
                                    thresholds=thresholds, now=now)
        ensure_dir(log_dir)
        path = os.path.join(log_dir, "decisions_%s.jsonl" % _utc_date(now))
        line = json.dumps(record, sort_keys=True)
        with open(path, "a", encoding="ascii", errors="replace") as fh:
            fh.write(line + "\n")
        return True
    except Exception:
        return False
