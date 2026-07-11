"""
Trade-throttle learning (UPGRADE_PLAN Phase U6.4).

Mines the U1.4 decision journal (``logs/decisions_<YYYY-MM-DD>.jsonl`` written by
``core/utils/decision_log.py``) to learn which VETO GATES actually saved money
and which merely cost missed profit, then recommends AUTO-TIGHTENING only the
gates that earned it. This module NEVER inverts or creates a signal, NEVER
loosens a gate, and NEVER touches the live trading path: it is a report-only
analyzer invoked offline by ``scripts/learn_throttle.py``. Applying a
recommendation stays a manual, opt-in config edit.

How a blocked entry is scored
-----------------------------
Each journal line records ``action`` (what the engine did: +1/-1/0), ``score``
(the blended signal in [-1, +1]), a per-source ``components`` map, and the
human-readable ``reasons`` list. A veto shows up as a reason string prefixed
``veto_`` (e.g. ``veto_news_blackout=1``, ``veto_time_gate=1``,
``veto_learner=...``, ``veto_meta_label=1``). When a line carries at least one
``veto_`` reason AND its final ``action`` is 0 (flat), the engine WANTED to act
but a gate blocked it - a "blocked event".

For each blocked event we take the direction the raw score implied
(``+1`` if score > 0 else ``-1``) and look ``horizon`` journal lines ahead (same
symbol+timeframe, ordered by ``ts``) to read the realized move via the recorded
``components`` price proxy (``mid`` if present, else the engine's ``score`` is
NOT a price so we fall back to the mid/close-like field the journal exposes).
Because the journal is decision-level (not tick-level), we score direction
CORRECTNESS conservatively: a blocked signal "would have lost" (gate SAVED
money) when the price proxy moves AGAINST the implied direction over the horizon,
and "would have won" (gate COST money) when it moves WITH it. Ambiguous / missing
price data counts as neither.

Aggregation & recommendation
-----------------------------
Per gate we tally ``events`` (blocked entries attributable to it), ``saved`` (it
avoided an adverse move) and ``cost`` (it forfeited a favourable move). The
save-rate is ``saved / (saved + cost)``. A gate is recommended for tightening
only when it has ``>= min_events`` decided events AND its save-rate is
``>= min_save_rate``; otherwise it is left exactly as configured (or, if it is
mostly costing money, flagged as a candidate to REVIEW - never auto-loosened
here). The output is a plain-language Markdown report.

Design rules (repo-wide): pure standard library (json/os/time), ASCII English
only, Windows 7 + Python 3.8 friendly, degrades gracefully (a malformed or empty
journal yields an empty analysis, never an exception into the caller).
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

# Veto reason strings the engine emits (see core/decision/engine.py). A reason is
# attributed to a gate by prefix match so future veto_* reasons are picked up
# automatically without editing this list.
KNOWN_GATES = (
    "veto_news_blackout",
    "veto_time_gate",
    "veto_learner",
    "veto_meta_label",
)

# A price-proxy field the journal may carry inside components. The engine records
# raw per-source component values; when a mid/close proxy is present we can judge
# the realized direction. Kept as a tuple so the first available wins.
PRICE_PROXY_KEYS = ("mid", "close", "price", "mid_price")


def _gate_of(reason: str) -> Optional[str]:
    """Return the gate name a reason string belongs to, or None.

    Matches on the ``veto_<gate>`` prefix so ``veto_learner=0.7<=-0.50`` maps to
    ``veto_learner``. Non-veto reasons return None.
    """
    r = str(reason)
    if not r.startswith("veto_"):
        return None
    # Strip any trailing "=value" so "veto_meta_label=1" -> "veto_meta_label".
    head = r.split("=", 1)[0].strip()
    return head or None


def _price_proxy(components: Dict[str, Any]) -> Optional[float]:
    """Extract a numeric price proxy from a journal line's components map."""
    if not isinstance(components, dict):
        return None
    for key in PRICE_PROXY_KEYS:
        if key in components:
            try:
                return float(components[key])
            except Exception:
                continue
    return None


def read_journal(paths: List[str]) -> List[Dict[str, Any]]:
    """Read and parse one or more decision journal files into a flat list.

    Malformed lines are skipped silently (append-only journals can end mid-line
    after a crash). Records are returned in file order; callers that need chrono
    order should sort by ``ts``.
    """
    records: List[Dict[str, Any]] = []
    for path in paths or []:
        try:
            if not os.path.isfile(path):
                continue
            with open(path, "r", encoding="ascii", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(rec, dict):
                        records.append(rec)
        except Exception:
            continue
    return records


def _blocked_direction(rec: Dict[str, Any]) -> Optional[int]:
    """Direction a blocked entry WOULD have taken, or None if not a blocked entry.

    A blocked entry is a line whose final ``action`` is 0 (flat) yet carries at
    least one ``veto_`` reason and a non-zero ``score`` (so the engine genuinely
    wanted to act). The implied direction is the sign of the score.
    """
    try:
        action = int(rec.get("action", 0))
    except Exception:
        action = 0
    if action != 0:
        return None
    reasons = rec.get("reasons", []) or []
    if not any(_gate_of(r) for r in reasons):
        return None
    try:
        score = float(rec.get("score", 0.0))
    except Exception:
        score = 0.0
    if score > 0.0:
        return 1
    if score < 0.0:
        return -1
    return None


def analyze_journal(records: List[Dict[str, Any]], horizon: int = 5
                    ) -> Dict[str, Dict[str, int]]:
    """Tally per-gate saved/cost/undecided events over the journal.

    For each blocked entry we compare the price proxy ``horizon`` lines ahead
    (same symbol+timeframe) against the implied direction. Returns a dict keyed
    by gate name -> {"events", "saved", "cost", "undecided"}.
    """
    horizon = max(1, int(horizon))
    # Group indices by (symbol, timeframe) in chronological order for lookahead.
    ordered = sorted(
        range(len(records)),
        key=lambda i: (
            str(records[i].get("symbol", "")),
            str(records[i].get("timeframe", "")),
            _safe_ts(records[i]),
        ),
    )
    # Build a per-key ordered list of (position_in_group, record_index).
    groups: Dict[str, List[int]] = {}
    for idx in ordered:
        rec = records[idx]
        key = "%s|%s" % (rec.get("symbol", ""), rec.get("timeframe", ""))
        groups.setdefault(key, []).append(idx)

    stats: Dict[str, Dict[str, int]] = {}

    def _bump(gate: str, field: str) -> None:
        row = stats.setdefault(
            gate, {"events": 0, "saved": 0, "cost": 0, "undecided": 0})
        row[field] += 1

    for key, idxs in groups.items():
        for pos, idx in enumerate(idxs):
            rec = records[idx]
            direction = _blocked_direction(rec)
            if direction is None:
                continue
            gates = _gates_in(rec)
            if not gates:
                continue
            verdict = _score_lookahead(records, idxs, pos, direction, horizon)
            for gate in gates:
                _bump(gate, "events")
                if verdict == "saved":
                    _bump(gate, "saved")
                elif verdict == "cost":
                    _bump(gate, "cost")
                else:
                    _bump(gate, "undecided")
    return stats


def _safe_ts(rec: Dict[str, Any]) -> int:
    try:
        return int(rec.get("ts", 0))
    except Exception:
        return 0


def _gates_in(rec: Dict[str, Any]) -> List[str]:
    """Distinct gate names that vetoed this record, preserving first-seen order."""
    seen: List[str] = []
    for r in rec.get("reasons", []) or []:
        gate = _gate_of(r)
        if gate and gate not in seen:
            seen.append(gate)
    return seen


def _score_lookahead(records: List[Dict[str, Any]], idxs: List[int], pos: int,
                     direction: int, horizon: int) -> str:
    """Classify one blocked event as 'saved', 'cost' or 'undecided'.

    Uses the price proxy of the current line vs the line ``horizon`` positions
    ahead within the SAME symbol/timeframe group. Move WITH the implied direction
    => the gate cost money (trade would have won). Move AGAINST => the gate saved
    money (trade would have lost). Missing price data => undecided.
    """
    here = records[idxs[pos]]
    ahead_pos = pos + horizon
    if ahead_pos >= len(idxs):
        return "undecided"
    there = records[idxs[ahead_pos]]
    p0 = _price_proxy(here.get("components", {}) or {})
    p1 = _price_proxy(there.get("components", {}) or {})
    if p0 is None or p1 is None:
        return "undecided"
    move = (p1 - p0) * float(direction)
    if move > 0.0:
        return "cost"   # price went the way the blocked trade wanted -> forfeited gain
    if move < 0.0:
        return "saved"  # price went against it -> gate avoided a loss
    return "undecided"


def recommend(stats: Dict[str, Dict[str, int]], min_events: int = 20,
              min_save_rate: float = 0.60) -> Dict[str, Dict[str, Any]]:
    """Turn raw per-gate tallies into tighten/keep/review recommendations.

    A gate is recommended TIGHTEN only when it has >= ``min_events`` DECIDED
    events (saved + cost) and its save-rate >= ``min_save_rate``. A gate whose
    save-rate is clearly poor (< 1 - min_save_rate) with enough evidence is
    flagged REVIEW (a human should check whether it is too aggressive) - it is
    NEVER auto-loosened here. Everything else is KEEP.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for gate, row in sorted(stats.items()):
        saved = int(row.get("saved", 0))
        cost = int(row.get("cost", 0))
        decided = saved + cost
        save_rate = (saved / decided) if decided > 0 else 0.0
        if decided >= int(min_events) and save_rate >= float(min_save_rate):
            action = "tighten"
        elif decided >= int(min_events) and save_rate <= (1.0 - float(min_save_rate)):
            action = "review"
        else:
            action = "keep"
        out[gate] = {
            "events": int(row.get("events", 0)),
            "saved": saved,
            "cost": cost,
            "undecided": int(row.get("undecided", 0)),
            "decided": decided,
            "save_rate": round(save_rate, 4),
            "action": action,
        }
    return out


def render_report(recommendations: Dict[str, Dict[str, Any]],
                  min_events: int, min_save_rate: float,
                  journal_files: Optional[List[str]] = None) -> str:
    """Render a plain-language Markdown report of the recommendations."""
    lines: List[str] = []
    lines.append("# Trade-throttle learning report (UPGRADE_PLAN U6.4)")
    lines.append("")
    lines.append("Report-only. This NEVER changes the live path; applying a")
    lines.append("recommendation is a manual, opt-in config edit. Gates are only")
    lines.append("ever TIGHTENED, never loosened, and signals are never inverted.")
    lines.append("")
    lines.append("- min_events: %d" % int(min_events))
    lines.append("- min_save_rate: %.2f" % float(min_save_rate))
    if journal_files:
        lines.append("- journals analyzed: %d" % len(journal_files))
    lines.append("")
    if not recommendations:
        lines.append("No blocked entries with veto gates were found in the")
        lines.append("journal, so there is nothing to recommend yet.")
        lines.append("")
        return "\n".join(lines)
    lines.append("| gate | events | saved | cost | undecided | save_rate | action |")
    lines.append("|------|-------:|------:|-----:|----------:|----------:|--------|")
    for gate, row in sorted(recommendations.items()):
        lines.append("| %s | %d | %d | %d | %d | %.2f | %s |" % (
            gate, row["events"], row["saved"], row["cost"],
            row["undecided"], row["save_rate"], row["action"]))
    lines.append("")
    tighten = [g for g, r in recommendations.items() if r["action"] == "tighten"]
    review = [g for g, r in recommendations.items() if r["action"] == "review"]
    lines.append("## What to do")
    if tighten:
        lines.append("- TIGHTEN (these gates reliably saved money): %s"
                     % ", ".join(sorted(tighten)))
    if review:
        lines.append("- REVIEW (these gates mostly cost missed profit - check "
                     "whether they are too aggressive): %s"
                     % ", ".join(sorted(review)))
    if not tighten and not review:
        lines.append("- KEEP everything: no gate has enough evidence to change.")
    lines.append("")
    return "\n".join(lines)


def run_analysis(journal_files: List[str], horizon: int = 5,
                 min_events: int = 20, min_save_rate: float = 0.60
                 ) -> Tuple[Dict[str, Dict[str, Any]], str]:
    """Convenience end-to-end: read -> analyze -> recommend -> render.

    Returns ``(recommendations, markdown_report)``. Pure, side-effect free
    (the caller decides whether/where to write the report).
    """
    records = read_journal(journal_files)
    stats = analyze_journal(records, horizon=horizon)
    recs = recommend(stats, min_events=min_events, min_save_rate=min_save_rate)
    report = render_report(recs, min_events, min_save_rate,
                           journal_files=journal_files)
    return recs, report
