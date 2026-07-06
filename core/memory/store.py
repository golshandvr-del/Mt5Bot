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
council(fingerprint PK, rewards_json, total_seen, updated_at)  -- Phase 5 / P5.2

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
        # Statistical-significance registry filter (Track A / A3, P2.4). A
        # strategy that cannot be separated from randomness is still RECORDED in
        # SQLite (for memory) but is never PROMOTED to the JSON registry. The
        # thresholds come from config; defaults keep the filter effectively
        # permissive-safe so a missing/partial config never crashes.
        self.sig_enabled = bool(
            cfg.get_path("memory.search.significance.enabled", True)
        )
        try:
            self.sig_max_pvalue = float(
                cfg.get_path("memory.search.significance.max_pvalue", 0.05)
            )
        except (TypeError, ValueError):
            self.sig_max_pvalue = 0.05
        try:
            self.sig_min_winrate_ci_low = float(
                cfg.get_path("memory.search.significance.min_winrate_ci_low",
                             0.0)
            )
        except (TypeError, ValueError):
            self.sig_min_winrate_ci_low = 0.0
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
            # Phase 5 / P5.2: live strategy-council credibility. One row per
            # strategy fingerprint holding its rolling window of normalized
            # rewards (JSON list) so the council's LIVE credibility survives
            # restarts. Kept in the same SQLite DB as the offline memory.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS council (
                    fingerprint  TEXT PRIMARY KEY,
                    rewards_json TEXT,
                    total_seen   INTEGER,
                    updated_at   REAL
                )
                """
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
                       min_trades: int = 30,
                       allowed_fingerprints: Optional[Any] = None,
                       apply_significance: bool = True
                       ) -> List[Dict[str, Any]]:
        """
        Return the top-k strategy specs for a symbol/timeframe ranked by the
        AVERAGE score across all their stored segments (walk-forward robust).
        Only strategies whose average num_trades >= min_trades are considered.

        allowed_fingerprints (A2 / P1.4): when a set/collection is supplied, only
        strategies whose fingerprint is in it are eligible (used by the locked
        holdout gate so only holdout-passing specs are promoted). When None
        (default) no such filtering happens and behavior is unchanged. An empty
        collection means "nothing passed" and yields no results.

        apply_significance (A3 / P2.4): when True (default) AND the
        memory.search.significance filter is enabled in config, strategies whose
        AVERAGE bootstrap p-value across segments exceeds max_pvalue (or whose
        average Wilson win-rate lower bound is below min_winrate_ci_low, when that
        gate is > 0) are treated as statistically indistinguishable from random
        and are NOT promoted, even though their results stay recorded in SQLite.
        Set apply_significance=False to fetch the raw ranking (e.g. for
        inspection) regardless of significance. If the stored metrics predate the
        significance fields (older results), the missing p-value is treated as
        conservative 1.0 so such legacy specs are filtered out only when the
        filter is enabled.
        """
        k = k or self.top_k
        allowed = None
        if allowed_fingerprints is not None:
            allowed = set(allowed_fingerprints)
            if not allowed:
                # Nothing passed the holdout gate: promote nothing.
                return []
        sig_on = bool(apply_significance) and bool(self.sig_enabled)
        # We may need more than k rows before the allowlist/significance filters,
        # so fetch a generous window and trim after filtering.
        if allowed is None and not sig_on:
            fetch_limit = int(k)
        else:
            fetch_limit = max(int(k) * 20, 200)
        try:
            conn = self._connect()
            cur = conn.cursor()
            cur.execute(
                "SELECT fingerprint, AVG(score) AS avg_score, "
                "       COUNT(*) AS n_segments, "
                "       AVG(json_extract(metrics_json, '$.num_trades')) AS avg_trades, "
                "       AVG(json_extract(metrics_json, '$.pnl_pvalue')) AS avg_pvalue, "
                "       AVG(json_extract(metrics_json, '$.win_rate_ci_low')) "
                "           AS avg_ci_low "
                "FROM results WHERE symbol=? AND timeframe=? AND rank_metric=? "
                "GROUP BY fingerprint "
                "HAVING avg_trades >= ? "
                "ORDER BY avg_score DESC LIMIT ?",
                (symbol, timeframe, rank_metric, float(min_trades),
                 int(fetch_limit)),
            )
            rows = cur.fetchall()
            results: List[Dict[str, Any]] = []
            for row in rows:
                fp = row["fingerprint"]
                if allowed is not None and fp not in allowed:
                    continue
                if sig_on and not self._is_significant(
                    row["avg_pvalue"], row["avg_ci_low"]
                ):
                    continue
                if len(results) >= int(k):
                    break
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
                        "avg_pvalue": row["avg_pvalue"],
                        "avg_ci_low": row["avg_ci_low"],
                        "spec": json.loads(srow["spec_json"]),
                    }
                )
            conn.close()
            return results
        except Exception as exc:
            self.log.error("top_strategies failed: %s", exc)
            return []

    def _is_significant(self, avg_pvalue: Any, avg_ci_low: Any) -> bool:
        """
        Return True if a strategy's averaged significance stats clear the
        configured thresholds (A3 / P2.4). A missing/None p-value is treated as
        the conservative 1.0 (fails the gate) so legacy results without the
        P2.3 fields are not silently promoted. The win-rate lower-bound gate is
        only applied when min_winrate_ci_low > 0.
        """
        try:
            pvalue = 1.0 if avg_pvalue is None else float(avg_pvalue)
        except (TypeError, ValueError):
            pvalue = 1.0
        if pvalue > self.sig_max_pvalue:
            return False
        if self.sig_min_winrate_ci_low > 0.0:
            try:
                ci_low = 0.0 if avg_ci_low is None else float(avg_ci_low)
            except (TypeError, ValueError):
                ci_low = 0.0
            if ci_low < self.sig_min_winrate_ci_low:
                return False
        return True

    # ------------------------------------------------------------------ #
    def update_registry(self, symbol: str, timeframe: str,
                        rank_metric: str = "expectancy",
                        min_trades: int = 30,
                        allowed_fingerprints: Optional[Any] = None
                        ) -> Dict[str, Any]:
        """
        Recompute the best strategies for symbol/timeframe and write them into
        the JSON registry. Returns the registry section that was written.

        allowed_fingerprints (A2 / P1.4): forwarded to top_strategies so the
        locked-holdout gate can restrict promotion to holdout-passing specs.
        None (default) keeps the previous unfiltered behavior.

        Statistical-significance filter (A3 / P2.4): top_strategies applies the
        memory.search.significance gate by default, so a strategy that is not
        statistically distinguishable from random is recorded in SQLite but is
        never promoted into this registry. Disable it via config
        (memory.search.significance.enabled: false).
        """
        best = self.top_strategies(symbol, timeframe, self.top_k,
                                   rank_metric, min_trades,
                                   allowed_fingerprints=allowed_fingerprints)
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

    # ------------------------------------------------------------------ #
    # Phase 5 / P5.2: persist the live strategy-council credibility so it
    # survives restarts. The council itself (core/strategy/council.py) is a
    # pure in-memory calculator; these two methods snapshot it into / restore it
    # from the `council` SQLite table using its to_dict()/load_dict() hooks.
    # Both degrade gracefully (log + no-op on any failure) so a DB problem never
    # crashes the live path.
    # ------------------------------------------------------------------ #
    def save_council(self, council: Any) -> None:
        """
        Snapshot a StrategyCouncil's per-strategy rolling rewards into SQLite.

        Uses INSERT OR REPLACE keyed on fingerprint so repeated saves simply
        overwrite the latest window. Accepts anything exposing ``to_dict()``
        returning {"arms": {fingerprint: {"rewards": [...], "total_seen": int}}}.
        """
        if council is None:
            return
        try:
            snapshot = council.to_dict()
        except Exception as exc:
            self.log.error("save_council: to_dict failed: %s", exc)
            return
        arms = snapshot.get("arms", {}) if isinstance(snapshot, dict) else {}
        if not isinstance(arms, dict):
            return
        try:
            conn = self._connect()
            cur = conn.cursor()
            now = time.time()
            for fp, arm in arms.items():
                if not fp or not isinstance(arm, dict):
                    continue
                rewards = arm.get("rewards", [])
                try:
                    total_seen = int(arm.get("total_seen", len(rewards)))
                except (TypeError, ValueError):
                    total_seen = len(rewards) if isinstance(rewards, list) else 0
                cur.execute(
                    "INSERT OR REPLACE INTO council "
                    "(fingerprint, rewards_json, total_seen, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (fp, json.dumps(rewards), total_seen, now),
                )
            conn.commit()
            conn.close()
        except Exception as exc:
            self.log.error("save_council failed: %s", exc)

    def load_council(self, council: Any) -> Any:
        """
        Restore a StrategyCouncil's rolling rewards from SQLite into ``council``.

        Reads every row of the `council` table and feeds it to the council's
        ``load_dict``. Returns the same council object (for chaining). Missing
        table / empty DB / malformed rows are tolerated and simply leave the
        council cold.
        """
        if council is None:
            return council
        try:
            conn = self._connect()
            cur = conn.cursor()
            cur.execute(
                "SELECT fingerprint, rewards_json, total_seen FROM council"
            )
            rows = cur.fetchall()
            conn.close()
        except Exception as exc:
            self.log.error("load_council read failed: %s", exc)
            return council
        arms: Dict[str, Any] = {}
        for row in rows:
            fp = row["fingerprint"]
            if not fp:
                continue
            try:
                rewards = json.loads(row["rewards_json"] or "[]")
            except (TypeError, ValueError):
                rewards = []
            if not isinstance(rewards, list):
                rewards = []
            try:
                total_seen = int(row["total_seen"])
            except (TypeError, ValueError):
                total_seen = len(rewards)
            arms[fp] = {"rewards": rewards, "total_seen": total_seen}
        try:
            council.load_dict({"arms": arms})
        except Exception as exc:
            self.log.error("load_council load_dict failed: %s", exc)
        return council
