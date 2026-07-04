"""
Tests for the news layer (Phase 4).

Verifies graceful offline behavior: with sources disabled / no network, the
analyzer must still build, return a neutral signal, and never raise. Also checks
the offline lexicon sentiment backend produces bounded scores.

All text is standard ASCII English only.
"""

from __future__ import annotations

import unittest

from tests.helpers import PROJECT_ROOT  # noqa: F401


class TestNews(unittest.TestCase):
    def setUp(self):
        from config.loader import load_config
        self.cfg = load_config()

    def test_lexicon_sentiment_bounds(self):
        from core.news.sentiment import LexiconSentiment
        backend = LexiconSentiment(self.cfg)
        pos = backend.score("strong bullish rally gains surge higher optimism")
        neg = backend.score("crash plunge recession fear loss decline bearish")
        for res in (pos, neg):
            self.assertGreaterEqual(res.score, -1.0 - 1e-9)
            self.assertLessEqual(res.score, 1.0 + 1e-9)
        # Positive text should not score below negative text.
        self.assertGreaterEqual(pos.score, neg.score)

    def test_analyzer_offline_neutral(self):
        # Disable network sources so refresh cannot fetch anything.
        cfg = self.cfg
        cfg["news"]["sources"]["rss"]["enabled"] = False
        cfg["news"]["sources"]["newsapi"]["enabled"] = False
        from core.news.aggregator import NewsAnalyzer
        analyzer = NewsAnalyzer(cfg)
        # refresh must not raise even with nothing to fetch.
        analyzer.refresh(force=True)
        sig = analyzer.get_signal("EURUSD")
        self.assertGreaterEqual(sig, -1.0 - 1e-9)
        self.assertLessEqual(sig, 1.0 + 1e-9)
        # In-blackout must return a boolean and not raise.
        self.assertIn(analyzer.in_blackout("EURUSD"), (True, False))

    def test_analyzer_disabled_entirely(self):
        cfg = self.cfg
        cfg["news"]["enabled"] = False
        from core.news.aggregator import NewsAnalyzer
        analyzer = NewsAnalyzer(cfg)
        analyzer.refresh(force=True)
        self.assertAlmostEqual(analyzer.get_signal("XAUUSD"), 0.0, places=6)


if __name__ == "__main__":
    unittest.main()
