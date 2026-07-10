"""
U5.3 - Live pre-flight gauntlet gate.

`general.live_requires_gauntlet` (default true) makes LIVE mode refuse to start
unless a PASS gauntlet verdict exists for the registry top-1 strategy of every
traded symbol/timeframe, AND that verdict is newer than the last search that
produced the registry. Paper / backtest / search / train modes are never
blocked.

The verdict file is `backtests/gauntlet_<fingerprint>.md`, written by
`scripts/gauntlet.py` (U5.1/U5.2). It carries two machine-parseable lines that
this gate reads WITHOUT re-running anything:

    - created_at_epoch: <int unix seconds>
    - overall_pass: true|false

"Newer than the last search" is judged against the registry section's
`updated_at` timestamp (written by MemoryStore.update_registry): the verdict
must have been produced at or after the registry was last rebuilt, so a strategy
promoted AFTER its last gauntlet run is correctly treated as un-vetted.

Pure stdlib, ASCII only, Windows 7 + Python 3.8 + CPU-only friendly. The gate
degrades safely: any unexpected error while checking is treated as a BLOCK (fail
loudly, never let un-vetted money trade because of a parsing bug).
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from config.loader import resolve_path
from core.utils.helpers import read_json


# Machine-parseable stamps emitted by scripts/gauntlet.py::write_verdict_md.
_RE_EPOCH = re.compile(r"^\s*-\s*created_at_epoch:\s*([0-9]+)\s*$", re.MULTILINE)
_RE_PASS = re.compile(r"^\s*-\s*overall_pass:\s*(true|false)\s*$",
                      re.IGNORECASE | re.MULTILINE)


class GauntletGateResult(object):
    """Outcome of the live pre-flight check for one or more symbols."""

    def __init__(self, allowed: bool, reasons: List[str]):
        self.allowed = bool(allowed)
        self.reasons = list(reasons)

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return self.allowed

    __nonzero__ = __bool__  # Py2 style safety; harmless on Py3.


def _report_dir(cfg) -> str:
    return resolve_path(cfg, cfg.get_path("backtest.report_dir", "backtests"))


def _verdict_path(report_dir: str, fingerprint: str) -> str:
    return os.path.join(report_dir, "gauntlet_%s.md" % fingerprint)


def parse_verdict_file(path: str) -> Optional[Dict[str, Any]]:
    """
    Read a gauntlet verdict .md and return {"overall_pass": bool,
    "created_at_epoch": int} or None if the file is missing/unparseable.
    """
    try:
        with open(path, "r", encoding="ascii", errors="replace") as fh:
            text = fh.read()
    except (IOError, OSError):
        return None
    m_pass = _RE_PASS.search(text)
    m_epoch = _RE_EPOCH.search(text)
    if not m_pass or not m_epoch:
        return None
    try:
        epoch = int(m_epoch.group(1))
    except (TypeError, ValueError):
        return None
    return {
        "overall_pass": m_pass.group(1).lower() == "true",
        "created_at_epoch": epoch,
    }


def _registry_section(memory, symbol: str, timeframe: str) -> Dict[str, Any]:
    """Return the raw registry section {rank_metric, updated_at, top} or {}."""
    try:
        registry = read_json(memory.registry_path, default={}) or {}
    except Exception:
        return {}
    return registry.get("%s|%s" % (symbol, timeframe), {}) or {}


def _top1_fingerprint(section: Dict[str, Any]) -> Optional[str]:
    top = section.get("top", []) or []
    for entry in top:
        fp = entry.get("fingerprint")
        if not fp:
            spec = entry.get("spec", {}) or {}
            fp = spec.get("fingerprint")
        if fp:
            return str(fp)
    return None


def check_symbol(cfg, memory, symbol: str, timeframe: str) -> Tuple[bool, str]:
    """
    Check the live gate for one symbol/timeframe.

    Returns (allowed, reason). `allowed` is True only when a PASS verdict for
    the registry top-1 exists and is at least as new as the registry update.
    """
    section = _registry_section(memory, symbol, timeframe)
    fp = _top1_fingerprint(section)
    if not fp:
        # No promoted strategy => parity/blend would trade nothing for this
        # symbol; nothing to vet, so this symbol does not block live start.
        return True, "%s %s: no promoted strategy (nothing to trade)" % (
            symbol, timeframe)

    verdict_path = _verdict_path(_report_dir(cfg), fp)
    verdict = parse_verdict_file(verdict_path)
    if verdict is None:
        return False, (
            "%s %s: no gauntlet verdict for top-1 %s (expected %s). "
            "Run: python scripts/gauntlet.py --symbol %s --tf %s" % (
                symbol, timeframe, fp, verdict_path, symbol, timeframe))
    if not verdict["overall_pass"]:
        return False, (
            "%s %s: gauntlet verdict for %s is FAIL (%s)" % (
                symbol, timeframe, fp, verdict_path))

    updated_at = section.get("updated_at", 0) or 0
    try:
        updated_at = float(updated_at)
    except (TypeError, ValueError):
        updated_at = 0.0
    if verdict["created_at_epoch"] < updated_at:
        return False, (
            "%s %s: gauntlet verdict for %s is STALE - it predates the last "
            "search/registry update (verdict=%d < registry=%d). Re-run the "
            "gauntlet." % (symbol, timeframe, fp,
                           verdict["created_at_epoch"], int(updated_at)))
    return True, "%s %s: PASS verdict for %s is current" % (symbol, timeframe, fp)


def check_live_allowed(cfg, memory, symbols: List[str],
                       timeframe: str) -> GauntletGateResult:
    """
    Aggregate the per-symbol check. Live start is allowed only if EVERY symbol
    passes. When the config flag is off, this always allows (with a note).
    """
    if not bool(cfg.get_path("general.live_requires_gauntlet", True)):
        return GauntletGateResult(
            True, ["general.live_requires_gauntlet is false - gate disabled"])

    reasons: List[str] = []
    allowed = True
    try:
        for symbol in symbols:
            ok, reason = check_symbol(cfg, memory, symbol, timeframe)
            reasons.append(("OK  " if ok else "BLOCK ") + reason)
            if not ok:
                allowed = False
    except Exception as exc:  # fail loudly: a checking bug must not open live
        return GauntletGateResult(
            False, ["gauntlet gate check errored (blocking for safety): %s" %
                    exc])
    return GauntletGateResult(allowed, reasons)
