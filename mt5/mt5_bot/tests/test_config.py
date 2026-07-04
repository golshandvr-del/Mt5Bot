"""
Tests for the config loader (config/loader.py).

Verifies that the master config loads (via PyYAML or the built-in minimal
fallback parser), exposes dotted access, and resolves paths relative to the
project root.

All text is standard ASCII English only.
"""

from __future__ import annotations

import os
import unittest

from tests.helpers import PROJECT_ROOT  # noqa: F401  (path side-effect)


class TestConfigLoader(unittest.TestCase):
    def setUp(self):
        from config.loader import load_config
        self.cfg = load_config()

    def test_basic_sections_present(self):
        self.assertIsNotNone(self.cfg.get_path("general.mode"))
        self.assertIsNotNone(self.cfg.get_path("mt5.symbols"))
        self.assertIsNotNone(self.cfg.get_path("decision.weights"))

    def test_dotted_default_when_missing(self):
        self.assertEqual(self.cfg.get_path("does.not.exist", "fallback"),
                         "fallback")

    def test_symbols_is_list(self):
        symbols = self.cfg.get_path("mt5.symbols", [])
        self.assertIsInstance(symbols, list)
        self.assertGreater(len(symbols), 0)

    def test_resolve_path_is_absolute(self):
        from config.loader import resolve_path
        p = resolve_path(self.cfg, "data_store")
        self.assertTrue(os.path.isabs(p))


if __name__ == "__main__":
    unittest.main()
