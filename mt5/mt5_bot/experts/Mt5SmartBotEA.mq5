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
input string InpParamsFile      = "";      // params file in MQL5\Files (optional)
input double InpRiskPerTrade    = 0.01;    // fraction of equity risked per trade
input double InpLongThreshold   = 0.30;    // blended score to go long
input double InpShortThreshold  = 0.30;    // blended score (abs) to go short
input double InpSlAtrMult       = 2.0;     // stop-loss in ATR multiples
input double InpTpAtrMult       = 3.0;     // take-profit in ATR multiples
input int    InpAtrPeriod       = 14;      // ATR period for SL/TP + labels
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
//| Compute the blended [-1,+1] signal at the last CLOSED bar        |
//| Mirrors the Python decision blend for the supported indicators.  |
//+------------------------------------------------------------------+
double BlendedSignal()
  {
   int shift = 1; // last fully closed bar
   double close1 = iClose(_Symbol, _Period, shift);

   double weighted = 0.0;
   double wsum     = 0.0;

   // EMA / SMA: sign of price vs moving average (trend follow).
   if(g_cfg.useEma)
     {
      double ema = BufVal(g_hEma, 0, shift);
      double s = 0.0;
      if(ema>0.0) s = (close1>ema) ? 1.0 : ((close1<ema) ? -1.0 : 0.0);
      weighted += g_cfg.emaWeight * s;
      wsum     += g_cfg.emaWeight;
     }
   if(g_cfg.useSma)
     {
      double sma = BufVal(g_hSma, 0, shift);
      double s = 0.0;
      if(sma>0.0) s = (close1>sma) ? 1.0 : ((close1<sma) ? -1.0 : 0.0);
      weighted += g_cfg.smaWeight * s;
      wsum     += g_cfg.smaWeight;
     }
   // RSI: map 0..100 around 50 into [-1,+1] (>70 strong long, <30 strong short).
   if(g_cfg.useRsi)
     {
      double rsi = BufVal(g_hRsi, 0, shift);
      double s = (rsi-50.0)/50.0;
      if(s> 1.0) s= 1.0;
      if(s<-1.0) s=-1.0;
      weighted += g_cfg.rsiWeight * s;
      wsum     += g_cfg.rsiWeight;
     }
   // MACD: sign of (main - signal) histogram.
   if(g_cfg.useMacd)
     {
      double macdMain = BufVal(g_hMacd, 0, shift);
      double macdSig  = BufVal(g_hMacd, 1, shift);
      double hist = macdMain - macdSig;
      double s = (hist>0.0) ? 1.0 : ((hist<0.0) ? -1.0 : 0.0);
      weighted += g_cfg.macdWeight * s;
      wsum     += g_cfg.macdWeight;
     }
   // ADX: strength gate turned into a mild trend-confirmation in DI direction.
   if(g_cfg.useAdx)
     {
      double diPlus  = BufVal(g_hAdx, 1, shift);
      double diMinus = BufVal(g_hAdx, 2, shift);
      double s = (diPlus>diMinus) ? 1.0 : ((diPlus<diMinus) ? -1.0 : 0.0);
      weighted += g_cfg.adxWeight * s;
      wsum     += g_cfg.adxWeight;
     }

   if(wsum<=0.0)
      return(0.0);
   return(weighted/wsum);
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
//| Main loop - act once per new bar (matches Python bar decisions)  |
//+------------------------------------------------------------------+
void OnTick()
  {
   datetime barTime = iTime(_Symbol, _Period, 0);
   if(barTime==g_lastBarTime)
      return;             // only act on a fresh bar
   g_lastBarTime = barTime;

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
