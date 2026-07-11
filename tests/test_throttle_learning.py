"""
Offline tests for trade-throttle learning (UPGRADE_PLAN.md U6.4).

The analyzer mines the U1.4 decision journal (logs/decisions_*.jsonl) for
blocked entries - lines whose final `action` is 0 (flat) yet carry a `veto_`
reason and a non-zero `score` - and, by looking `horizon` journal lines ahead at
a price proxy, decides whether each veto gate SAVED money (price moved against
the blocked signal) or COST missed profit (price moved with it). Gates with a
high enough save-rate over enough events are recommended for TIGHTENING; a gate
is NEVER auto-loosened and signals are NEVER inverted.

These tests exercise the analysis LOGIC in isolation with tiny hand-built
journals so they are deterministic, fast, and need no MT5 or data files:

  1. `_gate_of` maps veto reason strings to gate names and ignores non-vetoes.
  2. `_blocked_direction` fires only for flat-action lines with a veto + score.
  3. A gate that reliably avoided adverse moves is tallied `saved` and
     recommended TIGHTEN.
  4. A gate that reliably forfeited favourable moves is tallied `cost` and
     flagged REVIEW (never auto-loosened).
  5. Below `min_events` a gate stays KEEP regardless of save-rate.
  6. A malformed / empty journal degrades to an empty analysis, no exception.

All text is standard ASCII English only.
"""

from __future__ import annotations

import unittest

from tests.helpers import ensure_project_on_path  # noqa: F401 (path fix)

from core.utils import throttle_learning as tl


def _line(ts, action, score, reasons, mid, symbol="X", tf="M15"):
    return {
        "ts": ts, "symbol": symbol, "timeframe": tf,
        "action": action, "score": score,
        "reasons": list(reasons), "components": {"mid": mid},
    }


def _trend(symbol, tf, gate, direction, start_price, step, n=6):
    """Build a blocked entry followed by a monotonic price trend.

    direction is the implied signal (+1 long / -1 short). `step` is the per-bar
    price delta; the first line is the blocked entry (action 0 + veto + score of
    `direction` sign), the rest are neutral follow-ups carrying the price proxy.
    """
    score = 0.6 * direction
    lines = [_line(1, 0, score, ["%s=1" % gate], start_price, symbol, tf)]
    price = start_price
    for i in range(1, n):
        price += step
        lines.append(_line(1 + i, 0, 0.0, [], price, symbol, tf))
    return lines


class TestGateOf(unittest.TestCase):
    def test_maps_veto_prefix_to_gate(self):
        self.assertEqual(tl._gate_of("veto_news_blackout=1"), "veto_news_blackout")
        self.assertEqual(tl._gate_of("veto_learner=0.7<=-0.50"), "veto_learner")
        self.assertEqual(tl._gate_of("veto_meta_label=1"), "veto_meta_label")

    def test_ignores_non_veto_reasons(self):
        self.assertIsNone(tl._gate_of("parity_strategy=abc123"))
        self.assertIsNone(tl._gate_of("strategy_signal=0.30"))
        self.assertIsNone(tl._gate_of(""))


class TestBlockedDirection(unittest.TestCase):
    def test_fires_only_for_blocked_entry(self):
        # flat action + veto + positive score -> would-be long
        rec = _line(1, 0, 0.5, ["veto_time_gate=1"], 100.0)
        self.assertEqual(tl._blocked_direction(rec), 1)
        # flat action + veto + negative score -> would-be short
        rec = _line(1, 0, -0.5, ["veto_time_gate=1"], 100.0)
        self.assertEqual(tl._blocked_direction(rec), -1)

    def test_no_veto_is_not_blocked(self):
        rec = _line(1, 0, 0.5, ["strategy_signal=0.5"], 100.0)
        self.assertIsNone(tl._blocked_direction(rec))

    def test_taken_trade_is_not_blocked(self):
        # action != 0 means the trade was actually taken, not blocked
        rec = _line(1, 1, 0.5, ["veto_time_gate=1"], 100.0)
        self.assertIsNone(tl._blocked_direction(rec))

    def test_zero_score_is_not_blocked(self):
        rec = _line(1, 0, 0.0, ["veto_time_gate=1"], 100.0)
        self.assertIsNone(tl._blocked_direction(rec))


class TestSavedGate(unittest.TestCase):
    def test_gate_that_avoids_loss_is_tightened(self):
        # blocked LONG, price then FALLS -> gate saved money
        recs = _trend("X", "M15", "veto_news_blackout", +1,
                      start_price=100.0, step=-1.0, n=6)
        stats = tl.analyze_journal(recs, horizon=5)
        self.assertEqual(stats["veto_news_blackout"]["saved"], 1)
        self.assertEqual(stats["veto_news_blackout"]["cost"], 0)
        rec = tl.recommend(stats, min_events=1, min_save_rate=0.6)
        self.assertEqual(rec["veto_news_blackout"]["action"], "tighten")


class TestCostGate(unittest.TestCase):
    def test_gate_that_forfeits_gain_is_reviewed(self):
        # blocked LONG, price then RISES -> gate cost missed profit
        recs = _trend("X", "M15", "veto_time_gate", +1,
                      start_price=100.0, step=+1.0, n=6)
        stats = tl.analyze_journal(recs, horizon=5)
        self.assertEqual(stats["veto_time_gate"]["cost"], 1)
        self.assertEqual(stats["veto_time_gate"]["saved"], 0)
        rec = tl.recommend(stats, min_events=1, min_save_rate=0.6)
        # poor save-rate with enough evidence -> REVIEW, never auto-loosened
        self.assertEqual(rec["veto_time_gate"]["action"], "review")


class TestMinEvents(unittest.TestCase):
    def test_below_min_events_stays_keep(self):
        recs = _trend("X", "M15", "veto_learner", +1,
                      start_price=100.0, step=-1.0, n=6)
        stats = tl.analyze_journal(recs, horizon=5)
        # only 1 event, but min_events=20 -> must not recommend a change
        rec = tl.recommend(stats, min_events=20, min_save_rate=0.6)
        self.assertEqual(rec["veto_learner"]["action"], "keep")


class TestRobustness(unittest.TestCase):
    def test_empty_journal_is_empty_analysis(self):
        stats = tl.analyze_journal([], horizon=5)
        self.assertEqual(stats, {})
        rec = tl.recommend(stats)
        self.assertEqual(rec, {})
        report = tl.render_report(rec, 20, 0.6)
        self.assertIn("nothing to recommend", report)

    def test_malformed_records_do_not_raise(self):
        junk = [{"nonsense": True}, {"reasons": None}, {}, 12345, "x"]
        # analyze_journal must tolerate rows missing expected keys
        clean = [r for r in junk if isinstance(r, dict)]
        stats = tl.analyze_journal(clean, horizon=5)
        self.assertEqual(stats, {})

    def test_run_analysis_missing_files(self):
        recs, report = tl.run_analysis(["/no/such/file.jsonl"], horizon=5)
        self.assertEqual(recs, {})
        self.assertIn("U6.4", report)


if __name__ == "__main__":
    unittest.main()
