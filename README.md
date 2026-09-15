# 🏛️ Gold (XAUUSD) 4-Hour Reversal Strategy

Institutional algorithmic trading engine executing pure 3-candle reversal setups on Gold (XAU/USD) 4-Hour charts using MetaTrader 5 (MT5).

---

## 📌 Architecture & Design
- **Timeframe**: 4-Hour (H4) Completed Candles.
- **Instrument**: `XAUUSD` (Spot Gold).
- **Session**: 24/5 global FX/CFD market (Sunday 22:00 UTC to Friday 22:00 UTC).
- **Execution Modes**: PAPER (simulation) and LIVE (real MT5 broker orders).
- **Risk Management**: Dynamic lot sizing based on account equity, fixed SL/TP with ATR/candle bounds, dynamic Trailing Stop Loss (TSL).
- **Validation Engine**: Dedicated setup tracker, trade tracker, and performance analytics.

---

## 🚀 Setup & Execution

### 1. Requirements
- Windows 10/11 or Windows Server with MetaTrader 5 installed.
- Python 3.10 - 3.12 64-bit.

### 2. Installation
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 3. Configuration
Copy `.env.example` to `.env` and set your trading mode:
```ini
TRADING_MODE=PAPER  # or LIVE
```

### 4. Running the Engine
```powershell
python main.py
```
Or for non-interactive / background service mode:
```powershell
python main.py --daemon
```

### 5. Running the Audit Test Suite
```powershell
python test_audit_comprehensive.py
```
