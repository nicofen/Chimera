# Project Chimera

Institutional-grade, multi-asset trading mainframe built on an async
Producer-Consumer microservices architecture.

## Architecture

```
Ingestor Layer   →  DataAgent (WebSocket + REST)
                    NewsAgent (LLM NLP + Veto)
         ↓
Processor Layer  →  StrategyAgent (TA Engine + Sp Score)
                    RiskAgent (Kelly + ATR Stops)
         ↓
Executor Layer   →  OMS (Alpaca REST + Trade Logger)
```

## Quick Start

```bash
# 1. Create a virtual environment
python -m venv .venv && source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Create your .env file (never commit this)
cat > .env << EOF
ALPACA_KEY=your_alpaca_key
ALPACA_SECRET=your_alpaca_secret
OPENAI_API_KEY=your_openai_key
WHALE_ALERT_KEY=your_whale_alert_key   # optional
DUNE_API_KEY=your_dune_key             # optional
CHIMERA_MODE=paper                     # ALWAYS start with paper
EOF

# 4. Run the mainframe
python -m chimera.mainframe
```

## The Four Pillars

| Sector  | Edge                         | Key Data Sources              |
|---------|------------------------------|-------------------------------|
| Crypto  | Exchange inflow/outflow      | Whale Alert, Dune, Alpaca WS  |
| Stocks  | Short Squeeze (Sp score)     | Finviz, Stocktwits, Alpaca    |
| Forex   | NLP momentum on EMA          | FinancialJuice, Alpaca        |
| Futures | Value Area mean reversion    | Alpaca CME, AVWAP             |

## Squeeze Probability Score

```
Sp = (SI × 0.4) + (V_velocity × 0.3) + (S_sentiment × 0.3)
```

Where:
- `SI`          = Normalised short interest (0–1, cap at 50%)
- `V_velocity`  = Normalised relative volume (RVOL 1–10 → 0–1)
- `S_sentiment` = Normalised Z-score of social mentions (0–5 → 0–1)

Signals with Sp < 0.60 are discarded. Sp > 0.75 triggers a long.

## Position Sizing

```
Position Size = (Account Equity × Risk%) / (ATR × 2)
```

Risk% is bounded by:
1. `base_risk_pct` from config (default 1%)
2. Kelly Criterion (computed from rolling 50-trade win history)
3. News Agent confidence multiplier (0 during veto)

## The Veto System

The News Agent raises `veto_active = True` when any of the following are
detected in FinancialJuice or Stocktwits headlines:
- FOMC / Fed meeting / rate decision
- CPI / PCE / NFP releases
- Emergency central bank actions

All pending signals are **dropped** and the system stays in cash for a
configurable cool-down window (default: 10 minutes).

## Important Warning

Past performance of any trading strategy is not indicative of future results.
Always paper-trade for a minimum of 3 months before risking real capital.
Never risk more than you can afford to lose entirely.
