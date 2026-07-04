"""
Empirical time-bucket edge learning (Phase 5, user-update-request).

This is the "recognize it itself" part of the request. Instead of assuming that
a session/day/season is good or bad, the bot AGGREGATES its own historical trade
outcomes into per-time-bucket statistics and only trusts a bucket once it has
enough samples.

For every trade produced during offline search / walk-forward backtesting we
know the entry-bar timestamp, so we can attribute the trade's PnL to that bar's
time buckets (session, day, hour block, month, quarter, season). Aggregating
across many trades yields, per (symbol, timeframe, bucket_type, bucket_value):

  - n        : number of trades that fell in this bucket,
  - wins     : number of profitable trades,
  - sum_pnl  : total PnL,
  - sum_pnl2 : sum of squared PnL (for a variance / stability estimate),
  - win_rate : wins / n,
  - avg_pnl  : sum_pnl / n,
  - edge     : a bounded score in [-1, +1] derived from avg_pnl normalized by
               the trade PnL spread, shrunk toward 0 when n is small so rare
               buckets do not dominate.

Persistence
-----------
Stored in the SAME SQLite database as the Phase 3 memory (data_store/
memory.sqlite) in a dedicated `time_stats` table, so learned time edges survive
restarts alongside strategy memory. SQLite ships with Python's stdlib, so there
is no extra Windows 7 dependency.

All numbers are computed with pure Python; no numpy needed.
All text is standard ASCII English only.
"""

from __future__ import annotations

import math
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from core.timing.session import SessionCalendar
from core.utils.helpers import ensure_dir
from core.utils.logger import get_logger


# Primary key of the time_stats table is
# (symbol, timeframe, bucket_type, bucket_value).
_UPSERT_SQL = """
INSERT INTO time_stats
    (symbol, timeframe, bucket_type, bucket_value, n, wins,
     sum_pnl, sum_pnl2, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(symbol, timeframe, bucket_type, bucket_value) DO UPDATE SET
    n        = n        + excluded.n,
    wins     = wins     + excluded.wins,
    sum_pnl  = sum_pnl  + excluded.sum_pnl,
    sum_pnl2 = sum_pnl2 + excluded.sum_pnl2,
    updated_at = excluded.updated_at
"""


class TimeStats(object):
    """
    Learns and serves per-time-bucket trade-edge statistics.

    Construct with the loaded config. It resolves the same SQLite path used by
    the memory store (memory.db_file) and ensures the `time_stats` table exists.
    """

    def __init__(self, cfg: Any):
        self.cfg = cfg
        self.log = get_logger("timing.time_stats", cfg)
        root = cfg.get("project_root", ".") if hasattr(cfg, "get") else "."
        db_rel = cfg.get_path("memory.db_file", "data_store/memory.sqlite")
        self.db_path = db_rel if os.path.isabs(db_rel) else os.path.join(root, db_rel)
        try:
            ensure_dir(os.path.dirname(self.db_path))
        except Exception:
            pass
        # Minimum trades before a bucket's edge is trusted (else neutral).
        # P3.1 raised the default from 20 to 50 (a ~20-trade time bucket is too
        # noisy to trust). Read defensively so a bad/missing value degrades to
        # the safe default rather than crashing the live-light path.
        try:
            self.min_samples = int(cfg.get_path("timing.learning.min_samples", 50))
        except (TypeError, ValueError):
            self.min_samples = 50
        # Bayesian shrinkage constant (P3.1). The bucket edge is multiplied by
        # n / (n + shrinkage), pulling small buckets toward a neutral 0 edge in
        # proportion to how few samples they have. Decoupled from min_samples
        # (which is only the trust threshold) so the two can be tuned
        # independently. Default falls back to min_samples to preserve the old
        # n / (n + min_samples) behavior; shrinkage <= 0 disables shrinkage.
        try:
            self.shrinkage = float(
                cfg.get_path("timing.learning.shrinkage", self.min_samples)
            )
        except (TypeError, ValueError):
            self.shrinkage = float(self.min_samples)
        if self.shrinkage < 0.0:
            self.shrinkage = 0.0
        self.calendar = SessionCalendar(cfg)
        self._init_db()
        # Small in-process cache: (symbol, timeframe) -> {(type,value): row}.
        self._cache: Dict[str, Dict[Tuple[str, str], Dict[str, Any]]] = {}

    # ------------------------------------------------------------------ #
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        try:
            conn = self._connect()
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS time_stats (
                    symbol       TEXT,
                    timeframe    TEXT,
                    bucket_type  TEXT,
                    bucket_value TEXT,
                    n            INTEGER DEFAULT 0,
                    wins         INTEGER DEFAULT 0,
                    sum_pnl      REAL DEFAULT 0.0,
                    sum_pnl2     REAL DEFAULT 0.0,
                    updated_at   REAL,
                    PRIMARY KEY (symbol, timeframe, bucket_type, bucket_value)
                )
                """
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            self.log.error("time_stats DB init failed: %s", exc)

    # ------------------------------------------------------------------ #
    # Recording (learning) API.
    # ------------------------------------------------------------------ #
    def record_trades(self, symbol: str, timeframe: str,
                      trades: List[Dict[str, Any]]) -> int:
        """
        Record a batch of trades into the per-bucket aggregates.

        Each trade dict should contain at least:
          {"entry_ts": <epoch int/float>, "pnl": <float>}
        Trades missing a parseable timestamp are skipped.

        Returns the number of trades recorded. The aggregation is additive, so
        calling this repeatedly across many search runs keeps improving the
        statistics (ties into Phase 3 self-improvement).
        """
        if not trades:
            return 0

        # Accumulate per-bucket deltas in memory first, then one upsert each.
        agg: Dict[Tuple[str, str], Dict[str, float]] = {}
        recorded = 0
        for tr in trades:
            ts = tr.get("entry_ts", None)
            pnl = tr.get("pnl", None)
            if ts is None or pnl is None:
                continue
            ctx = self.calendar.context(ts)
            if ctx is None:
                continue
            try:
                pnl = float(pnl)
            except Exception:
                continue
            win = 1 if pnl > 0 else 0
            for bt, bv in ctx.buckets():
                key = (bt, bv)
                slot = agg.setdefault(
                    key, {"n": 0.0, "wins": 0.0, "sum": 0.0, "sum2": 0.0}
                )
                slot["n"] += 1.0
                slot["wins"] += float(win)
                slot["sum"] += pnl
                slot["sum2"] += pnl * pnl
            recorded += 1

        if not agg:
            return 0

        try:
            conn = self._connect()
            cur = conn.cursor()
            now = time.time()
            for (bt, bv), slot in agg.items():
                cur.execute(
                    _UPSERT_SQL,
                    (
                        symbol, timeframe, bt, bv,
                        int(slot["n"]), int(slot["wins"]),
                        float(slot["sum"]), float(slot["sum2"]), now,
                    ),
                )
            conn.commit()
            conn.close()
        except Exception as exc:
            self.log.error("record_trades failed: %s", exc)
            return 0

        # Invalidate cache for this key.
        self._cache.pop("%s|%s" % (symbol, timeframe), None)
        return recorded

    # ------------------------------------------------------------------ #
    # Query (serving) API.
    # ------------------------------------------------------------------ #
    def _load_key(self, symbol: str,
                  timeframe: str) -> Dict[Tuple[str, str], Dict[str, Any]]:
        """Load and cache all bucket rows for a symbol/timeframe."""
        key = "%s|%s" % (symbol, timeframe)
        if key in self._cache:
            return self._cache[key]
        rows: Dict[Tuple[str, str], Dict[str, Any]] = {}
        try:
            conn = self._connect()
            cur = conn.cursor()
            cur.execute(
                "SELECT bucket_type, bucket_value, n, wins, sum_pnl, sum_pnl2 "
                "FROM time_stats WHERE symbol=? AND timeframe=?",
                (symbol, timeframe),
            )
            for r in cur.fetchall():
                rows[(r["bucket_type"], r["bucket_value"])] = {
                    "n": int(r["n"]),
                    "wins": int(r["wins"]),
                    "sum_pnl": float(r["sum_pnl"]),
                    "sum_pnl2": float(r["sum_pnl2"]),
                }
            conn.close()
        except Exception as exc:
            self.log.error("_load_key failed: %s", exc)
        self._cache[key] = rows
        return rows

    @staticmethod
    def _edge_from_row(row: Dict[str, Any], min_samples: int,
                       shrinkage: Optional[float] = None) -> Dict[str, Any]:
        """
        Turn raw aggregates into interpretable stats + a bounded edge score.

        edge in [-1, +1]:
          base = avg_pnl / (std_pnl + |avg_pnl| + eps)   (a t-like ratio, bounded)
          shrink toward 0 by n / (n + shrinkage) so small samples are cautious
          (Bayesian shrinkage, P3.1). `shrinkage` is decoupled from
          `min_samples` (the trust threshold); when it is None the old
          n / (n + min_samples) behavior is used, and shrinkage <= 0 disables
          shrinkage (raw edge).
        favorable/unfavorable are derived by the caller via thresholds.
        """
        n = int(row.get("n", 0))
        wins = int(row.get("wins", 0))
        sum_pnl = float(row.get("sum_pnl", 0.0))
        sum_pnl2 = float(row.get("sum_pnl2", 0.0))
        if n <= 0:
            return {"n": 0, "win_rate": 0.0, "avg_pnl": 0.0, "edge": 0.0,
                    "trusted": False}
        avg = sum_pnl / n
        var = max(0.0, sum_pnl2 / n - avg * avg)
        std = math.sqrt(var)
        eps = 1e-9
        base = avg / (std + abs(avg) + eps)     # naturally in about [-1, +1]
        base = max(-1.0, min(1.0, base))
        # Bayesian shrinkage: edge * n / (n + shrinkage_k).
        if shrinkage is None:
            k = float(max(1, min_samples))
        else:
            k = float(shrinkage)
        if k <= 0.0:
            shrink = 1.0                        # shrinkage disabled -> raw edge
        else:
            shrink = n / float(n + k)
        edge = base * shrink
        return {
            "n": n,
            "win_rate": wins / float(n),
            "avg_pnl": avg,
            "std_pnl": std,
            "edge": max(-1.0, min(1.0, edge)),
            "trusted": n >= min_samples,
        }

    def bucket_edge(self, symbol: str, timeframe: str,
                    bucket_type: str, bucket_value: str) -> Dict[str, Any]:
        """
        Return the learned stats for one bucket. If the bucket is unknown or has
        fewer than min_samples trades, `edge` is returned but `trusted` is False,
        so the caller can decide to ignore untrusted edges.
        """
        rows = self._load_key(symbol, timeframe)
        row = rows.get((bucket_type, bucket_value))
        if row is None:
            return {"n": 0, "win_rate": 0.0, "avg_pnl": 0.0, "edge": 0.0,
                    "trusted": False}
        return self._edge_from_row(row, self.min_samples, self.shrinkage)

    def context_edges(self, symbol: str, timeframe: str,
                      ctx: Any) -> Dict[str, Dict[str, Any]]:
        """
        Return the learned stats for every bucket of a TimeContext, keyed by
        bucket_type (session/day/hour/month/quarter/season).
        """
        out: Dict[str, Dict[str, Any]] = {}
        if ctx is None:
            return out
        for bt, bv in ctx.buckets():
            out[bt] = self.bucket_edge(symbol, timeframe, bt, bv)
        return out

    def summary(self, symbol: str, timeframe: str) -> Dict[str, Any]:
        """Small dict of how many buckets are learned (for logging/tests)."""
        rows = self._load_key(symbol, timeframe)
        trusted = sum(
            1 for r in rows.values() if int(r.get("n", 0)) >= self.min_samples
        )
        total_trades = sum(int(r.get("n", 0)) for r in rows.values())
        return {
            "buckets": len(rows),
            "trusted_buckets": trusted,
            "total_trade_attributions": total_trades,
            "min_samples": self.min_samples,
        }
