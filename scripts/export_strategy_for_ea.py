"""
Export the best learned strategy for use inside the native MT5 Strategy Tester.

Phase 3 stores the top strategies per symbol/timeframe in
data_store/strategy_registry.json. The MT5 Expert Advisor
(experts/Mt5SmartBotEA.mq5) cannot read that nested JSON directly, so this
script flattens the chosen strategy into a simple, EA-friendly key=value file:

    experts/params/<SYMBOL>_<TIMEFRAME>.params

That file is loaded by the EA at runtime (from the terminal's MQL5\\Files folder;
see experts/README_EA.md for where to copy it).

PARITY HARD GUARD (U2.1)
------------------------
Only the indicators the EA implements natively (EA_SUPPORTED_INDICATORS) can be
run 1:1 in the Strategy Tester. By DEFAULT this exporter runs in --strict mode:
if the chosen strategy uses ANY indicator the EA cannot run, the export FAILS
with a clear message listing the offenders, instead of silently writing a
crippled strategy that will not match what was validated in Python. Pass
--allow-partial for experiments: the unsupported indicators are dropped, the
surviving weights are rescaled to conserve total weight, and a prominent WARNING
block is stamped into the .params header.

Usage
-----
    python scripts/export_strategy_for_ea.py                 # strict (default)
    python scripts/export_strategy_for_ea.py --symbol EURUSD --timeframe M15
    python scripts/export_strategy_for_ea.py --all
    python scripts/export_strategy_for_ea.py --allow-partial  # degraded, warned

All text is standard ASCII English only.
"""

from __future__ import annotations

import argparse
import os
import sys


# Indicators the EA (Mt5SmartBotEA.mq5) understands natively. Keep this list in
# sync with the EA's ParseParams() switch.
# U2.3 grew the EA's native indicator set: it now also implements supertrend,
# bbands and stoch in Mt5SmartBotEA.mq5::BlendedSignal(), with param keys that
# match Python's default_params (supertrend: period/multiplier, bbands:
# period/std, stoch: k/d/smooth). Keep this tuple in sync with the EA's
# ApplyParam() switch.
EA_SUPPORTED_INDICATORS = (
    "ema", "sma", "rsi", "macd", "atr", "adx",
    "supertrend", "bbands", "stoch",
)


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _unsupported_indicators(spec: dict) -> list:
    """Return the sorted list of indicator names the EA cannot run."""
    indicators = spec.get("indicators", {}) or {}
    bad = [name for name in indicators if name not in EA_SUPPORTED_INDICATORS]
    return sorted(set(bad))


def _flatten_spec(spec: dict):
    """
    Turn a StrategySpec dict into flat key=value lines the EA can parse.

    Only the EA-supported indicators are emitted. When some indicators are
    dropped (partial export), the weights of the SURVIVING supported indicators
    are rescaled so their relative proportions are preserved AND their sum
    matches the original supported-plus-unsupported total (so the blended signal
    keeps a comparable magnitude instead of silently shrinking).

    Emitted keys:
        long_threshold, short_threshold, sl_atr_mult, tp_atr_mult
        ind.<name>.enabled, ind.<name>.weight, ind.<name>.<param>

    Returns (lines, skipped, rescaled) where `rescaled` is True when at least
    one supported weight was changed because unsupported indicators were dropped.
    """
    lines = []
    lines.append("long_threshold=%s" % spec.get("long_threshold", 0.3))
    lines.append("short_threshold=%s" % spec.get("short_threshold", 0.3))
    lines.append("sl_atr_mult=%s" % spec.get("sl_atr_mult", 2.0))
    lines.append("tp_atr_mult=%s" % spec.get("tp_atr_mult", 3.0))

    indicators = spec.get("indicators", {}) or {}
    weights = spec.get("weights", {}) or {}

    supported = [n for n in indicators if n in EA_SUPPORTED_INDICATORS]
    skipped = [n for n in indicators if n not in EA_SUPPORTED_INDICATORS]

    # Rescale surviving weights when we drop unsupported ones, so the total
    # weight mass is conserved (preserves signal magnitude, not just ratios).
    def _w(name):
        try:
            return float(weights.get(name, 1.0))
        except (TypeError, ValueError):
            return 1.0

    scaled = {name: _w(name) for name in supported}
    rescaled = False
    if skipped and supported:
        total_all = sum(_w(n) for n in indicators)
        total_supported = sum(scaled.values())
        if total_supported > 0 and total_all > 0 \
                and abs(total_all - total_supported) > 1e-12:
            factor = total_all / total_supported
            scaled = {name: w * factor for name, w in scaled.items()}
            rescaled = True

    for name in supported:
        params = indicators.get(name)
        lines.append("ind.%s.enabled=1" % name)
        lines.append("ind.%s.weight=%s" % (name, scaled[name]))
        if isinstance(params, dict):
            for pkey, pval in params.items():
                lines.append("ind.%s.%s=%s" % (name, pkey, pval))
    return lines, skipped, rescaled


def _export_one(root, registry, symbol, timeframe, out_dir, strict=True):
    """
    Export ONE symbol/timeframe.

    strict=True (DEFAULT): if the spec contains ANY indicator the EA cannot run,
    the export FAILS (returns False) with a clear message listing them, instead
    of silently writing a crippled strategy.

    strict=False (--allow-partial): the unsupported indicators are dropped, the
    remaining weights are rescaled, and a prominent WARNING block is written into
    the .params header so a human can never mistake it for a faithful export.
    """
    key = "%s|%s" % (symbol, timeframe)
    section = registry.get(key, {})
    top = section.get("top", [])
    if not top:
        print("  [WARN] No learned strategy in registry for %s %s. "
              "Run 'python main.py --mode search' first." % (symbol, timeframe))
        return False
    spec = top[0].get("spec", {})

    unsupported = _unsupported_indicators(spec)
    if unsupported and strict:
        print("  [FAIL] %s %s NOT exported: the strategy uses indicator(s) the "
              "EA cannot run: %s" % (symbol, timeframe, ", ".join(unsupported)))
        print("         EA supports: %s" % ", ".join(EA_SUPPORTED_INDICATORS))
        print("         Fix by re-running search with "
              "memory.search.ea_compatible_only: true, add the indicator(s) to "
              "the EA, or pass --allow-partial to export a DEGRADED strategy.")
        return False

    lines, skipped, rescaled = _flatten_spec(spec)

    supported_left = [ln for ln in lines if ln.startswith("ind.")]
    if skipped and not supported_left:
        print("  [FAIL] %s %s NOT exported: every indicator in the strategy is "
              "unsupported by the EA (%s). Nothing to export."
              % (symbol, timeframe, ", ".join(sorted(set(skipped)))))
        return False

    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "%s_%s.params" % (symbol, timeframe))
    header = [
        "# Auto-generated by scripts/export_strategy_for_ea.py",
        "# Strategy params for the MT5 Strategy Tester EA (Mt5SmartBotEA.mq5).",
        "# Copy this file into your terminal's MQL5\\Files folder.",
    ]
    if skipped:
        header += [
            "# !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
            "# !! WARNING: PARTIAL / DEGRADED EXPORT (--allow-partial).",
            "# !! The validated strategy used indicator(s) this EA CANNOT run:",
            "# !!   %s" % ", ".join(sorted(set(skipped))),
            "# !! They were DROPPED. This .params does NOT match what was",
            "# !! validated in Python, so Strategy Tester results here will",
            "# !! differ from the backtest. Do NOT trade this as-is.",
        ]
        if rescaled:
            header.append("# !! Surviving indicator weights were RESCALED to "
                          "conserve total weight.")
        header.append("# !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    header += [
        "symbol=%s" % symbol,
        "timeframe=%s" % timeframe,
    ]
    with open(out_path, "w", encoding="ascii") as fh:
        fh.write("\n".join(header + lines) + "\n")
    if skipped:
        print("  [WARN] %s %s -> %s (PARTIAL: dropped %s%s)"
              % (symbol, timeframe, out_path, ", ".join(sorted(set(skipped))),
                 "; weights rescaled" if rescaled else ""))
    else:
        print("  [ OK ] %s %s -> %s" % (symbol, timeframe, out_path))
    return True


def main(argv=None) -> int:
    root = _project_root()
    if root not in sys.path:
        sys.path.insert(0, root)

    from config.loader import load_config
    from core.utils.helpers import read_json

    parser = argparse.ArgumentParser(
        description="Export the best learned strategy for the MT5 tester EA."
    )
    parser.add_argument("--config", default=None)
    parser.add_argument("--symbol", default=None,
                        help="Single symbol (default: all config symbols).")
    parser.add_argument("--timeframe", default=None,
                        help="Timeframe label (default: config mt5.timeframe).")
    parser.add_argument("--all", action="store_true",
                        help="Export every configured symbol.")
    # Parity hard guard (U2.1). Strict is the DEFAULT: refuse to export a
    # strategy the EA cannot faithfully reproduce.
    parser.add_argument("--strict", dest="strict", action="store_true",
                        default=True,
                        help="Refuse to export if the strategy uses any "
                             "EA-unsupported indicator (DEFAULT).")
    parser.add_argument("--allow-partial", dest="strict", action="store_false",
                        help="Experiments only: drop unsupported indicators, "
                             "rescale weights, and stamp a WARNING into the "
                             ".params header instead of failing.")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    cfg = load_config(args.config)
    registry_rel = cfg.get_path("memory.registry_file",
                                "data_store/strategy_registry.json")
    registry_path = registry_rel if os.path.isabs(registry_rel) \
        else os.path.join(root, registry_rel)
    registry = read_json(registry_path, default={}) or {}
    if not registry:
        print("[ERROR] Registry is empty (%s). Run a search first:" % registry_path)
        print("        python main.py --mode search")
        return 1

    timeframe = args.timeframe or str(cfg.get_path("mt5.timeframe", "M15"))
    if args.symbol:
        symbols = [args.symbol.upper()]
    else:
        symbols = [str(s) for s in cfg.get_path("mt5.symbols", ["EURUSD"])]

    out_dir = os.path.join(root, "experts", "params")
    print("Exporting EA strategy params to %s" % out_dir)
    if args.strict:
        print("Mode: STRICT (default) - refusing any partial/degraded export.")
    else:
        print("Mode: --allow-partial - DEGRADED exports are permitted.")
    exported = 0
    failed = 0
    for sym in symbols:
        if _export_one(root, registry, sym, timeframe, out_dir,
                       strict=args.strict):
            exported += 1
        else:
            failed += 1

    print("Done. Exported %d file(s); %d skipped/failed." % (exported, failed))
    print("Next: copy experts/params/*.params into your terminal's MQL5\\Files "
          "folder, then run Mt5SmartBotEA in the Strategy Tester.")
    return 0 if exported > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
