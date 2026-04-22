"""
chimera/oms/preflight.py
Pre-flight validation layer — runs before every order is submitted.

If ANY check fails the order is rejected and logged; nothing reaches Alpaca.
This is the last safety net before real money moves.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, time as dtime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chimera.oms.models import Order
    from chimera.utils.state import SharedState

log = logging.getLogger("chimera.oms.preflight")


class PreflightError(Exception):
    """Raised when a pre-flight check fails. Message is the rejection reason."""


class PreflightChecker:
    """
    Runs a pipeline of checks. Each check raises PreflightError on failure.
    All checks must pass for the order to proceed.
    """

    def __init__(self, state: "SharedState", config: dict):
        self.state  = state
        self.config = config

    def run(self, order: "Order") -> None:
        """
        Run all pre-flight checks sequentially.
        Raises PreflightError with a reason string if any check fails.
        """
        self._check_veto(order)
        self._check_duplicate(order)
        self._check_position_limit(order)
        self._check_qty_positive(order)
        self._check_stop_sane(order)
        self._check_market_hours(order)
        self._check_equity_sufficient(order)

    # ── Individual checks ─────────────────────────────────────────────────────

    def _check_veto(self, order: "Order") -> None:
        if self.state.is_vetoed():
            raise PreflightError(
                f"News Agent veto active: {self.state.news.veto_reason[:80]}"
            )

    def _check_duplicate(self, order: "Order") -> None:
        """Reject if we already have an open position in the same symbol."""
        if order.symbol in self.state.open_positions:
            existing = self.state.open_positions[order.symbol]
            raise PreflightError(
                f"Duplicate: open position already exists for {order.symbol} "
                f"(entered @ {existing.fill_price:.4f})"
            )

    def _check_position_limit(self, order: "Order") -> None:
        max_positions = self.config.get("max_open_positions", 5)
        n_open = len(self.state.open_positions)
        if n_open >= max_positions:
            raise PreflightError(
                f"Position limit reached: {n_open}/{max_positions} slots used"
            )

    def _check_qty_positive(self, order: "Order") -> None:
        if order.qty <= 0:
            raise PreflightError(f"Invalid qty={order.qty} for {order.symbol}")

    def _check_stop_sane(self, order: "Order") -> None:
        """
        For a buy: stop must be below entry.
        For a sell: stop must be above entry.
        Also checks stop is not unreasonably far (> 10× ATR).
        """
        from chimera.oms.models import OrderSide
        if order.entry_price <= 0 or order.stop_price <= 0:
            raise PreflightError("Entry or stop price is zero")

        if order.side == OrderSide.BUY and order.stop_price >= order.entry_price:
            raise PreflightError(
                f"BUY stop {order.stop_price} must be below entry {order.entry_price}"
            )
        if order.side == OrderSide.SELL and order.stop_price <= order.entry_price:
            raise PreflightError(
                f"SELL stop {order.stop_price} must be above entry {order.entry_price}"
            )

        if order.atr > 0:
            stop_distance = abs(order.entry_price - order.stop_price)
            max_allowed   = order.atr * self.config.get("max_atr_stop_multiple", 5.0)
            if stop_distance > max_allowed:
                raise PreflightError(
                    f"Stop distance {stop_distance:.4f} exceeds max "
                    f"({max_allowed:.4f} = {self.config.get('max_atr_stop_multiple', 5)}×ATR)"
                )

    def _check_market_hours(self, order: "Order") -> None:
        """
        Only enforce hours for stock orders. Crypto and forex trade 24/7.
        Futures have CME session hours but we allow extended hours via config.
        """
        if order.sector not in ("stocks",):
            return
        if self.config.get("allow_extended_hours", False):
            return

        now_et = datetime.now(timezone.utc).astimezone(
            __import__("zoneinfo").ZoneInfo("America/New_York")
        )
        market_open  = dtime(9, 30)
        market_close = dtime(16, 0)
        weekday      = now_et.weekday()  # 0=Mon … 6=Sun

        if weekday >= 5:
            raise PreflightError("US equity market closed (weekend)")
        if not (market_open <= now_et.time() < market_close):
            raise PreflightError(
                f"US equity market closed (now {now_et.strftime('%H:%M')} ET)"
            )

    def _check_equity_sufficient(self, order: "Order") -> None:
        """Ensure the trade's max loss doesn't exceed remaining risk budget."""
        equity    = self.state.equity
        max_daily = equity * self.config.get("max_daily_loss_pct", 0.05)
        max_trade = equity * self.config.get("max_risk_per_trade", 0.02)

        risk = abs(order.entry_price - order.stop_price) * order.qty
        if risk > max_trade:
            raise PreflightError(
                f"Trade risk ${risk:.2f} exceeds per-trade max ${max_trade:.2f}"
            )

        # Cumulative daily loss guard
        daily_loss = self.state.open_positions.get("_daily_loss_usd", 0.0)
        if isinstance(daily_loss, (int, float)) and daily_loss + risk > max_daily:
            raise PreflightError(
                f"Adding this trade would exceed daily loss limit "
                f"(current=${daily_loss:.2f}, risk=${risk:.2f}, limit=${max_daily:.2f})"
            )
