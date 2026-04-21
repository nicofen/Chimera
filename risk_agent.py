"""
chimera/config/settings.py
Load configuration from environment variables or a .env file.

ALWAYS start with mode="paper" until you have 3+ months of
consistent paper-trading P&L. Use mode="live" only when you
are prepared to manage real capital drawdowns.
"""

import os
from typing import Any


def load_config() -> dict[str, Any]:
    """
    Returns the Chimera configuration dict.
    Secrets are read from environment variables — never hardcode them.

    Required env vars:
      ALPACA_KEY, ALPACA_SECRET, OPENAI_API_KEY

    Optional:
      WHALE_ALERT_KEY, CMC_API_KEY, DUNE_API_KEY, FINANCIALJUICE_URL
    """
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    return {
        # ── Mode ──────────────────────────────────────────────────────────────
        "mode": os.getenv("CHIMERA_MODE", "paper"),   # "paper" | "live"

        # ── API Keys ──────────────────────────────────────────────────────────
        "alpaca_key":    os.environ["ALPACA_KEY"],
        "alpaca_secret": os.environ["ALPACA_SECRET"],
        "openai_api_key": os.environ["OPENAI_API_KEY"],
        "whale_alert_key":  os.getenv("WHALE_ALERT_KEY", ""),
        "cmc_api_key":      os.getenv("CMC_API_KEY", ""),
        "dune_api_key":     os.getenv("DUNE_API_KEY", ""),
        "financialjuice_url": os.getenv(
            "FINANCIALJUICE_URL",
            "https://www.financialjuice.com/feed.ashx?c=market",
        ),

        # ── Symbols ───────────────────────────────────────────────────────────
        "stock_symbols":      ["AAPL", "TSLA", "GME", "AMC", "BBBY"],
        "crypto_symbols":     ["BTC/USD", "ETH/USD", "SOL/USD"],
        "forex_pairs":        ["EUR/USD", "GBP/USD", "USD/JPY"],
        "futures_contracts":  ["ES1!"],

        # ── Risk ──────────────────────────────────────────────────────────────
        "base_risk_pct":    0.01,   # 1% of equity per trade (paper trading default)
        "kelly_lookback":   50,     # trades to include in Kelly calculation
        "avg_win_r":        1.5,    # expected win in R-multiples
        "avg_loss_r":       1.0,    # expected loss in R-multiples

        # ── Strategy thresholds ───────────────────────────────────────────────
        "min_sp_score":            0.60,   # minimum Sp to act on a squeeze
        "btc_inflow_threshold":    1_000_000,  # USD — risk-off trigger
        "sol_memecoin_spike_threshold": 50_000_000,  # USD 24h vol

        # ── Polling intervals (seconds) ───────────────────────────────────────
        "news_poll_seconds":    30,
        "strategy_interval_seconds": 15,
        "whale_poll_seconds":   60,
        "finviz_poll_seconds":  300,
        "dune_poll_seconds":    300,
        "futures_poll_seconds": 60,

        # ── News / veto ───────────────────────────────────────────────────────
        "veto_cooldown_seconds": 600,   # 10 minutes of silence after macro event

        # ── Dune query IDs ────────────────────────────────────────────────────
        "dune_memecoin_query_id": "3152691",
    }
