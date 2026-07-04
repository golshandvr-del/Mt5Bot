"""
Memory store (Phase 3): the bot's persistent "experience".

Persists every backtest result for every strategy spec into a local SQLite
database, plus a human-readable JSON registry of the current best strategies
per symbol/timeframe. This is the realistic, honest form of "self-improvement":
the more combinations the bot explores and stores, the better its future
strategy SELECTION becomes. It does NOT rewrite its own source code.

Tables
------
strategies(fingerprint PK, symbol, timeframe, spec_json, created_at)
results(id PK, fingerprint, symbol, timeframe, segment, rank_metric,
        metrics_json, score, created_at)

The JSON registry (strategy_registry.json) stores, per (symbol, timeframe),
the top-K specs and their aggregated walk-forward score so the live decision
engine can load them instantly without touching SQLite.

All persistence survives restarts. SQLite ships with the Python standard
library, so there is no extra dependency on Windows 7.

All text is standard ASCII English only.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional

from core.strategy.strategy import StrategySpec
from core.strategy.metrics import rank_value
from core.utils.helpers import read_json, write_json, ensure_dir
from core.utils.logger import get_logger


class MemoryStore(object):
    """SQLite-backed strategy/result memory with a JSON best-strategy registry."""

    def __init__(self, cfg: Any):
        self.cfg = cfg
        self.log = get_logger("memory.store", cfg)
        root = cfg.get("project_root", ".")
        db_rel = cfg.get_path("memory.db_file", "data_store/memory.sqlite")
        reg_rel = cfg.get_path("memory.registry_file",
                               "data_store/strategy_registry.json")
        self.db_path = db_rel if os.path.isabs(db_rel) else os.path.join(root, db_rel)
        self.registry_path = reg_rel if os.path.isabs(reg_rel) else os.path.join(root, reg_rel)
        ensure_dir(os.path.dirname(self.db_path))
        ensure_dir(os.path.dirname(self.registry_path))
        self.top_k = int(cfg.get_path("memory.ensemble_top_k", 3))
        self._init_db()

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
                CREATE TABLE IF NOT EXISTS strategies (
                    fingerprint TEXT PRIMARY KEY,
                    symbol      TEXT,
                    timeframe   TEXT,
                    spec_json   TEXT,
                    created_at  REAL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS results (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint  TEXT,
                    symbol       TEXT,
                    timeframe    TEXT,
                    segment      TEXT,
                    rank_metric  TEXT,
                    metrics_json TEXT,
                    score        REAL,
                    created_at   REAL
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_results_key "
                "ON results(symbol, timeframe, score)"
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            self.log.error("DB init failed: %s", exc)

    # ------------------------------------------------------------------ #
    def record_strategy(self, spec: StrategySpec) -> None:
        """Insert a strategy spec if not already present."""
        try:
            conn = self._connect()
            cur = conn.cursor()
            cur.execute(
                "INSERT OR IGNORE INTO strategies "
                "(fingerprint, symbol, timeframe, spec_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    spec.fingerprint(), spec.symbol, spec.timeframe,
                    json.dumps(spec.to_dict(), sort_keys=True), time.time(),
                ),
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            self.log.error("record_strategy failed: %s", exc)

    def record_result(self, spec: StrategySpec, metrics: Dict[str, Any],
                      segment: str, rank_metric: str) -> None:
        """Persist a backtest result row for a strategy on a data segment."""
        self.record_strategy(spec)
        try:
            score = rank_value(metrics, rank_metric)
            conn = self._connect()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO results "
                "(fingerprint, symbol, timeframe, segment, rank_metric, "
                " metrics_json, score, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    spec.fingerprint(), spec.symbol, spec.timeframe, segment,
                    rank_metric, json.dumps(metrics, sort_keys=True),
                    float(score), time.time(),
                ),
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            self.log.error("record_result failed: %s", exc)

    # ------------------------------------------------------------------ #
    def top_strategies(self, symbol: str, timeframe: str, k: Optional[int] = None,
                       rank_metric: str = "expectancy",
                       min_trades: int = 30) -> List[Dict[str, Any]]:
        """
        Return the top-k strategy specs for a symbol/timeframe ranked by the
        AVERAGE score across all their stored segments (walk-forward robust).
        Only strategies whose average num_trades >= min_trades are considered.
        """
        k = k or self.top_k
        try:
            conn = self._connect()
            cur = conn.cursor()
            cur.execute(
                "SELECT fingerprint, AVG(score) AS avg_score, "
                "       COUNT(*) AS n_segments, "
                "       AVG(json_extract(metrics_json, '$.num_trades')) AS avg_trades "
                "FROM results WHERE symbol=? AND timeframe=? AND rank_metric=? "
                "GROUP BY fingerprint "
                "HAVING avg_trades >= ? "
                "ORDER BY avg_score DESC LIMIT ?",
                (symbol, timeframe, rank_metric, float(min_trades), int(k)),
            )
            rows = cur.fetchall()
            results: List[Dict[str, Any]] = []
            for row in rows:
                fp = row["fingerprint"]
                cur2 = conn.cursor()
                cur2.execute(
                    "SELECT spec_json FROM strategies WHERE fingerprint=?", (fp,)
                )
                srow = cur2.fetchone()
                if not srow:
                    continue
                results.append(
                    {
                        "fingerprint": fp,
                        "avg_score": row["avg_score"],
                        "n_segments": row["n_segments"],
                        "avg_trades": row["avg_trades"],
                        "spec": json.loads(srow["spec_json"]),
                    }
                )
            conn.close()
            return results
        except Exception as exc:
            self.log.error("top_strategies failed: %s", exc)
            return []

    # ------------------------------------------------------------------ #
    def update_registry(self, symbol: str, timeframe: str,
                        rank_metric: str = "expectancy",
                        min_trades: int = 30) -> Dict[str, Any]:
        """
        Recompute the best strategies for symbol/timeframe and write them into
        the JSON registry. Returns the registry section that was written.
        """
        best = self.top_strategies(symbol, timeframe, self.top_k,
                                   rank_metric, min_trades)
        registry = read_json(self.registry_path, default={}) or {}
        key = "%s|%s" % (symbol, timeframe)
        registry[key] = {
            "rank_metric": rank_metric,
            "updated_at": time.time(),
            "top": best,
        }
        write_json(self.registry_path, registry)
        self.log.info(
            "Registry updated for %s %s with %d strategies.",
            symbol, timeframe, len(best),
        )
        return registry[key]

    def load_registry_top(self, symbol: str, timeframe: str) -> List[Dict[str, Any]]:
        """Read the persisted top strategies for symbol/timeframe (or [])."""
        registry = read_json(self.registry_path, default={}) or {}
        key = "%s|%s" % (symbol, timeframe)
        section = registry.get(key, {})
        return section.get("top", [])

    def stats(self) -> Dict[str, Any]:
        """Return simple counters about what the memory currently holds."""
        try:
            conn = self._connect()
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) AS c FROM strategies")
            n_strats = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM results")
            n_results = cur.fetchone()["c"]
            conn.close()
            return {"strategies": n_strats, "results": n_results}
        except Exception as exc:
            self.log.error("stats failed: %s", exc)
            return {"strategies": 0, "results": 0}
