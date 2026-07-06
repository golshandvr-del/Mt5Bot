"""
Convenience test runner.

Discovers and runs every test in this folder without needing to remember the
unittest discovery flags. Exits non-zero if any test fails, so it is CI-friendly.

Usage:
    python tests/run_all.py

All text is standard ASCII English only.
"""

from __future__ import annotations

import os
import sys
import unittest


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    if root not in sys.path:
        sys.path.insert(0, root)

    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=here, pattern="test_*.py",
                            top_level_dir=root)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
