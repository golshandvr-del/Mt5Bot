"""
Tests for the time-bucket Bayesian shrinkage in TimeStats (Phase 3,
Track A / P3.1; this file is sub-step P3.2).

These are offline, standard-library-only tests. They exercise two layers:

  1. The pure edge math (TimeStats._edge_from_row): a small (5-sample) bucket's
     edge is heavily shrunk toward 0, while a large (500-sample) bucket keeps
     nearly its raw edge; shrinkage=None reproduces the pre-P3.1
     n / (n + min_samples) behavior; and shrinkage <= 0 disables shrinkage
     (raw edge, no damping regardless of n).
  2. The full record -> query round-trip (TimeStats.record_trades ->
     TimeStats.bucket_edge) against a TEMP SQLite DB so the real
     data_store/memory.sqlite is never touched. This confirms the config
     `timing.learning.shrinkage` / `min_samples` knobs flow through to the
     served edge, and that persistence survives a fresh TimeStats instance
     (restart simulation).

All text is standard ASCII English only.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from tests.helpers import PROJECT_ROOT  # noqa: F401


def _positive_row(n, avg=1.0, spread=0.2):
    """
    Build a raw aggregate row for `n` consistently-positive trades.

    Every trade is a win with mean PnL `avg` and a small spread, so the base
    (pre-shrinkage) edge is strongly positive and well away from 0. Keeping the
    per-trade statistics identical across `n` lets us isolate the effect of the
    sample count on the shrinkage factor alone.
    """
    # sum_pnl = n * avg. To fix the variance regardless of n, use
    # var = spread^2 -> sum_pnl2 = n * (avg^2 + spread^2).
    sum_pnl = float(n) * avg
    sum_pnl2 = float(n) * (avg * avg + spread * spread)
    return {"n": int(n), "wins": int(n), "sum_pnl": sum_pnl, "sum_pnl2": sum_pnl2}


class TestEdgeShrinkageMath(unittest.TestCase):
    """Directly exercise the static shrinkage formula."""

    def test_small_bucket_heavily_shrunk_large_bucket_nearly_raw(self):
        from core.timing.time_stats import TimeStats
        min_samples = 50
        shrinkage = 50.0
        row5 = _positive_row(5)
        row500 = _positive_row(500)

        e5 = TimeStats._edge_from_row(row5, min_samples, shrinkage)
        e500 = TimeStats._edge_from_row(row500, min_samples, shrinkage)
        # The raw (unshrunk) base edge, for comparison.
        raw = TimeStats._edge_from_row(row5, min_samples, 0.0)["edge"]

        self.assertGreater(raw, 0.5)  # base edge is strongly positive
        # A 5-sample bucket keeps only 5 / (5 + 50) ~= 9% of its base edge.
        self.assertLess(e5["edge"], 0.15 * raw)
        # A 500-sample bucket keeps 500 / (500 + 50) ~= 91% of its base edge.
        self.assertGreater(e500["edge"], 0.85 * raw)
        # And the big bucket must dominate the small one.
        self.assertGreater(e500["edge"], 5.0 * e5["edge"])

    def test_trust_threshold_is_min_samples_not_shrinkage(self):
        from core.timing.time_stats import TimeStats
        # trusted is governed by min_samples only, independent of shrinkage.
        e5 = TimeStats._edge_from_row(_positive_row(5), 50, 50.0)
        e500 = TimeStats._edge_from_row(_positive_row(500), 50, 50.0)
        self.assertFalse(e5["trusted"])
        self.assertTrue(e500["trusted"])
        # Exactly at the threshold -> trusted.
        e50 = TimeStats._edge_from_row(_positive_row(50), 50, 50.0)
        self.assertTrue(e50["trusted"])

    def test_shrinkage_none_reproduces_old_behavior(self):
        from core.timing.time_stats import TimeStats
        # shrinkage=None must equal shrinkage=min_samples (the pre-P3.1 default).
        for n in (5, 50, 500):
            row = _positive_row(n)
            old = TimeStats._edge_from_row(row, 50, None)["edge"]
            equiv = TimeStats._edge_from_row(row, 50, 50.0)["edge"]
            self.assertAlmostEqual(old, equiv, places=12)

    def test_shrinkage_zero_disables_damping(self):
        from core.timing.time_stats import TimeStats
        # With shrinkage <= 0 the edge is the raw base regardless of n, so a
        # 5-sample bucket and a 500-sample bucket share the same edge.
        e5 = TimeStats._edge_from_row(_positive_row(5), 50, 0.0)
        e500 = TimeStats._edge_from_row(_positive_row(500), 50, 0.0)
        self.assertAlmostEqual(e5["edge"], e500["edge"], places=12)
        self.assertGreater(e5["edge"], 0.5)

    def test_empty_bucket_is_neutral(self):
        from core.timing.time_stats import TimeStats
        e = TimeStats._edge_from_row({"n": 0}, 50, 50.0)
        self.assertEqual(e["edge"], 0.0)
        self.assertFalse(e["trusted"])


class TestRecordAndServeShrinkage(unittest.TestCase):
    """Full record -> persist -> serve round-trip against a temp DB."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(
            prefix="test_timestats_", suffix=".sqlite", delete=False
        )
        self._tmp.close()
        self.db_path = self._tmp.name

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def _cfg(self, shrinkage=50, min_samples=50):
        from config.loader import load_config
        cfg = load_config()
        # Point the memory DB at our temp file so the real store is untouched.
        cfg["memory"]["db_file"] = self.db_path
        learning = cfg["timing"].setdefault("learning", {})
        learning["min_samples"] = min_samples
        learning["shrinkage"] = shrinkage
        return cfg

    def _trades(self, n, hour_ts, pnl=1.0):
        """n trades that all land on the same UTC hour, all winning."""
        out = []
        for i in range(n):
            # Same hour bucket each time; spacing is irrelevant to the bucket.
            out.append({"entry_ts": hour_ts + i, "pnl": pnl})
        return out

    def test_recorded_small_vs_large_bucket_edge(self):
        from core.timing.time_stats import TimeStats
        cfg = self._cfg(shrinkage=50, min_samples=50)
        ts = TimeStats(cfg)

        # Two different symbols so their buckets never mix. 2026-01-05 is a
        # Monday; 12:00 UTC lands in the London/New York overlap.
        base_ts = 1767614400  # 2026-01-05 12:00:00 UTC
        ts.record_trades("SMALLSYM", "M15", self._trades(5, base_ts))
        ts.record_trades("BIGSYM", "M15", self._trades(500, base_ts))

        small = ts.bucket_edge("SMALLSYM", "M15", "hour", "h12_15")
        big = ts.bucket_edge("BIGSYM", "M15", "hour", "h12_15")

        self.assertEqual(small["n"], 5)
        self.assertEqual(big["n"], 500)
        self.assertFalse(small["trusted"])
        self.assertTrue(big["trusted"])
        # Same per-trade profile -> shrinkage alone makes the big bucket's edge
        # far larger than the small one's.
        self.assertGreater(big["edge"], small["edge"])
        self.assertGreater(big["edge"], 5.0 * max(small["edge"], 1e-9))

    def test_persistence_survives_new_instance(self):
        from core.timing.time_stats import TimeStats
        cfg = self._cfg(shrinkage=50, min_samples=50)
        ts = TimeStats(cfg)
        base_ts = 1767614400  # 2026-01-05 12:00:00 UTC
        ts.record_trades("PERSYM", "M15", self._trades(500, base_ts))
        first = ts.bucket_edge("PERSYM", "M15", "hour", "h12_15")["edge"]

        # New instance, same DB path -> learned edge must reload (restart sim).
        ts2 = TimeStats(cfg)
        again = ts2.bucket_edge("PERSYM", "M15", "hour", "h12_15")["edge"]
        self.assertAlmostEqual(first, again, places=12)
        self.assertGreater(again, 0.5)

    def test_config_shrinkage_zero_gives_raw_edge(self):
        from core.timing.time_stats import TimeStats
        base_ts = 1767614400  # 2026-01-05 12:00:00 UTC

        cfg_shrink = self._cfg(shrinkage=50, min_samples=50)
        ts_shrink = TimeStats(cfg_shrink)
        ts_shrink.record_trades("SYMA", "M15", self._trades(5, base_ts))
        shrunk = ts_shrink.bucket_edge("SYMA", "M15", "hour", "h12_15")["edge"]

        cfg_raw = self._cfg(shrinkage=0, min_samples=50)
        ts_raw = TimeStats(cfg_raw)
        ts_raw.record_trades("SYMB", "M15", self._trades(5, base_ts))
        raw = ts_raw.bucket_edge("SYMB", "M15", "hour", "h12_15")["edge"]

        # With shrinkage disabled the 5-sample edge is far stronger.
        self.assertGreater(raw, shrunk)
        self.assertGreater(raw, 0.5)


if __name__ == "__main__":
    unittest.main()
