"""
chimera/oms/trailing_stop.py
Trailing Stop Manager.

Ratchets the stop price upward (for longs) or downward (for shorts)
as price moves in our favour. Never moves the stop against us.

Strategy: ATR-based trailing — stop trails at (current_price - N×ATR).
Once the trade reaches 1R profit, the stop is moved to breakeven.
Once the trade reaches 2R profit, the stop trails at 1R profit (locked in).

This gives the trade room to breathe early, then protects gains as they grow.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chimera.oms.models import Order

log = logging.getLogger("chimera.oms.trailing_stop")

ATR_TRAIL_MULTIPLE = 2.0   # default: stop trails at 2×ATR behind price


class TrailingStopManager:
    """
    Called on every price tick for each open position.
    Returns the new stop price if it should be updated, else None.
    """

    def __init__(self, config: dict):
        self.atr_multiple = config.get("trailing_atr_multiple", ATR_TRAIL_MULTIPLE)
        self.breakeven_r  = config.get("breakeven_at_r", 1.0)   # move to BE at 1R
        self.lock_r       = config.get("lock_profit_at_r", 2.0) # lock profit at 2R

    def evaluate(self, order: "Order", current_price: float) -> float | None:
        """
        Evaluate whether the trailing stop should move.

        Returns:
            float: new stop price if it should be updated (always better than current)
            None:  no change required
        """
        from chimera.oms.models import OrderSide

        if not order.is_open:
            return None
        if order.fill_price <= 0 or order.atr <= 0:
            return None

        risk_per_unit = abs(order.fill_price - order.initial_stop)
        if risk_per_unit <= 0:
            return None

        r_current = self._r_multiple(order, current_price)

        if order.side == OrderSide.BUY:
            return self._trail_long(order, current_price, r_current, risk_per_unit)
        else:
            return self._trail_short(order, current_price, r_current, risk_per_unit)

    # ── Long trailing logic ───────────────────────────────────────────────────

    def _trail_long(
        self,
        order: "Order",
        price:  float,
        r:      float,
        risk:   float,
    ) -> float | None:
        atr = order.atr

        # Stage 3: trail at ATR×multiple behind price (standard trailing)
        trail_stop = price - atr * self.atr_multiple

        # Stage 2: lock in profit — stop never below fill + lock_r × risk
        if r >= self.lock_r:
            locked_floor = order.fill_price + self.lock_r * risk * 0.5
            trail_stop   = max(trail_stop, locked_floor)

        # Stage 1: at breakeven — stop never below fill price
        if r >= self.breakeven_r:
            trail_stop = max(trail_stop, order.fill_price)

        # Only ratchet up — never lower the stop on a long
        if trail_stop > order.stop_price:
            log.debug(
                f"Trail [{order.symbol}] long stop {order.stop_price:.4f}"
                f" → {trail_stop:.4f}  (R={r:.2f})"
            )
            return round(trail_stop, 6)

        return None

    # ── Short trailing logic ──────────────────────────────────────────────────

    def _trail_short(
        self,
        order: "Order",
        price:  float,
        r:      float,
        risk:   float,
    ) -> float | None:
        atr = order.atr

        trail_stop = price + atr * self.atr_multiple

        if r >= self.lock_r:
            locked_ceil = order.fill_price - self.lock_r * risk * 0.5
            trail_stop  = min(trail_stop, locked_ceil)

        if r >= self.breakeven_r:
            trail_stop = min(trail_stop, order.fill_price)

        # Only ratchet down — never raise the stop on a short
        if trail_stop < order.stop_price:
            log.debug(
                f"Trail [{order.symbol}] short stop {order.stop_price:.4f}"
                f" → {trail_stop:.4f}  (R={r:.2f})"
            )
            return round(trail_stop, 6)

        return None

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _r_multiple(order: "Order", current_price: float) -> float:
        from chimera.oms.models import OrderSide
        risk = abs(order.fill_price - order.initial_stop)
        if risk <= 0:
            return 0.0
        if order.side == OrderSide.BUY:
            return (current_price - order.fill_price) / risk
        return (order.fill_price - current_price) / risk

    def stop_hit(self, order: "Order", current_price: float) -> bool:
        """Returns True if the current price has crossed through the stop."""
        from chimera.oms.models import OrderSide
        if order.side == OrderSide.BUY:
            return current_price <= order.stop_price
        return current_price >= order.stop_price

    def tp_hit(self, order: "Order", current_price: float) -> bool:
        """Returns True if take-profit has been reached."""
        from chimera.oms.models import OrderSide
        if order.take_profit <= 0:
            return False
        if order.side == OrderSide.BUY:
            return current_price >= order.take_profit
        return current_price <= order.take_profit
