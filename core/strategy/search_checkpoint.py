"""
Search checkpointing (U4.6) - let a long (12-24h) strategy search survive a
reboot and resume with --resume WITHOUT re-evaluating fingerprints it already
scored.

SQLite already persists every backtest RESULT (so no compute is ever lost from
the memory's point of view). What the raw results table does NOT capture is the
SEARCH STATE itself: which fingerprints were already tried (so the resumed run
can skip them), how many trials have been evaluated so far (so the budget/trial
count continues instead of restarting), and the current evolutionary elite pool
(so evolution resumes converging instead of starting cold). This module persists
exactly that, as a single small JSON file per (symbol, timeframe), written
atomically every N trials.

Design goals (same repo rules): pure stdlib, ASCII English only, Win7 + Py3.8 +
CPU-only friendly, degrade gracefully (a corrupt/absent checkpoint just means
"start fresh", never crash a 24h run). The file is human-readable so a user can
inspect where a run got to.

File layout (data_store/search_ckpt_<SYMBOL>_<TF>.json):

    {
      "version": 1,
      "symbol": "XAUUSD",
      "timeframe": "M15",
      "method": "evolution",
      "evaluated": 1234,          # trials scored so far
      "updated_utc": "2026-07-10T12:00:00Z",
      "seen": ["<fp>", ...],       # every fingerprint already evaluated
      "scored": [[score, {spec}], ...]  # elite-pool seed (top specs by score)
    }

Only a bounded number of top-scored specs are stored (``max_scored``) so the
file stays small even after thousands of trials - that is enough to reconstruct
the elite pool exactly, and the full result history still lives in SQLite.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

from core.strategy.strategy import StrategySpec

CHECKPOINT_VERSION = 1


def checkpoint_path(data_dir: str, symbol: str, timeframe: str) -> str:
    """Return the checkpoint file path for one (symbol, timeframe) run.

    The name is sanitized to plain ASCII alnum so it is safe on Windows.
    """
    def _clean(s: str) -> str:
        return "".join(c if (c.isalnum() or c in ("-", "_")) else "_"
                       for c in str(s)) or "X"
    fname = "search_ckpt_%s_%s.json" % (_clean(symbol), _clean(timeframe))
    return os.path.join(data_dir, fname)


class SearchCheckpoint(object):
    """Persist + restore search state (seen fingerprints, trial count, elite
    pool) for one (symbol, timeframe) run.

    Usage::

        ck = SearchCheckpoint(path, symbol, tf, method, max_scored=200)
        if resume:
            state = ck.load()           # None if absent/invalid
        ...
        ck.save(evaluated, seen, scored)   # called every N trials
        ck.clear()                          # on clean completion (optional)
    """

    def __init__(self, path: str, symbol: str, timeframe: str,
                 method: str, max_scored: int = 200,
                 log: Optional[object] = None):
        self.path = path
        self.symbol = symbol
        self.timeframe = timeframe
        self.method = method
        self.max_scored = max(1, int(max_scored))
        self.log = log

    # ------------------------------------------------------------------ #
    def _info(self, msg: str, *args: Any) -> None:
        if self.log is not None:
            try:
                self.log.info(msg, *args)
            except Exception:
                pass

    def _warn(self, msg: str, *args: Any) -> None:
        if self.log is not None:
            try:
                self.log.warning(msg, *args)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    def exists(self) -> bool:
        return os.path.isfile(self.path)

    def load(self) -> Optional[Dict[str, Any]]:
        """Load a checkpoint. Returns a dict with keys ``evaluated`` (int),
        ``seen`` (set of fingerprints) and ``scored`` (list of (score, spec))
        or None when the file is missing / unreadable / for a different run.

        A checkpoint written for a DIFFERENT method is still honored for its
        ``seen`` + ``evaluated`` (so no fingerprint is re-scored), but its elite
        ``scored`` pool is only meaningful for the evolution method; callers may
        ignore it otherwise.
        """
        if not self.exists():
            return None
        try:
            with open(self.path, "r") as fh:
                data = json.load(fh)
        except Exception as exc:  # corrupt / partial write -> start fresh
            self._warn("Search checkpoint %s unreadable (%s); starting fresh.",
                       self.path, exc)
            return None
        if not isinstance(data, dict):
            return None
        # Guard against loading a checkpoint from a different symbol/timeframe
        # (should never happen given the per-run filename, but be safe).
        if (data.get("symbol") not in (None, self.symbol)
                or data.get("timeframe") not in (None, self.timeframe)):
            self._warn("Search checkpoint %s is for a different run "
                       "(%s %s); ignoring.", self.path,
                       data.get("symbol"), data.get("timeframe"))
            return None
        seen = set(data.get("seen", []) or [])
        scored: List[Tuple[float, StrategySpec]] = []
        for item in (data.get("scored", []) or []):
            try:
                score = float(item[0])
                spec = StrategySpec.from_dict(item[1])
                scored.append((score, spec))
            except Exception:
                continue  # skip malformed entries, keep the rest
        evaluated = int(data.get("evaluated", 0) or 0)
        self._info("Resuming search from checkpoint %s: %d trial(s) done, "
                   "%d fingerprint(s) seen, %d elite spec(s) restored.",
                   self.path, evaluated, len(seen), len(scored))
        return {"evaluated": evaluated, "seen": seen, "scored": scored,
                "method": data.get("method", self.method)}

    def save(self, evaluated: int, seen: Any,
             scored: List[Tuple[float, StrategySpec]]) -> None:
        """Atomically write the current search state.

        Only the top ``max_scored`` specs (by score) are stored so the file
        stays small; that is enough to rebuild the elite pool exactly. Writes to
        a temp file in the same directory then os.replace() so a crash mid-write
        can never corrupt an existing good checkpoint.
        """
        try:
            ordered = sorted(scored, key=lambda t: t[0], reverse=True)
            top = ordered[:self.max_scored]
            payload = {
                "version": CHECKPOINT_VERSION,
                "symbol": self.symbol,
                "timeframe": self.timeframe,
                "method": self.method,
                "evaluated": int(evaluated),
                "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                             time.gmtime()),
                "seen": sorted(seen),
                "scored": [[float(s), spec.to_dict()] for s, spec in top],
            }
            directory = os.path.dirname(self.path) or "."
            if directory and not os.path.isdir(directory):
                os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix="search_ckpt_", suffix=".tmp",
                                       dir=directory)
            try:
                with os.fdopen(fd, "w") as fh:
                    json.dump(payload, fh)
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
        except Exception as exc:
            # Never let a checkpoint write failure kill a long search.
            self._warn("Failed to write search checkpoint %s: %s",
                       self.path, exc)

    def clear(self) -> None:
        """Remove the checkpoint file (call on a clean, complete run)."""
        try:
            if self.exists():
                os.remove(self.path)
        except OSError as exc:
            self._warn("Could not remove search checkpoint %s: %s",
                       self.path, exc)
