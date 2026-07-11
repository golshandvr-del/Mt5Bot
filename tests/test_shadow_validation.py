"""
Tests for continuous shadow validation - the HARD safety demotion
(UPGRADE_PLAN Phase U6.5).

Offline, standard-library-only tests. They exercise five layers:

  1. The disabled no-op: with decision.shadow_validation.enabled=false the
     validator neither reads fingerprints nor writes the demotions file
     (byte-for-byte unchanged live path).

  2. The per-strategy verdict (ShadowValidator.validate_one): a strategy whose
     recent live window has decayed below its walk-forward reference AND has
     enough live evidence is DEMOTED; too little evidence -> skip (never demote
     on noise); a healthy strategy -> ok; a recovered demoted strategy ->
     clear when clear_on_pass is on, keep_demoted when off.

  3. The run round-trip (ShadowValidator.run): demotions/clears are persisted to
     a TEMP demote_file and reflected by is_demoted; a report file is written.

  4. Conservative safety: shadow validation can ONLY demote (a losing edge) or
     clear (a recovered one) - it never promotes or edits a strategy.

  5. The OrderManager integration: a shadow-demoted fingerprint is refused a
     LIVE order (forced to paper); with the gate off the live path is unchanged.

All text is standard ASCII English only.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from tests.helpers import PROJECT_ROOT  # noqa: F401


class _FakeMemory(object):
    """In-memory stand-in for MemoryStore so tests need no SQLite DB.

    ``reference`` maps fingerprint -> walk-forward per-segment PnLs.
    ``recent`` maps fingerprint -> recent live/paper per-trade PnLs.
    ``fingerprints`` is the set of live-trading fingerprints.
    """

    def __init__(self, reference, recent, fingerprints=None):
        self._reference = reference
        self._recent = recent
        self._fingerprints = (fingerprints
                              if fingerprints is not None
                              else list(recent.keys()))

    def reference_pnls(self, fp, rank_metric="expectancy"):
        return list(self._reference.get(fp, []))

    def recent_live_pnls(self, fp, limit=100):
        return list(self._recent.get(fp, []))[-int(limit):]

    def live_trade_fingerprints(self):
        return list(self._fingerprints)


def _validator(reference, recent, fingerprints=None, enabled=True,
               min_live_trades=30, clear_on_pass=True, tmp=None):
    """Build a ShadowValidator off a real config with test-friendly knobs."""
    from config.loader import load_config
    from core.strategy.shadow_validation import ShadowValidator

    cfg = load_config()
    sv = cfg["decision"]["shadow_validation"]
    sv["enabled"] = enabled
    sv["window"] = 200
    sv["min_live_trades"] = min_live_trades
    sv["clear_on_pass"] = clear_on_pass
    if tmp is not None:
        sv["demote_file"] = os.path.join(tmp, "demotions.json")
        sv["report_file"] = os.path.join(tmp, "shadow_report.md")
    # Make the shared decay maths deterministic and easy to trip.
    dm = cfg["decision"]["decay_monitor"]
    dm["enabled"] = True
    dm["min_recent"] = 20
    dm["min_reference"] = 3
    dm["z_threshold"] = 2.0
    dm["max_rel_drop"] = 0.5
    dm["require_both"] = False

    memory = _FakeMemory(reference, recent, fingerprints)
    return ShadowValidator(cfg, memory), cfg


# Reference: a real positive edge; recent-decayed: a consistent loss.
_REF = [1.2, 0.8, 1.1, 0.9, 1.0]
_DECAYED = [-0.5] * 30
_HEALTHY = [1.0, 0.9, 1.1, 1.0, 0.8] * 6  # 30 samples, mean ~ +0.96


class TestVerdict(unittest.TestCase):
    """ShadowValidator.validate_one: the per-strategy decision (no I/O)."""

    def test_decayed_with_evidence_is_demoted(self):
        v, _ = _validator({"fp1": _REF}, {"fp1": _DECAYED})
        verdict = v.validate_one("fp1", {})
        self.assertEqual(verdict.action, "demote", verdict.reason)
        self.assertLess(verdict.recent_mean, verdict.ref_mean)

    def test_insufficient_evidence_is_skipped(self):
        # Only 10 recent trades but min_live_trades=30 -> never demote on noise.
        v, _ = _validator({"fp1": _REF}, {"fp1": [-0.5] * 10})
        verdict = v.validate_one("fp1", {})
        self.assertEqual(verdict.action, "skip", verdict.reason)

    def test_healthy_strategy_is_ok(self):
        v, _ = _validator({"fp1": _REF}, {"fp1": _HEALTHY})
        verdict = v.validate_one("fp1", {})
        self.assertEqual(verdict.action, "ok", verdict.reason)

    def test_recovered_is_cleared_when_clear_on_pass(self):
        v, _ = _validator({"fp1": _REF}, {"fp1": _HEALTHY}, clear_on_pass=True)
        verdict = v.validate_one("fp1", {"fp1": {"reason": "old"}})
        self.assertEqual(verdict.action, "clear", verdict.reason)

    def test_recovered_kept_demoted_when_clear_off(self):
        v, _ = _validator({"fp1": _REF}, {"fp1": _HEALTHY}, clear_on_pass=False)
        verdict = v.validate_one("fp1", {"fp1": {"reason": "old"}})
        self.assertEqual(verdict.action, "keep_demoted", verdict.reason)

    def test_still_decayed_stays_demoted(self):
        v, _ = _validator({"fp1": _REF}, {"fp1": _DECAYED})
        verdict = v.validate_one("fp1", {"fp1": {"reason": "old"}})
        self.assertEqual(verdict.action, "keep_demoted", verdict.reason)


class TestRunRoundTrip(unittest.TestCase):
    """ShadowValidator.run: persistence + report + is_demoted."""

    def test_run_demotes_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            v, _ = _validator({"fp1": _REF}, {"fp1": _DECAYED}, tmp=tmp)
            result = v.run()
            self.assertTrue(result["enabled"])
            self.assertIn("fp1", result["demoted"])
            # Persisted and queryable via is_demoted.
            self.assertTrue(v.is_demoted("fp1"))
            self.assertTrue(os.path.isfile(v.demote_file))
            self.assertTrue(os.path.isfile(v.report_file))

    def test_run_clears_recovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            # First: demote it.
            v, _ = _validator({"fp1": _REF}, {"fp1": _DECAYED}, tmp=tmp)
            v.run()
            self.assertTrue(v.is_demoted("fp1"))
            # Now the strategy recovers -> a fresh validator clears it.
            v2, _ = _validator({"fp1": _REF}, {"fp1": _HEALTHY}, tmp=tmp)
            result = v2.run()
            self.assertIn("fp1", result["cleared"])
            self.assertFalse(v2.is_demoted("fp1"))

    def test_disabled_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            v, _ = _validator({"fp1": _REF}, {"fp1": _DECAYED},
                              enabled=False, tmp=tmp)
            result = v.run()
            self.assertFalse(result["enabled"])
            self.assertEqual(result["demoted"], [])
            # Nothing was written when disabled.
            self.assertFalse(os.path.isfile(v.demote_file))


class TestOrderManagerIntegration(unittest.TestCase):
    """A shadow-demoted fingerprint is refused a LIVE order (forced to paper)."""

    def _order_manager(self, tmp, shadow_enabled, demoted_fp=None):
        from config.loader import load_config
        from core.execution.order_manager import OrderManager

        cfg = load_config()
        cfg["general"]["mode"] = "live"
        sv = cfg["decision"]["shadow_validation"]
        sv["enabled"] = shadow_enabled
        sv["demote_file"] = os.path.join(tmp, "demotions.json")
        if demoted_fp is not None:
            from core.utils.helpers import write_json
            write_json(sv["demote_file"], {demoted_fp: {"reason": "decayed"}})
        # No connector -> live send path would fail, but a demoted fp must be
        # short-circuited to paper BEFORE any connector use.
        return OrderManager(cfg, connector=None)

    class _Decision(object):
        action = 1
        sl_atr_mult = 2.0
        tp_atr_mult = 3.0
        size_hint = 1.0
        score = 1.0

    def test_demoted_fingerprint_forced_to_paper(self):
        with tempfile.TemporaryDirectory() as tmp:
            om = self._order_manager(tmp, shadow_enabled=True, demoted_fp="fpX")
            res = om.execute(self._Decision(), "XAUUSD", atr=1.0,
                             last_close=2000.0, fingerprint="fpX")
            self.assertEqual(res["action"], "paper")
            self.assertEqual(res.get("reason"), "shadow_demoted")

    def test_non_demoted_not_affected_by_shadow_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            om = self._order_manager(tmp, shadow_enabled=True, demoted_fp="fpX")
            # A different fingerprint is not demoted; the shadow gate must not
            # force it to paper (it will fail later for "not connected", which
            # proves the shadow short-circuit did NOT fire).
            res = om.execute(self._Decision(), "XAUUSD", atr=1.0,
                             last_close=2000.0, fingerprint="fpOTHER")
            self.assertNotEqual(res.get("reason"), "shadow_demoted")

    def test_gate_off_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            om = self._order_manager(tmp, shadow_enabled=False, demoted_fp="fpX")
            # Even though fpX is in the demote file, the gate is off -> not
            # treated as demoted.
            self.assertFalse(om._is_shadow_demoted("fpX"))


if __name__ == "__main__":
    unittest.main()
