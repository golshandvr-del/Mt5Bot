"""
Sentiment backends for the news layer (Phase 4).

Two backends:
  1. LexiconSentiment (default, offline, zero dependencies):
     A curated finance/forex sentiment lexicon plus simple negation handling.
     Works entirely offline, so it satisfies the "degrade gracefully" rule and
     needs nothing extra on Windows 7.
  2. VaderSentiment (optional): uses the vaderSentiment package if installed.
     If the import fails, build_sentiment() silently falls back to the lexicon.

The score returned is always normalized to [-1, +1]:
    +1 = very bullish/positive, 0 = neutral/no matched words, -1 = very bearish.

All text is standard ASCII English only.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from core.news.base import SentimentBackend, SentimentResult
from core.utils.logger import get_logger


# -----------------------------------------------------------------------------
# Built-in finance-oriented sentiment lexicon.
# Positive words push the score up (bullish); negative words push it down.
# Weights are rough magnitudes in (0, 2]. Kept ASCII-only and hand-curated for
# forex / macro / equities headlines.
# -----------------------------------------------------------------------------
_POSITIVE: Dict[str, float] = {
    "gain": 1.0, "gains": 1.0, "gained": 1.0, "rise": 1.0, "rises": 1.0,
    "rising": 1.0, "rose": 1.0, "rally": 1.4, "rallies": 1.4, "surge": 1.6,
    "surged": 1.6, "soar": 1.6, "soared": 1.6, "jump": 1.2, "jumped": 1.2,
    "climb": 1.0, "climbed": 1.0, "up": 0.6, "higher": 1.0, "boost": 1.2,
    "boosted": 1.2, "strong": 1.2, "stronger": 1.2, "strength": 1.2,
    "beat": 1.2, "beats": 1.2, "outperform": 1.4, "upgrade": 1.4,
    "upgraded": 1.4, "bullish": 1.8, "optimism": 1.2, "optimistic": 1.2,
    "positive": 1.0, "growth": 1.0, "expand": 1.0, "expansion": 1.0,
    "recovery": 1.2, "recover": 1.2, "rebound": 1.2, "record": 1.0,
    "profit": 1.2, "profits": 1.2, "profitable": 1.2, "improve": 1.0,
    "improved": 1.0, "improvement": 1.0, "support": 0.8, "supported": 0.8,
    "demand": 0.8, "hawkish": 1.0, "hike": 0.6, "resilient": 1.0,
    "solid": 1.0, "robust": 1.2, "accelerate": 1.0, "accelerated": 1.0,
    "buy": 1.0, "buying": 1.0, "inflow": 1.0, "inflows": 1.0, "upbeat": 1.2,
}

_NEGATIVE: Dict[str, float] = {
    "loss": 1.2, "losses": 1.2, "fall": 1.0, "falls": 1.0, "falling": 1.0,
    "fell": 1.0, "drop": 1.2, "drops": 1.2, "dropped": 1.2, "plunge": 1.6,
    "plunged": 1.6, "slump": 1.4, "slumped": 1.4, "crash": 1.8, "crashed": 1.8,
    "tumble": 1.4, "tumbled": 1.4, "down": 0.6, "lower": 1.0, "weak": 1.2,
    "weaker": 1.2, "weakness": 1.2, "miss": 1.2, "misses": 1.2, "missed": 1.2,
    "underperform": 1.4, "downgrade": 1.4, "downgraded": 1.4, "bearish": 1.8,
    "pessimism": 1.2, "pessimistic": 1.2, "negative": 1.0, "recession": 1.8,
    "slowdown": 1.4, "contraction": 1.4, "contract": 1.0, "decline": 1.2,
    "declined": 1.2, "fear": 1.2, "fears": 1.2, "concern": 1.0, "concerns": 1.0,
    "worry": 1.0, "worries": 1.0, "risk": 0.8, "risks": 0.8, "crisis": 1.6,
    "default": 1.6, "downturn": 1.4, "cut": 0.6, "cuts": 0.6, "layoff": 1.2,
    "layoffs": 1.2, "sell": 1.0, "selloff": 1.4, "sell-off": 1.4,
    "outflow": 1.0, "outflows": 1.0, "warning": 1.2, "warn": 1.2,
    "dovish": 0.8, "uncertainty": 1.0, "volatile": 0.8, "volatility": 0.8,
    "sanction": 1.0, "sanctions": 1.0, "conflict": 1.2, "war": 1.4,
    "tariff": 1.0, "tariffs": 1.0, "inflation": 0.6, "deficit": 0.8,
}

# Words that flip the polarity of the next sentiment word within a short window.
_NEGATORS = {"not", "no", "never", "without", "less", "lower", "fails", "fail",
             "failed", "unlikely", "avoid", "avoids", "avoided"}

# Amplifiers/dampeners scale the next sentiment word.
_AMPLIFIERS = {"very": 1.5, "sharply": 1.5, "strongly": 1.4, "significantly": 1.4,
               "slightly": 0.6, "somewhat": 0.7, "marginally": 0.6}

_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z\-']+")


class LexiconSentiment(SentimentBackend):
    """Offline lexicon sentiment scorer with negation and amplifier handling."""

    name = "lexicon"

    def __init__(self, cfg: Any):
        super().__init__(cfg)
        self.log = get_logger("news.sentiment.lexicon", cfg)

    def score(self, text: str) -> SentimentResult:
        if not text:
            return SentimentResult(0.0, 0, 0, 0)
        tokens = [t.lower() for t in _TOKEN_RE.findall(text)]
        pos_count = 0
        neg_count = 0
        total = 0.0
        for i, tok in enumerate(tokens):
            weight = 0.0
            polarity = 0
            if tok in _POSITIVE:
                weight = _POSITIVE[tok]
                polarity = 1
            elif tok in _NEGATIVE:
                weight = _NEGATIVE[tok]
                polarity = -1
            if polarity == 0:
                continue

            # Look back up to 2 tokens for negators / amplifiers.
            flip = 1.0
            scale = 1.0
            for back in (1, 2):
                j = i - back
                if j < 0:
                    break
                prev = tokens[j]
                if prev in _NEGATORS:
                    flip *= -1.0
                if prev in _AMPLIFIERS:
                    scale *= _AMPLIFIERS[prev]

            contribution = polarity * weight * scale * flip
            total += contribution
            if contribution > 0:
                pos_count += 1
            elif contribution < 0:
                neg_count += 1

        hits = pos_count + neg_count
        if hits == 0:
            return SentimentResult(0.0, 0, 0, 0)

        # Normalize by number of hits and squash into [-1, 1] with tanh-like map.
        raw = total / float(hits)
        # Soft clamp: divide by (1 + |raw|) keeps sign, bounds magnitude < 1,
        # then scale slightly so strong headlines can approach +/-1.
        norm = raw / (1.0 + abs(raw))
        norm = max(-1.0, min(1.0, norm * 1.3))
        return SentimentResult(norm, pos_count, neg_count, hits)


class VaderSentiment(SentimentBackend):
    """Optional VADER-based sentiment scorer (used only if vaderSentiment is installed)."""

    name = "vader"

    def __init__(self, cfg: Any):
        super().__init__(cfg)
        self.log = get_logger("news.sentiment.vader", cfg)
        self._analyzer = None
        try:
            from vaderSentiment.vaderSentiment import (  # type: ignore
                SentimentIntensityAnalyzer,
            )
            self._analyzer = SentimentIntensityAnalyzer()
        except Exception:
            self.available = False
            self.log.warning(
                "vaderSentiment not available; VADER backend disabled."
            )

    def score(self, text: str) -> SentimentResult:
        if not self.available or self._analyzer is None or not text:
            return SentimentResult(0.0, 0, 0, 0)
        try:
            scores = self._analyzer.polarity_scores(text)
            compound = float(scores.get("compound", 0.0))
            pos = 1 if compound > 0 else 0
            neg = 1 if compound < 0 else 0
            hits = 1 if compound != 0 else 0
            return SentimentResult(compound, pos, neg, hits)
        except Exception as exc:
            self.log.error("VADER score error: %s", exc)
            return SentimentResult(0.0, 0, 0, 0)


def build_sentiment(cfg: Any) -> SentimentBackend:
    """
    Build the configured sentiment backend. Falls back to the offline lexicon
    if the requested optional backend is unavailable, so the bot always has a
    working scorer.
    """
    log = get_logger("news.sentiment", cfg)
    backend = cfg.get_path("news.sentiment_backend", "lexicon")
    backend = (backend or "lexicon").lower()
    if backend == "vader":
        vader = VaderSentiment(cfg)
        if vader.available:
            log.info("Using VADER sentiment backend.")
            return vader
        log.info("Falling back to lexicon sentiment backend.")
    return LexiconSentiment(cfg)
