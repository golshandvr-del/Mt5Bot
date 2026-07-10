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
live_trades(id PK, fingerprint, pnl, created_at)  -- Phase 5 / P5.6 (decay monitor)

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


def _py_json_extract(text: Any, path: Any) -> Any:
    """
    JSON1-independent replacement for SQLite's built-in ``json_extract``.

    Older SQLite builds (notably the one bundled with some Windows 7 Python
    distributions) are compiled WITHOUT the JSON1 extension, so a SQL call to
    ``json_extract(metrics_json, '$.num_trades')`` raises
    ``no such function: json_extract`` and every ranking query silently returns
    nothing. To stay portable we register THIS Python implementation as a SQLite
    user function named ``json_extract`` on every connection, so the same SQL
    text works on every SQLite build regardless of JSON1.

    Only the simple top-level path form ``$.<key>`` (and bare ``<key>``) that the
    memory store actually uses is supported; any miss, malformed JSON, or
    unsupported path returns ``None`` (SQL NULL), matching how the built-in
    behaves for an absent key. It never raises, so a bad row can never crash a
    ranking query.
    """
    if text is None or path is None:
        return None
    try:
        obj = json.loads(text) if isinstance(text, (str, bytes, bytearray)) else text
    except (TypeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    key = str(path)
    if key.startswith("$."):
        key = key[2:]
    elif key == "$":
        return None
    # We only need flat, single-level keys for the stored metrics_json.
    value = obj.get(key)
    # SQLite json_extract returns scalars as-is; nested objects/arrays are
    # returned as JSON text. We only ever extract numeric scalars, but keep the
    # behavior reasonable for other types.
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, sort_keys=True)
        except (TypeError, ValueError):
            return None
    return value


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
        # Recency weighting (Track B / B8, P6.1). When aggregating a strategy's
        # per-segment scores, newer segments can count more than older ones via
        # a geometric decay. Read defensively; 1.0 (default) or anything outside
        # (0, 1] means OFF = the plain SQL AVG(score), byte-identical to before.
        try:
            rd = float(cfg.get_path("memory.walk_forward.recency_decay", 1.0))
        except (TypeError, ValueError):
            rd = 1.0
        if rd <= 0.0 or rd > 1.0:
            rd = 1.0
        self.recency_decay = rd
        self._init_db()

    # ------------------------------------------------------------------ #
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # Portability guard (Windows 7 / SQLite without JSON1): always register a
        # Python-side ``json_extract`` so ranking queries never hit
        # "no such function: json_extract". On builds that already ship JSON1 the
        # SQL text is identical and this user function simply shadows it with
        # equivalent behavior for the flat "$.key" paths we use.
        try:
            conn.create_function("json_extract", 2, _py_json_extract)
        except Exception as exc:  # pragma: no cover - extremely defensive
            self.log.error("could not register json_extract shim: %s", exc)
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
            # Phase 5 / P5.6 (Track B / B3): per-strategy realized live/paper trade
            # PnLs. The decay monitor compares this RECENT distribution against the
            # strategy's walk-forward reference to flag statistical expiry. One row
            # per closed trade; append-only, read as a trailing window.
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS live_trades (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT,
                    pnl         REAL,
                    created_at  REAL
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_live_trades_fp "
                "ON live_trades(fingerprint, id)"
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
                       apply_significance: bool = True,
                       score_overrides: Optional[Dict[str, float]] = None
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

        score_overrides (U4.4): optional {fingerprint: score} map. When a
        fingerprint has an override, that value REPLACES its walk-forward
        average as the ranking key (the search passes
        min(own_score, median_neighbor_score) here so a knife-edge peak is
        demoted below a broad robust plateau). The stored avg_score is left
        intact in the returned entry for transparency; only the ORDER changes.
        None (default) means no override and the ranking is unchanged.
        Overrides and recency weighting are mutually independent; if both are
        active the override wins (it is the intended promotion score).
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
            # B8 / P6.1: when recency weighting is ON (decay < 1.0), replace the
            # SQL plain-average score with a recency-weighted score (newer
            # segments count more) and RE-RANK the candidate rows by it. With the
            # default decay == 1.0 this block is skipped entirely, so the SQL
            # AVG(score) ordering is preserved byte-for-byte.
            recency_on = self.recency_decay < 1.0
            rec_scores: Dict[str, float] = {}
            if recency_on and rows:
                fps = [r["fingerprint"] for r in rows]
                rec_scores = self._recency_weighted_scores(
                    conn, symbol, timeframe, rank_metric, fps
                )
            # U4.4: per-fingerprint ranking override (min(own, neighbor)) takes
            # precedence over recency/plain-average when present.
            overrides = dict(score_overrides) if score_overrides else {}

            def _rank_key(r):
                fp = r["fingerprint"]
                if fp in overrides:
                    return overrides[fp]
                if recency_on:
                    return rec_scores.get(fp, r["avg_score"] or 0.0)
                return r["avg_score"] or 0.0

            # Re-rank only when an override or recency weighting is active; with
            # neither, the SQL AVG(score) DESC order is preserved byte-for-byte.
            if (recency_on or overrides) and rows:
                rows = sorted(rows, key=_rank_key, reverse=True)
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
                entry = {
                    "fingerprint": fp,
                    "avg_score": row["avg_score"],
                    "n_segments": row["n_segments"],
                    "avg_trades": row["avg_trades"],
                    "avg_pvalue": row["avg_pvalue"],
                    "avg_ci_low": row["avg_ci_low"],
                    "spec": json.loads(srow["spec_json"]),
                }
                if recency_on:
                    # Expose the recency-weighted score used for ranking; keep
                    # the plain average alongside it for transparency.
                    entry["recency_score"] = rec_scores.get(
                        fp, row["avg_score"]
                    )
                if fp in overrides:
                    # U4.4: expose the robustness-adjusted ranking score
                    # (min(own, median neighbor)) that decided this spec's rank.
                    entry["neighborhood_score"] = overrides[fp]
                results.append(entry)
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

    def _recency_weighted_scores(self, conn: Any, symbol: str, timeframe: str,
                                 rank_metric: str,
                                 fingerprints: List[str]
                                 ) -> Dict[str, float]:
        """Compute a recency-weighted score per fingerprint (B8 / P6.1).

        For each strategy, its per-segment scores are ordered OLDEST -> NEWEST
        (by created_at, then id, so insertion order breaks ties) and combined
        with a geometric decay: the i-th oldest gets weight decay ** (last - i),
        so the newest segment always weighs 1.0. Returns {fingerprint: score}.
        Only called when recency_decay < 1.0. On any failure a fingerprint is
        simply omitted (its plain SQL average is used as the ranking fallback).
        """
        d = self.recency_decay
        out: Dict[str, float] = {}
        if d <= 0.0 or d >= 1.0 or not fingerprints:
            return out
        try:
            cur = conn.cursor()
            for fp in fingerprints:
                cur.execute(
                    "SELECT score FROM results "
                    "WHERE symbol=? AND timeframe=? AND rank_metric=? "
                    "  AND fingerprint=? "
                    "ORDER BY created_at ASC, id ASC",
                    (symbol, timeframe, rank_metric, fp),
                )
                seg_scores = [
                    float(r["score"]) for r in cur.fetchall()
                    if r["score"] is not None
                ]
                if not seg_scores:
                    continue
                last = len(seg_scores) - 1
                num = 0.0
                wsum = 0.0
                for i, s in enumerate(seg_scores):
                    w = d ** (last - i)
                    num += w * s
                    wsum += w
                if wsum > 0:
                    out[fp] = num / wsum
        except Exception as exc:
            self.log.error("_recency_weighted_scores failed: %s", exc)
            return out
        return out

    # ------------------------------------------------------------------ #
    def update_registry(self, symbol: str, timeframe: str,
                        rank_metric: str = "expectancy",
                        min_trades: int = 30,
                        allowed_fingerprints: Optional[Any] = None,
                        apply_significance: bool = True,
                        score_overrides: Optional[Dict[str, float]] = None
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
        (memory.search.significance.enabled: false) or, per-call, by passing
        apply_significance=False (used by the rebuild-registry recovery path so
        an over-strict gate cannot leave the registry empty).

        score_overrides (U4.4): forwarded to top_strategies so the parameter-
        neighborhood robustness gate can rank finalists by
        min(own_score, median_neighbor_score) instead of the raw own score.
        None (default) leaves ranking unchanged.
        """
        best = self.top_strategies(symbol, timeframe, self.top_k,
                                   rank_metric, min_trades,
                                   allowed_fingerprints=allowed_fingerprints,
                                   apply_significance=apply_significance,
                                   score_overrides=score_overrides)
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

    def known_symbol_timeframes(self) -> List[Dict[str, str]]:
        """
        Return every distinct (symbol, timeframe) pair that has stored results.

        Used by the ``rebuild-registry`` recovery path so the JSON registry can
        be regenerated from the already-collected SQLite data WITHOUT re-running
        a search. Empty list on error / empty DB.
        """
        try:
            conn = self._connect()
            cur = conn.cursor()
            cur.execute(
                "SELECT DISTINCT symbol, timeframe FROM results "
                "WHERE symbol IS NOT NULL AND timeframe IS NOT NULL"
            )
            rows = cur.fetchall()
            conn.close()
        except Exception as exc:
            self.log.error("known_symbol_timeframes failed: %s", exc)
            return []
        out: List[Dict[str, str]] = []
        for r in rows:
            sym = r["symbol"]
            tf = r["timeframe"]
            if sym and tf:
                out.append({"symbol": str(sym), "timeframe": str(tf)})
        return out

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

    # ------------------------------------------------------------------ #
    # Phase 5 / P5.6 (Track B / B3): realized live/paper PnL capture per strategy
    # and the walk-forward reference distribution, both consumed by the decay
    # monitor (core/strategy/decay_monitor.py). All degrade gracefully (log +
    # no-op / empty on any failure) so a DB problem never crashes the live path.
    # ------------------------------------------------------------------ #
    def record_live_trade(self, fingerprint: str, pnl: float) -> None:
        """
        Append one realized live/paper trade PnL for a strategy fingerprint.

        Append-only; the decay monitor reads a trailing window via
        ``recent_live_pnls``. A missing/empty fingerprint is ignored so trades
        not attributable to a registry strategy never pollute the table.
        """
        if not fingerprint:
            return
        try:
            conn = self._connect()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO live_trades (fingerprint, pnl, created_at) "
                "VALUES (?, ?, ?)",
                (str(fingerprint), float(pnl), time.time()),
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            self.log.error("record_live_trade failed: %s", exc)

    def live_trade_fingerprints(self) -> List[str]:
        """
        Return the distinct strategy fingerprints that have at least one recorded
        live/paper trade. Used by the decay monitor to know which strategies have
        enough live evidence to be assessed. Empty list on error / empty table.
        """
        try:
            conn = self._connect()
            cur = conn.cursor()
            cur.execute(
                "SELECT DISTINCT fingerprint FROM live_trades "
                "WHERE fingerprint IS NOT NULL AND fingerprint <> ''"
            )
            rows = cur.fetchall()
            conn.close()
        except Exception as exc:
            self.log.error("live_trade_fingerprints failed: %s", exc)
            return []
        return [r["fingerprint"] for r in rows if r["fingerprint"]]

    def recent_live_pnls(self, fingerprint: str,
                         limit: int = 100) -> List[float]:
        """
        Return up to ``limit`` most-recent realized live/paper PnLs for a
        strategy, oldest-first. Empty list when the strategy has no live trades
        or on any error.
        """
        if not fingerprint:
            return []
        try:
            conn = self._connect()
            cur = conn.cursor()
            cur.execute(
                "SELECT pnl FROM live_trades WHERE fingerprint=? "
                "ORDER BY id DESC LIMIT ?",
                (str(fingerprint), int(max(1, limit))),
            )
            rows = cur.fetchall()
            conn.close()
        except Exception as exc:
            self.log.error("recent_live_pnls failed: %s", exc)
            return []
        # Rows come newest-first; reverse to oldest-first for a natural series.
        pnls = [float(r["pnl"]) for r in rows if r["pnl"] is not None]
        pnls.reverse()
        return pnls

    def reference_pnls(self, fingerprint: str,
                       rank_metric: str = "expectancy") -> List[float]:
        """
        Return the strategy's walk-forward REFERENCE distribution of mean
        per-trade PnL: one ``expectancy`` value per stored backtest segment.

        The decay monitor compares the strategy's recent live mean PnL against
        this distribution. Using the per-segment expectancy (mean trade PnL on
        each walk-forward slice) keeps both sides on the same "mean trade PnL"
        scale. Empty list when the strategy has no stored results or on error.
        """
        if not fingerprint:
            return []
        try:
            conn = self._connect()
            cur = conn.cursor()
            cur.execute(
                "SELECT json_extract(metrics_json, '$.expectancy') AS exp "
                "FROM results WHERE fingerprint=? AND rank_metric=?",
                (str(fingerprint), rank_metric),
            )
            rows = cur.fetchall()
            conn.close()
        except Exception as exc:
            self.log.error("reference_pnls failed: %s", exc)
            return []
        out: List[float] = []
        for r in rows:
            val = r["exp"]
            if val is None:
                continue
            try:
                out.append(float(val))
            except (TypeError, ValueError):
                continue
        return out
