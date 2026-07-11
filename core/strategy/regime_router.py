"""
Regime router (UPGRADE_PLAN Phase U6.2).

Instead of AVERAGING the top-K registry strategies (which blends together edges
that disagree - a trend follower and a mean-reverter cancel out exactly when the
market is choppy), the regime router picks, for the CURRENT market regime, the
single validated strategy that historically did best IN THAT REGIME, and trades
it exactly as parity mode would. A tiny, pure-Python detector labels each bar's
regime the SAME way the walk-forward validation labels its segments (U4.5), so
"the regime we route on" is the same "regime we validated per-regime scores in".

Two public pieces:

  * ``RegimeDetector`` - labels a trailing OHLCV window as
    ``"<low|mid|high>_<trend|range>"`` using an ATR%-like realized-volatility
    proxy (bucketed by the window's OWN 33rd/66th percentiles) plus median ADX
    (>= threshold -> "trend", else "range"). Identical maths to
    ``WalkForward._label_segments`` so live == validated.

  * ``RegimeRouter`` - holds a per-regime CHAMPION map (regime label ->
    strategy fingerprint) built in ``train`` mode by scoring each candidate
    strategy on the bars of each regime, persisted to one JSON file. At live
    time ``champion_for(regime)`` returns the fingerprint to trade; the engine
    then trades that validated strategy through the normal parity path.

For research/validation the router's COMPOSITE (route each bar to its regime's
champion signal) is exposed as a ``Strategy``-compatible ``RegimeRouterStrategy``
so the existing ``Backtester`` / ``scripts/validate_ensemble.py`` can walk-forward
score it end-to-end - satisfying the U2.5 rule that no unvalidated composite may
go live.

Design constraints (same as the rest of the repo): pure Python / stdlib only,
Windows 7 + Python 3.8 + CPU-only, fully optional and config-gated
(``decision.regime_router.enabled``, default OFF), degrades gracefully (an empty
/ untrained router routes to nobody and the engine falls back to plain parity
top-1), deterministic. ASCII English only.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from core.strategy.strategy import Strategy, StrategySpec
from core.utils.logger import get_logger


# --------------------------------------------------------------------------- #
# Regime labelling - shared maths with WalkForward (U4.5).
# --------------------------------------------------------------------------- #
def _window_volatility(highs: List[float], lows: List[float],
                       closes: List[float]) -> float:
    """Realized-volatility proxy: mean of (high-low)/close over the window.

    ATR%-like, needs no indicator warmup, robust on any instrument. Mirrors
    ``WalkForward._segment_volatility`` so live labels match validation labels.
    """
    n = min(len(highs), len(lows), len(closes))
    if n <= 0:
        return 0.0
    acc = 0.0
    cnt = 0
    for i in range(n):
        c = closes[i]
        if c:
            acc += abs(highs[i] - lows[i]) / abs(c)
            cnt += 1
    return (acc / cnt) if cnt > 0 else 0.0


def _median(vals: List[float]) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    m = len(s)
    mid = m // 2
    if m % 2 == 1:
        return float(s[mid])
    return float((s[mid - 1] + s[mid]) / 2.0)


def _terciles(values: List[float]) -> List[float]:
    """[p33, p66] cutoffs (sorted, nearest-rank). Mirrors WalkForward._terciles."""
    vals = sorted(float(v) for v in values)
    if not vals:
        return [0.0, 0.0]
    n = len(vals)

    def _pct(p: float) -> float:
        idx = int(round(p * (n - 1)))
        idx = max(0, min(n - 1, idx))
        return vals[idx]

    return [_pct(1.0 / 3.0), _pct(2.0 / 3.0)]


def _vol_tercile(vol: float, cutoffs: List[float]) -> str:
    if not cutoffs or len(cutoffs) < 2:
        return "mid"
    if vol <= cutoffs[0]:
        return "low"
    if vol <= cutoffs[1]:
        return "mid"
    return "high"


class RegimeDetector(object):
    """Label a trailing OHLCV window with its market regime.

    The detector needs volatility CUTOFFS to bucket low/mid/high. In validation
    those cutoffs come from the run's own segments; at live time we compute them
    once from the training history (``fit_cutoffs``) and reuse them, so a single
    live bar is bucketed against the SAME distribution the champions were chosen
    on. Absent fitted cutoffs, every window is "mid" (a safe, non-discriminating
    default).
    """

    def __init__(self, cfg: Any):
        self.cfg = cfg
        rg = cfg.get_path("memory.search.regime", {}) if hasattr(cfg, "get_path") else {}
        rg = rg if hasattr(rg, "get") else {}
        try:
            self.adx_trend = float(rg.get("adx_trend_threshold", 25.0))
        except Exception:
            self.adx_trend = 25.0
        rr = cfg.get_path("decision.regime_router", {}) if hasattr(cfg, "get_path") else {}
        rr = rr if hasattr(rr, "get") else {}
        try:
            self.window = int(rr.get("detect_window", 96))
        except Exception:
            self.window = 96
        if self.window < 8:
            self.window = 8
        try:
            self.adx_period = int(rr.get("adx_period", 14))
        except Exception:
            self.adx_period = 14
        self._cutoffs: List[float] = [0.0, 0.0]
        self._fitted = False

    # ------------------------------------------------------------------ #
    def fit_cutoffs(self, ohlcv: Any, seg_len: Optional[int] = None) -> List[float]:
        """Compute low/mid/high volatility cutoffs from history by slicing it
        into ``seg_len``-bar chunks and taking their per-chunk volatilities'
        terciles. ``seg_len`` defaults to the detection window so the cutoffs
        match the granularity we label live bars at."""
        highs = list(getattr(ohlcv, "high", []) or [])
        lows = list(getattr(ohlcv, "low", []) or [])
        closes = list(getattr(ohlcv, "close", []) or [])
        n = len(closes)
        step = int(seg_len or self.window)
        if step < 8:
            step = 8
        vols: List[float] = []
        i = 0
        while i + step <= n:
            vols.append(_window_volatility(
                highs[i:i + step], lows[i:i + step], closes[i:i + step]))
            i += step
        if len(vols) >= 3:
            self._cutoffs = _terciles(vols)
            self._fitted = True
        else:
            self._cutoffs = [0.0, 0.0]
            self._fitted = False
        return list(self._cutoffs)

    def set_cutoffs(self, cutoffs: List[float]) -> None:
        if cutoffs and len(cutoffs) >= 2:
            self._cutoffs = [float(cutoffs[0]), float(cutoffs[1])]
            self._fitted = True

    def cutoffs(self) -> List[float]:
        return list(self._cutoffs)

    # ------------------------------------------------------------------ #
    def _window_adx(self, highs, lows, closes) -> float:
        try:
            from core.indicators.registry import get_indicator_class
            from core.data.data_feed import OHLCV
            tmp = OHLCV(symbol="", timeframe="")
            for i in range(len(closes)):
                # time/open/volume are unused by ADX; fill with sane values.
                tmp.append_row(i, closes[i], highs[i], lows[i], closes[i], 1)
            adx = get_indicator_class("adx")(params={"period": self.adx_period})
            res = adx.compute(tmp)
            series = res.get("adx") if res else None
        except Exception:
            series = None
        if not series:
            return 0.0
        vals = [v for v in series if v is not None]
        return _median(vals)

    def label(self, ohlcv: Any) -> str:
        """Return the regime label of the LAST ``window`` bars of ``ohlcv``."""
        highs = list(getattr(ohlcv, "high", []) or [])
        lows = list(getattr(ohlcv, "low", []) or [])
        closes = list(getattr(ohlcv, "close", []) or [])
        n = len(closes)
        if n <= 0:
            return "mid_range"
        w = min(self.window, n)
        hs, ls, cs = highs[n - w:], lows[n - w:], closes[n - w:]
        vol = _window_volatility(hs, ls, cs)
        vol_label = _vol_tercile(vol, self._cutoffs)
        adx = self._window_adx(hs, ls, cs)
        trend_label = "trend" if adx >= self.adx_trend else "range"
        return "%s_%s" % (vol_label, trend_label)

    def label_series(self, ohlcv: Any) -> List[str]:
        """Per-bar regime label aligned to ``ohlcv``.

        For each bar i the label uses the trailing ``window`` bars ending at i
        (or all bars up to i when fewer are available). Bars before a minimum
        history simply reuse the first computable label so the series is always
        the full length. This is the SAME trailing-window labelling used live,
        so a validation backtest of the router routes bars identically to
        production.
        """
        highs = list(getattr(ohlcv, "high", []) or [])
        lows = list(getattr(ohlcv, "low", []) or [])
        closes = list(getattr(ohlcv, "close", []) or [])
        n = len(closes)
        out: List[str] = ["mid_range"] * n
        if n == 0:
            return out
        w = self.window
        for i in range(n):
            lo = max(0, i - w + 1)
            hs, ls, cs = highs[lo:i + 1], lows[lo:i + 1], closes[lo:i + 1]
            vol = _window_volatility(hs, ls, cs)
            vol_label = _vol_tercile(vol, self._cutoffs)
            adx = self._window_adx(hs, ls, cs)
            trend_label = "trend" if adx >= self.adx_trend else "range"
            out[i] = "%s_%s" % (vol_label, trend_label)
        return out


# --------------------------------------------------------------------------- #
# Regime router - per-regime champion map + validatable composite strategy.
# --------------------------------------------------------------------------- #
class RegimeRouter(object):
    """Per-regime champion map (regime label -> strategy fingerprint) (U6.2).

    Built in ``train`` mode by scoring each candidate strategy on the bars of
    each regime and keeping, per regime, the single best performer. Persisted to
    one JSON file so live routing needs no re-training. The router is fully
    optional (``decision.regime_router.enabled``, default OFF) and degrades to
    "route to nobody" (engine falls back to plain parity top-1) when untrained.
    """

    def __init__(self, cfg: Any, memory: Optional[object] = None):
        self.cfg = cfg
        self.memory = memory
        self.log = get_logger("strategy.regime_router", cfg)
        self.detector = RegimeDetector(cfg)
        rr = cfg.get_path("decision.regime_router", {}) if hasattr(cfg, "get_path") else {}
        rr = rr if hasattr(rr, "get") else {}
        self.enabled = bool(rr.get("enabled", False)) if hasattr(rr, "get") else False
        try:
            self.min_bars_per_regime = int(rr.get("min_bars_per_regime", 200))
        except Exception:
            self.min_bars_per_regime = 200
        self.rank_metric = (
            cfg.get_path("memory.search.rank_metric", "expectancy")
            if hasattr(cfg, "get_path") else "expectancy")
        path = rr.get("champions_file", "data_store/regime_champions.json") \
            if hasattr(rr, "get") else "data_store/regime_champions.json"
        if hasattr(cfg, "get_path"):
            from config.loader import resolve_path
            try:
                path = resolve_path(cfg, path)
            except Exception:
                pass
        self.champions_file = path
        # regime label -> {"fingerprint": str, "score": float, "spec": dict}
        self._champions: Dict[str, Dict[str, Any]] = {}
        self._cutoffs: List[float] = [0.0, 0.0]

    # ------------------------------------------------------------------ #
    def champion_for(self, regime: str) -> Optional[Dict[str, Any]]:
        """Return the champion entry for a regime, or None if none was learned."""
        return self._champions.get(regime)

    def champions(self) -> Dict[str, Dict[str, Any]]:
        return dict(self._champions)

    def is_ready(self) -> bool:
        """True when the router has at least one learned champion."""
        return self.enabled and bool(self._champions)

    # ------------------------------------------------------------------ #
    def train(self, specs: List[StrategySpec], ohlcv: Any,
              backtester: Any, point: Optional[float] = None) -> Dict[str, Any]:
        """Score every candidate spec per regime and keep the per-regime best.

        For each regime label present in the history, we backtest each candidate
        on ONLY that regime's bars (masking the rest to flat) and keep the spec
        with the highest rank score, provided the regime has at least
        ``min_bars_per_regime`` bars (a rarely-seen regime is not trustworthy).
        Returns the champion map. Pure Python; no network / MT5.
        """
        from core.strategy.metrics import rank_value

        self.detector.fit_cutoffs(ohlcv)
        self._cutoffs = self.detector.cutoffs()
        labels = self.detector.label_series(ohlcv)
        regimes = sorted(set(labels))
        # Count bars per regime; skip regimes without enough evidence.
        counts: Dict[str, int] = {}
        for lbl in labels:
            counts[lbl] = counts.get(lbl, 0) + 1

        champions: Dict[str, Dict[str, Any]] = {}
        for regime in regimes:
            if counts.get(regime, 0) < self.min_bars_per_regime:
                self.log.info(
                    "Regime %s has only %d bars (< %d); skipping.",
                    regime, counts.get(regime, 0), self.min_bars_per_regime)
                continue
            best = None
            for spec in specs:
                try:
                    score = self._score_spec_on_regime(
                        spec, ohlcv, labels, regime, backtester, point,
                        rank_value)
                except Exception as exc:
                    self.log.error("Router scoring failed for %s in %s: %s",
                                   spec.fingerprint(), regime, exc)
                    continue
                if best is None or score > best[1]:
                    best = (spec, score)
            if best is not None:
                spec, score = best
                champions[regime] = {
                    "fingerprint": spec.fingerprint(),
                    "score": float(score),
                    "spec": spec.to_dict(),
                }
                self.log.info("Regime %s champion: %s (score=%.4f).",
                              regime, spec.fingerprint()[:8], score)
        self._champions = champions
        return dict(champions)

    def _score_spec_on_regime(self, spec: StrategySpec, ohlcv: Any,
                              labels: List[str], regime: str, backtester: Any,
                              point: Optional[float], rank_value) -> float:
        """Rank score of a spec counting ONLY trades entered in ``regime`` bars.

        We run the normal whole-history backtest and then rank the metrics; to
        keep it CPU-light and dependency-free we approximate "regime-only" by
        masking the strategy's decision series to flat outside the regime.
        """
        strat = _MaskedRegimeStrategy(spec, labels, {regime})
        result = backtester.run(strat, ohlcv, warmup=60, point=point)
        return rank_value(result.metrics, self.rank_metric)

    # ------------------------------------------------------------------ #
    def save(self, path: Optional[str] = None) -> str:
        path = path or self.champions_file
        try:
            d = os.path.dirname(path)
            if d and not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
            payload = {
                "cutoffs": list(self._cutoffs),
                "rank_metric": self.rank_metric,
                "champions": self._champions,
            }
            with open(path, "w", encoding="ascii") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
        except Exception as exc:
            self.log.error("Failed to save regime champions to %s: %s", path, exc)
        return path

    def load(self, path: Optional[str] = None) -> bool:
        path = path or self.champions_file
        try:
            if not os.path.exists(path):
                return False
            with open(path, "r", encoding="ascii") as fh:
                payload = json.load(fh)
            self._champions = dict(payload.get("champions", {}))
            self._cutoffs = list(payload.get("cutoffs", [0.0, 0.0]))
            if self._cutoffs and len(self._cutoffs) >= 2:
                self.detector.set_cutoffs(self._cutoffs)
            return bool(self._champions)
        except Exception as exc:
            self.log.error("Failed to load regime champions from %s: %s",
                           path, exc)
            return False


class _MaskedRegimeStrategy(object):
    """A Strategy wrapper whose decisions are forced flat outside a regime set.

    Used only by ``RegimeRouter.train`` to score a candidate on one regime's
    bars. Presents the Strategy surface the Backtester needs (``spec``,
    ``decision_series``, ``atr_series``, ``signal_series``).
    """

    def __init__(self, spec: StrategySpec, labels: List[str],
                 keep_regimes: Any):
        self.spec = spec
        self._inner = Strategy(spec)
        self._labels = labels
        self._keep = set(keep_regimes)

    def decision_series(self, ohlcv: Any) -> List[int]:
        base = self._inner.decision_series(ohlcv)
        out: List[int] = [0] * len(base)
        for i, d in enumerate(base):
            lbl = self._labels[i] if i < len(self._labels) else None
            out[i] = d if lbl in self._keep else 0
        return out

    def signal_series(self, ohlcv: Any) -> List[float]:
        base = self._inner.signal_series(ohlcv)
        out: List[float] = [0.0] * len(base)
        for i, s in enumerate(base):
            lbl = self._labels[i] if i < len(self._labels) else None
            out[i] = s if lbl in self._keep else 0.0
        return out

    def atr_series(self, ohlcv: Any):
        return self._inner.atr_series(ohlcv)


class RegimeRouterStrategy(object):
    """Strategy-compatible COMPOSITE that routes each bar to its regime champion.

    For every bar the detector labels the trailing window; the bar's decision /
    signal is taken from the champion strategy of THAT regime (flat if no
    champion). This is the object handed to the Backtester / ensemble validator
    so the router's composite is walk-forward scored end-to-end, satisfying the
    U2.5 rule that no unvalidated composite may go live.

    SL/TP come from a representative spec (the champion of the most-populated
    regime, or an explicit ``base_spec``) because the Backtester reads a single
    ``spec.sl_atr_mult`` / ``spec.tp_atr_mult``. The routing of ENTRIES is what
    the router controls; exits stay a fixed, validated ATR rule.
    """

    def __init__(self, router: "RegimeRouter", base_spec: Optional[StrategySpec] = None):
        self._router = router
        self._detector = router.detector
        # Build one executable Strategy per champion fingerprint.
        self._by_regime: Dict[str, Strategy] = {}
        rep_spec = base_spec
        best_count = -1
        for regime, entry in router.champions().items():
            spec = StrategySpec.from_dict(entry["spec"])
            self._by_regime[regime] = Strategy(spec)
            # First champion also seeds the representative spec if none given.
            if rep_spec is None:
                rep_spec = spec
        if rep_spec is None:
            # Fully empty router: a neutral spec that never trades.
            rep_spec = StrategySpec(indicators={}, weights={})
        self.spec = rep_spec

    def decision_series(self, ohlcv: Any) -> List[int]:
        labels = self._detector.label_series(ohlcv)
        n = len(labels)
        # Precompute each champion's decision series once (O(regimes * n)).
        champ_dec: Dict[str, List[int]] = {
            r: s.decision_series(ohlcv) for r, s in self._by_regime.items()
        }
        out: List[int] = [0] * n
        for i in range(n):
            lbl = labels[i]
            dec = champ_dec.get(lbl)
            out[i] = dec[i] if (dec is not None and i < len(dec)) else 0
        return out

    def signal_series(self, ohlcv: Any) -> List[float]:
        labels = self._detector.label_series(ohlcv)
        n = len(labels)
        champ_sig: Dict[str, List[float]] = {
            r: s.signal_series(ohlcv) for r, s in self._by_regime.items()
        }
        out: List[float] = [0.0] * n
        for i in range(n):
            lbl = labels[i]
            sig = champ_sig.get(lbl)
            out[i] = sig[i] if (sig is not None and i < len(sig)) else 0.0
        return out

    def atr_series(self, ohlcv: Any):
        return Strategy(self.spec).atr_series(ohlcv)
