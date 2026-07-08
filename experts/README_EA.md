# MT5 Strategy Tester Expert Advisor (`Mt5SmartBotEA.mq5`)

This folder contains a native MetaTrader 5 Expert Advisor (EA) that lets you
validate a **learned strategy** from this project inside the **native MT5
Strategy Tester** (tick-accurate, real spread/swaps/history). This is the
authoritative check to run before trading live, complementing the fast internal
Python backtester used during strategy search.

All text here is standard ASCII English only.

---

## Why an EA in addition to the Python backtester?

- The Python backtester (`core/strategy/backtester.py`) is a **fast, relative**
  simulator used to rank thousands of strategy candidates during search.
- The MT5 Strategy Tester models the market far more faithfully (real ticks,
  spread, swaps, commissions, gaps). Use it for the **final** validation of the
  single best strategy the Python side selected.

The EA re-implements the same **blended-indicator** logic for the indicators it
supports natively, so the tester evaluates the same idea the bot learned.

---

## Supported indicators

The EA implements these natively (kept in sync with
`scripts/export_strategy_for_ea.py` -> `EA_SUPPORTED_INDICATORS`):

The mappings below are kept **byte-for-byte identical** to the Python
implementations (`core/indicators/*.py` and `core/strategy/strategy.py`). They
are CONTINUOUS `[-1, +1]` signals, not hard `+/-1` votes - matching this
exactly is what makes the tester result meaningful.

| Indicator | Signal mapping in the EA (identical to Python)                       |
|-----------|---------------------------------------------------------------------|
| `ema`     | `clamp((close - EMA)/EMA * 50, -1, +1)` (distance-scaled trend)      |
| `sma`     | `clamp((close - SMA)/SMA * 50, -1, +1)` (distance-scaled trend)      |
| `rsi`     | mean-reversion: `<=30` bullish `min(1,(30-rsi)/30+0.5)`, `>=70` bearish `-min(1,(rsi-70)/30+0.5)`, else `(rsi-50)/50*0.3` |
| `macd`    | `base*(0.5+0.5*strength)`, `base=+1 if hist>0 else -1`, `strength=min(1,|hist|/(|macd|+1e-9))` |
| `adx`     | `direction * clamp((ADX-20)/30, 0, 1)`, `direction=+1 if +DI>-DI else -1` (strength-gated) |
| `atr`     | used for SL/TP distance and lot sizing (not a directional vote)     |

The final blend is `sum(w_i * s_i) / sum(|w_i|)`, clamped to `[-1, +1]`, exactly
as in `Strategy.blended_signal()`.

> **Historical note / why old test results were poor:** earlier versions of this
> EA used hard `+/-1` votes for EMA/SMA/MACD, ignored the ADX strength gate, and
> - most damaging - used a plain `(RSI-50)/50` for RSI, which is the OPPOSITE
> sign of Python's mean-reversion reading in the overbought/oversold zones (it
> bought when Python sold). Those divergences made the tester evaluate a
> different, often inverted strategy than the one the bot actually learned. All
> are fixed now; re-run your test after recompiling.

Indicators the Python strategy uses that the EA does not implement (e.g.
`supertrend`, `bbands`, `candle_patterns`) are **skipped** by the exporter with a
note. The exported blend therefore reflects only the supported subset; the full
blend still runs in the Python live path.

---

## Step 1 - Export the learned strategy (on the Python side)

First build the memory with a search, then export the best strategy to an
EA-readable params file:

```bash
cd main
python main.py --mode search                 # builds data_store/strategy_registry.json
python scripts/export_strategy_for_ea.py     # writes experts/params/<SYMBOL>_<TF>.params
# or a single pair:
python scripts/export_strategy_for_ea.py --symbol EURUSD --timeframe M15
```

Each `.params` file is a simple `key=value` text file, e.g.:

```
symbol=EURUSD
timeframe=M15
long_threshold=0.35
short_threshold=0.35
sl_atr_mult=2.0
tp_atr_mult=3.0
ind.ema.enabled=1
ind.ema.weight=1.0
ind.ema.period=21
ind.rsi.enabled=1
ind.rsi.weight=1.0
ind.rsi.period=14
```

---

## Step 2 - Install the EA and params in the terminal

1. In MT5: **File -> Open Data Folder**. This opens `...\MQL5\`.
2. Copy `experts/Mt5SmartBotEA.mq5` into `MQL5\Experts\`.
3. Copy the generated `experts/params/<SYMBOL>_<TF>.params` file into
   `MQL5\Files\` (that is where the EA looks for it via `FileOpen`).

---

## Step 3 - Compile the EA

1. Open **MetaEditor** (F4 from the terminal, or the toolbar).
2. Open `Experts\Mt5SmartBotEA.mq5`.
3. Press **Compile** (F7). It should compile with 0 errors.

---

## Step 4 - Run in the Strategy Tester

1. In MT5: **View -> Strategy Tester** (Ctrl+R).
2. Select **Expert**: `Mt5SmartBotEA`.
3. Set **Symbol** and **Timeframe** to match the exported params
   (e.g. EURUSD, M15).
4. Choose the modeling mode ("Every tick based on real ticks" is most accurate),
   a date range, and the initial deposit.
5. Open **Inputs** and set:
   - `InpParamsFile` = the file name you copied, e.g. `EURUSD_M15.params`.
     (If left empty, or the file is missing, the EA uses the input parameters
     below it as a fallback.)
   - `InpRiskPerTrade`, `InpMagic`, etc. as desired.
6. Press **Start**.

The EA acts **once per new bar** (matching how the Python bot makes bar-based
decisions), skips a **warmup** of leading bars so every indicator is stable
(mirrors the Python backtester and avoids trading on half-formed RSI/ADX/ATR),
enters when the blended score crosses the long/short threshold, sets ATR-based
SL/TP, sizes the lot so a stop-out loses about `InpRiskPerTrade` of equity, and
closes when the signal flips.

> **Always load a real params file.** For a meaningful test set `InpParamsFile`
> to your exported `<SYMBOL>_<TF>.params`. The built-in input defaults are only
> a self-contained fallback (they match the Python StrategySpec defaults:
> long/short = 0.30, sl = 2.0, tp = 3.0). Note the strategy threshold `0.30` is
> intentionally NOT the decision engine's `0.60` gate: that `0.60` is applied to
> the FULL blend (indicators + ML + news), which the EA does not reproduce - the
> EA validates the indicator/strategy component only.

---

## Parameter precedence

1. `InpParamsFile` values (if the file loads) **override** the EA inputs.
2. If no file is found, the EA uses its **input parameters** as a self-contained
   fallback so it always runs.

---

## Notes and honest limitations

- The EA mirrors the Python **indicator blend** for the supported indicators; it
  is not a byte-for-byte replica of the full Python decision engine (which also
  blends the ML learner and news). Treat it as a faithful validation of the
  **indicator/strategy** component of a learned setup.
- Always test on a **demo** account after a good tester result, before risking
  real capital. Past performance does not guarantee future results.
