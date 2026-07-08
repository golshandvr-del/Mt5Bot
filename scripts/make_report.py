"""
Render a single-file HTML audit report from a backtest trade CSV (Phase U1.3).

After a backtest writes its per-trade receipt CSV (see core/utils/trade_log.py,
U1.2) this script turns it into ONE self-contained .html file the user can open
offline on Windows 7 with no external dependencies (no CDN, no JS libraries):
an inline-SVG equity + drawdown chart plus tables for the summary, per-month
PnL, the 10 worst trades, the exit-reason breakdown, and the cost share of PnL.

Usage
-----
    python scripts/make_report.py backtests/trades_XAUUSD_M15_20260708_120000.csv
    python scripts/make_report.py <trades.csv> --equity <equity.csv> \
        --out backtests/report_XAUUSD.html --title "XAUUSD M15"

If --equity is omitted the script looks for the sibling equity_*.csv produced
in the same run (same directory, same timestamp tag); if none is found it
rebuilds an equity curve from the per-trade balance_after column.

Everything is pure standard library, ASCII English only, and never depends on
network access. Designed to be import-friendly so the offline tests (U1.6) can
call build_report_html() on a synthetic run without touching the filesystem.
"""

from __future__ import annotations

import argparse
import csv
import html
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple


# --------------------------------------------------------------------------- #
# CSV loading
# --------------------------------------------------------------------------- #
def load_trades(path: str) -> List[Dict[str, Any]]:
    """Read a trade CSV into a list of dicts (values kept as strings)."""
    rows: List[Dict[str, Any]] = []
    with open(path, "r", newline="", encoding="ascii", errors="replace") as fh:
        for row in csv.DictReader(fh):
            rows.append(dict(row))
    return rows


def load_equity(path: str) -> List[float]:
    """Read an equity CSV (point_index,equity) into a list of floats."""
    values: List[float] = []
    with open(path, "r", newline="", encoding="ascii", errors="replace") as fh:
        for row in csv.DictReader(fh):
            try:
                values.append(float(row.get("equity", "")))
            except (TypeError, ValueError):
                continue
    return values


def _f(row: Dict[str, Any], key: str, default: float = 0.0) -> float:
    """Parse a float field defensively (blank/bad -> default)."""
    try:
        val = row.get(key, "")
        if val in ("", None):
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Aggregations
# --------------------------------------------------------------------------- #
def summarize(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute the headline summary numbers from the trade rows."""
    n = len(trades)
    pnls = [_f(t, "pnl") for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    net = sum(pnls)
    total_cost = sum(
        _f(t, "cost_spread") + _f(t, "cost_slippage")
        + _f(t, "cost_commission") + _f(t, "cost_swap")
        for t in trades)
    win_rate = (len(wins) / n) if n else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (
        gross_profit if gross_profit > 0 else 0.0)
    expectancy = (net / n) if n else 0.0
    return {
        "num_trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "net_profit": net,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "total_cost": total_cost,
        "avg_win": (gross_profit / len(wins)) if wins else 0.0,
        "avg_loss": (-gross_loss / len(losses)) if losses else 0.0,
    }


def monthly_pnl(trades: List[Dict[str, Any]]) -> List[Tuple[str, float, int]]:
    """Group net PnL by YYYY-MM of the EXIT time. Returns sorted (month, pnl, n)."""
    buckets: Dict[str, List[float]] = {}
    for t in trades:
        exit_time = str(t.get("exit_time", "")).strip()
        month = exit_time[:7] if len(exit_time) >= 7 else "unknown"
        buckets.setdefault(month, []).append(_f(t, "pnl"))
    out = [(m, sum(v), len(v)) for m, v in buckets.items()]
    out.sort(key=lambda x: x[0])
    return out


def worst_trades(trades: List[Dict[str, Any]], k: int = 10
                 ) -> List[Dict[str, Any]]:
    """Return the k most negative trades by net PnL."""
    ordered = sorted(trades, key=lambda t: _f(t, "pnl"))
    return ordered[:k]


def exit_reason_breakdown(trades: List[Dict[str, Any]]
                          ) -> List[Tuple[str, int, float]]:
    """Count trades and sum PnL per exit reason (sl/tp/flip/eod)."""
    buckets: Dict[str, List[float]] = {}
    for t in trades:
        reason = str(t.get("exit_reason", "") or "unknown")
        buckets.setdefault(reason, []).append(_f(t, "pnl"))
    out = [(r, len(v), sum(v)) for r, v in buckets.items()]
    out.sort(key=lambda x: -x[1])
    return out


def drawdown_series(equity: List[float]) -> List[float]:
    """Return the running drawdown fraction at each equity point."""
    dd: List[float] = []
    peak = equity[0] if equity else 0.0
    for v in equity:
        if v > peak:
            peak = v
        dd.append(((peak - v) / peak) if peak > 0 else 0.0)
    return dd


def equity_from_trades(trades: List[Dict[str, Any]],
                       initial: float = 10000.0) -> List[float]:
    """Rebuild an equity curve from the per-trade balance_after column."""
    curve = [initial]
    for t in trades:
        bal = t.get("balance_after", "")
        if bal not in ("", None):
            try:
                curve.append(float(bal))
                continue
            except (TypeError, ValueError):
                pass
        curve.append(curve[-1] + _f(t, "pnl"))
    return curve


# --------------------------------------------------------------------------- #
# Inline SVG chart (no JS, no external deps)
# --------------------------------------------------------------------------- #
def _svg_polyline(values: List[float], width: int, height: int,
                  color: str, pad: int = 4) -> str:
    """Build an SVG <polyline> scaled to fit width x height for `values`."""
    if not values:
        return ""
    lo = min(values)
    hi = max(values)
    span = (hi - lo) or 1.0
    n = len(values)
    step = (width - 2 * pad) / (n - 1) if n > 1 else 0.0
    pts = []
    for i, v in enumerate(values):
        x = pad + i * step
        # invert y so higher values sit near the top
        y = pad + (height - 2 * pad) * (1.0 - (v - lo) / span)
        pts.append("%.2f,%.2f" % (x, y))
    return ('<polyline fill="none" stroke="%s" stroke-width="1.5" '
            'points="%s" />' % (color, " ".join(pts)))


def equity_svg(equity: List[float], width: int = 900,
               height: int = 260) -> str:
    """Render an equity line + drawdown shading as a self-contained SVG."""
    if not equity:
        return "<p>No equity data.</p>"
    dd = drawdown_series(equity)
    # Draw drawdown (as negative bars) beneath a thin baseline.
    dd_h = 70
    line = _svg_polyline(equity, width, height - dd_h, "#1a7f37")
    # Drawdown polyline scaled in its own band.
    dd_neg = [-d for d in dd]
    dd_line = _svg_polyline(dd_neg, width, dd_h, "#cf222e")
    return (
        '<svg viewBox="0 0 %d %d" width="100%%" height="%d" '
        'xmlns="http://www.w3.org/2000/svg" style="background:#fafbfc;'
        'border:1px solid #d0d7de;border-radius:6px">'
        '<g>%s</g>'
        '<g transform="translate(0,%d)">'
        '<line x1="0" y1="0" x2="%d" y2="0" stroke="#d0d7de" '
        'stroke-width="1"/>%s</g>'
        '</svg>' % (width, height, height, line,
                    height - dd_h, width, dd_line)
    )


# --------------------------------------------------------------------------- #
# HTML assembly
# --------------------------------------------------------------------------- #
_CSS = """
body{font-family:-apple-system,Segoe UI,Arial,sans-serif;margin:0;padding:24px;
color:#1f2328;background:#fff;max-width:1000px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:24px 0 8px;
border-bottom:1px solid #d0d7de;padding-bottom:4px}
.meta{color:#656d76;font-size:13px;margin-bottom:16px}
table{border-collapse:collapse;width:100%;font-size:13px;margin:6px 0}
th,td{border:1px solid #d0d7de;padding:5px 8px;text-align:right}
th{background:#f6f8fa;text-align:left}td.l,th.l{text-align:left}
.pos{color:#1a7f37}.neg{color:#cf222e}
.cards{display:flex;flex-wrap:wrap;gap:10px;margin:8px 0}
.card{border:1px solid #d0d7de;border-radius:6px;padding:10px 14px;min-width:120px}
.card .k{color:#656d76;font-size:12px}.card .v{font-size:18px;font-weight:600}
"""


def _fmt_money(v: float) -> str:
    return format(v, ",.2f") if abs(v) >= 1 else format(v, ".4f")


def _cls(v: float) -> str:
    return "pos" if v > 0 else ("neg" if v < 0 else "")


def _card(label: str, value: str, css: str = "") -> str:
    return ('<div class="card"><div class="k">%s</div>'
            '<div class="v %s">%s</div></div>'
            % (html.escape(label), css, html.escape(value)))


def build_report_html(trades: List[Dict[str, Any]],
                      equity: Optional[List[float]] = None,
                      title: str = "Backtest report",
                      config_snapshot: Optional[Dict[str, Any]] = None
                      ) -> str:
    """Assemble the full single-file HTML report string. Pure, no I/O."""
    if equity is None or not equity:
        equity = equity_from_trades(trades)

    s = summarize(trades)
    dd = drawdown_series(equity)
    max_dd = max(dd) if dd else 0.0
    generated = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())

    parts: List[str] = []
    parts.append("<!DOCTYPE html><html lang='en'><head><meta charset='ascii'>")
    parts.append("<meta name='viewport' content='width=device-width,"
                 "initial-scale=1'>")
    parts.append("<title>%s</title><style>%s</style></head><body>"
                 % (html.escape(title), _CSS))
    parts.append("<h1>%s</h1>" % html.escape(title))
    parts.append("<div class='meta'>Generated %s &middot; %d trades</div>"
                 % (generated, s["num_trades"]))

    # Summary cards.
    parts.append("<div class='cards'>")
    parts.append(_card("Net profit", _fmt_money(s["net_profit"]),
                       _cls(s["net_profit"])))
    parts.append(_card("Win rate", "%.1f%%" % (100.0 * s["win_rate"])))
    parts.append(_card("Profit factor", "%.2f" % s["profit_factor"]))
    parts.append(_card("Expectancy", _fmt_money(s["expectancy"]),
                       _cls(s["expectancy"])))
    parts.append(_card("Max drawdown", "%.2f%%" % (100.0 * max_dd), "neg"))
    parts.append(_card("Total cost", _fmt_money(s["total_cost"]), "neg"))
    parts.append("</div>")

    # Equity + drawdown chart.
    parts.append("<h2>Equity curve &amp; drawdown</h2>")
    parts.append(equity_svg(equity))

    # Cost share of PnL.
    parts.append("<h2>Cost share of gross PnL</h2>")
    gross_abs = sum(abs(_f(t, "gross_pnl")) for t in trades) or 1.0
    cost_pct = 100.0 * s["total_cost"] / gross_abs
    parts.append("<p>Costs consumed <b>%.1f%%</b> of gross traded PnL "
                 "(spread + slippage + commission + swap = %s).</p>"
                 % (cost_pct, _fmt_money(s["total_cost"])))

    # Per-month PnL.
    parts.append("<h2>Monthly PnL</h2><table><tr><th class='l'>Month</th>"
                 "<th>Trades</th><th>Net PnL</th></tr>")
    for month, pnl, cnt in monthly_pnl(trades):
        parts.append("<tr><td class='l'>%s</td><td>%d</td>"
                     "<td class='%s'>%s</td></tr>"
                     % (html.escape(month), cnt, _cls(pnl), _fmt_money(pnl)))
    parts.append("</table>")

    # Exit-reason breakdown.
    parts.append("<h2>Exit-reason breakdown</h2><table><tr><th class='l'>"
                 "Reason</th><th>Trades</th><th>Net PnL</th></tr>")
    for reason, cnt, pnl in exit_reason_breakdown(trades):
        parts.append("<tr><td class='l'>%s</td><td>%d</td>"
                     "<td class='%s'>%s</td></tr>"
                     % (html.escape(reason), cnt, _cls(pnl), _fmt_money(pnl)))
    parts.append("</table>")

    # Top-10 worst trades.
    parts.append("<h2>10 worst trades</h2><table><tr><th class='l'>Entry</th>"
                 "<th class='l'>Exit</th><th class='l'>Dir</th>"
                 "<th class='l'>Reason</th><th>Net PnL</th></tr>")
    for t in worst_trades(trades, 10):
        pnl = _f(t, "pnl")
        parts.append("<tr><td class='l'>%s</td><td class='l'>%s</td>"
                     "<td class='l'>%s</td><td class='l'>%s</td>"
                     "<td class='%s'>%s</td></tr>"
                     % (html.escape(str(t.get("entry_time", ""))),
                        html.escape(str(t.get("exit_time", ""))),
                        html.escape(str(t.get("direction", ""))),
                        html.escape(str(t.get("exit_reason", ""))),
                        _cls(pnl), _fmt_money(pnl)))
    parts.append("</table>")

    # Config snapshot (U1.5) if provided.
    if config_snapshot:
        parts.append("<h2>Config snapshot</h2><table><tr><th class='l'>Key</th>"
                     "<th class='l'>Value</th></tr>")
        for k in sorted(config_snapshot.keys()):
            parts.append("<tr><td class='l'>%s</td><td class='l'>%s</td></tr>"
                         % (html.escape(str(k)),
                            html.escape(str(config_snapshot[k]))))
        parts.append("</table>")

    parts.append("</body></html>")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# Filename helpers + CLI
# --------------------------------------------------------------------------- #
def _sibling_equity(trades_path: str) -> Optional[str]:
    """Find the equity CSV that shares this trade file's SYMBOL_TF_timestamp."""
    base = os.path.basename(trades_path)
    if base.startswith("trades_") and base.endswith(".csv"):
        cand = os.path.join(os.path.dirname(trades_path),
                            "equity_" + base[len("trades_"):])
        if os.path.exists(cand):
            return cand
    return None


def _default_out(trades_path: str) -> str:
    base = os.path.basename(trades_path)
    stem = base[len("trades_"):-4] if base.startswith("trades_") else \
        os.path.splitext(base)[0]
    return os.path.join(os.path.dirname(trades_path), "report_%s.html" % stem)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Render a single-file HTML backtest audit report.")
    parser.add_argument("trades_csv", help="Path to a trades_*.csv file.")
    parser.add_argument("--equity", default=None,
                        help="Path to the equity_*.csv (auto-detected if set "
                             "in the same run; else rebuilt from balances).")
    parser.add_argument("--out", default=None,
                        help="Output .html path (default: report_<tag>.html).")
    parser.add_argument("--title", default=None,
                        help="Report title (default derived from file name).")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if not os.path.exists(args.trades_csv):
        print("Trade CSV not found: %s" % args.trades_csv)
        return 2

    trades = load_trades(args.trades_csv)
    equity_path = args.equity or _sibling_equity(args.trades_csv)
    equity = load_equity(equity_path) if equity_path and \
        os.path.exists(equity_path) else None

    title = args.title
    if title is None:
        base = os.path.basename(args.trades_csv)
        title = base[len("trades_"):-4].replace("_", " ") \
            if base.startswith("trades_") else base

    html_text = build_report_html(trades, equity, title=title)
    out = args.out or _default_out(args.trades_csv)
    with open(out, "w", encoding="ascii", errors="replace") as fh:
        fh.write(html_text)
    print("Report written to %s (%d trades)" % (out, len(trades)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
