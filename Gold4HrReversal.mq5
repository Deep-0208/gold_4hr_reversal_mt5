//+------------------------------------------------------------------+
//|                                            Gold4HrReversal.mq5   |
//|                                  Copyright 2026, Algo Strategies |
//|                                 https://github.com/Deep-0208     |
//+------------------------------------------------------------------+
#property copyright   "Copyright 2026, Algo Strategies"
#property link        "https://github.com/Deep-0208"
#property version     "3.00"
#property description "Gold (XAUUSD) 4-Hour 3-Candle U-Shape Reversal Strategy"
#property description "Matches Python gold_4hr_reversal engine with TSL & 4.0R Max Target"

#include <Trade\Trade.mqh>
#include <Trade\PositionInfo.mqh>
#include <Trade\SymbolInfo.mqh>

//+------------------------------------------------------------------+
//| INPUT PARAMETERS                                                 |
//+------------------------------------------------------------------+
input group "── General Settings ──"
input ulong             InpMagicNumber       = 10104;       // Magic Number
input double            InpLotSize           = 0.01;        // Trade Volume (Lots)
input ENUM_TIMEFRAMES   InpTimeframe         = PERIOD_H4;   // Strategy Timeframe

input group "── Risk & Target Settings ──"
input double            InpSLBuffer          = 0.50;        // SL Buffer Beyond C3 ($)
input double            InpRRRatio           = 2.0;         // Base R:R (if TSL disabled)
input double            InpMaxTargetRR       = 4.0;         // Hard Exit Max Target (R-Multiple)
input double            InpMaxRiskPct        = 3.0;         // Max Risk % of Entry Price (3.0%)
input double            InpMaxSpread         = 1.20;        // Max Spread Allowed for Entry ($)

input group "── Trailing Stop Loss (TSL) ──"
input bool              InpUseTSL            = true;        // Enable Trailing Stop Loss
input double            InpTSLActivationRR   = 1.0;         // TSL Activation Level (R)
input double            InpTSLFloorRR        = 1.0;         // TSL Guaranteed Floor Level (R)
input double            InpTSLTrailFraction  = 0.20;        // Trailing Distance (Fraction of Risk)
input double            InpTSLMinStep        = 0.50;        // Minimum SL Modification Step ($)

input group "── Pattern Tolerance (U-Shape) ──"
input double            InpTolerancePct      = 0.05;        // C2 Overlap Tolerance (% of C1 range)
input double            InpMinTolerancePts   = 1.00;        // Minimum C2 Overlap Floor ($)

//+------------------------------------------------------------------+
//| GLOBAL OBJECTS & STATE                                           |
//+------------------------------------------------------------------+
CTrade         m_trade;
CPositionInfo  m_position;
CSymbolInfo    m_symbol;

datetime       m_last_bar_time    = 0;
double         m_entry_price      = 0.0;
double         m_risk_distance    = 0.0;
double         m_peak_price       = 0.0;
bool           m_tsl_active       = false;
int            m_trade_dir        = 0; // +1 = LONG, -1 = SHORT

//+------------------------------------------------------------------+
//| CANDLE & PATTERN HELPERS                                         |
//+------------------------------------------------------------------+
bool IsBullish(const MqlRates &c) { return (c.close > c.open); }
bool IsBearish(const MqlRates &c) { return (c.close < c.open); }
double BodyTop(const MqlRates &c) { return MathMax(c.open, c.close); }
double BodyBottom(const MqlRates &c) { return MathMin(c.open, c.close); }

// Check whether C2 body connects with C1 body (U-Shape absorption)
bool C2OverlapsC1(const MqlRates &c1, const MqlRates &c2)
{
   double c1_bt = BodyTop(c1);
   double c1_bb = BodyBottom(c1);
   double c1_range = c1.high - c1.low;
   double tolerance = MathMax(c1_range * InpTolerancePct, InpMinTolerancePts);

   double lower = c1_bb - tolerance;
   double upper = c1_bt + tolerance;

   bool open_inside  = (c2.open >= lower && c2.open <= upper);
   bool close_inside = (c2.close >= lower && c2.close <= upper);

   return (open_inside || close_inside);
}

//+------------------------------------------------------------------+
//| EXPERT INITIALIZATION                                            |
//+------------------------------------------------------------------+
int OnInit()
{
   if(!m_symbol.Name(_Symbol))
   {
      Print("❌ Failed to initialize symbol: ", _Symbol);
      return INIT_FAILED;
   }

   m_trade.SetExpertMagicNumber(InpMagicNumber);
   m_trade.SetDeviationInPoints(20);
   m_trade.SetTypeFillingBySymbol(_Symbol);

   PrintFormat("✅ Gold 4Hr Reversal EA v3.00 Initialized | Symbol: %s | TF: H4 | Lots: %.2f | Magic: %d",
               _Symbol, InpLotSize, InpMagicNumber);

   // Sync state if position already open
   CheckExistingPosition();

   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//| EXPERT DEINITIALIZATION                                          |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   Print("Gold 4Hr Reversal EA deinitialized. Reason: ", reason);
}

//+------------------------------------------------------------------+
//| RESTORE / CHECK EXISTING POSITION ON STARTUP                     |
//+------------------------------------------------------------------+
void CheckExistingPosition()
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      if(m_position.SelectByIndex(i))
      {
         if(m_position.Symbol() == _Symbol && m_position.Magic() == InpMagicNumber)
         {
            m_entry_price = m_position.PriceOpen();
            double sl = m_position.StopLoss();
            if(m_position.PositionType() == POSITION_TYPE_BUY)
            {
               m_trade_dir = 1;
               m_risk_distance = (sl > 0) ? (m_entry_price - sl) : 0.0;
               m_peak_price = MathMax(m_entry_price, m_symbol.Bid());
            }
            else
            {
               m_trade_dir = -1;
               m_risk_distance = (sl > 0) ? (sl - m_entry_price) : 0.0;
               m_peak_price = MathMin(m_entry_price, m_symbol.Ask());
            }
            m_tsl_active = false;
            PrintFormat("🔄 Resumed monitoring open position #%I64u | Type: %s | Open: $%.2f | SL: $%.2f",
                        m_position.Ticket(), (m_trade_dir == 1 ? "LONG" : "SHORT"), m_entry_price, sl);
            return;
         }
      }
   }
   // Reset state if no position
   m_trade_dir = 0;
   m_entry_price = 0.0;
   m_risk_distance = 0.0;
   m_peak_price = 0.0;
   m_tsl_active = false;
}

//+------------------------------------------------------------------+
//| ON TICK: REAL-TIME TSL & POSITION MANAGEMENT                     |
//+------------------------------------------------------------------+
void OnTick()
{
   if(!m_symbol.RefreshRates()) return;

   // 1. Manage Active Position (TSL & Max Target)
   ManageActivePosition();

   // 2. Check for New H4 Bar to Evaluate Entry Pattern
   datetime current_bar_time = iTime(_Symbol, InpTimeframe, 0);
   if(current_bar_time == 0) return;

   if(current_bar_time != m_last_bar_time)
   {
      m_last_bar_time = current_bar_time;
      EvaluateNewBarSignals();
   }
}

//+------------------------------------------------------------------+
//| MANAGE ACTIVE POSITION: TSL & 4.0R MAX TARGET                    |
//+------------------------------------------------------------------+
void ManageActivePosition()
{
   bool has_position = false;
   ulong ticket = 0;
   double current_sl = 0.0;
   double current_tp = 0.0;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      if(m_position.SelectByIndex(i))
      {
         if(m_position.Symbol() == _Symbol && m_position.Magic() == InpMagicNumber)
         {
            has_position = true;
            ticket = m_position.Ticket();
            current_sl = m_position.StopLoss();
            current_tp = m_position.TakeProfit();
            break;
         }
      }
   }

   if(!has_position)
   {
      m_trade_dir = 0;
      m_tsl_active = false;
      return;
   }

   if(m_risk_distance <= 0) return;

   // ── LONG POSITION MANAGEMENT ──
   if(m_trade_dir == 1)
   {
      double bid = m_symbol.Bid();
      if(bid > m_peak_price) m_peak_price = bid;

      double r_current = (bid - m_entry_price) / m_risk_distance;

      // Check Hard Max Target (e.g. 4.0R)
      if(r_current >= InpMaxTargetRR)
      {
         PrintFormat("🎯 [MAX TARGET] Long hit %.2fR at $%.2f. Closing position #%I64u.",
                     r_current, bid, ticket);
         m_trade.PositionClose(ticket);
         return;
      }

      // Check Trailing Stop Loss
      if(InpUseTSL)
      {
         if(!m_tsl_active && r_current >= InpTSLActivationRR)
         {
            m_tsl_active = true;
            PrintFormat("🛡️ [TSL ACTIVATED] Long reached +%.2fR (Bid=$%.2f). Floor locked at +%.2fR.",
                        r_current, bid, InpTSLFloorRR);
         }

         if(m_tsl_active)
         {
            double trail_sl = NormalizeDouble(m_peak_price - (m_risk_distance * InpTSLTrailFraction), _Digits);
            bool floor_reached = (m_peak_price - m_entry_price) >= (m_risk_distance * InpTSLFloorRR);
            double floor_sl = floor_reached ? NormalizeDouble(m_entry_price + (m_risk_distance * InpTSLFloorRR), _Digits) : trail_sl;
            double target_sl = MathMax(trail_sl, floor_sl);

            if(target_sl > current_sl + InpTSLMinStep)
            {
               PrintFormat("📈 [TSL UPDATE] Ratcheting Long SL: $%.2f -> $%.2f (Peak=$%.2f, Bid=$%.2f)",
                           current_sl, target_sl, m_peak_price, bid);
               m_trade.PositionModify(ticket, target_sl, current_tp);
            }
         }
      }
   }
   // ── SHORT POSITION MANAGEMENT ──
   else if(m_trade_dir == -1)
   {
      double ask = m_symbol.Ask();
      if(m_peak_price == 0.0 || ask < m_peak_price) m_peak_price = ask;

      double r_current = (m_entry_price - ask) / m_risk_distance;

      // Check Hard Max Target (e.g. 4.0R)
      if(r_current >= InpMaxTargetRR)
      {
         PrintFormat("🎯 [MAX TARGET] Short hit %.2fR at $%.2f. Closing position #%I64u.",
                     r_current, ask, ticket);
         m_trade.PositionClose(ticket);
         return;
      }

      // Check Trailing Stop Loss
      if(InpUseTSL)
      {
         if(!m_tsl_active && r_current >= InpTSLActivationRR)
         {
            m_tsl_active = true;
            PrintFormat("🛡️ [TSL ACTIVATED] Short reached +%.2fR (Ask=$%.2f). Floor locked at +%.2fR.",
                        r_current, ask, InpTSLFloorRR);
         }

         if(m_tsl_active)
         {
            double trail_sl = NormalizeDouble(m_peak_price + (m_risk_distance * InpTSLTrailFraction), _Digits);
            bool floor_reached = (m_entry_price - m_peak_price) >= (m_risk_distance * InpTSLFloorRR);
            double floor_sl = floor_reached ? NormalizeDouble(m_entry_price - (m_risk_distance * InpTSLFloorRR), _Digits) : trail_sl;
            double target_sl = MathMin(trail_sl, floor_sl);

            if(current_sl == 0.0 || target_sl < current_sl - InpTSLMinStep)
            {
               PrintFormat("📉 [TSL UPDATE] Ratcheting Short SL: $%.2f -> $%.2f (Trough=$%.2f, Ask=$%.2f)",
                           current_sl, target_sl, m_peak_price, ask);
               m_trade.PositionModify(ticket, target_sl, current_tp);
            }
         }
      }
   }
}

//+------------------------------------------------------------------+
//| EVALUATE 3-CANDLE REVERSAL PATTERNS ON COMPLETED BARS            |
//+------------------------------------------------------------------+
void EvaluateNewBarSignals()
{
   // Do not take new entries if already in a trade
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      if(m_position.SelectByIndex(i))
      {
         if(m_position.Symbol() == _Symbol && m_position.Magic() == InpMagicNumber)
            return;
      }
   }

   // Fetch last 3 completed bars: Bar 1 (C3), Bar 2 (C2), Bar 3 (C1)
   MqlRates rates[];
   ArraySetAsSeries(rates, true);
   int copied = CopyRates(_Symbol, InpTimeframe, 1, 3, rates);
   if(copied < 3)
   {
      Print("⚠️ Failed to copy 3 completed H4 candles.");
      return;
   }

   MqlRates c3 = rates[0]; // Bar 1: newest completed candle
   MqlRates c2 = rates[1]; // Bar 2: middle candle
   MqlRates c1 = rates[2]; // Bar 3: oldest candle

   // Check spread filter before analyzing setup
   double spread = m_symbol.Ask() - m_symbol.Bid();
   if(spread > InpMaxSpread)
   {
      PrintFormat("⚠️ [SPREAD GUARD] Spread $%.2f exceeds limit $%.2f. Skipping evaluation.",
                  spread, InpMaxSpread);
      return;
   }

   // ─────────────────────────────────────────────
   // 1. BULLISH REVERSAL (LONG)
   // ─────────────────────────────────────────────
   if(IsBullish(c3))
   {
      if(C2OverlapsC1(c1, c2))
      {
         // Exclude 🟢🟢🟢 (momentum continuation)
         if(!(IsBullish(c1) && IsBullish(c2)))
         {
            bool valid_pattern = false;
            string pattern_name = "";

            if(IsBearish(c1) && IsBearish(c2))      { valid_pattern = true; pattern_name = "Pattern 1 (🔴🔴🟢)"; }
            else if(IsBullish(c1) && IsBearish(c2)) { valid_pattern = true; pattern_name = "Pattern 2 (🟢🔴🟢)"; }
            else if(IsBearish(c1) && IsBullish(c2)) { valid_pattern = true; pattern_name = "Pattern 3 (🔴🟢🟢)"; }

            if(valid_pattern)
            {
               double c1_bt = BodyTop(c1);
               double c2_bt = BodyTop(c2);

               // C3.close must be strictly above body_top of BOTH C1 and C2
               if(c3.close > c1_bt && c3.close > c2_bt)
               {
                  PrintFormat("🎯 [SIGNAL] Bullish Reversal Detected: %s | C3 Close: $%.2f > C1_BT: $%.2f & C2_BT: $%.2f",
                              pattern_name, c3.close, c1_bt, c2_bt);
                  ExecuteLongOrder(c3);
                  return;
               }
            }
         }
      }
   }

   // ─────────────────────────────────────────────
   // 2. BEARISH REVERSAL (SHORT)
   // ─────────────────────────────────────────────
   if(IsBearish(c3))
   {
      if(C2OverlapsC1(c1, c2))
      {
         // Exclude 🔴🔴🔴 (momentum continuation)
         if(!(IsBearish(c1) && IsBearish(c2)))
         {
            bool valid_pattern = false;
            string pattern_name = "";

            if(IsBullish(c1) && IsBullish(c2))      { valid_pattern = true; pattern_name = "Pattern 4 (🟢🟢🔴)"; }
            else if(IsBearish(c1) && IsBullish(c2)) { valid_pattern = true; pattern_name = "Pattern 5 (🔴🟢🔴)"; }
            else if(IsBullish(c1) && IsBearish(c2)) { valid_pattern = true; pattern_name = "Pattern 6 (🟢🔴🔴)"; }

            if(valid_pattern)
            {
               double c1_bb = BodyBottom(c1);
               double c2_bb = BodyBottom(c2);

               // C3.close must be strictly below body_bottom of BOTH C1 and C2
               if(c3.close < c1_bb && c3.close < c2_bb)
               {
                  PrintFormat("🎯 [SIGNAL] Bearish Reversal Detected: %s | C3 Close: $%.2f < C1_BB: $%.2f & C2_BB: $%.2f",
                              pattern_name, c3.close, c1_bb, c2_bb);
                  ExecuteShortOrder(c3);
                  return;
               }
            }
         }
      }
   }
}

//+------------------------------------------------------------------+
//| EXECUTE LONG ORDER                                               |
//+------------------------------------------------------------------+
void ExecuteLongOrder(const MqlRates &c3)
{
   double ask = m_symbol.Ask();
   double formation_low = c3.low;
   double sl = NormalizeDouble(formation_low - InpSLBuffer, _Digits);
   double risk = ask - sl;

   // Sanity Guard: Inverted Risk
   if(sl >= ask || risk <= 0)
   {
      PrintFormat("⚠️ [REJECT] Inverted Long SL: SL=$%.2f >= Ask=$%.2f", sl, ask);
      return;
   }

   // Sanity Guard: Max Risk %
   if(risk > ask * (InpMaxRiskPct / 100.0))
   {
      PrintFormat("⚠️ [REJECT] Risk $%.2f exceeds %.1f%% threshold of entry ($%.2f)",
                  risk, InpMaxRiskPct, ask * (InpMaxRiskPct / 100.0));
      return;
   }

   double tp = (InpUseTSL) ? 0.0 : NormalizeDouble(ask + (risk * InpRRRatio), _Digits);

   PrintFormat("🚀 [ENTRY LONG] Placing BUY %.2f lots at $%.2f | SL: $%.2f | Risk: $%.2f (1.0R)",
               InpLotSize, ask, sl, risk);

   if(m_trade.Buy(InpLotSize, _Symbol, ask, sl, tp, "Gold4H_Long"))
   {
      m_entry_price = ask;
      m_risk_distance = risk;
      m_peak_price = ask;
      m_tsl_active = false;
      m_trade_dir = 1;
      PrintFormat("✅ Long order executed successfully. Ticket: #%I64u", m_trade.ResultOrder());
   }
   else
   {
      PrintFormat("❌ Long order failed. Error code: %d", m_trade.ResultRetcode());
   }
}

//+------------------------------------------------------------------+
//| EXECUTE SHORT ORDER                                              |
//+------------------------------------------------------------------+
void ExecuteShortOrder(const MqlRates &c3)
{
   double bid = m_symbol.Bid();
   double formation_high = c3.high;
   double sl = NormalizeDouble(formation_high + InpSLBuffer, _Digits);
   double risk = sl - bid;

   // Sanity Guard: Inverted Risk
   if(sl <= bid || risk <= 0)
   {
      PrintFormat("⚠️ [REJECT] Inverted Short SL: SL=$%.2f <= Bid=$%.2f", sl, bid);
      return;
   }

   // Sanity Guard: Max Risk %
   if(risk > bid * (InpMaxRiskPct / 100.0))
   {
      PrintFormat("⚠️ [REJECT] Risk $%.2f exceeds %.1f%% threshold of entry ($%.2f)",
                  risk, InpMaxRiskPct, bid * (InpMaxRiskPct / 100.0));
      return;
   }

   double tp = (InpUseTSL) ? 0.0 : NormalizeDouble(bid - (risk * InpRRRatio), _Digits);

   PrintFormat("🚀 [ENTRY SHORT] Placing SELL %.2f lots at $%.2f | SL: $%.2f | Risk: $%.2f (1.0R)",
               InpLotSize, bid, sl, risk);

   if(m_trade.Sell(InpLotSize, _Symbol, bid, sl, tp, "Gold4H_Short"))
   {
      m_entry_price = bid;
      m_risk_distance = risk;
      m_peak_price = bid;
      m_tsl_active = false;
      m_trade_dir = -1;
      PrintFormat("✅ Short order executed successfully. Ticket: #%I64u", m_trade.ResultOrder());
   }
   else
   {
      PrintFormat("❌ Short order failed. Error code: %d", m_trade.ResultRetcode());
   }
}
//+------------------------------------------------------------------+
