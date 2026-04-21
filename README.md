# Project Chimera
Institutional-grade, multi-asset trading mainframe built on an async
Producer-Consumer microservices architecture.
Architecture
Ingestor Layer   →  DataAgent (WebSocket + REST)
                    NewsAgent (LLM NLP + Veto)
         ↓
Processor Layer  →  StrategyAgent (TA Engine + Sp Score)
                    RiskAgent (Kelly + ATR Stops)
         ↓
Executor Layer   →  OMS (Alpaca REST + Trade Logger)
Quick Start
bash# 1. Create a virtual environment
python -m venv .venv && source .venv/bin/activate

## 2. Install dependencies
pip install -r requirements.txt

## 3. Create your .env file (never commit this)
cat > .env << EOF
ALPACA_KEY=your_alpaca_key
ALPACA_SECRET=your_alpaca_secret
OPENAI_API_KEY=your_openai_key
WHALE_ALERT_KEY=your_whale_alert_key   # optional
DUNE_API_KEY=your_dune_key             # optional
CHIMERA_MODE=paper                     # ALWAYS start with paper
EOF

## 4. Run the mainframe
python -m chimera.mainframe
The Four Pillars
SectorEdgeKey Data SourcesCryptoExchange inflow/outflowWhale Alert, Dune, Alpaca WSStocksShort Squeeze (Sp score)Finviz, Stocktwits, AlpacaForexNLP momentum on EMAFinancialJuice, AlpacaFuturesValue Area mean reversionAlpaca CME, AVWAP
Squeeze Probability Score
Sp = (SI × 0.4) + (V_velocity × 0.3) + (S_sentiment × 0.3)
Where:

SI          = Normalised short interest (0–1, cap at 50%)
V_velocity  = Normalised relative volume (RVOL 1–10 → 0–1)
S_sentiment = Normalised Z-score of social mentions (0–5 → 0–1)

Signals with Sp < 0.60 are discarded. Sp > 0.75 triggers a long.
Position Sizing
Position Size = (Account Equity × Risk%) / (ATR × 2)
Risk% is bounded by:

base_risk_pct from config (default 1%)
Kelly Criterion (computed from rolling 50-trade win history)
News Agent confidence multiplier (0 during veto)

The Veto System
The News Agent raises veto_active = True when any of the following are
detected in FinancialJuice or Stocktwits headlines:

FOMC / Fed meeting / rate decision
CPI / PCE / NFP releases
Emergency central bank actions

All pending signals are dropped and the system stays in cash for a
configurable cool-down window (default: 10 minutes).

## News Agent:
chimera/agents/news_agent.py
News Agent — polls FinancialJuice and Stocktwits, runs LLM-based NLP
classification, sets confidence intervals, and raises macro veto flags.

## FOR SAFTEY:
The veto system is the most important safety mechanism in Chimera:
if a high-impact Fed / CPI / NFP event is detected, ALL technical signals
are suppressed until the dust settles (configurable cool-down window).


import asyncio
import re
from datetime import datetime
from typing import Any

import aiohttp
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser

from chimera.utils.state import SharedState, NewsState, Sentiment
from chimera.utils.logger import setup_logger

log = setup_logger("news_agent")

## Veto keywords
VETO_PATTERNS = [
    r"fed\s+(decision|meeting|statement|chair|powell)",
    r"fomc",
    r"nonfarm\s+payroll|nfp",
    r"cpi|pce|inflation\s+data",
    r"rate\s+(hike|cut|hold|decision)",
    r"emergency\s+(rate|fed|meeting)",
    r"bank\s+of\s+(england|japan|ecb|europe)\s+(decision|hike|cut)",
]
HAWKISH_KEYWORDS = ["hawkish", "rate hike", "tighten", "inflation concerns", "restrict"]
BEARISH_KEYWORDS = ["rate cut", "dovish", "recession", "layoffs", "credit event", "default"]

_VETO_RE = re.compile("|".join(VETO_PATTERNS), re.IGNORECASE)


class NewsAgent:
    """
    Autonomous News Agent.
    1. Polls REST endpoints on a configurable interval.
    2. Passes headlines through an LLM classifier (LangChain + OpenAI).
    3. Writes Sentiment + CI score to shared state.
    4. Sets veto_active=True if macro event keywords are detected.
    """

    SYSTEM_PROMPT = """You are a quantitative trading news analyst.
Classify the following financial headline(s) and return ONLY valid JSON:
{
  "sentiment": "bullish" | "bearish" | "neutral",
  "confidence": <float 0.0–1.0>,
  "macro_event": <bool>,
  "macro_reason": "<string, empty if not macro>"
}
Be conservative: a single ambiguous headline should return neutral with low confidence.
High-impact macro events (FOMC, CPI, NFP) must set macro_event=true regardless of direction."""

    def __init__(self, state: SharedState, config: dict[str, Any]):
        self.state  = state
        self.config = config
        self.poll_interval = config.get("news_poll_seconds", 30)
        self.veto_cooldown = config.get("veto_cooldown_seconds", 600)  # 10 min default
        self._veto_until: datetime | None = None

        llm = ChatOpenAI(
            model="gpt-4o-mini",
            temperature=0,
            api_key=config["openai_api_key"],
        )
        prompt = ChatPromptTemplate.from_messages([
            ("system", self.SYSTEM_PROMPT),
            ("human", "Headlines:\n{headlines}"),
        ])
        self._chain = prompt | llm | JsonOutputParser()

    async def run(self) -> None:
        log.info("NewsAgent started.")
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    headlines = await self._fetch_all(session)
                    if headlines:
                        await self._classify_and_update(headlines)
                except Exception as e:
                    log.warning(f"NewsAgent poll error: {e}")
                await asyncio.sleep(self.poll_interval)

    # ── Fetchers ───────────────────────────────────────────────────────────────

    async def _fetch_all(self, session: aiohttp.ClientSession) -> list[str]:
        results = await asyncio.gather(
            self._fetch_financial_juice(session),
            self._fetch_stocktwits(session),
            return_exceptions=True,
        )
        headlines: list[str] = []
        for r in results:
            if isinstance(r, list):
                headlines.extend(r)
        return headlines[:20]  # cap to keep LLM token cost low

    async def _fetch_financial_juice(self, session: aiohttp.ClientSession) -> list[str]:
        """
        FinancialJuice provides a free headline RSS / JSON feed.
        Replace the URL with their paid API endpoint if you have a key.
        """
        url = self.config.get("financialjuice_url", "https://www.financialjuice.com/feed.ashx?c=market")
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            data = await resp.json(content_type=None)
            return [item.get("title", "") for item in data.get("items", [])[:10]]

    async def _fetch_stocktwits(self, session: aiohttp.ClientSession) -> list[str]:
        """Stocktwits trending stream — no API key required for public feed."""
        url = "https://api.stocktwits.com/api/2/streams/trending.json"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            data = await resp.json()
            messages = data.get("messages", [])
            return [m.get("body", "") for m in messages[:10]]

    # ── Classification ─────────────────────────────────────────────────────────

    async def _classify_and_update(self, headlines: list[str]) -> None:
        combined = "\n".join(f"- {h}" for h in headlines if h.strip())

        # Fast regex veto check (don't spend LLM tokens if pattern match fires)
        if _VETO_RE.search(combined):
            await self._raise_veto(combined)
            return

        result: dict[str, Any] = await self._chain.ainvoke({"headlines": combined})

        sentiment_str = result.get("sentiment", "neutral").lower()
        confidence    = float(result.get("confidence", 0.5))
        macro_event   = bool(result.get("macro_event", False))
        macro_reason  = result.get("macro_reason", "")

        if macro_event:
            await self._raise_veto(macro_reason or combined)
            return

        self.state.news = NewsState(
            sentiment    = Sentiment(sentiment_str),
            confidence   = confidence,
            veto_active  = False,
            last_updated = datetime.utcnow(),
        )
        log.info(f"News: {sentiment_str} CI={confidence:.2f}")

    async def _raise_veto(self, reason: str) -> None:
        from datetime import timedelta
        self._veto_until = datetime.utcnow() + timedelta(seconds=self.veto_cooldown)
        self.state.news.veto_active = True
        self.state.news.veto_reason = reason[:120]
        log.warning(f"VETO RAISED — {reason[:80]}... (cool-down {self.veto_cooldown}s)")

        # Auto-clear after cool-down
        await asyncio.sleep(self.veto_cooldown)
        self.state.news.veto_active = False
        self.state.news.veto_reason = ""
        log.info("Veto cleared — signals re-enabled.")
