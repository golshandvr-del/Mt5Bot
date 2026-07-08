"""
News analyzer / aggregator (Phase 4).

Ties the news layer together:
  1. Builds enabled sources (RSS, NewsAPI) and the sentiment backend.
  2. Fetches items (with an on-disk cache to avoid hammering sources).
  3. Scores each item's sentiment and maps items to relevant symbols using a
     configurable keyword map.
  4. Aggregates per-symbol scores, weighting recent items more heavily, into a
     single directional news signal in [-1, +1].
  5. Exposes a blackout check so the decision engine can avoid trading right
     around fresh, high-impact news.

The whole layer degrades gracefully: if nothing is enabled, network is down, or
no items match, get_signal() returns a neutral 0.0 and the bot keeps running.

All text is standard ASCII English only.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

from core.news.base import NewsItem, NewsSource
from core.news.sentiment import build_sentiment
from core.news.sources import build_sources
from core.utils.helpers import read_json, write_json, ensure_dir
from core.utils.logger import get_logger


# Default keyword -> symbol relevance map. A news item is considered relevant
# to a symbol if any of that symbol's keywords appear in the item text. The map
# is intentionally simple and can be extended freely without code changes
# elsewhere. All keywords are lowercase ASCII.
_DEFAULT_SYMBOL_KEYWORDS: Dict[str, List[str]] = {
    "EURUSD": ["euro", "eur", "ecb", "eurozone", "european central bank",
               "dollar", "usd", "fed", "federal reserve"],
    "GBPUSD": ["pound", "sterling", "gbp", "boe", "bank of england",
               "united kingdom", "britain", "dollar", "usd", "fed"],
    "USDJPY": ["yen", "jpy", "boj", "bank of japan", "japan", "dollar", "usd",
               "fed"],
    # XAUUSD (gold) is this bot's dedicated instrument, so its keyword set is
    # richer: the drivers that actually move gold are the USD, Fed policy /
    # interest rates, real yields, inflation/CPI, and safe-haven / geopolitical
    # risk. More relevant keywords -> a more informative news signal for gold.
    "XAUUSD": ["gold", "xau", "xauusd", "bullion", "precious metal",
               "safe haven", "safe-haven", "dollar", "usd", "dxy",
               "inflation", "cpi", "ppi", "fed", "federal reserve",
               "interest rate", "rate cut", "rate hike", "fomc",
               "treasury yield", "real yield", "bond yield",
               "geopolitical", "war", "recession", "risk-off"],
    "USDCAD": ["loonie", "cad", "boc", "bank of canada", "canada", "oil",
               "crude", "dollar", "usd"],
    "AUDUSD": ["aussie", "aud", "rba", "reserve bank of australia",
               "australia", "dollar", "usd"],
    "BTCUSD": ["bitcoin", "btc", "crypto", "cryptocurrency"],
}

# Currency-direction hints: for a PAIR like BASE/QUOTE (e.g. EUR/USD), positive
# news about the BASE currency or negative news about the QUOTE currency is
# bullish for the pair. We keep it simple: default relevance treats overall
# item sentiment as bullish/bearish for the pair directly. Advanced per-currency
# attribution can be layered later without breaking this interface.


class NewsAnalyzer(object):
    """Fetches, scores, caches, and aggregates market news into a signal."""

    def __init__(self, cfg: Any):
        self.cfg = cfg
        self.log = get_logger("news.aggregator", cfg)
        self.enabled = bool(cfg.get_path("news.enabled", True))
        self.degrade = bool(cfg.get_path("news.degrade_gracefully", True))
        self.signal_weight = float(cfg.get_path("news.signal_weight", 0.2))
        self.blackout_minutes = int(cfg.get_path("news.blackout_minutes", 15))
        self.cache_ttl = int(cfg.get_path("news.cache_ttl_minutes", 30)) * 60

        root = cfg.get("project_root", ".")
        cache_rel = cfg.get_path("news.cache_dir", "data_store/news_cache")
        self.cache_dir = cache_rel if os.path.isabs(cache_rel) else os.path.join(root, cache_rel)
        ensure_dir(self.cache_dir)
        self.cache_file = os.path.join(self.cache_dir, "news_cache.json")

        # Symbol keyword map (overridable via config.news.symbol_keywords).
        self.symbol_keywords = dict(_DEFAULT_SYMBOL_KEYWORDS)
        cfg_map = cfg.get_path("news.symbol_keywords", {})
        if hasattr(cfg_map, "items"):
            for sym, words in cfg_map.items():
                try:
                    self.symbol_keywords[str(sym).upper()] = [str(w).lower() for w in words]
                except Exception:
                    continue

        # Lazily built to keep import light and avoid network on construction.
        self._sources: Optional[List[NewsSource]] = None
        self._sentiment = None
        # In-memory scored items for the current process run.
        self._items: List[NewsItem] = []
        self._last_refresh_ts: float = 0.0

    # ------------------------------------------------------------------ #
    def _ensure_built(self) -> None:
        if self._sources is None:
            self._sources = build_sources(self.cfg)
        if self._sentiment is None:
            self._sentiment = build_sentiment(self.cfg)

    # ------------------------------------------------------------------ #
    def _load_cache(self) -> Optional[List[NewsItem]]:
        data = read_json(self.cache_file, default=None)
        if not data:
            return None
        try:
            fetched_at = float(data.get("fetched_at", 0.0))
            if (time.time() - fetched_at) > self.cache_ttl:
                return None  # stale
            items = [NewsItem.from_dict(d) for d in data.get("items", [])]
            self.log.info("Loaded %d news items from cache.", len(items))
            return items
        except Exception:
            return None

    def _save_cache(self, items: List[NewsItem]) -> None:
        payload = {
            "fetched_at": time.time(),
            "items": [it.to_dict() for it in items],
        }
        write_json(self.cache_file, payload)

    # ------------------------------------------------------------------ #
    def refresh(self, force: bool = False) -> List[NewsItem]:
        """
        Fetch news (using cache when fresh), score sentiment, tag symbols, and
        store the scored items. Returns the scored items. Never raises.
        """
        if not self.enabled:
            self._items = []
            return self._items

        self._ensure_built()

        # Use cache unless forced or stale.
        if not force:
            cached = self._load_cache()
            if cached is not None:
                self._items = self._score_and_tag(cached)
                self._last_refresh_ts = time.time()
                return self._items

        # Fetch fresh from all sources.
        raw: List[NewsItem] = []
        for src in (self._sources or []):
            try:
                raw.extend(src.fetch())
            except Exception as exc:
                self.log.error("Source %s failed: %s", getattr(src, "name", "?"), exc)

        if not raw:
            if self.degrade:
                self.log.info("No news fetched; degrading to neutral signal.")
                self._items = []
                self._last_refresh_ts = time.time()
                return self._items

        scored = self._score_and_tag(raw)
        self._save_cache(scored)
        self._items = scored
        self._last_refresh_ts = time.time()
        self.log.info("Refreshed news: %d scored items.", len(scored))
        return scored

    def _score_and_tag(self, items: List[NewsItem]) -> List[NewsItem]:
        """Assign sentiment scores and relevant symbols to each item."""
        self._ensure_built()
        out: List[NewsItem] = []
        for it in items:
            text = it.text()
            low = text.lower()
            try:
                res = self._sentiment.score(text)
                it.score = res.score
            except Exception:
                it.score = 0.0
            # Tag relevant symbols by keyword match.
            symbols: List[str] = []
            for sym, words in self.symbol_keywords.items():
                for w in words:
                    if w in low:
                        symbols.append(sym)
                        break
            it.symbols = symbols
            out.append(it)
        return out

    # ------------------------------------------------------------------ #
    def get_signal(self, symbol: str, refresh: bool = False) -> float:
        """
        Return the aggregated news signal in [-1, +1] for a symbol.

        Recent items are weighted more heavily via an exponential time-decay.
        Items with no matched symbol are given a small "market-wide" weight so a
        strong macro tone still nudges the signal. Returns 0.0 when there is no
        usable news (graceful degradation).
        """
        if not self.enabled:
            return 0.0
        if refresh or not self._items:
            self.refresh(force=refresh)
        if not self._items:
            return 0.0

        symbol = (symbol or "").upper()
        now = time.time()
        # Half-life for time decay (hours): recent news matters more.
        half_life_s = 6.0 * 3600.0

        weighted_sum = 0.0
        weight_total = 0.0
        for it in self._items:
            if it.score == 0.0:
                continue
            # Relevance weight: direct symbol match = 1.0, macro-wide = 0.25.
            if symbol in it.symbols:
                relevance = 1.0
            elif not it.symbols:
                relevance = 0.25
            else:
                # Item is about other symbols only: minimal spillover.
                relevance = 0.1
            # Time decay.
            if it.published_ts > 0:
                age = max(0.0, now - it.published_ts)
                decay = 0.5 ** (age / half_life_s)
            else:
                decay = 0.5  # unknown time: neutral-ish weight
            w = relevance * decay
            weighted_sum += w * it.score
            weight_total += w

        if weight_total <= 0.0:
            return 0.0
        signal = weighted_sum / weight_total
        return max(-1.0, min(1.0, signal))

    # ------------------------------------------------------------------ #
    def in_blackout(self, symbol: str) -> bool:
        """
        Return True if there is very recent, strongly-polarized news for the
        symbol within the configured blackout window. The decision engine can
        use this to avoid opening new trades during volatile news bursts.
        """
        if not self.enabled or self.blackout_minutes <= 0:
            return False
        if not self._items:
            return False
        symbol = (symbol or "").upper()
        now = time.time()
        window = self.blackout_minutes * 60
        for it in self._items:
            if it.published_ts <= 0:
                continue
            if (now - it.published_ts) > window:
                continue
            relevant = (symbol in it.symbols) or (not it.symbols)
            if relevant and abs(it.score) >= 0.6:
                return True
        return False

    # ------------------------------------------------------------------ #
    def summary(self, symbol: str) -> Dict[str, Any]:
        """Return a small human-readable summary for logging/inspection."""
        sig = self.get_signal(symbol)
        relevant = [it for it in self._items
                    if (symbol.upper() in it.symbols) or (not it.symbols)]
        return {
            "symbol": symbol.upper(),
            "signal": round(sig, 4),
            "total_items": len(self._items),
            "relevant_items": len(relevant),
            "blackout": self.in_blackout(symbol),
            "weight": self.signal_weight,
        }
