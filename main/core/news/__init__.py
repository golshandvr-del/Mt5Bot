# News analysis layer (Phase 4).
#
# Analyzes market news/sentiment and produces a single directional news signal
# in [-1, +1] that the decision engine can blend with indicator and learning
# signals. All sources are configurable and the whole layer degrades gracefully
# to a neutral signal when offline or when optional dependencies are missing.
from core.news.base import NewsItem, NewsSource, SentimentResult  # noqa: F401
from core.news.sentiment import LexiconSentiment, build_sentiment  # noqa: F401
from core.news.aggregator import NewsAnalyzer  # noqa: F401
