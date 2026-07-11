"""
Continuous shadow validation - the HARD safety demotion (UPGRADE_PLAN U6.5).

The soft decay monitor (``core/strategy/decay_monitor.py``, wired in P5.6) only
ZERO-WEIGHTS a drifting strategy inside a blend - it never pulls the strategy off
live money. This module is the hard layer on top of it: run OFFLINE (e.g. every
weekend on the VPS via ``scripts/shadow_validate.py``), it re-scores each live
strategy on its trailing window of realized live/paper trades against the
walk-forward reference distribution it was promoted on, reusing the SAME
``DecayMonitor.assess`` maths so "shadow-suspect" is exactly "decay-suspect".

When a strategy's live window has decayed below the decay threshold AND it has at
least ``min_live_trades`` of live evidence, the validator DEMOTES it: the
fingerprint is persisted to a demotions file with a plain-language reason, and
the live path (order manager) refuses to send REAL orders for a demoted
fingerprint until a fresh search re-validates it. When ``clear_on_pass`` is true
a previously demoted strategy whose live window has RECOVERED above the threshold
is auto-un-demoted.

Hard guarantees (repo-wide):
  * NEVER promotes, edits a strategy, or trades - it can only pull a decayed edge
    OFF live money (a strictly conservative, one-way-safe action).
  * Config-gated and DEFAULT OFF (``decision.shadow_validation.enabled``): with
    the gate off nothing is demoted and the live path is byte-for-byte unchanged.
  * Pure standard library, ASCII English only, Windows 7 + Python 3.8 friendly,
    and degrades gracefully (any error -> no demotion, live path untouched).
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from core.strategy.decay_monitor import DecayMonitor
from core.utils.helpers import read_json, write_json
from core.utils.logger import get_logger


class ShadowVerdict(object):
    """Outcome of shadow-validating one strategy."""

    def __init__(self, fingerprint: str, action: str, reason: str,
                 ref_mean: float, recent_mean: float, recent_n: int):
        self.fingerprint = fingerprint
        # action is one of: "demote", "clear", "keep_demoted", "ok", "skip"
        self.action = action
        self.reason = reason
        self.ref_mean = ref_mean
        self.recent_mean = recent_mean
        self.recent_n = recent_n

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "action": self.action,
            "reason": self.reason,
            "ref_mean": self.ref_mean,
            "recent_mean": self.recent_mean,
            "recent_n": self.recent_n,
        }


class ShadowValidator(object):
    """
    Re-score live strategies and DEMOTE decayed ones off live money.

    Config (all under ``decision.shadow_validation``):
      * enabled          (bool, default False) master switch.
      * window           (int, default 200) trailing live trades to re-score on.
      * min_live_trades  (int, default 30) minimum live evidence before demoting.
      * clear_on_pass    (bool, default True) auto-un-demote a recovered strategy.
      * demote_file      (str) persisted set of demoted fingerprints.
      * report_file      (str) where a plain-language run report is written.

    The decay decision itself reuses ``DecayMonitor`` so the threshold semantics
    match the soft monitor exactly (a strategy the soft layer would zero-weight is
    the same one this layer demotes when it has enough live evidence).
    """

    def __init__(self, cfg: Any, memory: Any, monitor: Optional[DecayMonitor] = None):
        self.cfg = cfg
        self.memory = memory
        self.log = get_logger("strategy.shadow_validation", cfg)

        def _cfg(path, default):
            if cfg is None or not hasattr(cfg, "get_path"):
                return default
            try:
                return cfg.get_path(path, default)
            except Exception:
                return default

        self.enabled = bool(_cfg("decision.shadow_validation.enabled", False))
        self.window = int(_cfg("decision.shadow_validation.window", 200))
        self.min_live_trades = int(
            _cfg("decision.shadow_validation.min_live_trades", 30))
        self.clear_on_pass = bool(
            _cfg("decision.shadow_validation.clear_on_pass", True))
        self.demote_file = str(
            _cfg("decision.shadow_validation.demote_file",
                 "data_store/demotions.json"))
        self.report_file = str(
            _cfg("decision.shadow_validation.report_file",
                 "backtests/shadow_report.md"))
        # Reuse (or build) a DecayMonitor so thresholds are shared with the soft
        # layer. We force the monitor "enabled" for the assessment call because a
        # user may run the hard shadow layer without the soft in-blend layer.
        self.monitor = monitor if monitor is not None else DecayMonitor(cfg)

    # ------------------------------------------------------------------ #
    def load_demotions(self) -> Dict[str, Any]:
        """Load the persisted demotions map ``{fingerprint: {reason, ts}}``."""
        data = read_json(self.demote_file, default={})
        return data if isinstance(data, dict) else {}

    def save_demotions(self, demotions: Dict[str, Any]) -> bool:
        return write_json(self.demote_file, demotions)

    def is_demoted(self, fingerprint: str) -> bool:
        """True if ``fingerprint`` is currently demoted (live orders refused)."""
        if not fingerprint:
            return False
        try:
            return str(fingerprint) in self.load_demotions()
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    def _assess_suspect(self, reference: List[float], recent: List[float]) -> bool:
        """Decayed? Reuse DecayMonitor maths, forcing an enabled assessment.

        DecayMonitor.assess short-circuits to non-suspect when its OWN enabled
        flag is off; here we want the decay maths regardless of the soft layer's
        switch, so we temporarily assert enabled around the call.
        """
        prev = self.monitor.enabled
        try:
            self.monitor.enabled = True
            return self.monitor.assess(reference, recent).suspect
        except Exception as exc:
            self.log.error("shadow assess failed: %s", exc)
            return False
        finally:
            self.monitor.enabled = prev

    def validate_one(self, fingerprint: str,
                     demotions: Dict[str, Any]) -> ShadowVerdict:
        """Assess a single strategy and return the action to take (no I/O)."""
        fp = str(fingerprint)
        already = fp in demotions
        try:
            reference = self.memory.reference_pnls(fp)
            recent = self.memory.recent_live_pnls(fp, limit=self.window)
        except Exception as exc:
            self.log.error("shadow validate fetch %s failed: %s", fp, exc)
            reference, recent = [], []

        def _mean(xs):
            return (sum(xs) / len(xs)) if xs else 0.0

        ref_mean, rec_mean, rec_n = _mean(reference), _mean(recent), len(recent)

        if rec_n < self.min_live_trades:
            # Not enough live evidence: never demote on noise. If it was demoted
            # before, we leave it demoted (a recovery needs real evidence).
            action = "keep_demoted" if already else "skip"
            reason = ("insufficient live evidence (%d < %d trades)"
                      % (rec_n, self.min_live_trades))
            return ShadowVerdict(fp, action, reason, ref_mean, rec_mean, rec_n)

        suspect = self._assess_suspect(reference, recent)
        if suspect:
            if already:
                return ShadowVerdict(fp, "keep_demoted",
                                     "still decayed on the live window",
                                     ref_mean, rec_mean, rec_n)
            return ShadowVerdict(
                fp, "demote",
                ("live-window mean PnL %.4f decayed below the reference %.4f "
                 "(walk-forward) beyond the decay threshold" % (rec_mean, ref_mean)),
                ref_mean, rec_mean, rec_n)
        # Not suspect (recovered or never decayed).
        if already and self.clear_on_pass:
            return ShadowVerdict(
                fp, "clear",
                ("live-window mean PnL %.4f recovered above the decay threshold "
                 "(reference %.4f) - re-enabled for live" % (rec_mean, ref_mean)),
                ref_mean, rec_mean, rec_n)
        if already:
            return ShadowVerdict(fp, "keep_demoted",
                                 "recovered but clear_on_pass is off",
                                 ref_mean, rec_mean, rec_n)
        return ShadowVerdict(fp, "ok", "within tolerance",
                             ref_mean, rec_mean, rec_n)

    def run(self) -> Dict[str, Any]:
        """Validate every live strategy, persist demotions, return a summary.

        Returns ``{"enabled", "verdicts": [ShadowVerdict.to_dict...],
        "demoted", "cleared", "report"}``. When disabled it is a pure no-op that
        neither reads fingerprints nor writes the demotions file.
        """
        if not self.enabled:
            return {"enabled": False, "verdicts": [], "demoted": [],
                    "cleared": [], "report": self._render([], disabled=True)}

        demotions = self.load_demotions()
        try:
            fingerprints = self.memory.live_trade_fingerprints()
        except Exception as exc:
            self.log.error("shadow run fingerprint fetch failed: %s", exc)
            fingerprints = []
        # Also re-check anything already demoted even if it has stopped trading.
        for fp in list(demotions.keys()):
            if fp not in fingerprints:
                fingerprints.append(fp)

        verdicts: List[ShadowVerdict] = []
        demoted_now: List[str] = []
        cleared_now: List[str] = []
        now = int(time.time())
        for fp in fingerprints:
            v = self.validate_one(fp, demotions)
            verdicts.append(v)
            if v.action == "demote":
                demotions[fp] = {"reason": v.reason, "ts": now,
                                 "recent_mean": v.recent_mean,
                                 "ref_mean": v.ref_mean}
                demoted_now.append(fp)
                self.log.info("SHADOW DEMOTE %s to paper: %s", fp, v.reason)
            elif v.action == "clear":
                demotions.pop(fp, None)
                cleared_now.append(fp)
                self.log.info("SHADOW CLEAR %s back to live: %s", fp, v.reason)

        if demoted_now or cleared_now:
            self.save_demotions(demotions)

        report = self._render(verdicts, disabled=False)
        try:
            self._write_report(report)
        except Exception as exc:
            self.log.error("shadow report write failed: %s", exc)

        return {"enabled": True,
                "verdicts": [v.to_dict() for v in verdicts],
                "demoted": demoted_now, "cleared": cleared_now,
                "report": report}

    # ------------------------------------------------------------------ #
    def _write_report(self, report: str) -> None:
        from core.utils.helpers import ensure_dir
        import os
        directory = os.path.dirname(os.path.abspath(self.report_file))
        if directory:
            ensure_dir(directory)
        with open(self.report_file, "w", encoding="ascii", errors="replace") as fh:
            fh.write(report)

    def _render(self, verdicts: List[ShadowVerdict], disabled: bool) -> str:
        lines: List[str] = []
        lines.append("# Continuous shadow validation report (UPGRADE_PLAN U6.5)")
        lines.append("")
        lines.append("Hard safety layer. It can ONLY demote a decayed strategy to")
        lines.append("paper (or clear a recovered one); it never promotes, edits,")
        lines.append("or trades. A demoted fingerprint is refused REAL orders until")
        lines.append("a fresh search re-validates it.")
        lines.append("")
        if disabled:
            lines.append("Shadow validation is DISABLED "
                         "(decision.shadow_validation.enabled=false); nothing was")
            lines.append("assessed or demoted.")
            lines.append("")
            return "\n".join(lines)
        lines.append("- window: %d trailing live trades" % self.window)
        lines.append("- min_live_trades: %d" % self.min_live_trades)
        lines.append("- clear_on_pass: %s" % ("true" if self.clear_on_pass else "false"))
        lines.append("")
        if not verdicts:
            lines.append("No live strategies with recorded trades were found.")
            lines.append("")
            return "\n".join(lines)
        lines.append("| fingerprint | action | recent_mean | ref_mean | live_trades | reason |")
        lines.append("|-------------|--------|------------:|---------:|------------:|--------|")
        for v in verdicts:
            lines.append("| %s | %s | %.4f | %.4f | %d | %s |" % (
                v.fingerprint[:12], v.action, v.recent_mean, v.ref_mean,
                v.recent_n, v.reason))
        lines.append("")
        demoted = [v.fingerprint for v in verdicts if v.action == "demote"]
        cleared = [v.fingerprint for v in verdicts if v.action == "clear"]
        lines.append("## Summary")
        lines.append("- DEMOTED to paper this run: %s"
                     % (", ".join(f[:12] for f in demoted) or "(none)"))
        lines.append("- CLEARED back to live this run: %s"
                     % (", ".join(f[:12] for f in cleared) or "(none)"))
        lines.append("")
        return "\n".join(lines)
