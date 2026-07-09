"""
Internal backtest engine (Phase 3).

This is a lightweight, bar-by-bar simulator used for strategy/parameter search
and walk-forward evaluation. It is NOT the MT5 Strategy Tester; it is the bot's
own fast offline evaluator so the search loop can score thousands of strategies
without launching the terminal. The README explains how to also validate the
final chosen strategy inside the real MT5 Strategy Tester on Windows.

Model
-----
- Single position at a time (long/short/flat) per symbol.
- Enter when the strategy decision changes to +1 (long) or -1 (short).
- Exit on opposite signal, on stop-loss, or on take-profit (ATR-based).
- Costs: spread + commission + slippage applied on entry and exit.
- Position size: fixed lot from config.backtest.fixed_lot.

PnL is computed in "price units * contract" approximated by treating one lot as
a fixed notional; because the search only needs RELATIVE ranking of strategies,
exact broker accounting is not required here. Final validation happens in MT5.

All text is pure Python; ASCII English only.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.strategy.strategy import Strategy, StrategySpec
from core.strategy.metrics import compute_metrics
from core.utils.helpers import symbol_offline_specs
from core.utils.logger import get_logger


class BacktestResult(object):
    """Holds the outcome of a single backtest run."""

    def __init__(self, metrics: Dict[str, Any], equity_curve: List[float],
                 trade_pnls: List[float],
                 trades: Optional[List[Dict[str, Any]]] = None):
        self.metrics = metrics
        self.equity_curve = equity_curve
        self.trade_pnls = trade_pnls
        # Phase 5 (user-update-request): optional per-trade records with the
        # entry-bar timestamp so the timing layer can attribute PnL to time
        # buckets (session/day/season).
        #
        # Phase U1.1 (transparency overhaul): when record_trades=True each item
        # now carries the FULL trade receipt so the user can audit any trade:
        #   entry_ts, exit_ts, direction (+1/-1), entry_price, exit_price,
        #   stop_price, take_price, exit_reason (sl/tp/flip/eod),
        #   pnl, gross_pnl, cost_spread, cost_slippage, cost_commission,
        #   cost_swap, balance_after, signal (blended signal value at entry).
        # The legacy keys (entry_ts, pnl, direction) are still present so the
        # existing timing layer keeps working unchanged. Empty unless
        # record_trades=True was requested.
        self.trades: List[Dict[str, Any]] = trades or []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metrics": self.metrics,
            "num_equity_points": len(self.equity_curve),
            "num_trades": len(self.trade_pnls),
        }


class Backtester(object):
    """Bar-by-bar single-position simulator."""

    def __init__(self, cfg: Any):
        self.cfg = cfg
        self.log = get_logger("strategy.backtester", cfg)
        bt = cfg.get("backtest", {})
        self.initial_balance = float(bt.get("initial_balance", 10000.0)) if hasattr(bt, "get") else 10000.0
        self.spread_points = float(bt.get("spread_points", 10)) if hasattr(bt, "get") else 10.0
        self.commission = float(bt.get("commission_per_lot", 7.0)) if hasattr(bt, "get") else 7.0
        self.slippage_points = float(bt.get("slippage_points", 2)) if hasattr(bt, "get") else 2.0
        self.fixed_lot = float(bt.get("fixed_lot", 0.10)) if hasattr(bt, "get") else 0.10
        # Notional per lot used to scale price moves into PnL (relative ranking).
        self.contract = 100000.0
        # Point size assumption when symbol info is unavailable.
        self.default_point = 0.0001
        # ------------------------------------------------------------------ #
        # Weekend / rollover swap + Monday gap model (A6 / P3.6). Defaults are
        # a no-op so the backtester behaves byte-identically when unset. Read
        # defensively (bad/missing values fall back to the safe defaults).
        # ------------------------------------------------------------------ #
        self.swap_long_pts = self._cfg_float(bt, "swap_long_pts", 0.0)
        self.swap_short_pts = self._cfg_float(bt, "swap_short_pts", 0.0)
        self.swap_triple_day = self._cfg_int(bt, "swap_triple_day", 2)
        self.model_weekend_gap = self._cfg_bool(bt, "model_weekend_gap", False)
        # ------------------------------------------------------------------ #
        # Phase U3 - pessimistic, realistic execution (fixes diagnosis D3).
        # Every knob below defaults to the REALISTIC (pessimistic) behavior so
        # internal numbers move closer to (and never wildly above) the MT5
        # Strategy Tester. The legacy optimistic behavior stays reachable via
        # explicit config so before/after sensitivity studies are possible.
        # ------------------------------------------------------------------ #
        # U3.1 fill policy: "next_open" (DEFAULT) fills entries and signal-flip
        # exits at the NEXT bar's open (+half-spread + slippage), matching what a
        # real EA can do (it only acts on a new bar). "signal_close" restores the
        # legacy same-bar-close fills for comparison.
        self.fill_policy = self._cfg_str(bt, "fill_policy", "next_open").lower()
        if self.fill_policy not in ("next_open", "signal_close"):
            self.fill_policy = "next_open"
        # U3.2 intrabar ambiguity: when one bar touches BOTH SL and TP, which is
        # counted first? "pessimistic" (DEFAULT) always counts the STOP first for
        # both directions. "optimistic" counts the TAKE first. "midpoint"
        # averages the two outcomes (SL and TP each half).
        self.intrabar_policy = self._cfg_str(bt, "intrabar_policy",
                                             "pessimistic").lower()
        if self.intrabar_policy not in ("pessimistic", "optimistic", "midpoint"):
            self.intrabar_policy = "pessimistic"
        # U3.3 session-aware spread: spread widens during a configured rollover
        # window (and, if wired later, around news). Defaults reproduce a
        # constant spread (all mults 1.0) so old numbers are preserved when the
        # sub-block is absent.
        sm = bt.get("spread_model", {}) if hasattr(bt, "get") else {}
        self.spread_base_points = self._cfg_float(sm, "base_points",
                                                  self.spread_points)
        self.spread_rollover_mult = self._cfg_float(sm, "rollover_mult", 1.0)
        self.spread_news_mult = self._cfg_float(sm, "news_mult", 1.0)
        self.spread_rollover_hours = self._cfg_hours(
            sm, "rollover_hours_utc", [])
        # Whether a spread_model sub-block was actually provided; when absent we
        # keep using the flat spread_points for byte-identical old behavior.
        self.has_spread_model = bool(hasattr(sm, "get") and len(sm) > 0) if \
            hasattr(sm, "__len__") else False
        # U3.4 sizing: "risk_pct" (DEFAULT) sizes each trade by risk % of the
        # simulated equity using the SAME formula as RiskManager.position_size so
        # the backtest and live equity curves share geometry; "fixed_lot"
        # restores the legacy constant-lot behavior.
        self.sizing = self._cfg_str(bt, "sizing", "risk_pct").lower()
        if self.sizing not in ("risk_pct", "fixed_lot"):
            self.sizing = "risk_pct"
        risk = cfg.get("risk", {}) if hasattr(cfg, "get") else {}
        self.risk_per_trade = self._cfg_float(risk, "risk_per_trade", 0.01)
        self.min_lot = self._cfg_float(risk, "min_lot", 0.01)
        self.max_lot = self._cfg_float(risk, "max_lot", 1.0)
        self.max_daily_loss = self._cfg_float(risk, "max_daily_loss", 0.0)
        # U3.5 broker minimum stop distance (in points). Entries whose SL sits
        # closer than this are REJECTED (the MT5 tester rejects them too). 0
        # (DEFAULT) disables the check for byte-identical old behavior.
        self.min_stop_points = self._cfg_float(bt, "min_stop_points", 0.0)

    @staticmethod
    def _cfg_float(bt: Any, key: str, default: float) -> float:
        """Read a float from the backtest config block, safe on bad values."""
        try:
            if hasattr(bt, "get"):
                return float(bt.get(key, default))
        except Exception:
            pass
        return float(default)

    @staticmethod
    def _cfg_int(bt: Any, key: str, default: int) -> int:
        """Read an int from the backtest config block, safe on bad values."""
        try:
            if hasattr(bt, "get"):
                return int(bt.get(key, default))
        except Exception:
            pass
        return int(default)

    @staticmethod
    def _cfg_bool(bt: Any, key: str, default: bool) -> bool:
        """Read a bool from the backtest config block, safe on bad values."""
        if not hasattr(bt, "get"):
            return bool(default)
        val = bt.get(key, default)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.strip().lower() in ("1", "true", "yes", "on")
        try:
            return bool(val)
        except Exception:
            return bool(default)

    @staticmethod
    def _cfg_str(bt: Any, key: str, default: str) -> str:
        """Read a string from a config block, safe on bad/missing values."""
        try:
            if hasattr(bt, "get"):
                val = bt.get(key, default)
                if val is None:
                    return str(default)
                return str(val)
        except Exception:
            pass
        return str(default)

    @staticmethod
    def _cfg_hours(bt: Any, key: str, default: List[int]) -> List[int]:
        """
        Read an hours list from a config block (U3.3 spread model).

        Accepts a list/tuple of hour numbers, or a two-element [start, end]
        window which is expanded into the inclusive set of UTC hours it spans
        (wrapping across midnight when start > end). Bad values fall back to the
        provided default. Returns a sorted, de-duplicated list of ints 0..23.
        """
        try:
            if not hasattr(bt, "get"):
                return list(default)
            raw = bt.get(key, default)
            if raw is None:
                return list(default)
            hours = [int(h) % 24 for h in raw]
        except Exception:
            return list(default)
        if len(hours) == 2 and hours[0] != hours[1]:
            start, end = hours[0], hours[1]
            span: List[int] = []
            h = start
            # Walk inclusively from start to end, wrapping over midnight.
            while True:
                span.append(h)
                if h == end:
                    break
                h = (h + 1) % 24
            hours = span
        return sorted(set(int(h) % 24 for h in hours))

    @staticmethod
    def _infer_bar_seconds(times: List[int], ohlcv: Any) -> int:
        """
        Estimate the normal per-bar spacing in seconds so a weekend gap (a much
        larger pause) can be told apart from a normal bar (A6 / P3.6). Prefers
        the timeframe helper for the OHLCV's timeframe; falls back to the most
        common positive delta between consecutive timestamps; 0 if unknown.
        """
        tf = getattr(ohlcv, "timeframe", "") or ""
        if tf:
            try:
                from core.utils.helpers import timeframe_seconds
                secs = int(timeframe_seconds(tf))
                if secs > 0:
                    return secs
            except Exception:
                pass
        # Fall back to the median-ish (most common) positive delta.
        deltas: Dict[int, int] = {}
        limit = min(len(times), 200)
        for i in range(1, limit):
            try:
                d = int(times[i]) - int(times[i - 1])
            except Exception:
                continue
            if d > 0:
                deltas[d] = deltas.get(d, 0) + 1
        if not deltas:
            return 0
        return max(deltas.items(), key=lambda kv: kv[1])[0]

    @staticmethod
    def _rollovers_between(prev_ts: int, cur_ts: int, triple_day: int) -> float:
        """
        Count the swap "nights" charged between two bar timestamps.

        A swap is charged for every UTC day boundary (midnight) crossed while a
        position is held. The weekday that carries the weekend (triple_day, MT5
        convention Wednesday=2) is charged 3x; every other rollover is 1x. If a
        span crosses several midnights (e.g. over a weekend) each one is
        counted. Returns 0.0 when no midnight is crossed or timestamps are bad.
        """
        try:
            prev_ts = int(prev_ts)
            cur_ts = int(cur_ts)
        except Exception:
            return 0.0
        if cur_ts <= prev_ts:
            return 0.0
        prev_day = prev_ts // 86400
        cur_day = cur_ts // 86400
        if cur_day <= prev_day:
            return 0.0
        total = 0.0
        # Each crossed midnight belongs to the day being ENTERED. Weekday of a
        # day index d (days since epoch, 1970-01-01 = Thursday = weekday 3).
        for d in range(int(prev_day) + 1, int(cur_day) + 1):
            weekday = (d + 3) % 7  # 0=Mon .. 6=Sun
            total += 3.0 if weekday == triple_day else 1.0
        return total

    def _round_trip_cost(self, point: float, contract: float) -> float:
        """Approximate per-trade cost in PnL units (spread+slippage+commission)."""
        pts = (self.spread_points + 2.0 * self.slippage_points) * point
        cost_price = pts * self.fixed_lot * contract
        commission = self.commission * self.fixed_lot * 2.0
        return cost_price + commission

    def _cost_breakdown(self, point: float, contract: float) -> Dict[str, float]:
        """
        Split the round-trip cost into its named components (U1.1 transparency).

        Returns a dict with separate spread / slippage / commission money
        amounts (round-trip, i.e. entry + exit). Their sum equals
        `_round_trip_cost` exactly so the per-trade CSV can attribute every
        dollar of cost to a source and still reconcile with the metrics.
        """
        spread_money = self.spread_points * point * self.fixed_lot * contract
        # Slippage is applied on BOTH entry and exit (2x), matching the
        # round-trip cost formula above.
        slippage_money = 2.0 * self.slippage_points * point * self.fixed_lot * contract
        commission_money = self.commission * self.fixed_lot * 2.0
        return {
            "spread": spread_money,
            "slippage": slippage_money,
            "commission": commission_money,
        }

    def _swap_money(self, direction: int, nights: float,
                    point: float, contract: float, lot: float) -> float:
        """
        Money charged (positive) or credited (negative) for holding a position
        over `nights` rollovers (A6 / P3.6). Uses the long/short swap point
        rates converted to money via point * contract * lot. Returns 0.0 when
        swap is not modeled (both rates 0.0) so old behavior is preserved.

        Phase U3.4: `lot` is now passed explicitly because risk_pct sizing means
        each trade may hold a different lot size (previously always fixed_lot).
        """
        if nights <= 0.0:
            return 0.0
        pts = self.swap_long_pts if direction == 1 else self.swap_short_pts
        if pts == 0.0:
            return 0.0
        return pts * point * contract * lot * nights

    # ------------------------------------------------------------------ #
    # Phase U3 helpers
    # ------------------------------------------------------------------ #
    def _spread_points_at(self, ts: int) -> float:
        """
        Effective spread (in points) for a bar with UTC timestamp `ts` (U3.3).

        When no spread_model sub-block is configured this returns the flat
        `spread_points`, reproducing the legacy constant spread exactly. When a
        model IS configured, the base spread is multiplied by `rollover_mult`
        during any of the configured `rollover_hours_utc`. The news multiplier
        is exposed for future wiring; it defaults to 1.0 (no effect).
        """
        if not self.has_spread_model:
            return self.spread_points
        pts = self.spread_base_points
        try:
            hour = int((int(ts) % 86400) // 3600)
        except Exception:
            hour = -1
        if hour in self.spread_rollover_hours and self.spread_rollover_mult > 0:
            pts *= self.spread_rollover_mult
        return pts

    def _entry_cost_parts(self, lot: float, point: float, contract: float,
                          spread_points: float) -> Dict[str, float]:
        """
        Round-trip cost breakdown for ONE trade of `lot` lots (U3.1/U3.3/U3.4).

        Costs now scale with the ACTUAL traded lot (risk_pct sizing) and with
        the ACTUAL spread at entry (session-aware). Slippage is charged on both
        entry and exit (2x). Their sum is the total round-trip cost.
        """
        spread_money = spread_points * point * lot * contract
        slippage_money = 2.0 * self.slippage_points * point * lot * contract
        commission_money = self.commission * lot * 2.0
        return {
            "spread": spread_money,
            "slippage": slippage_money,
            "commission": commission_money,
        }

    def _sized_lot(self, balance: float, entry_price: float,
                   stop_price: float, contract: float) -> float:
        """
        Lot size for one entry (U3.4).

        In "fixed_lot" mode returns the constant configured lot (legacy). In
        "risk_pct" mode uses the SAME formula as RiskManager.position_size:
        risk_amount = balance * risk_per_trade; lot = risk_amount /
        (stop_distance * contract), clamped to [min_lot, max_lot]. This makes
        the backtest equity curve share geometry with live sizing.
        """
        if self.sizing == "fixed_lot":
            return self.fixed_lot
        risk_amount = balance * self.risk_per_trade
        if risk_amount <= 0:
            return self.min_lot
        stop_distance = abs(entry_price - stop_price)
        if stop_distance <= 0:
            return self.min_lot
        loss_per_lot = stop_distance * contract
        if loss_per_lot <= 0:
            return self.min_lot
        lot = risk_amount / loss_per_lot
        lot = max(self.min_lot, min(self.max_lot, lot))
        return lot

    @staticmethod
    def _make_trade_record(entry_ts: int, exit_ts: int, direction: int,
                           entry_price: float, exit_price: float,
                           stop_price: float, take_price: float,
                           exit_reason: str, pnl: float, gross_pnl: float,
                           cost_parts: Dict[str, float], swap: float,
                           balance_after: float,
                           signal: float, lot: float = 0.0) -> Dict[str, Any]:
        """
        Build one FULL per-trade receipt (U1.1 transparency).

        The dict keeps the three legacy keys (entry_ts, pnl, direction) that the
        Phase 5 timing layer already reads, and adds the full audit fields. The
        cost components (spread/slippage/commission) plus swap are broken out
        separately so a report can show "how much of my PnL went to costs".
        By construction: pnl == gross_pnl - (cost_spread + cost_slippage +
        cost_commission) - cost_swap.
        """
        return {
            # --- legacy keys (do not remove: timing layer depends on them) ---
            "entry_ts": int(entry_ts),
            "pnl": float(pnl),
            "direction": int(direction),
            # --- U1.1 full receipt ---
            "exit_ts": int(exit_ts),
            "entry_price": float(entry_price),
            "exit_price": float(exit_price),
            "stop_price": float(stop_price),
            "take_price": float(take_price),
            "exit_reason": str(exit_reason),
            "gross_pnl": float(gross_pnl),
            "cost_spread": float(cost_parts.get("spread", 0.0)),
            "cost_slippage": float(cost_parts.get("slippage", 0.0)),
            "cost_commission": float(cost_parts.get("commission", 0.0)),
            "cost_swap": float(swap),
            "balance_after": float(balance_after),
            "signal": float(signal),
            # U3.4: the actual lot traded (risk_pct sizing varies per trade).
            "lot": float(lot),
        }

    def run(self, strategy: Strategy, ohlcv: Any,
            warmup: int = 60, point: Optional[float] = None,
            contract: Optional[float] = None,
            record_trades: bool = False) -> BacktestResult:
        """
        Run the backtest over the OHLCV series.

        warmup   : number of leading bars skipped so indicators are stable.
        point    : symbol point size. If not given it is inferred from the
                   strategy/ohlcv symbol (FX 0.0001, JPY/metals 0.01).
        contract : notional units per 1.0 lot. If not given it is inferred from
                   the symbol (FX/JPY 100000, metals 100). Making these
                   symbol-aware keeps reported PnL sensible per instrument;
                   RELATIVE ranking across strategies is unaffected.
        record_trades : when True, also collect per-trade records carrying the
                   ENTRY-bar timestamp and PnL so the Phase 5 timing layer can
                   attribute outcomes to time buckets. Kept optional so the hot
                   search loop stays as light as possible when not needed.
        """
        # Infer symbol specs so gold/JPY do not report absurd PnL magnitudes.
        symbol = getattr(strategy.spec, "symbol", "") or getattr(ohlcv, "symbol", "")
        specs = symbol_offline_specs(symbol)
        if point is None:
            point = specs["point"]
        if contract is None:
            contract = specs["contract"]
        self.contract = float(contract)

        close = ohlcv.close
        high = ohlcv.high
        low = ohlcv.low
        times = getattr(ohlcv, "time", None) or []
        n = len(close)
        if n <= warmup + 5:
            return BacktestResult(compute_metrics([], [self.initial_balance]),
                                  [self.initial_balance], [], [])

        # -------------------------------------------------------------- #
        # PERFORMANCE: precompute the full per-bar decision and ATR series
        # ONCE. Previously this loop rebuilt a growing OHLCV slice and
        # recomputed every indicator from scratch on each bar, which was
        # O(n^2) per indicator and far too slow on the target Windows 7
        # hardware. Now it is a single O(n) pass. The per-bar decision is
        # computed only from data up to and including bar i (no lookahead),
        # so this is numerically equivalent to the old growing-window loop.
        # -------------------------------------------------------------- #
        decision_series = strategy.decision_series(ohlcv)
        atr_series = strategy.atr_series(ohlcv)
        # U1.1: the blended signal value AT ENTRY is part of the trade receipt.
        # Stub strategies used in tests may not implement signal_series(); read
        # it defensively so those keep working (signal recorded as 0.0 then).
        signal_series: List[float] = []
        if record_trades:
            try:
                signal_series = list(strategy.signal_series(ohlcv))
            except Exception:
                signal_series = []

        balance = self.initial_balance
        equity_curve: List[float] = [balance]
        trade_pnls: List[float] = []
        trades: List[Dict[str, Any]] = []

        position = 0          # 0 flat, +1 long, -1 short
        entry_price = 0.0
        entry_ts = 0          # entry-bar timestamp (for the timing layer)
        entry_signal = 0.0    # blended signal value at entry (U1.1 receipt)
        stop_price = 0.0
        take_price = 0.0
        swap_accum = 0.0      # money charged so far for holding this position
        lot = self.fixed_lot  # actual lot of the OPEN position (U3.4)
        # U3.1: when a directional decision appears we do NOT fill on the same
        # bar's close; we remember it and fill at the NEXT bar's open. This is
        # what a real EA can do (it only acts on the OPEN of a new bar). In
        # "signal_close" mode this stays 0 and entries fill same-bar (legacy).
        pending_entry = 0     # +1/-1 queued for next-bar-open fill, else 0
        pending_flip_exit = False  # signal-flip exit queued for next-bar open
        # Detect a weekend/holiday gap: a bar whose gap from the previous bar is
        # noticeably larger than one normal bar spacing (A6 / P3.6). Only used
        # when model_weekend_gap is on; otherwise stops fill exactly at the stop.
        tf_seconds = self._infer_bar_seconds(times, ohlcv)
        gap_threshold = tf_seconds * 3 if tf_seconds > 0 else 0
        # U3.4 daily circuit breaker: track realized loss within each UTC day.
        # 0 (or fixed_lot legacy) disables it. When today's realized loss exceeds
        # max_daily_loss * that day's starting equity, no NEW entries are taken
        # for the rest of that day (open positions still manage their exits).
        use_breaker = self.max_daily_loss > 0.0 and self.sizing == "risk_pct"
        cur_day = None
        day_start_equity = balance
        day_realized_loss = 0.0
        breaker_tripped = False
        next_open = getattr(ohlcv, "open", None) or close

        def _finalize_exit(exit_price, exit_reason, exit_ts_local):
            """Close the current position, book PnL, append receipt."""
            nonlocal balance, position, swap_accum, lot
            nonlocal day_realized_loss
            move = (exit_price - entry_price) * position
            gross_pnl = move * lot * self.contract
            # Round-trip cost scaled by the actual lot + the spread at ENTRY.
            entry_spread_pts = self._spread_points_at(entry_ts)
            cost_parts = self._entry_cost_parts(lot, point, self.contract,
                                                entry_spread_pts)
            cost = (cost_parts["spread"] + cost_parts["slippage"]
                    + cost_parts["commission"])
            pnl = gross_pnl - cost - swap_accum
            balance += pnl
            trade_pnls.append(pnl)
            equity_curve.append(balance)
            if pnl < 0:
                day_realized_loss += -pnl
            if record_trades:
                trades.append(self._make_trade_record(
                    entry_ts=entry_ts,
                    exit_ts=exit_ts_local,
                    direction=position,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    stop_price=stop_price,
                    take_price=take_price,
                    exit_reason=exit_reason,
                    pnl=pnl,
                    gross_pnl=gross_pnl,
                    cost_parts=cost_parts,
                    swap=swap_accum,
                    balance_after=balance,
                    signal=entry_signal,
                    lot=lot,
                ))
            position = 0
            swap_accum = 0.0

        for i in range(warmup, n):
            decision = decision_series[i] if i < len(decision_series) else 0
            atr = atr_series[i] if (i < len(atr_series) and atr_series[i]) else None
            if atr is None:
                atr = close[i] * 0.001

            cur_ts = times[i] if i < len(times) else 0
            prev_ts = times[i - 1] if 0 < i <= len(times) else 0
            # A gap bar is one that opens after an unusually long pause (weekend).
            is_gap_bar = (
                self.model_weekend_gap and gap_threshold > 0
                and prev_ts and cur_ts and (cur_ts - prev_ts) > gap_threshold
            )

            # -------------------------------------------------------------- #
            # U3.4 daily circuit breaker bookkeeping: roll the day and reset.
            # -------------------------------------------------------------- #
            if use_breaker and cur_ts:
                day_idx = int(cur_ts) // 86400
                if cur_day is None or day_idx != cur_day:
                    cur_day = day_idx
                    day_start_equity = balance
                    day_realized_loss = 0.0
                    breaker_tripped = False
                if (not breaker_tripped and day_start_equity > 0
                        and day_realized_loss
                        >= self.max_daily_loss * day_start_equity):
                    breaker_tripped = True

            # -------------------------------------------------------------- #
            # U3.1 FIRST: fill any queued next-bar-open ENTRY at THIS bar open.
            # -------------------------------------------------------------- #
            if pending_entry != 0 and position == 0:
                # Do not open if the breaker tripped in the meantime.
                if not (use_breaker and breaker_tripped):
                    raw_open = next_open[i] if i < len(next_open) else close[i]
                    spread_pts = self._spread_points_at(cur_ts)
                    # Adverse fill: buy pays +half-spread+slippage, sell gets
                    # -half-spread-slippage. This makes next_open never better
                    # than the raw open (pessimism, U3.1).
                    adj = (0.5 * spread_pts + self.slippage_points) * point
                    fill = raw_open + adj * pending_entry
                    tent_stop, tent_take = self._sl_tp(strategy, pending_entry,
                                                       fill, atr)
                    if self._stop_ok(fill, tent_stop, point):
                        cand_lot = self._sized_lot(balance, fill, tent_stop,
                                                   self.contract)
                        position = pending_entry
                        entry_price = fill
                        entry_ts = cur_ts
                        entry_signal = (signal_series[i]
                                        if i < len(signal_series) else 0.0)
                        stop_price = tent_stop
                        take_price = tent_take
                        lot = cand_lot
                        swap_accum = 0.0
                pending_entry = 0

            # Accrue overnight swap for every rollover crossed while holding.
            if position != 0 and prev_ts and cur_ts:
                nights = self._rollovers_between(prev_ts, cur_ts,
                                                 self.swap_triple_day)
                if nights > 0.0:
                    swap_accum += self._swap_money(position, nights,
                                                   point, self.contract, lot)

            # -------------------------------------------------------------- #
            # U3.1: a queued signal-FLIP exit fills at THIS bar's open (only if
            # SL/TP did not already fire on the prior bar).
            # -------------------------------------------------------------- #
            if pending_flip_exit and position != 0:
                raw_open = next_open[i] if i < len(next_open) else close[i]
                spread_pts = self._spread_points_at(cur_ts)
                # Closing pays the adverse side too.
                adj = (0.5 * spread_pts + self.slippage_points) * point
                exit_price = raw_open - adj * position
                pending_flip_exit = False
                _finalize_exit(exit_price, "flip", cur_ts)

            # Manage an open position (check SL/TP using this bar range).
            if position != 0:
                exit_now = False
                exit_price = close[i]
                exit_reason = ""     # "sl" / "tp" / "flip" (U1.1 receipt)
                hit_stop = False
                hit_take = False
                if position == 1:
                    hit_stop = low[i] <= stop_price
                    hit_take = high[i] >= take_price
                else:
                    hit_stop = high[i] >= stop_price
                    hit_take = low[i] <= take_price

                # U3.2 intrabar ambiguity when BOTH are touched on one bar.
                if hit_stop and hit_take:
                    resolved = self._resolve_ambiguous(
                        position, entry_price, stop_price, take_price,
                        is_gap_bar, ohlcv.open[i] if i < len(ohlcv.open) else close[i])
                    exit_price, exit_reason = resolved
                    exit_now = True
                elif hit_stop:
                    if is_gap_bar and (
                        (position == 1 and ohlcv.open[i] < stop_price) or
                        (position == -1 and ohlcv.open[i] > stop_price)
                    ):
                        exit_price = ohlcv.open[i]
                    else:
                        exit_price = stop_price
                    exit_now = True
                    exit_reason = "sl"
                elif hit_take:
                    exit_price = take_price
                    exit_now = True
                    exit_reason = "tp"
                elif ((position == 1 and decision == -1) or
                      (position == -1 and decision == 1)):
                    # Signal flip. In next_open mode we do NOT exit here; we
                    # queue it for the next bar's open (U3.1). In signal_close
                    # mode we exit at this bar's close (legacy).
                    if self.fill_policy == "next_open":
                        pending_flip_exit = True
                    else:
                        exit_now = True
                        exit_reason = "flip"

                if exit_now:
                    _finalize_exit(exit_price, exit_reason, cur_ts)

            # -------------------------------------------------------------- #
            # Enter a new position if flat and a directional decision appears.
            # -------------------------------------------------------------- #
            if (position == 0 and decision != 0 and pending_entry == 0
                    and not (use_breaker and breaker_tripped)):
                if self.fill_policy == "next_open":
                    # Queue for next-bar-open fill (U3.1). Sizing/SL/TP are
                    # decided when it actually fills.
                    pending_entry = decision
                else:
                    # Legacy same-bar-close fill.
                    entry_price = close[i]
                    tent_stop, tent_take = self._sl_tp(strategy, decision,
                                                       entry_price, atr)
                    if self._stop_ok(entry_price, tent_stop, point):
                        position = decision
                        entry_ts = cur_ts
                        entry_signal = (signal_series[i]
                                        if i < len(signal_series) else 0.0)
                        stop_price = tent_stop
                        take_price = tent_take
                        lot = self._sized_lot(balance, entry_price, tent_stop,
                                              self.contract)
                        swap_accum = 0.0

        # Close any residual position at the last close.
        if position != 0:
            last_ts = times[-1] if times else 0
            _finalize_exit(close[-1], "eod", last_ts)

        metrics = compute_metrics(trade_pnls, equity_curve)
        return BacktestResult(metrics, equity_curve, trade_pnls, trades)

    def _sl_tp(self, strategy, direction, entry_price, atr):
        """Compute (stop_price, take_price) for a direction (helper for run)."""
        sl = strategy.spec.sl_atr_mult * atr
        tp = strategy.spec.tp_atr_mult * atr
        if direction == 1:
            return entry_price - sl, entry_price + tp
        return entry_price + sl, entry_price - tp

    def _stop_ok(self, entry_price, stop_price, point):
        """U3.5: reject entries whose SL is closer than the broker minimum."""
        if self.min_stop_points <= 0.0:
            return True
        min_dist = self.min_stop_points * point
        return abs(entry_price - stop_price) >= min_dist

    def _resolve_ambiguous(self, position, entry_price, stop_price, take_price,
                           is_gap_bar, bar_open):
        """
        U3.2: resolve a bar that touched BOTH SL and TP.

        Returns (exit_price, exit_reason). "pessimistic" (default) counts the
        STOP first for both directions; "optimistic" counts the TAKE; "midpoint"
        returns the price whose PnL is the average of the two outcomes (labeled
        "sl_tp" because it is neither a pure SL nor TP fill).
        """
        # Stop fill respects the Monday-gap worse-fill rule.
        if is_gap_bar and (
            (position == 1 and bar_open < stop_price) or
            (position == -1 and bar_open > stop_price)
        ):
            stop_fill = bar_open
        else:
            stop_fill = stop_price
        if self.intrabar_policy == "optimistic":
            return take_price, "tp"
        if self.intrabar_policy == "midpoint":
            mid = 0.5 * (stop_fill + take_price)
            return mid, "sl_tp"
        # pessimistic (default)
        return stop_fill, "sl"
