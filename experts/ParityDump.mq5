//+------------------------------------------------------------------+
//|  ParityDump.mq5 - Python-vs-EA signal parity harness (U2.6)       |
//|                                                                  |
//|  Reads tests/fixtures/parity_ohlcv.csv (written by                |
//|  scripts/parity_fixture.py), recomputes the EA's BlendedSignal()  |
//|  per bar using the SAME formulas as Mt5SmartBotEA.mq5, and writes  |
//|  tests/fixtures/parity_ea.csv. A Python test then asserts          |
//|  max|python - ea| < 1e-6 so the two implementations can never      |
//|  silently drift apart (diagnosis D1).                             |
//|                                                                  |
//|  IMPORTANT: This is a SELF-CONTAINED reference. It does NOT use    |
//|  iMA/iRSI/iMACD/iADX handles; it computes the indicators directly  |
//|  from the CSV bars so the result depends ONLY on the shared bars   |
//|  and matches Python's pure-Python indicator math exactly.         |
//|                                                                  |
//|  HOW TO RUN                                                        |
//|  1. python scripts/parity_fixture.py   (creates the input CSV)     |
//|  2. Copy tests/fixtures/parity_ohlcv.csv into MT5's                |
//|     <Data Folder>/MQL5/Files/ (File > Open Data Folder).           |
//|  3. Attach/compile this script; it writes parity_ea.csv into the   |
//|     same MQL5/Files/ folder. Copy it back to tests/fixtures/.      |
//|  4. python -m pytest tests/test_parity_harness.py                  |
//|                                                                  |
//|  All text is standard ASCII English only.                         |
//+------------------------------------------------------------------+
#property script_show_inputs
#property strict

input string InpInFile  = "parity_ohlcv.csv"; // input bars (MQL5/Files)
input string InpOutFile = "parity_ea.csv";    // output signal (MQL5/Files)

// --- The parity spec (MUST match PARITY_SPEC in scripts/parity_fixture.py). --
input int    InpEmaPeriod  = 20;
input double InpEmaWeight   = 1.0;
input int    InpRsiPeriod  = 14;
input double InpRsiWeight   = 2.0;
input int    InpMacdFast   = 12;
input int    InpMacdSlow   = 26;
input int    InpMacdSignal = 9;
input double InpMacdWeight  = 1.5;
input int    InpAdxPeriod  = 14;
input double InpAdxWeight   = 1.0;

//--- Loaded bars.
double g_open[];
double g_high[];
double g_low[];
double g_close[];
int    g_n = 0;

//------------------------------------------------------------------
double Clamp1(double v)
  {
   if(v >  1.0) return(1.0);
   if(v < -1.0) return(-1.0);
   return(v);
  }

//------------------------------------------------------------------
// Read the OHLCV CSV (header: time,open,high,low,close,volume).
//------------------------------------------------------------------
bool LoadBars(const string fname)
  {
   int h = FileOpen(fname, FILE_READ|FILE_ANSI|FILE_TXT);
   if(h == INVALID_HANDLE)
     {
      PrintFormat("ParityDump: cannot open %s (err %d). Put it in MQL5/Files.",
                  fname, GetLastError());
      return(false);
     }
   ArrayResize(g_open, 0);
   ArrayResize(g_high, 0);
   ArrayResize(g_low, 0);
   ArrayResize(g_close, 0);
   g_n = 0;
   bool header = true;
   while(!FileIsEnding(h))
     {
      string line = FileReadString(h);
      if(line == "")
         continue;
      if(header) { header = false; continue; } // skip header row
      string parts[];
      int k = StringSplit(line, ',', parts);
      if(k < 5)
         continue;
      int idx = g_n;
      ArrayResize(g_open,  idx+1);
      ArrayResize(g_high,  idx+1);
      ArrayResize(g_low,   idx+1);
      ArrayResize(g_close, idx+1);
      g_open[idx]  = StringToDouble(parts[1]);
      g_high[idx]  = StringToDouble(parts[2]);
      g_low[idx]   = StringToDouble(parts[3]);
      g_close[idx] = StringToDouble(parts[4]);
      g_n++;
     }
   FileClose(h);
   PrintFormat("ParityDump: loaded %d bars from %s", g_n, fname);
   return(g_n > 0);
  }

//------------------------------------------------------------------
// EMA series over close (Wilder-style seed = SMA of first `period`).
// Mirrors core/indicators/trend.py EMA.compute (span EMA, adjust=False).
//------------------------------------------------------------------
void EmaSeries(int period, double &out[])
  {
   ArrayResize(out, g_n);
   ArrayInitialize(out, 0.0);
   if(g_n == 0 || period <= 0)
      return;
   double alpha = 2.0 / (period + 1.0);
   // Python pandas ewm(span, adjust=False) seeds with the first value.
   double ema = g_close[0];
   out[0] = ema;
   for(int i=1; i<g_n; i++)
     {
      ema = alpha * g_close[i] + (1.0 - alpha) * ema;
      out[i] = ema;
     }
  }

//------------------------------------------------------------------
// RSI (Wilder smoothing) - core/indicators/momentum.py RSI.
//------------------------------------------------------------------
void RsiSeries(int period, double &out[])
  {
   ArrayResize(out, g_n);
   ArrayInitialize(out, 50.0);
   if(g_n <= period || period <= 0)
      return;
   double gain = 0.0, loss = 0.0;
   for(int i=1; i<=period; i++)
     {
      double d = g_close[i] - g_close[i-1];
      if(d >= 0) gain += d; else loss += -d;
     }
   double avgGain = gain / period;
   double avgLoss = loss / period;
   for(int i=period+1; i<g_n; i++)
     {
      double d = g_close[i] - g_close[i-1];
      double g = (d > 0) ? d : 0.0;
      double l = (d < 0) ? -d : 0.0;
      avgGain = (avgGain * (period-1) + g) / period;
      avgLoss = (avgLoss * (period-1) + l) / period;
      double rs = (avgLoss == 0.0) ? 100.0 : avgGain / avgLoss;
      out[i] = 100.0 - (100.0 / (1.0 + rs));
     }
  }

//------------------------------------------------------------------
// MACD hist and main - core/indicators/trend.py MACD.
//------------------------------------------------------------------
void MacdSeries(int fast, int slow, int sig, double &mainOut[], double &histOut[])
  {
   ArrayResize(mainOut, g_n);
   ArrayResize(histOut, g_n);
   ArrayInitialize(mainOut, 0.0);
   ArrayInitialize(histOut, 0.0);
   if(g_n == 0)
      return;
   double emaF[], emaS[];
   EmaSeries(fast, emaF);
   EmaSeries(slow, emaS);
   double macd[];
   ArrayResize(macd, g_n);
   for(int i=0; i<g_n; i++)
      macd[i] = emaF[i] - emaS[i];
   // signal line = EMA(macd, sig)
   double alpha = 2.0 / (sig + 1.0);
   double sline = macd[0];
   for(int i=0; i<g_n; i++)
     {
      if(i == 0) sline = macd[0];
      else       sline = alpha * macd[i] + (1.0 - alpha) * sline;
      mainOut[i] = macd[i];
      histOut[i] = macd[i] - sline;
     }
  }

//------------------------------------------------------------------
// ADX with +DI/-DI (Wilder) - core/indicators/trend.py ADX.
//------------------------------------------------------------------
void AdxSeries(int period, double &adxOut[], double &plusOut[], double &minusOut[])
  {
   ArrayResize(adxOut, g_n);
   ArrayResize(plusOut, g_n);
   ArrayResize(minusOut, g_n);
   ArrayInitialize(adxOut, 0.0);
   ArrayInitialize(plusOut, 0.0);
   ArrayInitialize(minusOut, 0.0);
   if(g_n <= 2*period || period <= 0)
      return;
   double tr[], plusDM[], minusDM[];
   ArrayResize(tr, g_n);
   ArrayResize(plusDM, g_n);
   ArrayResize(minusDM, g_n);
   tr[0] = 0.0; plusDM[0] = 0.0; minusDM[0] = 0.0;
   for(int i=1; i<g_n; i++)
     {
      double up   = g_high[i] - g_high[i-1];
      double down = g_low[i-1] - g_low[i];
      plusDM[i]  = (up > down && up > 0)   ? up   : 0.0;
      minusDM[i] = (down > up && down > 0) ? down : 0.0;
      double hl = g_high[i] - g_low[i];
      double hc = MathAbs(g_high[i] - g_close[i-1]);
      double lc = MathAbs(g_low[i]  - g_close[i-1]);
      tr[i] = MathMax(hl, MathMax(hc, lc));
     }
   // Wilder smoothing of TR, +DM, -DM.
   double atr = 0.0, sPlus = 0.0, sMinus = 0.0;
   for(int i=1; i<=period; i++)
     {
      atr += tr[i]; sPlus += plusDM[i]; sMinus += minusDM[i];
     }
   double dxSum = 0.0;
   int dxCount = 0;
   double firstAdxSet = false;
   for(int i=period+1; i<g_n; i++)
     {
      atr    = atr    - (atr/period)    + tr[i];
      sPlus  = sPlus  - (sPlus/period)  + plusDM[i];
      sMinus = sMinus - (sMinus/period) + minusDM[i];
      double pdi = (atr==0.0) ? 0.0 : 100.0 * sPlus  / atr;
      double mdi = (atr==0.0) ? 0.0 : 100.0 * sMinus / atr;
      plusOut[i]  = pdi;
      minusOut[i] = mdi;
      double denom = pdi + mdi;
      double dx = (denom==0.0) ? 0.0 : 100.0 * MathAbs(pdi-mdi)/denom;
      dxSum += dx; dxCount++;
      if(dxCount == period)
         adxOut[i] = dxSum / period;              // first ADX = SMA of DX
      else if(dxCount > period)
         adxOut[i] = (adxOut[i-1]*(period-1) + dx)/period; // Wilder smooth
     }
  }

//------------------------------------------------------------------
// BlendedSignal for one bar index, using the SAME math as
// Mt5SmartBotEA.mq5::BlendedSignal (ema+rsi+macd+adx subset).
//------------------------------------------------------------------
double BlendedAt(int i, const double &ema[], const double &rsi[],
                 const double &macdMain[], const double &macdHist[],
                 const double &adx[], const double &pdi[], const double &mdi[])
  {
   double close1 = g_close[i];
   double weighted = 0.0;
   double wsum = 0.0;

   // EMA: diff = (close-ema)/ema; s = clamp(diff*50)
   if(ema[i] > 0.0)
     {
      double s = Clamp1(((close1 - ema[i]) / ema[i]) * 50.0);
      weighted += InpEmaWeight * s;
      wsum     += MathAbs(InpEmaWeight);
     }

   // RSI mean-reversion.
   {
    double r = rsi[i];
    double s = 0.0;
    if(r <= 30.0)      { s = (30.0 - r)/30.0 + 0.5; if(s>1.0) s=1.0; }
    else if(r >= 70.0) { double m=(r-70.0)/30.0+0.5; if(m>1.0) m=1.0; s=-m; }
    else               { s = (r - 50.0)/50.0 * 0.3; }
    weighted += InpRsiWeight * s;
    wsum     += MathAbs(InpRsiWeight);
   }

   // MACD.
   {
    double hist = macdHist[i];
    double base = (hist > 0.0) ? 1.0 : -1.0;
    double denom = MathAbs(macdMain[i]) + 1e-9;
    double strength = MathAbs(hist)/denom;
    if(strength > 1.0) strength = 1.0;
    double s = base * (0.5 + 0.5*strength);
    weighted += InpMacdWeight * s;
    wsum     += MathAbs(InpMacdWeight);
   }

   // ADX with strength gate.
   {
    double direction = (pdi[i] > mdi[i]) ? 1.0 : -1.0;
    double strength = (adx[i] - 20.0)/30.0;
    if(strength < 0.0) strength = 0.0;
    if(strength > 1.0) strength = 1.0;
    double s = direction * strength;
    weighted += InpAdxWeight * s;
    wsum     += MathAbs(InpAdxWeight);
   }

   if(wsum <= 0.0)
      return(0.0);
   return(Clamp1(weighted / wsum));
  }

//------------------------------------------------------------------
void OnStart()
  {
   if(!LoadBars(InpInFile))
      return;

   double ema[], rsi[], macdMain[], macdHist[], adx[], pdi[], mdi[];
   EmaSeries(InpEmaPeriod, ema);
   RsiSeries(InpRsiPeriod, rsi);
   MacdSeries(InpMacdFast, InpMacdSlow, InpMacdSignal, macdMain, macdHist);
   AdxSeries(InpAdxPeriod, adx, pdi, mdi);

   int h = FileOpen(InpOutFile, FILE_WRITE|FILE_ANSI|FILE_TXT|FILE_CSV, ',');
   if(h == INVALID_HANDLE)
     {
      PrintFormat("ParityDump: cannot write %s (err %d)",
                  InpOutFile, GetLastError());
      return;
     }
   FileWrite(h, "bar_index", "blended_signal");
   for(int i=0; i<g_n; i++)
     {
      double s = BlendedAt(i, ema, rsi, macdMain, macdHist, adx, pdi, mdi);
      FileWrite(h, IntegerToString(i), DoubleToString(s, 10));
     }
   FileClose(h);
   PrintFormat("ParityDump: wrote %d signals to %s", g_n, InpOutFile);
  }
//+------------------------------------------------------------------+
