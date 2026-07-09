//+------------------------------------------------------------------+
//|                                              Mt5SmartBotEA.mq5     |
//|   MT5 Smart Trading Bot - native Strategy Tester Expert Advisor   |
//|                                                                   |
//|   PURPOSE                                                          |
//|   The Python side (Phase 3) searches many strategies and stores   |
//|   the best one per symbol/timeframe. This Expert Advisor replays   |
//|   that SAME blended-indicator logic inside the native MT5 Strategy |
//|   Tester so you get an authoritative, tick-accurate validation of  |
//|   a learned strategy (spread, swaps, real history) before going    |
//|   live. It is intentionally self-contained and dependency-free.    |
//|                                                                   |
//|   HOW IT GETS THE STRATEGY                                         |
//|   Run on the Python side:                                          |
//|       python scripts/export_strategy_for_ea.py                     |
//|   That writes experts/params/<SYMBOL>_<TF>.params. Copy that file  |
//|   into your terminal's MQL5\Files folder. Set InpParamsFile to its |
//|   name (e.g. "EURUSD_M15.params"). If the file is missing, the EA  |
//|   falls back to the input parameters below.                        |
//|                                                                   |
//|   NOTE: All text is standard ASCII English only.                   |
//+------------------------------------------------------------------+
#property copyright "MT5 Smart Trading Bot"
#property link      ""
#property version   "1.00"
#property strict

#include <Trade\Trade.mqh>

//--- Inputs (used as fallback when no params file is present) ------------------
// IMPORTANT: for a meaningful test, ALWAYS load a real strategy via
// InpParamsFile (exported by scripts/export_strategy_for_ea.py). The values
// below are only a self-contained fallback so the EA still runs with no file.
// They deliberately match the Python StrategySpec defaults (long/short=0.30,
// sl=2.0, tp=3.0) so the fallback behaves like an un-tuned default strategy,
// NOT like the decision engine's 0.60 threshold (that 0.60 gate is applied to
// the FULL blend incl. ML+news, which the EA does not reproduce).
input string InpParamsFile      = "";      // params file in MQL5\Files (optional)
input double InpRiskPerTrade    = 0.01;    // fraction of equity risked per trade
input double InpLongThreshold   = 0.30;    // blended score to go long
input double InpShortThreshold  = 0.30;    // blended score (abs) to go short
input double InpSlAtrMult       = 2.0;     // stop-loss in ATR multiples
input double InpTpAtrMult       = 3.0;     // take-profit in ATR multiples
input int    InpAtrPeriod       = 14;      // ATR period (Python uses 14 fixed)
input long   InpMagic           = 990011;  // magic number to tag orders

//--- Fallback indicator toggles / params (overridden by the params file) ------
input bool   InpUseEma          = true;
input int    InpEmaPeriod       = 21;
input double InpEmaWeight       = 1.0;
input bool   InpUseSma          = true;
input int    InpSmaPeriod       = 50;
input double InpSmaWeight       = 1.0;
input bool   InpUseRsi          = true;
input int    InpRsiPeriod       = 14;
input double InpRsiWeight       = 1.0;
input bool   InpUseMacd         = true;
input int    InpMacdFast        = 12;
input int    InpMacdSlow        = 26;
input int    InpMacdSignal      = 9;
input double InpMacdWeight      = 1.0;
input bool   InpUseAdx          = false;
input int    InpAdxPeriod       = 14;
input double InpAdxWeight       = 1.0;
// --- U2.3: additional EA-native indicators (default OFF; enabled via params) ---
input bool   InpUseSupertrend   = false;   // trend-following (Python "supertrend")
input int    InpStPeriod        = 10;      // ATR period for SuperTrend
input double InpStMultiplier    = 3.0;     // ATR multiplier for SuperTrend
input double InpStWeight        = 1.0;
input bool   InpUseBbands       = false;   // mean-reversion (Python "bbands")
input int    InpBbPeriod        = 20;
input double InpBbStd           = 2.0;     // std multiplier for the bands
input double InpBbWeight        = 1.0;
input bool   InpUseStoch        = false;   // stochastic oscillator (Python "stoch")
input int    InpStochK          = 14;
input int    InpStochD          = 3;
input int    InpStochSmooth     = 3;
input double InpStochWeight      = 1.0;

//--- Runtime strategy configuration (filled from inputs then params file) -----
struct StrategyConfig
  {
   double longThreshold;
   double shortThreshold;
   double slAtrMult;
   double tpAtrMult;
   int    atrPeriod;
   // ema
   bool   useEma;   int emaPeriod;  double emaWeight;
   // sma
   bool   useSma;   int smaPeriod;  double smaWeight;
   // rsi
   bool   useRsi;   int rsiPeriod;  double rsiWeight;
   // macd
   bool   useMacd;  int macdFast; int macdSlow; int macdSignal; double macdWeight;
   // adx
   bool   useAdx;   int adxPeriod;  double adxWeight;
   // supertrend (U2.3)
   bool   useSt;    int stPeriod;   double stMult;   double stWeight;
   // bollinger bands (U2.3)
   bool   useBb;    int bbPeriod;   double bbStd;    double bbWeight;
   // stochastic (U2.3)
   bool   useStoch; int stochK; int stochD; int stochSmooth; double stochWeight;
  };

StrategyConfig g_cfg;
CTrade         g_trade;

//--- Indicator handles --------------------------------------------------------
int g_hEma  = INVALID_HANDLE;
int g_hSma  = INVALID_HANDLE;
int g_hRsi  = INVALID_HANDLE;
int g_hMacd = INVALID_HANDLE;
int g_hAdx  = INVALID_HANDLE;
int g_hAtr  = INVALID_HANDLE;
int g_hStAtr = INVALID_HANDLE;  // dedicated ATR handle for SuperTrend period
int g_hBb   = INVALID_HANDLE;
int g_hStoch = INVALID_HANDLE;

datetime g_lastBarTime = 0;

//+------------------------------------------------------------------+
//| Load defaults from inputs                                        |
//+------------------------------------------------------------------+
void LoadDefaults()
  {
   g_cfg.longThreshold  = InpLongThreshold;
   g_cfg.shortThreshold = InpShortThreshold;
   g_cfg.slAtrMult      = InpSlAtrMult;
   g_cfg.tpAtrMult      = InpTpAtrMult;
   g_cfg.atrPeriod      = InpAtrPeriod;

   g_cfg.useEma  = InpUseEma;  g_cfg.emaPeriod = InpEmaPeriod;  g_cfg.emaWeight = InpEmaWeight;
   g_cfg.useSma  = InpUseSma;  g_cfg.smaPeriod = InpSmaPeriod;  g_cfg.smaWeight = InpSmaWeight;
   g_cfg.useRsi  = InpUseRsi;  g_cfg.rsiPeriod = InpRsiPeriod;  g_cfg.rsiWeight = InpRsiWeight;
   g_cfg.useMacd = InpUseMacd; g_cfg.macdFast  = InpMacdFast;
   g_cfg.macdSlow = InpMacdSlow; g_cfg.macdSignal = InpMacdSignal; g_cfg.macdWeight = InpMacdWeight;
   g_cfg.useAdx  = InpUseAdx;  g_cfg.adxPeriod = InpAdxPeriod;  g_cfg.adxWeight = InpAdxWeight;
   // U2.3 additions
   g_cfg.useSt = InpUseSupertrend; g_cfg.stPeriod = InpStPeriod;
   g_cfg.stMult = InpStMultiplier; g_cfg.stWeight = InpStWeight;
   g_cfg.useBb = InpUseBbands; g_cfg.bbPeriod = InpBbPeriod;
   g_cfg.bbStd = InpBbStd; g_cfg.bbWeight = InpBbWeight;
   g_cfg.useStoch = InpUseStoch; g_cfg.stochK = InpStochK;
   g_cfg.stochD = InpStochD; g_cfg.stochSmooth = InpStochSmooth;
   g_cfg.stochWeight = InpStochWeight;
  }

//+------------------------------------------------------------------+
//| Trim helper                                                     |
//+------------------------------------------------------------------+
string TrimStr(string s)
  {
   StringTrimLeft(s);
   StringTrimRight(s);
   return s;
  }

//+------------------------------------------------------------------+
//| Apply a single key=value pair from the params file              |
//+------------------------------------------------------------------+
void ApplyParam(const string key, const string value)
  {
   double d = StringToDouble(value);
   int    i = (int)StringToInteger(value);

   if(key=="long_threshold")       g_cfg.longThreshold  = d;
   else if(key=="short_threshold") g_cfg.shortThreshold = d;
   else if(key=="sl_atr_mult")     g_cfg.slAtrMult      = d;
   else if(key=="tp_atr_mult")     g_cfg.tpAtrMult      = d;
   // EMA
   else if(key=="ind.ema.enabled") g_cfg.useEma  = (i!=0);
   else if(key=="ind.ema.period")  g_cfg.emaPeriod = i;
   else if(key=="ind.ema.weight")  g_cfg.emaWeight = d;
   // SMA
   else if(key=="ind.sma.enabled") g_cfg.useSma  = (i!=0);
   else if(key=="ind.sma.period")  g_cfg.smaPeriod = i;
   else if(key=="ind.sma.weight")  g_cfg.smaWeight = d;
   // RSI
   else if(key=="ind.rsi.enabled") g_cfg.useRsi  = (i!=0);
   else if(key=="ind.rsi.period")  g_cfg.rsiPeriod = i;
   else if(key=="ind.rsi.weight")  g_cfg.rsiWeight = d;
   // MACD
   else if(key=="ind.macd.enabled") g_cfg.useMacd = (i!=0);
   else if(key=="ind.macd.fast")    g_cfg.macdFast = i;
   else if(key=="ind.macd.slow")    g_cfg.macdSlow = i;
   else if(key=="ind.macd.signal")  g_cfg.macdSignal = i;
   else if(key=="ind.macd.weight")  g_cfg.macdWeight = d;
   // ADX
   else if(key=="ind.adx.enabled")  g_cfg.useAdx = (i!=0);
   else if(key=="ind.adx.period")   g_cfg.adxPeriod = i;
   else if(key=="ind.adx.weight")   g_cfg.adxWeight = d;
   // SUPERTREND (U2.3)
   else if(key=="ind.supertrend.enabled")    g_cfg.useSt = (i!=0);
   else if(key=="ind.supertrend.period")     g_cfg.stPeriod = i;
   else if(key=="ind.supertrend.multiplier") g_cfg.stMult = d;
   else if(key=="ind.supertrend.weight")     g_cfg.stWeight = d;
   // BOLLINGER BANDS (U2.3)
   else if(key=="ind.bbands.enabled") g_cfg.useBb = (i!=0);
   else if(key=="ind.bbands.period")  g_cfg.bbPeriod = i;
   else if(key=="ind.bbands.std")     g_cfg.bbStd = d;
   else if(key=="ind.bbands.weight")  g_cfg.bbWeight = d;
   // STOCHASTIC (U2.3)
   else if(key=="ind.stoch.enabled") g_cfg.useStoch = (i!=0);
   else if(key=="ind.stoch.k")       g_cfg.stochK = i;
   else if(key=="ind.stoch.d")       g_cfg.stochD = i;
   else if(key=="ind.stoch.smooth")  g_cfg.stochSmooth = i;
   else if(key=="ind.stoch.weight")  g_cfg.stochWeight = d;
   // symbol/timeframe lines are informational; ignore silently.
  }

//+------------------------------------------------------------------+
//| Load params from a file in MQL5\Files (returns true if loaded)   |
//+------------------------------------------------------------------+
bool LoadParamsFile(const string fname)
  {
   if(fname=="")
      return(false);
   int h = FileOpen(fname, FILE_READ|FILE_TXT|FILE_ANSI);
   if(h==INVALID_HANDLE)
     {
      PrintFormat("Params file '%s' not found; using EA input parameters.", fname);
      return(false);
     }
   int applied = 0;
   while(!FileIsEnding(h))
     {
      string line = TrimStr(FileReadString(h));
      if(StringLen(line)==0 || StringGetCharacter(line,0)=='#')
         continue;
      int pos = StringFind(line, "=");
      if(pos<=0)
         continue;
      string key = TrimStr(StringSubstr(line, 0, pos));
      string val = TrimStr(StringSubstr(line, pos+1));
      ApplyParam(key, val);
      applied++;
     }
   FileClose(h);
   PrintFormat("Loaded %d parameter(s) from '%s'.", applied, fname);
   return(applied>0);
  }

//+------------------------------------------------------------------+
//| Create indicator handles based on the active config             |
//+------------------------------------------------------------------+
bool CreateHandles()
  {
   g_hAtr = iATR(_Symbol, _Period, g_cfg.atrPeriod);
   if(g_cfg.useEma)  g_hEma  = iMA(_Symbol, _Period, g_cfg.emaPeriod, 0, MODE_EMA, PRICE_CLOSE);
   if(g_cfg.useSma)  g_hSma  = iMA(_Symbol, _Period, g_cfg.smaPeriod, 0, MODE_SMA, PRICE_CLOSE);
   if(g_cfg.useRsi)  g_hRsi  = iRSI(_Symbol, _Period, g_cfg.rsiPeriod, PRICE_CLOSE);
   if(g_cfg.useMacd) g_hMacd = iMACD(_Symbol, _Period, g_cfg.macdFast, g_cfg.macdSlow, g_cfg.macdSignal, PRICE_CLOSE);
   if(g_cfg.useAdx)  g_hAdx  = iADX(_Symbol, _Period, g_cfg.adxPeriod);
   // U2.3: SuperTrend needs its own Wilder-ATR (its own period), matching the
   // Python SuperTrend which smooths true-range with its own `period`, not the
   // fixed 14-bar ATR used for SL/TP sizing.
   if(g_cfg.useSt)   g_hStAtr = iATR(_Symbol, _Period, g_cfg.stPeriod);
   // U2.3: Bollinger Bands. MT5 iBands uses population std (divide by N), which
   // matches the Python _rolling_std (var = sum/period). Applied price = close.
   if(g_cfg.useBb)   g_hBb   = iBands(_Symbol, _Period, g_cfg.bbPeriod, 0,
                                      g_cfg.bbStd, PRICE_CLOSE);
   // U2.3: Stochastic. MT5 iStochastic(k, d, smooth) with SMA smoothing and
   // low/high price fields matches the Python Stochastic (SMA-smoothed %K, %D).
   if(g_cfg.useStoch) g_hStoch = iStochastic(_Symbol, _Period, g_cfg.stochK,
                                             g_cfg.stochD, g_cfg.stochSmooth,
                                             MODE_SMA, STO_LOWHIGH);
   if(g_hAtr==INVALID_HANDLE)
     {
      Print("Failed to create ATR handle.");
      return(false);
     }
   return(true);
  }

//+------------------------------------------------------------------+
//| Read a single buffer value at shift                             |
//+------------------------------------------------------------------+
double BufVal(int handle, int buffer, int shift)
  {
   if(handle==INVALID_HANDLE)
      return(0.0);
   double tmp[];
   if(CopyBuffer(handle, buffer, shift, 1, tmp)!=1)
      return(0.0);
   return(tmp[0]);
  }

//+------------------------------------------------------------------+
//| Clamp a value into [-1, +1] (matches Python max(-1,min(1,x))).    |
//+------------------------------------------------------------------+
double Clamp1(double v)
  {
   if(v >  1.0) return( 1.0);
   if(v < -1.0) return(-1.0);
   return(v);
  }

//+------------------------------------------------------------------+
//| SuperTrend direction at the last CLOSED bar (shift=1).           |
//| Mirrors core/indicators/trend.py SuperTrend EXACTLY:            |
//|   hl2   = (high+low)/2                                          |
//|   upper = hl2 + mult*atr ; lower = hl2 - mult*atr              |
//|   seed: prev_st=lower, prev_dir=1 at the first valid ATR bar    |
//|   dir = +1 if close>prev_st ; -1 if close<prev_st ; else prev   |
//|   st  = lower if dir==1 else upper                              |
//| ATR is Wilder-smoothed true range (MT5 iATR == Wilder), so the  |
//| stateful recursion must be replayed forward from the first bar   |
//| where ATR is defined. Returns +1 / -1 (0 if not enough data).   |
//| signal_at() in Python returns exactly 1.0/-1.0 from direction.   |
//+------------------------------------------------------------------+
double SuperTrendDir()
  {
   int need = g_cfg.stPeriod + 3;                 // ATR warmup head-room
   int bars = Bars(_Symbol, _Period);
   if(bars < need + 2 || g_hStAtr==INVALID_HANDLE)
      return(0.0);

   // Replay from an oldest usable shift down to shift=1 (the closed bar).
   // Cap the replay window so OnTick stays cheap on huge histories; the
   // SuperTrend recursion converges quickly, and a few hundred bars of
   // warmup reproduces the steady-state direction to full parity.
   int startShift = bars - 2;                      // oldest fully-closed bar
   int maxWindow  = g_cfg.stPeriod * 20 + 200;
   if(startShift > maxWindow)
      startShift = maxWindow;

   double prev_st = 0.0;
   int    prev_dir = 1;
   bool   seeded = false;
   int    cur_dir = 0;

   for(int sh = startShift; sh >= 1; sh--)
     {
      double atr = BufVal(g_hStAtr, 0, sh);
      if(atr <= 0.0)
         continue;                                 // ATR not defined yet
      double hi = iHigh(_Symbol, _Period, sh);
      double lo = iLow(_Symbol, _Period, sh);
      double cl = iClose(_Symbol, _Period, sh);
      double hl2 = (hi + lo) / 2.0;
      double upper = hl2 + g_cfg.stMult * atr;
      double lower = hl2 - g_cfg.stMult * atr;
      if(!seeded)
        {
         prev_st = lower;                           // Python seed: prev_st=lower
         prev_dir = 1;                              //              prev_dir=1
         seeded = true;
        }
      if(cl > prev_st)       cur_dir = 1;
      else if(cl < prev_st)  cur_dir = -1;
      else                   cur_dir = prev_dir;
      double cur_st = (cur_dir==1) ? lower : upper;
      prev_st = cur_st;
      prev_dir = cur_dir;
     }
   if(cur_dir==0)
      return(0.0);
   return(cur_dir > 0 ? 1.0 : -1.0);
  }

//+------------------------------------------------------------------+
//| Compute the blended [-1,+1] signal at the last CLOSED bar        |
//| Mirrors the Python decision blend for the supported indicators.  |
//|                                                                  |
//| IMPORTANT: this must reproduce the SAME per-indicator signal      |
//| mapping and the SAME weighted-blend math as the Python side       |
//| (core/indicators/*.py and core/strategy/strategy.py). Any         |
//| divergence makes the tester result meaningless. The Python blend  |
//| is: acc = sum(w_i * s_i); total_w = sum(|w_i|); blend = acc/total_w|
//| clamped to [-1,+1]. Each s_i is CONTINUOUS, not just +/-1.        |
//+------------------------------------------------------------------+
double BlendedSignal()
  {
   int shift = 1; // last fully closed bar
   double close1 = iClose(_Symbol, _Period, shift);

   double weighted = 0.0;   // acc  = sum(w * s)
   double wsum     = 0.0;   // total_w = sum(|w|)   (Python uses abs(weight))

   // ---------------------------------------------------------------- //
   // EMA / SMA (core/indicators/trend.py _signal_at):                  //
   //   diff = (close - ma) / ma;  signal = clamp(diff * 50.0, -1, +1)  //
   // A CONTINUOUS distance-scaled signal, NOT a hard +/-1 sign. Using  //
   // +/-1 (the old EA behavior) made the EA enter far too aggressively //
   // because a price barely above the MA already voted a full +1.      //
   // ---------------------------------------------------------------- //
   if(g_cfg.useEma)
     {
      double ema = BufVal(g_hEma, 0, shift);
      double s = 0.0;
      if(ema>0.0) s = Clamp1(((close1-ema)/ema) * 50.0);
      weighted += g_cfg.emaWeight * s;
      wsum     += MathAbs(g_cfg.emaWeight);
     }
   if(g_cfg.useSma)
     {
      double sma = BufVal(g_hSma, 0, shift);
      double s = 0.0;
      if(sma>0.0) s = Clamp1(((close1-sma)/sma) * 50.0);
      weighted += g_cfg.smaWeight * s;
      wsum     += MathAbs(g_cfg.smaWeight);
     }
   // ---------------------------------------------------------------- //
   // RSI (core/indicators/momentum.py RSI._signal_at) - MEAN REVERSION://
   //   if rsi <= 30: signal = min(1, (30 - rsi)/30 + 0.5)   (BULLISH)  //
   //   if rsi >= 70: signal = -min(1, (rsi - 70)/30 + 0.5)  (BEARISH)  //
   //   else:         signal = (rsi - 50)/50 * 0.3   (mild trend bias)  //
   // CRITICAL FIX: the old EA used a plain (rsi-50)/50 which is the     //
   // OPPOSITE sign in the overbought/oversold zones (it went LONG when  //
   // Python went SHORT and vice versa). That single sign inversion, on  //
   // a strongly-weighted RSI strategy, is enough to flip a winning      //
   // recipe into a losing one in the tester.                            //
   // ---------------------------------------------------------------- //
   if(g_cfg.useRsi)
     {
      double rsi = BufVal(g_hRsi, 0, shift);
      double s = 0.0;
      if(rsi <= 30.0)
        {
         s = (30.0 - rsi)/30.0 + 0.5;
         if(s > 1.0) s = 1.0;
        }
      else if(rsi >= 70.0)
        {
         double m = (rsi - 70.0)/30.0 + 0.5;
         if(m > 1.0) m = 1.0;
         s = -m;
        }
      else
        {
         s = (rsi - 50.0)/50.0 * 0.3;
        }
      weighted += g_cfg.rsiWeight * s;
      wsum     += MathAbs(g_cfg.rsiWeight);
     }
   // ---------------------------------------------------------------- //
   // MACD (core/indicators/trend.py _signal_at):                       //
   //   base = +1 if hist>0 else -1                                     //
   //   strength = min(1, |hist| / (|macd| + 1e-9))                     //
   //   signal = base * (0.5 + 0.5*strength)   -> magnitude in [0.5,1]  //
   // The old EA used a hard +/-1, over-weighting MACD vs Python.       //
   // ---------------------------------------------------------------- //
   if(g_cfg.useMacd)
     {
      double macdMain = BufVal(g_hMacd, 0, shift);
      double macdSig  = BufVal(g_hMacd, 1, shift);
      double hist = macdMain - macdSig;
      // Python: base = 1.0 if hist>0 else -1.0  (hist==0 -> base=-1, matched).
      double base = (hist>0.0) ? 1.0 : -1.0;
      double denom = MathAbs(macdMain) + 1e-9;
      double strength = MathAbs(hist) / denom;
      if(strength > 1.0) strength = 1.0;
      double s = base * (0.5 + 0.5*strength);
      weighted += g_cfg.macdWeight * s;
      wsum     += MathAbs(g_cfg.macdWeight);
     }
   // ---------------------------------------------------------------- //
   // ADX (core/indicators/trend.py _signal_at):                        //
   //   direction = +1 if +DI > -DI else -1                             //
   //   strength  = clamp((ADX - 20)/30, 0, 1)   <- STRENGTH GATE       //
   //   signal    = direction * strength                                //
   // The old EA ignored the ADX strength gate and voted a full +/-1    //
   // on every bar, so a flat/rangebound market (low ADX, where Python  //
   // votes ~0) got a full-strength directional vote here. That single  //
   // bug alone can wreck results when ADX is enabled.                  //
   // ---------------------------------------------------------------- //
   if(g_cfg.useAdx)
     {
      double adx     = BufVal(g_hAdx, 0, shift); // main ADX buffer
      double diPlus  = BufVal(g_hAdx, 1, shift);
      double diMinus = BufVal(g_hAdx, 2, shift);
      double direction = (diPlus>diMinus) ? 1.0 : -1.0;
      double strength = (adx - 20.0) / 30.0;
      if(strength < 0.0) strength = 0.0;
      if(strength > 1.0) strength = 1.0;
      double s = direction * strength;
      weighted += g_cfg.adxWeight * s;
      wsum     += MathAbs(g_cfg.adxWeight);
     }
   // ---------------------------------------------------------------- //
   // SuperTrend (core/indicators/trend.py SuperTrend._signal_at):      //
   //   signal = +1.0 if direction==+1 else -1.0  (a hard trend vote).  //
   // SuperTrendDir() replays the exact stateful recursion and returns  //
   // +1.0 / -1.0 (0.0 when there is not enough data -> no vote, which  //
   // matches Python returning 0.0 before the indicator is defined).    //
   // ---------------------------------------------------------------- //
   if(g_cfg.useSt)
     {
      double s = SuperTrendDir();
      if(s != 0.0)
        {
         weighted += g_cfg.stWeight * s;
         wsum     += MathAbs(g_cfg.stWeight);
        }
     }
   // ---------------------------------------------------------------- //
   // Bollinger Bands (core/indicators/volatility.py BollingerBands.    //
   // _signal_at) - MEAN REVERSION:                                     //
   //   if price <= lower: +0.7                                         //
   //   if price >= upper: -0.7                                         //
   //   else: position = (price-mid)/(band/2); s = -clamp(position)*0.3 //
   // MT5 iBands buffers: 0=base(middle), 1=upper, 2=lower. iBands uses //
   // population std (divide by N), matching Python _rolling_std.       //
   // ---------------------------------------------------------------- //
   if(g_cfg.useBb)
     {
      double mid   = BufVal(g_hBb, 0, shift);
      double upper = BufVal(g_hBb, 1, shift);
      double lower = BufVal(g_hBb, 2, shift);
      if(upper != 0.0 || lower != 0.0 || mid != 0.0)
        {
         double s = 0.0;
         if(close1 <= lower)
            s = 0.7;
         else if(close1 >= upper)
            s = -0.7;
         else
           {
            double band = upper - lower;
            if(band != 0.0)
              {
               double position = (close1 - mid) / (band / 2.0);
               s = -Clamp1(position) * 0.3;
              }
           }
         weighted += g_cfg.bbWeight * s;
         wsum     += MathAbs(g_cfg.bbWeight);
        }
     }
   // ---------------------------------------------------------------- //
   // Stochastic (core/indicators/momentum.py Stochastic._signal_at):   //
   //   base = +0.7 if %K < 20 ; -0.7 if %K > 80 ; else 0              //
   //   cross = +0.3 if %K > %D else -0.3                               //
   //   signal = clamp(base + cross, -1, +1)                            //
   // MT5 iStochastic buffers: 0=MAIN(%K), 1=SIGNAL(%D). Built with     //
   // MODE_SMA + STO_LOWHIGH to match Python's SMA-smoothed %K over the //
   // low/high range.                                                   //
   // ---------------------------------------------------------------- //
   if(g_cfg.useStoch)
     {
      double kv = BufVal(g_hStoch, 0, shift);   // %K (MAIN)
      double dv = BufVal(g_hStoch, 1, shift);   // %D (SIGNAL)
      double base = 0.0;
      if(kv < 20.0)      base = 0.7;
      else if(kv > 80.0) base = -0.7;
      double cross = (kv > dv) ? 0.3 : -0.3;
      double s = Clamp1(base + cross);
      weighted += g_cfg.stochWeight * s;
      wsum     += MathAbs(g_cfg.stochWeight);
     }

   if(wsum<=0.0)
      return(0.0);
   return(Clamp1(weighted/wsum));
  }

//+------------------------------------------------------------------+
//| Position sizing so hitting the stop loses ~risk*equity           |
//+------------------------------------------------------------------+
double ComputeLot(double stopDistancePrice)
  {
   double equity   = AccountInfoDouble(ACCOUNT_EQUITY);
   double riskCash = equity * InpRiskPerTrade;

   double tickValue = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   double tickSize  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tickSize<=0.0 || tickValue<=0.0 || stopDistancePrice<=0.0)
      return(SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN));

   double ticks   = stopDistancePrice / tickSize;
   double lossPerLot = ticks * tickValue;
   if(lossPerLot<=0.0)
      return(SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN));

   double lot = riskCash / lossPerLot;

   double minLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double step   = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   if(step>0.0)
      lot = MathFloor(lot/step)*step;
   if(lot<minLot) lot = minLot;
   if(lot>maxLot) lot = maxLot;
   return(lot);
  }

//+------------------------------------------------------------------+
//| Are we already in a position tagged by our magic number?         |
//+------------------------------------------------------------------+
bool HasOpenPosition(int &dir)
  {
   dir = 0;
   if(!PositionSelect(_Symbol))
      return(false);
   if(PositionGetInteger(POSITION_MAGIC)!=InpMagic)
      return(false);
   long ptype = PositionGetInteger(POSITION_TYPE);
   dir = (ptype==POSITION_TYPE_BUY) ? 1 : -1;
   return(true);
  }

//+------------------------------------------------------------------+
//| Expert initialization                                            |
//+------------------------------------------------------------------+
int OnInit()
  {
   LoadDefaults();
   LoadParamsFile(InpParamsFile);   // overrides defaults if file exists
   if(!CreateHandles())
      return(INIT_FAILED);
   g_trade.SetExpertMagicNumber(InpMagic);
   g_trade.SetDeviationInPoints(20);
   PrintFormat("Mt5SmartBotEA init on %s %s. long=%.2f short=%.2f sl=%.2f tp=%.2f",
               _Symbol, EnumToString(_Period), g_cfg.longThreshold,
               g_cfg.shortThreshold, g_cfg.slAtrMult, g_cfg.tpAtrMult);
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
//| Expert deinitialization                                          |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   if(g_hEma !=INVALID_HANDLE) IndicatorRelease(g_hEma);
   if(g_hSma !=INVALID_HANDLE) IndicatorRelease(g_hSma);
   if(g_hRsi !=INVALID_HANDLE) IndicatorRelease(g_hRsi);
   if(g_hMacd!=INVALID_HANDLE) IndicatorRelease(g_hMacd);
   if(g_hAdx !=INVALID_HANDLE) IndicatorRelease(g_hAdx);
   if(g_hAtr !=INVALID_HANDLE) IndicatorRelease(g_hAtr);
  }

//+------------------------------------------------------------------+
//| Number of leading bars to skip so every indicator is stable.     |
//| Mirrors the Python backtester's warmup (default 60). Without this |
//| the EA could trade on half-formed indicators: e.g. before RSI has |
//| enough data BufVal() returns 0.0, which the RSI rule reads as     |
//| "deeply oversold" and votes a full BUY - a pure warmup artifact.  |
//+------------------------------------------------------------------+
int WarmupBars()
  {
   int w = g_cfg.atrPeriod;
   if(g_cfg.useEma  && g_cfg.emaPeriod  > w) w = g_cfg.emaPeriod;
   if(g_cfg.useSma  && g_cfg.smaPeriod  > w) w = g_cfg.smaPeriod;
   if(g_cfg.useRsi  && g_cfg.rsiPeriod  > w) w = g_cfg.rsiPeriod;
   if(g_cfg.useMacd && g_cfg.macdSlow   > w) w = g_cfg.macdSlow;
   if(g_cfg.useAdx  && g_cfg.adxPeriod  > w) w = g_cfg.adxPeriod;
   // Add generous head-room for Wilder-smoothed lines (RSI/ADX/ATR) to settle,
   // and never go below the Python default of 60 leading bars.
   w = w * 3 + 10;
   if(w < 60) w = 60;
   return(w);
  }

//+------------------------------------------------------------------+
//| Main loop - act once per new bar (matches Python bar decisions)  |
//+------------------------------------------------------------------+
void OnTick()
  {
   datetime barTime = iTime(_Symbol, _Period, 0);
   if(barTime==g_lastBarTime)
      return;             // only act on a fresh bar
   g_lastBarTime = barTime;

   // Skip until enough history exists for every enabled indicator to be
   // stable (matches the Python backtester warmup and avoids fake signals).
   if(Bars(_Symbol, _Period) < WarmupBars())
      return;

   double atr = BufVal(g_hAtr, 0, 1);
   if(atr<=0.0)
      return;

   double score = BlendedSignal();

   int curDir = 0;
   bool inPos = HasOpenPosition(curDir);

   // Exit rule: close if the signal flips against the open position.
   if(inPos)
     {
      if(curDir==1 && score <= -g_cfg.shortThreshold)
         g_trade.PositionClose(_Symbol);
      else if(curDir==-1 && score >= g_cfg.longThreshold)
         g_trade.PositionClose(_Symbol);
      // Re-select in case we just closed.
      inPos = HasOpenPosition(curDir);
     }

   if(inPos)
      return;   // one position at a time

   // Entry rules.
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);

   if(score >= g_cfg.longThreshold)
     {
      double sl = ask - g_cfg.slAtrMult*atr;
      double tp = ask + g_cfg.tpAtrMult*atr;
      double lot = ComputeLot(ask - sl);
      g_trade.Buy(lot, _Symbol, ask, sl, tp, "SmartBot long");
     }
   else if(score <= -g_cfg.shortThreshold)
     {
      double sl = bid + g_cfg.slAtrMult*atr;
      double tp = bid - g_cfg.tpAtrMult*atr;
      double lot = ComputeLot(sl - bid);
      g_trade.Sell(lot, _Symbol, bid, sl, tp, "SmartBot short");
     }
  }
//+------------------------------------------------------------------+
