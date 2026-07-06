"""
Tests for the memory store (Phase 3 persistence).

Uses a temporary DB + registry so the real data_store is never touched. Verifies
that strategies/results record, aggregate, rank, persist to a JSON registry, and
survive being re-opened (restart simulation).

All text is standard ASCII English only.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from tests.helpers import PROJECT_ROOT  # noqa: F401


def _temp_cfg():
    """Load config, then point memory paths at a fresh temp directory."""
    from config.loader import load_config
    cfg = load_config()
    tmp = tempfile.mkdtemp(prefix="mt5bot_mem_")
    cfg["memory"]["db_file"] = os.path.join(tmp, "memory.sqlite")
    cfg["memory"]["registry_file"] = os.path.join(tmp, "registry.json")
    return cfg, tmp


def _sample_spec(symbol="TESTX", tf="M15"):
    from core.strategy.strategy import StrategySpec
    return StrategySpec(
        indicators={"ema": {"period": 21}, "rsi": {"period": 14}},
        weights={"ema": 1.0, "rsi": 1.0},
        long_threshold=0.3, short_threshold=0.3,
        sl_atr_mult=2.0, tp_atr_mult=3.0,
        symbol=symbol, timeframe=tf,
    )


class TestMemoryStore(unittest.TestCase):
    def setUp(self):
        from core.memory.store import MemoryStore
        self.cfg, self.tmp = _temp_cfg()
        self.store = MemoryStore(self.cfg)

    def test_record_and_stats(self):
        spec = _sample_spec()
        self.store.record_strategy(spec)
        metrics = {"win_rate": 0.55, "profit_factor": 1.4,
                   "expectancy": 5.0, "max_drawdown": -100.0,
                   "num_trades": 40, "sharpe": 0.5, "net_profit": 200.0,
                   "win_rate_ci_low": 0.45, "pnl_pvalue": 0.01}
        self.store.record_result(spec, metrics, segment=0, rank_metric="expectancy")
        stats = self.store.stats()
        self.assertGreaterEqual(stats.get("strategies", 0), 1)
        self.assertGreaterEqual(stats.get("results", 0), 1)

    def test_registry_persist_and_reload(self):
        from core.memory.store import MemoryStore
        spec = _sample_spec()
        self.store.record_strategy(spec)
        for seg in range(3):
            metrics = {"win_rate": 0.6, "profit_factor": 1.5,
                       "expectancy": 6.0, "max_drawdown": -80.0,
                       "num_trades": 45, "sharpe": 0.6, "net_profit": 300.0,
                       "win_rate_ci_low": 0.5, "pnl_pvalue": 0.01}
            self.store.record_result(spec, metrics, segment=seg,
                                     rank_metric="expectancy")
        self.store.update_registry(spec.symbol, spec.timeframe,
                                   rank_metric="expectancy", min_trades=1)
        top = self.store.load_registry_top(spec.symbol, spec.timeframe)
        self.assertGreaterEqual(len(top), 1)

        # Simulate a restart: open a brand-new store on the same files.
        reopened = MemoryStore(self.cfg)
        top2 = reopened.load_registry_top(spec.symbol, spec.timeframe)
        self.assertGreaterEqual(len(top2), 1)


if __name__ == "__main__":
    unittest.main()
