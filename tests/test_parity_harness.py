"""
Python-vs-EA signal parity harness tests (UPGRADE_PLAN.md U2.6).

This guarantees the EA trades the SAME blended signal the Python search
validated (diagnosis D1). It works in two layers:

LAYER 1 (always runs, no MT5 needed): an in-Python reference implementation of
the EA's BlendedSignal() math (a line-by-line port of Mt5SmartBotEA.mq5, mirrored
in experts/ParityDump.mq5) is diffed against the REAL Python Strategy on the
shared fixture bars. If someone edits an indicator's Python signal but forgets
the EA (or vice-versa), this test fails. This is the drift guard that runs in CI.

LAYER 2 (runs only if tests/fixtures/parity_ea.csv exists): after the user runs
experts/ParityDump.mq5 inside MetaTrader 5 on the shared CSV, the resulting
parity_ea.csv is diffed bar-by-bar against the Python reference with a strict
1e-6 tolerance. This catches real MQL5 compile/runtime differences.

A short warm-up region is skipped because indicators need history before their
values stabilize; parity is asserted on the settled region only, which is what
actually trades.

Standard library only; no MT5, no network. ASCII English only.
"""

from __future__ import annotations

import csv
import os
import unittest

from tests.helpers import ensure_project_on_path, make_synthetic_ohlcv

ensure_project_on_path()

from core.strategy.strategy import StrategySpec, Strategy  # noqa: E402
import scripts.parity_fixture as pf  # noqa: E402


WARMUP = 60          # bars to skip so every indicator is fully warmed up
TOL = 1e-6           # max allowed |python - ea|


# --------------------------------------------------------------------------- #
# In-Python port of the EA BlendedSignal() math (ema+rsi+macd+adx subset).
# This is the SAME arithmetic as experts/ParityDump.mq5 and the EA. Keeping it
# here lets the drift guard run without MetaTrader.
# --------------------------------------------------------------------------- #
def _clamp1(v):
    return max(-1.0, min(1.0, v))


def _ema_series(close, period):
    n = len(close)
    out = [None] * n
    if period <= 0 or n == 0:
        return out
    alpha = 2.0 / (period + 1.0)
    ema = None
    for i, v in enumerate(close):
        ema = v if ema is None else alpha * v + (1.0 - alpha) * ema
        if i >= period - 1:
            out[i] = ema
    return out


def _rsi_series(close, period):
    # EXACT match to core/indicators/momentum.py RSI + base._wilder_smooth:
    # gains/losses include index 0 (=0.0), the Wilder seed is the SIMPLE mean of
    # the first `period` gains/losses placed at index period-1, and RSI starts at
    # index period-1. Getting this warm-up right is what makes parity exact.
    n = len(close)
    out = [None] * n
    if period <= 0 or n < period:
        return out
    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        change = close[i] - close[i - 1]
        gains[i] = max(0.0, change)
        losses[i] = max(0.0, -change)

    def wilder(values):
        o = [None] * n
        first = sum(values[:period]) / period
        o[period - 1] = first
        prev = first
        for i in range(period, n):
            prev = (prev * (period - 1) + values[i]) / period
            o[i] = prev
        return o

    avg_gain = wilder(gains)
    avg_loss = wilder(losses)
    for i in range(n):
        if avg_gain[i] is None or avg_loss[i] is None:
            continue
        if avg_loss[i] == 0.0:
            out[i] = 100.0
        else:
            rs = avg_gain[i] / avg_loss[i]
            out[i] = 100.0 - (100.0 / (1.0 + rs))
    return out


def _ea_blended_reference(ohlcv, spec):
    """Reproduce the EA BlendedSignal per bar for the ema+rsi+macd+adx subset,
    reusing the project's own indicator math for macd/adx to avoid re-deriving
    Wilder smoothing (the EA mirrors these exactly)."""
    from core.indicators.trend import MACD, ADX

    close = list(ohlcv.close)
    n = len(close)

    inds = spec.indicators
    w = spec.weights

    ema = _ema_series(close, int(inds.get("ema", {}).get("period", 20)))
    rsi = _rsi_series(close, int(inds.get("rsi", {}).get("period", 14)))

    macd_res = MACD(params=dict(inds.get("macd", {}))).compute(ohlcv)
    macd_main = macd_res.get("macd")
    macd_hist = macd_res.get("hist")

    adx_res = ADX(params=dict(inds.get("adx", {}))).compute(ohlcv)
    adx = adx_res.get("adx")
    pdi = adx_res.get("plus_di")
    mdi = adx_res.get("minus_di")

    out = [0.0] * n
    for i in range(n):
        weighted = 0.0
        wsum = 0.0
        # EMA
        if "ema" in inds and ema[i] is not None and ema[i] > 0.0:
            s = _clamp1(((close[i] - ema[i]) / ema[i]) * 50.0)
            weighted += w.get("ema", 1.0) * s
            wsum += abs(w.get("ema", 1.0))
        # RSI
        if "rsi" in inds and rsi[i] is not None:
            r = rsi[i]
            if r <= 30.0:
                s = min(1.0, (30.0 - r) / 30.0 + 0.5)
            elif r >= 70.0:
                s = -min(1.0, (r - 70.0) / 30.0 + 0.5)
            else:
                s = (r - 50.0) / 50.0 * 0.3
            weighted += w.get("rsi", 1.0) * s
            wsum += abs(w.get("rsi", 1.0))
        # MACD
        if "macd" in inds and macd_main[i] is not None and macd_hist[i] is not None:
            hist = macd_hist[i]
            base = 1.0 if hist > 0 else -1.0
            denom = abs(macd_main[i]) + 1e-9
            strength = min(1.0, abs(hist) / denom)
            s = base * (0.5 + 0.5 * strength)
            weighted += w.get("macd", 1.0) * s
            wsum += abs(w.get("macd", 1.0))
        # ADX
        if "adx" in inds and adx[i] is not None and pdi[i] is not None \
                and mdi[i] is not None:
            direction = 1.0 if pdi[i] > mdi[i] else -1.0
            strength = min(1.0, max(0.0, (adx[i] - 20.0) / 30.0))
            s = direction * strength
            weighted += w.get("adx", 1.0) * s
            wsum += abs(w.get("adx", 1.0))
        out[i] = 0.0 if wsum <= 0.0 else _clamp1(weighted / wsum)
    return out


class TestParityHarness(unittest.TestCase):
    def setUp(self):
        self.ohlcv = make_synthetic_ohlcv(
            n=pf.N_BARS, symbol="PARITY", timeframe="M15", seed=pf.SEED)
        self.spec = StrategySpec.from_dict(dict(pf.PARITY_SPEC))

    # ------------------------------------------------------------------ #
    # LAYER 1: the drift guard (no MT5 required).
    # ------------------------------------------------------------------ #
    def test_python_matches_ea_reference(self):
        strat = Strategy(self.spec)
        py = strat.signal_series(self.ohlcv)
        ea = _ea_blended_reference(self.ohlcv, self.spec)
        self.assertEqual(len(py), len(ea))
        worst = 0.0
        for i in range(WARMUP, len(py)):
            worst = max(worst, abs(py[i] - ea[i]))
        self.assertLess(
            worst, TOL,
            "Python Strategy and the EA reference drifted by %.3e (> %.0e). "
            "An indicator's signal math changed on one side only." % (worst, TOL))

    def test_reference_is_nontrivial(self):
        # Guard against a bug where both sides are all-zeros (vacuous parity).
        ea = _ea_blended_reference(self.ohlcv, self.spec)
        nonzero = sum(1 for i in range(WARMUP, len(ea)) if abs(ea[i]) > 1e-9)
        self.assertGreater(nonzero, 50, "parity fixture produced ~no signal")

    def test_fixture_generator_is_deterministic(self):
        # Regenerating the Python side must yield identical values (byte-stable
        # fixtures are what make the MQL5 diff meaningful).
        a = Strategy(self.spec).signal_series(self.ohlcv)
        ohlcv2 = make_synthetic_ohlcv(
            n=pf.N_BARS, symbol="PARITY", timeframe="M15", seed=pf.SEED)
        b = Strategy(self.spec).signal_series(ohlcv2)
        self.assertEqual(a, b)

    # ------------------------------------------------------------------ #
    # LAYER 2: real MQL5 output diff (skipped unless the user ran the EA).
    # ------------------------------------------------------------------ #
    def test_ea_csv_matches_python_if_present(self):
        root = ensure_project_on_path()
        ea_path = os.path.join(root, pf.FIXTURE_DIR, pf.EA_CSV)
        if not os.path.exists(ea_path):
            self.skipTest(
                "parity_ea.csv not present; run experts/ParityDump.mq5 in MT5 "
                "to enable the full EA-vs-Python diff.")
        py = Strategy(self.spec).signal_series(self.ohlcv)
        ea = {}
        with open(ea_path, "r", encoding="ascii") as fh:
            reader = csv.reader(fh)
            next(reader, None)  # header
            for row in reader:
                if len(row) >= 2:
                    ea[int(row[0])] = float(row[1])
        worst = 0.0
        for i in range(WARMUP, len(py)):
            if i in ea:
                worst = max(worst, abs(py[i] - ea[i]))
        self.assertLess(worst, TOL,
                        "MQL5 EA vs Python drifted by %.3e (> %.0e)"
                        % (worst, TOL))


if __name__ == "__main__":
    unittest.main()
