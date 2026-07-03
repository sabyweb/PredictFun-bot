"""Data models for PredictFun bot.

Adapted from Polymarket MarketState; Predict.Fun uses numeric market IDs and
Yes-priced books.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class OrderSlot:
    """Tracked live order on one side."""

    order_id: str | None = None
    price: float = 0.0
    shares: float = 0.0
    placed_at: float = 0.0
    last_check: float = 0.0


@dataclass
class MarketState:
    """Runtime state for one Predict.Fun market."""

    market_id: int
    question: str
    yes_token_id: str
    no_token_id: str
    condition_id: str
    decimal_precision: int
    tick_size: float
    min_size: float
    max_spread: float
    fee_rate_bps: int
    is_neg_risk: bool
    is_yield_bearing: bool
    status: str = ""
    trading_status: str = ""
    end_date_iso: str | None = None
    game_start_time: str | None = None

    # Dynamic
    yes_price: float | None = None
    midpoint: float = 0.0
    cached_book: dict[str, Any] | None = None

    orders: dict[str, OrderSlot] = field(default_factory=lambda: {"yes": OrderSlot(), "no": OrderSlot()})
    dump_orders: dict[str, str | None] = field(default_factory=lambda: {"yes": None, "no": None})
    dump_state: dict[str, dict[str, Any] | None] = field(default_factory=lambda: {"yes": None, "no": None})

    def __hash__(self) -> int:
        return hash(self.market_id)
