"""
chimera/oms/models.py
Order lifecycle dataclasses and status enums.

Every object that moves through the OMS is typed here.
The OMS never passes raw dicts — only these typed objects.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class OrderSide(str, Enum):
    BUY  = "buy"
    SELL = "sell"


class OrderStatus(str, Enum):
    PENDING     = "pending"       # created locally, not yet sent
    SUBMITTED   = "submitted"     # accepted by Alpaca, awaiting fill
    PARTIALLY_FILLED = "partially_filled"
    FILLED      = "filled"        # fully filled — position is open
    CANCELLED   = "cancelled"
    REJECTED    = "rejected"
    CLOSED      = "closed"        # position exited (stop or TP hit)
    ERROR       = "error"


class CloseReason(str, Enum):
    STOP_HIT        = "stop_hit"
    TP_HIT          = "tp_hit"
    TRAILING_STOP   = "trailing_stop"
    MANUAL          = "manual"
    VETO            = "veto_forced_close"
    ERROR           = "error"


@dataclass
class Order:
    """Represents a single trade from signal to close."""

    # Identity
    client_order_id: str = field(default_factory=lambda: f"chimera-{uuid.uuid4().hex[:12]}")
    alpaca_order_id: Optional[str] = None
    bracket_stop_id: Optional[str] = None    # Alpaca's child stop-loss order id
    bracket_tp_id:   Optional[str] = None    # Alpaca's child take-profit order id

    # Instrument
    symbol:  str = ""
    sector:  str = ""           # "crypto" | "stocks" | "forex" | "futures"
    side:    OrderSide = OrderSide.BUY

    # Sizing
    qty:         float = 0.0    # shares / contracts / units
    entry_price: float = 0.0    # estimated at signal time; actual set on fill
    fill_price:  float = 0.0    # actual fill price from Alpaca
    stop_price:  float = 0.0    # current stop (ratchets as trailing stop moves)
    initial_stop: float = 0.0   # original stop, kept for R-multiple calc
    take_profit:  float = 0.0
    atr:          float = 0.0   # ATR at time of entry — used by trailing stop

    # P&L
    realised_pnl:   float = 0.0
    unrealised_pnl: float = 0.0
    r_multiple:     float = 0.0   # realised_pnl / initial_risk_per_unit

    # Lifecycle
    status:       OrderStatus  = OrderStatus.PENDING
    close_reason: Optional[CloseReason] = None

    # Risk metadata
    kelly_fraction:  float = 0.0
    news_multiplier: float = 1.0
    sp_score:        float = 0.0

    # Timestamps
    created_at:   datetime = field(default_factory=datetime.utcnow)
    submitted_at: Optional[datetime] = None
    filled_at:    Optional[datetime] = None
    closed_at:    Optional[datetime] = None

    @property
    def is_open(self) -> bool:
        return self.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED)

    @property
    def initial_risk_usd(self) -> float:
        """Maximum loss if stop is hit at entry."""
        if self.fill_price <= 0 or self.initial_stop <= 0:
            return 0.0
        return abs(self.fill_price - self.initial_stop) * self.qty

    def update_unrealised(self, current_price: float) -> None:
        if self.fill_price <= 0 or self.qty <= 0:
            return
        if self.side == OrderSide.BUY:
            self.unrealised_pnl = (current_price - self.fill_price) * self.qty
        else:
            self.unrealised_pnl = (self.fill_price - current_price) * self.qty

    def compute_r_multiple(self) -> float:
        risk = self.initial_risk_usd
        if risk <= 0:
            return 0.0
        self.r_multiple = round(self.realised_pnl / risk, 3)
        return self.r_multiple
