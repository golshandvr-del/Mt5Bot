"""
Meta-labeling filter (UPGRADE_PLAN Phase U6.1).

The single highest-leverage ML use in this project. Instead of predicting the
DIRECTION of the market (which every indicator already votes on), the
meta-labeler predicts a much easier, more useful question:

    "Given that the validated top strategy is ABOUT TO FIRE a signal here,
     will that particular trade WIN?"

It learns this from regime/context features (ATR% volatility, ADX trend
strength, FX session, day-of-week, and how far past its threshold the strategy
signal is) paired with the realized win/loss of each historical firing of that
strategy. At live time it becomes a VETO-ONLY quality gate that composes
cleanly with parity mode: it can BLOCK a low-probability entry the validated
strategy wanted, but it can never create, flip, or resize a trade.

Design constraints (same as the rest of the repo):
  - Pure Python, no third-party deps (a tiny logistic regression), so it runs
    on Windows 7 / Python 3.8 / CPU-only.
  - Fully optional and config-gated (decision.meta_label.enabled, default off).
  - Degrades gracefully: an untrained / unavailable model NEVER vetoes.
  - Deterministic given the project seed.

All text is standard ASCII English only.
"""

from __future__ import annotations

import json
import math
import os
from typing import Any, Dict, List, Optional, Tuple

from core.indicators.volatility import ATR
from core.indicators.registry import get_indicator_class
from core.strategy.strategy import Strategy, StrategySpec
from core.utils.logger import get_logger


def _sigmoid(z: float) -> float:
    if z < -60.0:
        return 0.0
    if z > 60.0:
        return 1.0
    return 1.0 / (1.0 + math.exp(-z))


class _LogReg(object):
    """Minimal L2-regularized binary logistic regression (pure Python).

    Trained by plain gradient descent with feature standardization so the fixed
    learning rate behaves across differently-scaled inputs. Serializes to a
    plain dict for JSON persistence.
    """

    def __init__(self, n_features: int, lr: float = 0.1, epochs: int = 300,
                 l2: float = 1e-3):
        self.n = int(n_features)
        self.lr = float(lr)
        self.epochs = int(epochs)
        self.l2 = float(l2)
        self.w = [0.0] * self.n
        self.b = 0.0
        # Standardization stats (filled at fit()).
        self.mean = [0.0] * self.n
        self.std = [1.0] * self.n

    def _standardize(self, x: List[float]) -> List[float]:
        return [(x[j] - self.mean[j]) / (self.std[j] or 1.0)
                for j in range(self.n)]

    def fit(self, X: List[List[float]], y: List[int]) -> None:
        m = len(X)
        if m == 0:
            return
        # Compute standardization stats.
        for j in range(self.n):
            col = [row[j] for row in X]
            mu = sum(col) / m
            var = sum((v - mu) ** 2 for v in col) / m
            self.mean[j] = mu
            self.std[j] = math.sqrt(var) if var > 1e-12 else 1.0
        Xs = [self._standardize(row) for row in X]
        for _ in range(self.epochs):
            gw = [0.0] * self.n
            gb = 0.0
            for xi, yi in zip(Xs, y):
                p = _sigmoid(self._raw(xi))
                err = p - float(yi)
                for j in range(self.n):
                    gw[j] += err * xi[j]
                gb += err
            for j in range(self.n):
                gw[j] = gw[j] / m + self.l2 * self.w[j]
                self.w[j] -= self.lr * gw[j]
            self.b -= self.lr * (gb / m)

    def _raw(self, xs: List[float]) -> float:
        s = self.b
        for j in range(self.n):
            s += self.w[j] * xs[j]
        return s

    def predict_proba(self, x: List[float]) -> float:
        return _sigmoid(self._raw(self._standardize(x)))

    def to_dict(self) -> Dict[str, Any]:
        return {"n": self.n, "w": self.w, "b": self.b,
                "mean": self.mean, "std": self.std,
                "lr": self.lr, "epochs": self.epochs, "l2": self.l2}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "_LogReg":
        obj = cls(int(d.get("n", 0)), lr=float(d.get("lr", 0.1)),
                  epochs=int(d.get("epochs", 300)), l2=float(d.get("l2", 1e-3)))
        obj.w = list(d.get("w", [0.0] * obj.n))
        obj.b = float(d.get("b", 0.0))
        obj.mean = list(d.get("mean", [0.0] * obj.n))
        obj.std = list(d.get("std", [1.0] * obj.n))
        return obj


# Feature layout (order matters and is persisted with the model):
#   0 signal_mag   : |strategy signal| in [0,1] (how strong the firing was)
#   1 atr_pct      : ATR / close (realized volatility)
#   2 adx          : ADX/100 (trend strength, scaled to ~[0,1])
#   3 hour_sin     : sin(2*pi*hour/24)
#   4 hour_cos     : cos(2*pi*hour/24)
#   5 dow_sin      : sin(2*pi*dow/7)
#   6 dow_cos      : cos(2*pi*dow/7)
_FEATURE_NAMES = ["signal_mag", "atr_pct", "adx",
                  "hour_sin", "hour_cos", "dow_sin", "dow_cos"]
_N_FEATURES = len(_FEATURE_NAMES)


class MetaLabeler(object):
    """Trainable, persistable meta-labeling quality gate for ONE strategy.

    Keyed by a strategy fingerprint so different top strategies can each have
    their own gate. All model state lives in one JSON file (a dict keyed by
    fingerprint), so a single MetaLabeler instance can hold gates for several
    fingerprints.
    """

    def __init__(self, cfg: Any):
        self.cfg = cfg
        self.log = get_logger("strategy.meta_label", cfg)
        dec = cfg.get_path("decision.meta_label", {})
        self._dec = dec if hasattr(dec, "get") else {}
        self.enabled = bool(self._get("enabled", False))
        # P(win) below which an otherwise-valid entry is vetoed.
        self.min_win_prob = float(self._get("min_win_prob", 0.5))
        # Below this many training samples the gate stays inactive (never veto).
        self.min_train_samples = int(self._get("min_train_samples", 50))
        self.lr = float(self._get("learning_rate", 0.1))
        self.epochs = int(self._get("epochs", 300))
        root = cfg.get("project_root", ".")
        mf = self._get("model_file", "models/meta_label.json")
        self.model_path = mf if os.path.isabs(mf) else os.path.join(root, mf)
        self._models: Dict[str, _LogReg] = {}
        self._atr = ATR(params={"period": 14})

    def _get(self, key: str, default: Any) -> Any:
        if hasattr(self._dec, "get"):
            v = self._dec.get(key, default)
            return default if v is None else v
        return default

    # ------------------------------------------------------------------ #
    # Feature construction.
    # ------------------------------------------------------------------ #
    def _series(self, ohlcv: Any) -> Dict[str, Any]:
        """Precompute ATR and ADX series once for a whole history."""
        out: Dict[str, Any] = {}
        try:
            out["atr"] = self._atr.compute(ohlcv).get("atr")
        except Exception:
            out["atr"] = None
        try:
            out["adx"] = get_indicator_class("adx")(
                params={"period": 14}).compute(ohlcv).get("adx")
        except Exception:
            out["adx"] = None
        return out

    @staticmethod
    def _time_parts(ts: Any) -> Tuple[int, int]:
        """Return (hour_utc, day_of_week) from a bar timestamp (epoch seconds).

        Falls back to (0, 0) on any parse error so features never crash.
        """
        try:
            import datetime as _dt
            d = _dt.datetime.utcfromtimestamp(int(ts))
            return d.hour, d.weekday()
        except Exception:
            return 0, 0

    def _feature_row(self, ohlcv: Any, i: int, sig: float,
                     series: Dict[str, Any]) -> List[float]:
        closes = getattr(ohlcv, "close", []) or []
        times = getattr(ohlcv, "time", []) or []
        c = closes[i] if i < len(closes) else 0.0
        atr_ser = series.get("atr")
        adx_ser = series.get("adx")
        atr = atr_ser[i] if atr_ser and i < len(atr_ser) and atr_ser[i] is not None else 0.0
        adx = adx_ser[i] if adx_ser and i < len(adx_ser) and adx_ser[i] is not None else 0.0
        atr_pct = (float(atr) / float(c)) if c else 0.0
        ts = times[i] if i < len(times) else 0
        hour, dow = self._time_parts(ts)
        two_pi = 2.0 * math.pi
        return [
            abs(float(sig)),
            atr_pct,
            float(adx) / 100.0,
            math.sin(two_pi * hour / 24.0),
            math.cos(two_pi * hour / 24.0),
            math.sin(two_pi * dow / 7.0),
            math.cos(two_pi * dow / 7.0),
        ]

    # ------------------------------------------------------------------ #
    # Training.
    # ------------------------------------------------------------------ #
    def build_dataset(self, spec: StrategySpec, ohlcv: Any,
                      horizon: int = 5, warmup: int = 60
                      ) -> Tuple[List[List[float]], List[int]]:
        """Build (X, y) where each row is a bar the strategy FIRED and y is 1 if
        the forward `horizon`-bar move went the strategy's way, else 0.

        This is a cheap, lookahead-safe proxy for "did the trade win": for a long
        firing, win = close[i+h] > close[i]; for a short, win = close[i+h] <
        close[i]. It intentionally ignores exact SL/TP paths (that is the
        backtester's job) - the meta-labeler only needs a robust WIN/LOSS label
        to learn which CONTEXTS favour this strategy.
        """
        strat = Strategy(spec)
        try:
            sigs = strat.signal_series(ohlcv)
        except Exception:
            return [], []
        closes = getattr(ohlcv, "close", []) or []
        n = len(closes)
        series = self._series(ohlcv)
        lt = float(spec.long_threshold)
        st = float(spec.short_threshold)
        X: List[List[float]] = []
        y: List[int] = []
        for i in range(warmup, n - horizon):
            s = sigs[i] if i < len(sigs) else 0.0
            action = 0
            if s >= lt:
                action = 1
            elif s <= -st:
                action = -1
            if action == 0:
                continue
            fwd = closes[i + horizon] - closes[i]
            win = 1 if (fwd * action) > 0 else 0
            X.append(self._feature_row(ohlcv, i, s, series))
            y.append(win)
        return X, y

    def train(self, spec: StrategySpec, ohlcv: Any, horizon: int = 5) -> bool:
        """Train (or retrain) the gate for `spec` and persist it. Returns True on
        success (enough samples to be usable), False otherwise."""
        fp = spec.fingerprint()
        X, y = self.build_dataset(spec, ohlcv, horizon=horizon)
        if len(X) < self.min_train_samples:
            self.log.info(
                "Meta-labeler for %s: only %d firings (< %d); gate stays "
                "inactive.", fp, len(X), self.min_train_samples)
            return False
        # Avoid a degenerate single-class fit (all wins or all losses).
        pos = sum(y)
        if pos == 0 or pos == len(y):
            self.log.info(
                "Meta-labeler for %s: single-class labels (%d/%d wins); "
                "gate stays inactive.", fp, pos, len(y))
            return False
        model = _LogReg(_N_FEATURES, lr=self.lr, epochs=self.epochs)
        model.fit(X, y)
        self._models[fp] = model
        self.save()
        self.log.info(
            "Meta-labeler trained for %s on %d firings (%d wins, %.1f%% base "
            "rate).", fp, len(X), pos, 100.0 * pos / len(y))
        return True

    # ------------------------------------------------------------------ #
    # Inference / veto.
    # ------------------------------------------------------------------ #
    def win_probability(self, spec: StrategySpec, ohlcv: Any,
                        sig: Optional[float] = None) -> Optional[float]:
        """Return P(win) for the strategy firing on the LAST bar, or None if no
        trained gate exists for this strategy."""
        fp = spec.fingerprint()
        model = self._models.get(fp)
        if model is None:
            return None
        n = len(getattr(ohlcv, "close", []) or [])
        if n == 0:
            return None
        i = n - 1
        if sig is None:
            try:
                sig = float(Strategy(spec).blended_signal(ohlcv))
            except Exception:
                return None
        series = self._series(ohlcv)
        row = self._feature_row(ohlcv, i, sig, series)
        try:
            return float(model.predict_proba(row))
        except Exception:
            return None

    def should_veto(self, spec: StrategySpec, ohlcv: Any,
                    sig: Optional[float] = None) -> Tuple[bool, Optional[float]]:
        """Return (veto, win_prob). veto is True only when the gate is enabled,
        a trained model exists, AND P(win) < min_win_prob. When the gate is
        disabled or untrained, veto is always False (never blocks)."""
        if not self.enabled:
            return False, None
        p = self.win_probability(spec, ohlcv, sig=sig)
        if p is None:
            return False, None
        return (p < self.min_win_prob), p

    # ------------------------------------------------------------------ #
    # Persistence (one JSON file, dict keyed by fingerprint).
    # ------------------------------------------------------------------ #
    def save(self) -> bool:
        try:
            os.makedirs(os.path.dirname(self.model_path), exist_ok=True)
            payload = {"models": {fp: m.to_dict()
                                  for fp, m in self._models.items()},
                       "feature_names": _FEATURE_NAMES}
            tmp = self.model_path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(payload, fh)
            os.replace(tmp, self.model_path)
            return True
        except Exception as exc:
            self.log.error("meta-label save failed: %s", exc)
            return False

    def load(self) -> bool:
        try:
            if not os.path.exists(self.model_path):
                return False
            with open(self.model_path, "r") as fh:
                payload = json.load(fh)
            models = payload.get("models", {}) if isinstance(payload, dict) else {}
            self._models = {fp: _LogReg.from_dict(d)
                            for fp, d in models.items()}
            return True
        except Exception as exc:
            self.log.error("meta-label load failed: %s", exc)
            return False
