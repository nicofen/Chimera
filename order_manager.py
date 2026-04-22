"""
chimera/oms/trade_logger.py
Trade Logger — persists every order lifecycle event to SQLite.

Why SQLite and not Postgres/Redis?
- Zero infrastructure: runs locally alongside the mainframe
- Fast enough for swing/intraday volumes (not HFT tick storage)
- Easy to inspect with any SQL client or pandas

Schema is append-only: we INSERT rows, never UPDATE.
Each status transition is its own row — full audit trail.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chimera.oms.models import Order

log = logging.getLogger("chimera.oms.trade_logger")

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id   TEXT NOT NULL,
    alpaca_order_id   TEXT,
    symbol            TEXT NOT NULL,
    sector            TEXT,
    side              TEXT NOT NULL,
    qty               REAL,
    entry_price       REAL,
    fill_price        REAL,
    stop_price        REAL,
    initial_stop      REAL,
    take_profit       REAL,
    atr               REAL,
    realised_pnl      REAL,
    r_multiple        REAL,
    kelly_fraction    REAL,
    sp_score          REAL,
    news_multiplier   REAL,
    status            TEXT NOT NULL,
    close_reason      TEXT,
    created_at        TEXT,
    submitted_at      TEXT,
    filled_at         TEXT,
    closed_at         TEXT,
    metadata          TEXT    -- JSON blob for anything extra
);

CREATE INDEX IF NOT EXISTS idx_symbol   ON orders(symbol);
CREATE INDEX IF NOT EXISTS idx_status   ON orders(status);
CREATE INDEX IF NOT EXISTS idx_created  ON orders(created_at);
"""


class TradeLogger:
    """
    Thin wrapper around a SQLite connection.
    All writes are synchronous — called from async context via
    asyncio.to_thread() in the OMS to avoid blocking the event loop.
    """

    def __init__(self, db_path: str | Path = "chimera_trades.db"):
        self.db_path = Path(db_path)
        self._init_db()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)
        log.info(f"TradeLogger ready — {self.db_path.resolve()}")

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def log_order(self, order: "Order", metadata: dict | None = None) -> None:
        """
        Upsert an order snapshot.
        Each call writes the current state — full audit trail for every
        status transition.
        """
        row = {
            "client_order_id": order.client_order_id,
            "alpaca_order_id": order.alpaca_order_id,
            "symbol":          order.symbol,
            "sector":          order.sector,
            "side":            order.side.value,
            "qty":             order.qty,
            "entry_price":     order.entry_price,
            "fill_price":      order.fill_price,
            "stop_price":      order.stop_price,
            "initial_stop":    order.initial_stop,
            "take_profit":     order.take_profit,
            "atr":             order.atr,
            "realised_pnl":    order.realised_pnl,
            "r_multiple":      order.r_multiple,
            "kelly_fraction":  order.kelly_fraction,
            "sp_score":        order.sp_score,
            "news_multiplier": order.news_multiplier,
            "status":          order.status.value,
            "close_reason":    order.close_reason.value if order.close_reason else None,
            "created_at":      _fmt(order.created_at),
            "submitted_at":    _fmt(order.submitted_at),
            "filled_at":       _fmt(order.filled_at),
            "closed_at":       _fmt(order.closed_at),
            "metadata":        json.dumps(metadata) if metadata else None,
        }
        cols   = ", ".join(row.keys())
        placeholders = ", ".join("?" * len(row))
        with self._conn() as conn:
            conn.execute(
                f"INSERT INTO orders ({cols}) VALUES ({placeholders})",
                list(row.values()),
            )
        log.debug(f"Logged [{order.status.value}] {order.symbol} {order.client_order_id}")

    def get_open_orders(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM orders WHERE status IN ('filled','partially_filled') "
                "ORDER BY filled_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_closed_trades(self, limit: int = 100) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM orders WHERE status='closed' "
                "ORDER BY closed_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def daily_pnl_summary(self, date: str | None = None) -> dict:
        """
        Returns {total_pnl, win_rate, trade_count, avg_r} for a given date
        (defaults to today UTC).
        """
        date = date or datetime.utcnow().strftime("%Y-%m-%d")
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT realised_pnl, r_multiple FROM orders "
                "WHERE status='closed' AND closed_at LIKE ?",
                (f"{date}%",),
            ).fetchall()

        if not rows:
            return {"total_pnl": 0.0, "win_rate": 0.0, "trade_count": 0, "avg_r": 0.0}

        pnls = [r["realised_pnl"] for r in rows]
        rs   = [r["r_multiple"]   for r in rows]
        wins = sum(1 for p in pnls if p > 0)
        return {
            "total_pnl":   round(sum(pnls), 2),
            "win_rate":    round(wins / len(pnls), 3),
            "trade_count": len(pnls),
            "avg_r":       round(sum(rs) / len(rs), 3),
        }


def _fmt(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None
