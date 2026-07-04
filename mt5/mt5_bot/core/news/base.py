"""
Base classes and common interfaces for the news analysis layer (Phase 4).

The news layer converts raw headlines/articles into a single directional signal
in [-1, +1] used by the decision engine:
    +1 = strongly bullish news, 0 = neutral/no data, -1 = strongly bearish.

Design principles
-----------------
- Every concrete news source subclasses NewsSource and implements fetch().
- Sources must NEVER raise on network failure; they return [] and log instead,
  so the live bot keeps running offline (config.news.degrade_gracefully).
- Sentiment backends subclass SentimentBackend and implement score(text).
- Everything is standard-library only by default (urllib + a tiny RSS/XML
  parser + a built-in lexicon). Optional backends (VADER, NewsAPI) are used
  only when explicitly enabled and importable.

All text is standard ASCII English only.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional


class NewsItem(object):
    """A single normalized news headline/article."""

    __slots__ = ("title", "summary", "source", "url", "published_ts",
                 "symbols", "score")

    def __init__(
        self,
        title: str = "",
        summary: str = "",
        source: str = "",
        url: str = "",
        published_ts: float = 0.0,
        symbols: Optional[List[str]] = None,
        score: float = 0.0,
    ):
        self.title = title or ""
        self.summary = summary or ""
        self.source = source or ""
        self.url = url or ""
        # Unix timestamp (seconds). 0 means unknown.
        self.published_ts = float(published_ts or 0.0)
        # Symbols/keywords this item is deemed relevant to.
        self.symbols = symbols or []
        # Sentiment score in [-1, +1] filled in by the analyzer.
        self.score = float(score)

    def text(self) -> str:
        """Combined title + summary used for sentiment scoring."""
        return ("%s. %s" % (self.title, self.summary)).strip()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "summary": self.summary,
            "source": self.source,
            "url": self.url,
            "published_ts": self.published_ts,
            "symbols": self.symbols,
            "score": self.score,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "NewsItem":
        return cls(
            title=data.get("title", ""),
            summary=data.get("summary", ""),
            source=data.get("source", ""),
            url=data.get("url", ""),
            published_ts=data.get("published_ts", 0.0),
            symbols=data.get("symbols", []),
            score=data.get("score", 0.0),
        )


class SentimentResult(object):
    """Structured result of scoring a piece of text."""

    __slots__ = ("score", "positive", "negative", "hits")

    def __init__(self, score: float = 0.0, positive: int = 0,
                 negative: int = 0, hits: int = 0):
        # Normalized polarity in [-1, +1].
        self.score = float(score)
        self.positive = int(positive)
        self.negative = int(negative)
        # Total number of matched sentiment tokens (0 => no signal).
        self.hits = int(hits)


class SentimentBackend(object):
    """Abstract sentiment scorer. Subclasses implement score(text)."""

    name = "base"

    def __init__(self, cfg: Any):
        self.cfg = cfg
        self.available = True

    def score(self, text: str) -> SentimentResult:
        """Return a SentimentResult for the given text. Override this."""
        raise NotImplementedError


class NewsSource(object):
    """
    Abstract news source. Subclasses implement fetch() and return a list of
    NewsItem objects. Sources must handle their own errors and never raise.
    """

    name = "base"

    def __init__(self, cfg: Any, source_cfg: Any):
        self.cfg = cfg
        self.source_cfg = source_cfg
        self.enabled = bool(source_cfg.get("enabled", False)) if hasattr(source_cfg, "get") else False

    def fetch(self) -> List[NewsItem]:
        """Return a list of NewsItem. Must not raise on failure (return [])."""
        raise NotImplementedError

    @staticmethod
    def now_ts() -> float:
        return time.time()
