"""
Tests for EA-compatible search mode (UPGRADE_PLAN U2.2).

When config.memory.search.ea_compatible_only is true the StrategySearch spec
generators must draw directional voters ONLY from the EA-supported indicator
set (the same set the MQL5 EA can run), so any promoted strategy exports 1:1
with no dropped indicators. When false the full research indicator set is used.

Stdlib-only; no MT5, no network. Exercises the spec generator directly so the
test stays fast and does not require a walk-forward run.

All text is standard ASCII English only.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from tests.helpers import PROJECT_ROOT  # noqa: F401  (path side-effect)

from config.loader import load_config
from core.memory.store import MemoryStore
from core.strategy.search import (
    StrategySearch,
    _EA_SUPPORTED_DIRECTIONAL,
    _DIRECTIONAL,
)
# The exporter's authoritative EA-supported set (source of truth for parity).
from scripts.export_strategy_for_ea import EA_SUPPORTED_INDICATORS


def _make_search(ea_compatible_only):
    cfg = load_config()
    tmp = tempfile.mkdtemp(prefix="ea_search_test_")
    cfg["memory"]["db_file"] = os.path.join(tmp, "memory.sqlite")
    cfg["memory"]["registry_file"] = os.path.join(tmp, "registry.json")
    cfg["memory"]["search"]["ea_compatible_only"] = ea_compatible_only
    store = MemoryStore(cfg)
    return StrategySearch(cfg, store)


class TestEaCompatibleSearch(unittest.TestCase):
    def test_ea_directional_subset_of_exporter_set(self):
        """Every search-restricted voter must be EA-exportable."""
        for name in _EA_SUPPORTED_DIRECTIONAL:
            self.assertIn(
                name, EA_SUPPORTED_INDICATORS,
                "search EA set drifted from exporter: %s" % name,
            )
        # And every EA-supported directional voter must be a real directional
        # indicator (so the filter never yields an empty pool by typo).
        for name in _EA_SUPPORTED_DIRECTIONAL:
            self.assertIn(name, _DIRECTIONAL)

    def test_pool_filtered_when_flag_on(self):
        search = _make_search(ea_compatible_only=True)
        self.assertTrue(search.ea_compatible_only)
        pool = search._available_directional()
        self.assertTrue(pool, "EA-compatible pool must not be empty")
        for name in pool:
            self.assertIn(name, _EA_SUPPORTED_DIRECTIONAL)

    def test_pool_full_when_flag_off(self):
        search = _make_search(ea_compatible_only=False)
        self.assertFalse(search.ea_compatible_only)
        pool = search._available_directional()
        # The default set must include voters the EA cannot run (proving the
        # flag actually changes behavior).
        non_ea = [n for n in pool if n not in _EA_SUPPORTED_DIRECTIONAL]
        self.assertTrue(
            non_ea,
            "default search pool should contain non-EA voters",
        )

    def test_generated_random_specs_are_ea_only(self):
        """With the flag on, many random specs use only EA-supported voters."""
        search = _make_search(ea_compatible_only=True)
        for _ in range(200):
            spec = search._random_spec("TESTX", "M15")
            for name in spec.indicators.keys():
                self.assertIn(
                    name, EA_SUPPORTED_INDICATORS,
                    "EA-compatible search produced non-EA indicator: %s" % name,
                )


if __name__ == "__main__":
    unittest.main()
