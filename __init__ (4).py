"""
chimera/oms/order_manager.py
Order Management System — the Executor Layer of Project Chimera.

Responsibilities:
  1. Consume RiskParameters from the order queue
  2. Run pre-flight checks (PreflightChecker)
  3. Submit bracket orders to Alpaca (entry + stop-loss + take-profit)
  4. Listen to Alpaca's order-update WebSocket for fill events
  5. Manage trailing stops on open positions (tick-by-tick via price stream)
  6. Close positions, record outcomes, notify RiskAgent for Kelly updates
  7. Persist every lifecycle event to SQLite (TradeLogger)

Paper trading note:
  Set mode="paper" in config to use Alpaca's paper endpoints.
  The code is identical — only the base URLs differ.
  NEVER skip paper trading validation before going live.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, TYPE_CHECKING

import aiohttp

from chimera.oms.models import Order, OrderSide, OrderStatus, CloseReason
from chimera.oms.preflight import PreflightChecker, PreflightError
from chimera.oms.trailing_stop import TrailingStopManager
from chimera.oms.trade_logger import TradeLogger
from chimera.utils.state import RiskParameters, SharedState
from chimera.utils.logger import setup_logger

if TYPE_CHECKING:
    pass

log = setup_logger("oms")

# ── Alpaca endpoint sets ───────────────────────────────────────────────────────
ENDPOINTS = {
    "paper": {
        "rest": "https://paper-api.alpaca.markets",
        "stream": "wss://paper-api.alpaca.markets/stream",
        "data":   "wss://stream.data.alpaca.markets/v2/iex",
    },
    "live": {
        "rest": "https://api.alpaca.markets",
        "stream": "wss://api.alpaca.markets/stream",
        "data":   "wss://stream.data.alpaca.markets/v2/iex",
    },
}


class OrderManager:
    """
    Top-level OMS class. Instantiated by Mainframe and run as an asyncio task.
    """

    def __init__(self, state: SharedState, config: dict[str, Any]):
        self.state  = state
        self.config = config
        self.mode   = config.get("mode", "paper")
        self.ep     = ENDPOINTS[self.mode]

        self._preflight = PreflightChecker(state, config)
        self._trailing  = TrailingStopManager(config)
        self._logger    = TradeLogger(config.get("db_path", "chimera_trades.db"))

        self._headers = {
            "APCA-API-KEY-ID":     config["alpaca_key"],
            "APCA-API-SECRET-KEY": config["alpaca_secret"],
            "Content-Type":        "application/json",
        }

        # symbol → Order (only filled/open positions)
        self._open: dict[str, Order] = {}

    async def run(self) -> None:
        log.info(f"OrderManager started — mode={self.mode.upper()}")
        if self.mode == "live":
            log.warning("=" * 55)
            log.warning("  LIVE MODE — real capital at risk")
            log.warning("=" * 55)

        # Sync equity from Alpaca on boot
        await self._sync_equity()

        await asyncio.gather(
            self._consume_order_queue(),
            self._stream_order_updates(),
            self._trailing_stop_loop(),
        )

    # ══════════════════════════════════════════════════════════════════════════
    # 1. Consume RiskParameters → build Order → pre-flight → submit
    # ══════════════════════════════════════════════════════════════════════════

    async def _consume_order_queue(self) -> None:
        while True:
            try:
                rp: RiskParameters = await asyncio.wait_for(
                    self.state.order_queue.get(), timeout=5.0
                )
                await self._process_risk_params(rp)
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                log.exception(f"OMS consume error: {e}")

    async def _process_risk_params(self, rp: RiskParameters) -> None:
        order = self._build_order(rp)

        # Pre-flight
        try:
            self._preflight.run(order)
        except PreflightError as e:
            log.warning(f"Order REJECTED [{order.symbol}]: {e}")
            order.status = OrderStatus.REJECTED
            await asyncio.to_thread(self._logger.log_order, order, {"rejection": str(e)})
            return

        # Submit
        await self._submit_bracket(order)

    def _build_order(self, rp: RiskParameters) -> Order:
        from chimera.utils.state import Sentiment
        side = (
            OrderSide.BUY
            if self.state.market.stocks.get(rp.symbol, {}).get("direction", "long") != "short"
            else OrderSide.SELL
        )
        # Determine side from the latest signal direction in state
        for sig in reversed(self.state.signals):
            if sig.symbol == rp.symbol:
                side = OrderSide.BUY if sig.direction == "long" else OrderSide.SELL
                break

        return Order(
            symbol        = rp.symbol,
            sector        = next(
                (s.sector for s in self.state.signals if s.symbol == rp.symbol),
                "unknown",
            ),
            side          = side,
            qty           = rp.position_size,
            entry_price   = rp.entry_price,
            stop_price    = rp.stop_price,
            initial_stop  = rp.stop_price,
            take_profit   = rp.take_profit,
            atr           = next(
                (s.atr for s in self.state.signals if s.symbol == rp.symbol),
                0.0,
            ),
            kelly_fraction  = rp.kelly_fraction,
            news_multiplier = self.state.news_multiplier(),
        )

    # ══════════════════════════════════════════════════════════════════════════
    # 2. Alpaca bracket order submission
    # ══════════════════════════════════════════════════════════════════════════

    async def _submit_bracket(self, order: Order) -> None:
        """
        Submit a bracket order: one market entry with two contingent legs
        (stop-loss and take-profit). Alpaca manages the child orders
        automatically — if either leg fills, it cancels the other.
        """
        payload = {
            "symbol":        order.symbol,
            "qty":           str(round(order.qty, 4)),
            "side":          order.side.value,
            "type":          "market",
            "time_in_force": "day",
            "client_order_id": order.client_order_id,
            "order_class":   "bracket",
            "stop_loss":     {"stop_price": str(round(order.stop_price, 4))},
            "take_profit":   {"limit_price": str(round(order.take_profit, 4))},
        }

        url = f"{self.ep['rest']}/v2/orders"
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                headers=self._headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                body = await resp.json()

        if resp.status not in (200, 201):
            log.error(f"Alpaca rejected order for {order.symbol}: {body}")
            order.status = OrderStatus.REJECTED
            await asyncio.to_thread(self._logger.log_order, order, body)
            return

        order.alpaca_order_id = body.get("id")
        order.status          = OrderStatus.SUBMITTED
        order.submitted_at    = datetime.utcnow()

        # Parse child order IDs from the bracket legs
        for leg in body.get("legs", []):
            if leg.get("type") == "stop":
                order.bracket_stop_id = leg["id"]
            elif leg.get("type") in ("limit", "take_profit"):
                order.bracket_tp_id = leg["id"]

        log.info(
            f"Submitted [{self.mode.upper()}] {order.side.value.upper()} "
            f"{order.qty} {order.symbol} | "
            f"stop={order.stop_price:.4f} TP={order.take_profit:.4f} | "
            f"alpaca_id={order.alpaca_order_id}"
        )
        await asyncio.to_thread(self._logger.log_order, order)

    # ══════════════════════════════════════════════════════════════════════════
    # 3. WebSocket order update stream — fill tracking
    # ══════════════════════════════════════════════════════════════════════════

    async def _stream_order_updates(self) -> None:
        import websockets

        while True:
            try:
                async with websockets.connect(
                    self.ep["stream"],
                    extra_headers=self._headers,
                ) as ws:
                    # Authenticate
                    await ws.send(json.dumps({
                        "action": "auth",
                        "key":    self.config["alpaca_key"],
                        "secret": self.config["alpaca_secret"],
                    }))
                    # Subscribe to trade updates
                    await ws.send(json.dumps({
                        "action": "listen",
                        "data":   {"streams": ["trade_updates"]},
                    }))

                    async for raw in ws:
                        msg = json.loads(raw)
                        await self._handle_order_event(msg)

            except Exception as e:
                log.warning(f"Order stream disconnected: {e} — reconnecting in 5s")
                await asyncio.sleep(5)

    async def _handle_order_event(self, msg: dict) -> None:
        """Process a single order-update event from Alpaca."""
        stream = msg.get("stream")
        if stream != "trade_updates":
            return

        data  = msg.get("data", {})
        event = data.get("event")
        ao    = data.get("order", {})

        # Find our Order by client_order_id
        client_id = ao.get("client_order_id", "")
        order = self._find_order_by_client_id(client_id)
        if order is None:
            return   # not our order (could be a bracket child)

        if event == "fill":
            await self._on_fill(order, ao)
        elif event == "partial_fill":
            await self._on_partial_fill(order, ao)
        elif event in ("canceled", "expired"):
            await self._on_cancelled(order, ao)
        elif event in ("stopped", "rejected"):
            order.status = OrderStatus.REJECTED
            log.warning(f"Order {order.symbol} {event}: {ao}")
            await asyncio.to_thread(self._logger.log_order, order)

    async def _on_fill(self, order: Order, ao: dict) -> None:
        order.fill_price = float(ao.get("filled_avg_price", order.entry_price))
        order.qty        = float(ao.get("filled_qty", order.qty))
        order.status     = OrderStatus.FILLED
        order.filled_at  = datetime.utcnow()

        self._open[order.symbol]             = order
        self.state.open_positions[order.symbol] = order

        log.info(
            f"FILLED {order.side.value.upper()} {order.qty} {order.symbol} "
            f"@ {order.fill_price:.4f}"
        )
        await asyncio.to_thread(self._logger.log_order, order)

    async def _on_partial_fill(self, order: Order, ao: dict) -> None:
        order.fill_price = float(ao.get("filled_avg_price", order.entry_price))
        order.qty        = float(ao.get("filled_qty", order.qty))
        order.status     = OrderStatus.PARTIALLY_FILLED
        log.info(f"PARTIAL FILL {order.symbol} qty={order.qty}")
        await asyncio.to_thread(self._logger.log_order, order)

    async def _on_cancelled(self, order: Order, ao: dict) -> None:
        order.status = OrderStatus.CANCELLED
        self._open.pop(order.symbol, None)
        self.state.open_positions.pop(order.symbol, None)
        log.info(f"Order {order.symbol} cancelled.")
        await asyncio.to_thread(self._logger.log_order, order)

    # ══════════════════════════════════════════════════════════════════════════
    # 4. Trailing stop loop — runs every N seconds for all open positions
    # ══════════════════════════════════════════════════════════════════════════

    async def _trailing_stop_loop(self) -> None:
        interval = self.config.get("trailing_stop_interval_seconds", 10)
        while True:
            await asyncio.sleep(interval)
            if not self._open:
                continue
            for symbol, order in list(self._open.items()):
                current_price = self._current_price(order)
                if current_price <= 0:
                    continue

                order.update_unrealised(current_price)

                # Check stop hit
                if self._trailing.stop_hit(order, current_price):
                    await self._close_position(order, current_price, CloseReason.TRAILING_STOP)
                    continue

                # Check TP hit
                if self._trailing.tp_hit(order, current_price):
                    await self._close_position(order, current_price, CloseReason.TP_HIT)
                    continue

                # Ratchet trailing stop
                new_stop = self._trailing.evaluate(order, current_price)
                if new_stop:
                    await self._update_stop_on_alpaca(order, new_stop)
                    order.stop_price = new_stop

    def _current_price(self, order: Order) -> float:
        """Pull latest close from shared state market data."""
        sector_map = {
            "crypto":  self.state.market.crypto,
            "stocks":  self.state.market.stocks,
            "forex":   self.state.market.forex,
            "futures": self.state.market.futures,
        }
        bars = sector_map.get(order.sector, {}).get(order.symbol, {})
        closes = bars.get("close", [])
        return float(closes[-1]) if closes else 0.0

    async def _update_stop_on_alpaca(self, order: Order, new_stop: float) -> None:
        """
        Patch the child stop-loss order on Alpaca with the new stop price.
        Alpaca bracket children are accessible by their order ID.
        """
        if not order.bracket_stop_id:
            return
        url = f"{self.ep['rest']}/v2/orders/{order.bracket_stop_id}"
        payload = {"stop_price": str(round(new_stop, 4))}
        async with aiohttp.ClientSession() as session:
            async with session.patch(
                url,
                headers=self._headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status not in (200, 201):
                    body = await resp.text()
                    log.warning(f"Failed to update stop for {order.symbol}: {body}")
                else:
                    log.info(f"Trailing stop updated [{order.symbol}]: {new_stop:.4f}")

    # ══════════════════════════════════════════════════════════════════════════
    # 5. Position close + Kelly outcome reporting
    # ══════════════════════════════════════════════════════════════════════════

    async def _close_position(
        self,
        order: Order,
        exit_price: float,
        reason: CloseReason,
    ) -> None:
        """
        Mark position closed, compute P&L, report to RiskAgent for Kelly
        history update, and remove from open positions.
        """
        if order.side == OrderSide.BUY:
            order.realised_pnl = (exit_price - order.fill_price) * order.qty
        else:
            order.realised_pnl = (order.fill_price - exit_price) * order.qty

        order.compute_r_multiple()
        order.status       = OrderStatus.CLOSED
        order.close_reason = reason
        order.closed_at    = datetime.utcnow()

        # Remove from open positions
        self._open.pop(order.symbol, None)
        self.state.open_positions.pop(order.symbol, None)

        log.info(
            f"CLOSED {order.symbol} [{reason.value}] "
            f"PnL=${order.realised_pnl:.2f} R={order.r_multiple:.2f}"
        )

        # Report outcome to RiskAgent for Kelly Criterion update
        from chimera.agents.risk_agent import RiskAgent  # avoid circular import
        # We post a message onto a dedicated callback queue instead of direct call
        await self.state.signal_queue.put(
            _TradeOutcome(symbol=order.symbol, won=order.realised_pnl > 0)
        )

        await asyncio.to_thread(
            self._logger.log_order, order, {"exit_price": exit_price}
        )

    # ══════════════════════════════════════════════════════════════════════════
    # Utilities
    # ══════════════════════════════════════════════════════════════════════════

    def _find_order_by_client_id(self, client_id: str) -> Order | None:
        for order in self._open.values():
            if order.client_order_id == client_id:
                return order
        return None

    async def _sync_equity(self) -> None:
        """Pull account equity from Alpaca on boot."""
        url = f"{self.ep['rest']}/v2/account"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers=self._headers,
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    data = await resp.json()
            equity = float(data.get("equity", 0))
            self.state.equity = equity
            log.info(f"Account equity synced: ${equity:,.2f} [{self.mode}]")
        except Exception as e:
            log.warning(f"Failed to sync equity: {e}")

    async def force_close_all(self, reason: CloseReason = CloseReason.MANUAL) -> None:
        """Emergency close all open positions. Call on shutdown or circuit breaker."""
        log.warning(f"FORCE CLOSE ALL positions — reason: {reason.value}")
        for symbol, order in list(self._open.items()):
            price = self._current_price(order)
            if price > 0:
                await self._close_position(order, price, reason)
            # Also cancel on Alpaca
            url = f"{self.ep['rest']}/v2/positions/{symbol}"
            async with aiohttp.ClientSession() as session:
                await session.delete(url, headers=self._headers)


class _TradeOutcome:
    """Lightweight message posted to the signal queue for Kelly updates."""
    def __init__(self, symbol: str, won: bool):
        self.symbol = symbol
        self.won    = won
