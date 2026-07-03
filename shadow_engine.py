"""Shadow execution engine for Predict.Fun dry run.

Simulates the Polymarket order lifecycle and dump manager without placing real
orders. Computes intended BUY prices, simulates fills when the market crosses,
and computes dump/unwind prices with the same slippage-floor logic used in the
Polymarket bot.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from models import MarketState, OrderSlot

log = logging.getLogger("predict_fun")

BUY = 0
SELL = 1


@dataclass
class ShadowOrder:
    side: str
    price: float
    shares: float
    placed_at: float
    filled: bool = False
    fill_price: float | None = None
    fill_time: float | None = None


@dataclass
class SimulatedPosition:
    shares: float = 0.0
    avg_price: float = 0.0

    def record_fill(self, shares: float, price: float) -> None:
        if shares <= 0:
            return
        total_cost = self.shares * self.avg_price + shares * price
        self.shares += shares
        self.avg_price = total_cost / self.shares if self.shares > 0 else 0.0

    def record_unwind(self, shares: float) -> None:
        self.shares = max(0.0, self.shares - shares)
        if self.shares == 0:
            self.avg_price = 0.0


class ShadowEngine:
    """Pure-simulation trading engine."""

    def __init__(self, max_slippage_frac: float = 0.05):
        self.max_slippage_frac = max_slippage_frac
        self.positions: dict[int, dict[str, SimulatedPosition]] = {}
        self.orders: dict[int, list[ShadowOrder]] = {}

    def _slippage_floor_frac(self, elapsed_min: float) -> float:
        """Time-bucketed floor matching Polymarket FX-APB."""
        if elapsed_min < 5.0:
            return 0.02
        if elapsed_min < 15.0:
            return 0.035
        return self.max_slippage_frac

    def compute_buy_price(self, ms: MarketState, side: str, ticks_inside: int = 1) -> float | None:
        """Compute a buy price one tick inside the best quote."""
        book = ms.cached_book
        if not book:
            return None
        if side == "yes":
            best_bid = book["bids"][0]["price"] if book["bids"] else 0.0
            best_ask = book["asks"][0]["price"] if book["asks"] else 0.0
        else:
            # NO side: derive from YES book.
            best_bid = round(1.0 - book["asks"][0]["price"], ms.decimal_precision) if book["asks"] else 0.0
            best_ask = round(1.0 - book["bids"][0]["price"], ms.decimal_precision) if book["bids"] else 0.0

        if best_bid <= 0 or best_ask <= 0:
            return None
        spread = best_ask - best_bid
        if spread > ms.max_spread:
            log.debug(f"{ms.market_id}: spread {spread:.4f} > max {ms.max_spread}")
            return None
        # Quote midpoint and move ticks inside from best bid.
        mid = (best_bid + best_ask) / 2.0
        target = round(mid - ticks_inside * ms.tick_size, ms.decimal_precision)
        target = max(target, best_bid + ms.tick_size)
        target = min(target, best_ask - ms.tick_size)
        return max(ms.tick_size, target)

    def place_shadow_order(self, ms: MarketState, side: str, shares: float) -> ShadowOrder | None:
        price = self.compute_buy_price(ms, side)
        if price is None:
            return None
        order = ShadowOrder(side=side, price=price, shares=shares, placed_at=time.time())
        self.orders.setdefault(ms.market_id, []).append(order)
        log.info(
            f"[SHADOW] would place BUY {side.upper()} {shares:.2f}sh @ {price:.4f} "
            f"| {ms.question[:40]}"
        )
        return order

    def simulate_fill(self, ms: MarketState) -> list[dict[str, Any]]:
        """Check pending shadow BUY orders against current book and simulate fills."""
        fills: list[dict[str, Any]] = []
        for order in self.orders.get(ms.market_id, []):
            if order.filled or order.side not in {"yes", "no"}:
                continue
            book = ms.cached_book
            if not book:
                continue
            # Fill if best ask <= order price (we'd be hit).
            if order.side == "yes":
                best_ask = book["asks"][0]["price"] if book["asks"] else 1.0
                if best_ask <= order.price:
                    order.filled = True
                    order.fill_price = best_ask
                    order.fill_time = time.time()
                    pos = self.positions.setdefault(ms.market_id, {}).setdefault(order.side, SimulatedPosition())
                    pos.record_fill(order.shares, best_ask)
                    fills.append({"market_id": ms.market_id, "side": order.side, "shares": order.shares, "price": best_ask})
                    log.info(f"[SHADOW] filled BUY {order.side.upper()} {order.shares:.2f}sh @ {best_ask:.4f}")
            else:
                # NO side: fill if YES best bid >= order NO price equivalent.
                best_bid = book["bids"][0]["price"] if book["bids"] else 0.0
                no_equivalent = round(1.0 - best_bid, ms.decimal_precision)
                if no_equivalent <= order.price:
                    order.filled = True
                    order.fill_price = no_equivalent
                    order.fill_time = time.time()
                    pos = self.positions.setdefault(ms.market_id, {}).setdefault(order.side, SimulatedPosition())
                    pos.record_fill(order.shares, no_equivalent)
                    fills.append({"market_id": ms.market_id, "side": order.side, "shares": order.shares, "price": no_equivalent})
                    log.info(f"[SHADOW] filled BUY {order.side.upper()} {order.shares:.2f}sh @ {no_equivalent:.4f}")
        return fills

    def compute_dump_price(self, ms: MarketState, side: str) -> float | None:
        """Compute a shadow dump price with slippage floor."""
        pos = self.positions.get(ms.market_id, {}).get(side)
        if not pos or pos.shares <= 0:
            return None
        book = ms.cached_book
        if not book:
            return None

        elapsed_min = 0.0  # assume immediate dump for simulation
        floor_frac = self._slippage_floor_frac(elapsed_min)
        cost_basis = pos.avg_price
        slip_floor = round(cost_basis * (1.0 - floor_frac), ms.decimal_precision)

        if side == "yes":
            best_bid = book["bids"][0]["price"] if book["bids"] else 0.0
        else:
            best_bid = round(1.0 - (book["asks"][0]["price"] if book["asks"] else 1.0), ms.decimal_precision)

        if best_bid <= 0:
            return None
        sell_price = max(best_bid, slip_floor)
        if sell_price < slip_floor:
            sell_price = slip_floor
        return sell_price

    def simulate_dump(self, ms: MarketState, side: str) -> dict[str, Any] | None:
        price = self.compute_dump_price(ms, side)
        if price is None:
            return None
        pos = self.positions[ms.market_id][side]
        log.info(
            f"[SHADOW] would dump SELL {side.upper()} {pos.shares:.2f}sh @ {price:.4f} "
            f"(cost {pos.avg_price:.4f}) | {ms.question[:40]}"
        )
        return {"market_id": ms.market_id, "side": side, "shares": pos.shares, "price": price}
