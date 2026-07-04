"""
News sources for the news layer (Phase 4).

Two configurable sources:
  1. RssSource      : fetches and parses RSS/Atom feeds using only the Python
                      standard library (urllib + a tiny regex/XML parser). No
                      API key required. This is the default.
  2. NewsApiSource  : optional; uses newsapi.org via a simple HTTPS GET when an
                      api_key is provided in config. Off by default.

Both sources:
  - Have a short network timeout so the bot is never blocked for long.
  - Return an empty list (never raise) on any failure, honoring the
    "degrade_gracefully" rule.
  - Return normalized NewsItem objects.

All text is standard ASCII English only.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, List, Optional
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

from core.news.base import NewsItem, NewsSource
from core.utils.logger import get_logger


# Default network timeout (seconds). Keep small so live loops stay responsive.
_HTTP_TIMEOUT = 8

# A neutral, standard user agent (some feeds reject empty UA strings).
_USER_AGENT = "Mozilla/5.0 (compatible; MT5SmartBot/1.0; +news-fetch)"


def _http_get(url: str, timeout: int = _HTTP_TIMEOUT) -> Optional[str]:
    """Perform a simple HTTP GET; return the body text or None on any failure."""
    try:
        req = Request(url, headers={"User-Agent": _USER_AGENT})
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        # Try utf-8 first, then latin-1 as a permissive fallback.
        try:
            return raw.decode("utf-8", errors="replace")
        except Exception:
            return raw.decode("latin-1", errors="replace")
    except Exception:
        return None


# -----------------------------------------------------------------------------
# Minimal RSS/Atom parsing using regex. This avoids a hard dependency on
# feedparser (which may not install cleanly on a minimal Windows 7 Python).
# It is intentionally lenient: it extracts <item>/<entry> blocks and pulls
# title, description/summary, link, and pubDate/updated when present.
# -----------------------------------------------------------------------------
_ITEM_RE = re.compile(r"<(item|entry)[\s>](.*?)</\1>", re.IGNORECASE | re.DOTALL)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_DESC_RE = re.compile(
    r"<(description|summary|content)[^>]*>(.*?)</\1>", re.IGNORECASE | re.DOTALL
)
_LINK_RE = re.compile(r"<link[^>]*>(.*?)</link>", re.IGNORECASE | re.DOTALL)
_LINK_HREF_RE = re.compile(r'<link[^>]*href="([^"]+)"', re.IGNORECASE)
_DATE_RE = re.compile(
    r"<(pubDate|published|updated|dc:date)[^>]*>(.*?)</\1>",
    re.IGNORECASE | re.DOTALL,
)
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_markup(text: str) -> str:
    """Remove CDATA wrappers, HTML tags, and decode a few common entities."""
    if not text:
        return ""
    # Unwrap CDATA.
    m = _CDATA_RE.search(text)
    if m:
        text = m.group(1)
    # Drop any remaining tags.
    text = _TAG_RE.sub(" ", text)
    # Decode a small set of common HTML entities (ASCII-safe replacements).
    replacements = {
        "&amp;": "and", "&lt;": "<", "&gt;": ">", "&quot;": '"',
        "&#39;": "'", "&apos;": "'", "&nbsp;": " ", "&mdash;": "-",
        "&ndash;": "-", "&hellip;": "...",
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    # Collapse whitespace.
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_rss_date(text: str) -> float:
    """Best-effort parse of an RSS/Atom date string into a unix timestamp."""
    text = (text or "").strip()
    if not text:
        return 0.0
    # Common RFC 822 and ISO 8601 formats seen in feeds.
    formats = [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    ]
    for fmt in formats:
        try:
            import datetime as _dt
            # Python 3.8 does not accept a literal 'Z' in %z; strip trailing Z.
            cleaned = text
            if fmt.endswith("Z") is False and cleaned.endswith("Z"):
                cleaned = cleaned[:-1]
            parsed = _dt.datetime.strptime(cleaned, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=_dt.timezone.utc)
            return parsed.timestamp()
        except Exception:
            continue
    return 0.0


class RssSource(NewsSource):
    """Fetch and parse RSS/Atom feeds listed in config.news.sources.rss.feeds."""

    name = "rss"

    def __init__(self, cfg: Any, source_cfg: Any):
        super().__init__(cfg, source_cfg)
        self.log = get_logger("news.sources.rss", cfg)
        feeds = source_cfg.get("feeds", []) if hasattr(source_cfg, "get") else []
        self.feeds: List[str] = [str(f) for f in feeds] if feeds else []

    def fetch(self) -> List[NewsItem]:
        if not self.enabled or not self.feeds:
            return []
        items: List[NewsItem] = []
        for url in self.feeds:
            body = _http_get(url)
            if not body:
                self.log.warning("RSS fetch failed or empty: %s", url)
                continue
            try:
                items.extend(self._parse_feed(body, url))
            except Exception as exc:
                self.log.error("RSS parse error for %s: %s", url, exc)
        self.log.info("RSS collected %d items from %d feeds.",
                      len(items), len(self.feeds))
        return items

    def _parse_feed(self, body: str, feed_url: str) -> List[NewsItem]:
        out: List[NewsItem] = []
        for match in _ITEM_RE.finditer(body):
            block = match.group(2)
            title_m = _TITLE_RE.search(block)
            desc_m = _DESC_RE.search(block)
            date_m = _DATE_RE.search(block)

            title = _strip_markup(title_m.group(1)) if title_m else ""
            summary = _strip_markup(desc_m.group(2)) if desc_m else ""

            # Link: try <link>text</link>, then <link href="..."/>.
            link = ""
            link_m = _LINK_RE.search(block)
            if link_m:
                link = _strip_markup(link_m.group(1))
            if not link:
                href_m = _LINK_HREF_RE.search(block)
                if href_m:
                    link = href_m.group(1).strip()

            published = _parse_rss_date(date_m.group(2)) if date_m else 0.0
            if not title and not summary:
                continue
            out.append(
                NewsItem(
                    title=title,
                    summary=summary,
                    source=feed_url,
                    url=link,
                    published_ts=published,
                )
            )
        return out


class NewsApiSource(NewsSource):
    """
    Optional newsapi.org source. Off by default. Requires an api_key in
    config.news.sources.newsapi.api_key. Uses the /v2/everything endpoint.
    """

    name = "newsapi"

    def __init__(self, cfg: Any, source_cfg: Any):
        super().__init__(cfg, source_cfg)
        self.log = get_logger("news.sources.newsapi", cfg)
        self.api_key = source_cfg.get("api_key", "") if hasattr(source_cfg, "get") else ""
        self.query = source_cfg.get("query", "forex OR currency") if hasattr(source_cfg, "get") else "forex"

    def fetch(self) -> List[NewsItem]:
        if not self.enabled:
            return []
        if not self.api_key:
            self.log.warning("NewsAPI enabled but no api_key provided; skipping.")
            return []
        url = (
            "https://newsapi.org/v2/everything?q=%s&language=en&sortBy="
            "publishedAt&pageSize=50&apiKey=%s"
            % (quote_plus(self.query), quote_plus(self.api_key))
        )
        body = _http_get(url)
        if not body:
            self.log.warning("NewsAPI fetch failed or empty.")
            return []
        try:
            data = json.loads(body)
        except Exception as exc:
            self.log.error("NewsAPI JSON parse error: %s", exc)
            return []
        if data.get("status") != "ok":
            self.log.warning("NewsAPI returned status=%s", data.get("status"))
            return []
        out: List[NewsItem] = []
        for art in data.get("articles", []):
            title = _strip_markup(art.get("title", "") or "")
            summary = _strip_markup(art.get("description", "") or "")
            published = _parse_rss_date(art.get("publishedAt", "") or "")
            src = ""
            if isinstance(art.get("source"), dict):
                src = art["source"].get("name", "newsapi")
            out.append(
                NewsItem(
                    title=title,
                    summary=summary,
                    source=src or "newsapi",
                    url=art.get("url", "") or "",
                    published_ts=published,
                )
            )
        self.log.info("NewsAPI collected %d items.", len(out))
        return out


def build_sources(cfg: Any) -> List[NewsSource]:
    """Build every enabled news source from config.news.sources."""
    log = get_logger("news.sources", cfg)
    sources: List[NewsSource] = []
    src_cfg = cfg.get_path("news.sources", {})
    if not hasattr(src_cfg, "get"):
        return sources

    rss_cfg = src_cfg.get("rss", {})
    if hasattr(rss_cfg, "get") and rss_cfg.get("enabled", False):
        sources.append(RssSource(cfg, rss_cfg))

    napi_cfg = src_cfg.get("newsapi", {})
    if hasattr(napi_cfg, "get") and napi_cfg.get("enabled", False):
        sources.append(NewsApiSource(cfg, napi_cfg))

    log.info("Built %d news source(s).", len(sources))
    return sources
